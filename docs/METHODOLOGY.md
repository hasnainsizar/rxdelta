# Methodology

How rxdelta turns two monthly CMS snapshots into a dollar range and a
severity score, in plain English. Every threshold and weight named here lives in
`config/rxdelta.toml`, not in the source.

## 1. What gets compared

The formulary file is keyed on formulary ID, not on plan. Many plans share one
formulary. The plan information file is what maps a plan to its formulary, so
rxdelta joins through it:

```
plan_info(contract_id, plan_id, segment_id) -> formulary_id -> formulary rows
```

The plan key is always the triple `(contract_id, plan_id, segment_id)`. A
contract ID on its own is not a plan.

For each plan present in both months, rxdelta resolves the formulary the plan
pointed at in the first month and the formulary it pointed at in the second, and
compares those two drug lists. Usually they are the same formulary. When a plan
switches formularies between months, the comparison still works, because the
unit of comparison is the pair of formularies that plan actually used.

Plans present in only one month are counted and reported separately. They are
not compared, because there is no counterpart to compare them to.

## 2. Change classification

For each drug and plan pair, rxdelta emits a list of changes, not one label.
A drug that moves up a tier and gains prior authorization in the same month
carries both.

| Classification | Fires when |
| --- | --- |
| Drug added | The NDC is on the second month's formulary and not the first |
| Drug removed | The NDC is on the first month's formulary and not the second |
| Tier up | The numeric tier level increased |
| Tier down | The numeric tier level decreased |
| Prior auth added or removed | The prior authorization flag flipped |
| Step therapy added or removed | The step therapy flag flipped |
| Quantity limit added or removed | The quantity limit flag flipped |
| Quantity limit tightened or loosened | Both months have a limit and the allowance per day changed |

Quantity limits are compared per day, not per window. A limit of 90 units per 30
days and 270 units per 90 days are the same allowance, so that pair produces no
change. If either month has a limit flag set but no amount or no window, the
comparison is skipped rather than guessed.

## 3. Cost estimation

Cost sharing comes from the beneficiary cost file, joined on the plan key and
the tier.

### The code tables

Three fields in that file are numeric codes. All three are transcribed into
`config/rxdelta.toml` from the BENEFICIARY COST FILE table on page 10 of
`docs/cms-reference/PUFRecordLayout-2026.pdf`. Getting these wrong is not a
cosmetic error, so they are written out here as well:

| Field | Code | Meaning |
| --- | --- | --- |
| `COVERAGE_LEVEL` | 0 | pre-deductible |
| | 1 | initial coverage |
| | 3 | catastrophic |
| `DAYS_SUPPLY` | 1 | 30 days |
| | 2 | 90 days |
| | 3 | other |
| | 4 | 60 days |
| `COST_TYPE_*` | 0 | not offered |
| | 1 | copay |
| | 2 | coinsurance |

There is no `COVERAGE_LEVEL` code 2, and the `DAYS_SUPPLY` codes are not in
ascending order of length. Code 3 has no published length, so it carries no
`days` value in config and any row using it is left unpriced rather than
normalized by a guess. A code that appears in a file and is not in these tables
fails the load, naming the code and the file.

Only one coverage phase is priced, set by `[impact].coverage_level`, which is
code 1, initial coverage.

### Reading the amounts

Each cost row publishes four pharmacy channels: retail preferred, retail non
preferred, mail preferred, mail non preferred. Each channel has a cost type:

- **Copay.** `COST_AMT` is a dollar figure. It is used directly.
- **Coinsurance.** `COST_AMT` is a fraction, not dollars; the record layout
  gives `.25` as 25 percent. A fraction cannot become a dollar figure without a
  drug price, and drug prices are not in these files. So only the
  `COST_MIN_AMT` and `COST_MAX_AMT` dollar bounds are used, and a bound of zero
  is read as no bound published rather than as zero dollars. A coinsurance leg
  with no usable bound contributes nothing and is counted as unpriced.
- **Not offered.** The channel does not exist for this plan and tier. Its
  amount columns are zero and the leg is skipped, because reading those zeros
  as a copay would report a free drug.

On the real May 2026 file, 99,490 of 100,157 coinsurance legs publish both
bounds as zero, so coinsurance tiers are largely unpriceable from this data.
That is stated in the report rather than filled in.

