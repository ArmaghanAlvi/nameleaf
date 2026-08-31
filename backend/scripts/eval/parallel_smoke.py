import os, sys
sys.path.insert(0, os.getcwd())
from app.db.session import SessionLocal
from sqlalchemy import select, text
from app.models.semantic import Lexeme, Sense, SenseEmbedding
from app.services.parallel_expansion import parallel_expand
from app.services.root_llm import fence_query_time_llm            # noqa: E402

fence_query_time_llm()

with SessionLocal() as db:
    db.execute(text("SET lock_timeout = '30s'"))
    sid = db.scalar(
        select(Sense.id).join(Lexeme, Lexeme.id == Sense.lexeme_id)
        .join(SenseEmbedding, SenseEmbedding.sense_id == Sense.id)
        .where(Lexeme.language_id == 1, Lexeme.normalized_lemma == "light",
               Sense.visibility_status == "visible")
        .order_by(Sense.sense_index).limit(1))
    px = parallel_expand(db, english_sense_id=sid, width=3, depth=2) # type: ignore
    for code, t in px.trees.items():
        rung = t.root.rung if t.root else ("selected" if code == "en" else "NO ROOT")
        print(f"[{code}] root_rung={rung} nodes={len(t.nodes)} pivoted={t.pivoted_count}")
        for n in t.nodes[:6]:
            print(f"   d{n.depth} {n.provenance:<18} {n.sense.lexeme.lemma}  "
                  f"({n.sense.lexeme.language.code})")
    print("\ninterleaved head:")
    for n in px.interleaved[:12]:
        print(f"  d{n.depth} {n.sense.lexeme.language.code} {n.sense.lexeme.lemma}")