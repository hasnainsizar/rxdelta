# Phase 1 plan: tool layer, eval set, eval harness

This plan was written after reading the whole repo and opening every database
the code can produce. It records the real schema the new code is built on, what
can be answered with the data that exists, the gaps, and the files being added.

## What exists today

- Package `rxdelta/` (the folder and the Python package share the name, so no
  mapping is needed). CLI entry point `rxdelta.cli:app`, a Typer app with
  `load`, `diff`, `summary`, `report`, `status`, `version`, and a `names` sub-app
  with one command, `names refresh`.
- Ingest: `rxdelta/ingest/` reads the three CMS files into SQLite, one partition
  per `snapshot_month`. NDCs are normalized to 11 digits by
  `rxdelta.ingest.normalize.normalize_ndc`.
- Diff: `rxdelta.diff.engine.diff_snapshots` pairs every plan present in both
  months with the formulary it pointed at in each month and classifies each NDC
  with `classify`. The result is a list of `DrugPlanChange`, one per drug and
  plan.
- Cost: `rxdelta.diff.impact.tier_cost` turns the beneficiary cost rows for one
  plan and tier into a monthly range, and `estimate_impact` turns a before and
  after range into a change range. `build_groups` rolls those up for the report.
  The plain-language caveats live in `rxdelta/limitations.py`.
- Config: `config/rxdelta.toml`, read by the typed loader in `rxdelta/config.py`
  (frozen dataclasses, every key validated). New sections go in the same file.
- Names: `drug_names` holds one name per RXCUI, filled from the committed CSV
  `data/reference/drug_names.csv` on every CLI command. `rxdelta/names/rxnav.py`
  is a urllib client used only by `names refresh`.
- Tests: 190 passing, pytest with fixtures in `tests/fixtures/` (two months,
  one formulary, two plans, 17 drugs). CI runs ruff, ruff format, mypy strict,
  pytest with coverage, and the synthetic demo end to end.

## The database

No database file exists in the checkout (`rxdelta.db` is the default path and is
gitignored). **Zero months are loaded locally.** To record the schema I loaded
the committed synthetic sample (`data/2025-01`, `data/2025-02`) and the test
fixtures into scratch databases.

| table | columns | rows, sample db | rows, fixture db |
|---|---|---|---|
| `formulary` | snapshot_month, formulary_id, ndc_11, ndc_raw, rxcui, tier_level, prior_auth, step_therapy, quantity_limit, quantity_limit_amount, quantity_limit_days | 49,496 | 34 |
| `plan_info` | snapshot_month, contract_id, plan_id, segment_id, formulary_id, plan_name, contract_name | 80 | 4 |
| `beneficiary_cost` | snapshot_month, contract_id, plan_id, segment_id, coverage_level, tier, days_supply, then cost_type, cost_amt, cost_min_amt, cost_max_amt for each of the four channels (`pref`, `nonpref`, `mail_pref`, `mail_nonpref`) | 3,600 | 20 |
| `rejected_rows` | snapshot_month, file_name, line_number, reason, raw_value | 514 | 0 |
| `drug_names` | rxcui (primary key), name, source, fetched_at | 648 | 648 |
| `ingest_log` | snapshot_month, file_name, sha256, row_count, rejected_row_count, loaded_at | 6 | 6 |

Findings:

- **Months loaded:** none in the real checkout. The sample has 2025-01 and
  2025-02; the fixtures have 2024-01 and 2024-02. Loaded months are read from
  `ingest_log`.
- **Drug names:** the CMS files carry only RXCUI and NDC. The only names are the
  648 synthetic ones in the committed cache, one full name per RXCUI, no
  ingredient or brand rows. The 648 rows also land in the fixture database
  because every CLI command loads the cache.
- **Plan table:** exists as `plan_info` with contract ID, plan ID, segment ID,
  plan name, contract name and formulary ID. Plan key is the triple
  `(contract_id, plan_id, segment_id)`.
- **Beneficiary cost:** wide rows, one per plan, coverage level, tier and days
  supply code, with four pharmacy channels side by side. The existing range
  uses only coverage level `1` (initial coverage), scales every leg to 30 days
  using the days supply code table in config, skips `not offered` legs, prices
  copay legs from `cost_amt` and coinsurance legs only from nonzero
  `cost_min_amt`/`cost_max_amt`. The change range is
  `after.low - before.high` to `after.high - before.low`, merged across plans by
  min and max.
- **Grain:** formulary rows are keyed by NDC. An RXCUI can cover several NDCs.
  The new tools take RXCUIs, so they aggregate NDC rows to the RXCUI.

## Names used by the new code

The new code uses the real names above. Where the prompt names something that
does not exist, the mapping is:

| prompt says | real equivalent |
|---|---|
| plan information table | `plan_info` |
| PA, ST, QL | `prior_auth`, `step_therapy`, `quantity_limit` (+ `quantity_limit_amount`, `quantity_limit_days`) |
| tier | `tier_level` |
| ten change types | the existing `rxdelta.types.ChangeType`, which has twelve: the ten listed plus `quantity_limit_tightened` and `quantity_limit_loosened` |
| change type names `pa_added` etc. | the existing values `prior_auth_added`, `step_therapy_added`, `quantity_limit_added`, and so on |
| config file | `config/rxdelta.toml` and `rxdelta/config.py` |
| decisions log | `docs/decisions.md` for Phase 1, linked from the existing root `DECISIONS.md` |

## What can be answered

With a database built from the new three month fixture set (below), every
category in the prompt is answerable:

| category | needs | available |
|---|---|---|
| coverage_lookup | formulary rows for one month and one formulary | yes |
| drug_change | the diff between two months | yes, three month pairs |
| filtered_list | the diff with filters | yes |
| count | counts in SQL | yes |
| cost | beneficiary cost rows at both tiers | yes, copay and coinsurance tiers |
| multi_step | names, plans, diff and coverage together | yes |
| no_answer | unknown drug, unloaded month, change that did not happen, drug not on formulary | yes |
| ambiguous | a brand with several RXCUIs whose changes differ | yes, planted |
| out_of_scope | no data needed | yes |
| typo | the fuzzy matcher | yes |

On the real checkout nothing is answerable until months are loaded.

## Gaps and how each is handled

1. **No months loaded locally.** Per the prompt, no CMS files are downloaded.
   A new deterministic fixture set, `tests/fixtures/ask/`, covers 2026-01,
   2026-02 and 2026-03, three formularies, five plans, about 45 products and
   every one of the twelve change types in each month pair. It is written by
   `scripts/build_ask_fixtures.py` and committed. `rxdelta evalset build` takes
   `--db`, and the published eval set is built from the fixture database. It
   must be rebuilt once real months are loaded.
2. **Names are synthetic and single-row.** The `drug_names` table is migrated in
   place to the new shape (one row per RXCUI per name type). Existing cache rows
   become `full` rows, and a brand row is derived from a trailing `[Brand]` in
   the full name when present. A fixture names CSV covers every RXCUI in every
   test fixture.
3. **RXCUIs in the fixtures are not real RxNorm identifiers.** The product names
   are real drug names so the questions read naturally, but the RXCUIs and NDCs
   are synthetic, in ranges that do not collide with the sample data.
4. **Diff cost on real data.** `diff_snapshots` takes about ten seconds on a real
   month pair. The change tools memoize the diff per database file and month
   pair within one process, so an agent session pays it once.
5. **`names refresh` already exists.** It stays exactly as it is. `names fetch`
   is new, writes to the new table, and uses `httpx` so tests can mock the
   transport.

## File layout added

```
config/rxdelta.toml                 new [tools], [names.match], [evalset], [eval] sections
rxdelta/config.py                   typed loaders for the new sections
rxdelta/db/schema.sql               drug_names in the new shape, drug_names_missing
rxdelta/db/migrate.py               in-place migration of an old drug_names table
rxdelta/names/normalize.py          name_norm
rxdelta/names/store.py              drug_names reads and writes, status
rxdelta/names/fetch.py              httpx RxNav client for names fetch
rxdelta/names/rrf.py                RXNCONSO.RRF loader
rxdelta/names/fixture.py            fixture names CSV loader
rxdelta/names/match.py              match_drug and product grouping
rxdelta/tools/envelope.py           ToolResult, ToolError, Provenance
rxdelta/tools/context.py            read-only connection, tool context
rxdelta/tools/changes_core.py       change rows built from diff_snapshots
rxdelta/tools/common.py             argument types and shared lookups
rxdelta/tools/list_months.py ... estimate_cost_change.py   one module per tool
rxdelta/tools/registry.py           specs, call_tool, schema export
rxdelta/ask/answer.py               AnswerKind, payload models, RunnerOutput, Runner
rxdelta/ask/values.py               canonical before and after strings
rxdelta/evals/gold.py               independent SQL for gold answers
rxdelta/evals/templates.py          question templates as data
rxdelta/evals/items.py              eval item format, reading and writing
rxdelta/evals/builder.py            evalset build, splits, manifest
rxdelta/evals/score.py              per-item scoring
rxdelta/evals/grounding.py          number grounding check
rxdelta/evals/runners.py            oracle and null runners, runner registry
rxdelta/evals/harness.py            eval run, outputs, compare
rxdelta/evals/report.py             summary.md and report.html
rxdelta/report/templates/eval_report.html.j2
scripts/build_ask_fixtures.py       writes tests/fixtures/ask/
tests/fixtures/ask/                 three months plus drug_names.csv
evals/handwritten.jsonl             20 hand-written items
evals/rxdelta_v1.jsonl              built eval set
evals/rxdelta_v1.manifest.json
docs/decisions.md, docs/eval-card.md, docs/phase2-handoff.md
tests/test_tools.py, tests/test_names_layer.py, tests/test_evals_*.py, tests/test_grounding.py
tests/noisy_runner.py               test-only runner that corrupts oracle answers
```

Runs go to `runs/`, which is gitignored.

The layout above was updated at the end of Phase 1 to match what was built.
