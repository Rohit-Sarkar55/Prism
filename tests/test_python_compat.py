"""The README promises Python 3.11 or newer. The one 3.12-only construct we could stumble into is an f-string
whose replacement field reuses the string's own quote character, and an auto-formatter can introduce it
silently. This scans every project file with the real tokenizer (meaningful on 3.12+, where the construct
becomes tokenizable; on 3.11 such a file would fail to import at all)."""

import io
import pathlib
import tokenize

from app.config import PROJECT_ROOT


def quote_of(token_string: str) -> str:
    body = token_string.lstrip("fFrRbBuU")
    return body[:3] if body[:3] in ('"""', "'''") else body[:1]


def nested_same_quote(path: pathlib.Path) -> list[int]:
    lines, stack = [], []
    for tok in tokenize.generate_tokens(io.StringIO(path.read_text(encoding="utf-8")).readline):
        name = tokenize.tok_name[tok.type]
        if name == "FSTRING_START":
            quote = quote_of(tok.string)
            if quote in stack:
                lines.append(tok.start[0])
            stack.append(quote)
        elif name == "FSTRING_END":
            stack.pop()
        elif name == "STRING" and stack and quote_of(tok.string) in stack:
            lines.append(tok.start[0])
    return lines


def test_no_file_uses_an_f_string_construct_that_needs_python_3_12():
    problems = {}
    for folder in ("app", "tools", "tests"):
        for path in sorted((PROJECT_ROOT / folder).rglob("*.py")):
            if lines := nested_same_quote(path):
                problems[str(path.relative_to(PROJECT_ROOT))] = lines
    assert not problems, f"3.12-only f-strings (the README says 3.11+): {problems}"


def test_the_scanner_itself_catches_the_construct(tmp_path):
    bad = tmp_path / "bad.py"
    bad.write_text("value = {'a': 1}\nline = f\"{value[\"a\"]}\"\nother = f'{value['a']}'\n", encoding="utf-8")
    assert nested_same_quote(bad) == [2, 3]
    fine = tmp_path / "fine.py"
    fine.write_text("value = {'a': 1}\nline = f\"{value['a']}\"\n", encoding="utf-8")
    assert nested_same_quote(fine) == []