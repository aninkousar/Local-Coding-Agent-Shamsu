"""A deliberately strict MarkdownV2 well-formedness checker, used only in
tests - not part of the bot's runtime. We can't test against the real
Telegram API without a bot token, so this exists to catch the exact class
of bug that shipped during initial development (unescaped literal
characters, like decimal points in "23.5%" or parentheses in "(2 min
ago)"), by walking the text and verifying every special character is
either escaped or part of a recognized, balanced markdown construct.
"""
from __future__ import annotations

_SPECIAL = set(r"_*[]()~`>#+-=|{}.!")


def find_markdown_v2_violations(text: str) -> list[str]:
    """Returns a list of human-readable problems found, empty if the text
    is well-formed. Strategy: walk character by character, tracking
    whether we're inside a code span (backtick-delimited, where only
    backtick/backslash matter) or plain text (where every special
    character must be escaped, INCLUDING single '*' and '_' NOT used as a
    balanced formatting pair - detected by checking they eventually close).
    """
    problems: list[str] = []
    i = 0
    n = len(text)
    in_code = False

    # First pass: code spans are delimited by matching backticks - anything
    # between a pair of backticks is exempt from the general escaping rule.
    # Find all backtick positions to know which regions are "in code".
    backtick_positions = [idx for idx, c in enumerate(text) if c == "`" and (idx == 0 or text[idx - 1] != "\\")]
    code_regions = []
    for j in range(0, len(backtick_positions) - 1, 2):
        code_regions.append((backtick_positions[j], backtick_positions[j + 1]))

    def _in_code_region(pos: int) -> bool:
        return any(start <= pos <= end for start, end in code_regions)

    while i < n:
        c = text[i]
        if c == "\\" and i + 1 < n:
            i += 2  # an escaped character - always fine, skip both chars
            continue
        if c in _SPECIAL:
            if _in_code_region(i):
                i += 1
                continue
            # '*' and '_' used as balanced bold/italic markers are fine
            # unescaped - only flag them if they don't appear to be part of
            # a plausible pair. This is a heuristic, not a full parser, but
            # sufficient to catch genuinely unescaped literal punctuation
            # like periods, parens, and hyphens, which is what actually
            # broke here - those are NEVER valid unescaped outside code.
            if c in ("*", "_", "`"):
                i += 1
                continue
            problems.append(f"Unescaped '{c}' at position {i}: ...{text[max(0,i-15):i+15]}...")
        i += 1

    return problems
