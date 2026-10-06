"""Official scoring prompts and verdict parsers for EverMemBench and GroupMemBench."""

from __future__ import annotations

import json
import re
import time
from threading import Event

from evog.core.errors import DeadlineExceeded, ProviderError
from evog.core.providers import Provider, complete_before
from evog.evaluation.data import Episode


def parse_mc_letter(response: str) -> str:
    """Normalize a multiple-choice response to an option letter when possible."""
    text = response.strip().replace("*", "").replace("`", "").replace("_", "").upper()
    if not text:
        return ""
    if len(text) == 1 and text in "ABCD":
        return text
    match = re.search(r"\b([ABCD])[.):,;、。，；\s]", text)
    if match:
        return match.group(1)
    match = re.search(r"(?:answer|choice|option|select)[:\s]+([ABCD])\b", text, re.IGNORECASE)
    if match:
        return match.group(1)
    if text[0] in "ABCD" and (len(text) == 1 or not text[1].isalpha()):
        return text[0]
    if text[-1] in "ABCD" and (len(text) == 1 or not text[-2].isalpha()):
        return text[-1]
    return text


def parse_evermem(content: str) -> bool | None:
    # Official parser permits an explanatory sentence or a JSON markdown fence.
    candidate = content
    if "```" in content:
        start = content.find("```json") + 7 if "```json" in content else content.find("```") + 3
        end = content.find("```", start)
        candidate = content[start:end] if end > start else content[start:]
    elif "{" in content and "}" in content:
        candidate = content[content.find("{") : content.rfind("}") + 1]
    try:
        payload = json.loads(candidate)
    except (ValueError, TypeError):
        return None
    if not isinstance(payload, dict):
        return None
    label = payload.get("label", "")
    if isinstance(label, dict):
        label = label.get("label", "")
    normalized = str(label).strip().upper()
    if normalized.startswith("CORRECT"):
        return True
    if normalized.startswith("WRONG") or normalized.startswith("INCORRECT"):
        return False
    return None


def parse_groupmem(content: str) -> bool | None:
    # The last Final:/Final answer: marker wins, matching the official eval_lib parser.
    lines = [line.strip() for line in content.splitlines() if line.strip()]
    final = lines[-1] if lines else ""
    for line in reversed(lines):
        if line.lower().startswith(("final:", "final answer:")):
            final = line.split(":", 1)[1].strip()
            break
    final = final.lower()
    if "incorrect" in final or "wrong" in final or "not correct" in final:
        return False
    if "correct" in final:
        return True
    return None


