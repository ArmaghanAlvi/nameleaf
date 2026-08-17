"""
WN-LMF importer: lands wordnet SYNSET MEMBERSHIPS (ILI) and synonym-grade
EDGES for ONE language, attached to EXISTING Kaikki senses.

Creates NO lexemes and NO senses (see Breakdown 2, Step 1b): a wordnet lemma
absent from Kaikki appears only as an edge target string, never as a node.
This is the OEWN precedent and the only shape compatible with zero review --
minted lexemes would bypass the prune taxonomy entirely.

Attachment policy (mirrors load_oewn_relations.py):
  match on (canonical normalized lemma, mapped POS) -> PRIMARY visible sense
  (lowest sense_index). Per-sense routing by gloss overlap was tried for OEWN
  and removed; disambiguation is downstream (embeddings + search-time sense
  selection).

Join key: normalize_lemma(lemma, lang_code) -- THE canonical key. This is
load-bearing for Arabic: omw-arb/awn4 lemmas are vocalized (tashkeel) and
only the Mn-strip lets them meet bare Kaikki headwords. (The old OEWN loader
used normalize_text; for English the two keys were measured identical on all
joins -- P1 == P2 -- so this importer's OEWN run is not a behavior change.)

Cross-source discipline: only indexed CILI (`i` + digits) is stored; synset
IDs are provenance metadata, never join keys. OEWN's ili="in" (proposed,
unindexed, ~16K synsets) is excluded by the regex.

Edges:
  synonym      -- synset co-membership (the only relation omw-ja/omw-arb
                  carry; their files contain no SynsetRelation elements).
  near_synonym -- SynsetRelation relType='similar' (awn4 and OEWN have these;
                  same treatment as load_oewn_relations.NEAR_SYNONYM_RELTYPES).

Idempotent: pg_insert ... ON CONFLICT DO NOTHING on both unique constraints,
plus in-memory dedup. Safe to re-run; a re-run skips, it does not repair
(same caveat as the seeded kaikki extractor).

Dry-run by default. THE DRY RUN IS THE MEASUREMENT: its counters (synset-size
histograms, attachable-edge yield) are gate 9's instrument when pointed at
awn4 vs omw-arb.

USAGE (from backend/):
  python3 -m app.importers.wordnet_lmf \
      --input <lmf.xml[.gz]> --language-code ja --provenance omw-ja \
      --source-slug omw-ja --source-name "Japanese WordNet (OMW)" \
      [--join-marker] [--memberships-only] [--apply]
"""
from __future__ import annotations

import argparse
import gzip
import re
from collections import Counter, defaultdict
from pathlib import Path
from xml.etree.ElementTree import iterparse

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert

from app.db.session import SessionLocal
from app.models.generated_name import Language
from app.utils.provenance import WORDNET_PROVENANCES, pivot_counting_provenances
from app.models.semantic import Lexeme, Sense, SenseRelation, SenseSynset, Source
from app.utils.text import normalize_lemma

_ILI_RE = re.compile(r"^i\d+$")

# GWN-LMF partOfSpeech -> Kaikki-style POS. 's' = satellite adjective.
# Mirrors load_oewn_relations.OEWN_POS_MAP.
POS_MAP = {"n": "noun", "v": "verb", "a": "adj", "s": "adj", "r": "adv"}

