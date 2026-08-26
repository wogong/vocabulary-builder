"""VocabContext note type definition (spec §4.2).

Single source of truth for the note type. The server never creates or modifies
it at runtime (D3) — it only verifies the field list. The definition lives here
so scripts/make_notetype_apkg.py can generate an importable package for the
one-time manual setup in §8.4.
"""

NOTETYPE_NAME = "VocabContext"
CARD_NAME = "Context Recall"

# Order matters: Word is the first field and therefore Anki's dedup key (D4).
FIELDS = [
    "Word",
    "Phonetic",
    "Definition",
    "ContextDefinition",
    "Contexts",
    "ContextsMasked",
    "Translation",
    "Audio",
    "Source",
    "Meta",
]

# Falls back to Word when there is no context, otherwise notes collected
# without a sentence (§7.3) would generate no card at all.
FRONT = """\
{{#ContextsMasked}}<div class="ctx">{{ContextsMasked}}</div>{{/ContextsMasked}}
{{^ContextsMasked}}<div class="word">{{Word}}</div>{{/ContextsMasked}}
"""

BACK = """\
{{FrontSide}}

<hr id=answer>

<div class="word">{{Word}}</div>
{{#Phonetic}}<div class="phon">{{Phonetic}}</div>{{/Phonetic}}
{{#Audio}}<div class="audio">{{Audio}}</div>{{/Audio}}
{{#ContextDefinition}}<div class="def-ctx">{{ContextDefinition}}</div>{{/ContextDefinition}}
{{#Definition}}<div class="def">{{Definition}}</div>{{/Definition}}
{{#Contexts}}<div class="ctxs">{{Contexts}}</div>{{/Contexts}}
{{#Translation}}<div class="trans">{{Translation}}</div>{{/Translation}}
{{#Source}}<div class="src">{{Source}}</div>{{/Source}}
"""

CSS = """\
.card {
  font-family: -apple-system, "Segoe UI", "Noto Sans", sans-serif;
  font-size: 20px;
  line-height: 1.6;
  text-align: left;
  color: #1a1a1a;
  background: #fdfdfd;
  padding: 16px;
}
.ctx { font-size: 22px; }
.ctx b, .ctxs b { color: #c0392b; font-weight: 700; }
.word { font-size: 26px; font-weight: 700; margin-bottom: 2px; }
.phon { color: #7f8c8d; font-size: 17px; margin-bottom: 8px; }
.def-ctx { font-size: 19px; margin: 6px 0; }
.def { color: #34495e; font-size: 17px; margin: 6px 0; }
.ctxs { margin-top: 12px; font-size: 17px; color: #2c3e50; }
.ctxs hr { border: none; border-top: 1px dashed #ccc; margin: 8px 0; }
.trans { color: #7f8c8d; font-size: 16px; margin-top: 8px; }
.src { margin-top: 12px; font-size: 14px; }
.src a { color: #2980b9; text-decoration: none; }
.nightMode.card { color: #e6e6e6; background: #2f2f31; }
.nightMode .ctx b, .nightMode .ctxs b { color: #ff7b6b; }
.nightMode .phon, .nightMode .trans { color: #a0a0a0; }
.nightMode .def, .nightMode .ctxs { color: #cfd8dc; }
"""


class SchemaError(RuntimeError):
    """Raised when the collection's note type does not match this definition."""


def build_notetype(col):
    """Create the note type in `col`. Setup scripts only — never the server (D3)."""
    mm = col.models
    nt = mm.new(NOTETYPE_NAME)
    for name in FIELDS:
        mm.add_field(nt, mm.new_field(name))
    tmpl = mm.new_template(CARD_NAME)
    tmpl["qfmt"] = FRONT
    tmpl["afmt"] = BACK
    mm.add_template(nt, tmpl)
    nt["css"] = CSS
    mm.add(nt)
    return mm.by_name(NOTETYPE_NAME)


def verify_notetype(col, name=NOTETYPE_NAME):
    """Read-only check (D3). Returns the note type dict or raises SchemaError."""
    nt = col.models.by_name(name)
    if nt is None:
        raise SchemaError(
            f"first-time setup is not finished: note type {name!r} does not exist in "
            f"this collection. This is not a sync failure — the server refuses to "
            f"create note types itself (D3). Run one of:\n"
            f"  docker compose run --rm app python scripts/bootstrap_collection.py --seed-local\n"
            f"      create it here and push it to the sync server, or\n"
            f"  docker compose run --rm app python scripts/bootstrap_collection.py\n"
            f"      download a collection you already prepared in desktop Anki (§8.4)."
        )
    actual = [f["name"] for f in nt["flds"]]
    if actual != FIELDS:
        raise SchemaError(
            f"note type {name!r} field mismatch.\n"
            f"  expected: {FIELDS}\n"
            f"  actual:   {actual}\n"
            "Refusing to migrate automatically — a schema change forces a full sync "
            "and risks data loss (D3). Fix it manually in desktop Anki."
        )
    return nt
