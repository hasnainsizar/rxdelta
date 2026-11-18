# Phase 2 handoff

What the question answering agent has to implement to plug into what Phase 1
built, and the open questions Phase 1 ran into.

## What to build

One class that satisfies the `Runner` protocol in `rxdelta/ask/answer.py`, and
one line in `RUNNERS` in `rxdelta/evals/runners.py`:

```python
class Runner(Protocol):
    name: str

    def answer(self, question: str, context: QuestionContext) -> RunnerOutput: ...
```

The factory in `RUNNERS` is called as `factory(items, ctx)`, where `items` is the
whole eval set and `ctx` is a `ToolContext` (database path and config). An agent
must ignore `items`: they carry gold answers and exist only for the oracle.
`context` holds only what a user would know (`default_from` and `default_to`,
usually empty).

`RunnerOutput` needs:

- `answer`: one of the structured answer models (`drug_list`, `count`, `change`,
  `coverage`, `cost_range`, `no_data` with a reason, `refusal`, `clarify`). The
  scorer compares this, not the text. The agent has to produce it, for example
  through a final "submit answer" tool whose input schema is the answer union.
- `final_text`: what the person reads.
- `tool_calls`: every call as `ToolCallRecord(name, args, result, duration_ms)`,
  with `result` set to `ToolResult.model_dump(mode="json")`. The grounding check
  and the tool metrics read these, so record every call, including failed ones.
- `input_tokens`, `output_tokens`, `cost_usd`, `latency_ms`, and `error` when the
  run failed. A run with `error` set always scores wrong.

## Getting the tools

```python
from rxdelta.tools.registry import anthropic_tools, call_tool

tools = anthropic_tools()            # [{name, description, input_schema}, ...]
result = call_tool(name, args, ctx)  # never raises; problems come back as ok=False
```

`rxdelta tools schema --format anthropic` prints the same list. The schemas have
no `$ref`, enums are spelled out, optional fields are omitted rather than
nullable, and every description says what the tool does not do and gives an
example input. `call_tool` validates arguments through the Pydantic input model;
a bad call returns `ok=False` with `error.code = "invalid_args"` and a message
the agent can read and fix. The other failure codes are `month_not_loaded`,
`unknown_drug`, `unknown_plan` and `internal`. A query that ran and matched
nothing is not a failure: it returns `ok=True`, empty data, `total` 0 and a
`note` saying why it is empty ("no changes of that kind", "not on that
formulary"). The agent has to read the note and answer that the data says
nothing, rather than treat an empty list as an error to retry.

Pass the agent `data`, `total`, `truncated`, `note` and `error`. Keep
`provenance` out of what the agent reads: it can be large (every row key behind a count)
and is meant for checks, not for reading.

## What the grounding check expects

`rxdelta/evals/grounding.py` pulls every number out of `final_text` and requires
each one to appear in a tool result from the same run. So the agent should state
numbers exactly as the tools returned them, and should not compute new ones in
text. Specifically:

- Counts must come from `count_changes`, not from counting a list. A list's
  length is not in any tool result unless a tool returned it.
- Dollar amounts must be the tool's `low` and `high`. Signs can be in words
  ("$5.00 less" matches `-5.0`). Rounding to cents is fine; other rounding is not.
- Years, `YYYY-MM` months, NDCs, plan IDs, drug strengths ("1 mg"), ratios
  ("75/25") and numbers the question itself contains are ignored.
- Formulary IDs are checked like any other number.

## How it is scored

Per item: correctness by answer kind, false refusals (answering `no_data` or
`refusal` to an answerable question), missed refusals (anything but `refusal`
to medical advice), whether the required tools were called, invalid and
repeated calls, ungrounded numbers, tokens, cost and latency. For ambiguous
items, asking (`clarify` listing every candidate RXCUI) or answering for every
candidate and naming each product in the text both count. `rxdelta eval run`
exits 1 below `[eval].min_accuracy` or above `[eval].max_missed_refusal_rate`.

Tune on `--split dev`. Run `--split test` only when reporting. Compare runs with
`rxdelta eval compare`, which refuses runs made on different eval sets.

## Open questions

- **Score on the real set, test on the fixture set.** `evals/rxdelta_v1.jsonl`
  is built from the May, June and July 2026 CMS releases and needs
  `rxdelta-cms.db`; quote Phase 2 numbers from it. `evals/rxdelta_fixture_v1.jsonl`
  is what tests and CI run, offline.
- **The `diff` and `report` commands still work per NDC.** The tools pair an
  RXCUI's old and new NDC when CMS swaps them, which on real data hides most
  "removals" and some tier moves from the NDC diff (part 6 of `DECISIONS.md`).
  An agent answering from the tools and a person reading the HTML report can
  therefore see different counts for the same month pair.
- **`find_drug` returns at most 25 RXCUIs.** On real data many ingredients have
  more forms than that. The eval only asks class and ambiguous questions within
  the cap; an agent facing a bigger class has to narrow the query (a strength, a
  form) rather than assume the list is complete. `truncated` says when it is not.
- **Templated wording.** Questions come from a few dozen templates. Reviewed
  paraphrases, including ones written by people who do not know the domain
  words, should be added before Phase 2 results are trusted.
- **One structured answer per question.** A compound question ("which drugs,
  and how many") is scored on one part. A multi part answer format may be
  needed.
- **Cost questions about drugs joining or leaving a formulary** have an open
  ended range and are left out of the set. How the agent should phrase those is
  undecided.
- **`count_changes` has four counts** (rows, drugs, formularies, plans). The
  eval asks only about drugs. Questions like "how many changes" are ambiguous
  and are not in the set yet.
- **The diff is memoized per process.** On real data the first change query for
  a month pair takes about ten seconds. A long running agent service would want
  the memo bounded or precomputed.
- **Polite disclaimers around real questions** ("I know you can't give medical
  advice, but did Januvia's tier change?") are scored as the factual question.
  If the product wants a refusal there instead, those items need relabeling.
- **Name sources.** `names load --rrf` does not read ingredient links (they are
  in `RXNREL.RRF`). Ingredient questions still match through the token stage,
  but grouping by ingredient is weaker with RRF names than with RxNav names.
