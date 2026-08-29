"""HTTP entry point (spec §5.3). P0 + P1.

Run with a single worker — the collection has exactly one writer (D2):
    uvicorn app.main:app --host 0.0.0.0 --port 8000 --workers 1
"""

import logging
import os
from contextlib import asynccontextmanager

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request
from fastapi.responses import JSONResponse

from .anki_writer import AnkiWriter
from .bot import Bot
from .capture import annotate, capture, resolve_lang
from .config import Config
from .db import Inbox
from .dictionary import Dictionaries
from .lemma import lemmatize
from .models import ItemAccepted, ItemIn, KnownIn, LookupOut, TranslateIn, TranslateOut
from .telegram import Telegram
from .translate import Translator, candidates
from .tts import Tts

logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
log = logging.getLogger("vocab.api")


@asynccontextmanager
async def lifespan(app: FastAPI):
    cfg = Config.from_env()
    inbox = Inbox(cfg.inbox_db)
    dictionaries = Dictionaries(cfg.ecdict_path, cfg.cedict_path)
    writer = AnkiWriter(cfg, inbox)
    writer.start()
    writer.wait_ready()  # raises -> uvicorn exits, which is what we want (D3)

    tts = Tts(cfg, inbox, writer)
    if cfg.tts_enabled:
        tts.start()

    app.state.cfg = cfg
    app.state.inbox = inbox
    app.state.dictionaries = dictionaries
    app.state.writer = writer
    app.state.tts = tts
    app.state.translator = Translator(cfg)
    app.state.bot = None
    app.state.telegram = None
    scheduler = None

    if cfg.bot_enabled:
        telegram = Telegram(cfg.tg_bot_token)
        app.state.telegram = telegram
        app.state.bot = Bot(cfg, inbox, dictionaries, writer, telegram,
                            app.state.translator, tts)
        await register_webhook(telegram, cfg)
        if cfg.tg_allowed_chat_ids and cfg.tg_reminder_times:
            scheduler = AsyncIOScheduler(timezone=cfg.tg_timezone)
            for slot in cfg.tg_reminder_times:
                hour, minute = slot.split(":")
                if _is_quiet(int(hour), cfg.tg_quiet_hours):
                    log.warning("reminder at %s falls in quiet hours, skipping", slot)
                    continue
                scheduler.add_job(
                    app.state.bot.reminder, CronTrigger(hour=int(hour), minute=int(minute))
                )
            scheduler.start()
            log.info("reminders scheduled at %s (%s)", cfg.tg_reminder_times, cfg.tg_timezone)
    else:
        log.info("telegram bot disabled (TG_BOT_TOKEN / TG_WEBHOOK_SECRET unset)")

    if not cfg.tokens:
        log.warning("API_TOKENS is empty — every request will be rejected")
    if not cfg.sync_enabled:
        log.warning("SYNC_USERNAME/PASSWORD unset — collection will not sync")
    try:
        yield
    finally:
        if scheduler:
            scheduler.shutdown(wait=False)
        if app.state.telegram:
            await app.state.telegram.aclose()
        if cfg.tts_enabled:
            tts.stop()
        writer.stop()


def redact(url: str, secret: str) -> str:
    """Webhook URLs carry the secret in the path; keep it out of the logs."""
    return url.replace(secret, "***") if secret else url


async def register_webhook(telegram, cfg) -> str:
    """Point Telegram at us on startup, if a public URL is configured (§7.1).

    Telegram keeps exactly one webhook per bot token, so a second deployment
    sharing the token silently steals delivery from the first. That is worth a
    loud log line rather than a silent takeover.
    """
    desired = cfg.webhook_url
    if not desired:
        log.info(
            "TG_PUBLIC_URL unset — register the webhook manually with "
            "scripts/set_webhook.py, or set it and restart"
        )
        return ""
    if not desired.startswith("https://"):
        log.error("TG_PUBLIC_URL must be https:// — Telegram will not deliver to %s",
                  cfg.tg_public_url)
        return ""
    try:
        info = await telegram.call("getWebhookInfo")
        current = info.get("url") or ""
        if current == desired:
            log.info("telegram webhook already points here (%s)",
                     redact(desired, cfg.tg_webhook_secret))
            return desired
        if current:
            log.warning(
                "telegram webhook was pointing at %s — taking it over. If another "
                "deployment shares this bot token, it will stop receiving messages.",
                redact(current, cfg.tg_webhook_secret),
            )
        await telegram.set_webhook(desired, cfg.tg_webhook_secret)
        log.info("telegram webhook registered -> %s", redact(desired, cfg.tg_webhook_secret))
        return desired
    except Exception as exc:
        # Not fatal: the bot still answers whatever Telegram already delivers.
        log.warning("could not register the telegram webhook: %s", exc)
        return ""


def _is_quiet(hour: int, window: tuple[int, int]) -> bool:
    start, end = window
    return hour >= start or hour < end if start > end else start <= hour < end


app = FastAPI(title="Vocabulary Builder", version="0.2.0", lifespan=lifespan)


def require_token(request: Request, authorization: str = Header(default="")) -> str:
    scheme, _, token = authorization.partition(" ")
    client = request.app.state.cfg.tokens.get(token) if scheme.lower() == "bearer" else None
    if not client:
        raise HTTPException(status_code=401, detail="invalid or missing bearer token")
    return client


