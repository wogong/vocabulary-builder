"""Offline dictionaries (spec §5.1, P1).

ECDICT for English (SQLite, carries phonetics, Chinese glosses, COCA/BNC
frequency and inflection data); CC-CEDICT for the occasional Chinese lookup
(§5.3, decision B1). Both are optional: with no data files the server still
captures words, it just cannot annotate them.
"""

import gzip
import logging
import re
import sqlite3
import threading
from dataclasses import dataclass, field
from pathlib import Path

log = logging.getLogger("vocab.dict")

CJK = re.compile(r"[㐀-䶿一-鿿豈-﫿]")


def is_cjk(text: str) -> bool:
    return bool(CJK.search(text))


@dataclass
class Entry:
    word: str
    phonetic: str | None = None
    definitions: list[str] = field(default_factory=list)
    freq_rank: int | None = None
    lemma: str | None = None      # base form, when the dictionary knows one
    source: str | None = None


class Ecdict:
    """Read-only access to ECDICT's `stardict` table.

    `word` is COLLATE NOCASE, so lookups are case-insensitive for free.
    Connections are per-thread: the API threadpool and the writer both read.
    """

    def __init__(self, path: str):
        self.path = path
        self._local = threading.local()
        with sqlite3.connect(f"file:{path}?mode=ro", uri=True) as probe:
            probe.execute("SELECT word FROM stardict LIMIT 1").fetchone()

    def _conn(self) -> sqlite3.Connection:
        conn = getattr(self._local, "conn", None)
        if conn is None:
            conn = sqlite3.connect(
                f"file:{self.path}?mode=ro", uri=True, check_same_thread=False
            )
            conn.row_factory = sqlite3.Row
            self._local.conn = conn
        return conn

    def lookup(self, word: str) -> Entry | None:
        row = self._conn().execute(
            "SELECT word, phonetic, definition, translation, frq, bnc, exchange "
            "FROM stardict WHERE word = ? LIMIT 1",
            (word,),
        ).fetchone()
        if row is None:
            return None
        return Entry(
            word=row["word"],
            phonetic=_clean_phonetic(row["phonetic"]),
            definitions=_split_glosses(row["translation"]) or _split_glosses(row["definition"]),
            freq_rank=_rank(row["frq"], row["bnc"]),
            lemma=_base_form(row["exchange"]),
            source="ecdict",
        )


class Cedict:
    """CC-CEDICT, parsed into memory (~120k entries, a few tens of MB)."""

    LINE = re.compile(r"^(\S+)\s+(\S+)\s+\[([^]]*)\]\s+/(.*)/\s*$")

    def __init__(self, path: str):
        self.entries: dict[str, Entry] = {}
        opener = gzip.open if str(path).endswith(".gz") else open
        with opener(path, "rt", encoding="utf-8", errors="replace") as handle:
            for line in handle:
                if line.startswith("#") or not line.strip():
                    continue
                match = self.LINE.match(line.rstrip("\n"))
                if not match:
                    continue
                traditional, simplified, pinyin, glosses = match.groups()
                entry = Entry(
                    word=simplified,
                    phonetic=pinyin or None,
                    definitions=[g for g in glosses.split("/") if g],
                    source="cedict",
                )
                # Later entries add senses rather than replacing earlier ones.
                for key in (simplified, traditional):
                    existing = self.entries.get(key)
                    if existing is None:
                        self.entries[key] = entry
                    else:
                        for gloss in entry.definitions:
                            if gloss not in existing.definitions:
                                existing.definitions.append(gloss)
        log.info("loaded %d CC-CEDICT entries from %s", len(self.entries), path)

    def lookup(self, word: str) -> Entry | None:
        return self.entries.get(word)


class Dictionaries:
    """Whatever dictionaries are actually present. Missing ones are not fatal."""

    def __init__(self, ecdict_path: str = "", cedict_path: str = ""):
        self.ecdict = self._open(Ecdict, ecdict_path, "ECDICT")
        self.cedict = self._open(Cedict, cedict_path, "CC-CEDICT")

    @staticmethod
    def _open(cls, path: str, label: str):
        if not path:
            # Silence here is how a missing config key turns into a feature that
            # quietly does nothing.
            log.warning("%s path is not configured — words will be captured unannotated", label)
            return None
        if not Path(path).exists():
            log.warning("%s not found at %s — words will be captured unannotated", label, path)
            return None
        try:
            return cls(path)
        except Exception as exc:
            log.warning("%s at %s is unusable (%s) — continuing without it", label, path, exc)
            return None

    @property
    def available(self) -> dict:
        return {"ecdict": self.ecdict is not None, "cedict": self.cedict is not None}

    def ready_for(self, lang: str) -> bool:
        """Whether a dictionary is loaded for this language.

        Distinct from "the word was found": a client needs to tell a missing
        dictionary apart from a word the dictionary simply does not list.
        """
        return (self.cedict if lang == "zh" else self.ecdict) is not None

    def lookup(self, word: str, lang: str = "en") -> Entry | None:
        if lang == "zh" or is_cjk(word):
            return self.cedict.lookup(word) if self.cedict else None
        return self.ecdict.lookup(word) if self.ecdict else None


def _clean_phonetic(raw: str | None) -> str | None:
    raw = (raw or "").strip()
    if not raw:
        return None
    return raw if raw.startswith("/") else f"/{raw}/"


def _split_glosses(raw: str | None) -> list[str]:
    return [line.strip() for line in (raw or "").split("\n") if line.strip()]


def _rank(frq, bnc) -> int | None:
    """COCA rank preferred, BNC as backup. 0 means "not ranked", not "rank 0"."""
    for value in (frq, bnc):
        if value:
            return int(value)
    return None


def _base_form(exchange: str | None) -> str | None:
    """ECDICT encodes inflections as 'p:went/d:gone/0:go'; 0 is the base form."""
    for part in (exchange or "").split("/"):
        tag, sep, value = part.partition(":")
        if sep and tag == "0" and value:
            return value
    return None
