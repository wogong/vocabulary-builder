"""The single writer that owns the Anki collection (spec §5.4, D2).

One thread, one Collection instance, all writes serialised through a queue.
Nothing else in the process may touch the collection.
"""

import fcntl
import json
import logging
import queue
import threading
import time
from pathlib import Path

from anki.collection import Collection, SearchNode
from anki.sync_pb2 import SyncCollectionResponse as SyncResp

from . import render
from .db import utcnow
from .notetype import verify_notetype

log = logging.getLogger("vocab.anki")

FULL_SYNC_REQUIRED = {SyncResp.FULL_SYNC, SyncResp.FULL_DOWNLOAD, SyncResp.FULL_UPLOAD}
_SHUTDOWN = object()
_SYNC_NOW = object()


class CollectionLocked(RuntimeError):
    """Another process already holds the collection (R3)."""


class AnkiWriter(threading.Thread):
    def __init__(self, cfg, inbox):
        super().__init__(name="anki-writer", daemon=True)
        self.cfg = cfg
        self.inbox = inbox
        self.col: Collection | None = None
        self.fatal: str | None = None

        self._q: queue.Queue = queue.Queue()
        self._ready = threading.Event()
        self._auth = None
        self._deck_id = None
        self._dirty = False
        self._last_write = 0.0
        self._last_sync = time.monotonic()
        self._lock_fd = self._acquire_lock()

        self.status = {
            "collection_open": False,
            "last_sync_at": None,
            "last_sync_error": None,
            "full_sync_required": False,
            "committed": 0,
            "failed": 0,
        }

    # -- lifecycle ------------------------------------------------------

    def _acquire_lock(self):
        """Exclusive file lock so two instances can never open one collection."""
        path = Path(self.cfg.collection_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        fd = open(path.with_suffix(".lock"), "w")
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            fd.close()
            raise CollectionLocked(
                f"{path} is locked by another process — refusing to start (R3)"
            ) from exc
        return fd

    def run(self):
        try:
            self.col = Collection(self.cfg.collection_path)
            verify_notetype(self.col, self.cfg.notetype)
            self._deck_id = self._ensure_deck()
            self.status["collection_open"] = True
            backlog = self.inbox.pending_ids()
            for item_id in backlog:
                self._q.put(item_id)
            if backlog:
                log.info("re-queued %d pending item(s) from a previous run", len(backlog))
        except BaseException as exc:
            self.fatal = f"{type(exc).__name__}: {exc}"
            log.error("anki writer failed to start: %s", self.fatal)
            self._ready.set()
            return
        self._ready.set()
        log.info("anki writer ready (deck=%r notetype=%r)", self.cfg.deck, self.cfg.notetype)
        self._loop()

    def wait_ready(self, timeout: float = 60.0) -> None:
        if not self._ready.wait(timeout):
            raise RuntimeError("anki writer did not become ready in time")
        if self.fatal:
            raise RuntimeError(self.fatal)

    def _ensure_deck(self):
        """Decks are ordinary synced objects — creating one is not a schema change (D3)."""
        deck_id = self.col.decks.id_for_name(self.cfg.deck)
        if deck_id is None:
            log.warning("deck %r not found, creating it", self.cfg.deck)
            deck_id = self.col.decks.id(self.cfg.deck)
        return deck_id

    def submit(self, item_id: int) -> None:
        self._q.put(item_id)

    def request_sync(self) -> None:
        """Ask for a sync on the writer thread — the collection is not thread-safe."""
        self._q.put(_SYNC_NOW)

    def stop(self, timeout: float = 120.0) -> None:
        self._q.put(_SHUTDOWN)
        self.join(timeout)

    @property
    def queue_depth(self) -> int:
        return self._q.qsize()

    # -- main loop ------------------------------------------------------

    def _loop(self):
        while True:
            try:
                job = self._q.get(timeout=self._sleep_for())
            except queue.Empty:
                job = None
            if job is _SHUTDOWN:
                break
            if job is _SYNC_NOW:
                self._sync()
                continue
            if job is not None:
                self._project(job)
            self._maybe_sync()
        self._shutdown()

    def _sleep_for(self) -> float:
        """Wake up no later than the next sync deadline."""
        now = time.monotonic()
        deadlines = [self._last_sync + self.cfg.sync_interval_sec - now]
        if self._dirty:
            deadlines.append(self._next_debounced_sync() - now)
        return max(1.0, min(30.0, min(deadlines)))

    def _next_debounced_sync(self) -> float:
        """Earliest time a debounced sync may run.

        Measured from the last attempt as well as the last write: a sync that
        failed (or that reported a full sync is needed) leaves _dirty set, and
        without the attempt in here it would retry every second forever.
        """
        return max(self._last_write, self._last_sync) + self.cfg.sync_debounce_sec

    def _shutdown(self):
        log.info("anki writer shutting down")
        try:
            if self._dirty and self.cfg.sync_enabled:
                self._sync()
        finally:
            if self.col is not None:
                self.col.close()
            self.status["collection_open"] = False
            fcntl.flock(self._lock_fd, fcntl.LOCK_UN)
            self._lock_fd.close()

    # -- projection -----------------------------------------------------

    def _project(self, item_id: int):
        item = self.inbox.get(item_id)
        if item is None or item["status"] != "pending":
            return
        try:
            note_id, mode, merge_target = self._upsert(item)
        except Exception as exc:
            log.exception("failed to project item %s", item_id)
            self.inbox.mark_failed(item_id, f"{type(exc).__name__}: {exc}")
            self.status["failed"] += 1
            return
        self.inbox.mark_committed(item_id, note_id, merge_target)
        self.status["committed"] += 1
        self._dirty = True
        self._last_write = time.monotonic()
        log.info("item %s -> note %s (%s) %r", item_id, note_id, mode, item["lemma"])

    def _upsert(self, item) -> tuple[int, str, int | None]:
        """Create the note, or append this context to the existing one (D4)."""
        notetype = self.col.models.by_name(self.cfg.notetype)
        word, sentence, offset = item["raw_word"], item["sentence"], item["word_offset"]
        context = render.context_html(sentence, word, offset)
        masked = render.masked_html(sentence, word, offset)
        source = render.source_html(item["url"], item["page_title"])

        existing = self.col.find_notes(
            self.col.build_search_string(
                SearchNode(
                    dupe=SearchNode.Dupe(
                        notetype_id=notetype["id"], first_field=item["lemma"]
                    )
                )
            )
        )
        if existing:
            note = self.col.get_note(existing[0])
            merge_target = _first_item_id(note["Meta"])
            note["Contexts"] = render.append_context(
                note["Contexts"], context, self.cfg.max_contexts_per_note
            )
            # The front shows the newest context; Contexts keeps the history.
            note["ContextsMasked"] = masked
            note["Meta"] = render.merge_meta(note["Meta"], item["id"])
            if source:
                note["Source"] = source
            self.col.update_note(note)
            return note.id, "merged", merge_target

        note = self.col.new_note(notetype)
        note["Word"] = item["lemma"]
        note["Contexts"] = context
        note["ContextsMasked"] = masked
        note["Source"] = source
        note["Meta"] = render.merge_meta("", item["id"])
        self.col.add_note(note, self._deck_id)
        return note.id, "new", None

    # -- sync -----------------------------------------------------------

    def _get_auth(self):
        if self._auth is None:
            self._auth = self.col.sync_login(
                self.cfg.sync_username,
                self.cfg.sync_password,
                self.cfg.sync_endpoint or None,
            )
        return self._auth

    def _maybe_sync(self):
        if not self.cfg.sync_enabled:
            return
        now = time.monotonic()
        debounced = self._dirty and now >= self._next_debounced_sync()
        periodic = (now - self._last_sync) >= self.cfg.sync_interval_sec
        if debounced or periodic:
            self._sync()

    def _sync(self):
        # Stamp first: a failing sync must not spin.
        self._last_sync = time.monotonic()
        try:
            out = self.col.sync_collection(self._get_auth(), True)
        except Exception as exc:
            self._auth = None
            self.status["last_sync_error"] = f"{type(exc).__name__}: {exc}"
            log.warning("sync failed: %s", self.status["last_sync_error"])
            return

        if out.server_message:
            log.info("sync server message: %s", out.server_message)

        if out.required in FULL_SYNC_REQUIRED:
            # Never run this automatically: it means choosing which side to
            # overwrite, and that is how collections get lost (R2, D3).
            self.status["full_sync_required"] = True
            self.status["last_sync_error"] = "full sync required"
            log.error(
                "SYNC REQUIRES FULL SYNC (required=%s) — not running it automatically. "
                "Resolve manually in desktop Anki; see spec §5.4.",
                out.required,
            )
            return

        self._dirty = False
        self.status["full_sync_required"] = False
        self.status["last_sync_error"] = None
        self.status["last_sync_at"] = utcnow()
        log.info("sync ok (required=%s)", out.required)


def _first_item_id(meta: str) -> int | None:
    try:
        ids = json.loads(meta or "{}").get("item_ids") or []
    except ValueError:
        return None
    return ids[0] if ids else None
