"""Three-layer backup (spec §8.2).

  L1  inbox.db            hourly, keep 48   — the source of truth (D1)
  L2  collection + media  daily,  keep 14   — the live Anki collection
  L3  .apkg export        weekly, keep 8    — importable anywhere, last resort

Both database layers use SQLite's online backup API, so nothing has to stop and
the copy is always consistent. L3 exports from the L2 copy, so it never touches
the collection the writer holds open.

    docker compose exec -T app python scripts/backup.py l1
    docker compose exec -T app python scripts/backup.py l2 l3
"""

import os
import shutil
import sqlite3
import sys
import tempfile
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config import Config  # noqa: E402

KEEP = {"l1": 48, "l2": 14, "l3": 8}


def stamp() -> str:
    return datetime.now().strftime("%Y%m%d-%H%M%S")


def sqlite_backup(src: Path, dest: Path) -> None:
    """Consistent copy of a live SQLite database."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    source = sqlite3.connect(f"file:{src}?mode=ro", uri=True)
    target = sqlite3.connect(dest)
    try:
        source.backup(target)
    finally:
        target.close()
        source.close()


def prune(directory: Path, pattern: str, keep: int) -> None:
    """Drop all but the newest `keep` — names are timestamped, so sort order is age."""
    for old in sorted(directory.glob(pattern))[:-keep]:
        shutil.rmtree(old) if old.is_dir() else old.unlink()
        print(f"  pruned {old.name}")


def l1(cfg: Config, root: Path) -> None:
    out = root / "l1" / f"inbox-{stamp()}.db"
    sqlite_backup(Path(cfg.inbox_db), out)
    print(f"L1 {out} ({out.stat().st_size // 1024} KiB)")
    prune(out.parent, "inbox-*.db", KEEP["l1"])


def l2(cfg: Config, root: Path) -> Path:
    tag = stamp()
    out = root / "l2" / f"collection-{tag}.anki2"
    sqlite_backup(Path(cfg.collection_path), out)
    print(f"L2 {out} ({out.stat().st_size // 1024} KiB)")

    media = Path(cfg.collection_path).parent / "collection.media"
    if media.is_dir():
        media_out = root / "l2" / f"media-{tag}"
        shutil.copytree(media, media_out, dirs_exist_ok=True)
        print(f"L2 {media_out} ({sum(1 for _ in media_out.rglob('*'))} files)")
        prune(out.parent, "media-*", KEEP["l2"])
    prune(out.parent, "collection-*.anki2", KEEP["l2"])
    return out


def l3(cfg: Config, root: Path, source: Path | None = None) -> None:
    """Export an .apkg from a copy, never from the live collection."""
    from anki.collection import Collection, ExportAnkiPackageOptions

    if source is None:
        source = l2(cfg, root)
    out = root / "l3" / f"vocab-{stamp()}.apkg"
    out.parent.mkdir(parents=True, exist_ok=True)

    with tempfile.TemporaryDirectory() as tmp:
        work = Path(tmp) / "collection.anki2"
        shutil.copy(source, work)
        media = Path(cfg.collection_path).parent / "collection.media"
        if media.is_dir():
            shutil.copytree(media, Path(tmp) / "collection.media")
        col = Collection(str(work))
        try:
            col.export_anki_package(
                out_path=str(out),
                options=ExportAnkiPackageOptions(
                    with_scheduling=True, with_media=True, legacy=False
                ),
                limit=None,
            )
        finally:
            col.close()
    print(f"L3 {out} ({out.stat().st_size // 1024} KiB)")
    prune(out.parent, "vocab-*.apkg", KEEP["l3"])


def main() -> int:
    stages = [a.lower() for a in sys.argv[1:]] or ["l1", "l2", "l3"]
    unknown = set(stages) - {"l1", "l2", "l3"}
    if unknown:
        print(f"unknown stage(s): {sorted(unknown)}; expected l1 / l2 / l3")
        return 2

    cfg = Config.from_env()
    root = Path(os.environ.get("BACKUP_DIR", "/data/backup"))
    root.mkdir(parents=True, exist_ok=True)

    l2_copy = None
    if "l1" in stages:
        l1(cfg, root)
    if "l2" in stages:
        l2_copy = l2(cfg, root)
    if "l3" in stages:
        l3(cfg, root, l2_copy)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
