"""
Romanization derivation for non-Latin-script lexemes.

SOURCE: Kaikki only. extract_kaikki_romanization() reads Lexeme.raw_entry,
which holds the COMPLETE wiktextract entry (kaikki_english.py:35). This is
the ONLY source for every one of the 10 non-Latn scripts in this corpus --
see the Phase D findings log for the container census that established this
(romanization_census.py, 2026-08-18/19).

WHY NO ALGORITHMIC TABLES ARE WIRED IN BY DEFAULT: the original Phase D plan
(roadmap D2) expected Cyrl/Grek/Deva to need a character-level fallback for
material gaps. The census found forms_tagged alone at 99.3-100% coverage for
EVERY ONE of those scripts, including Deva (hi AND sa) -- which resolves the
schwa-deletion problem (Hindi drops the inherent vowel, Sanskrit doesn't) by
never needing a shared table in the first place: Kaikki gives each word's
romanization individually, already correctly conventioned per language.
algorithmic_romanization() still exists, tested, for Cyrl/Grek ONLY, as a
mop-up for the <0.1% gap forms_tagged leaves in those two scripts. It is not
called by the backfill's default (--source kaikki) path; Step 4/5 decide
whether it's worth invoking at all once real coverage numbers are in.

ZH IS SPECIAL: its only usable container is sounds[], and that array mixes
Mandarin pinyin with Hokkien/POJ/Tai-lo, Cantonese/Jyutping, and other
topolect readings with NO distinguishing container -- only tags. A raw dump
of the first 3 zh lexemes in the corpus showed ZERO Mandarin entries, all
Hokkien. _from_sounds_zh requires an explicit Mandarin marker and rejects the
whole sound item if ANY topolect tag is present, rather than falling back to
whatever reading happens to be first.

ZERO-REVIEW CONSTRAINT: a blank is acceptable, a wrong value is not. Any
script not covered here gets None, not a guess.
"""
from __future__ import annotations

import unicodedata

MAX_ROMANIZATION_LEN = 400

_ROMAN_TAGS = frozenset({"romanization", "romanisation", "transliteration"})

# Tags marking a form as an INFLECTED variant rather than the headword form.
# A romanization-tagged or roman-keyed form that ALSO carries one of these is
# the romanization of an inflection, not of the lemma.
#
# The Korean-specific tags (non-past/indicative/formal/informal/polite/
# sequential/conditional/causative/passive/honorific/plain) were added after
# inspecting real ko raw_entry dumps during Step 1/2: ko-conj/adj tables emit
# a "roman" key on EVERY conjugated form (가능해 -> ganeunghae, informal), not
# just the canonical one (가능하다 -> ganeunghada). Without these tags,
# forms_romankey's fallback path could pick an inflected reading over the
# dictionary form. Belt-and-braces: forms_tagged already resolves 100% of ko
# via the dedicated "romanization" tag on the canonical form specifically, so
# this fallback rarely fires for ko in practice -- but it must be correct
# when it does, for whichever future language relies on it as primary.
_INFLECTION_TAGS = frozenset({
    "plural", "singular", "dual", "genitive", "dative", "accusative",
    "nominative", "instrumental", "locative", "vocative", "prepositional",
    "partitive", "ablative", "oblique", "ergative", "construct",
    "past", "present", "future", "participle", "gerund", "infinitive",
    "imperative", "perfective", "imperfective", "aorist", "subjunctive",
    "comparative", "superlative", "definite", "indefinite", "possessive",
    "diminutive", "feminine", "masculine", "neuter", "inflection-template",
    "error-unknown-tag", "table-tags", "class",
    # Korean conjugation-table tags (ko-conj/adj), confirmed via raw dump:
    "non-past", "indicative", "formal", "informal", "polite", "sequential",
    "conditional", "causative", "passive", "honorific", "plain",
})

# A form dict carrying source == "conjugation" is a conjugation-table row
# regardless of what its tags say -- a second, independent signal to the
# same effect as _INFLECTION_TAGS, seen throughout the ko raw dump.
_INFLECTION_SOURCES = frozenset({"conjugation", "declension", "inflection"})

_HEAD_ARG_KEYS = ("tr", "rom", "roman", "romanization", "translit", "latin")

