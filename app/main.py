"""HTTP entry point (spec §5.3). P0 + P1.

Run with a single worker — the collection has exactly one writer (D2):
    uvicorn app.main:app --host 0.0.0.0 --port 8000 --workers 1
"""

import logging
import os
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request
from fastapi.responses import JSONResponse

from . import render
from .anki_writer import AnkiWriter
from .config import Config
from .db import Inbox
from .dictionary import Dictionaries, is_cjk
from .lemma import lemmatize
from .models import ItemAccepted, ItemIn, KnownIn, LookupOut

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
    app.state.cfg = cfg
    app.state.inbox = inbox
    app.state.dictionaries = dictionaries
    app.state.writer = writer
    if not cfg.tokens:
        log.warning("API_TOKENS is empty — every request will be rejected")
    if not cfg.sync_enabled:
        log.warning("SYNC_USERNAME/PASSWORD unset — collection will not sync")
    try:
        yield
    finally:
        writer.stop()


app = FastAPI(title="Vocabulary Builder", version="0.2.0", lifespan=lifespan)


def require_token(request: Request, authorization: str = Header(default="")) -> str:
    scheme, _, token = authorization.partition(" ")
    client = request.app.state.cfg.tokens.get(token) if scheme.lower() == "bearer" else None
    if not client:
        raise HTTPException(status_code=401, detail="invalid or missing bearer token")
    return client


def resolve_lang(word: str, requested: str) -> str:
    """A CJK word is Chinese whatever the client claimed (decision B1)."""
    return "zh" if is_cjk(word) else (requested or "en")


def _annotate(request: Request, word: str, lang: str) -> tuple[str, object]:
    """Lemma plus whatever the dictionaries know about the word."""
    dictionaries = request.app.state.dictionaries
    lemma = lemmatize(word, dictionaries, lang)
    entry = dictionaries.lookup(lemma, lang) or dictionaries.lookup(word.strip(), lang)
    return lemma, entry


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

    word = payload.word.strip()
    lang = resolve_lang(word, payload.lang)
    lemma, entry = _annotate(request, word, lang)

    # Dictionary lookups are local and sub-millisecond, so enrichment happens
    # inline. The async worker of §5.2 only becomes necessary when P2/P3 add
    # TTS and LLM calls, which are slow and can fail.
    result = inbox.add_item(
        {
            "idem_key": payload.idem_key,
            "source": payload.source,
            "client_id": payload.client_id or client,
            "raw_word": word,
            "lemma": lemma,
            "lang": lang,
            "sentence": payload.sentence,
            "sentence_masked": render.mask_sentence(
                payload.sentence, word, payload.word_offset
            ),
            "word_offset": payload.word_offset,
            "context_before": payload.context_before,
            "context_after": payload.context_after,
            "url": payload.url,
            "page_title": payload.page_title,
            "definition": "\n".join(entry.definitions) if entry else None,
            "phonetic": entry.phonetic if entry else None,
            "freq_rank": entry.freq_rank if entry else None,
            "client_ts": payload.client_ts,
        },
        payload.model_dump(),
    )

    if result["status"] == "pending":
        writer.submit(result["id"])

    return ItemAccepted(
        id=result["id"],
        status="queued" if result["status"] == "pending" else result["status"],
        dedup=result["dedup"],
        lemma=lemma,
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
    lemma, entry = _annotate(request, word, lang)
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


@app.get("/v1/stats")
def stats(request: Request, client: str = Depends(require_token)):
    writer: AnkiWriter = request.app.state.writer
    return {
        "inbox": request.app.state.inbox.stats(),
        "queue_depth": writer.queue_depth,
        "last_sync_at": writer.status["last_sync_at"],
        "last_sync_error": writer.status["last_sync_error"],
        "dictionaries": request.app.state.dictionaries.available,
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
