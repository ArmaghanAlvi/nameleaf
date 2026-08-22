"""
Derivation rules for green cards (established names) -- Stage 3 of
ESTABLISHED_NAMES_ROADMAP.md.

PURE FUNCTIONS ONLY. Nothing here opens a session, reads a file or writes a
row; scripts/populate_established_names.py does all of that. The split keeps
every rule below unit-testable without Postgres.

WHY THE STAGE-1 PROBE LOGIC MOVED HERE. The probes measured the corpus with
these exact regexes, and the shipped feature must classify it with the SAME
ones or the N1/N2/N3 census stops describing what actually shipped. The
obvious way to guarantee that -- have the service import from
scripts/prune/*.py -- inverts the dependency (app/ must never depend on
scripts/) and drags app.db.session in through the probes' module-level
imports. So the code moved DOWN into app/ and the probes now re-export from
here. Part A below is a verbatim move: byte-for-byte the Stage-1 logic, no
behavioural edits. Part B is new Stage-3 code built on top of it.

SOURCE OF TRUTH FOR TYPE AND GENDER IS `senses.categories`, NOT `raw_tags`.
raw_tags carries GRAMMATICAL gender in inflecting languages, which is not the
name's gender: reading it first produced 593 male / 2,447 female German
surnames, a number that cannot be true. The Wiktionary category strings
("German surnames", "Polish female surnames", "Icelandic patronymics") state
the fact directly and in the same shape in every language. Note that Part A's
gender_of() still accepts tags, because the probes pass them and their output
must not move; production calls gender_from_head() instead.

The category parser is a PATTERN, not a list. It requires the category to
read "<this language's English name> [modifiers] <type> [from ...]", so
language #22 self-classifies with no edit here -- and "English terms derived
from given names" is rejected, because its modifier run is not a gender and
not an allowed modifier.
"""
from __future__ import annotations

import ast
import re

from collections.abc import Iterable
from app.utils.text import normalize_lemma

# ===========================================================================
# PART A -- verbatim move from the Stage-1 probes. Do not edit these rules
# without re-running the N1/N2/N3 census: the published numbers are theirs.
# ===========================================================================

# --- from scripts/prune/name_inventory_probe.py (N1) -----------------------

_PLACE_WORDS = (
    "city|town|village|hamlet|river|mountain|lake|island|state|province|"
    "country|county|region|district|commune|municipality|borough|suburb|"
    "neighborhood|neighbourhood|census-designated place|unincorporated|"
    "placename|place name|place-name|locality|locale|civil parish|parish|"
    "prefecture|ward|settlement|community|ghost town|local government area"
)

_RX = {
    "form_of": re.compile(
        r"^(?:nominative|genitive|dative|accusative|ablative|vocative|"
        r"locative|instrumental|oblique)\b.{0,40}\bof\b"
        r"|^(?:alternative|variant|obsolete|archaic) (?:form|spelling) of\b"
        r"|\b(?:singular|plural|definite|indefinite) of\b"
        r"|^(?:inflection|romanization|transliteration) of\b",
        re.IGNORECASE,
    ),
    "given": re.compile(
        r"\bgiven name\b|\bfirst name\b|\bforename\b"
        r"|^an? (?:male|female|unisex|masculine|feminine) name\b",
        re.IGNORECASE,
    ),
    "surname": re.compile(
        r"\bsurname\b|\bfamily name\b|\blast name\b", re.IGNORECASE,
    ),
    "patronymic": re.compile(r"\b(?:patronymic|matronymic)\b", re.IGNORECASE),
    "place": re.compile(
        rf"^an? (?:[a-z-]+ ){{0,3}}(?:{_PLACE_WORDS})\b"
        rf"|^an? place (?:in|of)\b"
        rf"|^a number of places\b"
        rf"|^the capital (?:city )?of\b"
        rf"|\((?:a|an|the) (?:{_PLACE_WORDS})\b",
        re.IGNORECASE,
    ),
    "diminutive": re.compile(
        r"\b(?:diminutive|pet form|short form|hypocorism|hypocoristic"
        r"|nickname)\b",
        re.IGNORECASE,
    ),
    # Gender is read from the HEAD PHRASE only. A loose \bfemale\b scan is
    # wrong on the systematic de/es shape "a male given name, feminine
    # equivalent Daniela" — both words are present and the loose reading
    # collapses it to unknown.
    "female_head": re.compile(
        r"\b(?:female|feminine)\s+(?:\w+\s+){0,2}?(?:given\s+)?name\b",
        re.IGNORECASE,
    ),
    "male_head": re.compile(
        r"\b(?:male|masculine)\s+(?:\w+\s+){0,2}?(?:given\s+)?name\b",
        re.IGNORECASE,
    ),
    "unisex_head": re.compile(
        r"\b(?:unisex|epicene)\s+(?:\w+\s+){0,2}?(?:given\s+)?name\b",
        re.IGNORECASE,
    ),
}

