"""Both first-run paths of scripts/bootstrap_collection.py (spec §8.4).

    uv run python tests/test_bootstrap.py
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


def run(env, *args):
    return subprocess.run(
        [sys.executable, "scripts/bootstrap_collection.py", *args],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
    )


def main():
    tmp = Path(tempfile.mkdtemp(prefix="vocab-boot-"))
    port = free_port()
    endpoint = f"http://127.0.0.1:{port}/"
    server = None
    try:
        from anki.collection import Collection

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
        deadline = time.time() + 15
        while time.time() < deadline:
            try:
                with socket.create_connection(("127.0.0.1", port), timeout=0.2):
                    break
            except OSError:
                time.sleep(0.1)

        def env_for(name):
            return {
                **os.environ,
                "ANKI_COLLECTION_PATH": str(tmp / name / "collection.anki2"),
                "ANKI_DECK": "Vocab::Reading",
                "ANKI_NOTETYPE": "VocabContext",
                "INBOX_DB": str(tmp / name / "inbox.db"),
                "API_TOKENS": "chrome:t",
                "SYNC_ENDPOINT": endpoint,
                "SYNC_USERNAME": "vocab",
                "SYNC_PASSWORD": "secret",
            }

        print("empty server, no flag: explains itself instead of a bogus error")
        env = env_for("server")
        (tmp / "server").mkdir()
        r = run(env, )
        check("exits non-zero", r.returncode == 1, r.stdout)
        check(
            "says the server is empty, not 'download unusable'",
            "has nothing to download" in r.stdout,
            r.stdout,
        )
        check("points at --seed-local", "--seed-local" in r.stdout, r.stdout)

        print("--seed-local on a fresh collection")
        r = run(env, "--seed-local")
        check("succeeds", r.returncode == 0, r.stdout + r.stderr)
        check("created the note type", "created note type" in r.stdout, r.stdout)
        check("uploaded a baseline", "uploading this collection" in r.stdout or "pushed to the sync server" in r.stdout, r.stdout)
        check("verified afterwards", "note type 'VocabContext': ok" in r.stdout, r.stdout)

        print("re-running is a no-op")
        r = run(env, "--seed-local")
        check("detects it is already done", "already present" in r.stdout, r.stdout)
        check("still exits zero", r.returncode == 0, r.stdout)

        print("a second device downloads what was seeded")
        (tmp / "phone").mkdir()
        r = run(env_for("phone"))
        check("plain download succeeds", r.returncode == 0, r.stdout + r.stderr)
        phone = Collection(str(tmp / "phone" / "collection.anki2"))
        check("note type arrived", phone.models.by_name("VocabContext") is not None)
        check("deck arrived", phone.decks.id_for_name("Vocab::Reading") is not None)
        phone.close()

        print("--seed-local refuses to overwrite a populated server")
        (tmp / "other").mkdir()
        r = run(env_for("other"), "--seed-local")
        check("refuses", r.returncode == 1, r.stdout)
        check(
            "explains why",
            "already holds a collection" in r.stdout and "--seed-local" in r.stdout,
            r.stdout,
        )

        print("config guard: credentials without an endpoint")
        bad = env_for("guard")
        bad["SYNC_ENDPOINT"] = ""
        (tmp / "guard").mkdir()
        r = run(bad, "--seed-local")
        check("refuses to fall back to AnkiWeb (D7)", r.returncode != 0, r.stdout)
        check("names the cause", "AnkiWeb" in (r.stdout + r.stderr), r.stdout + r.stderr)
    finally:
        if server:
            server.terminate()
            server.wait(timeout=10)
        shutil.rmtree(tmp, ignore_errors=True)

    print()
    if failures:
        print(f"{len(failures)} check(s) failed: {failures}")
        return 1
    print("bootstrap: both first-run paths work")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
