"""Explicit offline demonstration provider, never selected for live questions."""

from __future__ import annotations

import json
from typing import Any

from evog.core.io import dumps
from evog.core.providers import ModelReply, ToolCall


class DemoProvider:
    """A deterministic fixture for trying the product flow without sending data."""

    def complete(self, messages: list[dict[str, Any]], tools: list[dict[str, Any]]) -> ModelReply:
        system = messages[0]["content"]
        if system.startswith("Reflect"):
            return ModelReply(
                content=dumps(
                    {
                        "searched": ["release"],
                        "evidence_found": "Two source records",
                        "unresolved": ["No observed confirmation beyond the announced schedule"],
                        "limitation": "This offline fixture reports subjective uncertainty",
                    }
                )
            )
        if "read-only experience analyst" in system or "ou are a analyze agent" in system:
            tool_messages = [message for message in messages if message["role"] == "tool"]
            request = json.loads(messages[1]["content"])
            if not tool_messages:
                target = next(
                    event for event in request["trace"]["events"] if event["kind"] == "tool"
                )
                return ModelReply(
                    tool_calls=[
                        ToolCall(
                            id="inspect-demo",
                            name="inspect_trace",
                            arguments={
                                "run_id": request["run_id"],
                                "event_index": target["index"],
                                "field_path": "/result",
                            },
                        )
                    ]
                )
            evidence = json.loads(tool_messages[-1]["content"])["evidence_ref"]
            return ModelReply(
                content=dumps(
                    {
                        "run_id": request["run_id"],
                        "query_type": "temporal update",
                        "category": "uncertainty",
                        "earliest_break": "Schedule retrieval without a completion confirmation",
                        "cause": "The observed search alone does not establish whether the plan was executed",
                        "condensed_rationale": "Distinguish announced schedules from confirmed completion",
                        "actionable_implication": "Verify status when answering questions about completed events",
                        "evidence": [evidence],
                        "uncertainty": "One synthetic experience; no causal claim",
                    }
                )
            )
        if "synthesize EvoGroup" in system:
            request = json.loads(messages[1]["content"])
            diagnosis = next(iter(request["buckets"].values()))[0]
            return ModelReply(
                content=dumps(
                    {
                        "findings": [
                            {
                                "id": "status-check",
                                "query_types": ["temporal update"],
                                "pattern": "A schedule is different evidence from completion",
                                "suggested_change": "Add a reusable status verification skill",
                                "evidence": diagnosis["evidence"],
                                "counterevidence": "No control in this fixture",
                                "uncertainty": "Only an offline demonstration",
                                "purpose": diagnosis["signal"],
                                "support_kind": "isolated",
                            }
                        ]
                    }
                )
            )
        if (
            "propose evidence-driven revisions" in system
            or "You are the evolution agent." in system
        ):
            request = json.loads(messages[1]["content"])
            findings = request.get("findings", [])
            if not findings:
                return ModelReply(
                    content="No supported change is justified by the supplied evidence."
                )
            finding_id = findings[0]["id"]
            selected = request.get("selected_run_ids", [])
            controls = request.get("control_run_ids", [])
            path = "skills/status-verification/SKILL.md"
            content = (
                "When asked about completed work, distinguish a proposed schedule from an observed "
                "completion. Read a later status record when available; otherwise state that "
                "completion is unconfirmed.\n"
            )
            history = str(messages)
            if "write-skill" not in history:
                return ModelReply(
                    tool_calls=[
                        ToolCall(
                            id="write-skill",
                            name="write_workspace",
                            arguments={"path": path, "content": content},
                        )
                    ]
                )
            if "register-skill" not in history:
                return ModelReply(
                    tool_calls=[
                        ToolCall(
                            id="register-skill",
                            name="register_change",
                            arguments={
                                "paths": [path],
                                "interface": "Policy",
                                "finding_ids": [finding_id],
                                "rationale": "Keep announced schedules separate from confirmed completion.",
                                "expected_effect": "Status questions will distinguish plans from observed events.",
                                "regression_risk": "Status questions may require one additional source read.",
                                "validation": "Run the status question fixture and verify uncertainty is retained.",
                                "predicted_fix_runs": selected[:1],
                                "risk_runs": controls[:1],
                            },
                        )
                    ]
                )
            if "validate-skill" not in history:
                return ModelReply(
                    tool_calls=[
                        ToolCall(id="validate-skill", name="validate_workspace", arguments={})
                    ]
                )
            return ModelReply(
                content="Added a status verification skill and validated the candidate workspace."
            )
        observed = [message for message in messages if message["role"] == "tool"]
        if not observed:
            return ModelReply(
                tool_calls=[
                    ToolCall(
                        id="search-demo",
                        name="grep_search",
                        arguments={"target": "memory_units", "query": "release"},
                    )
                ]
            )
        result = json.loads(observed[-1]["content"])
        records = [json.loads(match["excerpt"]) for match in result.get("matches", [])]
        if not records:
            return ModelReply(
                content=dumps(
                    {
                        "text": "No release schedule found in the demo records.",
                        "confidence": 0.2,
                        "status": "insufficient",
                        "citations": [],
                    }
                )
            )
        latest = records[-1]
        return ModelReply(
            content=dumps(
                {
                    "text": latest["text"],
                    "confidence": 0.5,
                    "status": "complete",
                    "citations": [latest["ref"]],
                }
            )
        )


DEMO_MESSAGES = [
    {
        "group_id": "demo-team",
        "message_id": "001",
        "sender": "User_1",
        "timestamp": "2026-01-05T09:00:00+08:00",
        "text": "The release is scheduled for January 12.",
    },
    {
        "group_id": "demo-team",
        "message_id": "002",
        "sender": "User_2",
        "timestamp": "2026-01-06T10:00:00+08:00",
        "text": "The release schedule has changed to January 15.",
        "reply_to": "001",
    },
]
