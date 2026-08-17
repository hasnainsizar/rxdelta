-- rxdelta storage. Every fact table carries snapshot_month so a month can be
-- deleted and reloaded as one partition without touching the others.

PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS formulary (
    snapshot_month        TEXT    NOT NULL,
    formulary_id          TEXT    NOT NULL,
    ndc_11                TEXT    NOT NULL,
    ndc_raw               TEXT    NOT NULL,
    rxcui                 TEXT    NOT NULL,
    tier_level            INTEGER NOT NULL,
    prior_auth            INTEGER NOT NULL,
    step_therapy          INTEGER NOT NULL,
    quantity_limit        INTEGER NOT NULL,
    quantity_limit_amount REAL,
    quantity_limit_days   INTEGER,
    PRIMARY KEY (snapshot_month, formulary_id, ndc_11)
);

CREATE TABLE IF NOT EXISTS plan_info (
    snapshot_month TEXT NOT NULL,
    contract_id    TEXT NOT NULL,
    plan_id        TEXT NOT NULL,
    segment_id     TEXT NOT NULL,
    formulary_id   TEXT NOT NULL,
    plan_name      TEXT NOT NULL,
    contract_name  TEXT NOT NULL,
    PRIMARY KEY (snapshot_month, contract_id, plan_id, segment_id)
);

CREATE TABLE IF NOT EXISTS beneficiary_cost (
    snapshot_month           TEXT    NOT NULL,
    contract_id              TEXT    NOT NULL,
    plan_id                  TEXT    NOT NULL,
    segment_id               TEXT    NOT NULL,
    coverage_level           TEXT    NOT NULL,
    tier                     INTEGER NOT NULL,
    days_supply              TEXT    NOT NULL,
    cost_type_pref           TEXT    NOT NULL,
    cost_amt_pref            REAL,
    cost_min_amt_pref        REAL,
    cost_max_amt_pref        REAL,
    cost_type_nonpref        TEXT    NOT NULL,
    cost_amt_nonpref         REAL,
    cost_min_amt_nonpref     REAL,
    cost_max_amt_nonpref     REAL,
    cost_type_mail_pref      TEXT    NOT NULL,
    cost_amt_mail_pref       REAL,
    cost_min_amt_mail_pref   REAL,
    cost_max_amt_mail_pref   REAL,
    cost_type_mail_nonpref   TEXT    NOT NULL,
    cost_amt_mail_nonpref    REAL,
    cost_min_amt_mail_nonpref REAL,
    cost_max_amt_mail_nonpref REAL,
    PRIMARY KEY (snapshot_month, contract_id, plan_id, segment_id, coverage_level, tier, days_supply)
);

CREATE TABLE IF NOT EXISTS rejected_rows (
    snapshot_month TEXT    NOT NULL,
    file_name      TEXT    NOT NULL,
    line_number    INTEGER NOT NULL,
    reason         TEXT    NOT NULL,
    raw_value      TEXT    NOT NULL
);

-- Reference data, not a monthly fact. RXCUI to drug name, resolved once and
-- cached; deliberately not partitioned by snapshot_month.
CREATE TABLE IF NOT EXISTS drug_names (
    rxcui      TEXT NOT NULL PRIMARY KEY,
    name       TEXT NOT NULL,
    source     TEXT NOT NULL,
    fetched_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS ingest_log (
    snapshot_month     TEXT    NOT NULL,
    file_name          TEXT    NOT NULL,
    sha256             TEXT    NOT NULL,
    row_count          INTEGER NOT NULL,
    rejected_row_count INTEGER NOT NULL,
    loaded_at          TEXT    NOT NULL,
    PRIMARY KEY (snapshot_month, file_name)
);

CREATE INDEX IF NOT EXISTS idx_formulary_month_id ON formulary (snapshot_month, formulary_id);
CREATE INDEX IF NOT EXISTS idx_plan_info_month_form ON plan_info (snapshot_month, formulary_id);
CREATE INDEX IF NOT EXISTS idx_cost_lookup
    ON beneficiary_cost (snapshot_month, contract_id, plan_id, segment_id, tier);
CREATE INDEX IF NOT EXISTS idx_rejected_month ON rejected_rows (snapshot_month);
