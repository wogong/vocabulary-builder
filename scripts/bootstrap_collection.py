"""One-time collection setup (spec §8.4).

Two ways to get a usable collection onto the server. Both are deliberately
manual: they can overwrite a whole collection, and that is not a decision a
service should make on its own (R2).

  --seed-local   Create the note type here and push it to the sync server.
                 Fastest first run; no desktop Anki round trip. Adjust the
                 card template and styling later from any Anki client —
                 editing an existing template is a normal synced change, not
                 a schema change, so it will not force a full sync.

  (no flag)      Download a collection you already prepared in desktop Anki
                 and synced up. Use this when you want to design the note
                 type in the GUI first (the original §8.4 flow, S11).

    docker compose stop app
    docker compose run --rm app python scripts/bootstrap_collection.py --seed-local
    docker compose start app
"""

import argparse
import fcntl
import sys
from contextlib import contextmanager
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from anki.collection import Collection  # noqa: E402
from anki.sync_pb2 import SyncCollectionResponse as SyncResp  # noqa: E402

from app.config import Config  # noqa: E402
from app.notetype import SchemaError, build_notetype, verify_notetype  # noqa: E402

NEEDS_UPLOAD = (SyncResp.FULL_UPLOAD,)
NEEDS_DOWNLOAD = (SyncResp.FULL_DOWNLOAD, SyncResp.FULL_SYNC)


@contextmanager
def locked_collection(cfg):
    path = Path(cfg.collection_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    lock = open(path.with_suffix(".lock"), "w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        lock.close()
        raise SystemExit(
            "The collection is locked by another process.\n"
            "Stop the app first:  docker compose stop app"
        )
    col = Collection(str(path))
    try:
        yield col
    finally:
        col.close()
        fcntl.flock(lock, fcntl.LOCK_UN)
        lock.close()


def has_notetype(col, cfg) -> bool:
    try:
        verify_notetype(col, cfg.notetype)
        return True
    except SchemaError:
        return False


def login(col, cfg):
    print(f"logging in to {cfg.sync_endpoint} as {cfg.sync_username}")
    return col.sync_login(cfg.sync_username, cfg.sync_password, cfg.sync_endpoint)


def seed_local(col, cfg) -> int:
    """Create the note type here, then make the sync server match."""
    if col.note_count() > 0:
        print(
            f"Refusing to seed: this collection already holds {col.note_count()} notes "
            f"but no {cfg.notetype!r} note type. Sort that out by hand first."
        )
        return 1

    auth = None
    if cfg.sync_enabled:
        # Probe BEFORE touching anything locally. Adding a note type is itself a
        # schema change and would make the server demand a full sync, which is
        # indistinguishable from "the server holds a collection of its own"
        # unless we ask first, while this collection is still untouched.
        auth = login(col, cfg)
        probe = col.sync_collection(auth, True)
        if probe.required in NEEDS_DOWNLOAD:
            print(
                f"\nThe sync server already holds a collection (required="
                f"{probe.required}), so seeding would overwrite it. Re-run without "
                "--seed-local to download that collection instead."
            )
            return 1
        print("sync server holds no collection yet")

    build_notetype(col)
    col.decks.id(cfg.deck)
    print(f"created note type {cfg.notetype!r} and deck {cfg.deck!r} locally")

    if not cfg.sync_enabled:
        print("sync is not configured; nothing pushed. Local collection is ready.")
        return 0

    out = col.sync_collection(auth, True)
    if out.required == SyncResp.NO_CHANGES:
        print("pushed to the sync server")
        return 0

    # Creating a note type is a schema change, so a full sync is expected here.
    # Uploading is safe: the probe above established the server had nothing.
    print(f"uploading this collection as the baseline (required={out.required})")
    col.close_for_full_sync()
    col.full_upload_or_download(auth=auth, server_usn=out.server_media_usn, upload=True)
    col.reopen(after_full_sync=True)
    return 0


def download(col, cfg) -> int:
    """Pull the collection prepared in desktop Anki (§8.4)."""
    if not cfg.sync_enabled:
        print(
            "Sync is not configured, so there is nothing to download.\n"
            "Use --seed-local to create the note type here instead."
        )
        return 1

    if col.note_count() > 0:
        print(
            f"Refusing to continue: {cfg.collection_path} already holds "
            f"{col.note_count()} notes but not the expected note type. A full "
            "download would destroy them. Move the file aside and re-run if that "
            "is really what you want."
        )
        return 1

    auth = login(col, cfg)
    out = col.sync_collection(auth, True)

    if out.required in NEEDS_DOWNLOAD:
        print("downloading the collection from the sync server")
        col.close_for_full_sync()
        col.full_upload_or_download(auth=auth, server_usn=out.server_media_usn, upload=False)
        col.reopen(after_full_sync=True)
    else:
        # Nothing came down: the server has no collection to give.
        print(
            f"\nThe sync server has nothing to download (required={out.required}) — "
            f"it is empty, so no {cfg.notetype!r} note type arrived.\n\n"
            "Either finish the desktop Anki step first:\n"
            "  1. uv run python scripts/make_notetype.py VocabContext.apkg\n"
            "  2. import it into desktop Anki, point Anki's custom sync server at\n"
            f"     this server, and sync up\n"
            "  3. re-run this script\n\n"
            "or skip the round trip entirely:\n"
            "  docker compose run --rm app python scripts/bootstrap_collection.py --seed-local"
        )
        return 1

    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument(
        "--seed-local",
        action="store_true",
        help="create the note type here and push it, instead of downloading one",
    )
    args = parser.parse_args()

    cfg = Config.from_env()
    with locked_collection(cfg) as col:
        if has_notetype(col, cfg):
            print(f"{cfg.notetype!r} is already present — nothing to do.")
            return 0

        rc = seed_local(col, cfg) if args.seed_local else download(col, cfg)
        if rc != 0:
            return rc

        try:
            verify_notetype(col, cfg.notetype)
        except SchemaError as exc:
            print(f"\nCollection is still not usable:\n  {exc}")
            return 1

        print(f"\nnote type {cfg.notetype!r}: ok")
        print(f"deck {cfg.deck!r}: {'ok' if col.decks.id_for_name(cfg.deck) else 'missing (created on start)'}")
        print(f"notes in collection: {col.note_count()}")
        print("\nbootstrap complete — start the app:  docker compose start app")
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