# zh-specific: a sound item counts as Mandarin ONLY if one of these appears
# in its tags, AND none of _TOPOLECT_TAGS appears anywhere in the same item.
_MANDARIN_TAGS = frozenset({
    "mandarin", "standard-chinese", "hanyu-pinyin", "pinyin", "putonghua",
})
_TOPOLECT_TAGS = frozenset({
    "min-nan", "hokkien", "cantonese", "jyutping", "yue", "hakka", "gan",
    "wu", "xiang", "min-dong", "min-bei", "poj", "tai-lo", "taiwanese",
    "teochew", "shanghainese",
})
_SOUND_SKIP_KEYS = frozenset({"tags", "ipa", "audio", "raw_tags", "note",
                              "homophone", "rhymes", "hyphenation",
                              "ogg_url", "mp3_url", "wav_url"})


# ---------------------------------------------------------------- helpers

def _is_inflected(form_or_sound: dict) -> bool:
    tags = {str(t).lower() for t in (form_or_sound.get("tags") or [])}
    if tags & _INFLECTION_TAGS:
        return True
    source = str(form_or_sound.get("source") or "").lower()
    return source in _INFLECTION_SOURCES


def _clean(value: str | None) -> str | None:
    if not value:
        return None
    out = " ".join(str(value).split())
    if not out:
        return None
    # Guard, not truncation: an over-length value is DISCARDED. A truncated
    # romanization is a wrong romanization, forbidden under zero-review.
    if len(out) > MAX_ROMANIZATION_LEN:
        return None
    return out


# ---------------------------------------------------------------- container
# probes (shared, non-zh)

def _from_forms_tagged(entry: dict) -> str | None:
    for form in entry.get("forms") or []:
        tags = {str(t).lower() for t in (form.get("tags") or [])}
        if not (tags & _ROMAN_TAGS):
            continue
        if _is_inflected(form):
            continue
        value = form.get("form")
        if value:
            return str(value)
    return None


def _from_forms_roman_key(entry: dict) -> str | None:
    for form in entry.get("forms") or []:
        if _is_inflected(form):
            continue
        if form.get("roman"):
            return str(form["roman"])
    return None


def _from_head_templates(entry: dict) -> str | None:
    for head in entry.get("head_templates") or []:
        args = head.get("args") or {}
        for key in _HEAD_ARG_KEYS:
            if args.get(key):
                return str(args[key])
    return None


def _from_sounds_zh(entry: dict) -> str | None:
    """zh ONLY. Requires an explicit Mandarin tag; rejects any sound item
    carrying a topolect tag, even alongside a Mandarin one, rather than risk
    picking the wrong reading out of a mixed-tag item."""
    for sound in entry.get("sounds") or []:
        tags = {str(t).lower() for t in (sound.get("tags") or [])}
        if tags & _TOPOLECT_TAGS:
            continue
        if not (tags & _MANDARIN_TAGS):
            continue
        for key, value in sound.items():
            if key.lower() in _SOUND_SKIP_KEYS:
                continue
            if isinstance(value, str) and value:
                return value
    return None


_PROBES = {
    "forms_tagged": _from_forms_tagged,
    "forms_romankey": _from_forms_roman_key,
    "head_templates": _from_head_templates,
    "sounds_mandarin": _from_sounds_zh,
}

# Evidence-driven, from romanization_census.py (Phase D findings log,
# 2026-08-18/19 census of 20,000 lexemes/language). Order = preference.
# top_roman appears nowhere: 0.0% across all 10 languages, dropped entirely.
_PREFERENCE: dict[str, tuple[str, ...]] = {
    "Cyrl": ("forms_tagged", "head_templates", "forms_romankey"),   # ru: 99.9%
    "Grek": ("forms_tagged", "forms_romankey"),                      # el: 100.0%
    "Deva": ("forms_tagged", "head_templates", "forms_romankey"),   # hi 99.9% / sa 99.3%
    "Arab": ("forms_tagged", "head_templates", "forms_romankey"),   # ar 99.9% / fa 99.6%
    "Hebr": ("forms_tagged", "head_templates", "forms_romankey"),   # he: 99.5%
    "Jpan": ("forms_tagged", "forms_romankey"),                      # ja: 99.7%
    "Kore": ("forms_tagged", "forms_romankey"),                      # ko: 100.0%
    "Hani": ("sounds_mandarin",),                                    # zh: Mandarin-gated
}


