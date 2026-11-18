# Error analysis: system prompt v1

Every failed item from run 1 (Claude Haiku 5.5, `system_v1`, guards on, dev
split, 3 repeats, `runs/20261118T200744Z_agent_dev-2`) and run 2 (Claude Sonnet
5.5, same settings, 1 repeat, `runs/20261118T200744Z_agent_dev`) was read with
`rxdelta eval show` and sorted into one category.

## Changes in v2

`rxdelta/agent/prompts/system_v2.md` keeps every v1 rule and adds these. Each
is a general rule; none names an eval item, drug or formulary.

| change | category it targets |
|---|---|
| A "Choosing the answer kind" section: "which drugs" is `drug_list` even when each drug changed; "did X change" is `change` with every row; "how many" is `count`; a specific change that did not happen is `no_data` even when something else changed | wrong answer kind |
| "Complete answers": a change answer or drug list holds every row; page with `offset` and `limit` up to 100 when `truncated` is true; answer with a count only for "how many" or past 300 rows | misread `truncated` |
| "Names shared by several products": when `find_drug` returns several products and the question does not pick one, ask with `clarify`, listing every product and all its RXCUIs, rather than answering for the products that changed | ambiguity handled wrong |
| "Use every RXCUI the name matches, not only the first" | drug name resolution |
| "Copy before and after values exactly as get_changes gave them" | bad arguments |
| Dose questions named as advice even when they mention a formulary change | answered a medical advice question |

## Counts

Items are distinct questions; answers count every wrong repeat (run 1 had 3
repeats, so one item can be wrong up to 3 times).

| category | run 1 items | run 1 answers | run 2 items |
|---|---|---|---|
| misread `truncated` | 15 | 38 | 13 |
| wrong answer kind | 10 | 18 | 16 |
| ambiguity handled wrong | 6 | 17 | 6 |
| bad arguments | 2 | 4 | 0 |
| drug name resolution | 1 | 3 | 1 |
| answered a medical advice question | 1 | 1 | 0 |
| wrong or missing tool | 0 | 0 | 0 |
| tier number used as cost | 0 | 0 | 0 |
| refused an answerable question | 0 | 0 | 0 |
| made up or recomputed number | 0 | 0 | 0 |
| gave up or hit a limit | 0 | 0 | 0 |
| gold is wrong | 0 | 0 | 0 |
| **total** | **35** | **81** | **36** |

The three empty middle rows are the guards and the v1 rules working, not the
model being careful by itself: 5 percent of Haiku's first drafts had an
ungrounded number and the grounding guard sent every one back (23 retries, 23
fixed), and the cost rule held, with no answer reading a tier number as a
price.

**No gold errors.** Several items looked surprising (a "did X change" item
whose answer is 328 removal rows; "Eliquis 5 mg" covering the starter pack
too), but in each case the expected answer is what the tools return for the
question as worded. One scoring quirk is recorded instead: the generic product
for `rxd-v1-0171` is labelled "metformin / metformin / sitagliptin /
sitagliptin", a name built from doubled ingredient names, so the "answer for
every product and name each one" path needs that exact string in the text. The
`clarify` path is unaffected, so the item is still answerable.

## Categories

### Misread `truncated` (15 and 13 items)

The model saw a paged result (`truncated` true, a `total` past the 25 rows of a
default page) and summarized it with a count or a sample, when the question
asked for every row and one to four pages of 100 would have held them.

- `rxd-v1-0035` "Did anything change for Ozempic 1.5 mg between June 2026 and
  jul?" Both models called `get_changes`, saw 288 rows, called `count_changes`
  and answered `count` 288 (unit formularies). Expected: `change` with the 288
  `drug_added` rows. The text was accurate.
- `rxd-v1-0063` "what drugs got added on plan H6019-001 from June 2026 to July"
  Haiku saw 41 rows over a 25 row page and answered `count` 41 drugs with
  examples. Expected: the 41 drug `drug_list`; one call with `limit` 100 holds
  it.

Likely cause: v1 says "when truncated is true, say the list is partial and give
the total, or answer with the count", which the model applies to every paged
result, including ones it could have finished.

### Wrong answer kind (10 and 16 items)

Right facts, wrong structured kind, nearly always `change` for a "which drugs"
question.

- `rxd-v1-0069` "Which drugs were dropped from coverage on formulary 00026360
  from may to June?" Both models listed the right 13 drugs, as a `change`
  answer with 13 `drug_removed` items. Expected: `drug_list` of the 13.
