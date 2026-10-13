"""
Static guard (C4): the publish build keeps only the _EMBEDDABLE_SYNONYM_KEYS
keys of senses.raw_sense and lexemes.raw_entry, and empties
sense_embeddings.embedded_text. So no request-path module may read those
columns, except sense_embeddings.py's two synonym functions (which read only
those keys).

WHY A GREP: a new request-path read of raw_sense / raw_entry would work on
master and silently get empty data in production. Nothing at runtime
notices; a grep over app/ does.
"""
from pathlib import Path

APP = Path(__file__).resolve().parent.parent / "app"
COLUMNS = ("raw_sense", "raw_entry", "embedded_text")

# Each exemption carries its reason, so the list can't grow by habit.
EXEMPT_DIRS = {
    "importers": "import-time only; reads the full raw JSON from the dumps",
    "extractors": "import-time only",
    "review": "offline manual-review tooling, never on the request path",
}
EXEMPT_FILES = {
    "services/sense_embeddings.py":
        "the two request-path synonym readers (only _EMBEDDABLE_SYNONYM_KEYS) "
        "and the embedding backfill, which writes embedded_text",
    "services/romanization.py":
        "reads raw_entry; called only by scripts (populate_established_names, "
        "backfill_romanization), never by a route",
    "models/semantic.py": "defines the columns",
}


def _offenders() -> dict[str, list[str]]:
    found = {}
    for path in sorted(APP.rglob("*.py")):
        rel = path.relative_to(APP).as_posix()
        if rel.split("/")[0] in EXEMPT_DIRS or rel in EXEMPT_FILES:
            continue
        src = path.read_text(encoding="utf-8")
        hits = [c for c in COLUMNS if c in src]
        if hits:
            found[rel] = hits
    return found


def test_no_new_reader_of_slimmed_columns():
    offenders = _offenders()
    assert not offenders, (
        "the publish build drops these columns' contents (CLAUDE.md, publish "
        f"projection rule); a new reader would get empty data in production: "
        f"{offenders}")


def test_exemptions_still_exist():
    for d in EXEMPT_DIRS:
        assert (APP / d).is_dir(), d
    for f in EXEMPT_FILES:
        assert (APP / f).is_file(), f
