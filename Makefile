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

.PHONY: help install sample load diff summary report demo test lint typecheck check coverage bench example names distribution cms-load cms-example cms-distribution cms-bench clean

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
	@echo "make cms-load   load the real CMS months (requires the download)"
	@echo "make cms-example regenerate docs/example-report-cms.html from real data"
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

cms-load:
	$(RUN) rxdelta load --month $(CMS_FROM) --dir $(CMS_DIR) --db $(CMS_DB)
	$(RUN) rxdelta load --month $(CMS_TO) --dir $(CMS_DIR) --db $(CMS_DB)

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
