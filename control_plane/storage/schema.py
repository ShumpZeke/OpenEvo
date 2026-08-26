"""
Control-plane schema.

Two tiers, deliberately separated (SOURCE_OF_TRUTH section 23):

  events        append-only, high volume, never updated. The raw record.
  projections   small, indexed, updated-in-place current state (candidates,
                islands, MAP-Elites cells, runs...). Every projection row is
                derived from events, so the UI never reads a value that no
                event produced.

Projections exist because "latest state of 20k candidates" is a query the UI
runs constantly and re-scanning the event log for it would not hold up at the
scale section 25 requires. They are a cache with a rebuild path: dropping every
projection and replaying the event log reconstructs them exactly.

Upstream checkpoint data is NOT duplicated here. Candidate code lives in
OpenEvolve's own checkpoints; we store identity, metrics and pointers.
"""

SCHEMA_VERSION = 3

# Columns added after their table first shipped.
#
# `CREATE TABLE IF NOT EXISTS` leaves an existing table exactly as it is, so a
# workspace created before a column existed keeps the old shape and every query
# naming that column fails with "no such column" — silently, until someone
# opens the view that uses it. Reconciling these with `ALTER TABLE ADD COLUMN`
# on open is the fix. It is the one schema change SQLite does cheaply, and for
# an append-only projection store it is the only kind normally needed.
#
# Structural changes (a changed primary key, a dropped column) are not
# expressible here and need `Store.rebuild_projections_from_log`, which is what
# makes projections a cache rather than a second source of truth.
ADDITIVE_COLUMNS = {
    "candidates": {
        # Added in v2 with candidate→request attribution.
        "gen_request_id": "TEXT",
        "gen_provider": "TEXT",
        "gen_model": "TEXT",
        "gen_latency_ms": "REAL",
        "gen_tokens": "INTEGER",
        # Added in v3 with operator-labelled mutations.
        "gen_operator": "TEXT",
    },
}

# Versions whose changes cannot be applied by ALTER TABLE. Opening an older
# workspace logs what needs rebuilding rather than failing or, worse, quietly
# serving wrong numbers.
STRUCTURAL_CHANGES = {
    2: "map_elites_cells is keyed by (run_id, island_id, cell_key); a v1 "
       "database merged every island's feature map into one",
}

SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA synchronous=NORMAL;
PRAGMA foreign_keys=ON;

CREATE TABLE IF NOT EXISTS schema_meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

-- ---------------------------------------------------------------- events
CREATE TABLE IF NOT EXISTS events (
    seq             INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id        TEXT NOT NULL UNIQUE,
    trace_id        TEXT,
    span_id         TEXT,
    parent_span_id  TEXT,
    experiment_id   TEXT,
    run_id          TEXT,
    generation      INTEGER,
    iteration       INTEGER,
    candidate_id    TEXT,
    island_id       INTEGER,
    timestamp       REAL NOT NULL,
    duration_ms     REAL,
    component       TEXT NOT NULL,
    type            TEXT NOT NULL,
    status          TEXT NOT NULL,
    summary         TEXT,
    payload         TEXT NOT NULL,   -- full JSON event (input/output/metrics/metadata/error)
    pid             INTEGER
);
CREATE INDEX IF NOT EXISTS ix_events_run_seq    ON events(run_id, seq);
CREATE INDEX IF NOT EXISTS ix_events_type       ON events(type, seq);
CREATE INDEX IF NOT EXISTS ix_events_candidate  ON events(candidate_id, seq);
CREATE INDEX IF NOT EXISTS ix_events_trace      ON events(trace_id, seq);
CREATE INDEX IF NOT EXISTS ix_events_ts         ON events(timestamp);
CREATE INDEX IF NOT EXISTS ix_events_status     ON events(status, seq);
CREATE INDEX IF NOT EXISTS ix_events_island     ON events(run_id, island_id, seq);

-- ----------------------------------------------------------- experiments
CREATE TABLE IF NOT EXISTS experiments (
    experiment_id   TEXT PRIMARY KEY,
    name            TEXT NOT NULL,
    created_at      REAL NOT NULL,
    config_path     TEXT,
    config_revision TEXT,
    initial_program TEXT,
    evaluator_path  TEXT,
    status          TEXT NOT NULL DEFAULT 'created',
    metadata        TEXT NOT NULL DEFAULT '{}'
);

