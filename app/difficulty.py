"""Prompt difficulty classifier for the `auto` alias: a transparent, rule-based scorer.

Why rules and not a model: the router runs on EVERY `auto` request, so it must be fast, free and
deterministic, and every decision must be explainable in the request log (`route_reason`).

The idea: difficulty is a property of WHAT THE PROMPT ASKS FOR, not of how long it is.
  1. Separate the instruction from any payload: quoted spans and long or data-like sentences
     (pasted logs, rosters, emails) are ignored, so a long input with a trivial question stays
     trivial, and technical words inside pasted data do not count.
  2. Score the instruction with weighted signals:
       hard  (+): proofs, derivations, comparisons, design/plans, causal "why", estimation,
                  multi-part demands, algorithmic and systems vocabulary
       easy  (-): lookups, conversion, translation, extraction, light rewriting, one-line answers
  3. score >= COMPLEX_THRESHOLD -> "complex" (smart tier), else "simple" (fast tier).
     A tie-breaking bias is deliberate: below the threshold we pick the cheap tier.

Known limits (documented, and measured by the eval tool under tools/ on a held-out set): English only;
keyword-based, so hard prompts phrased without any of these cues (some word problems, puzzles,
open-ended ethics) can be missed; only the LAST user message is judged.
This module never sees the eval answer key.
"""

import re
from dataclasses import dataclass, field

COMPLEX_THRESHOLD = 2.0
VOCAB_CAP = 1.5  # technical vocabulary alone can add at most this much: it must stay BELOW the
# threshold, so jargon in the surrounding text can never decide a prompt without a hard ask
LONG_ASK_WORDS = 22


@dataclass(frozen=True)
class Signal:
    name: str
    pattern: re.Pattern
    weight: float  # positive = harder, negative = easier
    group: str | None = None  # signals in one group are capped together
    needs_payload: bool = False  # only counts when the prompt supplies text to work on


def _s(name: str, regex: str, weight: float, group: str | None = None, needs_payload: bool = False) -> Signal:
    return Signal(name, re.compile(regex, re.IGNORECASE), weight, group, needs_payload)


SIGNALS: list[Signal] = [
    # ---- harder -------------------------------------------------------------------------
    _s("proof or derivation", r"\b(prove|disprove|proof|derive|derivation|induction)\b", 3.0),
    _s(
        "reasoning requested",
        r"\b(show your (reasoning|work)|justify|justification|reason through|think through)\b",
        2.0,
    ),
    _s("estimation", r"\bestimate\b", 2.0),
    _s("assumptions requested", r"\bassumptions?\b", 1.0),
    _s(
        "comparison or evaluation",
        r"\b(compare|contrast|trade-?offs?|pros and cons|evaluate|assess|critique|versus|vs\.?)\b",
        2.0,
    ),
    _s("recommendation", r"\b(recommend|recommendation|which would you (choose|pick|use))\b", 2.0),
    _s(
        "causal or mechanism question",
        r"\b(why (can|could|would|does|do|might|is|are)|explain (why|how|what)|mechanisms?)\b",
        1.5,
    ),
    _s(
        "diagnosis",
        r"\b(root cause|most likely causes?|(what|which) (could|might|would) (explain|cause)|(causes?|reasons?) (of|for))\b",
        2.0,
    ),
    _s("design or planning", r"\b(design|architect(ure)?|plan|roadmap|strategy|migration)\b", 2.0),
    _s("algorithmic", r"\b(algorithm|complexity|recursi\w+|dynamic programming|O\([^)]*\)|in better than)\b", 2.0),
    _s(
        "non-trivial code task",
        r"\b(write|implement|refactor|build) (a|an|the)\b.{0,40}\b(function|class|parser|program|service|compiler|query)\b",
        1.0,
    ),
    _s(
        "multi-part demand",
        r"\b(and (explain|justify|recommend|discuss)|and give|how (you would|would you)|confirm each|in order of|give (two|three|four|five|several|\d+))\b",
        1.0,
    ),
    # vocabulary: counted once per distinct term, capped as a group
    _s(
        "systems vocabulary",
        r"\b(distributed|consistency|quorum|partition|replication|consensus|concurren\w+|deadlock|race condition|idempoten\w+|shard\w*|multi-tenant|scalab\w+|throughput|latency|zero-downtime|roll ?back)\b",
        1.0,
        "vocab",
    ),
    # ---- easier -------------------------------------------------------------------------
    _s(
        "lookup question",
        r"\b(what is|what's|what does .{0,40} mean|define|definition of|who (is|are|was)|when (is|was|did)|where (is|are|was))\b",
        -1.5,
    ),
    _s("conversion or translation", r"\b(translate|convert|format|capitali[sz]e|spell|abbreviate)\b", -2.0),
    _s("extraction", r"\b(extract|pull out|look up)\b", -2.0),
    _s(
        "light rewriting", r"\b(rewrite|rephrase|paraphrase|proofread|fix (the )?(typo|grammar)|polite|shorten)\b", -2.0
    ),
    # Summarising PROVIDED text is easy; "summarize the arguments for and against X" from
    # knowledge is a synthesis task, so this one only counts when there is a payload.
    _s("summarising provided text", r"\bsummari[sz]e\b", -2.0, needs_payload=True),
    _s(
        "short output requested",
        r"\b(one[- ]line|one sentence|one word|yes or no|briefly|tl;?dr|in a sentence)\b",
        -1.5,
    ),
    _s("yes/no about a number", r"\bis \d+ (divisible by|prime|even|odd)\b", -2.0),
]

