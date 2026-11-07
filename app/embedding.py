"""The cache's matcher: a documented LOCAL SUBSTITUTE for a sentence-embedding model.

An embedding model maps a prompt to a vector so that paraphrases land close together. This
module does the same job with no model download: it reduces a prompt to its canonical content
(stemmed content words, question type, identifier tokens) and compares those. It is fully
deterministic and testable offline. The `Embedder` shape (`embed` / `similarity`) is the seam
where a real embedding model would be swapped in; nothing else in the gateway knows how the
score is produced.

What it catches: rewordings that keep the same content words (frame words, word order and
punctuation do not matter):  "How do I reset my password on the dashboard?"  ==
"What are the steps to reset my dashboard password?".

What it deliberately refuses (a wrong cache hit serves the wrong answer, so it errs towards a miss):
  * identifier tokens (anything containing a digit: order numbers, versions, the random salts the
    test scripts use) must match EXACTLY, or the score is 0;
  * negation must agree: "cannot reset" is never "reset", "with alcohol" is never "without alcohol"
    (a hard block, not a soft penalty: in a long prompt one extra word barely moves a cosine score);
  * a different question type (how / why / what / yes-no ...) multiplies the score by 0.8;
  * "convert USD to EUR" is not "convert EUR to USD", and "A faster than B" is not "B faster than A":
    directed pairs around to / from / than / before / after must match.

What it cannot do (documented limitation, measured in the tests): paraphrases that share no content
words, e.g. "refund policy for annual plans" vs "can I get my money back for an annual plan".
That needs real semantics, i.e. an embedding model.
"""

import json
import math
import re
from dataclasses import dataclass, field

QTYPE_MISMATCH_FACTOR = 0.8

# Words that carry no content. Negations (not, no, never, without, ...) are deliberately NOT here.
STOPWORDS = frozenset(
    [
        "a",
        "an",
        "the",
        "is",
        "are",
        "was",
        "were",
        "be",
        "been",
        "being",
        "am",
        "do",
        "does",
        "did",
        "done",
        "doing",
        "have",
        "has",
        "had",
        "having",
        "i",
        "me",
        "my",
        "mine",
        "myself",
        "we",
        "us",
        "our",
        "ours",
        "you",
        "your",
        "yours",
        "he",
        "him",
        "his",
        "she",
        "her",
        "it",
        "its",
        "they",
        "them",
        "their",
        "this",
        "that",
        "these",
        "those",
        "there",
        "here",
        "to",
        "of",
        "in",
        "on",
        "at",
        "for",
        "with",
        "from",
        "by",
        "as",
        "into",
        "onto",
        "about",
        "over",
        "under",
        "and",
        "or",
        "but",
        "if",
        "so",
        "than",
        "then",
        "too",
        "very",
        "just",
        "also",
        "can",
        "could",
        "would",
        "should",
        "will",
        "shall",
        "may",
        "might",
        "must",
        "please",
        "tell",
        "give",
        "show",
        "lets",
        "what",
        "whats",
        "which",
        "who",
        "whom",
        "whose",
        "when",
        "where",
        "why",
        "how",
        "step",
        "steps",
        "instruction",
        "instructions",
        "procedure",
        "guide",
        "tutorial",
        "way",
        "ways",
    ]
)
DIRECTIONAL = frozenset({"to", "from", "into", "onto", "versus", "vs", "than", "before", "after"})
NEGATION_WORDS = frozenset(
    {"not", "no", "never", "without", "cannot", "cant", "dont", "wont", "neither", "nor", "none"}
)
TOKEN = re.compile(r"[a-z0-9]+(?:['\u2019][a-z]+)?")
LEADING_TAG = re.compile(r"^\s*\([^)]{1,40}\)\s*")  # "(abc123) ..." salts and tags
CONTRACTION_SUFFIX = re.compile(r"['\u2019](s|re|ve|ll|d|m)$")


def is_negation(token: str) -> bool:
    return token in NEGATION_WORDS or token.endswith(("n't", "n\u2019t"))


def stem(word: str) -> str:
    """A deliberately small suffix stripper: plans -> plan, policies -> policy, resetting -> reset."""
    if len(word) > 4 and word.endswith("ies"):
        return word[:-3] + "y"
    if len(word) > 5 and word.endswith("ing"):
        word = word[:-3]
        return word[:-1] if len(word) > 3 and word[-1] == word[-2] else word
    if len(word) > 4 and word.endswith("ed"):
        word = word[:-2]
        return word[:-1] if len(word) > 3 and word[-1] == word[-2] else word
    if len(word) > 3 and word.endswith("s") and not word.endswith(("ss", "us", "is")):
        return word[:-1]
    return word


