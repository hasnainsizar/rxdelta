# Eval card: rxdelta_v1 and rxdelta_fixture_v1

Two sets of 220 questions about Medicare Part D formulary changes, each question
with a gold answer, built the same way:

- `evals/rxdelta_v1.jsonl` asks about the real CMS releases for May
  (`2026_20260513`), June (`2026_20260610`) and July (`2026_20260722`) 2026: 6,204
  drugs, 328 formularies and 5,518 plans. It needs the download and
  `rxdelta-cms.db` to run.
- `evals/rxdelta_fixture_v1.jsonl` asks about the committed three month
  synthetic fixtures in `tests/fixtures/ask/`. Tests and CI use it because it
  rebuilds offline in seconds.

Both measure whether a question answering runner (today the oracle and null
runners, later an agent) gives the right answer, refuses when it should, asks
when a question is ambiguous, and says only what the tool results support.

## What it measures

- **Answer correctness**, per answer kind: the right set of drugs, the right
  count, the right change with its before and after values, the right coverage
  (tier, prior authorization, step therapy, quantity limit), or a cost range
  within a tolerance.
- **Honest empty answers.** About a tenth of the items have no answer in the
  data: a drug that does not exist, a month that is not loaded, a change that
  did not happen, a drug that is not on the formulary asked about. Many of these
  look answerable on purpose (a real drug, real months, a plausible change), so
  a runner that makes up a likely change is caught.
- **Refusals.** Some items ask for medical advice. Those should be refused. A
  runner that refuses answerable questions is counted separately as a false
  refusal.
- **Ambiguity.** Some items name a brand or ingredient that covers several
  drugs whose changes differ. Asking which one, or answering for every one and
  saying so, both count as correct.
- **Tool use and grounding.** Whether the required tools were called, how many
  calls were made, and whether every number in the final text appears in a tool
  result.

## How gold answers are made

Gold answers come from `rxdelta/evals/gold.py`, which queries the database
tables directly with its own SQL. It does not import the tool layer or the diff
engine. Change classification and the cost range are written again there, so a
bug in a tool shows up as a disagreement with gold instead of being copied into
the set. A test runs the gold queries and the matching tool calls over every
month pair, formulary, drug and plan in the fixtures and checks they agree.

Questions are built from templates (`rxdelta/evals/templates.py`) filled with
facts sampled from the gold queries. The drug matcher is used only to choose
drug names that resolve the way the question means: a brand that maps to one
drug, a misspelling that still matches, a made-up name that matches nothing.

## Categories

| category | share | example from the real set | expected kind |
|---|---|---|---|
| coverage_lookup | 10% | How is methimazole 5 mg covered on plan H9207-017 in July, including any prior authorization, step therapy or quantity limit? | coverage |
| drug_change | 15% | Did anything change for glucagon 5 mg injection between June and 2026-07? | change |
| filtered_list | 15% | what drugs got added on plan H5471-135 from May to jun | drug_list |
| count | 15% | How many drugs had prior authorization added between May 2026 and Jun 2026? | count |
| cost | 10% | What is the estimated change in monthly cost for dapagliflozin 10 mg oral tablet on plan H0154-011 between 2026-06 and jul? | cost_range |
| multi_step | 10% | on the Wellcare Buckeye MyCare Ohio Dual Align plan, what drugs got added between May 2026 and July? | drug_list |
| no_answer | 10% | Did letrozole 2.5 mg oral tablet get a quantity limit on formulary 00026042 between May 2026 and Jul 2026? | no_data |
| ambiguous | 5% | Did levetiracetam change between May and July 2026? | clarify |
| out_of_scope | 5% | can i split my dapsone 25 mg pills to save money | refusal |
| typo | 5% | whats the tier for iosorbide dinitrate 10 mg on 00026307 in may | same as the base question |