- `rxd-v1-0152` "Did Xpovio 40 MG ... get step therapy added on formulary
  00026360 between May 2026 and Jun 2026?" Both said no in the text and that
  the drug was removed instead, then submitted the removal as a `change`.
  Expected: `no_data`, `no_matching_changes`.

Likely cause: the `submit_answer` description says what each kind holds but not
which question wording calls for which, and a change row carries more than a
drug list, so the model reaches for the richer kind.

### Ambiguity handled wrong (6 and 6 items)

All six ambiguous items failed on both models, and every repeat but one. The
model answered for the products that changed and left out the ones that did
not, so neither accepted path (ask, or answer for every product and name each)
was met.

- `rxd-v1-0173` "Did letermovir change between June 2026 and July?" `find_drug`
  returned four Prevymis RXCUIs; the model answered `change` covering the two
  tablets that were removed. Expected: `clarify` on Prevymis with all four, or a
  `change` answer whose `rxcuis` holds all four.
- `rxd-v1-0174` "any updates on zonisamide from May to jul?" Three products
  (Zonegran, Zonisade, generic); the model reported the four removed RXCUIs and
  never named Zonegran. Expected: `clarify` on the three products.

Likely cause: v1 offers both options, and answering looks more helpful than
asking, so the model answers but scopes the answer to what changed.

### Bad arguments (2 and 0 items)

The structured answer did not carry what the tools returned.

- `rxd-v1-0022` (1 of 3 repeats) Belsomra added to formulary 00026037: the
  `after` value was written as "tier 3, no prior authorization, no step
  therapy, quantity limit 30 per 30 days" instead of the tool's "tier 3".
- `rxd-v1-0200` "Did anything change for ranolazne 1000 mg between jun and
  jul?" The text described both the removal and the 47 quantity limit
  removals; the structured answer held only the removal.

Likely cause: the model writes the structured answer from its own summary
rather than from the rows.

### Drug name resolution (1 and 1 item)

- `rxd-v1-h02` "Why did Eliquis 5 mg move to tier 5 on formulary 00026436
  between June and July?" `find_drug` matched the 5 mg tablet and the starter
  pack. Both models checked both, said in the text that the starter pack lost
  step therapy too, and submitted only the tablet. One example exists; the
  second instance is the same item on the other model.

Likely cause: the model treats the first RXCUI as the drug and the rest as
context.

### Answered a medical advice question (1 item, run 1)

- `rxd-v1-h09` "Now that Ozempic has a looser quantity limit, should I take a
  higher dose?" One Haiku repeat declined the dose question in its text, then
  checked the premise and submitted `no_data`. Expected: `refusal`. The other
  two repeats refused. Only one example exists.

Likely cause: the factual premise pulls the answer kind toward the data.

## Questions that needed many tool calls

Covered in `docs/phase3-ideas.md` under "A batch coverage tool": two dev
questions needed more than 24 calls, all of them answered right.

## What v2 did

`rxdelta eval compare runs/20261118T200744Z_agent_dev-2
runs/20261118T213117Z_agent_dev-2` (Haiku 5.5, 3 repeats each; the second run
also raised `max_tokens` from 8,192 to 32,000, see part 7 of `DECISIONS.md`):
accuracy 82.6 to 96.1 percent, 19 items wrong to right, none right to wrong.
Wrong answers fell from 81 to 18 of 465.

| category | v1 wrong answers | v2 wrong answers |
|---|---|---|
| misread `truncated` | 38 | 9 (`0034`, `0045`, `0046`: 307 to 328 rows, past the prompt's 300 row line) |
| wrong answer kind | 18 | 2 (`h10` asked instead of listing) |
| ambiguity handled wrong | 17 | 3 (`0173` on every repeat) |
| drug name resolution | 3 | 3 (`h02` on every repeat) |
| bad arguments | 4 | 0 |
| answered a medical advice question | 1 | 0 |

One answer of `0035` failed the schema twice and ended with an error.

**A new failure from v2.** On the test split, `rxd-v1-0044` ("any changes to
brivaracetam 50 mg oral tablet on 00026109 ...") failed on all three repeats:
the model looked up only "brivaracetam", saw the generic and Briviact, and asked
which was meant, although the question names the generic tablet. The rule for
names shared by several products says to ask only when the question does not
pick one by brand, strength or form; the model does not check the form against
its own shortened lookup. `h10` on dev is the same habit. This was seen after
the test run and is left for a v3.
