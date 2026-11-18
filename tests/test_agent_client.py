from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from rxdelta.agent.client import (
    CassetteMissError,
    ModelError,
    ModelRequest,
    ModelResponse,
    ReplayClient,
    ScriptedClient,
    Usage,
    _cached_tools,
    cost_usd,
    request_hash,
)
from rxdelta.agent.keys import MISSING_KEY, MissingKeyError, api_key
from rxdelta.config import Config, ModelPrices


def _request(text: str = "hi") -> ModelRequest:
    return ModelRequest(
        model="claude-haiku-5-5",
        system="system",
        messages=[{"role": "user", "content": text}],
        tools=[{"name": "t", "description": "d", "input_schema": {"type": "object"}}],
        max_tokens=100,
        effort="medium",
    )


def _response(text: str) -> ModelResponse:
    return ModelResponse(
        content=[{"type": "text", "text": text}],
        stop_reason="end_turn",
        usage=Usage(input_tokens=10, output_tokens=5),
    )


def test_cost_math_by_hand() -> None:
    prices = ModelPrices(
        input_per_mtok=2.0,
        output_per_mtok=10.0,
        cache_write_per_mtok=2.5,
        cache_read_per_mtok=0.1,
        estimate_usd_per_question=0.1,
    )
    usage = Usage(
        input_tokens=1_000, output_tokens=500, cache_write_tokens=4_000, cache_read_tokens=20_000
    )
    # 1000*2 + 500*10 + 4000*2.5 + 20000*0.1 = 2000 + 5000 + 10000 + 2000 = 19000 per million
    assert cost_usd(usage, prices) == pytest.approx(0.019)
    assert cost_usd(Usage(), prices) == 0.0


def test_cost_math_with_configured_haiku_prices(config: Config) -> None:
    prices = config.agent.prices("claude-haiku-5-5")
    usage = Usage(
        input_tokens=2_000, output_tokens=1_000, cache_write_tokens=8_000, cache_read_tokens=40_000
    )
    # 2000*0.10 + 1000*0.50 + 8000*0.125 + 40000*0.01 = 200 + 500 + 1000 + 400 = 2100 per million
    assert cost_usd(usage, prices) == pytest.approx(0.0021)


def test_usage_adds_and_counts_every_prompt_token() -> None:
    total = Usage(input_tokens=1, output_tokens=2, cache_write_tokens=3, cache_read_tokens=4) + (
        Usage(input_tokens=10, output_tokens=20, cache_write_tokens=30, cache_read_tokens=40)
    )
    assert total == Usage(
        input_tokens=11, output_tokens=22, cache_write_tokens=33, cache_read_tokens=44
    )
    assert total.prompt_tokens == 88


def test_unpriced_model_is_a_clear_error(config: Config) -> None:
    with pytest.raises(Exception, match="No prices for model 'gpt'"):
        config.agent.prices("gpt")


def test_only_the_last_tool_carries_the_cache_marker() -> None:
    tools = [{"name": "a"}, {"name": "b"}]
    marked = _cached_tools(tools)
    assert "cache_control" not in marked[0]
    assert marked[1]["cache_control"] == {"type": "ephemeral"}
    assert "cache_control" not in tools[1]


def test_scripted_client_records_requests_and_raises_when_empty() -> None:
    client = ScriptedClient([_response("one"), RuntimeError("boom")])
    assert client.create(_request()).text() == "one"
    with pytest.raises(RuntimeError, match="boom"):
        client.create(_request("again"))
    with pytest.raises(ModelError, match="no responses left"):
        client.create(_request())
    assert [r.messages[0]["content"] for r in client.requests] == ["hi", "again", "hi"]


def test_replay_records_then_replays_and_names_a_missing_hash(tmp_path: Path) -> None:
    cassette = tmp_path / "c.jsonl"
    inner = ScriptedClient([_response("recorded")])
    recorder = ReplayClient(cassette, "record", inner)
    assert recorder.create(_request()).text() == "recorded"

    player = ReplayClient(cassette, "replay")
    assert player.create(_request()).text() == "recorded"
    missing = _request("never asked")
    with pytest.raises(CassetteMissError, match=request_hash(missing)):
        player.create(missing)


def test_the_hash_covers_settings(tmp_path: Path) -> None:
    base = _request()
    assert request_hash(base) != request_hash(base.model_copy(update={"effort": "low"}))
    assert request_hash(base) != request_hash(base.model_copy(update={"model": "other"}))


def test_secrets_are_scrubbed_before_writing(tmp_path: Path) -> None:
    cassette = tmp_path / "c.jsonl"
    leaked = _response("key sk-ant-api03-SECRETVALUE here")
    ReplayClient(cassette, "record", ScriptedClient([leaked])).create(_request())
    text = cassette.read_text(encoding="utf-8")
    assert "SECRETVALUE" not in text
    assert json.loads(text)["response"]["content"][0]["text"] == "key [redacted] here"


def test_record_mode_needs_an_inner_client(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="needs a client"):
        ReplayClient(tmp_path / "c.jsonl", "record")


def test_missing_key_is_one_sentence(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    environ = {k: v for k, v in os.environ.items() if k != "ANTHROPIC_API_KEY"}
    monkeypatch.setattr(os, "environ", environ)
    with pytest.raises(MissingKeyError) as caught:
        api_key(tmp_path / "absent.env")
    assert str(caught.value) == MISSING_KEY
    env = tmp_path / ".env"
    env.write_text("ANTHROPIC_API_KEY=from-file\n", encoding="utf-8")
    assert api_key(env) == "from-file"