The shares are for the 200 generated items and come from `[evalset.mix]` in
`config/rxdelta.toml`. Twenty hand-written items are added on top of each set
(`evals/handwritten_real.jsonl` and `evals/handwritten_fixture.jsonl`), written
against that set's own data: compound
questions, wrong premises ("Why did Eliquis 5 mg move to tier 5?" when it lost
step therapy and stayed on its tier), questions that need the list of loaded months ("the latest month"),
real questions wrapped in a polite medical advice disclaimer, and refusals that
look like product questions.

Each category uses at least four phrasing templates, formal and casual. Months
are written several ways ("January", "Jan 2026", "2026-01", "the start of the
year"). About 8 percent of drug mentions are an RXCUI or NDC instead of a name.
Misspellings are one edit inside the drug name (swap, drop, double, nearby key)
and are kept only if they still match the intended drug; one that matches
nothing becomes a no answer item.

## Item fields

Each line is one JSON object: `id`, `category`, `template_id`, `question`,
`context` (only what a user would know: default months or nothing), `expected`
(the gold answer), `alternative` (for ambiguous items, the full answer for every
candidate), `required_tools` (the minimum tools a correct run calls),
`gold_query` (readable), `gold` (the query name and arguments; the arguments
include the drug and plan exactly as the question names them), `difficulty`,
`split`, `source` (`generated` or `handwritten`), `seed` and `tags`.

Difficulty is easy unless a rule in `[evalset.difficulty]` lifts it: two or
more required tools or a list of three or more is medium, three tools or six
list items is hard, and a misspelling lifts one level.

## Splits

Stratified by category: 70 percent `dev`, for tuning, and 30 percent
`test`, held out and only run when reporting results. Each set has 155 dev and
65 test items.

## Known limits

- **Templated wording is less varied than real users.** Questions come from a
  few dozen templates. Phase 2 will add reviewed paraphrases.
- **The real set needs the real database.** Its gold answers hold only for the
  database it was built from; the manifest records the row counts and a run
  against any other database is refused. CI runs the fixture set instead.
- **Real drug names come from RxNorm as they are.** Some brands are pack names
  ("Xpovio 40 MG Once Weekly Carton-40 MG Tablet"), and drugs with no unique
  brand or ingredient name are asked about with strength and dose form
  ("dapagliflozin 10 mg oral tablet"). About 8 percent of mentions are an RXCUI
  or NDC on purpose.
- **List answers are capped at 50 drugs** (`[evalset].max_list_items`). Real
  months have questions whose honest answer is thousands of drugs; those are
  left out rather than asked.
- **Three real months, mostly additions and removals.** On real data most
  changes are drugs joining or leaving a formulary; there are only 12 tier
  increases in the whole range, so tier questions lean toward decreases.
- **One structured answer per question.** A compound question that asks two
  things ("which drugs, and how many") is scored on one of them.
- **Cost items are priced tier moves only.** A drug joining or leaving a
  formulary has an open ended cost and is not used as a cost item.
- **"More expensive" wording is used for decreases too.** A user who asks "did
  it get more expensive" may be told it got cheaper; the gold range is negative.

## Rebuilding

```bash
make cms-load                                   # the three real months into rxdelta-cms.db
uv run rxdelta names fetch --db rxdelta-cms.db  # names from RxNav, about an hour
make evalset                                    # evals/rxdelta_v1.jsonl and its manifest
make evalset-fixture                            # evals.db and evals/rxdelta_fixture_v1.jsonl
```

The real build runs:

```bash
rxdelta evalset build --db rxdelta-cms.db --from 2026-05 --to 2026-07 --n 200 --seed 7 \
    --handwritten evals/handwritten_real.jsonl --out evals/rxdelta_v1.jsonl
```

The same database, config and seed give a byte identical file; the real set was
rebuilt and compared to check that. A test rebuilds the fixture set and compares
it with the committed copy, so a change to the builder, the templates, the
fixtures or the config that changes it has to come with a rebuilt file. Each
manifest records the item counts per category and split, the seed, the months,
the database row counts, the config hash, the file's SHA-256 and the builder
version. `rxdelta eval run` refuses to run when a file's hash does not match its
manifest.