ASK_STARTERS = frozenset(
    [
        "what",
        "whats",
        "why",
        "how",
        "who",
        "when",
        "where",
        "which",
        "is",
        "are",
        "can",
        "could",
        "should",
        "would",
        "do",
        "does",
        "did",
        "will",
        "explain",
        "prove",
        "design",
        "write",
        "list",
        "compare",
        "estimate",
        "draft",
        "convert",
        "translate",
        "extract",
        "rewrite",
        "summarize",
        "summarise",
        "describe",
        "give",
        "show",
        "derive",
        "create",
        "build",
        "implement",
        "plan",
        "recommend",
        "evaluate",
        "assess",
        "critique",
        "analyze",
        "analyse",
        "fix",
        "find",
        "identify",
        "format",
        "generate",
        "suggest",
        "tell",
        "calculate",
        "solve",
        "argue",
        "refactor",
        "optimize",
        "debug",
        "review",
        "outline",
    ]
)
QUOTED = re.compile(r"\"[^\"]{8,}\"|(?<!\w)'[^']{8,}'(?!\w)|\u201c[^\u201d]{8,}\u201d")
SENTENCE_SPLIT = re.compile(r"(?<=[.?!])\s+")
REFERS_TO_TEXT = re.compile(
    r"\b(following|below|above|this (text|paragraph|article|email|passage|message)|these)\b", re.IGNORECASE
)


@dataclass(frozen=True)
class Classification:
    tier: str  # "simple" or "complex"
    score: float
    reason: str
    matched: list[tuple[str, float]] = field(default_factory=list)


def last_user_text(messages: list[dict]) -> str:
    """Text of the last user message (content may be a string or a list of parts)."""
    for message in reversed(messages):
        if message.get("role") != "user":
            continue
        content = message.get("content")
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            return " ".join(p.get("text", "") for p in content if isinstance(p, dict) and p.get("type") == "text")
    return ""


def _is_data(sentence: str) -> bool:
    digits = sum(ch.isdigit() for ch in sentence)
    return (
        len(sentence) > 160
        or digits / max(len(sentence), 1) > 0.12
        or sentence.count(";") >= 2
        or sentence.count("=") >= 2
    )


def _is_ask(sentence: str) -> bool:
    first = re.split(r"[\s,:]+", sentence.strip().lower(), maxsplit=1)[0].strip("'\"")
    return sentence.rstrip().endswith("?") or first in ASK_STARTERS


def split_instruction(text: str) -> tuple[str, str, bool]:
    """Return (instruction, context, has_payload): the asking sentences, those plus short context
    sentences, and whether the prompt carries text to work on. The payload itself (quoted spans,
    long or data-like sentences) is dropped from the first two."""
    unquoted = QUOTED.sub(" ", text)
    sentences = [s.strip() for s in SENTENCE_SPLIT.split(unquoted) if s.strip()]
    asks = [s for s in sentences if _is_ask(s) and not (len(s) > 160 and s.count(";") >= 2)]
    context = [s for s in sentences if s not in asks and not _is_data(s)]
    if not asks:  # no recognisable ask: judge everything that is not data
        asks = [s for s in sentences if not _is_data(s)] or sentences[:1]
    has_payload = unquoted != text or any(_is_data(s) for s in sentences) or bool(REFERS_TO_TEXT.search(" ".join(asks)))
    return " ".join(asks), " ".join(asks + context), has_payload


def classify(messages: list[dict]) -> Classification:
    text = last_user_text(messages).strip()
    if not text:
        return Classification("simple", 0.0, "simple: no text to judge")

    instruction, context, has_payload = split_instruction(text)
    score, matched = 0.0, []
    group_totals: dict[str, float] = {}
    for signal in SIGNALS:
        if signal.needs_payload and not has_payload:
            continue
        scope = context if signal.group == "vocab" else instruction
        hits = {m.group(0).lower() for m in signal.pattern.finditer(scope)}
        if not hits:
            continue
        # one point per distinct vocabulary term; every other signal counts once
        weight = signal.weight * (len(hits) if signal.group else 1)
        if signal.group:
            room = VOCAB_CAP - group_totals.get(signal.group, 0.0)
            weight = max(min(weight, room), 0.0)
            group_totals[signal.group] = group_totals.get(signal.group, 0.0) + weight
        if weight:
            score += weight
            matched.append((signal.name, weight))

    if len(instruction.split()) >= LONG_ASK_WORDS:
        score += 1.0
        matched.append(("long multi-clause instruction", 1.0))

    tier = "complex" if score >= COMPLEX_THRESHOLD else "simple"
    shown = ", ".join(f"{name} {weight:+g}" for name, weight in matched) or "no strong signals"
    return Classification(tier, round(score, 2), f"{tier} (score {score:g}): {shown}", matched)