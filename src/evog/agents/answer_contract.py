"""Stable text answer protocol used by the group agent.

The wire format is intentionally plain text so providers do not have to produce a
second, nested JSON object while answering.  The application stores the parsed
fields in its typed SQLite record after validation.
"""

from __future__ import annotations

import re
from typing import Any

FINAL_RE = re.compile(
    r"^[ \t]*FINAL ANSWER[ \t]*:[ \t]*(.*?)\s*(?=^[ \t]*CONFIDENCE[ \t]*:|\Z)",
    re.IGNORECASE | re.MULTILINE | re.DOTALL,
)
CONFIDENCE_RE = re.compile(
    r"^[ \t]*CONFIDENCE[ \t]*:[ \t]*([0-9]*\.?[0-9]+)[ \t]*$",
    re.IGNORECASE | re.MULTILINE,
)
BIAS_RE = re.compile(
    r"^[ \t]*ANSWER BIAS[ \t]*:[ \t]*(.*)$",
    re.IGNORECASE | re.MULTILINE | re.DOTALL,
)
CONFIDENCE_THRESHOLD = 0.5
MIN_BIAS_CHARS = 80


def parse_final_message(text: str) -> dict[str, Any]:
    body = str(text or "")
    final = FINAL_RE.search(body)
    confidence = CONFIDENCE_RE.search(body)
    bias = BIAS_RE.search(body)
    value = None
    if confidence:
        try:
            value = float(confidence.group(1))
        except ValueError:
            value = None
    return {
        "final_answer": final.group(1).strip() if final else None,
        "confidence": value,
        "answer_bias": bias.group(1).strip() if bias else "",
    }


def validate_final_message(text: str) -> list[str]:
    parsed = parse_final_message(text)
    problems: list[str] = []
    if not parsed["final_answer"]:
        problems.append("the message must include 'FINAL ANSWER: <answer>'.")
    confidence = parsed["confidence"]
    if confidence is None:
        problems.append("the message must include 'CONFIDENCE: <0..1>'.")
    elif not 0 <= float(confidence) <= 1:
        problems.append("CONFIDENCE must be between 0 and 1.")
    if confidence is not None and float(confidence) <= CONFIDENCE_THRESHOLD:
        if len(str(parsed["answer_bias"] or "")) < MIN_BIAS_CHARS:
            problems.append(
                "confidence at or below 0.5 requires an ANSWER BIAS block of at least 80 characters"
            )
    return problems


def answer_draft(text: str):
    """Parse the text protocol into the internal AnswerDraft shape."""
    from evog.core.models import AnswerDraft

    problems = validate_final_message(text)
    if problems:
        raise ValueError(" ".join(problems))
    parsed = parse_final_message(text)
    if not parsed["final_answer"] or parsed["confidence"] is None:
        raise ValueError("Answer must use FINAL ANSWER and CONFIDENCE")
    status = "complete"
    if str(parsed["final_answer"]).strip() == "没有相关的query内容":
        status = "insufficient"
    elif float(parsed["confidence"]) <= CONFIDENCE_THRESHOLD:
        status = "partial"
    return AnswerDraft(
        text=parsed["final_answer"],
        confidence=float(parsed["confidence"]),
        status=status,
        citations=[],
    )