NEAR_SYNONYM_RELTYPES = {"similar"}


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def parse_lmf(path: Path, *, join_marker: bool):
    """Stream one WN-LMF file. Returns (members_by_synset, ili_by_synset,
    similar_pairs, stats). Only LexicalEntry and Synset are cleared, so their
    children are intact when the parent's end event fires."""
    opener = gzip.open if path.suffix == ".gz" else open
    members_by_synset: dict[str, list[tuple[str, str]]] = defaultdict(list)
    ili_by_synset: dict[str, str] = {}
    similar_pairs: list[tuple[str, str]] = []
    stats: Counter = Counter()

    with opener(path, "rb") as fh:
        for _event, elem in iterparse(fh, events=("end",)):
            tag = _local(elem.tag)
            if tag == "LexicalEntry":
                stats["entries"] += 1
                lemma_el = next(
                    (c for c in elem if _local(c.tag) == "Lemma"), None
                )
                if lemma_el is None:
                    stats["entries_no_lemma"] += 1
                    elem.clear()
                    continue
                surface = (lemma_el.get("writtenForm") or "").strip()
                surface = surface.replace("_", " ")
                if join_marker:
                    surface = surface.replace("+", "")  # omw-ja 呼吸+する
                pos = POS_MAP.get(
                    (lemma_el.get("partOfSpeech") or "").strip().lower()
                )
                if not surface or pos is None:
                    stats["entries_skipped_lemma_or_pos"] += 1
                    elem.clear()
                    continue
                for child in elem:
                    if _local(child.tag) != "Sense":
                        continue
                    synset_id = (child.get("synset") or "").strip()
                    if synset_id:
                        stats["senses"] += 1
                        members_by_synset[synset_id].append((surface, pos))
                elem.clear()

            elif tag == "Synset":
                stats["synsets"] += 1
                synset_id = (elem.get("id") or "").strip()
                ili = (elem.get("ili") or "").strip()
                if synset_id and _ILI_RE.fullmatch(ili):
                    ili_by_synset[synset_id] = ili
                elif synset_id and ili:
                    stats["synsets_unindexed_ili"] += 1
                for child in elem:
                    if (
                        _local(child.tag) == "SynsetRelation"
                        and (child.get("relType") or "").strip().lower()
                        in NEAR_SYNONYM_RELTYPES
                    ):
                        tgt = (child.get("target") or "").strip()
                        if synset_id and tgt:
                            similar_pairs.append((synset_id, tgt))
                elem.clear()

    stats["synsets_indexed_ili"] = len(ili_by_synset)
    stats["similar_relations"] = len(similar_pairs)
    return members_by_synset, ili_by_synset, similar_pairs, stats


def _hist(sizes: list[int]) -> str:
    buckets = Counter()
    for n in sizes:
        if n <= 0:
            buckets["0"] += 1
        elif n == 1:
            buckets["1"] += 1
        elif n == 2:
            buckets["2"] += 1
        elif n <= 5:
            buckets["3-5"] += 1
        else:
            buckets["6+"] += 1
    order = ["0", "1", "2", "3-5", "6+"]
    return "  ".join(f"{k}:{buckets.get(k, 0)}" for k in order)


