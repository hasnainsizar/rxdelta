.DEFAULT_GOAL := help

UV      ?= uv
RUN     := $(UV) run
DATA    ?= data
DB      ?= rxdelta.db
FROM    ?= 2025-01
TO      ?= 2025-02
REPORT  ?= report.html

ifeq ($(shell uname),Darwin)
OPEN := open
else
OPEN := xdg-open
endif

.PHONY: help install sample load diff summary report demo test lint typecheck check coverage bench example names distribution cms-fetch cms-load cms-example cms-distribution cms-bench clean ask-fixtures evaldb evalset-fixture eval-fixture-oracle evalset eval-oracle eval-null ask-record eval-agent-dev eval-agent-test prose-check

help:
	@echo "make demo       generate sample data, load both months, diff, open the report"
	@echo "make install    create the virtualenv and install dependencies"
	@echo "make sample     write two months of sample snapshots to $(DATA)/"
	@echo "make load       load both sample months into $(DB)"
	@echo "make summary    roll the diff up by plan and change type"
	@echo "make report     write $(REPORT)"
	@echo "make check      lint, typecheck and test"
	@echo "make bench      measure load and diff time on the sample data"
	@echo "make names      resolve drug names from RxNav into the committed cache"
	@echo "make example    regenerate docs/example-report.html"
	@echo "make cms-fetch  download the three real CMS months (only the tables read)"
	@echo "make cms-load   load the three real CMS months"
	@echo "make cms-example regenerate docs/example-report-cms.html from real data"
	@echo "make evalset    rebuild evals/rxdelta_v1.jsonl from the real CMS database"
	@echo "make eval-oracle score the oracle runner on the real set's dev split"
	@echo "make evalset-fixture rebuild the fixture eval set used by tests and CI"
	@echo "make ask-record record the agent replay cassette (needs ANTHROPIC_API_KEY)"
	@echo "make eval-agent-dev  score the agent on the real dev split (paid)"
	@echo "make eval-agent-test score the agent on the real test split (paid)"
	@echo "make prose-check check the repo for dashes and AI self talk"
	@echo "make clean      remove the database, generated data and report"

install:
	$(UV) sync --all-groups

sample:
	$(RUN) python scripts/generate_sample_data.py --out $(DATA)

load: sample
	$(RUN) rxdelta load --month $(FROM) --dir $(DATA) --db $(DB)
	$(RUN) rxdelta load --month $(TO) --dir $(DATA) --db $(DB)

summary:
	$(RUN) rxdelta summary --from $(FROM) --to $(TO) --db $(DB)

diff:
	$(RUN) rxdelta diff --from $(FROM) --to $(TO) --db $(DB)

report:
	$(RUN) rxdelta report --from $(FROM) --to $(TO) --out $(REPORT) --db $(DB)

demo: install load summary report
	$(OPEN) $(REPORT)

test:
	$(RUN) pytest

coverage:
	$(RUN) pytest --cov=rxdelta/diff --cov=rxdelta/ingest --cov-report=term-missing

lint:
	$(RUN) ruff check .
	$(RUN) ruff format --check .

typecheck:
	$(RUN) mypy

check: lint typecheck test

bench:
	$(RUN) python scripts/benchmark.py --dir $(DATA)

# The only target that uses the network. Everything else reads the committed cache.
names:
	$(RUN) rxdelta names refresh --db $(DB)

distribution:
	$(RUN) rxdelta summary --from $(FROM) --to $(TO) --db $(DB) --severity-distribution

# The committed copy uses a frozen stamp so regenerating it does not churn git.
# The committed CMS report. Requires the real files, which are not in the repo.
CMS_DB    ?= rxdelta-cms.db
CMS_DIR   ?= data
CMS_FROM  ?= 2026-05
CMS_TO    ?= 2026-06
CMS_NEXT  ?= 2026-07
# The data.cms.gov archive names of the three months above.
CMS_RELEASES ?= 2026_20260513 2026_20260610 2026_20260722

# Reads only the three tables out of each remote archive, about 10MB a month.
cms-fetch:
	for release in $(CMS_RELEASES); do \
		$(RUN) rxdelta fetch-cms --release $$release --dir $(CMS_DIR) || exit 1; \
	done

cms-load:
	$(RUN) rxdelta load --month $(CMS_FROM) --dir $(CMS_DIR) --db $(CMS_DB)
	$(RUN) rxdelta load --month $(CMS_TO) --dir $(CMS_DIR) --db $(CMS_DB)
	$(RUN) rxdelta load --month $(CMS_NEXT) --dir $(CMS_DIR) --db $(CMS_DB)

cms-example:
	$(RUN) rxdelta report --from $(CMS_FROM) --to $(CMS_TO) \
		--out docs/example-report-cms.html --db $(CMS_DB) --frozen-timestamp