def _tokens(text: str) -> list[str]:
    out = []
    for raw in TOKEN.findall(text.lower()):
        if raw.endswith(("n't", "n\u2019t")):
            out.append("not")
            continue
        word = CONTRACTION_SUFFIX.sub("", raw)
        if len(word) > 1 or word.isdigit():  # drop stray single letters ("e" in "e-mail")
            out.append(word)
    return out


def question_type(text: str) -> str:
    t = LEADING_TAG.sub("", text.lower())
    if re.search(r"\bwhy\b", t):
        return "why"
    if re.search(r"\bhow\b|\b(steps?|instructions?|procedure|tutorial|guide)\s+(to|for|on|of)\b|\bway to\b", t):
        return "how"
    for word in ("when", "where", "who", "which"):
        if re.search(rf"\b{word}\b", t):
            return word
    if re.match(r"^\W*(is|are|do|does|did|can|could|will|would|should|has|have|was|were)\b", t):
        return "yesno"
    if re.search(r"\bwhat\b|\bwhats\b", t):
        return "what"
    return "other"


@dataclass(frozen=True)
class Embedding:
    words: frozenset[str]  # stemmed content words
    qtype: str
    ids: frozenset[str]  # identifier-like tokens (contain a digit): must match exactly
    directed: frozenset[tuple[str, str, str]]  # (left, to/from/..., right) around directional words
    text_key: str  # all tokens in order: used to tell identical prompts apart when there are no content words
    extra: dict = field(default_factory=dict, compare=False)

    @property
    def negated(self) -> bool:
        """Derived from the stored tokens, so entries saved before this guard existed still work."""
        return any(is_negation(token) for token in self.text_key.split())

    def to_json(self) -> str:
        return json.dumps(
            {
                "v": 1,
                "w": sorted(self.words),
                "q": self.qtype,
                "i": sorted(self.ids),
                "d": sorted(self.directed),
                "k": self.text_key,
            }
        )

    @classmethod
    def from_json(cls, raw: str | bytes) -> "Embedding":
        data = json.loads(raw)
        return cls(
            frozenset(data["w"]),
            data["q"],
            frozenset(data["i"]),
            frozenset(tuple(t) for t in data["d"]),
            data["k"],
        )


class LexicalEmbedder:
    name = "lexical-content-words-v1"

    def embed(self, text: str) -> Embedding:
        tokens = _tokens(text)  # a leading "(salt)" tag stays in: it is an identifier and must match
        stems = [stem(t) for t in tokens]
        is_content = [t not in STOPWORDS and t not in DIRECTIONAL for t in tokens]
        words = frozenset(s for s, keep in zip(stems, is_content, strict=True) if keep)
        ids = frozenset(t for t in tokens if any(ch.isdigit() for ch in t))

        directed = set()
        for i, token in enumerate(tokens):
            if token not in DIRECTIONAL:
                continue
            # nearest content word on each side, looking past function words ("before THE tests")
            left = next((j for j in range(i - 1, max(i - 4, -1), -1) if is_content[j]), None)
            right = next((j for j in range(i + 1, min(i + 4, len(tokens))) if is_content[j]), None)
            if left is not None and right is not None:
                directed.add((stems[left], token, stems[right]))
        return Embedding(words, question_type(text), ids, frozenset(directed), " ".join(tokens))

    def similarity(self, a: Embedding, b: Embedding) -> float:
        """Score in [0, 1]. 1.0 means the same canonical content."""
        if a.ids != b.ids:
            return 0.0
        if a.negated != b.negated:
            return 0.0  # "with" vs "without", "can" vs "cannot": opposite questions, however many words they share
        if a.directed and b.directed and a.directed != b.directed:
            return 0.0
        if not a.words or not b.words:
            return 1.0 if a.text_key == b.text_key else 0.0
        cosine = len(a.words & b.words) / math.sqrt(len(a.words) * len(b.words))  # binary word vectors
        return cosine * (1.0 if a.qtype == b.qtype else QTYPE_MISMATCH_FACTOR)


TIME_SENSITIVE = re.compile(
    r"\b(current|currently|right now|now|today|tonight|tomorrow|yesterday|latest|newest|recent|recently|live|"
    r"real[- ]time|up[- ]to[- ]date|status of|this (morning|afternoon|evening|week|month|year)|breaking|"
    r"trending|as of|weather|stock price|exchange rate)\b",
    re.IGNORECASE,
)


def is_time_sensitive(text: str) -> bool:
    """Answers to these change with the clock, so caching them serves stale information."""
    return bool(TIME_SENSITIVE.search(text))