-- ------------------------------------------------------------------ runs
CREATE TABLE IF NOT EXISTS runs (
    run_id            TEXT PRIMARY KEY,
    experiment_id     TEXT NOT NULL,
    started_at        REAL,
    ended_at          REAL,
    status            TEXT NOT NULL DEFAULT 'created',
    pid               INTEGER,
    iterations_target INTEGER,
    iterations_done   INTEGER DEFAULT 0,
    best_candidate_id TEXT,
    best_fitness      REAL,
    checkpoint_dir    TEXT,
    output_dir        TEXT,
    -- reproducibility block (SOURCE_OF_TRUTH section 17 / architect section 17)
    provenance        TEXT NOT NULL DEFAULT '{}',
    error             TEXT,
    metadata          TEXT NOT NULL DEFAULT '{}',
    FOREIGN KEY(experiment_id) REFERENCES experiments(experiment_id)
);
CREATE INDEX IF NOT EXISTS ix_runs_experiment ON runs(experiment_id, started_at);
CREATE INDEX IF NOT EXISTS ix_runs_status     ON runs(status);

-- ------------------------------------------------------------ candidates
CREATE TABLE IF NOT EXISTS candidates (
    run_id           TEXT NOT NULL,
    candidate_id     TEXT NOT NULL,
    parent_id        TEXT,
    generation       INTEGER,
    iteration        INTEGER,
    island_id        INTEGER,
    created_at       REAL,
    candidate_type   TEXT DEFAULT 'code',
    language         TEXT,
    combined_score   REAL,
    metrics          TEXT NOT NULL DEFAULT '{}',
    complexity       REAL,
    diversity        REAL,
    code_hash        TEXT,
    code_length      INTEGER,
    changes_summary  TEXT,
    map_elites_cell  TEXT,
    is_best          INTEGER DEFAULT 0,
    eval_status      TEXT,
    -- Which model request produced this candidate. Upstream does not attach a
    -- candidate id to the generating call, so this is captured at generation
    -- time via a ContextVar rather than recovered by a join.
    gen_request_id   TEXT,
    gen_provider     TEXT,
    gen_model        TEXT,
    gen_latency_ms   REAL,
    gen_tokens       INTEGER,
    gen_operator     TEXT,
    metadata         TEXT NOT NULL DEFAULT '{}',
    PRIMARY KEY (run_id, candidate_id)
);
CREATE INDEX IF NOT EXISTS ix_cand_run_gen    ON candidates(run_id, generation);
CREATE INDEX IF NOT EXISTS ix_cand_parent     ON candidates(run_id, parent_id);
CREATE INDEX IF NOT EXISTS ix_cand_score      ON candidates(run_id, combined_score DESC);
CREATE INDEX IF NOT EXISTS ix_cand_island     ON candidates(run_id, island_id);
CREATE INDEX IF NOT EXISTS ix_cand_cell       ON candidates(run_id, map_elites_cell);
CREATE INDEX IF NOT EXISTS ix_cand_iteration  ON candidates(run_id, iteration);
CREATE INDEX IF NOT EXISTS ix_cand_gen_model  ON candidates(run_id, gen_provider, gen_model);
CREATE INDEX IF NOT EXISTS ix_cand_gen_op     ON candidates(run_id, gen_operator);

-- Explicit lineage edges: a candidate may have several parents (crossover),
-- which a single parent_id column cannot express.
CREATE TABLE IF NOT EXISTS candidate_parents (
    run_id       TEXT NOT NULL,
    candidate_id TEXT NOT NULL,
    parent_id    TEXT NOT NULL,
    role         TEXT DEFAULT 'parent',
    PRIMARY KEY (run_id, candidate_id, parent_id)
);
CREATE INDEX IF NOT EXISTS ix_cp_parent ON candidate_parents(run_id, parent_id);