BUCKETS = ("FORM_OF", "GIVEN", "SURNAME", "PATRONYMIC", "PLACE", "OTHER")


def classify_name_type(gloss: str, tags: list[str] | None = None) -> str:
    """
    Bucket ONE name sense. Priority order is deliberate:

      FORM_OF first  — an inflected/variant citation is not a name row at all,
                       regardless of what it inflects.
      GIVEN, SURNAME — the two shipping buckets; GIVEN wins a tie because
                       "a surname, also a given name" should surface as a
                       given name with the surname flag, not the reverse.
      PATRONYMIC     — only when neither of the above fired, so
                       "a surname, patronymic of X" stays SURNAME.
      PLACE          — toponyms; measured so they can be excluded knowingly.
      OTHER          — deities, organisations, brands, taxonomy, everything
                       else. Expected to be large; that is the point.
    """
    g = (gloss or "").strip()
    tagset = {str(t).strip().lower() for t in (tags or [])}
    if not g:
        return "OTHER"
    if tagset & {"form-of", "alt-of", "alternative"} or _RX["form_of"].search(g):
        return "FORM_OF"
    if _RX["given"].search(g):
        return "GIVEN"
    if _RX["surname"].search(g):
        return "SURNAME"
    if _RX["patronymic"].search(g):
        return "PATRONYMIC"
    if _RX["place"].search(g):
        return "PLACE"
    return "OTHER"


def gender_of(gloss: str, tags: list[str] | None = None) -> str:
    """
    m / f / x (unisex) / u (unknown). Read from the head phrase, and from the
    FIRST such phrase only — "a male given name, feminine equivalent Daniela"
    is a male name, not an ambiguous one.
    """
    tagset = {str(t).strip().lower() for t in (tags or [])}
    if "feminine" in tagset or "female" in tagset:
        return "f"
    if "masculine" in tagset or "male" in tagset:
        return "m"
    g = gloss or ""
    hits = []
    for label, key in (("f", "female_head"), ("m", "male_head"),
                       ("x", "unisex_head")):
        mt = _RX[key].search(g)
        if mt:
            hits.append((mt.start(), label))
    if not hits:
        return "u"
    hits.sort()
    return hits[0][1]


def is_diminutive(gloss: str) -> bool:
    return bool(_RX["diminutive"].search(gloss or ""))


def both_given_and_surname(gloss: str) -> bool:
    g = gloss or ""
    return bool(_RX["given"].search(g)) and bool(_RX["surname"].search(g))


SHIPPING_BUCKETS: tuple[str, ...] = ("GIVEN", "SURNAME", "PATRONYMIC")

_TYPE_ARG_TO_BUCKET: dict[str, tuple[str, ...]] = {
    "given": ("GIVEN",),
    "surname": ("SURNAME",),
    "patronymic": ("PATRONYMIC",),
}

# CLI choices every type-aware probe should use, so a new type is added in
# exactly one place.
NAME_TYPE_CHOICES: tuple[str, ...] = ("given", "surname", "patronymic", "all")


def shipping_types(name_type_arg: str) -> tuple[str, ...]:
    """
    Resolve a --name-type CLI value to the classify_name_type() bucket(s) it
    selects. 'all' is NOT accepted here and never means "combined": callers
    that accept 'all' must loop over expand_type_arg() and keep the reports
    SEPARATE, so the populations are never blended in one count.

    PATRONYMIC added in Stage 1d. Every probe before that silently excluded
    is (71), la (27) and ru (27) -- real populations that no census had seen.
    """
    try:
        return _TYPE_ARG_TO_BUCKET[name_type_arg]
    except KeyError:
        raise ValueError(
            f"shipping_types() takes one of "
            f"{sorted(_TYPE_ARG_TO_BUCKET)}, got {name_type_arg!r}. "
            "For 'all', call expand_type_arg() and keep the reports separate."
        ) from None