Every figure is scaled to a 30 day supply, set by `[impact].normalize_days`, so
a 90 day mail copay and a 30 day retail copay sit on one axis. A $30 copay on a
90 day supply becomes $10 per 30 days.

The result for one plan at one tier is a low and a high: the cheapest and the
most expensive normalized figure across all channels and all supply lengths.

## 4. The modal case

The full range answers "what could this cost?" across every pharmacy type and
supply length a plan publishes. That is the honest bound, but it is often too
wide to act on, and when it crosses zero it cannot even say which way cost moves.

So each row also carries a **modal case**: the same change priced for the single
combination a member most often actually fills. It is set in `[impact.modal]`
and defaults to a preferred retail pharmacy on a one month supply, in the
coverage phase from `[impact].coverage_level`.

The modal figure is **not a replacement for the range**. The report shows both,
with the range underneath in smaller type, and the column header says so. A
modal figure is a narrower claim, not a more certain one.

Two details matter:

- For a copay tier the modal case is one exact published dollar figure. For a
  coinsurance tier CMS publishes only min and max bounds, so the modal case is
  still a range, just a much tighter one, covering one channel and one supply
  length instead of all of them. No point estimate is invented.
- If a plan does not publish that channel and supply length at that tier, the
  row shows the range alone and says `not published`. Nothing nearby is
  substituted.

The **spans zero** flag stays on any row whose full range crosses zero, even
when the modal figure has a clear direction. That is the point of the flag: it
tells the reader the bound disagrees with the headline.

### Worked example: modal and range disagree

From the sample data:

```
Drug     Hydralium 500 MG oral extended release tablet
NDC      31882-1802-77          RXCUI 215983
Change   tier up, tier 4 to tier 5
Plans    11
Modal    +$28.86 to +$1,162.95   increase at modal case
Range    -$28.15 to +$1,643.90   spans zero
```

Tier 4 is a copay tier, tier 5 is coinsurance. At the modal case, a preferred
retail pharmacy on a one month supply, the member's cost sharing rises no matter
what: the tier 5 coinsurance minimum at that pharmacy already sits above the
tier 4 copay there. So the modal figure is unambiguously an increase.

The full range still crosses zero. It includes the cheapest tier 5 combination
the plan publishes, a mail order fill at the coinsurance minimum, against the
most expensive tier 4 combination, a non preferred retail copay on a long
supply. A member who moves from a non preferred retail pharmacy to preferred
mail order at the same time as this change could pay less.

Both readings are true. The modal figure is what happens if the member changes
nothing; the range is what the published data permits.

## 5. The range

For a tier move, where both sides are priced:

```
low  = after_low  - before_high
high = after_high - before_low
```

That is the widest honest bound on the change. The member's actual number
depends on which pharmacy they use and what supply they fill, and this range
covers every combination the plan publishes.

For a drug joining or leaving the formulary, one side is not priced at all. The
price a member pays for a drug with no formulary coverage is not published in
these files. Those changes are marked **open ended**: the range describes the
cost sharing on the covered side, and the report renders it as "or more". It is
not a delta and it is not presented as one.

If neither side has matching cost rows, the change is reported as **not priced**
rather than as zero.

## 6. Severity score

Severity exists only to sort the report. It is not a dollar figure and not a
probability. It runs 0 to 100.

Four components, each normalized to 0 to 1:

| Component | Definition |
| --- | --- |
| Cost | `abs(midpoint) / (abs(midpoint) + cost_reference)` |
| Direction | 1 if the change points toward the member paying more, 0 if less, 0.5 if mixed |
| Reach | `min(log10(1 + affected plans) / log10(1 + plan_reference), 1)` |
| Restriction | 1 if the change adds prior authorization, step therapy, a quantity limit, tightens one, or removes the drug; otherwise 0 |

Reach is logarithmic because the difference between one plan and eight plans
matters more than the difference between thirty and forty.

The cost term is a saturating curve, not a clamp. A move of exactly
`cost_reference` scores half the available cost weight, and larger moves keep
separating from each other with diminishing returns. An earlier version clamped
the term at 1, which gave every move above the reference an identical score and
destroyed the ranking among precisely the rows the report leads with.

```
severity = 100 * (w_cost * cost + w_direction * direction
                  + w_plans * reach + w_restriction * restriction)
                 / (w_cost + w_direction + w_plans + w_restriction)
```

