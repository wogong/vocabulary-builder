"""Zip the extension for the Chrome Web Store (or for loading on another machine).

    python scripts/pack_extension.py

Writes dist/vocabulary-builder-<version>.zip with manifest.json at the zip root,
which is the layout the Web Store upload expects.
"""

import json
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
EXT = ROOT / "extension"


def run() -> int:
    version = json.loads((EXT / "manifest.json").read_text())["version"]
    dist = ROOT / "dist"
    dist.mkdir(exist_ok=True)
    out = dist / f"vocabulary-builder-{version}.zip"

    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as zf:
        for path in sorted(EXT.rglob("*")):
            if path.is_file():
                zf.write(path, path.relative_to(EXT))
                print(f"  {path.relative_to(EXT)}")

    print(f"\n{out.relative_to(ROOT)} ({out.stat().st_size // 1024} KB)")
    return 0


if __name__ == "__main__":
    raise SystemExit(run())
