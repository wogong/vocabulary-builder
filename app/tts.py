"""Pronunciation audio (spec §4.2 Audio, decision B3).

edge-tts is a network call that can be slow or fail, so synthesis runs on its
own worker rather than blocking the Anki writer. Finished audio is handed to
the writer, which is the only thing allowed to touch the collection's media
folder (D2).
"""

import asyncio
import logging
import queue
import re
import threading

log = logging.getLogger("vocab.tts")

_SHUTDOWN = object()
SAFE = re.compile(r"[^a-z0-9_-]+")


def media_name(lemma: str) -> str:
    """Stable filename so re-capturing a word reuses its audio."""
    return f"vocab-{SAFE.sub('-', lemma.lower()).strip('-')}.mp3"


class Tts(threading.Thread):
    def __init__(self, cfg, inbox, writer):
        super().__init__(name="tts", daemon=True)
        self.cfg = cfg
        self.inbox = inbox
        self.writer = writer
        self._q: queue.Queue = queue.Queue()
        self.failures = 0

    def submit(self, lemma: str, lang: str = "en"):
        if self.cfg.tts_enabled and lang == "en":
            self._q.put(lemma)

    def stop(self, timeout: float = 30.0):
        self._q.put(_SHUTDOWN)
        self.join(timeout)

    @property
    def queue_depth(self) -> int:
        return self._q.qsize()

    async def speak(self, text: str) -> bytes | None:
        """Synthesise to mp3 bytes. Returns None rather than raising."""
        try:
            import edge_tts

            communicate = edge_tts.Communicate(text, self.cfg.tts_voice)
            audio = bytearray()
            async for chunk in communicate.stream():
                if chunk["type"] == "audio":
                    audio.extend(chunk["data"])
            return bytes(audio) or None
        except Exception as exc:
            log.warning("tts failed for %r: %s", text, exc)
            return None

    def run(self):
        while True:
            job = self._q.get()
            if job is _SHUTDOWN:
                break
            try:
                self._synthesise(job)
            except Exception:
                log.exception("tts worker failed on %r", job)
                self.failures += 1

    def _synthesise(self, lemma: str):
        filename = media_name(lemma)
        # Skip words that already have audio: this runs on every capture.
        if self.writer.ask(lambda col: col.media.have(filename)):
            self.writer.ask(lambda col: _attach(col, self.cfg.notetype, lemma, filename))
            return

        audio = asyncio.run(self.speak(lemma))
        if not audio:
            self.failures += 1
            return

        def store(col):
            col.media.write_data(filename, audio)
            return _attach(col, self.cfg.notetype, lemma, filename)

        if self.writer.ask(store, timeout=60):
            self.writer.mark_dirty()
            log.info("tts attached %s", filename)


def _attach(col, notetype_name, lemma, filename) -> bool:
    """Put [sound:...] on the note, if it does not have audio already."""
    from anki.collection import SearchNode

    notetype = col.models.by_name(notetype_name)
    found = col.find_notes(
        col.build_search_string(
            SearchNode(dupe=SearchNode.Dupe(notetype_id=notetype["id"], first_field=lemma))
        )
    )
    if not found:
        return False
    note = col.get_note(found[0])
    if note["Audio"].strip():
        return False
    note["Audio"] = f"[sound:{filename}]"
    col.update_note(note)
    return True
