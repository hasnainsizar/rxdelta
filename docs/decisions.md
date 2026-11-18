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

## Drug names

**`drug_names` is migrated in place, not replaced by a second table.** The old
table had one row per RXCUI. `rxdelta/db/migrate.py` runs before the schema on
every connect, detects the old shape by the missing `name_type` column, and
rebuilds the table in one transaction: each old name becomes a `full` row, and
a `brand` row is added when the name ends in an RxNorm style `[Brand]`. Empty
names, which the old cache used to mark "RxNav does not know this", are not
carried over. The report reads only `full` rows, so its output is unchanged
(checked by diffing `diff`, `summary` and `report` output against `main` on the
sample data). Would change: a need to keep the old table for some other reader.

**The primary key is `(rxcui, name_type, name_norm)`, and an RXCUI keeps one
full name.** Several ingredient and brand rows per RXCUI are the point, but two
full names for one concept would make the display name ambiguous, so writing a
new full name deletes the old one.

**`source` is not restricted to `rxnav`, `rrf`, `fixture`.** The committed cache
`data/reference/drug_names.csv` already uses `synthetic`, and every command loads
it, so a check constraint would break existing databases. The three new loaders
write exactly `rxnav`, `rrf` and `fixture`.

**`names refresh` is left exactly as it was; `names fetch` is new.** `refresh`
maintains the committed CSV cache through the old urllib client and is part of
the existing surface. `fetch` writes full, ingredient and brand names straight
to the database through `httpx`, which the prompt allows and which makes the
transport easy to mock. The two share the token bucket rate limiter and the
`[names]` rate, retry and timeout settings. Would change: retiring the CSV
cache, at which point `refresh` can be removed.

**RxNav endpoints.** Checked against the RxNav API documentation on 2026-11-17:
`/REST/rxcui/{rxcui}/properties.json` gives the concept name and returns no
`properties` element for an RXCUI outside the active set (treated as not found,
as is a 404); `/REST/rxcui/{rxcui}/related.json?tty=IN+MIN+BN` gives ingredients
(`IN`, and `MIN` for multi-ingredient names) and brands (`BN`) under
`relatedGroup.conceptGroup[].conceptProperties[].name`. The terms of service
allow 20 requests per second per IP; the default here stays at 8.

**`names fetch` skips RXCUIs already logged as missing.** "Resumable, skips
RXCUIs already stored" could mean names only. Skipping logged misses too means a
rerun after a full pass makes no requests at all. Would change: add a
`--retry-missing` flag if RxNav coverage improves.

**Hard failures are not logged to `drug_names_missing`.** A network failure says
nothing about whether the RXCUI exists, so it is counted and retried on the next
run rather than recorded as "not found".

**The RRF loader stores full names and bracketed brands, not ingredients.**
`RXNCONSO.RRF` carries names but the drug to ingredient links live in
`RXNREL.RRF`, which the prompt does not mention. Ingredient queries still match
through the token stage, because the ingredient is part of every clinical drug
name. Would change: accept an `--rel` file and walk the ingredient relations.

**Matcher scoring.** Exact and identifier matches score 100. A token match scores
the query length over the name length, so the shortest name containing every
token ranks first. A fuzzy match takes the better of the whole-string
`rapidfuzz` ratio and the weakest per-token ratio (each query token paired with
its closest name token), which handles both "ozempik" and "ozempik pen" without
letting one good token carry a bad one. Threshold 85, near-miss floor 60, both
in `[names.match]`.

**Products are grouped by brand, then ingredient, then full name.** Every RXCUI
that shares a brand name is one product; generics group by ingredient. So
"Ozempic" is one product with three RXCUIs, and "semaglutide" is three products
(Ozempic, Rybelsus, Wegovy).
