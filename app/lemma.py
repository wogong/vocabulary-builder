"""Lemmatisation (spec §5.1, P1).

ECDICT's `exchange` column is the authority: it is curated, so when it says
"applications" reduces to "application" we take it. lemminflect only covers
words ECDICT does not have.
"""

from lemminflect import getAllLemmas

# Most specific first: a noun reading is the likeliest for a word someone
# double-clicked while reading.
POS_ORDER = ("NOUN", "VERB", "ADJ", "ADV", "PROPN", "AUX")


def normalize(word: str) -> str:
    return word.strip().casefold()


def lemmatize(word: str, dictionaries=None, lang: str = "en") -> str:
    """Reduce a surface form to the key we deduplicate on (D4)."""
    surface = normalize(word)
    if not surface:
        return surface

    # Chinese has no inflection to undo.
    from .dictionary import is_cjk

    if lang == "zh" or is_cjk(word):
        return word.strip()

    if dictionaries is not None:
        entry = dictionaries.lookup(surface, lang)
        if entry is not None:
            # An entry with a base form is an inflection; one without is itself
            # a headword and must be left alone.
            return normalize(entry.lemma) if entry.lemma else surface

    return _lemminflect(surface)


def _lemminflect(surface: str) -> str:
    lemmas = getAllLemmas(surface)
    if not lemmas:
        return surface
    # If the word is its own lemma under any reading, it is already a base
    # form. Without this, "better" becomes "good" and "whose" becomes "who".
    for candidates in lemmas.values():
        if surface in (c.casefold() for c in candidates):
            return surface
    for pos in POS_ORDER:
        if pos in lemmas and lemmas[pos]:
            return normalize(lemmas[pos][0])
    first = next(iter(lemmas.values()))
    return normalize(first[0]) if first else surface
