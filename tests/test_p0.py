"""P0 acceptance test: HTTP in, Anki note out.

Exercises the real app (inbox -> writer -> collection) over the real API.

    python tests/test_p0.py
"""

import os
import shutil
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

PASS, FAIL = "\033[32mPASS\033[0m", "\033[31mFAIL\033[0m"
failures = []


def check(label, cond, detail=""):
    print(f"  [{PASS if cond else FAIL}] {label}{'' if cond else '  <- ' + str(detail)}")
    if not cond:
        failures.append(label)


def setup_env(tmp: Path):
    """Stand in for the one-time manual setup of §8.4."""
    from anki.collection import Collection

    from app.notetype import build_notetype

    col_path = tmp / "anki" / "collection.anki2"
    col_path.parent.mkdir(parents=True)
    col = Collection(str(col_path))
    build_notetype(col)
    col.decks.id("Vocab::Reading")
    col.close()

    os.environ.update(
        ANKI_COLLECTION_PATH=str(col_path),
        ANKI_DECK="Vocab::Reading",
        ANKI_NOTETYPE="VocabContext",
        INBOX_DB=str(tmp / "app" / "inbox.db"),
        API_TOKENS="chrome:secret-token",
        SYNC_USERNAME="",
        SYNC_PASSWORD="",
        SYNC_DEBOUNCE_SEC="60",
        SYNC_INTERVAL_SEC="1800",
    )
    return col_path


