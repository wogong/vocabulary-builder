"""P0 acceptance criterion, end to end (spec §9 P0).

    "Double-click a word on a page and the card, with its context, shows up
     in Anki on another device after a sync."

Stands up a real sync server, runs the documented §8.4 bootstrap, drives the
real API, then syncs a second collection down and looks for the card.

    python tests/test_acceptance.py
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


def wait_port(port, timeout=15):
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.2):
                return True
        except OSError:
            time.sleep(0.1)
    return False


SENTENCE = "Smartphones are now ubiquitous in daily life."


def main():
    tmp = Path(tempfile.mkdtemp(prefix="vocab-accept-"))
    port = free_port()
    endpoint = f"http://127.0.0.1:{port}/"
    server = None
    try:
        from anki.collection import Collection
        from anki.sync_pb2 import SyncCollectionResponse as SyncResp

        from app.notetype import build_notetype

        print("sync server")
        (tmp / "syncbase").mkdir(parents=True)
        server = subprocess.Popen(
            [sys.executable, "-m", "anki.syncserver"],
            env={
                **os.environ,
                "SYNC_USER1": "vocab:secret",
                "SYNC_BASE": str(tmp / "syncbase"),
                "SYNC_PORT": str(port),
                "SYNC_HOST": "127.0.0.1",
            },
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        check("sync server listening", wait_port(port))

        print("desktop anki prepares the note type (§8.4 steps 1-2)")
        desktop_dir = tmp / "desktop"
        desktop_dir.mkdir()
        desktop = Collection(str(desktop_dir / "collection.anki2"))
        build_notetype(desktop)
        desktop.decks.id("Vocab::Reading")
        auth = desktop.sync_login("vocab", "secret", endpoint)
        out = desktop.sync_collection(auth, True)
        desktop.close_for_full_sync()
        desktop.full_upload_or_download(auth=auth, server_usn=out.server_media_usn, upload=True)
        desktop.reopen(after_full_sync=True)
        desktop.close()
        check("note type uploaded to the sync server", True)

        print("server bootstraps from the sync server (§8.4 step 3)")
        server_dir = tmp / "server"
        server_dir.mkdir()
        env = {
            **os.environ,
            "ANKI_COLLECTION_PATH": str(server_dir / "collection.anki2"),
            "ANKI_DECK": "Vocab::Reading",
            "ANKI_NOTETYPE": "VocabContext",
            "INBOX_DB": str(tmp / "app" / "inbox.db"),
            "API_TOKENS": "chrome:secret-token",
            "SYNC_ENDPOINT": endpoint,
            "SYNC_USERNAME": "vocab",
            "SYNC_PASSWORD": "secret",
            "SYNC_DEBOUNCE_SEC": "2",
            "SYNC_INTERVAL_SEC": "1800",
        }
        boot = subprocess.run(
            [sys.executable, "scripts/bootstrap_collection.py"],
            cwd=ROOT,
            env=env,
            capture_output=True,
            text=True,
        )
        check("bootstrap_collection.py succeeds", boot.returncode == 0, boot.stdout + boot.stderr)
        check("bootstrap found the note type", "note type 'VocabContext': ok" in boot.stdout, boot.stdout)

        print("extension submits a word")
        os.environ.update(env)
        from fastapi.testclient import TestClient

        from app.main import app

        with TestClient(app) as client:
            r = client.post(
                "/v1/items",
                headers={"Authorization": "Bearer secret-token"},
                json={
                    "idem_key": "accept-1",
                    "source": "chrome",
                    "word": "ubiquitous",
                    "sentence": SENTENCE,
                    "word_offset": SENTENCE.index("ubiquitous"),
                    "url": "https://example.com/article",
                    "page_title": "The Rise of Mobile",
                },
            )
            check("server accepts the capture", r.status_code == 202, r.text)

            deadline = time.time() + 30
            while time.time() < deadline:
                health = client.get("/healthz").json()
                if health["queue_depth"] == 0 and health["last_sync_at"]:
                    break
                time.sleep(0.5)
            check("debounced sync fired on its own", bool(health["last_sync_at"]), health)
            check("no full sync demanded", health["full_sync_required"] is False, health)
            check("no sync error", health["last_sync_error"] is None, health)

        print("a second device syncs down")
        phone_dir = tmp / "phone"
        phone_dir.mkdir()
        phone = Collection(str(phone_dir / "collection.anki2"))
        phone_auth = phone.sync_login("vocab", "secret", endpoint)
        out = phone.sync_collection(phone_auth, True)
        if out.required in (SyncResp.FULL_DOWNLOAD, SyncResp.FULL_SYNC):
            phone.close_for_full_sync()
            phone.full_upload_or_download(
                auth=phone_auth, server_usn=out.server_media_usn, upload=False
            )
            phone.reopen(after_full_sync=True)

        note_ids = phone.find_notes('note:VocabContext "Word:ubiquitous"')
        check("card arrived on the second device", len(note_ids) == 1, list(note_ids))
        if note_ids:
            note = phone.get_note(note_ids[0])
            check(
                "context came with it",
                note["Contexts"] == "Smartphones are now <b>ubiquitous</b> in daily life.",
                note["Contexts"],
            )
            check(
                "front is the cloze-style prompt",
                note["ContextsMasked"] == "Smartphones are now <b>____</b> in daily life.",
                note["ContextsMasked"],
            )
            check("it has a reviewable card", len(note.card_ids()) == 1)
            check(
                "in the right deck",
                phone.decks.name(phone.get_card(note.card_ids()[0]).did) == "Vocab::Reading",
            )
        phone.close()

        print("backups")
        backup_env = {**env, "BACKUP_DIR": str(tmp / "backup")}
        result = subprocess.run(
            [sys.executable, "scripts/backup.py", "l1", "l2", "l3"],
            cwd=ROOT,
            env=backup_env,
            capture_output=True,
            text=True,
        )
        check("backup.py runs all three layers", result.returncode == 0, result.stdout + result.stderr)
        check("L1 inbox snapshot", any((tmp / "backup" / "l1").glob("inbox-*.db")))
        check("L2 collection snapshot", any((tmp / "backup" / "l2").glob("collection-*.anki2")))
        apkgs = list((tmp / "backup" / "l3").glob("vocab-*.apkg"))
        check("L3 apkg export", len(apkgs) == 1 and apkgs[0].stat().st_size > 0)
    finally:
        if server:
            server.terminate()
            server.wait(timeout=10)
        shutil.rmtree(tmp, ignore_errors=True)

    print()
    if failures:
        print(f"{len(failures)} check(s) failed: {failures}")
        return 1
    print("P0 acceptance: page capture reaches a second device, backups work")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
