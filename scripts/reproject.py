"""Replay inbox items into the Anki collection (spec D1).

The inbox is the source of truth; Anki is a projection of it. When the
collection is rebuilt or replaced — a full download, a restore from backup, a
switch to a different collection — the notes are gone but the captures are not.
This marks them pending again so the writer recreates them on next start.

    docker compose stop app
    docker compose run --rm app python scripts/reproject.py --all
    docker compose start app

Items already present in the collection are merged, not duplicated (D4), so
replaying more than necessary is safe.
"""

import argparse
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config import Config  # noqa: E402
from app.db import LIVE  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    scope = parser.add_mutually_exclusive_group(required=True)
    scope.add_argument("--all", action="store_true", help="every captured item")
    scope.add_argument("--failed", action="store_true", help="only items that failed")
    scope.add_argument("--since", metavar="YYYY-MM-DD", help="items captured on or after")
    scope.add_argument("--ids", help="comma-separated item ids")
    parser.add_argument(
        "--dry-run", action="store_true", help="show what would be replayed"
    )
    args = parser.parse_args()

    cfg = Config.from_env()

    if args.all:
        where, params = f"status IN {LIVE} OR status = 'failed'", ()
    elif args.failed:
        where, params = "status = 'failed'", ()
    elif args.since:
        where, params = f"created_at >= ? AND (status IN {LIVE} OR status='failed')", (args.since,)
    else:
        ids = [int(i) for i in args.ids.split(",") if i.strip()]
        if not ids:
            print("no ids given")
            return 2
        where, params = f"id IN ({','.join('?' * len(ids))})", tuple(ids)

    con = sqlite3.connect(cfg.inbox_db, timeout=30, isolation_level=None)
    con.row_factory = sqlite3.Row
    try:
        rows = con.execute(
            f"SELECT id, lemma, status FROM items WHERE {where} ORDER BY id", params
        ).fetchall()
        if not rows:
            print("nothing matched")
            return 0

        print(f"{len(rows)} item(s) to replay:")
        for row in rows[:20]:
            print(f"  {row['id']:>5}  {row['status']:<10} {row['lemma']}")
        if len(rows) > 20:
            print(f"  ... and {len(rows) - 20} more")

        if args.dry_run:
            print("\ndry run, nothing changed")
            return 0

        con.execute(
            f"UPDATE items SET status='pending', anki_note_id=NULL, merge_target=NULL, "
            f"committed_at=NULL, error=NULL WHERE {where}",
            params,
        )
        print(f"\n{len(rows)} item(s) marked pending.")
        print("Start the app to project them:  docker compose start app")
        return 0
    finally:
        con.close()


if __name__ == "__main__":
    raise SystemExit(main())
