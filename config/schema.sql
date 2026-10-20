CREATE TABLE jobs (
    job_id          TEXT PRIMARY KEY,
    dedupe_key      TEXT NOT NULL,
    source          TEXT NOT NULL,
    is_hidden       INTEGER NOT NULL DEFAULT 0,
    title           TEXT NOT NULL,
    company         TEXT NOT NULL,
    url             TEXT NOT NULL,
    location        TEXT,
    work_mode       TEXT,
    salary          TEXT,
    department      TEXT,
    employment_type TEXT,
    posted_at       TEXT,
    description     TEXT,
    fetched_at      TEXT,
    first_seen      TEXT,
    last_seen       TEXT,
    status          TEXT NOT NULL DEFAULT 'new'
);
CREATE INDEX idx_jobs_dedupe ON jobs(dedupe_key);
CREATE INDEX idx_jobs_status ON jobs(status);
CREATE TABLE scores (
    job_id               TEXT PRIMARY KEY REFERENCES jobs(job_id) ON DELETE CASCADE,
    match_score          REAL NOT NULL,
    tier                 TEXT,
    breakdown            TEXT,
    matched_competencies TEXT,
    missing_keywords     TEXT,
    ats_keywords         TEXT,
    seniority_detected   TEXT,
    growth_stage         TEXT,
    title_family         TEXT,
    llm_score            REAL,
    llm_rationale        TEXT,
    llm_model            TEXT,
    scored_at            TEXT
);
CREATE INDEX idx_scores_total ON scores(match_score DESC);
CREATE TABLE artifacts (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id        TEXT REFERENCES jobs(job_id) ON DELETE CASCADE,
    kind          TEXT NOT NULL,          -- resume_docx | resume_md | cover_letter | ats_txt
    path          TEXT NOT NULL,
    model_used    TEXT,
    ats_score     REAL,
    created_at    TEXT
);
CREATE TABLE sqlite_sequence(name,seq);
CREATE INDEX idx_artifacts_job ON artifacts(job_id);
CREATE TABLE runs (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at    TEXT,
    finished_at   TEXT,
    sources_ok    TEXT,
    sources_failed TEXT,
    jobs_found    INTEGER DEFAULT 0,
    jobs_new      INTEGER DEFAULT 0,
    jobs_scored   INTEGER DEFAULT 0,
    resumes_built INTEGER DEFAULT 0,
    status        TEXT DEFAULT 'running',
    notes         TEXT
);
CREATE TABLE llm_calls (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    task        TEXT,
    model       TEXT,
    ok          INTEGER,
    latency_ms  INTEGER,
    error       TEXT,
    created_at  TEXT
);
CREATE TABLE llm_ledger (
    day        TEXT NOT NULL,
    task       TEXT NOT NULL,
    calls      INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (day, task)
);
CREATE TABLE llm_cache (
    key        TEXT PRIMARY KEY,
    task       TEXT,
    model      TEXT,
    response   TEXT NOT NULL,
    created_at TEXT,
    hits       INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE llm_key_info (
    id         INTEGER PRIMARY KEY CHECK (id = 1),
    payload    TEXT,
    checked_at REAL
);
CREATE TABLE model_reliability (
    model      TEXT NOT NULL,
    task       TEXT NOT NULL,
    ok         INTEGER NOT NULL DEFAULT 0,
    bad_json   INTEGER NOT NULL DEFAULT 0,
    failed     INTEGER NOT NULL DEFAULT 0,
    updated_at TEXT,
    PRIMARY KEY (model, task)
);
CREATE TABLE nvidia_ledger (
    day   TEXT PRIMARY KEY,
    calls INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE cline_ledger (
    day   TEXT PRIMARY KEY,
    calls INTEGER NOT NULL DEFAULT 0
);