def needs_romanization(script: str | None) -> bool:
    """Non-Latin script => the written form is unreadable to a Latin reader.

    Derived from Language.script exactly as languages.py derives `rtl`, so a
    future import self-classifies with no code change here.
    """
    return script is not None and script != "Latn"


def extract_kaikki_romanization(
    raw_entry: dict | None,
    script: str | None,
) -> str | None:
    """Best Kaikki-supplied romanization for this entry, or None."""
    if not raw_entry or not needs_romanization(script):
        return None
    for name in _PREFERENCE.get(script or "", ()):
        got = _clean(_PROBES[name](raw_entry))
        if got:
            return got
    return None


# ------------------------------------------------------- algorithmic tables
#
# NOT called by the default backfill path (--source kaikki). Kaikki alone
# covers 99.3-100% of every script that has a table here, so this is a
# deliberately small, tested mop-up -- invoked only via --source algorithmic
# / --source both if Step 4's real coverage numbers show it's worth it.
# See the module docstring for why Deva has no table despite being tier-1
# in the original roadmap plan.

_CYRL: dict[str, str] = {
    "а": "a", "б": "b", "в": "v", "г": "g", "д": "d", "е": "e", "ё": "yo",
    "ж": "zh", "з": "z", "и": "i", "й": "y", "к": "k", "л": "l", "м": "m",
    "н": "n", "о": "o", "п": "p", "р": "r", "с": "s", "т": "t", "у": "u",
    "ф": "f", "х": "kh", "ц": "ts", "ч": "ch", "ш": "sh", "щ": "shch",
    "ъ": "", "ы": "y", "ь": "", "э": "e", "ю": "yu", "я": "ya",
}

_GREK: dict[str, str] = {
    "α": "a", "β": "v", "γ": "g", "δ": "d", "ε": "e", "ζ": "z", "η": "i",
    "θ": "th", "ι": "i", "κ": "k", "λ": "l", "μ": "m", "ν": "n", "ξ": "x",
    "ο": "o", "π": "p", "ρ": "r", "σ": "s", "ς": "s", "τ": "t", "υ": "y",
    "φ": "f", "χ": "ch", "ψ": "ps", "ω": "o",
}

_TABLES: dict[str, dict[str, str]] = {
    "Cyrl": _CYRL,
    "Grek": _GREK,
}


def _map_char(ch: str, table: dict[str, str]) -> str | None:
    """One character -> its table mapping, or None if unmapped.

    Direct lookup FIRST, not NFD-decomposed lookup first. This matters
    because some Cyrillic letters -- notably ё (U+0401) and й (U+0439) --
    have a CANONICAL Unicode decomposition (е+combining-diaeresis,
    и+combining-breve respectively) despite being their own distinct letters
    in the language. Decomposing before lookup would silently collapse them
    to е/и (confirmed by test failure: "ёлка" -> "elka" instead of "yolka",
    "й" in "нью-йорк" -> "i" instead of "y").

    Falling back to the NFD base letter is still correct for Greek, where
    accented vowels (ά, ή, ...) genuinely SHOULD lose their accent for a
    plain transliteration and are decomposition products, not distinct
    letters -- confirmed by test_greek_table's accented cases.
    """
    lower = ch.lower()
    if lower in table:
        mapped = table[lower]
        return mapped.title() if ch.isupper() and mapped else mapped

    decomposed = unicodedata.normalize("NFD", ch)
    base = decomposed[0] if decomposed else ch
    if (base.lower() in table
            and all(unicodedata.category(c) == "Mn" for c in decomposed[1:])):
        mapped = table[base.lower()]
        return mapped.title() if base.isupper() and mapped else mapped

    return None


def algorithmic_romanization(lemma: str, script: str | None) -> str | None:
    """Character-level romanization, ONLY for Cyrl/Grek. Returns None for
    every other script, including Deva -- see module docstring."""
    table = _TABLES.get(script or "")
    if not table or not lemma:
        return None

    out: list[str] = []
    for ch in lemma:
        # A combining mark that survives to here (not absorbed into a
        # precomposed letter above) is a genuine standalone diacritic, e.g.
        # an explicit stress accent some dictionary headwords carry. Drop it.
        if unicodedata.category(ch) == "Mn":
            continue
        mapped = _map_char(ch, table)
        if mapped is not None:
            out.append(mapped)
        elif not ch.isalpha():
            out.append(ch)
        else:
            return None   # unmapped letter -> abort, ship blank

    return _clean("".join(out))