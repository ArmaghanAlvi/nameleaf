"""
Input assembly for the LLM origin pass (Stages 19-22).

SHARED between the Stage-20 pilot and the Stage-22 full pass DELIBERATELY.
The pilot's four decision gates -- batch size, the Old English floor, one
prompt or two, and whether the loose bar holds -- are only meaningful if the
pilot asked about exactly what the full pass will ask about. Putting
assembly in a script would make that a thing to remember instead of a thing
that is true.

READ-ONLY. Nothing here calls an API or writes a row.

SELECTION ORDER (19d): rows carrying meaning_text first. G-4 measured
10,032 surnames and 2,769 given names with a meaning -- the population that
actually surfaces on a card. Same reasoning as _THIN_SQL's translation
breadth tier: if a session is cut short by the daily cap, the rows that
shipped are the ones users see.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from sqlalchemy import select, text
from sqlalchemy.orm import Session

from app.models.generated_name import Language

HOST_CODE = "en"

_PENDING_SQL = """
SELECT en.id, en.lemma, en.normalized_lemma, en.name_type, en.gender,
       en.meaning_text, en.meaning_channel, en.source_sense_id
FROM established_names en
JOIN languages l ON l.id = en.language_id
WHERE l.code = :code
  AND en.origin_source IS NULL
ORDER BY CASE WHEN en.meaning_text IS NULL THEN 1 ELSE 0 END,
         en.name_type, en.normalized_lemma
"""

_TWINS_SQL = text("""
SELECT t.normalized_lemma, t.name_type, l.name AS twin_language
FROM name_origin_twins t
JOIN languages l ON l.id = t.twin_language_id
JOIN languages h ON h.id = t.language_id
WHERE h.code = :code
ORDER BY t.normalized_lemma, l.name
""")


@dataclass(frozen=True)
class OriginItem:
    established_name_id: int
    lemma: str
    normalized_lemma: str
    name_type: str
    gender: str
    meaning_text: str | None
    meaning_channel: str | None
    source_sense_id: int | None
    twin_languages: tuple[str, ...] = field(default_factory=tuple)


def origin_vocabulary(db: Session) -> list[str]:
    """The closed vocabulary the prompt offers: every language display name
    in the corpus, plus 'other'.

    DERIVED from the languages table, never hardcoded. A hardcoded list rots
    silently the moment a 22nd language is imported, and the prompt would
    then be teaching the model a vocabulary the database no longer has --
    with every out-of-list verdict landing in `other` for a language that
    actually exists.
    """
    names = [n for (n,) in db.execute(
        select(Language.name).where(Language.code.isnot(None))
        .order_by(Language.name))]
    return names + ["other"]


def _twin_index(db: Session) -> dict[tuple[str, str], tuple[str, ...]]:
    """Twins keyed by (normalized_lemma, name_type).

    Joined in PYTHON rather than as a LEFT JOIN with array_agg, for two
    reasons: the table is a few thousand rows so the query cost is nil, and
    array_agg does not exist in SQLite, which is what backend/tests runs on.
    An assembly function that cannot be unit-tested is exactly the thing
    that drifts between the pilot and the pass.
    """
    index: dict[tuple[str, str], list[str]] = {}
    for norm, name_type, twin in db.execute(_TWINS_SQL, {"code": HOST_CODE}):
        index.setdefault((norm, name_type), []).append(twin)
    return {k: tuple(v) for k, v in index.items()}


def select_pending(db: Session, *, limit: int | None = None,
                   offset: int = 0) -> list[OriginItem]:
    """Pending English rows in 19d order, with twin evidence attached."""
    sql = _PENDING_SQL
    params: dict = {"code": HOST_CODE}
    if limit is not None:
        sql += " LIMIT :limit OFFSET :offset"
        params |= {"limit": limit, "offset": offset}
    twins = _twin_index(db)
    return [
        OriginItem(
            established_name_id=r.id, lemma=r.lemma,
            normalized_lemma=r.normalized_lemma, name_type=r.name_type,
            gender=r.gender, meaning_text=r.meaning_text,
            meaning_channel=r.meaning_channel,
            source_sense_id=r.source_sense_id,
            twin_languages=twins.get((r.normalized_lemma, r.name_type), ()),
        )
        for r in db.execute(text(sql), params)
    ]


def item_payload(item: OriginItem) -> dict:
    """EXACTLY what goes into the model's batch item. Nothing else.

    NOT SENT, deliberately: language_header_warning. §20.6 measured it at
    86.4% corpus-wide, firing on native Irish (218/225), Icelandic
    (1,361/1,387), Old Norse (151/157) and Latin (1,494/1,539) entries as
    readily as on English's foreign-sourced rows. It is Wiktionary editorial
    boilerplate, not a discriminating mis-filing signal, and telling the
    model an entry is "flagged as having an incorrect language header" can
    only bias it toward "foreign" on rows that are perfectly native. Do not
    add it here without evidence that supersedes §20.6.

    Gender 'u' is omitted rather than sent as "unknown": it is the DEFAULT
    in reduce_gender, so sending it would present an absence as a fact.
    """
    payload: dict = {"name": item.lemma, "type": item.name_type}
    if item.gender and item.gender != "u":
        payload["gender"] = item.gender
    if item.meaning_text:
        payload["meaning"] = item.meaning_text
        payload["meaning_source"] = item.meaning_channel
    if item.twin_languages:
        # Framed as attestation, not as a verdict. The model is being shown
        # that the same spelling is an established name elsewhere, which is
        # evidence toward the adaptation question -- it is NOT being told
        # the answer, and at 7.8% it could not be.
        payload["also_attested_in"] = list(item.twin_languages)
    return payload