"""Recovery from a collection swap, and the sync retry backoff.

Reproduces what happens when a client uploads its own collection to the sync
server after the server was seeded: the two diverge, sync demands a full sync,
and the captures have to be replayed into the collection that wins (D1).

    uv run python tests/test_recovery.py
"""

import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

PASS, FAIL = "\033[32mPASS\033[0m", "\033[31mFAIL\033[0m"
failures = []


def check(label, cond, detail=""):
    print(f"  [{PASS if cond else FAIL}] {label}{'' if cond else '  <- ' + str(detail)}")
    if not cond:
        failures.append(label)


def main():
    tmp = Path(tempfile.mkdtemp(prefix="vocab-recover-"))
    try:
        from anki.collection import Collection

        from app.anki_writer import AnkiWriter
        from app.config import Config
        from app.db import Inbox
        from app.notetype import build_notetype

        col_path = tmp / "anki" / "collection.anki2"
        col_path.parent.mkdir(parents=True)
        col = Collection(str(col_path))
        build_notetype(col)
        col.decks.id("Vocab")
        col.close()

        env = dict(
            ANKI_COLLECTION_PATH=str(col_path),
            ANKI_DECK="Vocab",
            ANKI_NOTETYPE="VocabContext",
            INBOX_DB=str(tmp / "app" / "inbox.db"),
            API_TOKENS="chrome:secret-token",
            SYNC_USERNAME="",
            SYNC_PASSWORD="",
            SYNC_DEBOUNCE_SEC="60",
            SYNC_INTERVAL_SEC="1800",
        )
        os.environ.update(env)

        print("capture three words")
        from fastapi.testclient import TestClient

        from app.main import app

        words = [
            ("applications", "Modern applications ship weekly."),
            ("whose", "A team whose tools fit works faster."),
            ("rigorous", "It demands rigorous testing."),
        ]
        with TestClient(app) as client:
            for word, sentence in words:
                client.post(
                    "/v1/items",
                    headers={"Authorization": "Bearer secret-token"},
                    json={
                        "word": word,
                        "sentence": sentence,
                        "word_offset": sentence.index(word),
                        "url": "https://example.com",
                    },
                )
            deadline = time.time() + 20
            while time.time() < deadline and client.get("/healthz").json()["queue_depth"]:
                time.sleep(0.1)
            time.sleep(0.3)

        col = Collection(str(col_path))
        check("three notes projected", col.note_count() == 3, col.note_count())
        col.close()

        print("the collection is replaced by a different one (client uploaded its own)")
        col_path.unlink()
        for stray in col_path.parent.glob("collection.anki2-*"):
            stray.unlink()
        replacement = Collection(str(col_path))
        build_notetype(replacement)
        replacement.decks.id("Vocab")
        note = replacement.new_note(replacement.models.by_name("VocabContext"))
        note["Word"] = "preexisting"
        replacement.add_note(note, replacement.decks.id("Vocab"))
        replacement.close()

        col = Collection(str(col_path))
        check("captures are gone from the new collection", col.note_count() == 1)
        col.close()

        print("replay from the inbox")
        result = subprocess.run(
            [sys.executable, "scripts/reproject.py", "--all"],
            cwd=ROOT,
            env={**os.environ},
            capture_output=True,
            text=True,
        )
        check("reproject succeeds", result.returncode == 0, result.stdout + result.stderr)
        check("reports three items", "3 item(s) marked pending" in result.stdout, result.stdout)

        cfg = Config.from_env()
        inbox = Inbox(cfg.inbox_db)
        writer = AnkiWriter(cfg, inbox)
        writer.start()
        writer.wait_ready()
        deadline = time.time() + 20
        while time.time() < deadline and writer.queue_depth:
            time.sleep(0.1)
        time.sleep(0.5)
        writer.stop()

        col = Collection(str(col_path))
        check(
            "captures are back, alongside the pre-existing note",
            col.note_count() == 4,
            col.note_count(),
        )
        # P1 files notes under the lemma, so that is what to look for.
        from app.lemma import lemmatize

        for word, _ in words:
            key = lemmatize(word)
            found = col.find_notes(f'note:VocabContext "Word:{key}"')
            check(f"  {word} restored (as {key!r})", len(found) == 1, list(found))
        check("pre-existing note untouched", len(col.find_notes('"Word:preexisting"')) == 1)
        col.close()

        print("replaying twice does not duplicate (D4)")
        subprocess.run(
            [sys.executable, "scripts/reproject.py", "--all"],
            cwd=ROOT, env={**os.environ}, capture_output=True, text=True,
        )
        writer = AnkiWriter(cfg, inbox)
        writer.start()
        writer.wait_ready()
        deadline = time.time() + 20
        while time.time() < deadline and writer.queue_depth:
            time.sleep(0.1)
        time.sleep(0.5)
        writer.stop()
        col = Collection(str(col_path))
        check("still four notes", col.note_count() == 4, col.note_count())
        col.close()

        print("sync retry backs off instead of spinning")
        cfg2 = Config(
            **{**cfg.__dict__, "sync_debounce_sec": 5, "sync_interval_sec": 1800}
        )
        w = AnkiWriter(cfg2, inbox)
        w._dirty = True
        w._last_write = time.monotonic() - 600
        w._last_sync = time.monotonic()  # an attempt just failed
        due = w._next_debounced_sync() - time.monotonic()
        check("next attempt waits a full debounce window", 4.0 < due <= 5.0, due)
        check("sleep is not a busy 1s tick", w._sleep_for() > 3.0, w._sleep_for())
        w._lock_fd.close()
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    print()
    if failures:
        print(f"{len(failures)} check(s) failed: {failures}")
        return 1
    print("recovery: captures survive a collection swap; sync retries back off")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