def expand_type_arg(name_type_arg: str) -> tuple[str, ...]:
    """
    Expand a --name-type CLI value into the SEQUENCE of single-type args a
    caller should loop over. 'all' now includes patronymic; that is a
    deliberate behaviour change, since 'all' previously meant
    'given + surname' and quietly dropped a third real population.
    """
    if name_type_arg == "all":
        return ("given", "surname", "patronymic")
    return (name_type_arg,)


# --- from scripts/prune/name_meaning_probe.py (N2) --------------------------

_OPEN = "\"“'‘«"
_CLOSE = "\"”'’»"

MEANING_MARKER = re.compile(
    rf"(?:meaning|literally|lit\.)\s*[{_OPEN}]([^{_CLOSE}]{{1,80}})[{_CLOSE}]",
    re.IGNORECASE,
)

EQUIV_EN = re.compile(
    r"\bequivalent to English\s+([A-Z][\w''\u2019-]*)",
)

# Apostrophes are deliberately NOT delimiters here: "don't ... isn't" would
# otherwise manufacture a quoted span ("t ... isn"). Only real quote marks.
ANY_QUOTED = re.compile(
    r"[\"“«]([^\"”»]{2,80})[\"”»]",
)

# ---------------------------------------------------------------------------
# ETYM_QUOTED repair (Stage 1c).
#
# The pre-1c extractor took the FIRST quoted span anywhere in etymology_text.
# Three documented failure modes:
#   (1) dithematic truncation -- 'from ead ("wealth") + weard ("guardian")'
#       yielded only "wealth", silently halving every compound name.
#   (2) unrelated first-quote capture -- a leading 'See "Avon".' or
#       'Compare the English "Smith".' produced a gloss from a cross-reference
#       rather than from a derivation.
#   (3) stem/continuative boilerplate -- ja/zh etymologies are dominated by
#       morphological notes that are not meanings at all.
#
# The repair: first sentence only, require a derivation anchor OUTSIDE the
# quoted material, concatenate every anchored span, drop boilerplate spans,
# and gate ja/zh off entirely. Every gate fails CLOSED -- blank over wrong.
# ---------------------------------------------------------------------------

ETYM_QUOTED_BLOCKED_LANGS = frozenset({"ja", "zh"})

# Narrow quote set, matching ANY_QUOTED's rationale: apostrophes are NOT
# delimiters, or "don't ... isn't" manufactures a span.
_ETYM_OPEN = "\"“«"
_ETYM_CLOSE = "\"”»"

_SENT_SPLIT = re.compile(
    r"(?<=[.!?])\s+(?=[A-Z\u0370-\u03FF\u0400-\u04FF])"
)
_TRAILING_ABBREV = re.compile(
    r"\b(?:cf|e\.g|i\.e|lit|c|ca|fl|Mr|St)\.$", re.IGNORECASE
)

# Anchor vocabulary. Deliberately EXCLUDES a bare "of": a draft of this used
# it and the gate passed on 'defender of men' -- the anchor matched a word
# INSIDE the gloss it was supposed to be validating, which is why the anchor
# is tested against the sentence with quotes and parens stripped out.
_DERIVATION_ANCHOR = re.compile(
    r"\b(?:from|derived from|borrowed from|inherited from|cognate with|"
    r"calque of|contraction of|composed of|compound of|equivalent to|via)\b",
    re.IGNORECASE,
)

_PAREN_SPAN = re.compile(r"\(([^()]{0,300})\)")
_QUOTED_SPAN = re.compile(
    rf"[{_ETYM_OPEN}]([^{_ETYM_CLOSE}]{{1,80}})[{_ETYM_CLOSE}]"
)
_STRIP_PAREN = re.compile(r"\([^()]{0,300}\)")
_STRIP_QUOTED = re.compile(
    rf"[{_ETYM_OPEN}][^{_ETYM_CLOSE}]{{1,80}}[{_ETYM_CLOSE}]"
)

