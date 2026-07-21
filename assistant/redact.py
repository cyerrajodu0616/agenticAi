"""First-pass regex PII redaction.

Runs BEFORE every LLM call (both backends). This is a starter pattern set, not a
substitute for a proper PII/NER pass — review before handling real traffic at scale.
The returned mapping must stay in process memory only; never persist or log it.
"""
import re

_PATTERNS: list[tuple[str, re.Pattern]] = [
    ("SSN", re.compile(r"\b\d{3}-\d{2}-\d{4}\b")),
    ("EMAIL", re.compile(r"\b[\w.+-]+@[\w-]+\.[\w.-]+\b")),
    ("PHONE", re.compile(r"\b(?:\+?1[\s.-]?)?\(?\d{3}\)?[\s.-]?\d{3}[\s.-]\d{4}\b")),
]


def redact(text: str) -> tuple[str, dict[str, str]]:
    mapping: dict[str, str] = {}
    for label, pattern in _PATTERNS:
        counter = 0

        def _sub(m: re.Match, label: str = label) -> str:
            nonlocal counter
            counter += 1
            placeholder = f"[REDACTED_{label}_{counter}]"
            mapping[placeholder] = m.group(0)
            return placeholder

        text = pattern.sub(_sub, text)
    return text, mapping