Defaults: `w_cost = 0.40`, `w_direction = 0.15`, `w_plans = 0.25`,
`w_restriction = 0.20`, `cost_reference = 100.0`, `plan_reference = 40`.

Severity alone leaves ties on real data, so `[report].sort_order` declares what
breaks them: affected plan count, then range width, then distance from zero,
then the NDC as a final deterministic key. Run
`rxdelta summary --severity-distribution` to see how much the score actually
discriminates on a given comparison; the report states this in its own prose
when the spread is poor.

### Worked example

From the sample data, the highest scoring change in the `2025-01` to `2025-02`
comparison:

```
NDC          10420-1780-12
Change       tier up, prior auth added
Tier         3 (Preferred brand) to 5 (Specialty)
Plans        8
Range        +$49.41 to +$1,098.99 per 30 day supply
```

Where the range comes from. Tier 3 is a copay tier in this plan. Across the four
channels the 30 day equivalent runs $38.25 to $60.75. Tier 5 is a coinsurance
tier, so the published dollar bounds are used: $110.16 to $1,137.24 per 30 days.

```
low  = 110.16 - 60.75 =   49.41
high = 1137.24 - 38.25 = 1098.99
```

Where the score comes from.

```
midpoint   = (49.41 + 1098.99) / 2 = 574.20
cost       = min(574.20 / 100, 1)              = 1.000
direction  = both changes raise member cost    = 1.000
reach      = log10(1 + 8) / log10(1 + 40)      = 0.592
restriction= prior auth was added              = 1.000

severity = 100 * (0.40 * 1.000 + 0.15 * 1.000 + 0.25 * 0.592 + 0.20 * 1.000) / 1.00
         = 100 * 0.898
         = 89.79
```

The range dominates here. A $5 copay move affecting the same eight plans with no
new restriction scores about 32, and sorts far below it.

## 7. Grouping

The report lists change groups, not raw changes. A group is one drug with one
combination of change types and one tier movement, rolled up across every plan
it hit. The affected plan count is the number of distinct plan keys in the
group. The group's range is the widest range across those plans.

A single drug can appear in more than one group when different plans treat it
differently, for example when two formularies moved it to different tiers.

## 8. What the estimate does not account for

This list is stored once, in `rxdelta/limitations.py`, and rendered by both
the terminal and the HTML report so the two cannot drift apart.

- The deductible phase. A member who has not met the deductible usually pays the
  full negotiated price, not the cost sharing shown here.
- The catastrophic phase. Only the initial coverage phase is priced. The Part D
  benefit has had three phases since 2025, deductible, initial coverage and
  catastrophic; the coverage gap phase no longer exists, per page 1 of
  `docs/cms-reference/Methodology-PUF-2026.pdf`.
- Low income subsidy status. A member with extra help pays subsidy amounts set
  by CMS, not the plan's published cost sharing.
- The quantity actually dispensed. Amounts are scaled to a 30 day supply, which
  is not the same as what a given prescription fills.
- Negotiated rebates and pharmacy specific pricing that is not published in
  these files.
- Manufacturer discounts. CMS states that these files do not reflect discounts
  applied under the Medicare Part D Manufacturer Discount Program.
- Whether anyone is taking the drug. A tier move only costs a member money if
  that member fills that drug.

## 9. Data quality rules

- **Schema.** Expected columns per file type are declared in
  `rxdelta/ingest/schema.py`. A mismatch fails the load and names the missing
  columns and the unexpected columns separately, plus the file and the month.
  Nothing is coerced and no column is filled with nulls.
- **NDC.** Codes are normalized to 11 digits and the original string is kept in
  `ndc_raw`. Hyphenated 10 digit codes carry their segment lengths, so the zero
  goes in the short segment: 4-4-2 pads the first, 5-3-2 the second, 5-4-1 the
  third. Unhyphenated 10 digit codes do not carry that information and cannot be
  resolved. The policy is set in `[ingest.ndc]` and defaults to rejecting them
  with a reason. `rxdelta status` prints the count.
- **Rejected rows.** Every rejected row is written to `rejected_rows` with the
  source file, the line number and the reason. A file that rejects more than
  `[ingest].max_rejected_pct` of its rows aborts the whole month, and nothing is
  written.
- **Idempotency.** Loading a month deletes that month's partition and reinserts
  it inside one transaction. Loading the same month twice gives identical table
  contents and identical file hashes.
