"""HTTP entry point (spec §5.3). P0 subset: submit, health, stats, manual sync.

Run with a single worker — the collection has exactly one writer (D2):
    uvicorn app.main:app --host 0.0.0.0 --port 8000 --workers 1
"""

import logging
import os
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.responses import JSONResponse

from . import render
from .anki_writer import AnkiWriter
from .config import Config
from .db import Inbox
from .models import ItemAccepted, ItemIn

logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
log = logging.getLogger("vocab.api")


def lemma_of(word: str) -> str:
    """P0 placeholder for lemmatisation: casefold and trim.

    P1 replaces this with lemminflect so 'ubiquitously' collapses onto
    'ubiquitous'. Until then the dedup key is just the normalised surface form.
    """
    return word.strip().casefold()


@asynccontextmanager
async def lifespan(app: FastAPI):
    cfg = Config.from_env()
    inbox = Inbox(cfg.inbox_db)
    writer = AnkiWriter(cfg, inbox)
    writer.start()
    writer.wait_ready()  # raises -> uvicorn exits, which is what we want (D3)
    app.state.cfg, app.state.inbox, app.state.writer = cfg, inbox, writer
    if not cfg.tokens:
        log.warning("API_TOKENS is empty — every request will be rejected")
    if not cfg.sync_enabled:
        log.warning("SYNC_USERNAME/PASSWORD unset — collection will not sync")
    try:
        yield
    finally:
        writer.stop()


app = FastAPI(title="Vocabulary Builder", version="0.1.0", lifespan=lifespan)


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

    lemma = lemma_of(payload.word)
    result = inbox.add_item(
        {
            "idem_key": payload.idem_key,
            "source": payload.source,
            "client_id": payload.client_id or client,
            "raw_word": payload.word.strip(),
            "lemma": lemma,
            "lang": payload.lang,
            "sentence": payload.sentence,
            "sentence_masked": render.mask_sentence(
                payload.sentence, payload.word.strip(), payload.word_offset
            ),
            "word_offset": payload.word_offset,
            "context_before": payload.context_before,
            "context_after": payload.context_after,
            "url": payload.url,
            "page_title": payload.page_title,
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


@app.get("/v1/stats")
def stats(request: Request, client: str = Depends(require_token)):
    writer: AnkiWriter = request.app.state.writer
    return {
        "inbox": request.app.state.inbox.stats(),
        "queue_depth": writer.queue_depth,
        "last_sync_at": writer.status["last_sync_at"],
        "last_sync_error": writer.status["last_sync_error"],
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
