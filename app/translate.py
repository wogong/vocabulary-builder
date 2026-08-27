"""Sentence handling for the bot (spec §5.3 /v1/translate, §7.4).

Two separate jobs:

  candidates()  pick the words in a sentence worth learning. Pure local work
                (dictionary + frequency + what you already have), always on.
  Translator    render the sentence in Chinese. Needs an LLM endpoint, so it
                is optional and off by default (decision B2).

The candidate list is the part that matters: it is what lets you pick words
out of a sentence instead of typing them (G5).
"""

import logging
import re

import httpx

log = logging.getLogger("vocab.translate")

WORD_RE = re.compile(r"[A-Za-z][A-Za-z'’-]*")
MAX_CANDIDATES = 8
MIN_CONTEXTS_BEFORE_SKIP = 3


def tokenize(text: str) -> list[tuple[str, int]]:
    return [(m.group(0), m.start()) for m in WORD_RE.finditer(text)]


def candidates(text, dictionaries, inbox, min_freq_rank: int, limit=MAX_CANDIDATES):
    """Words in `text` worth offering, hardest first (§5.3 candidate rules)."""
    from .lemma import lemmatize

    seen: set[str] = set()
    found = []

    for surface, offset in tokenize(text):
        # A capitalised word mid-sentence is usually a name, not vocabulary.
        if offset > 0 and surface[0].isupper() and text[max(0, offset - 2)] not in ".!?":
            continue

        lemma = lemmatize(surface, dictionaries)
        if lemma in seen:
            continue

        entry = dictionaries.lookup(lemma) if dictionaries else None
        if entry is None or not entry.definitions:
            continue  # not in the dictionary: a name, a typo, or jargon
        if entry.freq_rank is not None and entry.freq_rank <= min_freq_rank:
            continue  # too common to be worth a card
        if inbox.is_known(lemma):
            continue

        contexts = inbox.context_count(lemma, "en")
        if contexts >= MIN_CONTEXTS_BEFORE_SKIP:
            continue  # already has plenty of contexts

        seen.add(lemma)
        found.append(
            {
                "word": lemma,
                "raw": surface,
                "offset": offset,
                "freq_rank": entry.freq_rank,
                "brief": _brief(entry.definitions),
                "in_deck": contexts > 0,
                "existing_contexts": contexts,
            }
        )

    # Rarest first (§5.3): an unranked word is rarer than any ranked one.
    found.sort(key=lambda c: c["freq_rank"] if c["freq_rank"] is not None else 10**9,
               reverse=True)
    return found[:limit]


def _brief(definitions: list[str]) -> str:
    """First sense, trimmed to something that fits on a button."""
    if not definitions:
        return ""
    first = re.sub(r"^[a-z]+\.\s*", "", definitions[0]).strip()
    first = re.split(r"[;；,，]", first)[0].strip()
    return first[:14]


class Translator:
    """Sentence translation via an OpenAI-compatible chat endpoint.

    Disabled unless LLM_ENABLED and a base URL and model are configured; the
    sentence flow degrades to candidates only (which is the useful half).
    """

    def __init__(self, cfg):
        self.enabled = bool(cfg.llm_enabled and cfg.llm_api_base and cfg.llm_model)
        self.base = cfg.llm_api_base.rstrip("/")
        self.key = cfg.llm_api_key
        self.model = cfg.llm_model
        self.timeout = cfg.llm_timeout_sec

    async def translate(self, text: str, target: str = "zh") -> str | None:
        if not self.enabled:
            return None
        prompt = (
            "Translate the text into "
            + ("Simplified Chinese" if target == "zh" else target)
            + ". Reply with the translation only, no notes.\n\n"
            + text
        )
        try:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                response = await client.post(
                    f"{self.base}/chat/completions",
                    headers={"Authorization": f"Bearer {self.key}"} if self.key else {},
                    json={
                        "model": self.model,
                        "messages": [{"role": "user", "content": prompt}],
                        "temperature": 0,
                    },
                )
                response.raise_for_status()
                body = response.json()
                return body["choices"][0]["message"]["content"].strip()
        except Exception as exc:
            # Never let translation failure sink the sentence flow.
            log.warning("translation failed: %s", exc)
            return None
