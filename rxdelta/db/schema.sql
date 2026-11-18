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

-- Reference data, not a monthly fact; deliberately not partitioned by
-- snapshot_month. One RXCUI has several rows: its full name, each ingredient
-- and each brand, so any of them can be searched. name_norm is the matching key.
CREATE TABLE IF NOT EXISTS drug_names (
    rxcui      TEXT NOT NULL,
    name       TEXT NOT NULL,
    name_type  TEXT NOT NULL CHECK (name_type IN ('full', 'ingredient', 'brand')),
    name_norm  TEXT NOT NULL,
    source     TEXT NOT NULL,
    fetched_at TEXT NOT NULL,
    PRIMARY KEY (rxcui, name_type, name_norm)
);

-- RXCUIs a name source was asked about and could not resolve, so they are
-- reported rather than silently dropped, and a rerun does not ask again.
CREATE TABLE IF NOT EXISTS drug_names_missing (
    rxcui      TEXT NOT NULL PRIMARY KEY,
    source     TEXT NOT NULL,
    reason     TEXT NOT NULL,
    checked_at TEXT NOT NULL
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
CREATE INDEX IF NOT EXISTS idx_drug_names_norm ON drug_names (name_norm);
CREATE INDEX IF NOT EXISTS idx_formulary_rxcui ON formulary (snapshot_month, rxcui);
