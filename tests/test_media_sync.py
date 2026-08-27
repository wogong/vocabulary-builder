"""Media reaches a second device (decision B3).

Pronunciation audio is only useful if it survives the trip through the sync
server. Headless media sync is a known sore spot, so this runs the whole path:
synthesise -> collection media -> sync -> a second collection downloads it.

    uv run python tests/test_media_sync.py
"""

import os
import shutil
import socket
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


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def main():
    tmp = Path(tempfile.mkdtemp(prefix="vocab-media-"))
    port = free_port()
    endpoint = f"http://127.0.0.1:{port}/"
    server = writer = None
    try:
        from anki.collection import Collection
        from anki.sync_pb2 import SyncCollectionResponse as SyncResp

        from app.anki_writer import AnkiWriter
        from app.config import Config
        from app.db import Inbox
        from app.notetype import build_notetype
        from app.tts import Tts, media_name

        (tmp / "syncbase").mkdir(parents=True)
        server = subprocess.Popen(
            [sys.executable, "-m", "anki.syncserver"],
            env={**os.environ, "SYNC_USER1": "vocab:secret",
                 "SYNC_BASE": str(tmp / "syncbase"), "SYNC_PORT": str(port),
                 "SYNC_HOST": "127.0.0.1"},
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        deadline = time.time() + 15
        while time.time() < deadline:
            try:
                with socket.create_connection(("127.0.0.1", port), timeout=0.2):
                    break
            except OSError:
                time.sleep(0.1)

        col_path = tmp / "anki" / "collection.anki2"
        col_path.parent.mkdir(parents=True)
        col = Collection(str(col_path))
        build_notetype(col)
        col.decks.id("Vocab")
        auth = col.sync_login("vocab", "secret", endpoint)
        out = col.sync_collection(auth, True)
        col.close_for_full_sync()
        col.full_upload_or_download(auth=auth, server_usn=out.server_media_usn, upload=True)
        col.reopen(after_full_sync=True)
        col.close()

        os.environ.update(
            ANKI_COLLECTION_PATH=str(col_path), ANKI_DECK="Vocab",
            ANKI_NOTETYPE="VocabContext", INBOX_DB=str(tmp / "app" / "inbox.db"),
            API_TOKENS="t:t", SYNC_ENDPOINT=endpoint, SYNC_USERNAME="vocab",
            SYNC_PASSWORD="secret", SYNC_DEBOUNCE_SEC="2", SYNC_INTERVAL_SEC="1800",
            ECDICT_PATH="", CEDICT_PATH="", TTS_ENABLED="true",
        )
        cfg = Config.from_env()
        inbox = Inbox(cfg.inbox_db)
        writer = AnkiWriter(cfg, inbox)
        writer.start()
        writer.wait_ready()

        print("capture a word and give it audio")
        from app.capture import capture

        sentence = "Smartphones are now ubiquitous."
        capture(inbox, None, writer, word="ubiquitous", sentence=sentence,
                source="test", word_offset=sentence.index("ubiquitous"))
        deadline = time.time() + 20
        while time.time() < deadline and writer.queue_depth:
            time.sleep(0.05)
        time.sleep(0.3)

        tts = Tts(cfg, inbox, writer)
        # Real synthesis: this is the dependency worth testing for real.
        tts._synthesise("ubiquitous")
        filename = media_name("ubiquitous")
        check("audio generated and stored", writer.ask(lambda c: c.media.have(filename)),
              "no media file")
        size = writer.ask(lambda c: (Path(c.media.dir()) / filename).stat().st_size)
        check("audio is a real mp3", size > 2000, f"{size} bytes")
        check("note references it",
              writer.ask(lambda c: _audio_field(c)) == f"[sound:{filename}]")

        print("sync it up")
        deadline = time.time() + 60
        while time.time() < deadline and not writer.status["last_sync_at"]:
            time.sleep(0.5)
        check("collection synced", bool(writer.status["last_sync_at"]),
              writer.status)
        check("no full sync demanded", writer.status["full_sync_required"] is False)
        # Media transfers on its own task; give it a moment to finish.
        time.sleep(3)
        writer.ask(lambda c: c.media_sync_status())

        print("second device pulls it down")
        phone_dir = tmp / "phone"
        phone_dir.mkdir()
        phone = Collection(str(phone_dir / "collection.anki2"))
        phone_auth = phone.sync_login("vocab", "secret", endpoint)
        out = phone.sync_collection(phone_auth, True)
        if out.required in (SyncResp.FULL_DOWNLOAD, SyncResp.FULL_SYNC):
            phone.close_for_full_sync()
            phone.full_upload_or_download(
                auth=phone_auth, server_usn=out.server_media_usn, upload=False)
            phone.reopen(after_full_sync=True)
        deadline = time.time() + 60
        while time.time() < deadline:
            phone.sync_media(phone_auth)
            time.sleep(2)
            if phone.media.have(filename):
                break
        check("audio file arrived on the second device", phone.media.have(filename),
              "media did not sync")
        if phone.media.have(filename):
            check("and it is the same bytes",
                  (Path(phone.media.dir()) / filename).stat().st_size == size)
        note_ids = phone.find_notes('note:VocabContext "Word:ubiquitous"')
        check("so did the note", len(note_ids) == 1)
        if note_ids:
            check("with its [sound:] reference",
                  phone.get_note(note_ids[0])["Audio"] == f"[sound:{filename}]")
        phone.close()
    finally:
        if writer:
            writer.stop()
        if server:
            server.terminate()
            server.wait(timeout=10)
        shutil.rmtree(tmp, ignore_errors=True)

    print()
    if failures:
        print(f"{len(failures)} check(s) failed: {failures}")
        return 1
    print("media sync: pronunciation audio reaches a second device")
    return 0


def _audio_field(col):
    ids = col.find_notes('note:VocabContext "Word:ubiquitous"')
    return col.get_note(ids[0])["Audio"] if ids else None


if __name__ == "__main__":
    raise SystemExit(main())
