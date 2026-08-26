"""R1 smoke test: pin down the anki API surface we depend on.

Covers the four calls the spec calls out — add / update / find / sync — plus the
apkg export used by the L3 backup. Run this before bumping `anki` in
pyproject.toml:

    uv run python tests/smoke_anki.py
"""

import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from anki.collection import Collection, SearchNode  # noqa: E402
from anki.sync_pb2 import SyncCollectionResponse as SyncResp  # noqa: E402

from app.notetype import FIELDS, NOTETYPE_NAME, build_notetype, verify_notetype  # noqa: E402

PASS, FAIL = "\033[32mPASS\033[0m", "\033[31mFAIL\033[0m"


def check(label, cond):
    print(f"  [{PASS if cond else FAIL}] {label}")
    if not cond:
        raise SystemExit(1)


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def test_crud(tmp: Path):
    print("collection: create / add / find / update / export")
    col = Collection(str(tmp / "collection.anki2"))
    notetype = build_notetype(col)
    check("build_notetype creates all 10 fields", [f["name"] for f in notetype["flds"]] == FIELDS)
    check("verify_notetype accepts it", verify_notetype(col, NOTETYPE_NAME) is not None)

    deck_id = col.decks.id("Vocab::Reading")
    note = col.new_note(notetype)
    note["Word"] = "ubiquitous"
    note["Contexts"] = "Smartphones are now <b>ubiquitous</b>."
    note["ContextsMasked"] = "Smartphones are now <b>____</b>."
    col.add_note(note, deck_id)
    check("add_note assigns a note id", note.id > 0)
    check("add_note generates one card", len(note.card_ids()) == 1)

    dupe = col.build_search_string(
        SearchNode(dupe=SearchNode.Dupe(notetype_id=notetype["id"], first_field="ubiquitous"))
    )
    check("dupe search finds the note by first field", list(col.find_notes(dupe)) == [note.id])
    check(
        "dupe search misses a different word",
        not col.find_notes(
            col.build_search_string(
                SearchNode(dupe=SearchNode.Dupe(notetype_id=notetype["id"], first_field="other"))
            )
        ),
    )

    note["Contexts"] += "<hr>A second <b>ubiquitous</b> sighting."
    col.update_note(note)
    check("update_note persists", col.get_note(note.id)["Contexts"].count("<hr>") == 1)

    # An empty ContextsMasked must still produce a card (§7.3 words with no context).
    bare = col.new_note(notetype)
    bare["Word"] = "bare"
    col.add_note(bare, deck_id)
    check("note without context still generates a card", len(bare.card_ids()) == 1)

    from anki.collection import ExportAnkiPackageOptions

    out = tmp / "backup.apkg"
    col.export_anki_package(
        out_path=str(out),
        options=ExportAnkiPackageOptions(with_scheduling=True, with_media=True, legacy=False),
        limit=None,
    )
    check("export_anki_package writes a file", out.exists() and out.stat().st_size > 0)
    col.close()


def test_sync(tmp: Path):
    """Full round trip against a real self-hosted sync server (D7)."""
    print("sync: login / full upload / incremental")
    port = free_port()
    base = tmp / "syncbase"
    base.mkdir()
    env = {**os.environ, "SYNC_USER1": "tester:secret", "SYNC_BASE": str(base), "SYNC_PORT": str(port)}
    proc = subprocess.Popen(
        [sys.executable, "-m", "anki.syncserver"],
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        endpoint = f"http://127.0.0.1:{port}/"
        for _ in range(100):
            try:
                with socket.create_connection(("127.0.0.1", port), timeout=0.2):
                    break
            except OSError:
                time.sleep(0.1)
        else:
            raise SystemExit("sync server did not start")

        col = Collection(str(tmp / "sync-collection.anki2"))
        build_notetype(col)
        auth = col.sync_login("tester", "secret", endpoint)
        check("sync_login returns auth", bool(auth.hkey))

        out = col.sync_collection(auth, True)
        check(
            "empty server asks for a full upload",
            out.required in (SyncResp.FULL_UPLOAD, SyncResp.FULL_SYNC),
        )

        col.close_for_full_sync()
        col.full_upload_or_download(auth=auth, server_usn=out.server_media_usn, upload=True)
        col.reopen(after_full_sync=True)

        out = col.sync_collection(auth, True)
        check("second sync is a no-op", out.required == SyncResp.NO_CHANGES)

        note = col.new_note(col.models.by_name(NOTETYPE_NAME))
        note["Word"] = "incremental"
        note["ContextsMasked"] = "an <b>____</b> change"
        col.add_note(note, col.decks.id("Vocab::Reading"))
        out = col.sync_collection(auth, True)
        check("incremental change syncs without a full sync", out.required == SyncResp.NO_CHANGES)
        col.close()
    finally:
        proc.terminate()
        proc.wait(timeout=10)


def main():
    tmp = Path(tempfile.mkdtemp(prefix="vocab-smoke-"))
    try:
        from anki.buildinfo import version

        print(f"anki {version} on python {sys.version.split()[0]}\n")
        test_crud(tmp)
        print()
        test_sync(tmp)
        print("\nall smoke checks passed")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    main()