_ETYM_BOILERPLATE = re.compile(
    r"^(?:stem|continuative|attributive|conjunctive|combining|inflect\w*|"
    r"perfective|imperfective|classical|literary|colloquial|honorific)\b"
    r"|^(?:a |an |the )?(?:form|variant|spelling|reading|romani[sz]ation|"
    r"transliteration|transcription|abbreviation|contraction)\b"
    r"|\bof the (?:verb|noun|adjective|name)\b"
    r"|^(?:see|cf\.?|compare)\b"
    r"|^(?:given name|surname|personal name|a name)\b",
    re.IGNORECASE,
)

DIAGNOSTIC_CHANNELS = ("ETYM_QUOTED_LEGACY",)


def _etym_first_sentence(text: str) -> str:
    """First sentence, re-joining across common abbreviation false splits."""
    t = (text or "").strip()
    if not t:
        return ""
    parts = _SENT_SPLIT.split(t)
    out = parts[0]
    i = 1
    while i < len(parts) and _TRAILING_ABBREV.search(out):
        out = f"{out} {parts[i]}"
        i += 1
    return out


def _etym_span_ok(span: str) -> str | None:
    s = span.strip().strip(",;:").strip()
    if not s or len(s) > 80:
        return None
    if _ETYM_BOILERPLATE.search(s):
        return None
    if not re.search(r"[A-Za-z]", s):
        return None
    return s

CHANNELS = ("GLOSS_MEANING", "GLOSS_EQUIV_EN", "ETYM_MARKER",
            "ETYM_QUOTED", "HOMOGRAPH", "NONE")


def extract_gloss_meaning(gloss: str) -> str | None:
    m = MEANING_MARKER.search(gloss or "")
    return m.group(1).strip() if m else None


def extract_equiv_en(gloss: str) -> str | None:
    m = EQUIV_EN.search(gloss or "")
    return m.group(1).strip() if m else None


def extract_etym_marker(etym: str) -> str | None:
    m = MEANING_MARKER.search(etym or "")
    return m.group(1).strip() if m else None


def extract_etym_quoted_legacy(etym: str) -> str | None:
    """
    PRE-1c behaviour: first quoted span anywhere in the etymology. Retained
    ONLY so the repair can be priced against it in the same pass. Not a
    shipping channel.
    """
    m = ANY_QUOTED.search(etym or "")
    return m.group(1).strip() if m else None


def extract_etym_quoted(etym: str, lang_code: str) -> str | None:
    """
    Repaired ETYM_QUOTED (Stage 1c): anchored, first-sentence-only,
    multi-span, boilerplate-filtered, ja/zh gated off.

    Returns spans joined with ' + ' so a dithematic name reads
    "wealth, fortune + guardian" rather than losing its second element.
    Capped at 4 spans: beyond that the etymology is a chain, not a compound.
    """
    if lang_code in ETYM_QUOTED_BLOCKED_LANGS:
        return None
    sent = _etym_first_sentence(etym)
    if not sent:
        return None

    # Test the anchor against the sentence with parentheticals and quoted
    # material removed, so an anchor word appearing inside a gloss cannot
    # satisfy the gate that is meant to validate that gloss.
    bare = _STRIP_QUOTED.sub(" ", _STRIP_PAREN.sub(" ", sent))
    if not _DERIVATION_ANCHOR.search(bare):
        return None

    spans: list[str] = []
    seen: set[str] = set()

    def _add(raw: str) -> None:
        v = _etym_span_ok(raw)
        if v and v.casefold() not in seen:
            seen.add(v.casefold())
            spans.append(v)

    for m in MEANING_MARKER.finditer(sent):
        _add(m.group(1))
    for pm in _PAREN_SPAN.finditer(sent):
        for qm in _QUOTED_SPAN.finditer(pm.group(1)):
            _add(qm.group(1))

    if not spans:
        return None
    return " + ".join(spans[:4])


# --- from scripts/prune/name_join_probe.py (N3) -----------------------------

# Function words plus the boilerplate vocabulary of name glosses/etymologies.
# Not a linguistic stoplist: these are the tokens that, if left in, join to
# everything and make the flood report unreadable.
STOP = frozenset("""
a an the of and or to in from for with by on at as is are was were be been
this that these those it its his her their our your my he she they we you i
also see used use using such more most very not no
name names given surname surnames family first last male female masculine
feminine unisex form forms variant variants spelling diminutive short pet
equivalent english meaning literally lit means derived derivative cognate
son daughter child born place city town river god goddess mythology saint
one two three ancient modern old new common word words letter
""".split())

