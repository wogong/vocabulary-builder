-- Inbox: the single source of truth (spec §4.1, D1).
-- Anki is a downstream projection and can be rebuilt from this table.

CREATE TABLE IF NOT EXISTS items (
    id              INTEGER PRIMARY KEY,
    idem_key        TEXT UNIQUE,          -- client-generated UUID, for idempotency
    source          TEXT NOT NULL,        -- 'chrome' | 'telegram' | 'api'
    client_id       TEXT,
    raw_word        TEXT NOT NULL,        -- exactly what the user selected
    lemma           TEXT,                 -- P0: lowercased raw_word; P1: lemminflect
    lang            TEXT DEFAULT 'en',
    sentence        TEXT NOT NULL,
    sentence_masked TEXT,
    word_offset     INTEGER,              -- start of raw_word in sentence; kept so a
                                          -- reprojection can still mask/bold correctly
                                          -- when the word occurs twice (§5.3)
    context_before  TEXT,
    context_after   TEXT,
    url             TEXT,
    page_title      TEXT,
    definition      TEXT,                 -- P1 enrichment
    definition_ctx  TEXT,                 -- P3 (LLM)
    phonetic        TEXT,                 -- P1
    translation     TEXT,                 -- P2
    audio_file      TEXT,                 -- P2 (TTS)
    freq_rank       INTEGER,              -- P1
    status          TEXT NOT NULL,        -- pending|enriched|committed|skipped|failed
    anki_note_id    INTEGER,
    merge_target    INTEGER,
    error           TEXT,
    retry_count     INTEGER DEFAULT 0,
    raw_payload     TEXT,                 -- original client JSON, verbatim (S8)
    client_ts       TEXT,
    created_at      TEXT NOT NULL,
    committed_at    TEXT
);

CREATE INDEX IF NOT EXISTS idx_items_status  ON items(status);
CREATE INDEX IF NOT EXISTS idx_items_lemma   ON items(lemma, lang);
CREATE INDEX IF NOT EXISTS idx_items_created ON items(created_at);

CREATE TABLE IF NOT EXISTS settings (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

-- Populated from P1 onwards; created here so the schema is stable from day one.
CREATE TABLE IF NOT EXISTS known_words (
    lemma      TEXT PRIMARY KEY,
    marked_at  TEXT NOT NULL
);
