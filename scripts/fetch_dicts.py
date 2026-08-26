"""Download the offline dictionaries (spec §5.1, P1).

  ECDICT      ~206 MB zip -> ~800 MB stardict.db   (English: phonetics,
              Chinese glosses, COCA/BNC frequency, inflections)
  CC-CEDICT   ~4 MB gz                              (Chinese lookups, B1)

Both are optional — without them the server still captures words, it just
cannot annotate them. Neither is redistributed here; this fetches from source.

    uv run python scripts/fetch_dicts.py --dest data/dict
    uv run python scripts/fetch_dicts.py --dest data/dict --cedict-only

Mind where --dest points: 800 MB inside a synced folder (Dropbox, iCloud) is
a bad time. Somewhere outside it, with ECDICT_PATH set to match, is better.
"""

import argparse
import json
import shutil
import sys
import tempfile
import urllib.request
import zipfile
from pathlib import Path

ECDICT_RELEASES = "https://api.github.com/repos/skywind3000/ECDICT/releases/latest"
ECDICT_ASSET = "ecdict-sqlite-"
CEDICT_URL = "https://www.mdbg.net/chinese/export/cedict/cedict_1_0_ts_utf-8_mdbg.txt.gz"


def download(url: str, dest: Path, label: str) -> Path:
    print(f"downloading {label}\n  {url}")
    dest.parent.mkdir(parents=True, exist_ok=True)
    with urllib.request.urlopen(url) as response, open(dest, "wb") as out:
        total = int(response.headers.get("Content-Length") or 0)
        done = 0
        while chunk := response.read(1 << 20):
            out.write(chunk)
            done += len(chunk)
            if total:
                pct = done * 100 // total
                print(f"\r  {done >> 20} / {total >> 20} MiB ({pct}%)", end="", flush=True)
    print(f"\r  {dest.stat().st_size >> 20} MiB -> {dest}")
    return dest


def fetch_ecdict(dest_dir: Path) -> None:
    target = dest_dir / "stardict.db"
    if target.exists():
        print(f"ECDICT already at {target} ({target.stat().st_size >> 20} MiB), skipping")
        return

    with urllib.request.urlopen(ECDICT_RELEASES) as response:
        release = json.load(response)
    asset = next(
        (a for a in release["assets"] if a["name"].startswith(ECDICT_ASSET)), None
    )
    if asset is None:
        sys.exit(f"no {ECDICT_ASSET}* asset in ECDICT release {release.get('tag_name')}")

    with tempfile.TemporaryDirectory() as tmp:
        archive = download(
            asset["browser_download_url"],
            Path(tmp) / asset["name"],
            f"ECDICT {release['tag_name']} ({asset['size'] >> 20} MiB)",
        )
        print("extracting (this takes a while, the database is ~800 MiB)")
        with zipfile.ZipFile(archive) as zf:
            member = next(
                (n for n in zf.namelist() if n.endswith("stardict.db")), None
            )
            if member is None:
                sys.exit(f"no stardict.db inside {asset['name']}")
            with zf.open(member) as src, open(target, "wb") as out:
                shutil.copyfileobj(src, out, length=1 << 20)
    print(f"  -> {target} ({target.stat().st_size >> 20} MiB)")


def fetch_cedict(dest_dir: Path) -> None:
    target = dest_dir / "cedict.txt.gz"
    if target.exists():
        print(f"CC-CEDICT already at {target}, skipping")
        return
    download(CEDICT_URL, target, "CC-CEDICT")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--dest", default="data/dict", help="where to put the files")
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--ecdict-only", action="store_true")
    group.add_argument("--cedict-only", action="store_true")
    args = parser.parse_args()

    dest = Path(args.dest).resolve()
    dest.mkdir(parents=True, exist_ok=True)

    if not args.cedict_only:
        fetch_ecdict(dest)
    if not args.ecdict_only:
        fetch_cedict(dest)

    print("\nPoint the server at them:")
    print(f"  ECDICT_PATH={dest / 'stardict.db'}")
    print(f"  CEDICT_PATH={dest / 'cedict.txt.gz'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
