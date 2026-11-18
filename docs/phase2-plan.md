# Phase 2 plan: the `rxdelta ask` agent

Written after reading `docs/phase2-handoff.md`, parts 4 and 6 of `DECISIONS.md`,
the answer contract, the harness, the scorer, grounding, the tool registry and
every tool description, the config, the Makefile and CI.

## What exists

- Seven read-only tools behind `call_tool(name, args, ctx)`, which never raises.
  `anthropic_tools()` exports flat schemas (no `$ref`, enums spelled out,
  optionals omitted rather than nullable).
- `RunnerOutput` (structured answer, text, tool calls, tokens, cost, latency,
  error) and the `Runner` protocol. `RUNNERS` maps a name to a factory called as
  `factory(items, ctx)`.
- `harness.run` scores any runner, writes `results.jsonl`, `summary.json`,
  `summary.md` and `report.html`, and gates on accuracy and missed refusals.
  `grounding.check` pulls numbers out of a text and requires each in a tool
  result of the same run.
- Real set: 220 items, 155 dev and 65 test, needs `rxdelta-cms.db`. Fixture set:
  210 items in 10 categories (no `over_cap`), runs offline in CI.

## Files added

| file | what it holds |
|---|---|
| `rxdelta/agent/__init__.py` | package marker |
| `rxdelta/agent/client.py` | `ModelClient` protocol, `ModelRequest`, `ModelResponse`, `Usage`, `AnthropicClient`, `ScriptedClient`, `ReplayClient`, `cost_usd` |
| `rxdelta/agent/toolset.py` | the `submit_answer` tool built from `ANSWER_ADAPTER`, and the view of a `ToolResult` the model reads (no `provenance`, size cap) |
| `rxdelta/agent/guards.py` | schema, grounding and consistency checks, each a pure function returning a verdict |
| `rxdelta/agent/loop.py` | `AgentRunner` and `AgentSettings` |
| `rxdelta/agent/prompts/system_v1.md` | the baseline system prompt; later `system_v2.md` |
| `rxdelta/agent/prompts.py` | loads a prompt by version, refuses unknown versions |
| `rxdelta/agent/budget.py` | spend ledger, cost estimate, the running spend guard |
| `rxdelta/agent/trace.py` | renders one item's trace for `eval show` |
| `rxdelta/agent/keys.py` | reads `ANTHROPIC_API_KEY` (optionally from `.env`), one sentence when missing |
| `rxdelta/evals/paraphrase.py` | writes paraphrase candidates and builds the approved paraphrase set |
| `tests/test_agent_*.py` | client, cost, loop, guards, budget, CLI, replay and repeats tests |
| `tests/cassettes/` | recorded responses for the replay test (recorded with a key) |
| `docs/error-analysis-v1.md`, `docs/phase3-ideas.md` | written after the runs |

Changed: `config/rxdelta.toml` and `rxdelta/config.py` (`[agent]`,
`[agent.guards]`, `[agent.models.*]`), `rxdelta/ask/answer.py` (an optional
`trace` on `RunnerOutput`, never shown to the scorer), `rxdelta/evals/runners.py`
(one `RUNNERS` line), `rxdelta/evals/harness.py` (runner options, repeats,
resume, budget, the new summary fields, compare settings), the eval report
template, `rxdelta/cli.py` (`ask`, `eval show`, the new `eval run` flags,
`evalset paraphrase`), `Makefile`, CI, README, `DECISIONS.md` part 7.

## The client layer

`ModelClient.create(request) -> ModelResponse`. A `ModelRequest` holds the model
ID, system blocks, messages, tools, `max_tokens` and effort. A `ModelResponse`
holds content blocks as plain dicts (text, thinking, tool_use), the stop reason
and `Usage` (input, output, cache write, cache read tokens). Content blocks stay
plain dicts so they go back to the API unchanged on the next turn, which the
thinking blocks of current models require.

- `AnthropicClient` calls `messages.create` with the SDK's own retries and
  timeout from config. One cache breakpoint on the last tool definition and one
  on the system block, so the identical tools and prompt are read from the cache
  on every turn of every question, plus top-level automatic caching for the
  growing message tail within a question.
