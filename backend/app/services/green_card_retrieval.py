"""
Stage 7 -- green-card retrieval.

PURE READ. Nothing here writes a row or commits; the caller owns the
session. Every query is Core/ORM rather than raw SQL so the whole module is
exercisable against the sqlite test fixture -- which matters more here than
elsewhere, because this is the only green-card code that runs per request.

TWO MECHANISMS, ONE SHAPE.
  1. MEANING TOKEN (7b) -- English yellow-card lemmas -> established_name_
     tokens.token -> names. Every green-card meaning is English, which is
     why Stage 7a forces an English expansion even when English is not
     displayed. Matching is on the CANONICAL KEY, never substring: `light`
     must not reach `delight`, `slight`, `lightning`.
  2. HOMOGRAPH (7c) -- a visible yellow lexeme -> names sharing
     (language_id, normalized_lemma) in the SAME language.

Joined on the KEY, not on `homograph_lexeme_id`. That column holds the
lowest-id visible non-name lexeme for a key; the yellow card in hand may be
a different lexeme with the same key (`light` the noun vs `light` the verb),
and joining on the id would drop it silently.

A green card can never ALSO be a yellow card: prune_taxonomy's TIER_B_POS
contains "name", so name lexemes are hidden and never embedded. That is why
the gradient merge is between the name and its homograph WORD, and why the
two mechanisms cannot double-count one object.

SCOPED TO THE REQUESTED LANGUAGES. Not in the roadmap; without it a
Russian-only search returns Welsh names.
"""
from __future__ import annotations

from dataclasses import dataclass, replace

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.generated_name import Language
from app.models.semantic import EstablishedName, EstablishedNameToken

MECH_TOKEN = "meaning_token"
MECH_HOMOGRAPH = "homograph"

# 7e: given before surname. Patronymics sit between them -- they are real
# personal names (findings 4.1 keeps them as their own type) but a much
# smaller and more specialised population than given names.
TYPE_RANK: dict[str, int] = {"given": 0, "patronymic": 1, "surname": 2}

# 7c honesty gate. A gradient card asserts that the word and the name are
# ONE object wearing two tags. For `spelling_only` links that is exactly the
# claim we established we cannot make: IMPORT_PREP_FINDINGS.md 5.1's
# `Lucius` / `lucius` ("a fish, probably the pike"), and 8,635 of the 12,598
# links are spelling_only (findings 11.5). Those matches still SHIP -- as
# standalone green cards carrying Stage 6c's hedged provenance label, which
# says "spelled identically to" and never "means".
GRADIENT_REQUIRES_CORROBORATION = True

# Set in Breakdown D Step 6 from the Step-5 yield probe (findings 15.x).
# Measured at --scope all, w3_d2, 10 probe words, uncapped:
#   cards   median 24.5, max 164
#   m1=479 vs m2=79 pooled (D-4 falsified: mechanism 1 dominates, not m2)
#   surname median 13.5, max 57
#   a single high-yield token ('bright') alone connects to 183 names
# LIMIT sits above the median so a typical query ships uncapped and only the
# tail (max 164) is trimmed. The per-token cap is set just above the m1
# median so no single token like 'bright' can flood a result on its own.
# The surname cap sits AT the surname median rather than well below it,
# because D-7 (tier-0 surname heading the list, 1/10 words) showed the
# tier-before-type ordering already keeps given names from being buried --
# this is a backstop against the max (57), not a correction to the ordering.
DEFAULT_LIMIT = 50
DEFAULT_PER_TOKEN_CAP = 10
DEFAULT_SURNAME_CAP = 12


@dataclass(frozen=True)
class TriggerRef:
    """The yellow-card node that produced a green card (7d).

    `visible` is load-bearing: a card found through the HIDDEN English pass
    has a real trigger that is not on screen.
    """
    sense_id: int
    lemma: str
    language_code: str
    depth: int
    visible: bool
    order: int      # position in the source list; the stable tiebreak


@dataclass(frozen=True)
class GreenCard:
    name: EstablishedName
    language_code: str
    mechanisms: frozenset[str]
    matched_tokens: tuple[str, ...]
    trigger: TriggerRef
    # 7d + 9e. WHERE the card sits in the tree sort -- which is NOT always
    # where it came from. When the trigger is hidden, this is None, meaning
    # "top level, under the query". Substituting some visible node instead
    # would attribute the card to a word that did not produce it.
    anchor_sense_id: int | None
    trigger_count: int
    is_gradient: bool

    @property
    def tier(self) -> int:
        """7e match tier: 0 = the search term itself, 1 = one hop, 2 = deeper."""
        return min(self.trigger.depth, 2)


