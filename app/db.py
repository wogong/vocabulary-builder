"""Inbox persistence (spec §4.1). SQLite + WAL.

Connections are opened per call: WAL makes this cheap, and it keeps the
FastAPI threadpool and the Anki writer thread from sharing a connection.
"""

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

# Statuses that count as "we hold this word" for dedup purposes.
LIVE = "('pending','enriched','committed')"


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class Inbox:
    def __init__(self, path: str):
        self.path = path
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        schema = (Path(__file__).parent / "schema.sql").read_text(encoding="utf-8")
        with self._conn() as con:
            con.execute("PRAGMA journal_mode=WAL")
            con.executescript(schema)

    @contextmanager
    def _conn(self):
        con = sqlite3.connect(self.path, timeout=30, isolation_level=None)
        con.row_factory = sqlite3.Row
        con.execute("PRAGMA busy_timeout=30000")
        try:
            yield con
        finally:
            con.close()

    # -- writes ---------------------------------------------------------

    def add_item(self, fields: dict, raw_payload: dict) -> dict:
        """Insert one item and classify it against what we already hold.

        Returns {id, dedup, existing_contexts, status}. `dedup` is one of
        new / merged / duplicate (spec §5.3). A duplicate is stored for the
        record but goes straight to status=skipped and is never projected.
        """
        with self._conn() as con:
            con.execute("BEGIN IMMEDIATE")
            try:
                lemma, lang = fields["lemma"], fields["lang"]
                same_lemma = con.execute(
                    f"SELECT COUNT(*) FROM items "
                    f"WHERE lemma=? AND lang=? AND status IN {LIVE}",
                    (lemma, lang),
                ).fetchone()[0]
                same_sentence = con.execute(
                    f"SELECT COUNT(*) FROM items "
                    f"WHERE lemma=? AND lang=? AND sentence=? AND status IN {LIVE}",
                    (lemma, lang, fields["sentence"]),
                ).fetchone()[0]

                if same_sentence:
                    dedup, status = "duplicate", "skipped"
                elif same_lemma:
                    dedup, status = "merged", "pending"
                else:
                    dedup, status = "new", "pending"

                cols = dict(
                    fields,
                    status=status,
                    raw_payload=json.dumps(raw_payload, ensure_ascii=False),
                    created_at=utcnow(),
                )
                cur = con.execute(
                    f"INSERT INTO items ({','.join(cols)}) "
                    f"VALUES ({','.join('?' * len(cols))})",
                    tuple(cols.values()),
                )
                con.execute("COMMIT")
            except BaseException:
                con.execute("ROLLBACK")
                raise
            return {
                "id": cur.lastrowid,
                "dedup": dedup,
                "status": status,
                "existing_contexts": same_lemma,
            }

    def mark_committed(self, item_id: int, note_id: int, merge_target: int | None):
        with self._conn() as con:
            con.execute(
                "UPDATE items SET status='committed', anki_note_id=?, merge_target=?, "
                "committed_at=?, error=NULL WHERE id=?",
                (note_id, merge_target, utcnow(), item_id),
            )

    def mark_failed(self, item_id: int, error: str):
        with self._conn() as con:
            con.execute(
                "UPDATE items SET status='failed', error=?, retry_count=retry_count+1 "
                "WHERE id=?",
                (error[:2000], item_id),
            )

    def set_masked(self, item_id: int, masked: str):
        with self._conn() as con:
            con.execute(
                "UPDATE items SET sentence_masked=? WHERE id=?", (masked, item_id)
            )

    # -- reads ----------------------------------------------------------

    def get(self, item_id: int) -> dict | None:
        with self._conn() as con:
            row = con.execute("SELECT * FROM items WHERE id=?", (item_id,)).fetchone()
            return dict(row) if row else None

    def by_idem_key(self, key: str) -> dict | None:
        with self._conn() as con:
            row = con.execute("SELECT * FROM items WHERE idem_key=?", (key,)).fetchone()
            return dict(row) if row else None

    def pending_ids(self) -> list[int]:
        """Items awaiting projection — re-queued on startup after a crash."""
        with self._conn() as con:
            rows = con.execute(
                "SELECT id FROM items WHERE status='pending' ORDER BY id"
            ).fetchall()
            return [r["id"] for r in rows]

    def stats(self) -> dict:
        with self._conn() as con:
            counts = {
                r["status"]: r["n"]
                for r in con.execute(
                    "SELECT status, COUNT(*) n FROM items GROUP BY status"
                )
            }
            today = con.execute(
                "SELECT COUNT(*) FROM items "
                "WHERE created_at >= date('now') AND status != 'skipped'"
            ).fetchone()[0]
            distinct = con.execute(
                f"SELECT COUNT(DISTINCT lemma) FROM items WHERE status IN {LIVE}"
            ).fetchone()[0]
            return {"by_status": counts, "today": today, "distinct_words": distinct}

    # -- known words (P1) ----------------------------------------------

    def mark_known(self, lemma: str) -> None:
        with self._conn() as con:
            con.execute(
                "INSERT OR REPLACE INTO known_words (lemma, marked_at) VALUES (?, ?)",
                (lemma, utcnow()),
            )

    def unmark_known(self, lemma: str) -> None:
        with self._conn() as con:
            con.execute("DELETE FROM known_words WHERE lemma = ?", (lemma,))

    def is_known(self, lemma: str) -> bool:
        with self._conn() as con:
            return (
                con.execute(
                    "SELECT 1 FROM known_words WHERE lemma = ?", (lemma,)
                ).fetchone()
                is not None
            )

    def context_count(self, lemma: str, lang: str) -> int:
        """How many contexts this word already has in the deck (D4)."""
        with self._conn() as con:
            return con.execute(
                f"SELECT COUNT(*) FROM items "
                f"WHERE lemma=? AND lang=? AND status IN {LIVE}",
                (lemma, lang),
            ).fetchone()[0]

    # -- settings (P2) ---------------------------------------------------

    def setting(self, key: str, default: str | None = None) -> str | None:
        with self._conn() as con:
            row = con.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
            return row["value"] if row else default

    def set_setting(self, key: str, value: str) -> None:
        with self._conn() as con:
            con.execute(
                "INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)", (key, value)
            )

    # -- bot reviews and quiz (P2, §7.6) ---------------------------------

    def add_review(self, item_id: int, chat_id: int, rating: int) -> None:
        with self._conn() as con:
            con.execute(
                "INSERT INTO bot_reviews (item_id, chat_id, rating, reviewed_at) "
                "VALUES (?,?,?,?)",
                (item_id, chat_id, rating, utcnow()),
            )

    def quiz_items(self, count: int) -> list[dict]:
        """Pick words to quiz on (§7.6).

        60% recent, 30% previously rated poorly, 10% anything — mixed so the
        session is not only the words you just added.
        """
        recent_n = max(1, round(count * 0.6))
        weak_n = max(0, round(count * 0.3))
        picked: dict[int, dict] = {}

        def take(sql, params, limit):
            if limit <= 0:
                return
            with self._conn() as con:
                for row in con.execute(sql, (*params, limit)):
                    if row["id"] not in picked and len(picked) < count:
                        picked[row["id"]] = dict(row)

        base = (
            "SELECT i.* FROM items i WHERE i.status='committed' "
            "AND i.sentence_masked IS NOT NULL AND i.sentence_masked != '' "
        )
        take(base + "AND i.created_at >= datetime('now','-7 days') "
                    "ORDER BY RANDOM() LIMIT ?", (), recent_n)
        take(base + "AND EXISTS (SELECT 1 FROM bot_reviews r WHERE r.item_id=i.id "
                    "AND r.rating <= 2 AND r.reviewed_at < datetime('now','-2 days')) "
                    "ORDER BY RANDOM() LIMIT ?", (), weak_n)
        take(base + "ORDER BY RANDOM() LIMIT ?", (), count - len(picked))
        return list(picked.values())

    # -- listings (P2) ---------------------------------------------------

    def words_since(self, expr: str = "-1 days") -> list[dict]:
        with self._conn() as con:
            return [
                dict(r)
                for r in con.execute(
                    "SELECT id, lemma, definition, created_at FROM items "
                    "WHERE status='committed' AND created_at >= datetime('now', ?) "
                    "ORDER BY id",
                    (expr,),
                )
            ]

    def today_count(self) -> int:
        with self._conn() as con:
            return con.execute(
                "SELECT COUNT(*) FROM items WHERE status='committed' "
                "AND created_at >= date('now')"
            ).fetchone()[0]

    def contexts_for(self, lemma: str) -> list[dict]:
        with self._conn() as con:
            return [
                dict(r)
                for r in con.execute(
                    f"SELECT id, sentence, url, page_title, created_at FROM items "
                    f"WHERE lemma=? AND status IN {LIVE} ORDER BY id",
                    (lemma,),
                )
            ]

    def last_committed(self) -> dict | None:
        with self._conn() as con:
            row = con.execute(
                "SELECT * FROM items WHERE status='committed' ORDER BY committed_at DESC, "
                "id DESC LIMIT 1"
            ).fetchone()
            return dict(row) if row else None

    def mark_retracted(self, item_id: int) -> None:
        with self._conn() as con:
            con.execute(
                "UPDATE items SET status='skipped', anki_note_id=NULL WHERE id=?", (item_id,)
            )
