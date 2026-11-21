# Phase 3 plan: agent fixes for a long running service

Scope is the four fixes in Prompt 2 of `docs/rxdelta_saas_plan.md`, drawn from
`docs/phase3-ideas.md`. Compound questions and open ended cost questions wait.

## 1. Batch coverage

`get_coverage` takes either `rxcui` (as today) or `rxcuis`, a list, never both.
A list needs `formulary_id` or `plan_id`, because the batch answers "which of
these drugs does this formulary cover, and how" for one formulary and month.
The list is capped by a new `[tools.row_caps].get_coverage_rxcuis`; a longer
list is `invalid_args` naming the cap. One SQL query reads every RXCUI. The
result keeps today's fields for a single call, and a batch call fills a new
`drugs` list, one entry per RXCUI with its rows and whether the formulary covers
it. An unknown RXCUI fails the whole call and names it, as a single call does.

The tool description changes, so the tool definitions every model call sends
change. v1 and v2 are scored again only if needed; the README says which tool
version each column used.

## 2. Bounded diff memo

`tools/changes_core.all_rows` and `evals/gold.changes` each keep an unbounded
dict of every month pair seen. Both move to a small LRU in a new
`rxdelta/memo.py` (an `OrderedDict` behind a lock), sized by
`[tools].diff_memo_pairs`. Gold may import it: it is not `tools/` or `diff/`, so
the gold independence rule holds. The size is read at the call, from the config
each caller already has.

## 3. System prompt v3

`system_v3.md` is `system_v2.md` with two rules changed, both general:

- Drug names: call `find_drug` with the name as the question writes it,
  strength and form included, and ask which product only when that lookup
  still returns several products. Targets `rxd-v1-0044` (test) and the same
  habit on `h10` and the reworded `0115` (dev).
- Long answers: past the row limit, `final_text` gives a short summary (count,
  change types, a few examples, months) and the structured answer keeps every
  row. Replaces v2's "answer with a count past 300 rows", which failed `0034`,
  `0045` and `0046` on dev.

Also in v3: use the batch form of `get_coverage` to check several drugs on one
formulary, and say which unit a count is in. v1 and v2 stay selectable with
`--prompt`; the config default moves to v3 only if it wins on dev.

## 4. Count units

`count_changes` already returns rows, drugs, formularies and plans, and
`CountAnswer` has a `unit`. What is missing is the agent saying which one it
gives, and eval items for the three units the set never asks about.

- Gold: `count_drugs` takes an optional `unit` (default `drugs`, so every
  existing item is unchanged) and counts rows, distinct formularies (the later
  month's) or distinct plans from its own SQL.
- Oracle: reads the same unit from `count_changes` totals.
- Consistency guard: a count answer's text must name its unit.
- Eval items: `evals/handwritten_units.jsonl`, about four questions per unit,
  built with the existing builder's handwritten path into its own set,
  `evals/rxdelta_units_v1.jsonl`, split dev and test. A separate set keeps
  `rxdelta_v1.jsonl` byte for byte the same, so every earlier run stays
  comparable. The oracle must score 100 percent on it.

## Evals

Dev only while tuning: Haiku 5.5, v3, guards on, 3 repeats, on the real dev split
and the units dev items. Then the test split once, and the units test items
once. Spend stays inside `[agent].budget_usd` (about $17.40 of $50 used).

The cassette is recorded again with v3 so the CI replay test covers it.

## Done when

`make check` passes; v3 dev accuracy is at least v2's 96.1 percent;
`rxd-v1-0044` passes on every test repeat; `rxd-v1-0132` and `rxd-v1-0142`
finish in 10 tool calls or fewer.

`rxd-v1-0044` is a test item. v3's rule is written for the habit (seen on dev in
`h10` and the reworded `0115`), not for that item, and the test split is still
run once, after v3 is fixed.

## Skills

The project asks for ponytail, pstack and thermo-nuclear on every task.
thermo-nuclear is marked `disable-model-invocation`, so only the owner can start
it; pstack is installed but disabled because it registers a SessionStart hook.
ponytail is followed directly and pstack's `deslop` and `blast-radius` from
their files.

## Files

New: `rxdelta/memo.py`, `rxdelta/agent/prompts/system_v3.md`,
`evals/handwritten_units.jsonl`, `evals/rxdelta_units_v1.jsonl` and manifest,
`tests/test_memo.py`, tests for batch coverage and units. Changed:
`tools/get_coverage.py`, `tools/changes_core.py`, `evals/gold.py`,
`evals/runners.py`, `agent/guards.py`, `config/rxdelta.toml`, `config.py`,
the cassette, README, DECISIONS.md Part 9.
