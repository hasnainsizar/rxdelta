# Eval card: rxdelta_v1

`evals/rxdelta_v1.jsonl` is a set of 220 questions about Medicare Part D
formulary changes, each with a gold answer. It measures whether a question
answering runner (today the oracle and null runners, later an agent) gives the
right answer, refuses when it should, asks when a question is ambiguous, and
says only what the tool results support.

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

| category | share | example | expected kind |
|---|---|---|---|
| coverage_lookup | 10% | What tier is Crestor on formulary 00026125 in March? | coverage |
| drug_change | 15% | Did anything change for dapagliflozin between Feb 2026 and Mar 2026? | change |
| filtered_list | 15% | Which drugs had a quantity limit added on formulary 00026125 from 2026-02 to mar? | drug_list |
| count | 15% | How many drugs moved to a higher tier between January and February 2026? | count |
| cost | 10% | How much more might someone pay each month for Farxiga on plan H5678-001 after the change from January 2026 to 2026-02? | cost_range |
| multi_step | 10% | Which drugs were dropped from formulary 00026124 between January 2026 and February 2026 but are still covered on formulary 00026123 in February 2026? | drug_list |
| no_answer | 10% | did budesonide 0.16 mg get a looser quantity limit from 2026-01 to March? | no_data |
| ambiguous | 5% | Did semaglutide change between Jan 2026 and March 2026? | clarify |
| out_of_scope | 5% | Should I switch from Symbicort to Dupixent? | refusal |
| typo | 5% | Did anything change for Rybelus between jan and February 2026? | same as the base question |

The shares are for the 200 generated items and come from `[evalset.mix]` in
`config/rxdelta.toml`. Twenty hand-written items are added on top: compound
questions, wrong premises ("Why did Eliquis move to tier 5?" when it moved to
tier 2), questions that need the list of loaded months ("the latest month"),
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

Stratified by category: 70 percent `dev`, for tuning prompts, and 30 percent
`test`, held out and only run when reporting results. The current file has 155
dev and 65 test items.

## Known limits

- **Templated wording is less varied than real users.** Questions come from a
  few dozen templates. Phase 2 will add reviewed paraphrases.
- **Built on fixtures, not real CMS data.** No real months are loaded in this
  checkout, so the set is built from the three month fixture set in
  `tests/fixtures/ask/` (synthetic RXCUIs, real drug names). Rebuild it once
  real months are loaded; the manifest records the database row counts so a run
  against a different database is refused.
- **One structured answer per question.** A compound question that asks two
  things ("which drugs, and how many") is scored on one of them.
- **Cost items are priced tier moves only.** A drug joining or leaving a
  formulary has an open ended cost and is not used as a cost item.
- **"More expensive" wording is used for decreases too.** A user who asks "did
  it get more expensive" may be told it got cheaper; the gold range is negative.

## Rebuilding

```bash
make evalset        # builds evals.db from the fixtures, then the set and its manifest
```

That runs:

```bash
rxdelta evalset build --db evals.db --from 2026-01 --to 2026-03 --n 200 --seed 7 \
    --out evals/rxdelta_v1.jsonl
```

The same database, config and seed give a byte identical file. A test rebuilds
the set and compares it with the committed copy, so a change to the builder,
the templates, the fixtures or the config that changes the set has to come with
a rebuilt file. `evals/rxdelta_v1.manifest.json` records the item counts per
category and split, the seed, the months, the database row counts, the config
hash, the file's SHA-256 and the builder version. `rxdelta eval run` refuses to
run when the file's hash does not match its manifest.
