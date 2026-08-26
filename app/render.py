"""Turning a captured sentence into Anki field HTML (spec §4.2)."""

import html
import json

BLANK = "____"
SEP = "<hr>"


def locate(sentence: str, word: str, offset: int | None) -> tuple[int, int] | None:
    """Resolve the target word's span in the sentence.

    The client sends `word_offset` because the same word can occur twice in one
    sentence (§5.3). We trust it only if it actually lands on the word, and fall
    back to a case-insensitive search otherwise.
    """
    if not word:
        return None
    if offset is not None and 0 <= offset <= len(sentence) - len(word):
        if sentence[offset : offset + len(word)].lower() == word.lower():
            return offset, offset + len(word)
    found = sentence.lower().find(word.lower())
    if found < 0:
        return None
    return found, found + len(word)


def mask_sentence(sentence: str, word: str, offset: int | None) -> str:
    """Plain-text sentence with the target replaced by a blank."""
    span = locate(sentence, word, offset)
    if span is None:
        return sentence
    start, end = span
    return f"{sentence[:start]}{BLANK}{sentence[end:]}"


def _wrap(sentence: str, word: str, offset: int | None, replacement: str | None) -> str:
    """Escape the sentence and emphasise the target (or a blank standing in)."""
    span = locate(sentence, word, offset)
    if span is None:
        return html.escape(sentence)
    start, end = span
    middle = replacement if replacement is not None else sentence[start:end]
    return (
        html.escape(sentence[:start])
        + "<b>"
        + html.escape(middle)
        + "</b>"
        + html.escape(sentence[end:])
    )


def context_html(sentence: str, word: str, offset: int | None) -> str:
    return _wrap(sentence, word, offset, None)


def masked_html(sentence: str, word: str, offset: int | None) -> str:
    return _wrap(sentence, word, offset, BLANK)


def source_html(url: str | None, title: str | None) -> str:
    if not url:
        return html.escape(title or "")
    return f'<a href="{html.escape(url, quote=True)}">{html.escape(title or url)}</a>'


def append_context(existing: str, new_html: str, max_n: int) -> str:
    """Append a context, dropping the oldest beyond max_n and skipping repeats (D4)."""
    parts = [p for p in (existing or "").split(SEP) if p.strip()]
    if new_html in parts:
        return existing
    parts.append(new_html)
    return SEP.join(parts[-max_n:])


def merge_meta(existing: str, item_id: int) -> str:
    """Meta holds the inbox item ids behind this note, so cards trace back (§4.2)."""
    try:
        meta = json.loads(existing) if existing else {}
        ids = list(meta.get("item_ids", []))
    except (ValueError, AttributeError):
        meta, ids = {}, []
    if item_id not in ids:
        ids.append(item_id)
    meta["item_ids"] = ids
    return json.dumps(meta, ensure_ascii=False)