def drain(client, timeout=20.0):
    """Wait for the writer to finish projecting."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        if client.get("/healthz").json()["queue_depth"] == 0:
            time.sleep(0.15)  # let the in-flight item finish committing
            if client.get("/healthz").json()["queue_depth"] == 0:
                return True
        time.sleep(0.1)
    return False


AUTH = {"Authorization": "Bearer secret-token"}
SENTENCE = "Smartphones are now ubiquitous in daily life."
OFFSET = SENTENCE.index("ubiquitous")


def main():
    tmp = Path(tempfile.mkdtemp(prefix="vocab-p0-"))
    col_path = setup_env(tmp)
    try:
        from fastapi.testclient import TestClient

        from app.main import app

        with TestClient(app) as client:
            print("auth")
            check("no token -> 401", client.post("/v1/items", json={}).status_code == 401)
            check(
                "wrong token -> 401",
                client.post(
                    "/v1/items", json={}, headers={"Authorization": "Bearer nope"}
                ).status_code
                == 401,
            )

            print("submit")
            r = client.post(
                "/v1/items",
                headers=AUTH,
                json={
                    "idem_key": "key-1",
                    "source": "chrome",
                    "word": "ubiquitous",
                    "sentence": SENTENCE,
                    "word_offset": OFFSET,
                    "url": "https://example.com/a",
                    "page_title": "The Rise of Mobile",
                },
            )
            check("accepted with 202", r.status_code == 202, r.text)
            body = r.json()
            check("dedup=new", body["dedup"] == "new", body)
            check("lemma normalised", body["lemma"] == "ubiquitous", body)

            check(
                "invalid payload -> 422",
                client.post("/v1/items", headers=AUTH, json={"word": "x"}).status_code == 422,
            )
            check(
                "repeated idem_key -> 409",
                client.post(
                    "/v1/items",
                    headers=AUTH,
                    json={"idem_key": "key-1", "word": "ubiquitous", "sentence": SENTENCE},
                ).status_code
                == 409,
            )

            print("dedup")
            same = client.post(
                "/v1/items",
                headers=AUTH,
                json={"word": "Ubiquitous", "sentence": SENTENCE, "word_offset": OFFSET},
            ).json()
            check("same word + same sentence -> duplicate", same["dedup"] == "duplicate", same)

            second = "The device became ubiquitous within a decade."
            merged = client.post(
                "/v1/items",
                headers=AUTH,
                json={
                    "word": "ubiquitous",
                    "sentence": second,
                    "word_offset": second.index("ubiquitous"),
                    "url": "https://example.com/b",
                    "page_title": "Second Source",
                },
            ).json()
            check("same word + new sentence -> merged", merged["dedup"] == "merged", merged)
            check("existing_contexts reported", merged["existing_contexts"] >= 1, merged)

            check("writer drained", drain(client))
            health = client.get("/healthz").json()
            check("healthz ok", health["ok"] is True, health)
            check("nothing failed", health["failed"] == 0, health)

        print("projection")
        from anki.collection import Collection

        col = Collection(str(col_path))
        note_ids = col.find_notes('note:VocabContext "Word:ubiquitous"')
        check("exactly one note for the word (D4)", len(note_ids) == 1, note_ids)
        if note_ids:
            note = col.get_note(note_ids[0])
            check("Word field holds the lemma", note["Word"] == "ubiquitous")
            check(
                "both contexts merged onto one note",
                note["Contexts"].count("<hr>") == 1,
                note["Contexts"],
            )
            check(
                "target word is bolded",
                "<b>ubiquitous</b>" in note["Contexts"],
                note["Contexts"],
            )
            check(
                "front is the masked newest context",
                note["ContextsMasked"] == "The device became <b>____</b> within a decade.",
                note["ContextsMasked"],
            )
            check(
                "source link recorded",
                'href="https://example.com/b"' in note["Source"],
                note["Source"],
            )
            check("meta traces back to inbox items", '"item_ids"' in note["Meta"], note["Meta"])
            check("card was generated", len(note.card_ids()) == 1)
            check(
                "card landed in the configured deck",
                col.decks.name(col.get_card(note.card_ids()[0]).did) == "Vocab::Reading",
            )
        col.close()

        print("safety rails")
        from app.anki_writer import AnkiWriter, CollectionLocked
        from app.config import Config
        from app.db import Inbox
        from app.notetype import SchemaError, verify_notetype

        cfg = Config.from_env()
        holder = AnkiWriter(cfg, Inbox(cfg.inbox_db))
        try:
            second = None
            try:
                second = AnkiWriter(cfg, Inbox(cfg.inbox_db))
            except CollectionLocked:
                pass
            check("a second writer cannot open the collection (R3)", second is None)
        finally:
            holder._lock_fd.close()

        col = Collection(str(col_path))
        try:
            col.models.add_field(
                col.models.by_name("VocabContext"), col.models.new_field("Sneaky")
            )
            renamed = col.models.by_name("VocabContext")
            renamed["flds"].append(renamed["flds"].pop(0))  # reorder, don't persist
            try:
                verify_notetype(col, "VocabContext")
                drifted = False
            except SchemaError:
                drifted = True
            check("field drift is rejected, not migrated (D3)", drifted)
        finally:
            col.close()

        print("repeated word in one sentence")
        from app import render

        s = "A ubiquitous problem is that ubiquitous is hard to spell."
        second_at = s.index("ubiquitous", 5)
        check(
            "word_offset picks the right occurrence",
            render.mask_sentence(s, "ubiquitous", second_at)
            == "A ubiquitous problem is that ____ is hard to spell.",
            render.mask_sentence(s, "ubiquitous", second_at),
        )
        check(
            "bad offset falls back to first match",
            render.mask_sentence(s, "ubiquitous", 999)
            == "A ____ problem is that ubiquitous is hard to spell.",
        )
        check(
            "html in the page text is escaped",
            render.context_html("a <script> tag & more", "tag", 12)
            == "a &lt;script&gt; <b>tag</b> &amp; more",
            render.context_html("a <script> tag & more", "tag", 12),
        )
        check(
            "context list is capped",
            render.append_context("a<hr>b<hr>c", "d", 3) == "b<hr>c<hr>d",
        )
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    print()
    if failures:
        print(f"{len(failures)} check(s) failed: {failures}")
        return 1
    print("P0 acceptance: all checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