TOKEN_RX = re.compile(r"[^\W\d_]+", re.UNICODE)


def content_tokens(text: str) -> list[str]:
    return [
        t for t in (m.group(0).lower() for m in TOKEN_RX.finditer(text or ""))
        if len(t) > 2 and t not in STOP
    ]


# ===========================================================================
# PART B -- new Stage-3 derivation, built on Part A.
# ===========================================================================

# ---------------------------------------------------------------------------
# 3a -- type and gender from `categories`
# ---------------------------------------------------------------------------

def category_names(categories: Iterable[object] | None) -> list[str]:
    """
    `senses.categories` -> the plain category strings.

    The stored shape is stranger than list[dict] OR list[str]: each element
    is a STRING containing the Python repr of a dict --
    "{'name': 'German surnames', 'kind': 'other', ...}" -- not a real dict
    and not valid JSON (single quotes). Confirmed against a live Icelandic
    row: isinstance(item, dict) is False for every element, so a naive
    str-or-dict check silently appends the WHOLE repr as the category name,
    which is why the category predicate resolved 0% of every language until
    this was found.

    ast.literal_eval safely parses the repr back into a real dict (it will
    not execute arbitrary code, unlike eval). If a row is ever a genuine
    dict already, or a plain string, both are handled too, since a future
    import pass may fix the underlying shape without warning.
    """
    out: list[str] = []
    for item in categories or []:
        parsed = item
        if isinstance(item, str):
            try:
                parsed = ast.literal_eval(item)
            except (ValueError, SyntaxError):
                parsed = None
        if isinstance(parsed, dict):
            value = parsed.get("name")
            if value:
                out.append(str(value))
        elif isinstance(item, str) and item and parsed is None:
            # A plain string that wasn't a dict repr at all.
            out.append(item)
    return out


_CATEGORY_TYPE_TO_BUCKET: dict[str, str] = {
    "given names": "GIVEN",
    "forenames": "GIVEN",
    "surnames": "SURNAME",
    "family names": "SURNAME",
    # Latin: 1,131 name senses sit under "Latin nomina gentilia", and Latin
    # has no "Latin surnames" category at all. A gens name IS a family name,
    # and the N1 gloss classifier already buckets these SURNAME off the
    # phrase 'nomen gentile, gens or "family name"' -- so this agrees with
    # the census rather than making a new claim.
    "nomina gentilia": "SURNAME",
    "patronymic surnames": "PATRONYMIC",
    "matronymic surnames": "PATRONYMIC",
    "patronymics": "PATRONYMIC",
    "matronymics": "PATRONYMIC",
}

_CATEGORY_GENDER: dict[str, str] = {
    "male": "m", "masculine": "m",
    "female": "f", "feminine": "f",
    "unisex": "x", "epicene": "x",
}

# Non-gender words allowed between the language name and the type. Seeded
# ONLY from a category string the census actually showed ("Russian possessive
# surnames", 520 senses). Anything else FAILS CLOSED and is reported by
# name_category_census.py as a modifier candidate -- a word is added here
# against evidence, never in advance.
_CATEGORY_MODIFIERS: frozenset[str] = frozenset({"possessive"})

# Filled from the Step-7 census ONLY where a category's language prefix
# genuinely differs from Language.name. Empty until evidence says otherwise:
# an alias invented in advance is a guess, and a wrong one misfiles a whole
# language silently. Step 7 confirmed zero real aliases exist in this
# corpus -- every ALIAS-flagged prefix traced back to either a two-word
# Language.name the census's diagnostic-only prefix probe split incorrectly
# (Old English, Old Norse) or a foreign-origin cross-tag category sitting
# alongside one that already resolved. This stays empty by evidence.
CATEGORY_LANGUAGE_ALIASES: dict[str, tuple[str, ...]] = {}

def _language_prefixes(language_name: str) -> list[str]:
    names = [(language_name or "").casefold()]
    names.extend(a.casefold()
                 for a in CATEGORY_LANGUAGE_ALIASES.get(language_name, ()))
    return [n for n in names if n]

