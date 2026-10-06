"""Component middleware constrained by immutable interaction contracts."""

from collections.abc import Callable

from evog.core.errors import ContractError
from evog.core.io import dumps, fingerprint
from evog.harness.executor import MiddlewarePipeline
from evog.harness.schema import Harness


class RuntimeMiddleware:
    def __init__(
        self,
        harness: Harness,
        event: Callable[[str, dict], object],
        *,
        deadline: float | None = None,
    ):
        self.pipeline = MiddlewarePipeline(harness)
        self.event = event
        self.deadline = deadline

    def run(self, hook: str, payload: dict) -> dict:
        result = self.pipeline.run(hook, payload, deadline=self.deadline)
        if result:
            self.event(
                "middleware",
                {
                    "hook": hook,
                    "changed_fields": sorted(result),
                    "input_fingerprint": fingerprint(payload),
                    "output_fingerprint": fingerprint(result),
                },
            )
        return result

    def before_model(self, messages: list[dict], max_chars: int, target_chars: int) -> list[dict]:
        result = self.run(
            "before_model",
            {"messages": messages, "max_chars": max_chars, "target_chars": target_chars},
        )
        candidate = result.get("messages", messages)
        if not isinstance(candidate, list) or candidate[:2] != messages[:2]:
            raise ContractError("Middleware must preserve the system and question anchors")
        # Compaction can remove complete exchanges, but cannot invent tool
        # results, change source text, or promote untrusted data into a role.
        original_index = 2
        for message in candidate[2:]:
            while original_index < len(messages) and messages[original_index] != message:
                original_index += 1
            if original_index == len(messages):
                raise ContractError("Middleware context must be an unchanged message subsequence")
            original_index += 1
        pending: set[str] = set()
        for message in candidate:
            if not isinstance(message, dict) or message.get("role") not in {
                "system",
                "user",
                "assistant",
                "tool",
            }:
                raise ContractError("Middleware returned invalid message roles")
            if message["role"] == "tool":
                call_id = message.get("tool_call_id")
                if call_id not in pending:
                    raise ContractError("Middleware orphaned a tool response")
                pending.remove(call_id)
            else:
                if pending:
                    raise ContractError("Middleware discarded a requested tool response")
                calls = message.get("tool_calls", [])
                if not isinstance(calls, list):
                    raise ContractError("Middleware returned invalid tool requests")
                for call in calls:
                    if not isinstance(call, dict) or not isinstance(call.get("id"), str):
                        raise ContractError("Middleware returned invalid tool IDs")
                    if call["id"] in pending:
                        raise ContractError("Middleware duplicated a tool ID")
                    pending.add(call["id"])
        if pending:
            raise ContractError("Middleware left unanswered tool requests")
        return candidate

    def after_tool(self, result: dict, max_chars: int) -> dict:
        updated = self.run("after_tool", {"result": result, "max_chars": max_chars})
        rendered = updated.get("result", result)
        if not isinstance(rendered, dict) or len(dumps(rendered)) > 8000000:
            raise ContractError("Middleware returned an invalid tool result")
        return rendered

    def before_tool(self, name: str, arguments: dict) -> dict:
        result = self.run("before_tool", {"name": name, "arguments": arguments})
        if set(result) - {"arguments"}:
            raise ContractError("Tool middleware may only transform arguments")
        arguments = result.get("arguments", arguments)
        if not isinstance(arguments, dict) or len(dumps(arguments)) > 64000:
            raise ContractError("Tool middleware returned invalid arguments")
        return arguments

    def after_model(self, content: str) -> str:
        result = self.run("after_model", {"content": content})
        if set(result) - {"content"}:
            raise ContractError("Model middleware may only transform response content")
        content = result.get("content", content)
        if not isinstance(content, str) or len(content) > 256000:
            raise ContractError("Model middleware returned invalid response content")
        return content