def language_code_map(db: Session) -> dict[int, str]:
    """id -> code, fetched ONCE. Everything below keys on language_id rather
    than `lexeme.language.code`, which would lazy-load per node -- up to 175
    extra queries on a w3_d2 request."""
    return {
        lid: code
        for lid, code in db.execute(
            select(Language.id, Language.code).where(Language.code.isnot(None))
        )
    }


def visible_index(nodes, codes_by_id) -> dict[tuple[int, str], TriggerRef]:
    """(language_id, normalized_lemma) -> best visible trigger.

    Best == shallowest, then earliest in the interleave. Several senses of
    one lemma collapse to one entry: the mechanism-2 join is on the lemma
    key, so a per-sense index would query the same key repeatedly.
    """
    out: dict[tuple[int, str], TriggerRef] = {}
    for order, node in enumerate(nodes):
        lex = node.sense.lexeme
        key = (lex.language_id, lex.normalized_lemma)
        ref = TriggerRef(
            node.sense.id, lex.lemma,
            codes_by_id.get(lex.language_id, ""), node.depth, True, order,
        )
        cur = out.get(key)
        if cur is None or (ref.depth, ref.order) < (cur.depth, cur.order):
            out[key] = ref
    return out


def english_token_keys(nodes, english_visible) -> dict[str, TriggerRef]:
    """English normalized lemmas -> best trigger, from the Stage-7a pass.

    `english_visible` is the whole point: these same nodes are either the
    displayed English tree or the hidden pass, and the resulting green cards
    must be IDENTICAL either way. Only the trigger's `visible` flag differs.
    """
    out: dict[str, TriggerRef] = {}
    for order, node in enumerate(nodes):
        lex = node.sense.lexeme
        ref = TriggerRef(node.sense.id, lex.lemma, "en", node.depth,
                         english_visible, order)
        cur = out.get(lex.normalized_lemma)
        if cur is None or (ref.depth, ref.order) < (cur.depth, cur.order):
            out[lex.normalized_lemma] = ref
    return out


def match_by_meaning_token(db, token_keys, language_ids, per_token_cap):
    """7b. Returns raw (name, mechanism, token, trigger) tuples.

    The cap is applied in PYTHON, after a fully ordered fetch, not as a SQL
    LIMIT: a per-group SQL limit needs a window function, and the ordering
    has to be deterministic anyway for a re-run to be reproducible. If Step 5
    shows the raw row count is large enough for that to hurt, THAT is the
    measurement that justifies a window function -- not a guess now.
    """
    if not token_keys or not language_ids:
        return []
    rows = db.execute(
        select(EstablishedNameToken.token, EstablishedName)
        .join(EstablishedName,
              EstablishedName.id == EstablishedNameToken.established_name_id)
        .where(EstablishedNameToken.token.in_(sorted(token_keys)),
               EstablishedName.language_id.in_(sorted(language_ids)))
        .order_by(EstablishedNameToken.token,
                  EstablishedName.name_type,
                  EstablishedName.normalized_lemma,
                  EstablishedName.id)
    ).all()
    seen_per_token: dict[str, int] = {}
    out = []
    for token, name in rows:
        used = seen_per_token.get(token, 0)
        if used >= per_token_cap:
            continue
        seen_per_token[token] = used + 1
        out.append((name, MECH_TOKEN, token, token_keys[token]))
    return out


def match_by_homograph(db, vis_index, language_ids):
    """7c. One query per language, not one combined query.

    Same reasoning the language directory records (Phase C, Step 0/3): a
    single predicate spanning every language gives the planner room to
    abandon the per-language index. Here each query is a clean prefix probe
    of uq_established_names_key (language_id, normalized_lemma, name_type).
    """
    by_language: dict[int, list[str]] = {}
    for (language_id, norm) in vis_index:
        if language_id in language_ids:
            by_language.setdefault(language_id, []).append(norm)
    out = []
    for language_id in sorted(by_language):
        names = db.scalars(
            select(EstablishedName)
            .where(EstablishedName.language_id == language_id,
                   EstablishedName.normalized_lemma.in_(
                       sorted(by_language[language_id])))
            .order_by(EstablishedName.name_type, EstablishedName.id)
        ).all()
        for name in names:
            out.append((name, MECH_HOMOGRAPH, None,
                        vis_index[(language_id, name.normalized_lemma)]))
    return out


def _is_gradient(name, vis_index) -> bool:
    """A PROPERTY of the card, not a branch inside mechanism 2.

    Computed once against the visible index, this also catches the case the
    roadmap's phrasing misses: a card found by mechanism ONE whose own
    language's word happens to be visible too. That is the shape of the
    `light` -> `आकाश` example, and a mechanism-2-only rule would never merge
    it.
    """
    if (name.language_id, name.normalized_lemma) not in vis_index:
        return False
    if GRADIENT_REQUIRES_CORROBORATION:
        return name.homograph_confidence == "corroborated"
    return True


