"""P1: lemmatisation, dictionaries, /v1/lookup, /v1/known, enrichment.

Uses a fixture ECDICT with the real schema rather than the 800 MB download.

    uv run python tests/test_p1.py
"""

import os
import shutil
import sqlite3
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


# (word, phonetic, definition, translation, frq, bnc, exchange)
FIXTURE = [
    ("ubiquitous", "juːˈbɪkwɪtəs", "adj. being present everywhere",
     "adj. 无处不在的\nadj. 普遍存在的", 8421, 9000, ""),
    ("applications", "", "", "n. 应用（application 的复数）", 0, 0, "0:application/1:s"),
    ("application", "ˌæplɪˈkeɪʃn", "n. a formal request", "n. 申请；应用程序", 780, 690, ""),
    ("rigorous", "ˈrɪɡərəs", "adj. extremely thorough", "adj. 严格的；严谨的", 5600, 5200, ""),
    ("the", "ðə", "art. definite article", "art. 这；那", 1, 1, ""),
    ("qualitative", "ˈkwɒlɪtətɪv", "adj. relating to quality",
     "adj. 定性的；性质上的", 7300, 7100, ""),
]


def build_ecdict(path: Path):
    con = sqlite3.connect(path)
    con.execute("""CREATE TABLE stardict (
        id INTEGER PRIMARY KEY AUTOINCREMENT NOT NULL UNIQUE,
        word VARCHAR(64) COLLATE NOCASE NOT NULL UNIQUE,
        sw VARCHAR(64) COLLATE NOCASE NOT NULL DEFAULT '',
        phonetic VARCHAR(64), definition TEXT, translation TEXT,
        pos VARCHAR(16), collins INTEGER DEFAULT 0, oxford INTEGER DEFAULT 0,
        tag VARCHAR(64), bnc INTEGER, frq INTEGER, exchange TEXT,
        detail TEXT, audio TEXT)""")
    con.executemany(
        "INSERT INTO stardict (word, phonetic, definition, translation, frq, bnc, exchange)"
        " VALUES (?,?,?,?,?,?,?)", FIXTURE)
    con.commit()
    con.close()


def build_cedict(path: Path):
    path.write_text(
        "# CC-CEDICT fixture\n"
        "無處不在 无处不在 [wu2 chu4 bu4 zai4] /to be everywhere/ubiquitous/\n"
        "詞彙 词汇 [ci2 hui4] /vocabulary/list of words/\n",
        encoding="utf-8",
    )


AUTH = {"Authorization": "Bearer secret-token"}


