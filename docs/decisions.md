# Phase 1 decisions

Judgment calls made while building the tool layer, the eval set and the eval
harness. Each entry says what was decided, why, and what would change it. The
decisions for the original build are in [`DECISIONS.md`](../DECISIONS.md) at the
repo root.

## Setup

**The checkout was not a git repository, so one was created.** The folder had no
`.git` anywhere. The brief asks for a branch and a commit per step, so the
existing files were committed unchanged to `main` as a baseline and the work
happens on `feat/ask-phase1`. Would change: if this folder is later reconciled
with an upstream clone, the baseline commit should be replaced by the upstream
history and the branch rebased onto it.

**Package name mapping: none needed.** The package folder inside `rxdelta_main/`
is `rxdelta/`, which matches the brief.

**Ruff no longer formats Markdown.** The installed ruff formats Python code
blocks inside `.md` files, so `ruff format --check` failed on
`docs/phase1-prompt.md` before any work started. Markdown is excluded from ruff
rather than rewriting the brief. Would change: if the docs should have their
code blocks formatted, drop the exclusion and reformat them.

**This log lives at `docs/decisions.md`, beside the existing root `DECISIONS.md`.**
The brief names `docs/decisions.md`. The root file records the original build
and is long; Phase 1 entries are kept separate and the two link to each other.

## Data

**No months are loaded locally, so everything is built on fixtures.** There is no
database file in the checkout. Per the brief, no CMS files were downloaded. A
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

## Mapping the brief onto the real schema

**Twelve change types, not ten.** The existing classifier also emits
`quantity_limit_tightened` and `quantity_limit_loosened`. The brief says the
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
to the database through `httpx`, which the brief allows and which makes the
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
`RXNREL.RRF`, which the brief does not mention. Ingredient queries still match
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

## Tool layer

**An empty answer is `ok=False` with code `no_results`, and still carries data.**
The envelope has one error slot and the code list includes `no_results`, so an
empty but valid query uses it: the note says why it is empty (no changes of that
kind, drug not on that formulary), and `data` holds the empty shape so the
caller still sees what was searched. `unknown_drug` and `unknown_plan` are used
when the thing itself is not found. Would change: if the Phase 2 agent works
better with `ok=True` for empty answers, flip it in `envelope.failure`.

**Unknown formulary IDs return `unknown_plan`.** There is no `unknown_formulary`
code, `find_plan` is the tool that resolves formulary IDs, and the message says
which formulary was not found and to use `find_plan`.

**Change rows are one per RXCUI, formulary pair and change type.** The diff works
per NDC and plan. Questions are asked per drug and formulary, so `get_changes`
rolls the diff up, keeping the NDCs and plan IDs on each row and counting the
plans. Rows whose before or after coverage differs in any field stay separate,
so two NDCs of one RXCUI that changed differently are never merged.
`formulary_id` is the formulary the plans used in `to_month`;
`formulary_id_from` is kept for plans that switched formularies, and a
`formulary_ids` filter matches either.

**`count_changes` returns four counts, not one.** "How many changes" is
ambiguous: rows (drug, formulary and type combinations), distinct drugs,
formularies and plans are all reasonable readings. Each group and the total
carry all four and the description says which is which. The eval's count
questions ask about drugs and use the `drugs` count.

**Before and after values are canonical strings.** A tier move is `"3"` to
`"4"`, a flag is `"no"` to `"yes"`, a quantity limit is `"30 per 30 days"` or
`"none"`, a drug added is `null` to `"tier 3"`. The format lives in
`rxdelta/ask/values.py` and is shared by the tools, the gold queries and the
scorer, so comparison never depends on formatting.

**The diff is memoized per database file state and month pair.** On the real
files `diff_snapshots` takes about ten seconds. The memo key includes the file's
modification time and size, so a reload invalidates it. Would change: a long
running server would want a size bound on the memo.

**Provenance keys are not capped.** The brief asks for exactly which rows
produced an answer, so `count_changes` lists every row key it counted. On real
data that can be large. Phase 2 should keep provenance out of the text the agent
reads and use it only for the grounding check. Would change: a config cap with
a truncation flag, if the size becomes a problem.

**`estimate_cost_change` reuses `tier_cost` and `estimate_impact`, not
`build_groups`.** `build_groups` prices a whole diff and groups by NDC. The tool
needs one drug in one scope, so it calls the two functions `build_groups` is made
of, per plan, and spans the plans with the same min and max rule as
`impact._merge`. When the tier did not change, the change range is zero and the
note says other changes do not show in the figure. When the drug joins or
leaves the formulary, the existing open ended rule applies and the note says so.

