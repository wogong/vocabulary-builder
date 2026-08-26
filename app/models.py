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