cms-distribution:
	$(RUN) rxdelta summary --from $(CMS_FROM) --to $(CMS_TO) --db $(CMS_DB) \
		--severity-distribution

cms-bench:
	$(RUN) python scripts/benchmark.py --dir $(CMS_DIR) --months $(CMS_FROM) $(CMS_TO) --repeat 1

example: load
	$(RUN) rxdelta report --from $(FROM) --to $(TO) --out docs/example-report.html --db $(DB) \
		--frozen-timestamp

# Removes only what this Makefile generates. The paths below are written out in
# full on purpose. An earlier version was `rm -rf $(DATA)`, which deleted a real
# CMS release; the version after that interpolated $(FROM) and $(TO), so
# `make clean FROM=2026-05` or `make clean TO=reference` would have done the same
# to the real months or to the committed drug name cache. A recipe that deletes
# directories should not be steerable from the command line.
clean:
	rm -rf data/2025-01 data/2025-02
	rm -f rxdelta.db rxdelta.db-wal rxdelta.db-shm report.html
	rm -rf .pytest_cache .coverage .mypy_cache .ruff_cache
	find . -name __pycache__ -type d -prune -exec rm -rf {} +

# The question eval sets. The real set is built from the loaded CMS months and
# needs the download plus `rxdelta names fetch`. The fixture set is built from
# the committed three month fixtures, so tests and CI can rebuild it offline.
EVAL_SET  ?= evals/rxdelta_v1.jsonl
EVAL_SEED ?= 7
FIXTURE_SET ?= evals/rxdelta_fixture_v1.jsonl

ask-fixtures:
	$(RUN) python scripts/build_ask_fixtures.py --out tests/fixtures/ask

# The database path is written out, not a variable, for the same reason as clean.
evaldb:
	rm -f evals.db evals.db-wal evals.db-shm
	$(RUN) rxdelta load --month 2026-01 --dir tests/fixtures/ask --db evals.db
	$(RUN) rxdelta load --month 2026-02 --dir tests/fixtures/ask --db evals.db
	$(RUN) rxdelta load --month 2026-03 --dir tests/fixtures/ask --db evals.db
	$(RUN) rxdelta names load --fixture tests/fixtures/ask/drug_names.csv --db evals.db

evalset-fixture: evaldb
	$(RUN) rxdelta evalset build --db evals.db --from 2026-01 --to 2026-03 --n 200 \
		--seed $(EVAL_SEED) --handwritten evals/handwritten_fixture.jsonl --out $(FIXTURE_SET)

eval-fixture-oracle:
	$(RUN) rxdelta eval run --set $(FIXTURE_SET) --runner oracle --split all --db evals.db

evalset:
	$(RUN) rxdelta evalset build --db $(CMS_DB) --from $(CMS_FROM) --to $(CMS_NEXT) --n 200 \
		--seed $(EVAL_SEED) --handwritten evals/handwritten_real.jsonl --data-dir $(CMS_DIR) \
		--out $(EVAL_SET)

eval-oracle:
	$(RUN) rxdelta eval run --set $(EVAL_SET) --runner oracle --split dev --db $(CMS_DB)

eval-null:
	$(RUN) rxdelta eval run --set $(EVAL_SET) --runner null --split dev --db $(CMS_DB)

# The agent. Every target below except ask-record's replay check calls a paid
# model and needs ANTHROPIC_API_KEY. Spend is logged to runs/spend.jsonl and
# capped by [agent].budget_usd.
MODEL   ?= claude-haiku-5-5
PROMPT  ?= v3
REPEATS ?= 3
CASSETTE ?= tests/cassettes/agent_fixture.jsonl

# Records the replay cassette: one fixture question per category. CI replays it
# with no key. The cassette path is written out, not a variable, for the same
# reason as clean.
ask-record: evaldb
	rm -f tests/cassettes/agent_fixture.jsonl tests/cassettes/agent_fixture.scores.json
	$(RUN) rxdelta eval run --set $(FIXTURE_SET) --db evals.db --split all --limit 10 \
		--stratified --runner agent --model $(MODEL) --prompt $(PROMPT) --workers 1 \
		--record tests/cassettes/agent_fixture.jsonl --min-accuracy 0 \
		--max-missed-refusal-rate 1

eval-agent-dev:
	$(RUN) rxdelta eval run --set $(EVAL_SET) --runner agent --split dev --db $(CMS_DB) \
		--model $(MODEL) --prompt $(PROMPT) --repeats $(REPEATS)

eval-agent-test:
	$(RUN) rxdelta eval run --set $(EVAL_SET) --runner agent --split test --db $(CMS_DB) \
		--model $(MODEL) --prompt $(PROMPT) --repeats $(REPEATS)

# No em or en dashes, and no text where the agent or the code talks about being
# an AI or about how it was written. Words that describe what the code does
# (agent, model, anthropic, system_prompt, tool_use) are fine.
prose-check:
	$(RUN) pytest -q tests/test_prose.py
