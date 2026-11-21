# Phase 3 ideas

What is still open after Phase 2, each with why it matters.

- **Compound questions.** "Which drugs moved up a tier, and how many?" is scored
  on one part, because an answer holds one structured kind. People ask in pairs
  like this all the time, so a multi part answer format is needed before the
  agent can be scored on what people actually type.
- **Open ended cost questions.** A drug joining or leaving a formulary has no
  upper bound on what the member pays, so those questions are left out of the
  set. They are some of the most important questions a member has, and the
  agent needs an agreed way to phrase "we cannot put a number on this".
- **`count_changes` units.** It returns rows, drugs, formularies and plans, and
  the eval only asks about drugs. "How many changes" is ambiguous between them;
  the agent should either ask or state which count it gives, and the eval
  should have items for each.
- **Embeddings for drug names.** Matching is exact, token and fuzzy. Questions
  that name a drug class ("blood thinners") or a lay name ("my water pill") miss
  entirely, and the paraphrase set will show how often casual wording breaks
  the name step.
- **A small web UI.** `rxdelta ask --show-tools` works for a demo in a terminal.
  A page that shows the answer, the tool calls and the months used would let a
  non technical reader judge an answer without trusting it blindly.
- **Bounded diff memo.** The first change query for a month pair takes about ten
  seconds on real data and is memoized per process without a size bound. A long
  running service needs the memo bounded or precomputed.
- **A batch coverage tool.** On the real dev split (155 questions), 2 questions
  needed more than 24 tool calls in every run: `rxd-v1-0132` and `rxd-v1-0142`,
  both `multi_step` ("which drugs were dropped from formulary A but are still
  covered on formulary B"). They took 29 to 33 calls, 26 to 30 of them
  `get_coverage`, one per dropped drug. They cost $0.0054 per answer on Haiku
  5.5 (2.4 times the run's mean of $0.0023) and $0.092 on Sonnet 5.5 (3.5 times
  its mean of $0.026). All 8 of those answers were right, so the cost is the
  only problem. A `get_coverage` that takes a list of RXCUIs for one formulary
  and month would answer each with about 4 calls: list the months, get the
  changes, one batch coverage lookup, submit. The oracle needs a third such
  question (`rxd-v1-h01`, 95 calls, 94 of them `get_coverage`) only because it
  checks tiers drug by drug; the agent answered it in 5 calls with the
  `tier_to` filter on `get_changes`, so a batch tool would not change that one.
  The tools were not changed during the Phase 2 runs.
- **Stop asking about a product the question names.** On the test split,
  `rxd-v1-0044` ("any changes to brivaracetam 50 mg oral tablet on 00026109")
  failed on every repeat: the model looked up only "brivaracetam", saw the
  generic and Briviact, and asked which one, although the question names the
  generic tablet. `h10` on dev is the same habit. A v3 prompt (judged on dev)
  should tell it to look up the name as written, with strength and form, and to
  ask only when that lookup still returns several products.
- **Summarize long answers in the text, keep the full list in the structured
  answer.** v2 makes the model put every row in `final_text` as well as in the
  answer, and three dev questions with 300 or more rows still fail because the
  prompt allows a count past 300. The text should give a short summary (the
  count, the change types, a few examples, the months) while the structured
  answer carries every row, so a person reads a paragraph and the scorer gets
  the full set.
