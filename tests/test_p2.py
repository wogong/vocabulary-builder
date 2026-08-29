"""P2: Telegram bot, sentence candidates, quiz, reminders, TTS attach.

Drives the real Bot against a fake Telegram and a real collection.

    uv run python tests/test_p2.py
"""

import asyncio
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
CHAT = 4242
STRANGER = 9999


def check(label, cond, detail=""):
    print(f"  [{PASS if cond else FAIL}] {label}{'' if cond else '  <- ' + str(detail)}")
    if not cond:
        failures.append(label)


class FakeTelegram:
    """Records what the bot would have sent."""

    def __init__(self):
        self.sent, self.edits, self.answers, self.voices = [], [], [], []
        self._next_id = 100

    async def send_message(self, chat_id, text, buttons=None, preview=False):
        self._next_id += 1
        self.sent.append({"chat_id": chat_id, "text": text, "buttons": buttons,
                          "message_id": self._next_id})
        return {"message_id": self._next_id}

    async def edit_message(self, chat_id, message_id, text, buttons=None):
        self.edits.append({"chat_id": chat_id, "message_id": message_id,
                           "text": text, "buttons": buttons})
        return {}

    async def answer_callback(self, callback_id, text=None):
        self.answers.append(text)
        return {}

    async def send_voice(self, chat_id, audio, filename, caption=""):
        self.voices.append({"filename": filename, "bytes": len(audio)})
        return {}

    # helpers
    @property
    def last(self):
        return self.sent[-1] if self.sent else None

    @property
    def last_text(self):
        return (self.edits[-1]["text"] if self.edits else self.last["text"])

    def buttons_flat(self, source=None):
        rows = (source or self.last)["buttons"] or []
        return [b for row in rows for b in row]


FIXTURE = [
    ("ubiquitous", "juːˈbɪkwɪtəs", "", "adj. 无处不在的", 8421, 9000, ""),
    ("proliferation", "ˌprəlɪfəˈreɪʃn", "", "n. 激增；扩散", 9903, 9800, ""),
    ("fundamentally", "ˌfʌndəˈmentəli", "", "adv. 从根本上", 4200, 4100, ""),
    ("alter", "ˈɔːltə", "", "v. 改变", 3500, 3400, ""),
    ("the", "ðə", "", "art. 那", 1, 1, ""),
    ("smartphones", "", "", "n. 智能手机（复数）", 0, 0, "0:smartphone"),
    ("smartphone", "ˈsmɑːtfəʊn", "", "n. 智能手机", 6000, 5900, ""),
    ("consume", "kənˈsjuːm", "", "v. 消耗；消费", 2900, 2800, ""),
]

SENTENCE = ("Smartphones are now ubiquitous, and the proliferation of apps has "
            "fundamentally altered how we consume information in Beijing.")


def build_ecdict(path):
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
    con.commit(); con.close()


def msg(text, chat_id=CHAT):
    return {"message": {"chat": {"id": chat_id}, "text": text}}


def cb(data, message_id, chat_id=CHAT, text=""):
    return {"callback_query": {"id": "cq1", "data": data,
                               "message": {"chat": {"id": chat_id},
                                           "message_id": message_id, "text": text}}}


