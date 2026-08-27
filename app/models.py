"""Request/response schemas (spec §5.3). P0 subset."""

from pydantic import BaseModel, Field


class ItemIn(BaseModel):
    idem_key: str | None = None
    source: str = "api"
    client_id: str | None = None
    word: str = Field(min_length=1, max_length=100)
    lang: str = "en"
    sentence: str = Field(min_length=1, max_length=2000)
    word_offset: int | None = Field(default=None, ge=0)
    context_before: str | None = Field(default=None, max_length=2000)
    context_after: str | None = Field(default=None, max_length=2000)
    url: str | None = Field(default=None, max_length=2000)
    page_title: str | None = Field(default=None, max_length=500)
    client_ts: str | None = None


class ItemAccepted(BaseModel):
    id: int
    status: str
    dedup: str
    lemma: str
    existing_contexts: int


class LookupOut(BaseModel):
    word: str
    lemma: str
    phonetic: str | None = None
    definitions: list[str] = []
    context_definition: str | None = None
    freq_rank: int | None = None
    common: bool = False
    already_known: bool = False
    in_deck: bool = False
    existing_contexts: int = 0
    audio_url: str | None = None
    dictionary: str | None = None      # which dictionary answered, if any
    dictionary_ready: bool = False     # whether one was loaded at all


class KnownIn(BaseModel):
    word: str = Field(min_length=1, max_length=100)
    lang: str = "en"
    known: bool = True


class TranslateIn(BaseModel):
    text: str = Field(min_length=1, max_length=4000)
    lang: str = "en"
    target: str = "zh"


class TranslateOut(BaseModel):
    translation: str | None = None
    candidates: list[dict] = []
