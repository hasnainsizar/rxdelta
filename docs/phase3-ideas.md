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