**The plan ID format is `H1234-001` or `H1234-001-000`.** The segment defaults to
`000`, which is what the plan file uses for single segment plans.

**Tool schemas are post-processed.** Pydantic emits `$defs` for the change type
enum and `anyOf [X, null]` for optional fields. The exported schemas inline every
reference, drop titles, and turn an optional field into the plain type with no
`null`, because an omitted field already means "no filter". `required` is taken
from the model, so it always matches what validation enforces.

**There is no JSON Schema validator dependency.** The schema test checks the
structure that matters (no references, valid type names, required fields that
exist and match the model, every example round trips through its input model)
rather than adding `jsonschema`, which the brief's dependency rule would need a
justification for.

## Eval set

**Gold shares only the answer contract with the tools.** `gold.py` imports the
payload models and the canonical before and after strings from `rxdelta.ask`,
and the config (for the cost code tables). It does not import `rxdelta.tools`
or `rxdelta.diff`; both an AST check and a subprocess import check enforce
that. Classification, filters and the cost range are written again in SQL and
plain Python. Would change: nothing; this is the point of the gold module.

**Gold arguments carry the question's own wording.** The brief's item format
has a readable `gold_query` string. The oracle needs machine readable
arguments, so each item also has `gold: {query, args}`, and the arguments
include the drug and plan exactly as the question names them. The oracle looks
those up through `find_drug` and `find_plan` rather than using the gold RXCUI,
so a matcher bug shows up as an oracle miss.

**Eval items carry `alternative` and `tags` beyond the brief's format.**
`alternative` holds the full answer for every candidate of an ambiguous item,
which the scorer accepts in place of a clarifying question. `tags` marks
subtypes (wrong premise, needs the month list, no matching change), which keeps
the category list exactly as the brief defines it.

**Count questions ask about drugs.** "How many drugs moved up a tier" is scored
against the distinct RXCUI count (`count_changes` totals `drugs`), and the
expected answer says `unit: drugs`.

**A change answer is a set of (RXCUI, formulary, change type, before, after).**
The formulary is the one used in the later month. Without a formulary in the
question, every formulary's changes are part of the answer.

**Drug mentions must resolve to exactly one RXCUI outside the ambiguous
category.** "Ozempic" is three RXCUIs, so non-ambiguous Ozempic questions say
"Ozempic 1 mg". Every mention is checked against the matcher before use.

**The typo share is the `typo` entry of `[evalset.mix]`.** The brief lists
"typo share" separately from the mix, but the mix already has a typo category,
and a second key would let the two disagree. Misspellings that match nothing
become `no_answer` items (reason `unknown_drug`), capped at a quarter of the no
answer quota so that category keeps its planted mix. Misspellings that match a
different drug are discarded.

**No answer items are weighted toward the answerable-looking kind.** Of the no
answer items, the generator aims for 40 percent "change that did not happen",
25 percent "not on that formulary", 20 percent made-up drug and 15 percent
month not loaded, set in `[evalset.no_answer_mix]`.

**Hand-written items with a polite disclaimer around a real question expect the
real answer.** "I know you can't give medical advice, but did Januvia's tier
change?" is scored as a change question, because answering the factual part
while declining advice is the right behavior and a refusal is a false refusal.
Pure advice questions, however politely phrased, expect a refusal.

**Wrong premise items expect what actually happened.** "Why did Eliquis move to
tier 5?" expects Eliquis's real changes (tier 3 to 2 and prior authorization
added). When nothing at all happened ("Why was lisinopril removed?") the answer
is `no_data` with `no_matching_changes`.

**The committed set is built through the CLI.** `make evalset` builds
`evals.db` with `rxdelta load` and `rxdelta names load --fixture`. CLI commands
also load the committed sample name cache, so `evals.db` carries those 648
synthetic names too. That is harmless (none of them are formulary drugs) and it
is what a user's database looks like. A test rebuilds the set from a database
made the same way and compares it byte for byte with the committed file.

**The manifest has no timestamp.** A build time would make two identical builds
differ. The file hash, seed, config hash and database row counts identify a
build.

## Eval harness

**The oracle looks questions up by their text.** The `Runner` protocol passes
only the question and the context, and the context must never carry gold. The
oracle gets every item through its constructor and maps question text to gold
query. Its constructor refuses two items that ask the same question with
different gold queries.

