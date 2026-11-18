# Decisions

Every judgment call made while building rxdelta, with the evidence behind it, so
a reviewer can disagree with a specific choice rather than with the whole thing.

It is organised as six parts, not as a diary:

1. **The domain and the data model.** What a Part D formulary comparison is and
   how it is stored.
2. **Ingest.** How source files are read and what is refused.
3. **Architecture and the report.** How the layers are split and how the
   findings are presented.
4. **What real CMS data changed.** The part that matters most. Everything before
   it was built against synthetic fixtures; this is what happened when the code
   met the May and June 2026 public use files, including two bugs that had been
   corrupting every number the project produced.
5. **Incidents and open questions.** What went wrong operationally, and what is
   still unsettled.
6. **Question answering.** The tool layer, the drug name layer, the eval set
   and the eval harness that an `rxdelta ask` agent will be built on and
   measured by.

If you are reading one part, read part 4.


## Part 1: the domain and the data model

### Data and domain

> Superseded in part. The decision below was made without the record layout in
> hand and two of the three code maps turned out to be wrong. See
> [Three code lists were wrong](#three-code-lists-were-wrong-and-one-was-missing-entirely)
> in part 4. The reasoning is kept because the mitigation it describes, failing
> hard on an unrecognized code, is what eventually surfaced the error.

**The CMS code mappings in config are from the published record layout, not from
inspecting real files.** No real CMS download was available while building this,
so `[codes.coverage_level]`, `[codes.cost_type]` and `[codes.days_supply]`
reflect the Beneficiary Cost File record layout as documented, and the config
says so in a comment. The mitigation is that an unrecognized code is a hard
failure naming the code and the file, so a wrong or stale mapping surfaces on
the first load rather than becoming a wrong number in a report. Verify the
config against the data dictionary that shipped with the period you load.

**Cost type semantics live in config, not in the code.** A copay row carries
dollars in `COST_AMT`; a coinsurance row carries a fraction there, `.25` for 25
percent. Those have
to be read differently, and hardcoding "code 1 means copay" would put a CMS
convention in the source. So `[codes.cost_type]` declares a `kind` of `copay` or
`coinsurance` per code, and the estimator branches on the kind.

**Coinsurance rows are priced only from the CMS min and max dollar bounds.**
Turning a percentage into dollars needs a drug price, and drug prices are not in
these files. A coinsurance row with no published bounds contributes nothing and
is counted as an unpriced leg rather than assumed to be zero.

**Blank cost type is allowed; a non-blank unknown code is not.** Real files carry
blanks for channels a plan does not offer. A blank is stored and skipped during
estimation. Any non-blank code must be in the config.

**Costs are normalized to a 30 day supply.** Without this, a 90 day mail copay
sits in the same range as a 30 day retail copay and the range balloons for
reasons that have nothing to do with the change. The normalization window is
`[impact].normalize_days`.

**Only the initial coverage phase is priced.** Pricing the gap and catastrophic
phases would require knowing where in the year a member is, which the files do
not say. The phase is `[impact].coverage_level` and the limitation is stated in
every output surface.

**Formulary changes and drug removals are reported as open ended, not as a
delta.** When a drug leaves the formulary, the member pays a price that is not
published anywhere in these files. Reporting a delta would require inventing
that price. Instead the range describes the cost sharing on the covered side,
the change is flagged `open_ended`, and both the terminal and the report render
it as "or more". This is called out in the report prose and in the methodology.

**Quantity limits are compared per day.** A limit of 90 units per 30 days and
270 units per 90 days are the same allowance. Comparing raw amounts would report
a spurious tightening every time the window changed. If either side has the
limit flag set but no amount or no window, the comparison is skipped rather than
guessed.

**Plans present in only one month are counted, not compared.** There is no
counterpart to compare them against. The counts appear in the terminal output,
the JSON, and the report.

**A plan that switches formularies between months still compares correctly.**
The unit of comparison is the pair of formularies that plan actually pointed at,
resolved per plan per month, so a formulary switch reads as changes to that plan
rather than being missed.


## Part 2: ingest

### Ingest

**Unhyphenated 10 digit NDCs are rejected by default.** The segment lengths are
not recoverable from the digits. `[ingest.ndc].unhyphenated_10_digit` offers
three assume policies for anyone who knows their source well enough to pick one,
but the default writes the row to `rejected_rows` with a reason and the count
shows up in `rxdelta status`.

**Duplicate primary keys within one source file are rejected, not overwritten.**
Two rows claiming the same formulary and NDC in one month is a data problem
worth surfacing. The first wins and the second is rejected with a reason.

**The rejected row ceiling is enforced per file, not per load.** A formulary file
that is 90 percent junk should fail even if the two smaller files are perfect.
Exceeding the ceiling aborts the whole month and writes nothing.

**The load is delete-then-insert of the month partition inside one
transaction.** This is what makes reloading a month idempotent, and it means a
failed load leaves the previous state intact rather than a half written month.
A test loads the same month twice and asserts byte identical table contents.

**Schema validation compares the header to a declared layout and reports missing
and unexpected columns separately.** CMS layouts change between contract years,
and a message saying "these three columns are gone, these two are new" is more
useful than a stack trace. Duplicated column names are reported too.

**Exactly one file must match each file type pattern.** Two matching files is an
error naming both, not a silent pick of the first. Snapshot directories often
end up with a stray copy.

### Sample data and testing

**The sample generator plants preconditions as well as changes.** Placing a
"step therapy removed" change on a randomly chosen drug does nothing if that
drug never had step therapy. The generator sets the baseline state first, so all
twelve classifications appear in the demo diff.

**About one percent of generated NDCs are deliberately awkward.** The generator
emits 4-4-2, 5-3-2 and 5-4-1 hyphenated forms plus unhyphenated 10 digit codes,
so the demo exercises the normalizer and produces a non-empty `rejected_rows`
table. The reject rate stays under the configured ceiling.

**Fixture tests use a config with a raised rejected row ceiling where they plant
a bad row.** The fixture tables are deliberately small, so one bad row out of
eighteen is over the five percent production ceiling. Raising the ceiling in
those specific tests keeps the fixtures small and readable, and there is a
separate test asserting the ceiling fires.

**Coverage is measured on `diff/` and `ingest/` only.** That is where the brief
set the bar. Current line coverage is 95 percent against a target of 85. CI
fails below 85.

### Scope

**Cost estimation does not model the member's actual year.** No deductible
tracking, no accumulator, no plan year phase logic. Doing it properly needs
claims data. The limitations block says this in every output surface rather than
implying a precision that is not there.

**The pharmacy network file is not loaded.** The brief named three files. Adding
network data would let the report say which pharmacies a plan's preferred rates
apply at, which is a real gap, but it is out of scope here.


## Part 3: architecture and the report

### Architecture

**Added `rxdelta/types.py`, `rxdelta/db/connection.py`,
`rxdelta/db/queries.py` and `rxdelta/limitations.py` to the layout in the
brief.** The brief required that nothing in `diff/` import from `ingest/` beyond
shared types, which needs somewhere neutral for those types to live, and the
diff layer needs a read path to the database that is not the ingest writer.
`limitations.py` exists because the brief required the terminal and HTML text to
come from one shared constant.

**The report groups changes by drug and change signature.** The brief asks the
report to show "the count of affected plans per change", which only makes sense
if the reporting unit is above the individual plan. A group is one drug with one
combination of change types and one tier movement, rolled up across the plans it
hit. The same drug can appear in two groups when two formularies moved it
differently, which is the honest representation.

**Severity is documented as a sorting device, not a metric.** It mixes dollars,
direction, reach and restriction into one 0 to 100 number. That is useful for
ordering a table and misleading if read as a magnitude, so the docs say so
plainly.

**Reach is logarithmic in the plan count.** Going from one plan to eight matters
much more than going from thirty to forty, and a linear term let a single change
affecting every plan dominate purely on reach.

**The CLI catches domain errors and prints one line.** `RxdeltaError` and its
subclasses become a red `error` line and exit code 1. Anything else keeps its
traceback, because an unexpected exception is a bug and hiding it helps nobody.

### Report design

**Public Sans and IBM Plex Mono, vendored and inlined.** Public Sans is the
typeface of the US Web Design System, which is the register this report is
reaching for and a reasonable match for a tool that reads federal data. IBM Plex
Mono carries NDCs, RXCUIs and plan ids, where character disambiguation matters.
Both are OFL, and the license texts ship next to the woff2 files. Three static
faces total: Public Sans 400 and 600 instantiated from the variable font, and
Plex Mono 400. They are subset to Latin, total 31KB, and are base64 embedded at
render time so the report stays one file with no network dependency.

**The limitations block moved above the findings.** It was a footer section. A
reader who scrolls to the table and stops never sees it, which inverts the point
of the report. It now sits between the scope figures and the findings, in a
bordered block on a tinted ground, and a test asserts that ordering so it cannot
drift back down the page.

**Severity carries no hue at all.** Direction of cost is the only thing on the
page with color. Severity is a number plus a band label (High, Elevated,
Moderate, Low) differentiated by weight. Two independent color scales competing
in one table was noise, and a monochrome severity column is trivially safe for
grayscale printing and color vision deficiency. The bands are in
`[[report.severity_bands]]`, not the source, like every other threshold.

**Direction labels follow the cost range, not the rule change.** This started as
a design fix and turned into a correctness fix. A tier move upward can produce a
range that straddles zero, because a specialty coinsurance minimum can be below
a non preferred copay maximum. The report was labelling those "increase" and
colouring them red on the strength of the tier having moved up. On the sample
data that mislabelled 33 of the 50 listed changes. They now read "spans zero" in
neutral ink. Open ended changes still take their direction from the rule change,
since one side of those is genuinely unpriced.

**The rollup lost its proportion bars.** Across a 2088 to 40 spread a linear bar
renders an invisible nub on most rows, so it was decoration that also cost a
column of width. The counts are sorted and set in tabular figures, which already
carries the ranking.

**Row banding removed.** Zebra striping plus a hairline rule on every row is two
separators doing one job. The print stylesheet had already dropped the banding
and the printed proof read fine, which settled it. The band tint now means
exactly one thing on the page: the limitations block.

**At 375px the findings table restructures rather than scrolls.** Each row
becomes a labelled record with the drug as its heading. A horizontally scrolling
six column table on a phone hides the columns that matter most, which are the
last two.

**Tool files are gitignored, PRODUCT.md is not.** `npx impeccable install`
vendored about 6MB of a third party toolchain into `.claude/` and `.github/`.
That is someone else's source tree and does not belong in this repo, so it is
ignored. `PRODUCT.md` is the opposite: it is a short brief stating who reads
this report, the accessibility contract, and the principles the template is
accountable to (limitations stay prominent, print is a first class target,
nothing communicated by color alone). A maintainer editing the template needs
it, so it is committed. `DESIGN.md` was not generated: the visual system is
about forty lines of tokens at the top of one template, with comments, and a
second document restating them would go stale.

### Drug names

**RxNav has no bulk RXCUI to name endpoint**, so a refresh issues one request per
unresolved concept against `/REST/rxcui/{id}/properties.json`. "Batching" here
means bounded concurrency under a token bucket rate limiter rather than
multi-id requests. The limiter is shared across worker threads, so the
configured rate holds no matter how many workers run.

**The cache covers only drugs whose coverage changed.** Naming all 25,000
synthetic RXCUIs produced a 1.8MB CSV. Only a drug whose coverage actually
differs between the two months can ever appear in a report, so the generator
writes names for exactly that set: 648 rows, 52KB. The rule generalizes to real
data.

**`data/reference/` is un-ignored while the rest of `data/` stays ignored.** The
snapshots are generated and disposable; the name cache is the thing that makes
the demo work offline, so it has to be committed.

**Unresolved RXCUIs are cached with an empty name.** Without that, every refresh
would re-ask RxNav about concepts it has already said it does not know. The empty
rows are filtered out on the way into the database, so the report falls back to
the NDC. A hard network failure is deliberately *not* cached, so rerunning
retries only what failed.

**One in twenty nine sample RXCUIs is left out of the cache on purpose**, so the
demo report exercises the NDC fallback rather than only ever showing the happy
path.

### The modal case

**The modal figure is a bound, not a point.** For a copay tier the modal
combination gives one exact published dollar figure. For a coinsurance tier CMS
publishes only min and max, so a single number would have to be invented, most
obviously as a midpoint. That would have put a fabricated point estimate in the
headline column of a report whose whole thesis is that these are ranges. So the
modal case is a range too, just a much tighter one: one channel and one supply
length instead of all of them.

**Rows keep the spans zero flag even when the modal figure has a direction.** On
the sample data 354 groups are in exactly that state: the modal fill clearly
rises while the full bound still admits a saving, because the bound compares the
cheapest new combination against the most expensive old one. Both readings are
true and the reader needs both.

**A missing modal combination is marked, never substituted.** If a plan does not
publish that channel and supply length at that tier, the cell reads
`not published` and the row shows the range alone.

### The zero

**IBM Plex Mono was swapped for Roboto Mono.** Plex draws a dotted zero as its
*default* glyph and ships no `zero` OpenType feature, so
`font-feature-settings: "zero" 0` could not switch it off; the subset was not the
problem. Candidates were rendered and inspected rather than trusted: Noto Sans
Mono turned out to be *slashed*, Space Mono dotted, Roboto Mono plain. A glyph
contour count suggested all three were marked and was wrong, which is why the
specimen was rendered. Public Sans, the sans face, was already plain. Tabular
figures were preserved through the re-subset and re-verified by measuring that
`11111` and `00000` render at identical widths.

### Why the severity scores clustered

Measured before changing anything, on the previous sample data: 721 change groups
carried **24 distinct severity values**, and the top 50 rows carried 5. The cause
was overwhelmingly the generator, with a real secondary contribution from the
formula.

The score takes four inputs. On that data:

- **Affected plan count had exactly one distinct value.** All 40 plans were split
  evenly across 5 formularies, so every change reached exactly 8 plans. One of
  the four inputs was a constant.
- **Estimated cost had 22 distinct values across 721 groups**, because all 40
  plans published *identical* cost sharing (the tier 2 preferred 30 day copay was
  12.00 for every plan). That made the impact range a pure function of the tier
  pair, and there were 22 distinct tier pairs.
- Direction and restriction are two valued.

So the ceiling was 22 x 2 x 2 and the formula was being fed near constants. The
generator was fixed: formulary adoption is now uneven (3, 5, 8, 11 and 13 plans),
each plan prices its own benefit from a per plan factor, and the planted changes
and organic drift vary per formulary instead of being cloned five times.

**A secondary cause was real and was also fixed.** The cost term was
`min(amount / cost_reference, 1)`, a hard clamp. 73 of 721 groups exceeded the
reference and were flattened onto one value, and critically **37 of the top 50
rows** were in that plateau, collapsing 7 genuinely different midpoints. The
clamp became `amount / (amount + cost_reference)`, which is 0.5 at the reference
and asymptotic to 1, so large moves keep separating. This is not fabricated
spread: it recovers distinctions the data already contained and the clamp was
discarding.

Result on the regenerated sample: **117 distinct severity values across 665
groups**, largest tie 26 rather than 120.

**Ties are broken explicitly, from config.** `[report].sort_order` declares
severity, then affected plan count, then range width, then distance from zero,
then the NDC as a deterministic final key. The loader rejects an order that does
not end on the NDC, because without a unique final key the ranking is not
reproducible. `rxdelta summary --severity-distribution` exists so this can be
checked on any comparison rather than assumed, and the report says so in its own
prose when the score fails to discriminate.

### Small things

**The plan disclosure prints expanded via `::details-content`.** Current Chrome
hides closed disclosure content with `content-visibility` on the
`::details-content` pseudo element, which a `display: block` override does not
touch. This was established by printing a test page with four candidate rules and
checking which markers appeared in the PDF text. Both rules ship, since the
display rule still covers engines that predate the pseudo element.

**`--frozen-timestamp` stamps the compared months instead of the wall clock.**
The Makefile uses it for the committed copy, so regenerating
`docs/example-report.html` produces no diff unless the content actually changed.


## Part 4: what real CMS data changed

Everything in parts 1 to 3 was built against `scripts/generate_sample_data.py`
and had never failed. The sections below record what happened when the same code
was pointed at the May 2026 (`2026_20260513`) and June 2026 (`2026_20260610`)
public use files. In every case the real data was treated as correct and the code
as wrong until the CMS record layout said otherwise.

### Layout: discover table files by walking the tree

A CMS release nests each table in its own directory one level below the month
(`data/2026-05/2026_20260513/basic drugs formulary file  20260531/...txt`), and
both the directory and file names contain double spaces. The synthetic fixtures
are flat.

`reader.discover` now walks the month directory recursively instead of listing
it. Normalizing the layout on ingest was the alternative and was rejected: it
would mean either renaming files CMS shipped, which the task forbids, or copying
56MB per table to a staging directory for no benefit. Walking the tree reads both
layouts with one code path and the existing per table glob patterns, which
already tolerate the double spaces because the wildcard spans them.

A release also ships a `sample files` directory holding short extracts whose
names match the same patterns. Rather than tighten the patterns, which would be
fragile, `[source].exclude_dir_patterns` names the directory. The "exactly one
file per table" rule is unchanged, so an unexpected second match still fails.

### Encoding: the files are cp1252, not UTF-8

`config` declared `utf-8-sig`. The 2026 plan information file fails to decode as
UTF-8 at byte offset 5,719,186. There are 84 non-ASCII bytes in the file, all of
them 0xd3 or 0xe1, and they appear in Spanish plan names:

```
H4005|004|000|TRIPLE S ADVANTAGE, INC.|Optimo Plus (PPO)|...
H5427|112|000|FREEDOM HEALTH, INC.|Freedom Maximo (HMO-POS)|...
H6248|001|000|COMMUNITY HEALTH GROUP|Community y Mas (HMO C-SNP)|...
```

Those bytes are O acute and a acute in cp1252. The record layout does not state
an encoding, so this comes from the bytes on disk. `[source].encoding` is now
`cp1252`, which is a superset of ASCII and so reads the synthetic fixtures
unchanged.

### The plan information file has no PLAN_TYPE column

`ingest/schema.py` required `PLAN_TYPE`. Page 3 of the record layout lists the
fourteen fields of the plan information file and there is no such field; it was
invented by the synthetic generator and then required of the real data. The
requirement is removed. `CONTRACT_NAME`, which is real and documented, is stored
in its place, and `MA_REGION_CODE`, `PDP_REGION_CODE`, `STATE`, `COUNTY_CODE`,
`SNP` and `PLAN_SUPPRESSED_YN` are declared as known but unstored.

`SELECTED_DRUG_YN` was likewise rejected as unexpected in the formulary file. It
is documented on page 7 as marking a drug selected for negotiation under the
Medicare Drug Price Negotiation Program. Declared optional, not stored: it says
nothing about what a member pays this month.

### The plan information file is one row per plan per county

The first load that got past schema validation rejected 106,776 of 112,294 plan
information rows, 95.09 percent, as duplicate primary keys.

`COUNTY_CODE` is documented on page 3 as applicable to local MA contracts, and
the file carries one row per plan per county it is offered in. One plan appears
up to 387 times. Our grain is the plan, and the county columns are not stored.

Before changing anything I checked whether the repeats actually agree: across all
5,518 plan keys, **zero** carry a conflicting `FORMULARY_ID` between their county
rows. So collapsing identical repeats loses nothing.

The loader now compares the full stored projection on a repeated key. Identical
repeats are collapsed and counted separately from rejections, and the count is
printed. A repeat that disagrees on any stored value is still a rejection, with a
reason that says so. This is a statement about the grain of the file, not a
loosening of validation: a genuine conflict still fails.

### Three code lists were wrong, and one was missing entirely

The load then failed on `COST_TYPE` code 0. Looking the codes up on page 10
showed the existing config was wrong in more places than the one that failed:

| Field | Config said | Record layout page 10 |
| --- | --- | --- |
| COVERAGE_LEVEL 0 | Pre-initial coverage limit | pre-deductible |
| COVERAGE_LEVEL 1 | Coverage gap | initial coverage |
| COVERAGE_LEVEL 2 | Post out-of-pocket threshold | does not exist |
| COVERAGE_LEVEL 3 | absent | catastrophic |
| DAYS_SUPPLY 2 | 60 days | 90 days |
| DAYS_SUPPLY 3 | 90 days | other |
| DAYS_SUPPLY 4 | absent | 60 days |
| COST_TYPE 0 | absent | not offered |

Two of these were doing real damage:

- `[impact].coverage_level` was `0`, chosen to mean the initial coverage phase.
  Code 0 is the pre-deductible phase. Every cost estimate the project has ever
  produced priced the wrong phase. It is now `1`.
- `DAYS_SUPPLY` code 2 was treated as 60 days when it is 90, so any cost on that
  code was normalized by 60 instead of 90 and came out 50 percent too high.

Code 3, "other", has no published length. It carries no `days` value in config
and rows using it are counted as unpriced rather than normalized by a guess. It
does not occur in either real month.

`COST_TYPE` 0, "not offered", is a third cost kind. Its amount columns are all
zero. Treating those zeros as a copay would have reported a free drug at 104,882
channel legs in May alone; the estimator now skips the leg.

### Coinsurance is unpriceable on the real files

The estimator prices coinsurance from the CMS supplied `COST_MIN_AMT` and
`COST_MAX_AMT` dollar bounds, because `COST_AMT` on a coinsurance row is a
fraction (page 10 gives .25 as 25 percent) and dollars need a drug price that is
not in these files.

On the real May file, of 100,157 coinsurance legs, 99,490 have both bounds at
zero. Only 658 publish a maximum and 9 publish a minimum.

A zero bound means no bound was published, not a bound of zero dollars. The
estimator previously took a min and max of 0 at face value and would have priced
a 25 percent coinsurance tier at exactly $0.00 to $0.00. That is now treated as
unpriced. (These counts predate the pairing of package code swaps; see
[package code swaps](#package-code-swaps-were-reported-as-drugs-dropped-and-re-added)
for the current ones.) The visible consequence is that 286 of 1,367 real change groups carry
no price at all and 647 have no modal case, which the report states rather than
filling in. This limitation always existed; on synthetic data, where the
generator invented populated bounds, it never showed.

### Scale

Measured with `scripts/benchmark.py`, not by hand. Profiling first, then two
changes, each justified by a measurement:

- `tier_cost` was being called 2,060,982 times for about 38,000 distinct
  (plan, tier) answers. Memoized: `build_groups` went from 26.6s to 8.3s.
- The diff loaded all 2.25 million formulary rows for both months and threw
  almost all of them away. Only 882 rows differ in place between May and June,
  0.078 percent, with 28,538 additions and 27,794 removals, so 57,214 candidates
  out of 2.25 million. The candidate set is now selected in SQL and only those
  rows are built into objects. `diff_snapshots` went from 16.6s to 8.4s.
  Classification itself stays in `diff/engine.py`; SQL only prefilters.
- The loader held the whole file in memory as a list of tuples before writing.
  Rows now stream into the open transaction in chunks of
  `[ingest].insert_chunk_rows`. Peak RSS for a 1.3 million row month is 373MB.
  This did not make the load faster, and was not expected to: the load is
  parse bound, 26 of 33 seconds in row parsing.

Idempotency still holds at this scale. Loading 2026-05 twice gives byte identical
`formulary`, `plan_info`, `beneficiary_cost` and `rejected_rows` tables by
SHA256 over every row, and an `ingest_log` identical except for `loaded_at`,
which records when the load ran and is meant to differ.

### What the real month pair does and does not exercise

May and June 2026 are both contract year 2026 and share one record layout
version, so **this pair does not exercise the layout change detection path**. The
schema mismatch machinery was exercised only by pointing the old code at the new
files, which is how the `PLAN_TYPE` and `SELECTED_DRUG_YN` findings surfaced.

Nine of the twelve classifications fire. `tier_up`, `step_therapy_added` and
`quantity_limit_tightened` produce zero rows. That is the data, not a bug: a
direct SQL check over all 328 formularies confirms zero tier increases, zero step
therapy additions and zero quantity limit tightenings between the two months,
against 22 tier decreases, 63 step therapy removals and 99 quantity limit
loosenings. Every utilization change in this pair except prior authorization went
in the member's favour.

The formulary file carries 6,115 distinct NDCs across 1,123,842 rows, and exactly
6,115 distinct RXCUIs. The layout calls the NDC field an "11-digit proxy National
Drug Code", so CMS publishes one proxy NDC per RxNorm concept and the same drug
repeats across the 328 formularies. Every NDC in both real months is unhyphenated
11 digit, so the hyphenated and ambiguous branches of the normalizer are
exercised only by the synthetic fixtures. The real rejection rate is zero.


### Package code swaps were reported as drugs dropped and re-added

**What was wrong.** CMS lists exactly one NDC per RXCUI per formulary (May 2026:
1,123,842 rows, 1,123,842 distinct formulary and RXCUI pairs) and changes which
NDC stands for a drug from one month to the next. The diff compared NDC to NDC.
A swap therefore came out as one NDC removed and another added, so a drug that
never left a formulary was reported as dropped and re-added, and anything that
changed in the same month as the swap (a tier move, a step therapy requirement
lifted) was never compared, because no single NDC carried both sides.

**How many rows it affected.** Drug and plan changes from `rxdelta diff`, before
and after the fix, on the three real month pairs:

| | May to June | June to July | May to July |
|---|---|---|---|
| changes | 1,030,491 to 222,989 | 471,712 to 266,044 | 1,489,015 to 477,070 |
| change groups | 1,367 to 686 | 4,339 to 4,138 | 5,515 to 4,720 |
| drug removed | 465,646 to 62,769 | 218,116 to 112,793 | 677,719 to 169,997 |
| drug added | 481,042 to 75,523 | 239,164 to 133,841 | 714,163 to 203,759 |
| tier down | 346 to 380 | 1,891 to 6,557 | 1,436 to 6,136 |
| tier up | 0 to 0 | 0 to 208 | 0 to 208 |
| step therapy removed | 323 to 357 | 302 to 496 | 625 to 853 |
| prior auth added | 79,050 to 79,900 | none | 79,050 to 79,900 |

Between June and July every hidden tier move belongs to four dapagliflozin
products (the 5 and 10 MG tablets and the two extended release combinations with
metformin), whose brand NDCs were replaced by generic ones while their tiers
moved: down on most formularies, up from tier 3 to 6 on 104 plans. For the May
to June report the fix also changes the pricing counts quoted under
[coinsurance](#coinsurance-is-unpriceable-on-the-real-files): 235 of 686 groups
carry no price and 541 have no modal case.

**How it was found.** Not by the diff tests, which use fixtures with one NDC per
drug that never changes. The question layer (part 6) rolls changes up to RXCUIs,
and a check of the real months found that 24,179 of the 27,794 RXCUI level
removals between May and June were drugs still listed on that formulary under
another NDC. Looking at why showed the one NDC per RXCUI layout, and pairing the
NDCs then surfaced the tier moves hidden inside swaps. The question tools were
fixed first; the diff and the report followed, so that all four readers of the
data use one rule.

**The fix.** `rxdelta/swaps.py` holds the rule: on a formulary pair, an NDC
removed and an NDC added for the same RXCUI are one drug's before and after, and
`classify` compares them like any other pair. A removed NDC with no partner is a
removal only if the RXCUI is gone from the later formulary, and an added one only
if the RXCUI was absent before. The diff applies it per formulary pair, the
report and `summary` inherit it from the diff, the question tools take the diff's
output, and the eval's gold answers call the same function. `tests/test_swaps.py`
covers the rule directly and, on a fixture with a swap and nothing else, a swap
with a tier move and a real removal, checks that `diff`, the report, the tools
and gold all show exactly one tier change and one removal. On the real months
gold and the tools agree on every row of all three pairs (8,561, 19,041 and
26,917 rows), and the diff's own output agrees with the tools drug for drug and
plan for plan. `docs/example-report-cms.html` was regenerated.

Diff and score for May to June went from 21.20 to 6.48 seconds, because most of
the old changes were never real. The first version was far slower instead: the
lookup of a formulary's RXCUIs used `SELECT DISTINCT`, and SQLite answered it
from the `(snapshot_month, rxcui)` index by scanning the whole month, about a
second per formulary. Without `DISTINCT` it uses the formulary index.

## Part 5: incidents and open questions

### `make clean` deleted the real CMS download

While verifying the synthetic demo I ran `make clean`, whose recipe was
`rm -rf $(DATA) ...`. That was written when `data/` held nothing but generated
fixtures. It now also holds real CMS releases, gigabytes of them, downloaded by
hand, and it deleted them.

`clean` now removes only the two synthetic months it generates, by name, plus the
working database and report. It never touches `data/` wholesale. The committed
drug name cache under `data/reference/` was always at risk from the old recipe
too and is now safe.

### Open questions

Stated rather than smoothed over. None of these were adjusted away, and the
severity formula was not touched to make any of them look better. The same list
appears in the README.

- **104 groups share a severity of exactly 60.00.** A hard cluster on a round
  number suggests a term in the score is saturating or falling back to a default
  rather than varying. Not investigated.
- **Severity is bimodal on real data.** The 20 to 39 and 60 to 79 bands hold 71
  percent of groups (79 before package code swaps were paired). Real changes
  cluster into a few archetypes, so the score separates archetypes well and
  separates within them poorly. With swaps paired the May to June comparison has
  103 distinct severity values across 686 groups, and the 104 group tie at 60.00
  is unchanged.
- **The layout change detection path has never been triggered by data.** May and
  June 2026 share one record layout version, so it fired only because old code
  met new files.
- **5,517 of 5,518 plans were affected.** When nearly every plan is affected the
  figure stops discriminating; plans per change group is probably the more
  informative number and the report leads with the wrong one.

### The committed CMS report predates the limitations correction

> Superseded. The real files were downloaded again with `rxdelta fetch-cms` and
> `docs/example-report-cms.html` was regenerated after package code swaps were
> paired, so it now carries the corrected limitations text too.

`docs/example-report-cms.html` was generated before the limitations text was
corrected, and the real files it was built from are gone, so it cannot be
regenerated. It still names the coverage gap as an unpriced phase. Page 1 of
`docs/cms-reference/Methodology-PUF-2026.pdf` says the Part D benefit has had
three phases since 2025 and the coverage gap phase was eliminated, so the text
shipped in `rxdelta/limitations.py` and in `docs/example-report.html` is the
corrected one. The CMS report is a point in time artifact and is left as it is.


## Part 6: question answering

The groundwork for `rxdelta ask`: a read-only tool layer an agent will call, a
drug name layer so questions can name drugs instead of RXCUIs, an eval set with
gold answers computed independently of the tools, and a harness that scores any
runner against it. Like parts 1 to 3, most of this was first built against
synthetic fixtures, because no real months were loaded in the checkout. Where
real data later changed a decision, the entry says so.

### Setup

> Superseded. The upstream repository was later added as `origin`, and the
> feature commits were moved onto `origin/main` with
> `git rebase --onto origin/main main feat/ask-phase1`. The local baseline
> commit is gone. Upstream had two commits the baseline lacked (removing
> `.claude/` and trimming `.gitignore`); the rebase applied without conflicts,
> and the only `.gitignore` line Phase 1 adds is `runs/`.

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

> Superseded. These entries first lived in `docs/decisions.md`, as the brief
> named it. They were moved here so every judgment call is in one file, in the
> same shape as parts 1 to 5.

**This log lived at `docs/decisions.md`, beside the root `DECISIONS.md`.** The
brief named that path. Keeping two decision logs meant two places to look and
two styles, so the entries became this part.

### Data

> Superseded. The May, June and July 2026 releases were later downloaded and
> loaded, and `evals/rxdelta_v1.jsonl` is now built from them. The fixture set
> moved to `evals/rxdelta_fixture_v1.jsonl` and is still what tests and CI use.
> See [What real CMS data changed](#what-real-cms-data-changed) below.

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

### Mapping the brief onto the real schema

**Twelve change types, not ten.** The existing classifier also emits
`quantity_limit_tightened` and `quantity_limit_loosened`. The brief says the
enum must match the classifier exactly, so the tools use
`rxdelta.types.ChangeType` itself, with its existing values (`prior_auth_added`
rather than `pa_added`).

**Plan table exists.** `plan_info` has contract ID, plan ID, segment ID, plan name
and formulary ID, so `find_plan` searches plans, not only formulary IDs.

### Drug names

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

> Superseded in part. Brands are no longer read from `related.json?tty=BN`;
> on real data that attached brand names to 1,956 generic drugs. See
> [RxNav gives generic drugs brand names](#rxnav-gives-generic-drugs-brand-names).

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

### Tool layer

> Superseded. The first version below returned `ok=False` with a `no_results`
> code. On review that read as "the call failed", which invites an agent to
> retry or apologize instead of answering that nothing changed.

**An empty answer is a success: `ok=True`, empty data, and a plain note.** A
query that ran and matched nothing (no changes of that kind, the drug is not on
that formulary, no months loaded) returns its normal data shape with empty
lists, `total` 0 and a `note` that says why. `ok=False` is kept for calls that
could not be answered as asked: `invalid_args`, `month_not_loaded`,
`unknown_drug`, `unknown_plan` and `internal`. The `no_results` code was
removed, and a test pins the five that remain. The oracle now turns an empty
success into `no_data` itself (no change rows, no coverage rows, a zero count,
no priced plans); the scorer needed no change, because it never read the error
code except to count `invalid_args` calls. The eval set did not change. Would
change: nothing expected.

The first version, kept for the record: an empty but valid query used
`ok=False` with code `no_results`, the note said why it was empty, and `data`
held the empty shape so the caller still saw what was searched.

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

### Eval set

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

> Superseded in naming. This describes the fixture set, now
> `evals/rxdelta_fixture_v1.jsonl`, built by `make evalset-fixture`. `make
> evalset` now builds the real set from `rxdelta-cms.db`.

**The committed set is built through the CLI.** `make evalset` builds
`evals.db` with `rxdelta load` and `rxdelta names load --fixture`. CLI commands
also load the committed sample name cache, so `evals.db` carries those 648
synthetic names too. That is harmless (none of them are formulary drugs) and it
is what a user's database looks like. A test rebuilds the set from a database
made the same way and compares it byte for byte with the committed file.

**The manifest has no timestamp.** A build time would make two identical builds
differ. The file hash, seed, config hash and database row counts identify a
build.

### Eval harness

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

> Superseded. The defaults are now the real set and its database,
> `--set evals/rxdelta_v1.jsonl --db rxdelta-cms.db`. CI and the tests pass the
> fixture set and `evals.db` explicitly.

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

### Config

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

### CI

> Superseded in part. `astral-sh/setup-uv@v10` failed to resolve on the
> first push ("unable to find version `v10`"): that action publishes only full
> version tags, with no moving major tag. The workflow pins `v10.3.3`, the
> latest release. `actions/checkout@v7` resolved, since that repository does
> keep a `v7` tag.

**Actions bumped to the current majors.** `actions/checkout@v4` became `@v7`
and `astral-sh/setup-uv@v5` became `@v10` (latest releases v7.0.1 and v10.3.3,
checked against the GitHub releases API on 2026-11-17). Both still take the
inputs the workflow uses.

> Superseded in naming. CI now rebuilds and gates on the fixture set,
> `evals/rxdelta_fixture_v1.jsonl`. The real set needs the CMS download, which
> CI does not have.

**The eval steps rebuild the set and compare bytes, then gate on the oracle.**
CI builds `evals.db` with `make evaldb`, rebuilds the eval set into the runner's
temp folder and `cmp`s it and its manifest against the committed copies, so a
change that alters the set without a rebuilt file fails. Then it runs the oracle
on every item with `--min-accuracy 1.0 --max-missed-refusal-rate 0`. The whole
job was run locally on Python 3.11 and 3.12 before committing. No step uses the
network beyond installing dependencies, and none needs a secret.

### What real CMS data changed

Everything above was first built and passed on the fixtures, with the oracle at
100 percent. Then the May (`2026_20260513`), June (`2026_20260610`) and July
(`2026_20260722`) 2026 releases were loaded, names were fetched for all 6,204
formulary RXCUIs, and the eval set was rebuilt from them. The oracle's first run
on the real set scored 96.5 percent. Each cause below was found, fixed, tested
on the fixtures where the fixtures could be made to show it, and the set was
rebuilt until the oracle reached 100 percent on all 220 items.

#### The CMS download page moved

The page the README linked returns "Page Not Found". The monthly files now live
on data.cms.gov, and its catalog (`https://data.cms.gov/data.json`) lists every
release's archive URL. Each archive is about 2.3GB, and 98 percent of that is the
pharmacy networks file, which rxdelta does not read. The machine had 3.6GB free.
The server honours HTTP range requests, so only the three needed tables (about
10MB zipped per month) were read out of each remote archive, by reading the zip
directory from the end of the file. Raw files sit under `data/`, which stays
gitignored. Loads matched part 4: 1,123,842 formulary rows in May, no rejected
rows in any month.

#### The demo name cache put invented names on 12 real drugs

Every CLI command loads `data/reference/drug_names.csv`, the synthetic cache
behind `make demo`. Its made-up RXCUIs (200010 and up) collide with real ones,
so before any fetch 12 real formulary drugs already had names like
"Quetiaprin 25 MG oral solution" (really carvedilol 6.25 MG, RXCUI 200031), and
`names fetch` skipped them as already named. Names from `rxnav`, `rrf` and
`fixture` now count as real; anything else is a placeholder that a real source
replaces and that never replaces a real name. The fetch then reported 6,204
fetched and 0 already present.

#### RxNav gives generic drugs brand names

`related.json?tty=BN` for a generic clinical drug returns every brand of the same
ingredient: generic carvedilol came back with "Coreg", generic medroxyprogesterone
with "Provera". 1,956 of the 4,990 RXCUIs that had brand rows were generics, so
"Coreg" would have matched the generic and grouped it with the brand. RxNorm
names a branded concept with its brand in trailing brackets, and that is now the
only brand source; the fetch asks for `IN` and `MIN` only. The real database was
repaired by applying the same rule to the stored full names rather than
refetching for an hour; the result is what a fresh fetch now writes (3,191 brand
rows, none on a generic).

#### One NDC per RXCUI, swapped every month

In every real month each formulary lists exactly one NDC per RXCUI
(1,123,842 rows, 1,123,842 distinct formulary and RXCUI pairs), and the NDC that
stands for a drug changes from month to month. The diff works per NDC, so a swap
reads as one NDC removed and another added. Between May and June, 24,179 of the
27,794 removals were swaps: the RXCUI was still on that formulary. At the RXCUI
grain the tools would have told someone 87 percent of "dropped" drugs were
dropped when they were not. Worse, the diff never compares the old and new NDC,
so a coverage change made in the same month as a swap vanished: between June
and July 300 of the 365 tier decreases and all 12 tier increases happened
inside swaps.

The tools now pair the swapped out NDC with the swapped in one under the same
RXCUI and formulary pair and run them through the diff's own `classify`, so
nothing is reclassified by new code. Gold does the same pairing in its own SQL.
After the fix gold and the tools agree exactly on all three real month pairs
(8,561, 19,041 and 26,917 change rows). The fixtures gained two swaps, one with
identical coverage and one that moves Januvia from tier 3 to 4 while its NDC
changes, so both cases are tested offline.

> Superseded. The pairing was later moved into `rxdelta/swaps.py` and the diff
> itself, so `diff`, `summary` and `report` use it too; see
> [package code swaps](#package-code-swaps-were-reported-as-drugs-dropped-and-re-added)
> in part 4.

The existing `diff` and `report` commands were left as they are, because they
must not change. They report per NDC and therefore still count swaps as an
addition plus a removal and still miss coverage changes inside a swap. That is
now an open question for the report, not just for the question layer.

#### An unpriced tier with no tier move returned no change

Six cost items and one misspelled cost item failed the same way. When a drug's
tier did not change but the plan publishes no dollar amount for that tier
(coinsurance with zero bounds, which part 4 found is most coinsurance), the tool
said in its note that the change was zero and then returned no change range,
marked unpriced. The brief asks for a zero change range when the tier did not
move, and gold followed the brief. The tool now returns zero, priced, with the
before and after amounts left empty. The fixtures always priced every tier the
generator happened to pick, so this never showed there; a fixture test now
covers tier 5 on plan S9012-001, which publishes no bounds.

#### Plan names repeat

"Gold Heart & Diabetes" matches 41 plans on three formularies. The builder had
used it as if it named one plan; the oracle, like any agent, took the first
match and found nothing. Plan name questions now use only names that pick out
exactly one plan, matched the way `find_plan` matches.

#### The fixtures had hidden how few real drugs have a plain name

On real data 43 percent of formulary RXCUIs had no brand or ingredient name
that resolved to them alone, and the builder had quietly fallen back to writing
the RXCUI, so 35 percent of drug mentions were IDs against a configured 8. It
now also tries the ingredient or brand with strength and dose form
("dapagliflozin 10 mg oral tablet"), uses an ID only at the configured share, and
skips a drug it cannot name. Two more limits came from real sizes: list
answers reached 3,335 drugs ("which drugs were dropped between May and July"),
so `[evalset].max_list_items` caps them at 50, and 41 ambiguous names and 50
drug classes had more drugs than `find_drug` returns (its cap is 25), which no
agent could list in full, so both now stay within that cap.

#### Scale

On the fixtures everything ran in seconds. On real data the builder's set up
took 183 seconds, 114 of them in the fuzzy stage of the matcher, reached by
candidate drug mentions that did not match exactly. Mentions must resolve
exactly, so their check now skips the fuzzy stage, and the matcher gained exact
name and token indexes. Gold reclassified a whole month pair on every
candidate; it now memoizes per database file state, like the tools. A real
build takes about 80 seconds and an oracle run over all 220 items about two and
a half minutes.

#### A duplicate question

Rebuilding the fixture set after the swap fixes produced two out of scope items
with the same text, "what dose of trelegy ellipta should i be taking". That
template names one drug, but the duplicate check keyed on an unused second
drug too. The builder now keeps only arguments a template uses and refuses any
question text it has already produced.

### Gold shares the swap rule

Gold was built to share nothing with the tools, so a tool bug could not reach the
eval set. The package code swap rule is the one exception: it now lives in
`rxdelta/swaps.py`, and the diff, the report, the tools and gold all call it, so
the four cannot disagree about what a swap is. The cost is that a bug in that
rule would be copied into gold and the gold versus tool test would not catch it.
The rule is therefore tested on its own in `tests/test_swaps.py`, with explicit
cases, and gold still fetches its rows with its own SQL and classifies pairs with
its own code rather than the diff's `classify`. The gold import test allows
`rxdelta.swaps` and still forbids `rxdelta.tools` and `rxdelta.diff`.

### Downloading a release

`rxdelta fetch-cms --release 2026_20260513` turns the one-off range read into a
command. It finds the release's archive in the data.cms.gov catalog
(`[source.download]` in config), reads the zip directory from the end of the
remote file, and unpacks only the inner archives matching `[source.files]`:
about 10MB instead of 2.3GB. It writes `SOURCE.json` with the URL and each file's
SHA-256 next to the files. Fetching the July release again reproduced the three
loaded files hash for hash. `make cms-fetch` fetches all three months.

The eval manifest records each month's release (`"releases"`). It does not trust
a folder name alone: `rxdelta evalset build --data-dir data` hashes the files
under each month and requires them to match the SHA-256 the load wrote to
`ingest_log`, then reads the release from the folder they sit in. The fixture
set has no releases and records an empty map.

### Questions too big for one tool call

The `over_cap` category (5 percent, taken from `filtered_list`) asks for lists
whose real answer has more change rows than `get_changes` returns in one call.
Its expected answer is the number of drugs; the full list is kept as the item's
`alternative`. A runner is right if it gives the count, or a list marked
`truncated` that states the right `total` and holds only correct drugs, or the
complete list. The summary records which path each runner took. The oracle reads
the first page, sees `truncated`, and takes the total from `count_changes`.

The fixtures have no answer over the real cap of 100 rows, so the fixture set
reports its `over_cap` quota as a shortfall in the manifest instead of filling
it with something else. A test builds a set with `get_changes` capped at 5, where
the fixtures do produce over cap questions, and requires the oracle to score 100
percent on them through the truncated path with every number grounded.

