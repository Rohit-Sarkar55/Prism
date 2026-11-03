"""Adapter for any OpenAI-compatible HTTP API (the provided mocks, or a real provider)."""

import json
import time
from collections.abc import AsyncIterator
from typing import Any

import httpx

from app.providers.base import (
    ChatResult,
    MidStreamError,
    ProviderError,
    StreamChunk,
    Usage,
)


def parse_usage(obj: Any) -> Usage | None:
    if not isinstance(obj, dict):
        return None
    prompt, completion = obj.get("prompt_tokens"), obj.get("completion_tokens")
    if isinstance(prompt, int) and isinstance(completion, int):
        return Usage(prompt, completion)
    return None


class OpenAICompatAdapter:
    def __init__(
        self,
        name: str,
        base_url: str,
        api_key: str,
        *,
        timeout_s: float = 2.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ):
        self.name = name
        self._url = base_url.rstrip("/") + "/chat/completions"
        self._api_key = api_key
        self._headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
        # Every call has a timeout. For a stream, `read` bounds the gap between chunks, so a
        # provider that goes silent mid-stream is cut off too.
        self._client = httpx.AsyncClient(
            timeout=httpx.Timeout(timeout_s, connect=min(timeout_s, 2.0)), transport=transport
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    # ---- non-streaming -------------------------------------------------------------------

    async def chat(self, model: str, messages: list[dict], **params: Any) -> ChatResult:
        payload = {**params, "model": model, "messages": messages, "stream": False}
        started = time.monotonic()
        try:
            resp = await self._client.post(self._url, json=payload, headers=self._headers)
        except httpx.TransportError as exc:
            raise self._transport_error(exc) from exc

        if resp.status_code >= 400:
            raise self._status_error(resp.status_code, resp.headers, resp.text)
        try:
            body = resp.json()
        except ValueError as exc:
            raise ProviderError(self.name, "malformed_response", detail="body is not JSON") from exc

        usage = parse_usage(body.get("usage")) if isinstance(body, dict) else None
        if usage is None or not isinstance(body.get("choices"), list):
            raise ProviderError(self.name, "malformed_response", detail="missing choices or usage")
        return ChatResult(body, usage, self.name, model, int((time.monotonic() - started) * 1000))

    # ---- streaming -----------------------------------------------------------------------

    async def stream(self, model: str, messages: list[dict], **params: Any) -> AsyncIterator[StreamChunk]:
        payload = {
            **params,
            "model": model,
            "messages": messages,
            "stream": True,
            "stream_options": {"include_usage": True},  # real OpenAI sends usage only on request
        }
        yielded = False
        try:
            async with self._client.stream("POST", self._url, json=payload, headers=self._headers) as resp:
                if resp.status_code >= 400:
                    body = (await resp.aread()).decode(errors="replace")
                    raise self._status_error(resp.status_code, resp.headers, body)

                finished = False
                async for line in resp.aiter_lines():
                    if not line.startswith("data:"):
                        continue
                    data = line[5:].strip()
                    if data == "[DONE]":
                        finished = True
                        break
                    try:
                        obj = json.loads(data)
                    except ValueError as exc:
                        raise self._stream_failure(yielded, "malformed_response", "bad chunk") from exc
                    yielded = True
                    yield StreamChunk(obj, parse_usage(obj.get("usage")) if isinstance(obj, dict) else None)

                if not finished:
                    raise self._stream_failure(yielded, "network", "stream ended without [DONE]")
        except httpx.TransportError as exc:
            if yielded:
                raise MidStreamError(self.name, detail=self._redact(repr(exc))) from exc
            raise self._transport_error(exc) from exc

    # ---- error mapping -------------------------------------------------------------------

    def _redact(self, text: str) -> str:
        return text.replace(self._api_key, "***") if self._api_key else text

    def _transport_error(self, exc: httpx.TransportError) -> ProviderError:
        # A connect timeout means "could not reach the provider", same as a refused connection.
        # Windows drops packets to a closed port instead of refusing, so a dead provider shows up
        # as ConnectTimeout there. This check must run before the generic timeout check.
        if isinstance(exc, (httpx.ConnectError, httpx.ConnectTimeout)):
            kind = "connect"
        elif isinstance(exc, httpx.TimeoutException):
            kind = "timeout"
        else:
            kind = "network"
        return ProviderError(self.name, kind, transient=True, detail=self._redact(repr(exc)))

    def _stream_failure(self, yielded: bool, kind: str, detail: str) -> ProviderError:
        if yielded:
            return MidStreamError(self.name, detail=detail)
        return ProviderError(self.name, kind, transient=(kind == "network"), detail=detail)

    def _status_error(self, status: int, headers: httpx.Headers, text: str) -> ProviderError:
        detail = self._redact(_upstream_message(text))
        if status == 429:
            return ProviderError(
                self.name, "rate_limited", status=status, transient=True,
                retry_after_s=_parse_retry_after(headers.get("retry-after")), detail=detail,
            )  # fmt: skip
        if status >= 500:
            return ProviderError(self.name, "server_error", status=status, transient=True, detail=detail)
        return ProviderError(self.name, "client_error", status=status, transient=False, detail=detail)


def _upstream_message(text: str) -> str:
    try:
        message = json.loads(text)["error"]["message"]
    except (ValueError, KeyError, TypeError):
        message = text
    return str(message)[:200]


def _parse_retry_after(value: str | None) -> float | None:
    try:
        return float(value) if value is not None else None
    except ValueError:
        return None