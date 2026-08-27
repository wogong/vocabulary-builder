"""One capture path, used by both the HTTP API and the Telegram bot.

Keeping this in one place means a word collected from Telegram is annotated,
deduplicated and projected exactly like one collected from the browser.
"""

from . import render
from .dictionary import is_cjk
from .lemma import lemmatize


def resolve_lang(word: str, requested: str = "en") -> str:
    """A CJK word is Chinese whatever the caller claimed (decision B1)."""
    return "zh" if is_cjk(word) else (requested or "en")


def annotate(dictionaries, word: str, lang: str):
    """Lemma plus whatever the dictionaries know about the word."""
    lemma = lemmatize(word, dictionaries, lang)
    entry = None
    if dictionaries is not None:
        entry = dictionaries.lookup(lemma, lang) or dictionaries.lookup(word.strip(), lang)
    return lemma, entry


def capture(inbox, dictionaries, writer, *, word, sentence, lang="en", source="api",
            client_id=None, idem_key=None, word_offset=None, context_before=None,
            context_after=None, url=None, page_title=None, client_ts=None,
            raw_payload=None) -> dict:
    """Record a captured word and queue it for projection into Anki."""
    word = word.strip()
    lang = resolve_lang(word, lang)
    lemma, entry = annotate(dictionaries, word, lang)

    result = inbox.add_item(
        {
            "idem_key": idem_key,
            "source": source,
            "client_id": client_id,
            "raw_word": word,
            "lemma": lemma,
            "lang": lang,
            "sentence": sentence,
            "sentence_masked": render.mask_sentence(sentence, word, word_offset),
            "word_offset": word_offset,
            "context_before": context_before,
            "context_after": context_after,
            "url": url,
            "page_title": page_title,
            "definition": "\n".join(entry.definitions) if entry else None,
            "phonetic": entry.phonetic if entry else None,
            "freq_rank": entry.freq_rank if entry else None,
            "client_ts": client_ts,
        },
        raw_payload if raw_payload is not None else {},
    )

    if result["status"] == "pending":
        writer.submit(result["id"])

    result["lemma"] = lemma
    result["entry"] = entry
    return result