def run(args: argparse.Namespace) -> None:
    path = Path(args.input).expanduser().resolve()
    members_by_synset, ili_by_synset, similar_pairs, stats = parse_lmf(
        path, join_marker=args.join_marker
    )

    with SessionLocal() as db:
        lang = db.scalars(
            select(Language).where(Language.code == args.language_code)
        ).first()
        if lang is None:
            raise SystemExit(f"No Language row with code={args.language_code!r}")

        source = db.scalars(
            select(Source).where(Source.slug == args.source_slug)
        ).first()
        if source is None:
            if not args.apply:
                print(f"(dry run: source {args.source_slug!r} would be created)")
                source_id = -1
            else:
                source = Source(
                    slug=args.source_slug,
                    name=args.source_name,
                    source_type="wordnet",
                )
                db.add(source)
                db.commit()
                db.refresh(source)
                source_id = source.id
        else:
            source_id = source.id

        # (stored normalized_lemma, pos) -> primary visible sense id.
        # Tie-break (sense_index, sense_id) for determinism across lexemes
        # sharing a key (etymology-split Kaikki entries).
        sense_of: dict[tuple[str, str], int] = {}
        best: dict[tuple[str, str], tuple[int, int]] = {}
        for sid, sidx, norm, pos in db.execute(
            select(
                Sense.id,
                Sense.sense_index,
                Lexeme.normalized_lemma,
                Lexeme.part_of_speech,
            )
            .join(Lexeme, Lexeme.id == Sense.lexeme_id)
            .where(
                Lexeme.language_id == lang.id,
                Sense.visibility_status == "visible",
            )
        ).yield_per(5000):
            key = (norm, (pos or "").strip().lower())
            rank = (sidx, sid)
            if key not in best or rank < best[key]:
                best[key] = rank
                sense_of[key] = sid

        # normalized_lemma -> lexeme_id, for target resolution.
        lex_of: dict[str, int] = {}
        for lid, norm in db.execute(
            select(Lexeme.id, Lexeme.normalized_lemma).where(
                Lexeme.language_id == lang.id
            )
        ).yield_per(5000):
            lex_of.setdefault(norm, lid)

        print(f"file ............................. {path.name}")
        print(f"language ......................... {lang.code} (id={lang.id})")
        for k in (
            "entries", "senses", "synsets",
            "synsets_indexed_ili", "synsets_unindexed_ili",
            "entries_skipped_lemma_or_pos", "similar_relations",
        ):
            print(f"{k:.<33} {stats.get(k, 0)}")

        # Resolve every synset's members once.
        matched = unmatched = 0
        raw_sizes: list[int] = []
        matched_sizes: list[int] = []
        resolved_by_synset: dict[str, list[tuple[str, str, int | None]]] = {}
        for synset_id, members in members_by_synset.items():
            resolved = []
            m = 0
            for surface, pos in members:
                norm = normalize_lemma(surface, lang.code)
                sid = sense_of.get((norm, pos))
                resolved.append((surface, norm, sid))
                if sid is None:
                    unmatched += 1
                else:
                    matched += 1
                    m += 1
            resolved_by_synset[synset_id] = resolved
            raw_sizes.append(len(members))
            matched_sizes.append(m)

        total_members = matched + unmatched
        rate = 100.0 * matched / total_members if total_members else 0.0
        print(f"members matched to a sense ....... {matched} / {total_members} ({rate:.2f}%)")
        print(f"synset size (raw members) ........ {_hist(raw_sizes)}")
        print(f"synset size (matched members) .... {_hist(matched_sizes)}")

        # ---- membership rows ----
        membership_rows: list[dict] = []
        seen_m: set[tuple[int, str]] = set()
        for synset_id, resolved in resolved_by_synset.items():
            ili = ili_by_synset.get(synset_id)
            if not ili:
                continue
            for _surface, _norm, sid in resolved:
                if sid is None or (sid, ili) in seen_m:
                    continue
                seen_m.add((sid, ili))
                membership_rows.append(
                    dict(
                        sense_id=sid,
                        ili=ili,
                        source_synset_id=synset_id[:120],
                        source_id=source_id,
                    )
                )
        print(f"membership rows .................. {len(membership_rows)}")

        # ---- edges ----
        edge_rows: list[dict] = []
        if not args.memberships_only:
            seen_e: set[tuple[int, str, str]] = set()

            def queue_edge(sid: int, rel: str, surface: str, norm: str) -> None:
                if not norm:
                    return
                key = (sid, rel, norm)
                if key in seen_e:
                    return
                seen_e.add(key)
                edge_rows.append(
                    dict(
                        from_sense_id=sid,
                        relation_type=rel,
                        provenance=args.provenance,
                        target_text=surface[:300],
                        target_normalized=norm[:300],
                        target_sense_hint=None,
                        target_lexeme_id=lex_of.get(norm),
                        source_id=source_id,
                    )
                )

            # synonym: co-membership
            for synset_id, resolved in resolved_by_synset.items():
                if len(resolved) < 2:
                    continue
                for _surf, norm, sid in resolved:
                    if sid is None:
                        continue
                    for o_surf, o_norm, _o_sid in resolved:
                        if o_norm != norm:
                            queue_edge(sid, "synonym", o_surf, o_norm)
            n_syn = len(edge_rows)
            print(f"synonym edges (attachable) ....... {n_syn}")

            # near_synonym: 'similar' relations, symmetric
            def attach(from_syn: str, to_syn: str) -> None:
                targets = resolved_by_synset.get(to_syn, [])
                for _surf, norm, sid in resolved_by_synset.get(from_syn, []):
                    if sid is None:
                        continue
                    for o_surf, o_norm, _o in targets:
                        if o_norm != norm:
                            queue_edge(sid, "near_synonym", o_surf, o_norm)

            for src_syn, tgt_syn in similar_pairs:
                attach(src_syn, tgt_syn)
                attach(tgt_syn, src_syn)
            print(f"near_synonym edges (attachable) .. {len(edge_rows) - n_syn}")

            n_resolved = sum(1 for r in edge_rows if r["target_lexeme_id"])
            pct = 100.0 * n_resolved / len(edge_rows) if edge_rows else 0.0
            print(f"edge targets resolved ............ {n_resolved} / {len(edge_rows)} ({pct:.2f}%)")

        if not args.apply:
            print("\nDRY RUN -- nothing written. Re-run with --apply to commit.")
            return

        for i in range(0, len(membership_rows), args.commit_every):
            stmt = pg_insert(SenseSynset).values(
                membership_rows[i : i + args.commit_every]
            )
            stmt = stmt.on_conflict_do_nothing(
                constraint="uq_sense_synsets_membership"
            )
            db.execute(stmt)
            db.commit()
        for i in range(0, len(edge_rows), args.commit_every):
            stmt = pg_insert(SenseRelation).values(
                edge_rows[i : i + args.commit_every]
            )
            stmt = stmt.on_conflict_do_nothing(constraint="uq_sense_relations_edge")
            db.execute(stmt)
            db.commit()

        # Refresh persisted pivot eligibility for THIS language only
        # (Language.has_wordnet_edges, migration a1c7f3e94b28). Recomputed
        # from the edge inventory rather than inferred from args.provenance:
        # this importer can also write 'oewn', which pivot_counting_
        # provenances() deliberately excludes, so "we just wrote edges" does
        # not imply "this language is now pivot-ineligible". ON CONFLICT DO
        # NOTHING above also means a non-empty edge_rows may have landed zero
        # new rows. One query, at import time -- cost irrelevant here.
        lang.has_wordnet_edges = db.scalar(
            select(Lexeme.id)
            .join(Sense, Sense.lexeme_id == Lexeme.id)
            .join(SenseRelation, SenseRelation.from_sense_id == Sense.id)
            .where(Lexeme.language_id == lang.id,
                   SenseRelation.provenance.in_(
                       tuple(sorted(pivot_counting_provenances()))))
            .limit(1)
        ) is not None
        db.commit()

        print(
            f"\nApplied: {len(membership_rows)} memberships, "
            f"{len(edge_rows)} edges (provenance={args.provenance})."
        )
        print(f"has_wordnet_edges[{args.language_code}] = "
              f"{lang.has_wordnet_edges}  (other languages unchanged; re-run "
              f"scripts/prune/backfill_wordnet_edge_flags.py to refresh all)")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--input", required=True)
    ap.add_argument("--language-code", required=True,
                    help="OUR language code (ja, ar, en) -- NOT the file's "
                         "lexicon code (omw files say 'arb'; our row is 'ar')")
    ap.add_argument("--provenance", required=True,
                    choices=sorted(WORDNET_PROVENANCES),
                    help="must be registered in app/utils/provenance.py")
    ap.add_argument("--source-slug", required=True)
    ap.add_argument("--source-name", required=True)
    ap.add_argument("--join-marker", action="store_true",
                    help="strip '+' morpheme-join markers (omw-ja)")
    ap.add_argument("--memberships-only", action="store_true",
                    help="write sense_synsets only, no edges (OEWN retrofit)")
    ap.add_argument("--apply", action="store_true",
                    help="write changes; default is dry-run (measure only)")
    ap.add_argument("--commit-every", type=int, default=5000)
    args = ap.parse_args()
    run(args)


if __name__ == "__main__":
    main()