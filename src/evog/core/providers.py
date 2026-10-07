"""Injectable model interface and a synchronous Chat Completions transport."""

from __future__ import annotations

import json
import time
from threading import BoundedSemaphore
from typing import Any, Protocol

import httpx
from pydantic import Field

from evog.core.config import Settings
from evog.core.errors import DeadlineExceeded, ProviderError
from evog.core.models import Record
from evog.core.usage import normalize_usage


class ToolCall(Record):
    id: str = Field(min_length=1, max_length=256)
    name: str = Field(min_length=1, max_length=100)
    arguments: dict[str, Any]


class ModelReply(Record):
    content: str = Field(default="", max_length=256000)
    tool_calls: list[ToolCall] = Field(default_factory=list, max_length=16)
    usage: dict[str, int] = Field(default_factory=dict)
    response_model: str = Field(default="", max_length=512)
    http_attempts: int = Field(default=1, ge=1)


class Provider(Protocol):
    def complete(
        self, messages: list[dict[str, Any]], tools: list[dict[str, Any]]
    ) -> ModelReply: ...


def stage_provider(provider: Provider, settings: Settings, stage: str) -> Provider:
    """Apply stage budgets and sampling while retaining the campaign backbone."""
    if isinstance(provider, ChatProvider):
        token_field = f"{stage}_max_output_tokens"
        temperature = getattr(settings, f"{stage}_temperature", None)
        bounded = settings.model_copy(
            update={
                "max_output_tokens": getattr(settings, token_field, settings.max_output_tokens),
                "temperature": settings.temperature if temperature is None else temperature,
                "call_retry_backoff_seconds": (
                    settings.analysis_retry_backoff_seconds
                    if stage in {"analysis", "synthesis"}
                    else settings.call_retry_backoff_seconds
                ),
            }
        )
        return ChatProvider(bounded, client=provider.client)
    return provider


class ConcurrentProvider:
    """Bound independently issued judge requests, including the waiting deadline."""

    def __init__(self, provider: Provider, concurrency: int):
        self.provider = provider
        self._slots = BoundedSemaphore(concurrency)

    def complete(self, messages: list[dict[str, Any]], tools: list[dict[str, Any]]) -> ModelReply:
        with self._slots:
            return self.provider.complete(messages, tools)

    def complete_before(
        self, messages: list[dict[str, Any]], tools: list[dict[str, Any]], *, deadline: float
    ) -> ModelReply:
        remaining = deadline - time.monotonic()
        if remaining <= 0 or not self._slots.acquire(timeout=remaining):
            raise DeadlineExceeded("Judge deadline reached while waiting for a request slot")
        try:
            return complete_before(self.provider, messages, tools, deadline)
        finally:
            self._slots.release()


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
        if self.settings.temperature is not None:
            payload["temperature"] = self.settings.temperature
        if self.settings.sampling_seed is not None:
            payload["seed"] = self.settings.sampling_seed
        if self.settings.reasoning_effort is not None:
            payload["reasoning_effort"] = self.settings.reasoning_effort
            # Several OpenAI-compatible gateways use Anthropic-style thinking
            # controls. Sending both explicit disable switches when ``none``
            # is configured prevents a downstream default from silently
            # re-enabling hidden reasoning on later calls (including reflection
            # and stage providers).
            if self.settings.reasoning_effort == "none":
                payload["thinking"] = {"type": "disabled"}

        def backoff(attempt: int) -> None:
            delay = self.settings.call_retry_backoff_seconds * (2**attempt)
            if deadline is not None and delay >= deadline - time.monotonic():
                raise DeadlineExceeded("Request deadline reached before retry")
            if delay:
                time.sleep(delay)

        # One transport layer owns retries; stage callers must not retry ProviderError.
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
                    backoff(attempt)
                    continue
                raise ProviderError("Provider transport failed") from exc
            if deadline is not None and time.monotonic() >= deadline:
                raise DeadlineExceeded("Question deadline reached")
            if (
                response.status_code in (429, 500, 502, 503, 504)
                and attempt + 1 < self.settings.call_attempts
            ):
                backoff(attempt)
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
                usage = normalize_usage(data.get("usage"))
                returned_model = data.get("model", "")
                return ModelReply(
                    content=raw.get("content") or "",
                    tool_calls=calls,
                    usage=usage,
                    response_model=returned_model if isinstance(returned_model, str) else "",
                    http_attempts=attempt + 1,
                )
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