**A runner exception is scored wrong, never right.** The harness catches it,
records the message in `error`, and the scorer marks the item incorrect even if
the placeholder answer happens to match (for example a `no_data` item).

**Grounding compares absolute values.** Text usually carries the sign in words
("$5.00 less"), so `-$5.00`, `$5 less` and `5` all match a tool value of `-5.0`.
A percentage matches either its number or its fraction (`25%` matches `0.25`).
Values are compared rounded to cents.

**What grounding does not count as a claim.** Years (1900 to 2099), `YYYY-MM`
months, identifiers (NDCs, plan IDs like `H1234-001`, anything with three or more
hyphenated parts or a part longer than three digits), numbers followed by a
strength unit (`1 mg`, `100 UNT/ML`), both sides of a ratio (`75/25`), numbers
that appear in the question, and anything inside a drug, plan or product name a
tool returned. A short hyphenated pair (`3-4`) and "10 to 20" are ranges, and
each end is checked. Would change: if an agent writes ranges like `3-400`, this
rule reads it as an identifier.

**A formulary ID in the text is a claim.** `00026123` is checked like any number,
so a made-up formulary ID is caught. It matches the tool's `"00026123"` because
both normalize to the same value.

**The worst items section lists failures first, then ungrounded passes, and
stops.** When every item is right and grounded the section says so instead of
showing correct items as "worst".

**`eval run` defaults to `--db evals.db`.** The eval set is built against the
fixture database that `make evaldb` writes there; the default makes the
documented command work without flags. Any other database is refused unless
its row counts match the manifest.

**A database whose row counts differ from the manifest is refused.** The brief
asks for a hash check on the eval file. Gold answers are also only valid for the
database they were computed from, so the harness compares the row counts the
manifest recorded too. Would change: a content hash of the tables, if row
counts ever prove too weak a check.

**`eval run` exits 1 when a gate fails.** `[eval].min_accuracy` and
`[eval].max_missed_refusal_rate` are the defaults, and `--min-accuracy` and
`--max-missed-refusal-rate` override them, which is how CI demands 100 percent
from the oracle. The null runner fails the default gate, by design.

**The eval report has its own template in the report's template folder.** It
copies the design tokens and table rules from `report.html.j2` and embeds the
same fonts, rather than restructuring the existing template into blocks and
risking a change to the committed report. About forty lines of CSS are
duplicated as a result.

**The noisy runner lives in `tests/`.** It is test-only, so it is not in
`RUNNERS` and `rxdelta eval run --runner noisy` is an error.

**Run folders get a numeric suffix on a clash.** Two runs started in the same
second (common in tests) would otherwise write into one folder.

## Config

**Every Phase 1 tunable is in `config/rxdelta.toml`.** New sections:
`[names.match]` (fuzzy threshold, near miss floor and count), `[tools]` and
`[tools.row_caps]` (page size, one cap per tool), `[evalset]` with
`[evalset.mix]`, `[evalset.no_answer_mix]`, `[evalset.splits]` and
`[evalset.difficulty]`, and `[eval]` (cost tolerance, accuracy and missed
refusal gates, workers, worst items shown). The RxNav rate, retries and timeout
were already in `[names]` and are shared by `names refresh` and `names fetch`.
The new sections are validated by Pydantic models inside the existing loader,
and a bad value is a `ConfigError` naming the section and the key, like every
other config error. Moving the builder's ratios into config left the eval set
byte for byte the same; only the manifest's config hash changed.

**What stays in code.** Request sizes the oracle asks for (it pages
`get_changes` 100 rows at a time and asks `find_drug` for 25) are bounded by the
tool caps in config, so they are not separate settings. The `find_drug` default
`limit` of 10 is part of the tool's input schema, as the brief specifies.

## CI

**Actions bumped to the current majors.** `actions/checkout@v4` became `@v7`
and `astral-sh/setup-uv@v5` became `@v10` (latest releases v7.0.1 and v10.3.3,
checked against the GitHub releases API on 2026-11-17). Both still take the
inputs the workflow uses.

**The eval steps rebuild the set and compare bytes, then gate on the oracle.**
CI builds `evals.db` with `make evaldb`, rebuilds the eval set into the runner's
temp folder and `cmp`s it and its manifest against the committed copies, so a
change that alters the set without a rebuilt file fails. Then it runs the oracle
on every item with `--min-accuracy 1.0 --max-missed-refusal-rate 0`. The whole
job was run locally on Python 3.11 and 3.12 before committing. No step uses the
network beyond installing dependencies, and none needs a secret.