@app.post("/v1/items", status_code=202, response_model=ItemAccepted)
def create_item(payload: ItemIn, request: Request, client: str = Depends(require_token)):
    """Accept a captured word. Returns as soon as it is durably in the inbox (D1)."""
    inbox: Inbox = request.app.state.inbox
    writer: AnkiWriter = request.app.state.writer

    if payload.idem_key:
        prior = inbox.by_idem_key(payload.idem_key)
        if prior:
            return JSONResponse(
                status_code=409,
                content={
                    "id": prior["id"],
                    "status": prior["status"],
                    "dedup": "duplicate",
                    "lemma": prior["lemma"],
                    "detail": "idem_key already accepted",
                },
            )

    result = capture(
        inbox,
        request.app.state.dictionaries,
        writer,
        word=payload.word,
        sentence=payload.sentence,
        lang=payload.lang,
        source=payload.source,
        client_id=payload.client_id or client,
        idem_key=payload.idem_key,
        word_offset=payload.word_offset,
        context_before=payload.context_before,
        context_after=payload.context_after,
        url=payload.url,
        page_title=payload.page_title,
        client_ts=payload.client_ts,
        raw_payload=payload.model_dump(),
    )
    if result["status"] == "pending":
        request.app.state.tts.submit(result["lemma"], payload.lang)

    return ItemAccepted(
        id=result["id"],
        status="queued" if result["status"] == "pending" else result["status"],
        dedup=result["dedup"],
        lemma=result["lemma"],
        existing_contexts=result["existing_contexts"],
    )


@app.get("/v1/lookup", response_model=LookupOut)
def lookup(
    request: Request,
    word: str = Query(min_length=1, max_length=100),
    lang: str = "en",
    client: str = Depends(require_token),
):
    """Look a word up without capturing it (§5.3). Drives the extension card."""
    inbox: Inbox = request.app.state.inbox
    cfg: Config = request.app.state.cfg

    word = word.strip()
    lang = resolve_lang(word, lang)
    lemma, entry = annotate(request.app.state.dictionaries, word, lang)
    contexts = inbox.context_count(lemma, lang)

    return LookupOut(
        word=word,
        lemma=lemma,
        phonetic=entry.phonetic if entry else None,
        definitions=entry.definitions if entry else [],
        context_definition=None,  # P3, once the LLM lands
        freq_rank=entry.freq_rank if entry else None,
        common=bool(
            entry and entry.freq_rank and entry.freq_rank <= cfg.min_freq_rank
        ),
        already_known=inbox.is_known(lemma),
        in_deck=contexts > 0,
        existing_contexts=contexts,
        audio_url=None,  # P2, once TTS lands
        dictionary=entry.source if entry else None,
        dictionary_ready=request.app.state.dictionaries.ready_for(lang),
    )


@app.post("/v1/known")
def mark_known(payload: KnownIn, request: Request, client: str = Depends(require_token)):
    """Mark a word as already known so it stops being suggested (§5.3)."""
    inbox: Inbox = request.app.state.inbox
    word = payload.word.strip()
    lang = resolve_lang(word, payload.lang)
    lemma = lemmatize(word, request.app.state.dictionaries, lang)
    if payload.known:
        inbox.mark_known(lemma)
    else:
        inbox.unmark_known(lemma)
    return {"lemma": lemma, "known": payload.known}


@app.post("/v1/translate", response_model=TranslateOut)
def translate_sentence(
    payload: TranslateIn, request: Request, client: str = Depends(require_token)
):
    """Sentence translation and the words in it worth learning (§5.3)."""
    import anyio

    cfg: Config = request.app.state.cfg
    found = candidates(
        payload.text, request.app.state.dictionaries, request.app.state.inbox,
        cfg.min_freq_rank,
    )
    translation = anyio.from_thread.run(
        request.app.state.translator.translate, payload.text, payload.target
    )
    return TranslateOut(translation=translation, candidates=found)


@app.post("/tg/{secret}")
async def telegram_webhook(secret: str, request: Request):
    """Telegram webhook (§7.1). The path secret is the only credential here."""
    cfg: Config = request.app.state.cfg
    bot: Bot = request.app.state.bot
    if bot is None or not cfg.tg_webhook_secret:
        raise HTTPException(status_code=404, detail="bot not configured")
    header = request.headers.get("x-telegram-bot-api-secret-token", "")
    if secret != cfg.tg_webhook_secret or header != cfg.tg_webhook_secret:
        raise HTTPException(status_code=403, detail="bad webhook secret")
    await bot.handle(await request.json())
    return {"ok": True}


@app.get("/v1/stats")
def stats(request: Request, client: str = Depends(require_token)):
    writer: AnkiWriter = request.app.state.writer
    return {
        "inbox": request.app.state.inbox.stats(),
        "queue_depth": writer.queue_depth,
        "last_sync_at": writer.status["last_sync_at"],
        "last_sync_error": writer.status["last_sync_error"],
        "dictionaries": request.app.state.dictionaries.available,
        "bot": request.app.state.bot is not None,
        "translation": request.app.state.translator.enabled,
        "tts_queue": request.app.state.tts.queue_depth,
    }


@app.post("/v1/sync")
def sync_now(request: Request, client: str = Depends(require_token)):
    request.app.state.writer.request_sync()
    return {"status": "requested"}


@app.get("/healthz")
def healthz(request: Request):
    """Liveness probe (§5.3): queue depth and whether the collection is writable."""
    writer: AnkiWriter = request.app.state.writer
    ok = writer.is_alive() and writer.status["collection_open"]
    return JSONResponse(
        status_code=200 if ok else 503,
        content={"ok": ok, "queue_depth": writer.queue_depth, **writer.status},
    )
