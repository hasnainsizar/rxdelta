# Decisions

Every judgment call made while building rxdelta, with the evidence behind it, so
a reviewer can disagree with a specific choice rather than with the whole thing.

It is organised as five parts, not as a diary:

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
unpriced. The visible consequence is that 286 of 1,367 real change groups carry
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
- **Severity is bimodal on real data.** The 20 to 39 and 60 to 79 bands hold 79
  percent of groups. Real changes cluster into a few archetypes, so the score
  separates archetypes well and separates within them poorly. The committed CMS
  report states 150 distinct severity values across 1,367 groups.
- **The layout change detection path has never been triggered by data.** May and
  June 2026 share one record layout version, so it fired only because old code
  met new files.
- **5,517 of 5,518 plans were affected.** When nearly every plan is affected the
  figure stops discriminating; plans per change group is probably the more
  informative number and the report leads with the wrong one.

### The committed CMS report predates the limitations correction

`docs/example-report-cms.html` was generated before the limitations text was
corrected, and the real files it was built from are gone, so it cannot be
regenerated. It still names the coverage gap as an unpriced phase. Page 1 of
`docs/cms-reference/Methodology-PUF-2026.pdf` says the Part D benefit has had
three phases since 2025 and the coverage gap phase was eliminated, so the text
shipped in `rxdelta/limitations.py` and in `docs/example-report.html` is the
corrected one. The CMS report is a point in time artifact and is left as it is.