- `ScriptedClient` returns a fixed list of responses and records each request.
- `ReplayClient(inner, cassette, mode)`: `record` calls the inner client and
  appends `{hash, request, response}`; `replay` answers only from the cassette
  and raises naming the missing hash. The hash is SHA-256 of the canonical JSON
  of model, system, messages, tools and settings. Anything that looks like a key
  is scrubbed before writing.
- `cost_usd(usage, prices)`: four token counts times four per-million rates.

## The loop

`AgentRunner.answer(question, context)`:

1. Build the first user message from the question and, when the context has
   them, the default months.
2. Call the model with the system prompt and the eight tools (seven plus
   `submit_answer`).
3. For every `tool_use` block other than `submit_answer`, run `call_tool`,
   record the full `ToolResult` in a `ToolCallRecord` (failed calls too), and
   answer with a JSON body of only `ok`, `data`, `total`, `truncated`, `note`,
   `error`. A body over `[agent].max_tool_result_chars` has its longest list cut,
   `total` kept and a note added saying it was shortened and how to narrow it.
   All results of one turn go back in one user message.
4. On `submit_answer`, run the guards in order: schema (`parse_answer`),
   grounding (`grounding.check` of `final_text` against this run's results),
   consistency. A failure goes back as an `is_error` tool result naming the
   problem, while that guard's retry budget lasts. When a budget runs out the
   last answer is accepted and the failed guard is recorded; a schema failure
   with no budget left ends the run with `error`, since there is no answer to
   accept. The first draft is recorded with the number of tool calls made
   before it, so the harness can grade the draft's grounding itself.
5. A plain-text stop gets one reminder; a second ends with `error`. Max turns,
   max tool calls and the per-question cost cap end with `error`. An API error
   after the SDK's retries ends the item with `error` and the error type.
6. Tokens, cost and latency are filled on every output. The trace (prompt
   version, every request and response, every guard decision) goes on
   `RunnerOutput.trace`, which the harness writes under `trace` and strips from
   `output` and from `ask --json`.

The factory receives the eval items and drops them. A test passes items that
raise on any access.

## The answer schema as a tool

`submit_answer` takes `{"answer": <StructuredAnswer>, "final_text": <string>}`.
The schema comes from `ANSWER_ADAPTER.json_schema()`, inlined with the same
`_inline` the tools use, the discriminator mapping (which holds `$ref`s)
dropped and `oneOf` written as `anyOf`. A test walks the schema for `$ref` and
validates one example of every answer kind through `parse_answer`.

## Tests without a key

Every loop test uses `ScriptedClient` on the fixture database. The replay test
runs the real loop through `ReplayClient` in replay mode on cassettes recorded
by `make ask-record`, and compares scores with the recorded run's scores; until
the cassettes exist it skips with a message. `ask` CLI tests inject a scripted
client through a module-level factory hook. Nothing in CI needs a key or the
network.

## Running evals with a model

`eval run --runner agent --model <id> --prompt v1 --repeats N
--no-grounding-guard ... --resume <dir> --stratified`. Results are appended to
`results.jsonl` as each item finishes, so a stopped run keeps its items, then
rewritten in item order at the end. Before starting, the estimate (items times
repeats times the mean cost per item of the last ledger entry for that model,
or a conservative per-model default from config) is printed and the run is
refused if it would take total spend past `[agent].budget_usd`. During the run
a shared counter stops new items once actual spend reaches the cap.

With repeats the pooled summary is computed over every (item, repeat) pair, and
a `repeats` block adds per-repeat accuracy, mean, min, max, standard deviation
and the right on all, some or none counts. Compare treats an item as right when
it was right on a majority of its repeats.

## Experiment plan (step 7)

Run on the real set, dev split (155 items), unless noted.

1. Baseline: `claude-haiku-5-5`, `system_v1`, all guards on, 3 repeats.
2. Stronger model: `claude-sonnet-5-5`, same, 3 repeats.
3. Guard ablation: the better of 1 and 2, all guards off, 1 repeat.
4. Error analysis of every failure in 1 and 2, written to `docs/error-analysis-v1.md`.
5. `system_v2` aimed at the top failure categories; the better model, 3 repeats.
6. Held-out: final configuration once on the test split (65 items), 3 repeats.
7. Paraphrase candidates for 40 stratified dev items with the cheapest model,
   then stop for review. `evalset paraphrase --approved` builds the set later.

Costs are estimated from a measured per-question token profile before STOP 1,
and nothing paid runs before confirmation.
