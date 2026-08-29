"""Telegram bot (spec §7).

Second capture entrance and a light review touchpoint. Everything it collects
goes through the same capture path as the browser extension, so a word caught
on the phone is annotated and deduplicated identically.

Quizzing here does NOT touch Anki's scheduler (D6): the bot records its own
ratings in bot_reviews, and real review progress stays with Anki clients.
"""

import logging
import secrets
import time

from .capture import annotate, capture, resolve_lang
from .lemma import lemmatize
from .retract import retract
from .translate import candidates

log = logging.getLogger("vocab.bot")

SESSION_TTL = 3600  # §7.4: candidate lists live server-side for an hour
MAX_WORDS_FOR_LOOKUP = 3
RATINGS = [("😰 忘了", 1), ("😐 勉强", 2), ("🙂 记得", 3), ("😎 简单", 4)]

HELP = """\
把词或句子直接发给我：

• 单个词 → 查释义，可一键加入生词本
• 一句话 → 挑出其中的生词让你选

/review [n]  抽查 n 个（默认 5）
/today       今天收集的词
/stats       统计
/search 词   查这个词在牌组里的所有语境
/known 词    标记为已认识
/undo        撤销最近一次加入
/pause [天]  暂停提醒
/sync        手动触发同步
"""


class Bot:
    def __init__(self, cfg, inbox, dictionaries, writer, telegram, translator, tts=None):
        self.cfg = cfg
        self.inbox = inbox
        self.dictionaries = dictionaries
        self.writer = writer
        self.tg = telegram
        self.translator = translator
        self.tts = tts
        self._sessions: dict[str, dict] = {}

    # -- session store (R8: callback_data is capped at 64 bytes) ---------

    def _store(self, data: dict) -> str:
        self._expire()
        key = secrets.token_hex(3)
        self._sessions[key] = {**data, "created": time.time()}
        return key

    def _load(self, key: str) -> dict | None:
        self._expire()
        return self._sessions.get(key)

    def _expire(self):
        cutoff = time.time() - SESSION_TTL
        for key in [k for k, v in self._sessions.items() if v["created"] < cutoff]:
            del self._sessions[key]

    # -- entry point -----------------------------------------------------

    def allowed(self, chat_id) -> bool:
        return chat_id in self.cfg.tg_allowed_chat_ids

    async def handle(self, update: dict):
        """Dispatch one webhook update. Never raises: Telegram retries on 5xx."""
        try:
            if "callback_query" in update:
                await self._on_callback(update["callback_query"])
            elif "message" in update:
                await self._on_message(update["message"])
        except Exception:
            log.exception("failed to handle update")

    async def _on_message(self, message: dict):
        chat_id = message.get("chat", {}).get("id")
        text = (message.get("text") or "").strip()
        if not self.allowed(chat_id):
            log.warning("ignoring message from chat %s (not allowlisted)", chat_id)
            return
        if not text:
            return
        if text.startswith("/"):
            await self._command(chat_id, text)
        elif len(text.split()) <= MAX_WORDS_FOR_LOOKUP:
            await self._word_flow(chat_id, text)
        else:
            await self._sentence_flow(chat_id, text)

    # -- word lookup (§7.3) ----------------------------------------------

    async def _word_flow(self, chat_id, word, sentence=None):
        lang = resolve_lang(word)
        lemma, entry = annotate(self.dictionaries, word, lang)
        contexts = self.inbox.contexts_for(lemma)
        known = self.inbox.is_known(lemma)

        lines = [f"📖 {lemma}" + (f"  {entry.phonetic}" if entry and entry.phonetic else "")]
        if entry and entry.definitions:
            lines += entry.definitions[:4]
        elif self.dictionaries and self.dictionaries.ready_for(lang):
            lines.append("词典里没有这个词")
        else:
            lines.append("未安装词典")
        if entry and entry.freq_rank:
            lines.append(f"词频排名 {entry.freq_rank}")
        if known:
            lines.append("✓ 已标为认识")
        if contexts:
            lines.append(f"\n📌 已在牌组 · {len(contexts)} 条语境")
            for row in contexts[-3:]:
                lines.append(f"· {row['sentence']}")
        elif not sentence:
            lines.append("\n⚠️ 没有语境，这样的卡片质量较低 —— 建议连同句子一起发过来")

        key = self._store({"kind": "word", "word": word, "lemma": lemma, "sentence": sentence})
        buttons = [[
            ("➕ 加入生词本" if not contexts else "➕ 追加语境", f"w|add|{key}"),
        ]]
        if self.cfg.tts_enabled and self.tts is not None and lang == "en":
            buttons[0].append(("🔊 发音", f"w|say|{key}"))
        if not known:
            buttons[0].append(("✓ 已认识", f"w|known|{key}"))
        await self.tg.send_message(chat_id, "\n".join(lines), buttons)

    # -- sentence flow (§7.4) --------------------------------------------

    async def _sentence_flow(self, chat_id, text):
        found = candidates(text, self.dictionaries, self.inbox, self.cfg.min_freq_rank)
        translation = await self.translator.translate(text)

        lines = []
        if translation:
            lines.append(f"🇨🇳 {translation}")
        elif self.translator.enabled:
            lines.append("（翻译失败）")
        if not found:
            lines.append("\n没找到值得收的生词。")
            await self.tg.send_message(chat_id, "\n".join(lines) or "没找到值得收的生词。")
            return

        key = self._store(
            {"kind": "sent", "text": text, "candidates": found, "selected": set()}
        )
        lines.append("\n可能的生词（点击选择）：")
        await self.tg.send_message(
            chat_id, "\n".join(lines), self._candidate_buttons(key)
        )

    def _candidate_buttons(self, key):
        session = self._sessions[key]
        rows = []
        for index, candidate in enumerate(session["candidates"]):
            mark = "☑️ " if index in session["selected"] else ""
            brief = f" {candidate['brief']}" if candidate["brief"] else ""
            rows.append([(f"{mark}{candidate['word']}{brief}", f"s|t|{key}|{index}")])
        count = len(session["selected"])
        rows.append([(f"✅ 加入选中 ({count})", f"s|ok|{key}"), ("❌ 取消", f"s|no|{key}")])
        return rows

    # -- callbacks --------------------------------------------------------

    async def _on_callback(self, query: dict):
        chat_id = query.get("message", {}).get("chat", {}).get("id")
        message_id = query.get("message", {}).get("message_id")
        if not self.allowed(chat_id):
            return
        parts = (query.get("data") or "").split("|")
        kind = parts[0] if parts else ""
        handler = {
            "w": self._callback_word,
            "s": self._callback_sentence,
            "q": self._callback_quiz,
            "r": self._callback_reminder,
        }.get(kind)
        if handler is None:
            await self.tg.answer_callback(query["id"])
            return
        await handler(query, chat_id, message_id, parts)

    async def _callback_word(self, query, chat_id, message_id, parts):
        _, action, key = parts[0], parts[1], parts[2]
        session = self._load(key)
        if session is None:
            await self.tg.answer_callback(query["id"], "这条已经过期了，重新发一次吧")
            return

        if action == "known":
            self.inbox.mark_known(session["lemma"])
            await self.tg.answer_callback(query["id"], "已标记为认识")
            await self.tg.edit_message(chat_id, message_id, f"✓ {session['lemma']} 已标为认识")
            return

        if action == "say":
            await self.tg.answer_callback(query["id"])
            audio = await self.tts.speak(session["lemma"]) if self.tts else None
            if audio:
                await self.tg.send_voice(chat_id, audio, f"{session['lemma']}.mp3",
                                         session["lemma"])
            else:
                await self.tg.send_message(chat_id, "发音生成失败")
            return

        if action == "add":
            sentence = session.get("sentence") or session["lemma"]
            result = capture(
                self.inbox, self.dictionaries, self.writer,
                word=session["word"], sentence=sentence, source="telegram",
                client_id=str(chat_id),
                word_offset=sentence.lower().find(session["word"].lower())
                if session.get("sentence") else None,
            )
            await self.tg.answer_callback(query["id"], "已加入")
            note = {
                "new": f"✅ 已加入 {result['lemma']}",
                "merged": f"✅ {result['lemma']} 已追加语境"
                          f"（原有 {result['existing_contexts']} 条）",
                "duplicate": f"• {result['lemma']} 这条语境已经收过了",
            }[result["dedup"]]
            await self.tg.edit_message(chat_id, message_id, note)

    async def _callback_sentence(self, query, chat_id, message_id, parts):
        action, key = parts[1], parts[2]
        session = self._load(key)
        if session is None:
            await self.tg.answer_callback(query["id"], "这条已经过期了，重新发一次吧")
            return

        if action == "t":
            index = int(parts[3])
            selected = session["selected"]
            selected.symmetric_difference_update({index})
            await self.tg.answer_callback(query["id"])
            await self.tg.edit_message(
                chat_id, message_id, query["message"]["text"], self._candidate_buttons(key)
            )
            return

        if action == "no":
            await self.tg.answer_callback(query["id"])
            await self.tg.edit_message(chat_id, message_id, "已取消")
            self._sessions.pop(key, None)
            return

        if action == "ok":
            chosen = [session["candidates"][i] for i in sorted(session["selected"])]
            if not chosen:
                await self.tg.answer_callback(query["id"], "还没有选词")
                return
            added = []
            for candidate in chosen:
                # The whole sentence is the shared context for every word
                # picked out of it (§7.4).
                result = capture(
                    self.inbox, self.dictionaries, self.writer,
                    word=candidate["raw"], sentence=session["text"], source="telegram",
                    client_id=str(chat_id), word_offset=candidate["offset"],
                )
                added.append(result["lemma"])
            await self.tg.answer_callback(query["id"], f"已加入 {len(added)} 个")
            await self.tg.edit_message(
                chat_id, message_id, f"✅ 已加入 {len(added)} 个词：{', '.join(added)}"
            )
            self._sessions.pop(key, None)

    # -- quiz (§7.6) ------------------------------------------------------

    async def start_quiz(self, chat_id, count=5):
        items = self.inbox.quiz_items(count)
        if not items:
            await self.tg.send_message(chat_id, "还没有可以抽查的词。")
            return
        key = self._store({"kind": "quiz", "items": items, "at": 0})
        await self._quiz_prompt(chat_id, key)

    async def _quiz_prompt(self, chat_id, key, message_id=None):
        session = self._sessions[key]
        item = session["items"][session["at"]]
        text = (
            f"🎲 抽查 {session['at'] + 1}/{len(session['items'])}\n\n"
            f"{item['sentence_masked']}"
        )
        buttons = [[("🤔 显示答案", f"q|show|{key}"), ("⏭ 跳过", f"q|skip|{key}")]]
        if message_id:
            await self.tg.edit_message(chat_id, message_id, text, buttons)
        else:
            await self.tg.send_message(chat_id, text, buttons)

    async def _callback_quiz(self, query, chat_id, message_id, parts):
        action, key = parts[1], parts[2]
        session = self._load(key)
        if session is None:
            await self.tg.answer_callback(query["id"], "这轮抽查已经过期了")
            return
        item = session["items"][session["at"]]

        if action == "show":
            await self.tg.answer_callback(query["id"])
            lines = [
                f"🎲 抽查 {session['at'] + 1}/{len(session['items'])}",
                "",
                item["sentence"],
                "",
                f"{item['lemma']}" + (f"  {item['phonetic']}" if item["phonetic"] else ""),
            ]
            if item["definition"]:
                lines += item["definition"].split("\n")[:3]
            await self.tg.edit_message(
                chat_id, message_id, "\n".join(lines),
                [[(label, f"q|rate|{key}|{value}") for label, value in RATINGS]],
            )
            return

        if action in ("rate", "skip"):
            if action == "rate":
                self.inbox.add_review(item["id"], chat_id, int(parts[3]))
            await self.tg.answer_callback(query["id"])
            session["at"] += 1
            if session["at"] >= len(session["items"]):
                await self.tg.edit_message(
                    chat_id, message_id, f"✅ 抽查结束，共 {len(session['items'])} 个词"
                )
                self._sessions.pop(key, None)
                return
            await self._quiz_prompt(chat_id, key, message_id)

    # -- reminders (§7.5) -------------------------------------------------

    async def _callback_reminder(self, query, chat_id, message_id, parts):
        action = parts[1]
        if action == "quiz":
            await self.tg.answer_callback(query["id"])
            await self.start_quiz(chat_id, int(parts[2]))
        elif action == "mute":
            self.inbox.set_setting("mute_date", _today())
            await self.tg.answer_callback(query["id"], "今天不再打扰")
            await self.tg.edit_message(chat_id, message_id, "😴 今天不再提醒")

    async def reminder(self):
        """Fired on a schedule. Stays quiet unless there is something to say (§7.5)."""
        if not self.cfg.tg_allowed_chat_ids:
            return
        if self.inbox.setting("mute_date") == _today():
            return
        paused = self.inbox.setting("paused_until")
        if paused and paused >= _today():
            return

        today = self.inbox.today_count()
        try:
            due = self.writer.ask(
                lambda col: len(col.find_cards(f'deck:"{self.cfg.deck}" is:due')), timeout=20
            )
        except Exception as exc:
            log.warning("could not read due count: %s", exc)
            due = 0
        if not today and not due:
            return  # nothing worth a notification

        lines = []
        if due:
            lines.append(f"🔔 今日待复习 {due} 张")
        if today:
            words = [row["lemma"] for row in self.inbox.words_since("-1 days")]
            lines.append(f"\n今天新收集了 {today} 个词：")
            lines.append("、".join(words[:12]) + ("…" if len(words) > 12 else ""))

        buttons = [[("🎲 在这里抽查 5 个", "r|quiz|5"), ("😴 今天算了", "r|mute")]]
        for chat_id in self.cfg.tg_allowed_chat_ids:
            await self.tg.send_message(chat_id, "\n".join(lines), buttons)

    # -- commands (§7.7) ---------------------------------------------------

    async def _command(self, chat_id, text):
        command, _, argument = text.partition(" ")
        command = command.split("@")[0].lstrip("/").lower()
        argument = argument.strip()

        if command in ("start", "help"):
            await self.tg.send_message(chat_id, HELP)

        elif command == "review":
            await self.start_quiz(chat_id, _int(argument, 5))

        elif command == "today":
            rows = self.inbox.words_since("-1 days")
            if not rows:
                await self.tg.send_message(chat_id, "今天还没有收集。")
                return
            lines = [f"今天收集了 {len(rows)} 个词：", ""]
            for row in rows:
                brief = (row["definition"] or "").split("\n")[0][:28]
                lines.append(f"· {row['lemma']}  {brief}")
            await self.tg.send_message(chat_id, "\n".join(lines))

        elif command == "stats":
            stats = self.inbox.stats()
            try:
                total = self.writer.ask(
                    lambda col: col.note_count(), timeout=20)
            except Exception:
                total = "?"
            await self.tg.send_message(
                chat_id,
                f"📊 今日采集 {stats['today']}\n"
                f"不同词数 {stats['distinct_words']}\n"
                f"牌组笔记 {total}\n"
                f"待处理 {self.writer.queue_depth}\n"
                f"上次同步 {self.writer.status['last_sync_at'] or '从未'}",
            )

        elif command == "search":
            if not argument:
                await self.tg.send_message(chat_id, "用法：/search <word>")
                return
            lemma = lemmatize(argument, self.dictionaries)
            rows = self.inbox.contexts_for(lemma)
            if not rows:
                await self.tg.send_message(chat_id, f"{lemma} 不在牌组里。")
                return
            lines = [f"📌 {lemma} · {len(rows)} 条语境", ""]
            for row in rows:
                lines.append(f"· {row['sentence']}")
                if row["page_title"]:
                    lines.append(f"  — {row['page_title']}")
            await self.tg.send_message(chat_id, "\n".join(lines))

        elif command == "known":
            if not argument:
                await self.tg.send_message(chat_id, "用法：/known <word>")
                return
            lemma = lemmatize(argument, self.dictionaries)
            self.inbox.mark_known(lemma)
            await self.tg.send_message(chat_id, f"✓ {lemma} 已标为认识")

        elif command == "undo":
            await self._undo(chat_id)

        elif command == "pause":
            days = _int(argument, 1)
            until = _today(days)
            self.inbox.set_setting("paused_until", until)
            await self.tg.send_message(chat_id, f"⏸ 提醒已暂停到 {until}")

        elif command == "sync":
            self.writer.request_sync()
            await self.tg.send_message(chat_id, "已请求同步")

        else:
            await self.tg.send_message(chat_id, f"不认识的命令：/{command}\n\n{HELP}")

    async def _undo(self, chat_id):
        item = self.inbox.last_committed()
        if item is None:
            await self.tg.send_message(chat_id, "没有可撤销的记录。")
            return
        try:
            removed = self.writer.ask(
                lambda col: retract(col, item), timeout=30
            )
            self.writer.mark_dirty()
        except Exception as exc:
            log.exception("undo failed")
            await self.tg.send_message(chat_id, f"撤销失败：{exc}")
            return
        self.inbox.mark_retracted(item["id"])
        await self.tg.send_message(
            chat_id,
            f"↩️ 已撤销 {item['lemma']}"
            + ("（整张卡已删除）" if removed == "note" else "（该条语境已移除）"),
        )


def _int(raw: str, default: int) -> int:
    try:
        return max(1, min(50, int(raw)))
    except (TypeError, ValueError):
        return default


def _today(offset_days: int = 0) -> str:
    from datetime import date, timedelta

    return (date.today() + timedelta(days=offset_days)).isoformat()
