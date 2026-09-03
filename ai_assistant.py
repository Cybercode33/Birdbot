"""Small, optional Groq-backed assistant used by the Discord message listener.

The provider credential is read only from the private ``GROQ_API_KEY``
environment variable.  Server-specific enablement and channel selection live
in the shared SQLite store; no conversation or credential is sent to the web
dashboard.
"""

from __future__ import annotations

import asyncio
from collections import deque
from typing import Any

from settings import (
    GROQ_API_KEY,
    GROQ_FALLBACK_MODEL,
    GROQ_MAX_COMPLETION_TOKENS,
    GROQ_MODEL,
    GROQ_TEMPERATURE,
)

try:  # Keep the dashboard importable while a host is installing dependencies.
    from groq import AsyncGroq
except ImportError:  # pragma: no cover - exercised only before dependency install.
    AsyncGroq = None  # type: ignore[assignment,misc]


AI_SYSTEM_PROMPT = (
    "You are BirdBot's helpful Discord assistant. "
    "Reply directly to the user's message with clear, friendly, concise help. "
    "Match the language of the user: use Arabic for Arabic messages and English "
    "for English messages. You may answer other languages when the user uses "
    "them. Do not claim to have performed Discord actions, accessed private "
    "data, or checked live information unless that information is in the chat. "
    "Never use @everyone, @here, or user mentions."
)


class AIAssistant:
    """Manage bounded per-channel context and asynchronous Groq completions."""

    def __init__(self) -> None:
        self.available = bool(GROQ_API_KEY and AsyncGroq is not None)
        self._client: Any | None = None
        if self.available:
            self._client = AsyncGroq(
                api_key=GROQ_API_KEY,
                max_retries=1,
                timeout=30.0,
            )
        # Once Groq tells us the configured model is unavailable for this
        # project, use the fallback for the rest of this process instead of
        # paying the failed-request latency on every message.
        self._runtime_model: str | None = None
        self._histories: dict[tuple[str, str], deque[dict[str, str]]] = {}
        self._locks: dict[tuple[str, str], asyncio.Lock] = {}

    @property
    def model(self) -> str:
        return GROQ_MODEL

    def _history(self, key: tuple[str, str]) -> deque[dict[str, str]]:
        history = self._histories.get(key)
        if history is None:
            # Keep prompts small enough for a busy Discord channel and avoid
            # retaining unbounded user content in process memory.
            history = deque(maxlen=12)
            self._histories[key] = history
        return history

    def _lock(self, key: tuple[str, str]) -> asyncio.Lock:
        lock = self._locks.get(key)
        if lock is None:
            lock = asyncio.Lock()
            self._locks[key] = lock
        return lock

    @staticmethod
    def _model_request(request: dict[str, object], model: str) -> dict[str, object]:
        """Apply a model and only send GPT-OSS reasoning options when valid."""
        request["model"] = model
        if model.casefold().startswith("openai/gpt-oss"):
            request["reasoning_effort"] = "medium"
        else:
            request.pop("reasoning_effort", None)
        return request

    async def _stream_response(self, request: dict[str, object]) -> str:
        stream = await self._client.chat.completions.create(**request)
        parts: list[str] = []
        async for chunk in stream:
            choices = getattr(chunk, "choices", None) or []
            if not choices:
                continue
            delta = getattr(choices[0], "delta", None)
            content = getattr(delta, "content", None) if delta else None
            if content:
                parts.append(str(content))
        return "".join(parts).strip()

    @staticmethod
    def _is_model_not_found(error: Exception) -> bool:
        detail = str(error).casefold()
        return "model_not_found" in detail or "does not exist" in detail or "do not have access" in detail

    async def complete(
        self,
        guild_id: str,
        channel_id: str,
        prompt: str,
    ) -> str | None:
        """Generate one response, returning ``None`` when the provider is off."""
        if not self.available or self._client is None:
            return None
        prompt = str(prompt or "").strip()[:4_000]
        if not prompt:
            return None
        key = (str(guild_id), str(channel_id))
        async with self._lock(key):
            history = self._history(key)
            messages: list[dict[str, str]] = [
                {"role": "system", "content": AI_SYSTEM_PROMPT},
                *list(history),
                {"role": "user", "content": prompt},
            ]
            primary_model = self._runtime_model or GROQ_MODEL
            request: dict[str, object] = {
                "model": primary_model,
                "messages": messages,
                "temperature": GROQ_TEMPERATURE,
                "max_completion_tokens": GROQ_MAX_COMPLETION_TOKENS,
                "top_p": 1,
                "stream": True,
            }
            self._model_request(request, primary_model)
            try:
                response = await self._stream_response(request)
            except Exception as error:
                # Llama models may be listed by Groq but restricted for a
                # particular project. Keep the configured model first, then
                # use a known developer-plan fallback when Groq returns 404.
                if not self._is_model_not_found(error) or not GROQ_FALLBACK_MODEL or GROQ_FALLBACK_MODEL == primary_model:
                    raise
                self._runtime_model = GROQ_FALLBACK_MODEL
                self._model_request(request, GROQ_FALLBACK_MODEL)
                response = await self._stream_response(request)
            if not response:
                return None
            history.append({"role": "user", "content": prompt})
            history.append({"role": "assistant", "content": response[:4_000]})
            return response

    async def close(self) -> None:
        """Close the provider's HTTP client when the bot shuts down."""
        if self._client is not None:
            close = getattr(self._client, "close", None)
            if close is not None:
                await close()
            self._client = None
