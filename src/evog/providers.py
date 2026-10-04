"""Injectable model interface and a synchronous Chat Completions transport."""

from __future__ import annotations

import json
import time
from typing import Any, Protocol

import httpx
from pydantic import Field

from evog.config import Settings
from evog.errors import DeadlineExceeded, ProviderError
from evog.models import Record


class ToolCall(Record):
    id: str = Field(min_length=1, max_length=256)
    name: str = Field(min_length=1, max_length=100)
    arguments: dict[str, Any]


class ModelReply(Record):
    content: str = Field(default="", max_length=256000)
    tool_calls: list[ToolCall] = Field(default_factory=list, max_length=16)
    usage: dict[str, int] = Field(default_factory=dict)


class Provider(Protocol):
    def complete(
        self, messages: list[dict[str, Any]], tools: list[dict[str, Any]]
    ) -> ModelReply: ...


def stage_provider(provider: Provider, settings: Settings, stage: str) -> Provider:
    """Use independent output budgets while retaining the same endpoint and reasoning flags."""
    if isinstance(provider, ChatProvider):
        token_field = f"{stage}_max_output_tokens"
        bounded = settings.model_copy(
            update={"max_output_tokens": getattr(settings, token_field, settings.max_output_tokens)}
        )
        return ChatProvider(bounded, client=provider.client)
    return provider


def complete_before(
    provider: Provider, messages: list[dict[str, Any]], tools: list[dict[str, Any]], deadline: float
) -> ModelReply:
    if time.monotonic() >= deadline:
        raise DeadlineExceeded("Question deadline reached")
    bounded = getattr(provider, "complete_before", None)
    reply = (
        bounded(messages, tools, deadline=deadline)
        if callable(bounded)
        else provider.complete(messages, tools)
    )
    if time.monotonic() >= deadline:
        raise DeadlineExceeded("Question deadline reached")
    return reply


class ChatProvider:
    """Provider keys are never written to traces. Inject an httpx client in tests."""

    def __init__(self, settings: Settings, *, client: httpx.Client | None = None):
        if not settings.base_url or not settings.model or not settings.api_key.get_secret_value():
            raise ProviderError("Set EVOG_BASE_URL, EVOG_MODEL and EVOG_API_KEY for live requests")
        try:
            parsed = httpx.URL(settings.base_url)
        except httpx.InvalidURL as exc:
            raise ProviderError("Invalid provider API root") from exc
        if (
            parsed.scheme not in ("http", "https")
            or not parsed.host
            or parsed.userinfo
            or parsed.query
            or parsed.fragment
        ):
            raise ProviderError("Base URL must be an HTTP(S) API root without credentials or query")
        self.settings = settings
        self.api_root = settings.base_url.rstrip("/")
        if parsed.path in ("", "/"):
            self.api_root += "/v1"
        self._owns_client = client is None
        self.client = client or httpx.Client(timeout=settings.request_timeout)

    def complete(self, messages: list[dict[str, Any]], tools: list[dict[str, Any]]) -> ModelReply:
        return self._complete(messages, tools)

    def complete_before(
        self, messages: list[dict[str, Any]], tools: list[dict[str, Any]], *, deadline: float
    ) -> ModelReply:
        return self._complete(messages, tools, deadline=deadline)

    def _complete(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        *,
        deadline: float | None = None,
    ) -> ModelReply:
        payload: dict[str, Any] = {
            "model": self.settings.model,
            "messages": messages,
            "max_completion_tokens": self.settings.max_output_tokens,
        }
        if tools:
            payload["tools"] = tools
        if self.settings.reasoning_effort is not None:
            payload["reasoning_effort"] = self.settings.reasoning_effort
            # Several OpenAI-compatible gateways use Anthropic-style thinking
            # controls. Sending both explicit disable switches when ``none``
            # is configured prevents a downstream default from silently
            # re-enabling hidden reasoning on later calls (including reflection
            # and stage providers).
            if self.settings.reasoning_effort == "none":
                payload["thinking"] = {"type": "disabled"}
        for attempt in range(self.settings.call_attempts):
            timeout = min(self.settings.request_timeout, self.settings.call_timeout_seconds)
            if deadline is not None:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise DeadlineExceeded("Question deadline reached")
                timeout = min(timeout, remaining)
            try:
                response = self.client.post(
                    self.api_root + "/chat/completions",
                    headers={"Authorization": f"Bearer {self.settings.api_key.get_secret_value()}"},
                    json=payload,
                    timeout=timeout,
                )
            except httpx.HTTPError as exc:
                if deadline is not None and time.monotonic() >= deadline:
                    raise DeadlineExceeded("Question deadline reached") from exc
                if (
                    isinstance(exc, httpx.TimeoutException)
                    and attempt + 1 < self.settings.call_attempts
                ):
                    continue
                raise ProviderError("Provider transport failed") from exc
            if deadline is not None and time.monotonic() >= deadline:
                raise DeadlineExceeded("Question deadline reached")
            if (
                response.status_code in (429, 500, 502, 503, 504)
                and attempt + 1 < self.settings.call_attempts
            ):
                delay = 0.5 * (2**attempt)
                if deadline is not None and delay >= deadline - time.monotonic():
                    raise DeadlineExceeded("Question deadline reached")
                time.sleep(delay)
                continue
            if response.is_error:
                raise ProviderError(f"Provider request failed (HTTP {response.status_code})")
            try:
                data = response.json()
                raw = data["choices"][0]["message"]
                calls = [
                    ToolCall(
                        id=call["id"],
                        name=call["function"]["name"],
                        arguments=json.loads(call["function"]["arguments"]),
                    )
                    for call in raw.get("tool_calls", [])
                ]
                if len({call.id for call in calls}) != len(calls) or len(calls) > 16:
                    raise ValueError("Invalid tool call IDs or oversized call batch")
                usage = {
                    key: int(value)
                    for key, value in (data.get("usage") or {}).items()
                    if key in ("prompt_tokens", "completion_tokens", "total_tokens")
                }
                return ModelReply(content=raw.get("content") or "", tool_calls=calls, usage=usage)
            except (KeyError, IndexError, TypeError, ValueError, AttributeError) as exc:
                raise ProviderError("Provider returned an invalid chat response") from exc
        raise ProviderError("Provider retry budget exhausted")

    def close(self) -> None:
        if self._owns_client:
            self.client.close()


def assistant_message(reply: ModelReply) -> dict[str, Any]:
    result: dict[str, Any] = {"role": "assistant", "content": reply.content or None}
    if reply.tool_calls:
        result["tool_calls"] = [
            {
                "id": call.id,
                "type": "function",
                "function": {
                    "name": call.name,
                    "arguments": json.dumps(call.arguments, ensure_ascii=False),
                },
            }
            for call in reply.tool_calls
        ]
    return result
