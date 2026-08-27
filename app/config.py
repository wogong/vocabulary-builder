"""Environment configuration (spec §5.5). P0 subset."""

import os
from dataclasses import dataclass


def _int(name: str, default: int) -> int:
    return int(os.environ.get(name) or default)


def _bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


def _ids(raw: str) -> tuple[int, ...]:
    return tuple(int(part) for part in raw.replace(" ", "").split(",") if part)


def _times(raw: str) -> tuple[str, ...]:
    """'09:00,21:00' -> ('09:00', '21:00')"""
    out = []
    for part in raw.replace(" ", "").split(","):
        if not part:
            continue
        hour, _, minute = part.partition(":")
        out.append(f"{int(hour):02d}:{int(minute or 0):02d}")
    return tuple(out)


def _quiet(raw: str) -> tuple[int, int]:
    """'23:00-08:00' -> (23, 8); reminders inside this window are held."""
    start, _, end = raw.partition("-")
    return (int(start.split(":")[0]), int(end.split(":")[0]))


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
    # Telegram (§7)
    tg_bot_token: str
    tg_allowed_chat_ids: tuple[int, ...]
    tg_webhook_secret: str
    tg_timezone: str
    tg_reminder_times: tuple[str, ...]
    tg_quiet_hours: tuple[int, int]
    bot_review_writes_back: bool
    # Sentence translation (§7.4) — off unless an endpoint is configured
    llm_enabled: bool
    llm_api_base: str
    llm_api_key: str
    llm_model: str
    llm_timeout_sec: float
    # TTS (§4.2 Audio)
    tts_enabled: bool
    tts_voice: str

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
            tg_bot_token=os.environ.get("TG_BOT_TOKEN", ""),
            tg_allowed_chat_ids=_ids(os.environ.get("TG_ALLOWED_CHAT_IDS", "")),
            tg_webhook_secret=os.environ.get("TG_WEBHOOK_SECRET", ""),
            tg_timezone=os.environ.get("TG_TIMEZONE", "Asia/Shanghai"),
            tg_reminder_times=_times(os.environ.get("TG_REMINDER_TIMES", "09:00,21:00")),
            tg_quiet_hours=_quiet(os.environ.get("TG_QUIET_HOURS", "23:00-08:00")),
            bot_review_writes_back=_bool("BOT_REVIEW_WRITES_BACK", False),
            llm_enabled=_bool("LLM_ENABLED", False),
            llm_api_base=os.environ.get("LLM_API_BASE", ""),
            llm_api_key=os.environ.get("LLM_API_KEY", ""),
            llm_model=os.environ.get("LLM_MODEL", ""),
            llm_timeout_sec=float(os.environ.get("LLM_TIMEOUT_SEC") or 15),
            tts_enabled=_bool("TTS_ENABLED", True),
            tts_voice=os.environ.get("TTS_VOICE", "en-US-AriaNeural"),
        )

    @property
    def sync_enabled(self) -> bool:
        return bool(self.sync_username and self.sync_password)

    @property
    def bot_enabled(self) -> bool:
        return bool(self.tg_bot_token and self.tg_webhook_secret)
