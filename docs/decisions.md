# Phase 1 decisions

Judgment calls made while building the tool layer, the eval set and the eval
harness. Each entry says what was decided, why, and what would change it. The
decisions for the original build are in [`DECISIONS.md`](../DECISIONS.md) at the
repo root.

## Setup

**The checkout was not a git repository, so one was created.** The folder had no
`.git` anywhere. The prompt asks for a branch and a commit per step, so the
existing files were committed unchanged to `main` as a baseline and the work
happens on `feat/ask-phase1`. Would change: if this folder is later reconciled
with an upstream clone, the baseline commit should be replaced by the upstream
history and the branch rebased onto it.

**Package name mapping: none needed.** The package folder inside `rxdelta_main/`
is `rxdelta/`, which matches the prompt.

**Ruff no longer formats Markdown.** The installed ruff formats Python code
blocks inside `.md` files, so `ruff format --check` failed on
`docs/phase1-prompt.md` before any work started. Markdown is excluded from ruff
rather than rewriting the prompt. Would change: if the docs should have their
code blocks formatted, drop the exclusion and reformat them.

**This log lives at `docs/decisions.md`, beside the existing root `DECISIONS.md`.**
The prompt names `docs/decisions.md`. The root file records the original build
and is long; Phase 1 entries are kept separate and the two link to each other.

## Data

**No months are loaded locally, so everything is built on fixtures.** There is no
database file in the checkout. Per the prompt, no CMS files were downloaded. A
new three month fixture set in `tests/fixtures/ask/` covers every change type in
every month pair, `rxdelta evalset build` takes `--db`, and the committed
`evals/rxdelta_v1.jsonl` is built from the fixture database. **The published
eval set must be rebuilt once real months are loaded.** Would change: loading
real months.

**A separate fixture set rather than extending `tests/fixtures/snapshots/`.** The
existing fixtures are one formulary, two plans and 17 unnamed drugs, sized for
the diff tests that pin exact counts. The eval needs named products, several
formularies and plans, ambiguous brands and cost tiers. Adding all that to the
existing set would change the numbers those tests assert. Would change: nothing
obvious; the two sets serve different tests.

**Fixture product names are real, identifiers are synthetic.** Questions should
read like what people ask ("Ozempic", "Eliquis"), but the fixture RXCUIs and NDCs
are invented, in ranges that do not collide with the sample data, so nothing in
the repo implies they are real RxNorm or FDA codes. Would change: if a real
RxNorm subset is ever committed, switch to its identifiers.

## Mapping the prompt onto the real schema

**Twelve change types, not ten.** The existing classifier also emits
`quantity_limit_tightened` and `quantity_limit_loosened`. The prompt says the
enum must match the classifier exactly, so the tools use
`rxdelta.types.ChangeType` itself, with its existing values (`prior_auth_added`
rather than `pa_added`).

**Plan table exists.** `plan_info` has contract ID, plan ID, segment ID, plan name
and formulary ID, so `find_plan` searches plans, not only formulary IDs.
