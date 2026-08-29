"""Undo a capture (spec §4.1).

The inbox is the source of truth (D1), so removing a word means removing both
the note's context and the inbox row behind it. Deleting only the note would
let scripts/reproject.py resurrect it on the next replay.

Runs on the writer thread — the collection has one writer (D2), so callers
reach it through AnkiWriter.ask().
"""

import html

from . import render


def retract(col, item) -> str:
    """Undo one capture: drop its context, and the note too if that was all."""
    note_id = item["anki_note_id"]
    if not note_id:
        return "nothing"
    try:
        note = col.get_note(note_id)
    except Exception:
        return "nothing"

    target = render.context_html(item["sentence"], item["raw_word"], item["word_offset"])
    kept = [p for p in (note["Contexts"] or "").split(render.SEP) if p.strip() and p != target]
    if not kept:
        col.remove_notes([note_id])
        return "note"
    note["Contexts"] = render.SEP.join(kept)
    note["ContextsMasked"] = kept[-1].replace(
        f"<b>{html.escape(item['raw_word'])}</b>", "<b>____</b>"
    )
    col.update_note(note)
    return "context"
