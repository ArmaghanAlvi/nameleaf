from typing import TYPE_CHECKING

from sqlalchemy import Column, ForeignKey, String, Table, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base

if TYPE_CHECKING:
    from app.models.semantic import Concept, EstablishedName, Word


generated_name_languages = Table(
    "generated_name_languages",
    Base.metadata,
    Column(
        "generated_name_id",
        ForeignKey("generated_names.id", ondelete="CASCADE"),
        primary_key=True,
    ),
    Column(
        "language_id",
        ForeignKey("languages.id", ondelete="CASCADE"),
        primary_key=True,
    ),
)


generated_name_flavors = Table(
    "generated_name_flavors",
    Base.metadata,
    Column(
        "generated_name_id",
        ForeignKey("generated_names.id", ondelete="CASCADE"),
        primary_key=True,
    ),
    Column(
        "flavor_id",
        ForeignKey("generation_flavors.id", ondelete="CASCADE"),
        primary_key=True,
    ),
)


class Language(Base):
    __tablename__ = "languages"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(
        String(100),
        unique=True,
        nullable=False,
    )

    # Nullable temporarily because your existing development rows
    # were created before language codes existed.
    code: Mapped[str | None] = mapped_column(
        String(20),
        nullable=True,
    )

    native_name: Mapped[str | None] = mapped_column(
        String(100),
        nullable=True,
    )

    script: Mapped[str | None] = mapped_column(
        String(100),
        nullable=True,
    )

    # Display/interleave ordering, decoupled from id (Stage 8 pre-flight).
    # NULL falls back to id (== import order); lower values sort first.
    # English is pinned first in code at both ordering sites regardless.
    display_order: Mapped[int | None] = mapped_column(nullable=True)

    # Persisted pivot eligibility input: does this language have ANY wordnet
    # synonym edge (pivot_counting_provenances -- oewn excluded)? Recomputing
    # this per process cost ~17s cold, because proving the NEGATIVE for the 14
    # wordnet-free languages means touching every sense of each. See
    # notes/LATENCY_INVESTIGATION.md F11.
    # NULL = not yet computed; readers fall back to the live probe, so the
    # column is byte-identical to the old behavior until it is backfilled.
    # ⚠ Derived from sense_relations. Re-run scripts/prune/backfill_wordnet_
    # edge_flags.py after ANY import that adds or removes wordnet edges, or
    # after adding a slug to WORDNET_PROVENANCES.
    has_wordnet_edges: Mapped[bool | None] = mapped_column(nullable=True)

    __table_args__ = (
        UniqueConstraint(
            "code",
            name="uq_languages_code",
        ),
    )

    generated_names: Mapped[list["GeneratedName"]] = relationship(
        secondary=generated_name_languages,
        back_populates="source_languages",
    )

    words: Mapped[list["Word"]] = relationship(
        back_populates="language",
    )

    established_names: Mapped[list["EstablishedName"]] = relationship(
        back_populates="language",
    )


class GenerationFlavorModel(Base):
    __tablename__ = "generation_flavors"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(100), unique=True, nullable=False)

    generated_names: Mapped[list["GeneratedName"]] = relationship(
        secondary=generated_name_flavors,
        back_populates="flavors",
    )


class GeneratedName(Base):
    __tablename__ = "generated_names"

    id: Mapped[int] = mapped_column(primary_key=True)
    slug: Mapped[str] = mapped_column(String(100), unique=True, nullable=False)
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    meaning: Mapped[str] = mapped_column(String(300), nullable=False)
    explanation: Mapped[str] = mapped_column(Text, nullable=False)

    source_languages: Mapped[list[Language]] = relationship(
        secondary=generated_name_languages,
        back_populates="generated_names",
    )

    flavors: Mapped[list[GenerationFlavorModel]] = relationship(
        secondary=generated_name_flavors,
        back_populates="generated_names",
    )

    parts: Mapped[list["NamePart"]] = relationship(
        back_populates="generated_name",
        cascade="all, delete-orphan",
        order_by="NamePart.position",
    )

    concepts: Mapped[list["Concept"]] = relationship(
        secondary="generated_name_concepts",
        back_populates="generated_names",
    )   


class NamePart(Base):
    __tablename__ = "name_parts"

    id: Mapped[int] = mapped_column(primary_key=True)

    generated_name_id: Mapped[int] = mapped_column(
        ForeignKey("generated_names.id", ondelete="CASCADE"),
        nullable=False,
    )

    position: Mapped[int] = mapped_column(nullable=False)
    text: Mapped[str] = mapped_column(String(100), nullable=False)
    meaning: Mapped[str] = mapped_column(String(300), nullable=False)
    language: Mapped[str] = mapped_column(String(100), nullable=False)
    kind: Mapped[str] = mapped_column(String(50), nullable=False)
    note: Mapped[str | None] = mapped_column(Text, nullable=True)

    generated_name: Mapped[GeneratedName] = relationship(
        back_populates="parts",
    )