# Ordered alternation, longest-first, shared by every category-tail pattern
# below (the closed-vocabulary path and the connector path alike) -- one
# definition, so a type added to _CATEGORY_TYPE_TO_BUCKET is recognized
# everywhere with no second edit.
_TYPE_ALTERNATION = "|".join(
    sorted((re.escape(k) for k in _CATEGORY_TYPE_TO_BUCKET),
          key=len, reverse=True)
)

_CATEGORY_TAIL_RX = re.compile(
    r"^(?P<mods>(?:[\w'\u2019-]+\s+)*?)"
    r"(?P<type>" + _TYPE_ALTERNATION + r")"
    r"(?:\s+(?:from|of|in|derived\s+from)\b.*)?$",
    re.IGNORECASE,
)

# Step-7 census finding: a large, structurally DIFFERENT category shape --
# "<Lang> renderings of <SourceLang> [gender] <type>" (also seen under
# "diminutives of" / "augmentatives of") -- is the DOMINANT category form in
# zh (~93% of its shipping population sat here, unresolved) and material in
# ko/ru/el. SourceLang is an OPEN set (any language name, one or several
# words -- "English", "Ancient Greek"), unlike the closed gender/modifier
# vocabulary the plain path validates word-by-word, so it cannot go through
# _CATEGORY_MODIFIERS: enumerating every language name defeats the point of
# a category-level predicate. Once a recognized connector phrase is seen,
# everything up to the final gender/type is accepted unconditionally and
# discarded -- fine, since nothing downstream reads what stood in for the
# source language, only the bucket and gender at the tail.
_CATEGORY_CONNECTORS: tuple[str, ...] = (
    "renderings of ", "diminutives of ", "augmentatives of ",
)

_OPEN_TAIL_RX = re.compile(
    r"^(?:[\w'\u2019-]+\s+)*?"
    r"(?:(?P<gender>male|female|unisex|masculine|feminine|epicene)\s+)?"
    r"(?P<type>" + _TYPE_ALTERNATION + r")$",
    re.IGNORECASE,
)


def parse_name_category(
    category: str,
    language_name: str,
) -> tuple[str, str] | None:
    """
    One category string -> (bucket, gender), or None when it is not a name
    category for THIS language. gender is "u" when the category states none.

    Requiring the string to START with this language's own English name is
    what makes the predicate safe: "English terms derived from given names"
    ends in a type phrase, but its modifier run is ("terms", "derived",
    "from") -- none a gender or an allowed modifier -- so it fails closed.

    Two shapes are recognized after that language prefix:
      * a CONNECTOR shape ("renderings of ...", "diminutives of ...") where
        everything between the connector and the trailing [gender] type is
        accepted without word-by-word validation, since it names another
        language rather than drawing from a fixed vocabulary.
      * the plain shape, where every word between the language prefix and
        the trailing [gender] type MUST be a recognized gender word or an
        evidence-admitted modifier, or the category is rejected.
    """
    text = " ".join((category or "").split())
    lowered = text.casefold()
    rest = None
    for prefix in _language_prefixes(language_name):
        if lowered.startswith(prefix + " "):
            rest = text[len(prefix) + 1:]
            break
    if rest is None:
        return None

    rest_lower = rest.casefold()
    for connector in _CATEGORY_CONNECTORS:
        if rest_lower.startswith(connector):
            m = _OPEN_TAIL_RX.match(rest[len(connector):])
            if not m:
                return None
            gender = _CATEGORY_GENDER.get(
                (m.group("gender") or "").casefold(), "u"
            )
            return _CATEGORY_TYPE_TO_BUCKET[m.group("type").casefold()], gender

    m = _CATEGORY_TAIL_RX.match(rest)
    if not m:
        return None

    gender = "u"
    for word in m.group("mods").split():
        word = word.casefold()
        if word in _CATEGORY_GENDER:
            gender = _CATEGORY_GENDER[word]
        elif word not in _CATEGORY_MODIFIERS:
            return None
    return _CATEGORY_TYPE_TO_BUCKET[m.group("type").casefold()], gender


# Same priority as classify_name_type: GIVEN beats SURNAME beats PATRONYMIC.
_BUCKET_PRIORITY: dict[str, int] = {"GIVEN": 0, "SURNAME": 1, "PATRONYMIC": 2}