def main():
    tmp = Path(tempfile.mkdtemp(prefix="vocab-p1-"))
    try:
        from anki.collection import Collection

        from app.notetype import build_notetype

        ecdict_path = tmp / "stardict.db"
        cedict_path = tmp / "cedict.txt"
        build_ecdict(ecdict_path)
        build_cedict(cedict_path)

        col_path = tmp / "anki" / "collection.anki2"
        col_path.parent.mkdir(parents=True)
        col = Collection(str(col_path))
        build_notetype(col)
        col.decks.id("Vocab::Reading")
        col.close()

        os.environ.update(
            ANKI_COLLECTION_PATH=str(col_path), ANKI_DECK="Vocab::Reading",
            ANKI_NOTETYPE="VocabContext", INBOX_DB=str(tmp / "app" / "inbox.db"),
            API_TOKENS="chrome:secret-token", SYNC_USERNAME="", SYNC_PASSWORD="",
            ECDICT_PATH=str(ecdict_path), CEDICT_PATH=str(cedict_path),
            MIN_FREQ_RANK="3000",
        )

        print("dictionaries")
        from app.dictionary import Dictionaries

        dicts = Dictionaries(str(ecdict_path), str(cedict_path))
        entry = dicts.lookup("ubiquitous")
        check("ECDICT gloss", entry.definitions == ["adj. 无处不在的", "adj. 普遍存在的"], entry)
        check("phonetic gets slashes", entry.phonetic == "/juːˈbɪkwɪtəs/", entry.phonetic)
        check("COCA rank", entry.freq_rank == 8421, entry.freq_rank)
        check("case-insensitive", dicts.lookup("Ubiquitous") is not None)
        check("CJK routes to CC-CEDICT",
              dicts.lookup("无处不在").source == "cedict")
        check("traditional form too", dicts.lookup("無處不在") is not None)
        check("unknown word is None", dicts.lookup("zzzznotaword") is None)

        import httpx
        from app.dictionary import RemoteDictionary
        remote = RemoteDictionary(
            "https://dict.test",
            client=httpx.Client(transport=httpx.MockTransport(lambda request: httpx.Response(
                200, json=[{"phonetic": "ˌpɒlɪ", "meanings": [{
                    "partOfSpeech": "noun",
                    "definitions": [{"definition": "the use of multiple forms"}],
                }]}],
            )))
        )
        remote_entry = remote.lookup("polymorphism")
        check("remote fallback parses definitions", remote_entry.definitions == [
            "noun. the use of multiple forms"
        ], remote_entry)
        check("remote fallback parses phonetic", remote_entry.phonetic == "/ˌpɒlɪ/",
              remote_entry.phonetic)

        missing = Dictionaries(str(tmp / "nope.db"), "")
        check("missing dictionary is not fatal", missing.lookup("ubiquitous") is None)
        check("availability reported", missing.available == {"ecdict": False, "cedict": False})
        check("not ready for either language",
              not missing.ready_for("en") and not missing.ready_for("zh"))
        check("ready_for is per language",
              dicts.ready_for("en") and dicts.ready_for("zh"))

        print("lemmatisation")
        from app.lemma import lemmatize

        check("ECDICT inflection table wins",
              lemmatize("applications", dicts) == "application")
        check("headword left alone", lemmatize("rigorous", dicts) == "rigorous")
        check("case folded", lemmatize("Ubiquitous", dicts) == "ubiquitous")
        check("chinese untouched", lemmatize("无处不在", dicts) == "无处不在")
        check("no dictionary -> lemminflect", lemmatize("geese", None) == "goose")
        check("lemminflect keeps its own base form", lemmatize("better", None) == "better")

        print("GET /v1/lookup")
        from fastapi.testclient import TestClient

        from app.main import app

        with TestClient(app) as client:
            check("auth required", client.get("/v1/lookup?word=x").status_code == 401)

            body = client.get("/v1/lookup", params={"word": "ubiquitous"}, headers=AUTH).json()
            check("definitions returned", len(body["definitions"]) == 2, body)
            check("freq_rank returned", body["freq_rank"] == 8421, body)
            check("not flagged common", body["common"] is False, body)
            check("not in deck yet", body["in_deck"] is False, body)
            check("dictionary named", body["dictionary"] == "ecdict", body)
            check("dictionary reported ready", body["dictionary_ready"] is True, body)
            check("no LLM gloss in P1", body["context_definition"] is None, body)

            common = client.get("/v1/lookup", params={"word": "the"}, headers=AUTH).json()
            check("common word flagged (词频过滤)", common["common"] is True, common)

            inflected = client.get(
                "/v1/lookup", params={"word": "applications"}, headers=AUTH).json()
            check("lookup lemmatises", inflected["lemma"] == "application", inflected)
            check("gloss follows the lemma",
                  "申请" in " ".join(inflected["definitions"]), inflected)

            zh = client.get("/v1/lookup", params={"word": "无处不在"}, headers=AUTH).json()
            check("chinese lookup works", "to be everywhere" in zh["definitions"], zh)

            unknown = client.get(
                "/v1/lookup", params={"word": "zzzznotaword"}, headers=AUTH).json()
            check("unknown word degrades cleanly", unknown["definitions"] == [], unknown)
            # The card words these two cases differently, so they must be
            # distinguishable: a loaded dictionary that lacks the word still
            # reports ready.
            check("missing word != missing dictionary",
                  unknown["dictionary"] is None and unknown["dictionary_ready"] is True,
                  unknown)

            print("POST /v1/known")
            check("mark known", client.post(
                "/v1/known", json={"word": "rigorous"}, headers=AUTH).json()["known"] is True)
            after = client.get("/v1/lookup", params={"word": "rigorous"}, headers=AUTH).json()
            check("reflected in lookup", after["already_known"] is True, after)
            client.post("/v1/known", json={"word": "rigorous", "known": False}, headers=AUTH)
            after = client.get("/v1/lookup", params={"word": "rigorous"}, headers=AUTH).json()
            check("can be undone", after["already_known"] is False, after)
            check("known key is the lemma", client.post(
                "/v1/known", json={"word": "applications"},
                headers=AUTH).json()["lemma"] == "application")

            print("enrichment on capture")
            sentence = "Smartphones are now ubiquitous in daily life."
            posted = client.post("/v1/items", headers=AUTH, json={
                "word": "ubiquitous", "sentence": sentence,
                "word_offset": sentence.index("ubiquitous"),
                "url": "https://example.com", "page_title": "T",
            }).json()
            check("accepted", posted["dedup"] == "new", posted)

            second = "Applications are ubiquitous."
            client.post("/v1/items", headers=AUTH, json={
                "word": "Applications", "sentence": second,
                "word_offset": 0, "url": "https://example.com",
            })

            deadline = time.time() + 20
            while time.time() < deadline and client.get("/healthz").json()["queue_depth"]:
                time.sleep(0.1)
            time.sleep(0.4)

            now_in_deck = client.get(
                "/v1/lookup", params={"word": "ubiquitous"}, headers=AUTH).json()
            check("in_deck after capture", now_in_deck["in_deck"] is True, now_in_deck)
            check("context count", now_in_deck["existing_contexts"] == 1, now_in_deck)

        col = Collection(str(col_path))
        note = col.get_note(col.find_notes('note:VocabContext "Word:ubiquitous"')[0])
        check("Definition field filled", "无处不在" in note["Definition"], note["Definition"])
        check("definition lines become <br>", "<br>" in note["Definition"], note["Definition"])
        check("Phonetic field filled", note["Phonetic"] == "/juːˈbɪkwɪtəs/", note["Phonetic"])
        check("inflected capture files under its lemma",
              len(col.find_notes('note:VocabContext "Word:application"')) == 1)
        check("no note under the surface form",
              len(col.find_notes('note:VocabContext "Word:applications"')) == 0)
        col.close()

        print("hand edits survive re-capture")
        col = Collection(str(col_path))
        note = col.get_note(col.find_notes('note:VocabContext "Word:ubiquitous"')[0])
        note["Definition"] = "my own wording"
        col.update_note(note)
        col.close()
        with TestClient(app) as client:
            third = "The pattern is ubiquitous across teams."
            client.post("/v1/items", headers=AUTH, json={
                "word": "ubiquitous", "sentence": third,
                "word_offset": third.index("ubiquitous"), "url": "https://example.com",
            })
            deadline = time.time() + 20
            while time.time() < deadline and client.get("/healthz").json()["queue_depth"]:
                time.sleep(0.1)
            time.sleep(0.4)
        col = Collection(str(col_path))
        note = col.get_note(col.find_notes('note:VocabContext "Word:ubiquitous"')[0])
        check("edited definition not overwritten", note["Definition"] == "my own wording",
              note["Definition"])
        check("context still appended", note["Contexts"].count("<hr>") == 1, note["Contexts"])
        col.close()
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    print()
    if failures:
        print(f"{len(failures)} check(s) failed: {failures}")
        return 1
    print("P1: dictionaries, lemmatisation, lookup and enrichment work")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