-- ----------------------------------------------------------- evaluations
CREATE TABLE IF NOT EXISTS evaluations (
    evaluation_id  TEXT PRIMARY KEY,
    run_id         TEXT NOT NULL,
    candidate_id   TEXT NOT NULL,
    evaluator_id   TEXT,
    stage          INTEGER DEFAULT 0,
    started_at     REAL,
    ended_at       REAL,
    duration_ms    REAL,
    status         TEXT,
    exit_code      INTEGER,
    timed_out      INTEGER DEFAULT 0,
    raw_metrics    TEXT NOT NULL DEFAULT '{}',
    combined_score REAL,
    failure_class  TEXT,
    sandbox_id     TEXT,
    stdout_excerpt TEXT,
    stderr_excerpt TEXT,
    artifacts      TEXT NOT NULL DEFAULT '{}',
    retry_of       TEXT,
    metadata       TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS ix_eval_run       ON evaluations(run_id, started_at);
CREATE INDEX IF NOT EXISTS ix_eval_candidate ON evaluations(run_id, candidate_id);
CREATE INDEX IF NOT EXISTS ix_eval_status    ON evaluations(run_id, status);

-- -------------------------------------------------------- model requests
CREATE TABLE IF NOT EXISTS model_requests (
    request_id        TEXT PRIMARY KEY,
    run_id            TEXT,
    candidate_id      TEXT,
    generation        INTEGER,
    iteration         INTEGER,
    role              TEXT,
    provider          TEXT,
    model             TEXT,
    api_base          TEXT,
    started_at        REAL,
    ended_at          REAL,
    latency_ms        REAL,
    ttft_ms           REAL,
    prompt_tokens     INTEGER,
    completion_tokens INTEGER,
    total_tokens      INTEGER,
    tokens_per_sec    REAL,
    context_limit     INTEGER,
    status            TEXT,
    http_status       INTEGER,
    rate_limited      INTEGER DEFAULT 0,
    retries           INTEGER DEFAULT 0,
    retry_of          TEXT,
    stop_reason       TEXT,
    estimated_cost    REAL,
    cost_basis        TEXT,
    error             TEXT,
    -- Prompt/response bodies are stored redacted; see redaction.py
    prompt_excerpt    TEXT,
    response_excerpt  TEXT,
    params            TEXT NOT NULL DEFAULT '{}',
    metadata          TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS ix_mr_run       ON model_requests(run_id, started_at);
CREATE INDEX IF NOT EXISTS ix_mr_provider  ON model_requests(run_id, provider, model);
CREATE INDEX IF NOT EXISTS ix_mr_candidate ON model_requests(run_id, candidate_id);
CREATE INDEX IF NOT EXISTS ix_mr_status    ON model_requests(run_id, status);

-- --------------------------------------------------------------- islands
CREATE TABLE IF NOT EXISTS islands (
    run_id                TEXT NOT NULL,
    island_id             INTEGER NOT NULL,
    updated_at            REAL,
    population            INTEGER,
    best_score            REAL,
    median_score          REAL,
    diversity             REAL,
    generation            INTEGER,
    stagnation_generations INTEGER,
    candidates_generated  INTEGER DEFAULT 0,
    eval_failures         INTEGER DEFAULT 0,
    model_calls           INTEGER DEFAULT 0,
    tokens                INTEGER DEFAULT 0,
    migrants_sent         INTEGER DEFAULT 0,
    migrants_received     INTEGER DEFAULT 0,
    best_candidate_id     TEXT,
    metadata              TEXT NOT NULL DEFAULT '{}',
    PRIMARY KEY (run_id, island_id)
);

CREATE TABLE IF NOT EXISTS migrations (
    migration_id  TEXT PRIMARY KEY,
    run_id        TEXT NOT NULL,
    generation    INTEGER,
    timestamp     REAL,
    source_island INTEGER,
    target_island INTEGER,
    candidate_id  TEXT,
    new_candidate_id TEXT,
    metadata      TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS ix_mig_run ON migrations(run_id, timestamp);

-- ------------------------------------------------------------ MAP-Elites
-- Current occupant per cell.
--
-- Keyed by island as well as cell: upstream keeps a SEPARATE feature map per
-- island (`island_feature_maps[i]`), so the same cell_key legitimately holds a
-- different elite on each island. Keying on cell_key alone would silently
-- collapse the islands into one grid and show the wrong occupant.
CREATE TABLE IF NOT EXISTS map_elites_cells (
    run_id        TEXT NOT NULL,
    island_id     INTEGER NOT NULL,
    cell_key      TEXT NOT NULL,
    coords        TEXT NOT NULL,
    dimensions    TEXT NOT NULL,
    candidate_id  TEXT,
    score         REAL,
    updated_at    REAL,
    generation    INTEGER,
    replacements  INTEGER DEFAULT 0,
    PRIMARY KEY (run_id, island_id, cell_key)
);
CREATE INDEX IF NOT EXISTS ix_mec_run  ON map_elites_cells(run_id, score DESC);
CREATE INDEX IF NOT EXISTS ix_mec_cell ON map_elites_cells(run_id, cell_key);

-- Full occupancy history, so the Lab's generation scrubber shows real past
-- state rather than only "now".
CREATE TABLE IF NOT EXISTS map_elites_history (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id        TEXT NOT NULL,
    island_id     INTEGER NOT NULL DEFAULT -1,
    cell_key      TEXT NOT NULL,
    candidate_id  TEXT,
    previous_candidate_id TEXT,
    score         REAL,
    previous_score REAL,
    generation    INTEGER,
    iteration     INTEGER,
    timestamp     REAL
);
CREATE INDEX IF NOT EXISTS ix_meh_run_gen ON map_elites_history(run_id, generation);
CREATE INDEX IF NOT EXISTS ix_meh_cell    ON map_elites_history(run_id, cell_key, id);

-- ----------------------------------------------------------- checkpoints
CREATE TABLE IF NOT EXISTS checkpoints (
    checkpoint_id TEXT PRIMARY KEY,
    run_id        TEXT NOT NULL,
    iteration     INTEGER,
    path          TEXT,
    created_at    REAL,
    size_bytes    INTEGER,
    num_programs  INTEGER,
    best_score    REAL,
    status        TEXT DEFAULT 'ok',
    metadata      TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS ix_ckpt_run ON checkpoints(run_id, iteration);

-- --------------------------------------------------------- sandbox / agents
CREATE TABLE IF NOT EXISTS sandbox_runs (
    sandbox_id     TEXT PRIMARY KEY,
    run_id         TEXT,
    candidate_id   TEXT,
    backend        TEXT,
    mode           TEXT,
    image          TEXT,
    workdir        TEXT,
    home_dir       TEXT,
    started_at     REAL,
    ended_at       REAL,
    status         TEXT,
    exit_code      INTEGER,
    cpu_limit      REAL,
    mem_limit_mb   INTEGER,
    timeout_s      REAL,
    network_policy TEXT,
    termination_reason TEXT,
    isolation      TEXT NOT NULL DEFAULT '{}',
    metadata       TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS ix_sbx_run ON sandbox_runs(run_id, started_at);

CREATE TABLE IF NOT EXISTS agent_runs (
    agent_run_id TEXT PRIMARY KEY,
    sandbox_id   TEXT,
    run_id       TEXT,
    candidate_id TEXT,
    harness      TEXT,          -- 'opencode' | 'omo'
    agent        TEXT,
    mode         TEXT,
    model        TEXT,
    started_at   REAL,
    ended_at     REAL,
    status       TEXT,
    tool_calls   INTEGER DEFAULT 0,
    tokens       INTEGER DEFAULT 0,
    metadata     TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS ix_ar_sandbox ON agent_runs(sandbox_id);

-- ------------------------------------------------------------- resources
CREATE TABLE IF NOT EXISTS resource_metrics (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id     TEXT,
    timestamp  REAL NOT NULL,
    kind       TEXT NOT NULL,
    value      REAL NOT NULL,
    unit       TEXT,
    scope      TEXT,
    metadata   TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS ix_rm_run_ts ON resource_metrics(run_id, kind, timestamp);

-- ---------------------------------------------------------------- alerts
CREATE TABLE IF NOT EXISTS alerts (
    alert_id     TEXT PRIMARY KEY,
    run_id       TEXT,
    rule         TEXT NOT NULL,
    severity     TEXT NOT NULL,
    raised_at    REAL NOT NULL,
    cleared_at   REAL,
    message      TEXT,
    value        REAL,
    threshold    REAL,
    metadata     TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS ix_alert_run ON alerts(run_id, raised_at);

-- ------------------------------------------------------- config revisions
CREATE TABLE IF NOT EXISTS config_revisions (
    revision_id   TEXT PRIMARY KEY,
    experiment_id TEXT,
    created_at    REAL,
    content       TEXT NOT NULL,
    content_hash  TEXT NOT NULL,
    format        TEXT DEFAULT 'yaml',
    note          TEXT
);
CREATE INDEX IF NOT EXISTS ix_cfg_exp ON config_revisions(experiment_id, created_at);

-- --------------------------------------------------------- provider health
CREATE TABLE IF NOT EXISTS provider_health (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    provider      TEXT NOT NULL,
    model         TEXT,
    checked_at    REAL NOT NULL,
    available     INTEGER,
    latency_ms    REAL,
    http_status   INTEGER,
    free_status   TEXT,
    rate_limited  INTEGER DEFAULT 0,
    success_rate  REAL,
    detail        TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS ix_ph_provider ON provider_health(provider, checked_at);

-- ------------------------------------------------------------ full text
-- Powers the global command palette (SOURCE_OF_TRUTH section 21).
CREATE VIRTUAL TABLE IF NOT EXISTS search_index USING fts5(
    entity_type,
    entity_id,
    run_id UNINDEXED,
    title,
    body,
    tokenize = 'porter unicode61'
);
"""