def main():
    tmp = Path(tempfile.mkdtemp(prefix="vocab-p2-"))
    writer = tts = None
    try:
        from anki.collection import Collection

        from app.anki_writer import AnkiWriter
        from app.bot import Bot
        from app.config import Config
        from app.db import Inbox
        from app.dictionary import Dictionaries
        from app.notetype import build_notetype
        from app.translate import Translator, candidates
        from app.tts import Tts, media_name

        ecdict = tmp / "stardict.db"
        build_ecdict(ecdict)
        col_path = tmp / "anki" / "collection.anki2"
        col_path.parent.mkdir(parents=True)
        col = Collection(str(col_path)); build_notetype(col); col.decks.id("Vocab"); col.close()

        os.environ.update(
            ANKI_COLLECTION_PATH=str(col_path), ANKI_DECK="Vocab",
            ANKI_NOTETYPE="VocabContext", INBOX_DB=str(tmp / "app" / "inbox.db"),
            API_TOKENS="chrome:t", SYNC_USERNAME="", SYNC_PASSWORD="",
            ECDICT_PATH=str(ecdict), CEDICT_PATH="", MIN_FREQ_RANK="3000",
            TG_BOT_TOKEN="token", TG_WEBHOOK_SECRET="s3cret",
            TG_ALLOWED_CHAT_IDS=str(CHAT), TTS_ENABLED="false", LLM_ENABLED="false",
        )
        print("automatic webhook registration (§7.1)")
        run_ = asyncio.run
        from app.main import register_webhook

        class FakeApi:
            def __init__(self, current=""):
                self.current, self.registered = current, None
            async def call(self, method, **params):
                if method == "getWebhookInfo":
                    return {"url": self.current}
                raise AssertionError(method)
            async def set_webhook(self, url, secret):
                self.registered = (url, secret)

        class Cfg:
            tg_webhook_secret = "s3cret"
            tg_public_url = "https://api.example.com"
            webhook_url = "https://api.example.com/tg/s3cret"

        api = FakeApi()
        check("registers when nothing is set",
              run_(register_webhook(api, Cfg())) == Cfg.webhook_url
              and api.registered == (Cfg.webhook_url, "s3cret"), api.registered)

        api = FakeApi(current=Cfg.webhook_url)
        run_(register_webhook(api, Cfg()))
        check("skips when it already points here", api.registered is None)

        api = FakeApi(current="https://old.example.com/tg/s3cret")
        run_(register_webhook(api, Cfg()))
        check("takes over a stale registration", api.registered is not None)

        class NoUrl(Cfg):
            tg_public_url = ""
            webhook_url = ""
        api = FakeApi()
        check("no public url -> does nothing", run_(register_webhook(api, NoUrl())) == "")

        class Insecure(Cfg):
            tg_public_url = "http://api.example.com"
            webhook_url = "http://api.example.com/tg/s3cret"
        check("refuses plain http", run_(register_webhook(api, Insecure())) == "")

        class Boom:
            async def call(self, *a, **k):
                raise RuntimeError("telegram unreachable")
        check("a failed registration does not stop startup",
              run_(register_webhook(Boom(), Cfg())) == "")

        print("webhook (§7.1)")
        # Before the manual writer takes the collection lock: TestClient starts
        # its own writer.
        from fastapi.testclient import TestClient

        from app.main import app

        with TestClient(app) as client:
            update = {"message": {"chat": {"id": CHAT}, "text": "/start"}}
            check("wrong path secret -> 403",
                  client.post("/tg/wrong", json=update,
                              headers={"x-telegram-bot-api-secret-token": "s3cret"}
                              ).status_code == 403)
            check("missing header secret -> 403",
                  client.post("/tg/s3cret", json=update).status_code == 403)
            check("correct secret accepted",
                  client.post("/tg/s3cret", json=update,
                              headers={"x-telegram-bot-api-secret-token": "s3cret"}
                              ).status_code == 200)
            body = client.get("/v1/stats", headers={"Authorization": "Bearer t"}).json()
            check("stats reports the bot is live", body["bot"] is True, body)
            check("stats reports translation is off", body["translation"] is False, body)
            trans = client.post("/v1/translate", json={"text": SENTENCE},
                                headers={"Authorization": "Bearer t"}).json()
            check("/v1/translate returns candidates without an LLM",
                  len(trans["candidates"]) >= 3 and trans["translation"] is None, trans)

            print("review page api (§5.3)")
            auth = {"Authorization": "Bearer t"}
            made = client.post("/v1/items", headers=auth, json={
                "word": "blunder", "sentence": "A blunder I did not mean to save.",
                "word_offset": 2, "url": "https://example.com/a", "page_title": "A page",
            }).json()
            time.sleep(2)  # let the writer commit it before we ask for the note
            listed = client.get("/v1/items", headers=auth).json()["items"]
            check("listing returns the capture", listed and listed[0]["id"] == made["id"], listed)
            check("listing carries what the page renders",
                  listed[0]["sentence"] and listed[0]["url"] == "https://example.com/a"
                  and listed[0]["word_offset"] == 2, listed[0])
            check("listing needs a token",
                  client.get("/v1/items").status_code == 401)
            found = client.get("/v1/items", params={"q": "blund"}, headers=auth).json()["items"]
            check("search matches by word", [i["id"] for i in found] == [made["id"]], found)
            check("search that matches nothing returns nothing",
                  client.get("/v1/items", params={"q": "zzzz"},
                             headers=auth).json()["items"] == [])

            gone = client.delete(f"/v1/items/{made['id']}", headers=auth)
            check("delete succeeds", gone.status_code == 200, gone.text)
            check("delete removes the whole note (it had one context)",
                  gone.json()["removed"] == "note", gone.json())
            after = client.get("/v1/items", headers=auth).json()["items"]
            check("deleted item is retracted, not hidden",
                  after[0]["id"] == made["id"] and after[0]["status"] == "skipped"
                  and after[0]["anki_note_id"] is None, after[0])
            check("deleting it again is a 404, not a second retraction",
                  client.delete(f"/v1/items/{made['id']}", headers=auth).status_code
                  in (200, 404))
            check("deleting an unknown id is a 404",
                  client.delete("/v1/items/999999", headers=auth).status_code == 404)
            check("delete needs a token",
                  client.delete(f"/v1/items/{made['id']}").status_code == 401)

        cfg = Config.from_env()
        inbox = Inbox(cfg.inbox_db)
        dicts = Dictionaries(cfg.ecdict_path, cfg.cedict_path)
        writer = AnkiWriter(cfg, inbox); writer.start(); writer.wait_ready()
        tg = FakeTelegram()
        tts = Tts(cfg, inbox, writer)
        bot = Bot(cfg, inbox, dicts, writer, tg, Translator(cfg), tts)
        run = asyncio.run

        def drain():
            deadline = time.time() + 15
            while time.time() < deadline and writer.queue_depth:
                time.sleep(0.05)
            time.sleep(0.3)

        print("access control (§7.1)")
        run(bot.handle(msg("hello", chat_id=STRANGER)))
        check("message from an unlisted chat is ignored", not tg.sent)
        run(bot.handle(cb("w|add|nope", 1, chat_id=STRANGER)))
        check("callback from an unlisted chat is ignored", not tg.answers)

        print("routing (§7.2)")
        run(bot.handle(msg("ubiquitous")))
        check("single word -> lookup", "📖 ubiquitous" in tg.last["text"], tg.last["text"])
        check("shows the gloss", "无处不在" in tg.last["text"])
        check("shows frequency", "8421" in tg.last["text"])
        check("warns about missing context", "没有语境" in tg.last["text"])
        word_msg = tg.last

        run(bot.handle(msg(SENTENCE)))
        check("long text -> sentence flow", "可能的生词" in tg.last["text"], tg.last["text"])
        sentence_msg = tg.last

        print("candidate rules (§5.3)")
        offered = [b[0] for b in tg.buttons_flat(sentence_msg) if "|s|t|" in b[1] or b[1].startswith("s|t|")]
        joined = " ".join(offered)
        check("rare words offered", "ubiquitous" in joined and "proliferation" in joined, offered)
        check("common words filtered out (词频过滤)", " the " not in f" {joined} ", offered)
        check("mid-sentence proper noun skipped", "Beijing" not in joined, offered)
        check("candidates are lemmas", "smartphone" in joined and "smartphones" not in joined,
              offered)
        check("rarest first", offered[0].startswith("proliferation"), offered)
        check("callback_data stays under 64 bytes (R8)",
              all(len(b[1].encode()) <= 64 for b in tg.buttons_flat(sentence_msg)))

        print("selecting words (§7.4)")
        key = tg.buttons_flat(sentence_msg)[0][1].split("|")[2]
        run(bot.handle(cb(f"s|t|{key}|0", sentence_msg["message_id"], text=sentence_msg["text"])))
        check("toggle marks the word", "☑️" in str(tg.edits[-1]["buttons"]))
        check("counter updates", "(1)" in str(tg.edits[-1]["buttons"]))
        run(bot.handle(cb(f"s|t|{key}|1", sentence_msg["message_id"], text=sentence_msg["text"])))
        run(bot.handle(cb(f"s|t|{key}|1", sentence_msg["message_id"], text=sentence_msg["text"])))
        check("toggling twice deselects", "(1)" in str(tg.edits[-1]["buttons"]))
        run(bot.handle(cb(f"s|ok|{key}", sentence_msg["message_id"])))
        check("confirms with a summary", "已加入 1 个词" in tg.edits[-1]["text"], tg.edits[-1])
        drain()

        # The writer owns the collection (D2); read through it, never alongside.
        note = writer.ask(lambda c: _note_fields(c, "proliferation"))
        check("selected word became a card", note is not None)
        if note:
            check("whole sentence is its context", "proliferation" in note["Contexts"]
                  and "Smartphones" in note["Contexts"], note["Contexts"])
            check("gloss carried over", "激增" in note["Definition"], note["Definition"])

        print("word flow buttons (§7.3)")
        wkey = tg.buttons_flat(word_msg)[0][1].split("|")[2]
        run(bot.handle(cb(f"w|add|{wkey}", word_msg["message_id"])))
        check("adds without context", "已加入 ubiquitous" in tg.edits[-1]["text"], tg.edits[-1])
        drain()
        run(bot.handle(msg("ubiquitous")))
        check("second lookup shows it is in the deck", "已在牌组" in tg.last["text"], tg.last["text"])

        run(bot.handle(msg("alter")))
        akey = tg.buttons_flat()[0][1].split("|")[2]
        run(bot.handle(cb(f"w|known|{akey}", tg.last["message_id"])))
        check("marks known", inbox.is_known("alter"))
        run(bot.handle(msg(SENTENCE)))
        later = " ".join(b[0] for b in tg.buttons_flat() if b[1].startswith("s|t|"))
        check("known words drop out of candidates", "alter" not in later, later)

        print("quiz (§7.6, 抽查 mode)")
        run(bot.start_quiz(CHAT, 2))
        quiz = tg.last
        check("prompt is the masked sentence", "____" in quiz["text"], quiz["text"])
        check("answer is hidden", "proliferation" not in quiz["text"], quiz["text"])
        qkey = tg.buttons_flat(quiz)[0][1].split("|")[2]
        run(bot.handle(cb(f"q|show|{qkey}", quiz["message_id"])))
        check("reveal shows the word", "proliferation" in tg.edits[-1]["text"]
              or "ubiquitous" in tg.edits[-1]["text"], tg.edits[-1]["text"])
        check("offers four ratings", len(tg.buttons_flat(tg.edits[-1])) == 4)
        run(bot.handle(cb(f"q|rate|{qkey}|1", quiz["message_id"])))
        with sqlite3.connect(cfg.inbox_db) as con:
            rated = con.execute("SELECT rating FROM bot_reviews").fetchall()
        check("rating recorded in bot_reviews", rated == [(1,)], rated)

        check("scheduler untouched (D6)",
              writer.ask(lambda c: c.card_count() == len(c.find_cards("is:new"))),
              "a quiz rating leaked into Anki's scheduling")

        print("commands (§7.7)")
        run(bot.handle(msg("/today")))
        check("/today lists the words", "今天收集" in tg.last["text"], tg.last["text"])
        run(bot.handle(msg("/stats")))
        check("/stats reports the deck", "牌组笔记" in tg.last["text"], tg.last["text"])
        run(bot.handle(msg("/search proliferation")))
        check("/search shows contexts", "1 条语境" in tg.last["text"], tg.last["text"])
        run(bot.handle(msg("/known consume")))
        check("/known marks it", inbox.is_known("consume"))
        run(bot.handle(msg("/pause 3")))
        check("/pause records a date", inbox.setting("paused_until") is not None)
        run(bot.handle(msg("/nonsense")))
        check("unknown command explains itself", "不认识的命令" in tg.last["text"])

        print("undo (§7.7)")
        before = writer.ask(lambda c: c.note_count())
        run(bot.handle(msg("/undo")))
        drain()
        after = writer.ask(lambda c: c.note_count())
        check("undo removes the note it created", after == before - 1, (before, after))
        check("undo reports what happened", "已撤销" in tg.last["text"], tg.last["text"])

        print("reminders (§7.5)")
        inbox.set_setting("paused_until", "1970-01-01")
        tg.sent.clear()
        run(bot.reminder())
        check("reminder fires when there is something", tg.sent, "no reminder sent")
        if tg.sent:
            check("offers a quiz and a mute", len(tg.buttons_flat()) == 2)
            mute = [b for b in tg.buttons_flat() if b[1] == "r|mute"][0]
            run(bot.handle(cb(mute[1], tg.last["message_id"])))
            tg.sent.clear()
            run(bot.reminder())
            check("muted for the day stays silent", not tg.sent, tg.sent)

        print("tts (§4.2 Audio)")
        tts.speak = lambda text: _fake_audio()
        lemma = "ubiquitous"
        run_capture(inbox, dicts, writer, lemma)
        drain()
        tts._synthesise(lemma)
        note = writer.ask(lambda c: _note_fields(c, lemma))
        check("Audio field points at the media file",
              note and note["Audio"] == f"[sound:{media_name(lemma)}]",
              note and note["Audio"])
        check("media file really exists in the collection",
              writer.ask(lambda c: c.media.have(media_name(lemma))),
              "missing from the media folder")
    finally:
        if writer:
            writer.stop()
        shutil.rmtree(tmp, ignore_errors=True)

    print()
    if failures:
        print(f"{len(failures)} check(s) failed: {failures}")
        return 1
    print("P2: bot captures, quizzes, reminds; audio reaches the collection")
    return 0


def _note_fields(col, lemma):
    """Read a note's fields on the writer thread."""
    from anki.collection import SearchNode

    notetype = col.models.by_name("VocabContext")
    found = col.find_notes(col.build_search_string(
        SearchNode(dupe=SearchNode.Dupe(notetype_id=notetype["id"], first_field=lemma))))
    if not found:
        return None
    note = col.get_note(found[0])
    return {f: note[f] for f in ("Word", "Contexts", "Definition", "Audio", "Phonetic")}


async def _fake_audio():
    return b"\xff\xf3fake-mp3-payload"


def run_capture(inbox, dicts, writer, word):
    from app.capture import capture

    capture(inbox, dicts, writer, word=word, sentence=f"A {word} sentence.",
            source="test", word_offset=2)


if __name__ == "__main__":
    raise SystemExit(main())
