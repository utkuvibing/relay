"""The Phase 1 API-backed adapter: OpenAI-compatible chat completions.

SPEC reference: §7 (Agent Abstraction), App. B.2 (API-backed family),
App. B.3 (credentials from environment only).

One adapter, three names (see :mod:`relay.agents.registry`): any endpoint
speaking the OpenAI chat-completions protocol — OpenAI, DeepSeek, local
OpenAI-compatible servers — is a ``base_url`` away. The response envelope is
Pydantic-validated at the boundary; usage maps onto the transport-neutral
:class:`TokenUsage` (``cost_usd`` stays ``None`` — Phase 1 has no pricing
table, and harness runs may carry no usage at all).

Secrets: the API key is read from the environment at call time and exists
only in process memory (App. B.3). Persistable errors are deliberately
sanitized: provider response bodies and validation details never enter error
messages that may be written to Relay history.
"""

from __future__ import annotations

import os

import httpx
from pydantic import BaseModel, Field

from relay.agents.base import Agent, AgentRequest, AgentResponse, BackendType, TokenUsage
from relay.agents.config import AgentSettings
from relay.agents.errors import AgentError, AgentNotConfigured

DEFAULT_BASE_URL = "https://api.openai.com/v1"
_TIMEOUT_SECONDS = 120.0


class _ChatMessage(BaseModel):
    content: str = ""


class _ChatChoice(BaseModel):
    message: _ChatMessage


class _ChatUsage(BaseModel):
    prompt_tokens: int = 0
    completion_tokens: int = 0


class _ChatCompletion(BaseModel):
    choices: list[_ChatChoice] = Field(default_factory=list)
    usage: _ChatUsage | None = None


class OpenAICompatibleAgent(Agent):
    """Async chat-completions adapter for any OpenAI-compatible endpoint."""

    name = "openai"
    backend = BackendType.API

    def __init__(
        self,
        settings: AgentSettings | None = None,
        *,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._settings = settings or AgentSettings(adapter=self.name)
        #: Injectable for offline tests/callers. Ownership stays with the caller.
        self._client = client

    def _endpoint(self) -> str:
        base = (self._settings.base_url or DEFAULT_BASE_URL).rstrip("/")
        return f"{base}/chat/completions"

    def _api_key(self) -> str:
        key = os.environ.get(self._settings.api_key_env)
        if not key:
            raise AgentNotConfigured(
                f"{self.name}: {self._settings.api_key_env} is not set — "
                "configure it in the environment (secrets are never stored by Relay)"
            )
        return key

    async def _post(self, payload: dict, headers: dict[str, str]) -> httpx.Response:
        if self._client is not None:
            return await self._client.post(self._endpoint(), json=payload, headers=headers)
        async with httpx.AsyncClient(timeout=_TIMEOUT_SECONDS) as client:
            return await client.post(self._endpoint(), json=payload, headers=headers)

    async def run(self, request: AgentRequest) -> AgentResponse:
        api_key = self._api_key()
        payload = {
            "model": self._settings.model or "gpt-4o-mini",
            "messages": [{"role": "user", "content": request.prompt}],
        }
        headers = {"Authorization": f"Bearer {api_key}"}

        try:
            response = await self._post(payload, headers)
        except httpx.TimeoutException as exc:
            raise AgentError(f"{self.name}: provider timed out after {_TIMEOUT_SECONDS}s") from exc
        except httpx.ConnectError as exc:
            raise AgentError(
                f"{self.name}: cannot reach {self._endpoint()} — check the base URL / network"
            ) from exc
        except httpx.HTTPError as exc:
            raise AgentError(f"{self.name}: HTTP transport failure") from exc

        if response.status_code == 401:
            raise AgentError(
                f"{self.name}: authentication failed (401) — check {self._settings.api_key_env} "
                "and that the key is valid for the configured base URL"
            )
        if response.status_code == 429:
            raise AgentError(
                f"{self.name}: rate limited (429) — retry later or lower concurrency"
            )
        if response.status_code >= 400:
            raise AgentError(f"{self.name}: provider returned HTTP {response.status_code}")

        try:
            body = _ChatCompletion.model_validate(response.json())
        except ValueError as exc:
            raise AgentError(f"{self.name}: malformed provider response") from exc

        if not body.choices or body.choices[0].message.content is None:
            raise AgentError(f"{self.name}: provider returned no completion content")

        usage = None
        if body.usage is not None:
            usage = TokenUsage(
                input_tokens=body.usage.prompt_tokens,
                output_tokens=body.usage.completion_tokens,
            )
        return AgentResponse(
            agent=self.name,
            role=request.role,
            output=body.choices[0].message.content,
            usage=usage,
        )
