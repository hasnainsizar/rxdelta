"""A thin layer between the agent loop and the model API.

The loop only sees `ModelClient`: a request goes in, a normalized response
comes out. `AnthropicClient` is the real one. `ScriptedClient` replays a fixed
list of responses for tests. `ReplayClient` records real responses to a
cassette file and later answers from it, so CI can run the real loop with no
key and no network.
"""

from __future__ import annotations

import hashlib
import json
import re
import threading
from pathlib import Path
from typing import Any, Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field

from rxdelta.config import ModelPrices
from rxdelta.types import RxdeltaError


class ModelError(RxdeltaError):
    """The model API could not answer, after the SDK's own retries."""

    def __init__(self, kind: str, message: str, *, account: bool = False) -> None:
        super().__init__(f"{kind}: {message}")
        self.kind = kind
        # The account cannot make any request (no credit, bad key, no access), so
        # every later request would fail the same way.
        self.account = account


class Usage(BaseModel):
    model_config = ConfigDict(frozen=True)

    input_tokens: int = 0
    output_tokens: int = 0
    cache_write_tokens: int = 0
    cache_read_tokens: int = 0

    def __add__(self, other: Usage) -> Usage:
        return Usage(
            input_tokens=self.input_tokens + other.input_tokens,
            output_tokens=self.output_tokens + other.output_tokens,
            cache_write_tokens=self.cache_write_tokens + other.cache_write_tokens,
            cache_read_tokens=self.cache_read_tokens + other.cache_read_tokens,
        )

    @property
    def prompt_tokens(self) -> int:
        """Every input token, cached or not."""
        return self.input_tokens + self.cache_write_tokens + self.cache_read_tokens


class ModelRequest(BaseModel):
    model_config = ConfigDict(frozen=True)

    model: str
    system: str
    messages: list[dict[str, Any]]
    tools: list[dict[str, Any]]
    max_tokens: int
    effort: str


class ModelResponse(BaseModel):
    # Plain dicts, so blocks go back to the API unchanged on the next turn.
    content: list[dict[str, Any]]
    stop_reason: str | None
    usage: Usage = Field(default_factory=Usage)

    def tool_uses(self) -> list[dict[str, Any]]:
        return [b for b in self.content if b.get("type") == "tool_use"]

    def text(self) -> str:
        return "\n".join(str(b.get("text", "")) for b in self.content if b.get("type") == "text")


class ModelClient(Protocol):
    def create(self, request: ModelRequest) -> ModelResponse: ...


def cost_usd(usage: Usage, prices: ModelPrices) -> float:
    """Dollars for one usage record at per-million-token prices."""
    return (
        usage.input_tokens * prices.input_per_mtok
        + usage.output_tokens * prices.output_per_mtok
        + usage.cache_write_tokens * prices.cache_write_per_mtok
        + usage.cache_read_tokens * prices.cache_read_per_mtok
    ) / 1_000_000


def _cached_tools(tools: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Mark the last tool so the whole tool list is one cached prefix."""
    if not tools:
        return tools
    last = {**tools[-1], "cache_control": {"type": "ephemeral"}}
    return [*tools[:-1], last]


class AnthropicClient:
    """The Messages API through the official SDK."""

    def __init__(self, api_key: str, timeout_seconds: float, max_retries: int) -> None:
        import anthropic

        self._anthropic = anthropic
        self._client = anthropic.Anthropic(
            api_key=api_key, timeout=timeout_seconds, max_retries=max_retries
        )

    def create(self, request: ModelRequest) -> ModelResponse:
        errors = self._anthropic
        params: dict[str, Any] = {
            "model": request.model,
            "max_tokens": request.max_tokens,
            "system": [
                {"type": "text", "text": request.system, "cache_control": {"type": "ephemeral"}}
            ],
            "messages": request.messages,
            "output_config": {"effort": request.effort},
            "cache_control": {"type": "ephemeral"},
        }
        if request.tools:
            params["tools"] = _cached_tools(request.tools)
        try:
            # Streamed, because a long answer can need a large max_tokens and the SDK
            # refuses large non-streaming requests that could pass its timeout.
            with self._client.messages.stream(**params) as stream:
                message = stream.get_final_message()
        except errors.APIStatusError as exc:
            account = exc.status_code in (401, 403) or "credit balance" in str(exc.message)
            raise ModelError(
                type(exc).__name__, f"HTTP {exc.status_code}: {exc.message}", account=account
            ) from exc
        except errors.APIConnectionError as exc:
            raise ModelError(type(exc).__name__, str(exc)) from exc
        usage = message.usage
        return ModelResponse(
            content=[block.model_dump(mode="json", exclude_none=True) for block in message.content],
            stop_reason=message.stop_reason,
            usage=Usage(
                input_tokens=usage.input_tokens,
                output_tokens=usage.output_tokens,
                cache_write_tokens=usage.cache_creation_input_tokens or 0,
                cache_read_tokens=usage.cache_read_input_tokens or 0,
            ),
        )


class ScriptedClient:
    """Answers with a fixed list of responses, in order, and records every request.

    An entry that is an exception is raised instead of returned."""

    def __init__(self, responses: list[ModelResponse | Exception]) -> None:
        self._responses = list(responses)
        self.requests: list[ModelRequest] = []
        self._lock = threading.Lock()

    def create(self, request: ModelRequest) -> ModelResponse:
        with self._lock:
            self.requests.append(request)
            if not self._responses:
                raise ModelError("ScriptExhausted", "the scripted client has no responses left")
            nxt = self._responses.pop(0)
        if isinstance(nxt, Exception):
            raise nxt
        return nxt


_SECRET = re.compile(r"sk-ant-[A-Za-z0-9_\-]+")


def request_hash(request: ModelRequest) -> str:
    canonical = json.dumps(request.model_dump(mode="json"), sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _scrub(text: str) -> str:
    return _SECRET.sub("[redacted]", text)


class CassetteMissError(ModelError):
    def __init__(self, digest: str, cassette: Path) -> None:
        super().__init__(
            "CassetteMiss",
            f"no recorded response for request {digest} in {cassette}. Record it again with "
            "make ask-record.",
        )


class ReplayClient:
    """Records responses to a cassette, or answers only from one."""

    def __init__(
        self,
        cassette: Path,
        mode: Literal["record", "replay"],
        inner: ModelClient | None = None,
    ) -> None:
        if mode == "record" and inner is None:
            raise ValueError("record mode needs a client to record from")
        self.cassette = cassette
        self.mode = mode
        self._inner = inner
        self._lock = threading.Lock()
        self._recorded: dict[str, ModelResponse] = {}
        if cassette.is_file():
            for line in cassette.read_text(encoding="utf-8").splitlines():
                if line.strip():
                    entry = json.loads(line)
                    self._recorded[entry["hash"]] = ModelResponse.model_validate(entry["response"])

    def create(self, request: ModelRequest) -> ModelResponse:
        digest = request_hash(request)
        if self.mode == "replay":
            if digest not in self._recorded:
                raise CassetteMissError(digest, self.cassette)
            return self._recorded[digest]
        assert self._inner is not None
        response = self._inner.create(request)
        entry = {
            "hash": digest,
            "request": request.model_dump(mode="json"),
            "response": response.model_dump(mode="json"),
        }
        line = _scrub(json.dumps(entry, sort_keys=True, ensure_ascii=False))
        with self._lock:
            self.cassette.parent.mkdir(parents=True, exist_ok=True)
            with self.cassette.open("a", encoding="utf-8") as handle:
                handle.write(line + "\n")
            self._recorded[digest] = response
        return response