def score(
    provider: Provider,
    episode: Episode,
    answer: str,
    *,
    cancel_event: Event | None = None,
    timeout_seconds: float = 120,
) -> dict:
    if cancel_event is not None and cancel_event.is_set():
        return {"passed": None, "status": "cancelled", "usage": {}}
    if episode.options:
        produced, expected = parse_mc_letter(answer), parse_mc_letter(episode.gold)
        return {
            "mode": "multiple_choice",
            "passed": bool(produced) and produced == expected,
            "produced": produced,
            "expected": expected,
            "usage": {},
        }
    prefix = "EVERMEM" if episode.benchmark == "evermembench" else "GROUPMEM"
    system = globals()[prefix + "_OFFICIAL_JUDGE_SYSTEM_PROMPT"]
    template = globals()[prefix + "_OFFICIAL_JUDGE_USER_PROMPT"]
    user = template.format(
        question=episode.question, golden_answer=episode.gold, generated_answer=answer
    )
    parser = parse_evermem if prefix == "EVERMEM" else parse_groupmem
    usage: dict[str, int] = {}
    usage_calls: list[dict[str, int]] = []
    response_models: list[str] = []
    http_attempts = 0
    raw = ""
    deadline = time.monotonic() + timeout_seconds
    for attempt in range(1, 4):
        if cancel_event is not None and cancel_event.is_set():
            return {
                "passed": None,
                "status": "cancelled",
                "usage": usage,
                "usage_calls": usage_calls,
                "usage_complete": False,
            }
        try:
            reply = complete_before(
                provider,
                [{"role": "system", "content": system}, {"role": "user", "content": user}],
                [],
                deadline,
            )
        except (ProviderError, DeadlineExceeded) as exc:
            return {
                "mode": "official_llm_judge",
                "passed": None,
                "status": "judge_timeout"
                if isinstance(exc, DeadlineExceeded)
                else "judge_unavailable",
                "attempts": attempt,
                "usage": usage,
                "usage_calls": usage_calls,
                "usage_complete": False,
                "reported_http_attempts_before_failure": http_attempts,
            }
        raw = reply.content
        usage_calls.append(reply.usage)
        response_models.append(reply.response_model)
        http_attempts += reply.http_attempts
        for key, value in reply.usage.items():
            usage[key] = usage.get(key, 0) + value
        if cancel_event is not None and cancel_event.is_set():
            return {
                "passed": None,
                "status": "cancelled",
                "usage": usage,
                "usage_calls": usage_calls,
                "usage_complete": True,
            }
        passed = parser(raw)
        if passed is not None:
            return {
                "mode": "official_llm_judge",
                "passed": passed,
                "raw": raw,
                "attempts": attempt,
                "usage": usage,
                "usage_calls": usage_calls,
                "usage_complete": True,
                "http_attempts": http_attempts,
                "response_models": response_models,
            }
    return {
        "mode": "official_llm_judge",
        "passed": None,
        "status": "judge_unparseable",
        "raw": raw,
        "attempts": 3,
        "usage": usage,
        "usage_calls": usage_calls,
        "usage_complete": True,
        "http_attempts": http_attempts,
        "response_models": response_models,
    }


EVERMEM_OFFICIAL_JUDGE_SYSTEM_PROMPT = "You are an expert grader that determines if answers to questions match a gold standard answer.\n"

EVERMEM_OFFICIAL_JUDGE_USER_PROMPT = 'Your task is to label an answer to a question as \'CORRECT\' or \'WRONG\'. You will be given:\n    (1) a question (about a multi-person group chat),\n    (2) a \'gold\' (ground truth) answer,\n    (3) a generated answer\nwhich you will score as CORRECT/WRONG.\n\nThe questions are about events, facts, or details mentioned in multi-person group chat conversations.\nThe gold answer is usually a concise answer that includes the key information.\n\nFor example:\nQuestion: What project was announced on January 9th?\nGold answer: Carbon Emission Accounting Platform\n\nThe generated answer might be longer, but you should be generous with your grading -\nas long as it contains the same key information as the gold answer, it should be CORRECT.\n\nFor time-related questions, the gold answer will be a specific date/time.\nThe generated answer might use different formats (e.g., "May 7th" vs "7 May" vs "2025-05-07"),\nbut as long as it refers to the same date/time, it should be CORRECT.\n\nFor the specific window of date, a +/- 1 day difference is acceptable due to timezone processing variations.\n\nFor multiple choice questions where the gold answer is a letter (A/B/C/D),\nthe generated answer should match exactly to be CORRECT.\n\nNow grade this:\nQuestion: {question}\nGold answer: {golden_answer}\nGenerated answer: {generated_answer}\n\nFirst, provide a short (one sentence) explanation of your reasoning,\nthen finish with CORRECT or WRONG.\nDo NOT include both CORRECT and WRONG in your response.\n\nReturn the label in JSON format with the key "label": {{"label": "CORRECT"}} or {{"label": "WRONG"}}\n'

GROUPMEM_OFFICIAL_JUDGE_SYSTEM_PROMPT = "You are a strict judge evaluating whether an agent's answer matches the gold answer for a question.\nConsider paraphrases correct if they have the same meaning as the gold answer.\nFirst provide a brief reasoning paragraph. Then provide the final judgment on a new line using the format:\nFinal: Correct\nor\nFinal: Incorrect\n"

GROUPMEM_OFFICIAL_JUDGE_USER_PROMPT = (
    "Question:\n{question}\n\nGold Answer:\n{golden_answer}\n\nAgent Answer:\n{generated_answer}\n"
)