def _better_trigger(a: TriggerRef, b: TriggerRef) -> TriggerRef:
    """7d's primary-trigger rule: visible beats hidden, then shallowest,
    then earliest. Visible first because the trigger is what anchors the
    card in the tree sort, and an on-screen anchor is worth more than a
    marginally shallower off-screen one."""
    ka = (0 if a.visible else 1, a.depth, a.order)
    kb = (0 if b.visible else 1, b.depth, b.order)
    return a if ka < kb else b


def fold(matches, vis_index, codes_by_id):
    """Raw matches -> one GreenCard per name, with the primary trigger picked
    and the anchor resolved."""
    by_name: dict[int, GreenCard] = {}
    for name, mechanism, token, trigger in matches:
        card = by_name.get(name.id)
        if card is None:
            by_name[name.id] = GreenCard(
                name=name,
                language_code=codes_by_id.get(name.language_id, ""),
                mechanisms=frozenset({mechanism}),
                matched_tokens=(token,) if token else (),
                trigger=trigger, anchor_sense_id=None, trigger_count=1,
                is_gradient=_is_gradient(name, vis_index),
            )
            continue
        tokens = card.matched_tokens + ((token,) if token else ())
        by_name[name.id] = replace(
            card,
            mechanisms=card.mechanisms | {mechanism},
            matched_tokens=tuple(dict.fromkeys(tokens)),
            trigger=_better_trigger(trigger, card.trigger),
            trigger_count=card.trigger_count + 1,
        )
    return [
        replace(card, anchor_sense_id=(card.trigger.sense_id
                                       if card.trigger.visible else None))
        for card in by_name.values()
    ]


def sort_key(card: GreenCard):
    """7e ordering, in the roadmap's stated priority: match tier, then type,
    then popularity where known, then alphabetical.

    `popularity_rank` is NULL for every row today -- the column is a Stage-2
    placeholder nothing populates -- so the NULLs-last term is currently a
    no-op that costs nothing and stops the ordering changing shape the day
    something fills it. `name.id` last makes the order total, so a rebuild
    is byte-reproducible.
    """
    return (
        card.tier,
        TYPE_RANK.get(card.name.name_type, 9),
        0 if card.name.popularity_rank is not None else 1,
        card.name.popularity_rank or 0,
        card.name.normalized_lemma,
        card.name.id,
    )


def collapse_clusters(cards):
    """7e dedup. One card per cluster; the member that RANKS BEST among those
    that actually matched wins, which under `sort_key` is the shallowest-tier
    match. The cluster head is not forced: a search that reached `Kathryn`
    should show `Kathryn`, with the rest of the family in her dropdown.
    Names with no cluster pass through untouched."""
    kept: set[int] = set()
    out = []
    for card in sorted(cards, key=sort_key):
        cluster_id = card.name.cluster_id
        if cluster_id is not None:
            if cluster_id in kept:
                continue
            kept.add(cluster_id)
        out.append(card)
    return out


def apply_caps(cards, limit, surname_cap):
    """7e flood control. The surname cap is the real instrument: en and pl
    surnames together are 66,000 of the 106,398 rows, and 7e's tier-before-
    type ordering means a tier-0 surname outranks a tier-1 given name, so
    type-ordering alone does not contain them."""
    out = []
    surnames = 0
    for card in cards:
        if card.name.name_type == "surname":
            if surnames >= surname_cap:
                continue
            surnames += 1
        out.append(card)
        if len(out) >= limit:
            break
    return out


def retrieve_green_cards(
    db: Session, *, english_nodes, visible_nodes, language_codes,
    limit: int = DEFAULT_LIMIT,
    per_token_cap: int = DEFAULT_PER_TOKEN_CAP,
    surname_cap: int = DEFAULT_SURNAME_CAP,
) -> list[GreenCard]:
    """The whole of Stage 7b-7e, in order.

    `english_nodes` is ParallelExpansion.english_pass -- the visible English
    tree when English is requested, the hidden pass when it isn't, and the
    same nodes either way.
    """
    requested = set(language_codes)
    codes_by_id = language_code_map(db)
    language_ids = {lid for lid, code in codes_by_id.items()
                    if code in requested}
    if not language_ids:
        return []
    vis = visible_index(visible_nodes, codes_by_id)
    tokens = english_token_keys(english_nodes, "en" in requested)
    matches = match_by_meaning_token(db, tokens, language_ids, per_token_cap)
    matches += match_by_homograph(db, vis, language_ids)
    cards = fold(matches, vis, codes_by_id)
    return apply_caps(collapse_clusters(cards), limit, surname_cap)