def reduce_gender(genders) -> str:
    """
    Collapse several gender readings of the SAME (lemma, type) into one.

      * "u" never outvotes a definite reading -- it means "not stated here",
        not "stated to be unknown".
      * m together with f is x, not a coin flip: a lemma attested as both a
        male and a female name in one language IS used for both, and unisex
        is the honest rendering.
      * an explicit x wins outright.
    """
    values = {g for g in genders if g}
    values.discard("u")
    if not values:
        return "u"
    if "x" in values or len(values) > 1:
        return "x"
    return next(iter(values))


def classify_from_categories(
    categories: Iterable[object] | None,
    language_name: str,
) -> tuple[str | None, str, bool]:
    """
    A sense's categories -> (bucket, gender, is_also_surname). bucket is None
    when nothing resolved -- the signal to fall back to the gloss classifier.

    Gender comes only from categories agreeing with the WINNING bucket. A
    sense in both "English male given names" and "English surnames" is a male
    given name; letting the genderless surname category vote would drag it to
    unknown.
    """
    hits: list[tuple[str, str]] = []
    for cat in category_names(categories):
        parsed = parse_name_category(cat, language_name)
        if parsed:
            hits.append(parsed)
    if not hits:
        return None, "u", False

    bucket = min((b for b, _ in hits), key=lambda b: _BUCKET_PRIORITY[b])
    genders = {g for b, g in hits if b == bucket}
    also_surname = bucket != "SURNAME" and any(b == "SURNAME" for b, _ in hits)
    return bucket, reduce_gender(genders), also_surname


# --- gender from the gloss head phrase -------------------------------------
#
# Part A's gender_of() reads "given name" only: \bname\b cannot match inside
# "surname", so "a male surname" and "a male patronymic" both read unknown
# there. That is why the N1 census's de/es/pl/is gender figures came from
# raw_tags -- and why dropping raw_tags without this would zero Icelandic's
# clean 36m/35f patronymic split and Polish's real male/female surname forms.
# Part A stays untouched (its output is the published census); production
# uses the reader below.

_UNISEX_PAIR_RX = re.compile(
    r"\b(?:male\s+(?:or|and)\s+female|female\s+(?:or|and)\s+male"
    r"|common[- ]gender)\b",
    re.IGNORECASE,
)

_PERSONAL_NAME_TAIL = (
    r"(?:given\s+name|surname|family\s+name|patronymic|matronymic|name)"
)

_HEAD_GENDER_RX: dict[str, "re.Pattern[str]"] = {
    "f": re.compile(rf"\b(?:female|feminine)\s+(?:\w+\s+){{0,2}}?"
                    rf"{_PERSONAL_NAME_TAIL}\b", re.IGNORECASE),
    "m": re.compile(rf"\b(?:male|masculine)\s+(?:\w+\s+){{0,2}}?"
                    rf"{_PERSONAL_NAME_TAIL}\b", re.IGNORECASE),
    "x": re.compile(rf"\b(?:unisex|epicene)\s+(?:\w+\s+){{0,2}}?"
                    rf"{_PERSONAL_NAME_TAIL}\b", re.IGNORECASE),
}


def gender_from_head(gloss: str) -> str:
    """
    m / f / x / u from the FIRST gender phrase in the gloss head.

    "a male given name, feminine equivalent Christiane" is a MALE name --
    first phrase wins, exactly as in Part A. "a male or female given name" is
    unisex and is tested before the singles, or it would read as male.
    """
    text = gloss or ""
    if _UNISEX_PAIR_RX.search(text):
        return "x"
    hits = []
    for label, rx in _HEAD_GENDER_RX.items():
        m = rx.search(text)
        if m:
            hits.append((m.start(), label))
    if not hits:
        return "u"
    hits.sort()
    return hits[0][1]


