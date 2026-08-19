import pytest

from app.services.romanization import (
    algorithmic_romanization,
    extract_kaikki_romanization,
    needs_romanization,
)


def test_latin_script_needs_nothing():
    assert needs_romanization("Latn") is False
    assert needs_romanization(None) is False
    assert needs_romanization("Cyrl") is True
    assert needs_romanization("Hani") is True


def test_forms_tagged_wins_and_skips_inflections():
    entry = {"forms": [
        {"form": "slova", "tags": ["romanization", "genitive", "singular"]},
        {"form": "slovo", "tags": ["romanization", "canonical"]},
    ]}
    assert extract_kaikki_romanization(entry, "Cyrl") == "slovo"


def test_korean_conjugation_table_row_is_rejected():
    # Modeled directly on the raw dump for 가능하다: the canonical form
    # carries {"romanization"} alone; every conjugated form carries a
    # "roman" key PLUS conjugation tags AND source="conjugation".
    entry = {"forms": [
        {"form": "ganeunghada", "tags": ["romanization"]},
        {"form": "가능해", "tags": ["indicative", "informal", "non-past"],
         "roman": "ganeunghae", "source": "conjugation"},
        {"form": "가능합니다",
         "tags": ["formal", "indicative", "non-past", "polite"],
         "roman": "ganeunghamnida", "source": "conjugation"},
    ]}
    assert extract_kaikki_romanization(entry, "Kore") == "ganeunghada"


def test_source_conjugation_rejected_even_without_matching_tag():
    entry = {"forms": [
        {"form": "x", "tags": ["romanization"], "source": "conjugation"},
        {"form": "canonical", "tags": ["romanization"]},
    ]}
    assert extract_kaikki_romanization(entry, "Kore") == "canonical"


def test_latin_script_entry_extracts_nothing():
    entry = {"forms": [{"form": "x", "tags": ["romanization"]}]}
    assert extract_kaikki_romanization(entry, "Latn") is None


def test_missing_raw_entry_is_none():
    assert extract_kaikki_romanization(None, "Cyrl") is None
    assert extract_kaikki_romanization({}, "Cyrl") is None


def test_overlong_value_is_discarded_not_truncated():
    entry = {"forms": [{"form": "a" * 500, "tags": ["romanization"]}]}
    assert extract_kaikki_romanization(entry, "Cyrl") is None


def test_whitespace_is_collapsed():
    entry = {"forms": [{"form": "  na  chalo ", "tags": ["romanization"]}]}
    assert extract_kaikki_romanization(entry, "Cyrl") == "na chalo"


def test_deva_uses_kaikki_directly_no_table_needed():
    # hi and sa both resolve through forms_tagged; no schwa-deletion logic
    # lives here at all -- Kaikki already gives the per-language convention.
    hi_entry = {"forms": [{"form": "kamal", "tags": ["romanization"]}]}
    sa_entry = {"forms": [{"form": "kamalam", "tags": ["romanization"]}]}
    assert extract_kaikki_romanization(hi_entry, "Deva") == "kamal"
    assert extract_kaikki_romanization(sa_entry, "Deva") == "kamalam"


# --- zh: Mandarin-gated sounds[] ---

def test_zh_pure_hokkien_entry_yields_nothing():
    # Modeled on the real first-3-lexemes dump for zh: every sound item is
    # Min-Nan/Hokkien/POJ/Tai-lo, zero Mandarin markers anywhere.
    entry = {"sounds": [
        {"tags": ["Min-Nan", "Hokkien", "POJ"], "zh_pron": "hoân-cho"},
        {"tags": ["Min-Nan", "Hokkien", "Xiamen", "Tai-lo"],
         "zh_pron": "huân-tso"},
        {"ipa": "/huan\u00b2\u2074\u207b\u00b2\u00b2 t\u0361so\u2074\u2074/",
         "tags": ["Min-Nan", "Hokkien", "Xiamen", "Sinological-IPA"]},
    ]}
    assert extract_kaikki_romanization(entry, "Hani") is None


def test_zh_explicit_mandarin_tag_extracted():
    entry = {"sounds": [
        {"tags": ["Mandarin", "Pinyin"], "zh_pron": "guāng"},
    ]}
    assert extract_kaikki_romanization(entry, "Hani") == "guāng"


def test_zh_mixed_topolect_and_mandarin_tags_on_same_item_rejected():
    # If a single sound item somehow carries both, do not trust it --
    # reject rather than guess which reading zh_pron actually is.
    entry = {"sounds": [
        {"tags": ["Mandarin", "Cantonese"], "zh_pron": "ambiguous"},
    ]}
    assert extract_kaikki_romanization(entry, "Hani") is None


def test_zh_cantonese_only_yields_nothing():
    entry = {"sounds": [
        {"tags": ["Cantonese", "Jyutping"], "zh_pron": "gwong1"},
    ]}
    assert extract_kaikki_romanization(entry, "Hani") is None


def test_zh_prefers_mandarin_item_even_when_topolect_items_precede_it():
    entry = {"sounds": [
        {"tags": ["Min-Nan", "Hokkien", "POJ"], "zh_pron": "kong"},
        {"tags": ["Standard-Chinese", "Pinyin"], "zh_pron": "guāng"},
    ]}
    assert extract_kaikki_romanization(entry, "Hani") == "guāng"


# --- algorithmic tables (mop-up only, not on the default backfill path) ---

CYRL_CASES = [
    ("свет", "svet"),
    ("щит", "shchit"),
    ("ёлка", "yolka"),
    ("мужчина", "muzhchina"),
    ("Москва", "Moskva"),
]


@pytest.mark.parametrize("lemma,expected", CYRL_CASES)
def test_cyrillic_table(lemma, expected):
    assert algorithmic_romanization(lemma, "Cyrl") == expected


GREK_CASES = [
    ("φως", "fos"),
    ("θάλασσα", "thalassa"),
    ("ψυχή", "psychi"),
    ("Ελλάδα", "Ellada"),
]


@pytest.mark.parametrize("lemma,expected", GREK_CASES)
def test_greek_table(lemma, expected):
    assert algorithmic_romanization(lemma, "Grek") == expected


def test_final_sigma_and_medial_sigma_agree():
    assert algorithmic_romanization("σ", "Grek") == "s"
    assert algorithmic_romanization("ς", "Grek") == "s"


@pytest.mark.parametrize("script", ["Deva", "Arab", "Hebr", "Jpan",
                                    "Hani", "Kore", "Latn", None])
def test_no_algorithmic_table_outside_cyrl_grek(script):
    # Deva included deliberately: even though Kaikki covers it well, this
    # function itself must still refuse to guess for it.
    assert algorithmic_romanization("क", script) is None


def test_unmapped_letter_aborts_rather_than_partially_romanizing():
    assert algorithmic_romanization("свет\u05d0", "Cyrl") is None


def test_separators_pass_through():
    assert algorithmic_romanization("нью-йорк", "Cyrl") == "nyu-york"