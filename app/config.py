"""Environment configuration (spec §5.5). P0 subset."""

import os
from dataclasses import dataclass


def _int(name: str, default: int) -> int:
    return int(os.environ.get(name) or default)


def _tokens(raw: str) -> dict[str, str]:
    """API_TOKENS=chrome:xxxx,bot:yyyy -> {token: client_label}."""
    out = {}
    for pair in raw.split(","):
        pair = pair.strip()
        if not pair:
            continue
        label, sep, token = pair.partition(":")
        if not sep or not token:
            raise ValueError(f"API_TOKENS entry {pair!r} must be 'label:token'")
        out[token] = label
    return out


@dataclass(frozen=True)
class Config:
    collection_path: str
    deck: str
    notetype: str
    inbox_db: str
    sync_endpoint: str
    sync_username: str
    sync_password: str
    sync_debounce_sec: int
    sync_interval_sec: int
    tokens: dict[str, str]
    max_contexts_per_note: int
    max_sentence_chars: int
    ecdict_path: str
    cedict_path: str
    min_freq_rank: int

    def __post_init__(self):
        # An empty endpoint means AnkiWeb to the anki library. D7 chose a
        # self-hosted server, so silently falling back would send these
        # credentials somewhere they were never meant to go.
        if self.sync_username and self.sync_password and not self.sync_endpoint:
            raise ValueError(
                "SYNC_USERNAME/SYNC_PASSWORD are set but SYNC_ENDPOINT is empty. "
                "An empty endpoint means AnkiWeb, which this deployment does not "
                "use (D7). Set SYNC_ENDPOINT (compose derives it from "
                "SYNC_SERVER_PORT), or clear the credentials to run without sync."
            )

    @classmethod
    def from_env(cls) -> "Config":
        return cls(
            collection_path=os.environ.get(
                "ANKI_COLLECTION_PATH", "/data/anki/collection.anki2"
            ),
            deck=os.environ.get("ANKI_DECK", "Vocab::Reading"),
            notetype=os.environ.get("ANKI_NOTETYPE", "VocabContext"),
            inbox_db=os.environ.get("INBOX_DB", "/data/app/inbox.db"),
            sync_endpoint=os.environ.get("SYNC_ENDPOINT", ""),
            sync_username=os.environ.get("SYNC_USERNAME", ""),
            sync_password=os.environ.get("SYNC_PASSWORD", ""),
            sync_debounce_sec=_int("SYNC_DEBOUNCE_SEC", 60),
            sync_interval_sec=_int("SYNC_INTERVAL_SEC", 1800),
            tokens=_tokens(os.environ.get("API_TOKENS", "")),
            max_contexts_per_note=_int("MAX_CONTEXTS_PER_NOTE", 5),
            max_sentence_chars=_int("MAX_SENTENCE_CHARS", 400),
            ecdict_path=os.environ.get("ECDICT_PATH", ""),
            cedict_path=os.environ.get("CEDICT_PATH", ""),
            min_freq_rank=_int("MIN_FREQ_RANK", 3000),
        )

    @property
    def sync_enabled(self) -> bool:
        return bool(self.sync_username and self.sync_password)
