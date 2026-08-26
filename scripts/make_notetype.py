"""Generate an .apkg containing the VocabContext note type (spec §8.4 step 1).

Import the result into desktop Anki, adjust the card template and styling to
taste, then sync it up. The server only ever verifies this note type — it never
creates or migrates one (D3).

    python scripts/make_notetype.py [out.apkg]
"""

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from anki.collection import Collection, ExportAnkiPackageOptions  # noqa: E402

from app.notetype import NOTETYPE_NAME, build_notetype  # noqa: E402

DECK = "Vocab::Reading"


def main():
    out = Path(sys.argv[1] if len(sys.argv) > 1 else "VocabContext.apkg").resolve()
    with tempfile.TemporaryDirectory() as tmp:
        col = Collection(str(Path(tmp) / "seed.anki2"))
        notetype = build_notetype(col)

        # One sample note so the note type survives the import and you can see
        # the card design immediately. Delete it in Anki afterwards.
        note = col.new_note(notetype)
        note["Word"] = "ubiquitous"
        note["Phonetic"] = "/juːˈbɪkwɪtəs/"
        note["Definition"] = "adj. 无处不在的；普遍存在的"
        note["Contexts"] = "Smartphones are now <b>ubiquitous</b> in daily life."
        note["ContextsMasked"] = "Smartphones are now <b>____</b> in daily life."
        note["Translation"] = "智能手机现在无处不在。"
        note["Source"] = '<a href="https://example.com">示例来源</a>'
        note["Meta"] = '{"item_ids": []}'
        col.add_note(note, col.decks.id(DECK))

        col.export_anki_package(
            out_path=str(out),
            options=ExportAnkiPackageOptions(
                with_scheduling=False, with_media=False, legacy=False
            ),
            limit=None,
        )
        col.close()

    print(f"wrote {out}")
    print(f"next: import it into desktop Anki, check the '{NOTETYPE_NAME}' note type")
    print(f"      and the '{DECK}' deck, then sync to your sync server.")


if __name__ == "__main__":
    main()