def classify_sense(
    gloss: str,
    tags: list[str] | None,
    categories: Iterable[object] | None,
    language_name: str,
) -> tuple[str, str, bool]:
    """
    ONE name sense -> (bucket, gender, is_also_surname).

    Order, and why:
      1. FORM_OF, from the gloss/tags, ALWAYS first. An inflected citation is
         not a name row whatever its categories say -- and categories ARE
         inherited by form-of senses, so skipping this would import
         "accusative singular of Zmyrna" as a Latin given name (Latin alone
         carries 313 such senses).
      2. categories, the primary predicate (3a).
      3. the gloss regexes, as fallback, wherever no category resolved.
    """
    bucket_gloss = classify_name_type(gloss, tags)
    if bucket_gloss == "FORM_OF":
        return "FORM_OF", "u", False

    bucket, gender, also_surname = classify_from_categories(
        categories, language_name
    )
    if bucket is None:
        bucket, gender = bucket_gloss, gender_from_head(gloss)
    elif gender == "u":
        gender = gender_from_head(gloss)

    if bucket in ("GIVEN", "PATRONYMIC") and not also_surname:
        also_surname = both_given_and_surname(gloss)
    return bucket, gender, also_surname


# ---------------------------------------------------------------------------
# 3b -- the meaning waterfall
# ---------------------------------------------------------------------------

# The three TEXT-BEARING channels, in priority order. HOMOGRAPH is absent: it
# carries no text of its own and is Stage 6a's inheritance, recorded here as
# homograph_lexeme_id. GLOSS_EQUIV_EN is absent too -- see extract_equivalence.
MEANING_WATERFALL: tuple[str, ...] = ("GLOSS_MEANING", "ETYM_MARKER",
                                      "ETYM_QUOTED")

# Lower rank == higher priority. Used to pick WHICH sense of a lemma becomes
# the row's source_sense_id.
MEANING_CHANNEL_RANK: dict[object, int] = {
    "GLOSS_MEANING": 0, "ETYM_MARKER": 1, "ETYM_QUOTED": 2, None: 9,
}

MAX_MEANING_LEN = 400
MAX_EQUIV_LEN = 120


def _clean_meaning(value: str | None, limit: int) -> str | None:
    if not value:
        return None
    out = " ".join(str(value).split()).strip(" ,;:.")
    if not out or len(out) > limit:
        return None
    if not re.search(r"[^\W\d_]", out, re.UNICODE):
        return None
    return out


def extract_meaning(
    gloss: str,
    etymology: str,
    lang_code: str,
) -> tuple[str | None, str | None]:
    """(meaning_text, meaning_channel), or (None, None)."""
    for channel, value in (
        ("GLOSS_MEANING", extract_gloss_meaning(gloss)),
        ("ETYM_MARKER", extract_etym_marker(etymology)),
        ("ETYM_QUOTED", extract_etym_quoted(etymology, lang_code)),
    ):
        text = _clean_meaning(value, MAX_MEANING_LEN)
        if text:
            return text, channel
    return None, None


def extract_equivalence(gloss: str) -> str | None:
    """
    "equivalent to English John" -> "John".

    ORTHOGONAL to the waterfall, not a rung inside it. The roadmap lists
    GLOSS_EQUIV_EN second in priority AND says it must be recorded as an
    equivalence rather than a meaning; both hold only if it is extracted
    independently. Inside the waterfall, a name with an English equivalent
    AND a real quoted etymological gloss would keep the equivalence and lose
    the gloss, for no gain.
    """
    return _clean_meaning(extract_equiv_en(gloss), MAX_EQUIV_LEN)


# ---------------------------------------------------------------------------
# tokens -- the mechanism-1 join surface
# ---------------------------------------------------------------------------

MAX_TOKEN_LEN = 80
MAX_TOKENS_PER_NAME = 12


def meaning_tokens(meaning_text: str | None) -> list[str]:
    """
    meaning_text -> its canonical English join keys, deduplicated and
    order-preserving.

    Tokens go through normalize_lemma(tok, "en") because Stage 7b joins them
    straight against Lexeme.normalized_lemma. Lowercasing alone is a
    DIFFERENT key -- normalize_lemma also applies NFKC and strips combining
    marks -- and a join surface keyed differently from the thing it joins to
    is a silent zero-match, not an error.

    Capped at 12: beyond that the "meaning" is a sentence, and every extra
    token is another chance to match a query the name has nothing to do with.
    """
    out: list[str] = []
    seen: set[str] = set()
    for raw in content_tokens(meaning_text or ""):
        token = normalize_lemma(raw, "en")
        if not token or len(token) > MAX_TOKEN_LEN or token in seen:
            continue
        seen.add(token)
        out.append(token)
        if len(out) >= MAX_TOKENS_PER_NAME:
            break
    return out