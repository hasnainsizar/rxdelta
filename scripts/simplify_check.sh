#!/bin/sh
# Safety net for the simplification pass.
#   --fast      tests, ruff, mypy, prose, oracle on both eval sets, replay test
#   --full      --fast plus byte-identical command output, unchanged eval files,
#               saved runs re-scoring the same, and coverage within 1 point
#   --baseline  record the reference outputs, rescores and coverage
# Needs evals.db (make evaldb) and rxdelta-cms.db (make cms-load).
set -eu

MODE="${1:---fast}"
BASE=runs/simplify-baseline
CUR=runs/simplify-current
RUN="uv run"
export COLUMNS=200

fail() { echo "FAIL: $*" >&2; exit 1; }

fast() {
    $RUN ruff check . || fail ruff
    $RUN ruff format --check . || fail "ruff format"
    $RUN mypy || fail mypy
    $RUN pytest -q -p no:cacheprovider || fail pytest
    $RUN pytest -q -rs tests/test_prose.py || fail prose
    replay=$($RUN pytest -o addopts= -q -rs tests/test_agent_replay.py 2>&1) || fail "replay: $replay"
    echo "$replay" | grep -q "1 passed" || fail "replay test did not run: $replay"
    out=$(mktemp -d)
    $RUN rxdelta eval run --set evals/rxdelta_fixture_v1.jsonl --db evals.db --runner oracle \
        --split all --min-accuracy 1.0 --max-missed-refusal-rate 0 --out-dir "$out" >/dev/null \
        || fail "oracle on the fixture set"
    $RUN rxdelta eval run --set evals/rxdelta_v1.jsonl --db rxdelta-cms.db --runner oracle \
        --split all --min-accuracy 1.0 --max-missed-refusal-rate 0 --out-dir "$out" >/dev/null \
        || fail "oracle on the real set"
    rm -rf "$out"
    echo "fast checks passed"
}

# Every command output the pass must leave byte for byte the same.
outputs() {
    dir="$1"
    rm -rf "$dir" && mkdir -p "$dir/rescore"
    tmp=$(mktemp -d)
    $RUN python scripts/generate_sample_data.py --out "$tmp/data" >/dev/null
    for month in 2025-01 2025-02; do
        $RUN rxdelta load --month "$month" --dir "$tmp/data" --db "$tmp/sample.db" >/dev/null
    done
    sample="--from 2025-01 --to 2025-02 --db $tmp/sample.db"
    $RUN rxdelta summary $sample >"$dir/sample-summary.txt"
    # Through a fixed path, so the "Wrote ..." line is the same each time.
    $RUN rxdelta diff $sample --json runs/sample-diff.json >"$dir/sample-diff.txt"
    mv runs/sample-diff.json "$dir/"
    $RUN rxdelta report $sample --out "$dir/sample-report.html" --frozen-timestamp >/dev/null
    for pair in 2026-05:2026-06 2026-06:2026-07 2026-05:2026-07; do
        a=${pair%:*}
        b=${pair#*:}
        $RUN rxdelta diff --from "$a" --to "$b" --db rxdelta-cms.db \
            --json "$dir/cms-diff-$a-$b.json" >/dev/null
        $RUN rxdelta report --from "$a" --to "$b" --db rxdelta-cms.db \
            --out "$dir/cms-report-$a-$b.html" --frozen-timestamp >/dev/null
    done
    $RUN rxdelta tools schema --format anthropic >"$dir/tools-schema.json"
    $RUN python -c "import json; from rxdelta.agent.toolset import submit_tool; \
print(json.dumps(submit_tool(), indent=1, sort_keys=True))" >"$dir/submit-answer.json"
    cp rxdelta/agent/prompts/system_v*.md "$dir/"
    call() {
        $RUN rxdelta tool "$1" --args "$2" --db rxdelta-cms.db >"$dir/tool-$1.json"
    }
    call list_months '{}'
    call find_drug '{"query": "ozempik"}'
    call find_plan '{"query": "H5216-308"}'
    call get_coverage '{"rxcui": "314075", "month": "2026-06", "plan_id": "H9306-007"}'
    call get_changes '{"from_month": "2026-06", "to_month": "2026-07", "change_types": ["tier_up"]}'
    call count_changes '{"from_month": "2026-05", "to_month": "2026-07", "group_by": "cost_direction"}'
    call estimate_cost_change '{"rxcui": "2282320", "from_month": "2026-06", "to_month": "2026-07", "formulary_id": "00026043"}'
    rm -rf "$tmp"
    # Every saved run scored again offline; the timestamp and commit fields differ by design.
    for run in runs/*/; do
        run=${run%/}
        case "$run" in "$BASE" | "$CUR") continue ;; esac
        [ -f "$run/summary.json" ] || continue
        $RUN rxdelta eval rescore "$run" >/dev/null
        $RUN python -c "import json, sys; s = json.load(open(sys.argv[1])); \
[s.pop(k) for k in ('rescored_at', 'rescored_commit')]; print(json.dumps(s, indent=1, sort_keys=True))" \
            "$run/summary.rescored.json" >"$dir/rescore/$(basename "$run").json"
    done
}

coverage() {
    $RUN pytest -q -p no:cacheprovider --cov=rxdelta --cov-report=json:"$1" >/dev/null
    $RUN python -c "import json, sys; print(round(json.load(open(sys.argv[1]))['totals']['percent_covered'], 2))" "$1"
}

case "$MODE" in
--fast)
    fast
    ;;
--baseline)
    outputs "$BASE"
    coverage "$BASE/coverage.json" >"$BASE/coverage.txt"
    echo "baseline recorded in $BASE, coverage $(cat "$BASE/coverage.txt")"
    ;;
--full)
    [ -d "$BASE" ] || fail "no baseline; run $0 --baseline on the untouched branch first"
    fast
    outputs "$CUR"
    diff -r -q -x coverage.json -x coverage.txt "$BASE" "$CUR" || fail "command output differs from the baseline"
    git diff --quiet main -- 'evals/*.jsonl' || fail "an eval file changed"
    before=$(cat "$BASE/coverage.txt")
    after=$(coverage "$CUR/coverage.json")
    $RUN python -c "import sys; b, a = map(float, sys.argv[1:]); sys.exit(0 if a >= b - 1 else 1)" \
        "$before" "$after" || fail "coverage fell from $before to $after"
    echo "full checks passed (coverage $before -> $after)"
    ;;
*)
    fail "usage: $0 [--fast | --full | --baseline]"
    ;;
esac
