from typing import TYPE_CHECKING
from datetime import datetime

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Column,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    JSON,
    String,
    Table,
    Text,
    UniqueConstraint,
    func,
    text,
)

from pgvector.sqlalchemy import Vector

from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base

if TYPE_CHECKING:
    from app.models.generated_name import GeneratedName, Language



class Source(Base):
    __tablename__ = "sources"

    id: Mapped[int] = mapped_column(primary_key=True)

    # Nullable temporarily so the existing development seed row
    # can survive the migration.
    slug: Mapped[str | None] = mapped_column(
        String(120),
        nullable=True,
    )

    name: Mapped[str] = mapped_column(
        String(200),
        nullable=False,
    )

    source_type: Mapped[str] = mapped_column(
        String(100),
        nullable=False,
    )

    url: Mapped[str | None] = mapped_column(
        Text,
        nullable=True,
    )

    license: Mapped[str | None] = mapped_column(
        String(200),
        nullable=True,
    )

    notes: Mapped[str | None] = mapped_column(
        Text,
        nullable=True,
    )

    __table_args__ = (
        UniqueConstraint(
            "slug",
            name="uq_sources_slug",
        ),
    )

    words: Mapped[list["Word"]] = relationship(
        back_populates="source",
    )

    concept_relationships: Mapped[list["ConceptRelationship"]] = relationship(
        back_populates="source",
    )

    word_senses: Mapped[list["WordSense"]] = relationship(
        back_populates="source",
    )

    external_concepts: Mapped[list["Concept"]] = relationship(
        back_populates="external_source",
        foreign_keys="Concept.external_source_id",
    )

    concept_mappings: Mapped[list["ConceptMapping"]] = relationship(
        back_populates="source",
    )


class Concept(Base):
    __tablename__ = "concepts"

    id: Mapped[int] = mapped_column(primary_key=True)

    slug: Mapped[str] = mapped_column(
        String(100),
        unique=True,
        nullable=False,
    )

    label: Mapped[str] = mapped_column(
        String(200),
        nullable=False,
    )

    description: Mapped[str | None] = mapped_column(
        Text,
        nullable=True,
    )

    domain: Mapped[str | None] = mapped_column(
        String(100),
        nullable=True,
    )

    status: Mapped[str] = mapped_column(
        String(30),
        default="active",
        nullable=False,
    )

    concept_type: Mapped[str] = mapped_column(
        String(50),
        default="curated",
        nullable=False,
    )

    is_public: Mapped[bool] = mapped_column(
        Boolean,
        default=True,
        nullable=False,
    )

    external_source_id: Mapped[int | None] = mapped_column(
        ForeignKey("sources.id"),
        nullable=True,
    )

    external_concept_id: Mapped[str | None] = mapped_column(
        String(200),
        nullable=True,
    )

    external_source: Mapped["Source | None"] = relationship(
        back_populates="external_concepts",
        foreign_keys=[external_source_id],
    )

    __table_args__ = (
        CheckConstraint(
            "status IN ('active', 'draft', 'retired')",
            name="ck_concepts_status",
        ),
        CheckConstraint(
            (
                "concept_type IN "
                "('curated', 'external_synset', "
                "'imported_candidate', 'merged', 'retired')"
            ),
            name="ck_concepts_concept_type",
        ),
        UniqueConstraint(
            "external_source_id",
            "external_concept_id",
            name="uq_concepts_external_source_concept_id",
        ),
        Index(
            "ix_concepts_concept_type",
            "concept_type",
        ),
    )

    aliases: Mapped[list["ConceptAlias"]] = relationship(
        back_populates="concept",
        cascade="all, delete-orphan",
    )

    outgoing_relationships: Mapped[list["ConceptRelationship"]] = relationship(
        foreign_keys="ConceptRelationship.source_concept_id",
        back_populates="source_concept",
        cascade="all, delete-orphan",
    )

    incoming_relationships: Mapped[list["ConceptRelationship"]] = relationship(
        foreign_keys="ConceptRelationship.target_concept_id",
        back_populates="target_concept",
    )

    outgoing_mappings: Mapped[list["ConceptMapping"]] = relationship(
        foreign_keys="ConceptMapping.source_concept_id",
        back_populates="source_concept",
        cascade="all, delete-orphan",
    )

    incoming_mappings: Mapped[list["ConceptMapping"]] = relationship(
        foreign_keys="ConceptMapping.target_concept_id",
        back_populates="target_concept",
    )

    word_senses: Mapped[list["WordSense"]] = relationship(
        back_populates="concept",
    )

    generated_names: Mapped[list["GeneratedName"]] = relationship(
        secondary="generated_name_concepts",
        back_populates="concepts",
    )


class ConceptAlias(Base):
    __tablename__ = "concept_aliases"

    id: Mapped[int] = mapped_column(primary_key=True)

    concept_id: Mapped[int] = mapped_column(
        ForeignKey("concepts.id", ondelete="CASCADE"),
        nullable=False,
    )

    text: Mapped[str] = mapped_column(String(200), nullable=False)
    normalized_text: Mapped[str] = mapped_column(String(200), nullable=False)

    concept: Mapped["Concept"] = relationship(
        back_populates="aliases",
    )

    __table_args__ = (
        UniqueConstraint(
            "concept_id",
            "normalized_text",
            name="uq_concept_aliases_concept_normalized_text",
        ),
        Index(
            "ix_concept_aliases_normalized_text",
            "normalized_text",
        ),
    )


class ConceptRelationship(Base):
    __tablename__ = "concept_relationships"

    id: Mapped[int] = mapped_column(primary_key=True)

    source_concept_id: Mapped[int] = mapped_column(
        ForeignKey("concepts.id", ondelete="CASCADE"),
        nullable=False,
    )

    target_concept_id: Mapped[int] = mapped_column(
        ForeignKey("concepts.id", ondelete="CASCADE"),
        nullable=False,
    )

    relationship_type: Mapped[str] = mapped_column(
        String(50),
        nullable=False,
    )

    weight: Mapped[float] = mapped_column(
        nullable=False,
    )

    source_id: Mapped[int | None] = mapped_column(
        ForeignKey("sources.id"),
        nullable=True,
    )

    source_locator: Mapped[str | None] = mapped_column(
        Text,
        nullable=True,
    )

    confidence: Mapped[str] = mapped_column(
        String(20),
        default="medium",
        nullable=False,
    )

    review_status: Mapped[str] = mapped_column(
        String(20),
        default="unreviewed",
        nullable=False,
    )

    source_concept: Mapped["Concept"] = relationship(
        foreign_keys=[source_concept_id],
        back_populates="outgoing_relationships",
    )

    target_concept: Mapped["Concept"] = relationship(
        foreign_keys=[target_concept_id],
        back_populates="incoming_relationships",
    )

    source: Mapped["Source | None"] = relationship(
        back_populates="concept_relationships",
    )

    __table_args__ = (
        UniqueConstraint(
            "source_concept_id",
            "target_concept_id",
            "relationship_type",
            name="uq_concept_relationships_source_target_type",
        ),
        CheckConstraint(
            "weight >= 0 AND weight <= 1",
            name="ck_concept_relationships_weight_range",
        ),
        CheckConstraint(
            "confidence IN ('high', 'medium', 'low')",
            name="ck_concept_relationships_confidence",
        ),
        CheckConstraint(
            "review_status IN ('unreviewed', 'reviewed', 'rejected')",
            name="ck_concept_relationships_review_status",
        ),
        Index(
            "ix_concept_relationships_source_concept_id",
            "source_concept_id",
        ),
        Index(
            "ix_concept_relationships_target_concept_id",
            "target_concept_id",
        ),
    )


class ConceptMapping(Base):
    __tablename__ = "concept_mappings"

    id: Mapped[int] = mapped_column(primary_key=True)

    source_concept_id: Mapped[int] = mapped_column(
        ForeignKey("concepts.id", ondelete="CASCADE"),
        nullable=False,
    )

    target_concept_id: Mapped[int] = mapped_column(
        ForeignKey("concepts.id", ondelete="CASCADE"),
        nullable=False,
    )

    mapping_type: Mapped[str] = mapped_column(
        String(50),
        nullable=False,
    )

    weight: Mapped[float] = mapped_column(
        default=1.0,
        nullable=False,
    )

    source_id: Mapped[int | None] = mapped_column(
        ForeignKey("sources.id"),
        nullable=True,
    )

    source_locator: Mapped[str | None] = mapped_column(
        Text,
        nullable=True,
    )

    confidence: Mapped[str] = mapped_column(
        String(20),
        default="medium",
        nullable=False,
    )

    review_status: Mapped[str] = mapped_column(
        String(20),
        default="unreviewed",
        nullable=False,
    )

    source_concept: Mapped["Concept"] = relationship(
        foreign_keys=[source_concept_id],
        back_populates="outgoing_mappings",
    )

    target_concept: Mapped["Concept"] = relationship(
        foreign_keys=[target_concept_id],
        back_populates="incoming_mappings",
    )

    source: Mapped["Source | None"] = relationship(
        back_populates="concept_mappings",
    )

    __table_args__ = (
        UniqueConstraint(
            "source_concept_id",
            "target_concept_id",
            "mapping_type",
            name="uq_concept_mappings_source_target_type",
        ),
        CheckConstraint(
            "weight >= 0 AND weight <= 1",
            name="ck_concept_mappings_weight_range",
        ),
        CheckConstraint(
            "mapping_type IN ('exact', 'near', 'broader', 'narrower', 'related')",
            name="ck_concept_mappings_mapping_type",
        ),
        CheckConstraint(
            "confidence IN ('high', 'medium', 'low')",
            name="ck_concept_mappings_confidence",
        ),
        CheckConstraint(
            "review_status IN ('unreviewed', 'reviewed', 'rejected')",
            name="ck_concept_mappings_review_status",
        ),
        Index(
            "ix_concept_mappings_source_concept_id",
            "source_concept_id",
        ),
        Index(
            "ix_concept_mappings_target_concept_id",
            "target_concept_id",
        ),
    )


# Yellow-card models
class Word(Base):
    __tablename__ = "words"

    id: Mapped[int] = mapped_column(primary_key=True)

    language_id: Mapped[int] = mapped_column(
        ForeignKey("languages.id"),
        nullable=False,
    )

    text: Mapped[str] = mapped_column(
        String(200),
        nullable=False,
    )

    normalized_text: Mapped[str] = mapped_column(
        String(200),
        nullable=False,
    )

    transliteration: Mapped[str | None] = mapped_column(
        String(200),
    )

    part_of_speech: Mapped[str | None] = mapped_column(
        String(50),
    )

    external_entry_id: Mapped[str | None] = mapped_column(
        String(200),
        nullable=True,
    )

    notes: Mapped[str | None] = mapped_column(
        Text,
    )

    source_id: Mapped[int | None] = mapped_column(
        ForeignKey("sources.id"),
    )

    language: Mapped["Language"] = relationship(
        back_populates="words",
    )

    source: Mapped["Source | None"] = relationship(
        back_populates="words",
    )

    senses: Mapped[list["WordSense"]] = relationship(
        back_populates="word",
        cascade="all, delete-orphan",
    )

    __table_args__ = (
        UniqueConstraint(
            "language_id",
            "normalized_text",
            "part_of_speech",
            name="uq_words_language_normalized_text_pos",
        ),
        Index(
            "ix_words_language_id",
            "language_id",
        ),
        Index(
            "ix_words_external_entry_id",
            "external_entry_id",
        ),
    )


class WordSense(Base):
    __tablename__ = "word_senses"

    id: Mapped[int] = mapped_column(primary_key=True)

    word_id: Mapped[int] = mapped_column(
        ForeignKey("words.id", ondelete="CASCADE"),
        nullable=False,
    )

    concept_id: Mapped[int] = mapped_column(
        ForeignKey("concepts.id", ondelete="CASCADE"),
        nullable=False,
    )

    gloss: Mapped[str] = mapped_column(
        Text,
        nullable=False,
    )

    is_primary: Mapped[bool] = mapped_column(
        default=True,
        nullable=False,
    )

    equivalence_type: Mapped[str] = mapped_column(
        String(50),
        default="direct_equivalent",
        nullable=False,
    )

    sense_rank: Mapped[int] = mapped_column(
        default=1,
        nullable=False,
    )

    external_sense_id: Mapped[str | None] = mapped_column(
        String(200),
        nullable=True,
    )

    external_synset_id: Mapped[str | None] = mapped_column(
        String(200),
        nullable=True,
    )

    source_id: Mapped[int | None] = mapped_column(
        ForeignKey("sources.id"),
        nullable=True,
    )

    source_locator: Mapped[str | None] = mapped_column(
        Text,
        nullable=True,
    )

    confidence: Mapped[str] = mapped_column(
        String(20),
        default="medium",
        nullable=False,
    )

    review_status: Mapped[str] = mapped_column(
        String(20),
        default="unreviewed",
        nullable=False,
    )

    word: Mapped["Word"] = relationship(
        back_populates="senses",
    )

    concept: Mapped["Concept"] = relationship(
        back_populates="word_senses",
    )

    source: Mapped["Source | None"] = relationship(
        back_populates="word_senses",
    )

    __table_args__ = (
        UniqueConstraint(
            "source_id",
            "source_locator",
            name="uq_word_senses_source_locator",
        ),
        CheckConstraint(
            (
                "equivalence_type IN "
                "('canonical', 'direct_equivalent', 'near_equivalent', "
                "'related', 'symbolic', 'technical', 'archaic', 'poetic')"
            ),
            name="ck_word_senses_equivalence_type",
        ),
        CheckConstraint(
            "sense_rank >= 1",
            name="ck_word_senses_sense_rank",
        ),
        CheckConstraint(
            "confidence IN ('high', 'medium', 'low')",
            name="ck_word_senses_confidence",
        ),
        CheckConstraint(
            "review_status IN ('unreviewed', 'reviewed', 'rejected')",
            name="ck_word_senses_review_status",
        ),
        Index(
            "ix_word_senses_concept_id",
            "concept_id",
        ),
        Index(
            "ix_word_senses_external_synset_id",
            "external_synset_id",
        ),
        Index(
            "ix_word_senses_external_sense_id",
            "external_sense_id",
        ),
    )


class Lexeme(Base):
    """
    A dictionary headword/lemma imported from a source such as Kaikki.

    This is not reviewed. It is the stored lexical entry.
    """

    __tablename__ = "lexemes"

    id: Mapped[int] = mapped_column(primary_key=True)

    language_id: Mapped[int] = mapped_column(
        ForeignKey("languages.id"),
        nullable=False,
    )

    lemma: Mapped[str] = mapped_column(
        String(300),
        nullable=False,
    )

    normalized_lemma: Mapped[str] = mapped_column(
        String(300),
        nullable=False,
    )

    # Latin-script rendering of `lemma`, for readers of neither the script nor
    # the language. NULL means "no trustworthy value" and MUST render as
    # nothing -- never as a guess. Populated only for non-Latn
    # Language.script; see app/services/romanization.py for the source
    # hierarchy (Kaikki raw_entry, evidence-driven per script) and
    # scripts/backfill_romanization.py for the derivation pass.
    #
    # 400, not lemma's 300: romanizations expand (щ -> shch, Arabic vowel
    # insertion; census maxlen for fa hit 63 on a single word with multiple
    # dialectal variants). The backfill DISCARDS over-length values rather
    # than truncating -- a truncated romanization is a wrong one.
    romanization: Mapped[str | None] = mapped_column(
        String(400),
        nullable=True,
    )

    part_of_speech: Mapped[str] = mapped_column(
        String(80),
        nullable=False,
    )

    source_id: Mapped[int] = mapped_column(
        ForeignKey("sources.id"),
        nullable=False,
    )

    source_entry_id: Mapped[str] = mapped_column(
        String(500),
        nullable=False,
    )

    raw_language_name: Mapped[str | None] = mapped_column(
        String(120),
        nullable=True,
    )

    raw_entry: Mapped[dict] = mapped_column(
        JSON,
        nullable=False,
        default=dict,
    )

    import_status: Mapped[str] = mapped_column(
        String(30),
        default="active",
        nullable=False,
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
    )

    language: Mapped["Language"] = relationship()
    source: Mapped["Source"] = relationship()

    senses: Mapped[list["Sense"]] = relationship(
        back_populates="lexeme",
        cascade="all, delete-orphan",
    )

    __table_args__ = (
        UniqueConstraint(
            "source_id",
            "source_entry_id",
            name="uq_lexemes_source_entry_id",
        ),
        Index(
            "ix_lexemes_normalized_lemma",
            "normalized_lemma",
        ),
        Index(
            "ix_lexemes_language_lemma",
            "language_id",
            "normalized_lemma",
        ),
        Index(
            "ix_lexemes_language_pos",
            "language_id",
            "part_of_speech",
        ),
        CheckConstraint(
            "import_status IN ('active', 'retired')",
            name="ck_lexemes_import_status",
        ),
    )


class Sense(Base):
    """
    One stored meaning from Kaikki/Wiktionary.

    This is not a candidate and does not require prerequisite review.
    Admin tools can hide/edit/merge it later.
    """

    __tablename__ = "senses"

    id: Mapped[int] = mapped_column(primary_key=True)

    lexeme_id: Mapped[int] = mapped_column(
        ForeignKey("lexemes.id", ondelete="CASCADE"),
        nullable=False,
    )

    source_id: Mapped[int] = mapped_column(
        ForeignKey("sources.id"),
        nullable=False,
    )

    source_locator: Mapped[str] = mapped_column(
        Text,
        nullable=False,
    )

    sense_index: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
    )

    source_order: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=0,
    )

    definition: Mapped[str] = mapped_column(
        Text,
        nullable=False,
        default="",
    )

    raw_glosses: Mapped[list[str]] = mapped_column(
        JSON,
        nullable=False,
        default=list,
    )

    raw_tags: Mapped[list[str]] = mapped_column(
        JSON,
        nullable=False,
        default=list,
    )

    categories: Mapped[list[str]] = mapped_column(
        JSON,
        nullable=False,
        default=list,
    )

    examples: Mapped[list[dict]] = mapped_column(
        JSON,
        nullable=False,
        default=list,
    )

    raw_sense: Mapped[dict] = mapped_column(
        JSON,
        nullable=False,
        default=dict,
    )

    etymology_text: Mapped[str | None] = mapped_column(
        Text,
        nullable=True,
    )

    visibility_status: Mapped[str] = mapped_column(
        String(30),
        default="visible",
        nullable=False,
    )

    admin_status: Mapped[str] = mapped_column(
        String(30),
        default="normal",
        nullable=False,
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
    )

    lexeme: Mapped["Lexeme"] = relationship(
        back_populates="senses",
    )

    source: Mapped["Source"] = relationship()

    embedding: Mapped["SenseEmbedding | None"] = relationship(
        back_populates="sense",
        cascade="all, delete-orphan",
        uselist=False,
    )

    selection_stat: Mapped["SenseSelectionStat | None"] = relationship(
        back_populates="sense",
        cascade="all, delete-orphan",
        uselist=False,
    )

    admin_override: Mapped["SenseAdminOverride | None"] = relationship(
        back_populates="sense",
        cascade="all, delete-orphan",
        uselist=False,
    )

    tags: Mapped[list["SenseTag"]] = relationship(
        back_populates="sense",
        cascade="all, delete-orphan",
    )

    relations: Mapped[list["SenseRelation"]] = relationship(
        back_populates="from_sense",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )

    __table_args__ = (
        UniqueConstraint(
            "source_id",
            "source_locator",
            name="uq_senses_source_locator",
        ),
        Index(
            "ix_senses_lexeme_id",
            "lexeme_id",
        ),
        Index(
            "ix_senses_visibility_status",
            "visibility_status",
        ),
        CheckConstraint(
            "visibility_status IN ('visible', 'hidden')",
            name="ck_senses_visibility_status",
        ),
        CheckConstraint(
            "admin_status IN ('normal', 'edited', 'merged', 'suppressed')",
            name="ck_senses_admin_status",
        ),
    )


class SenseSelectionStat(Base):
    __tablename__ = "sense_selection_stats"

    sense_id: Mapped[int] = mapped_column(
        ForeignKey("senses.id", ondelete="CASCADE"),
        primary_key=True,
    )

    selection_count: Mapped[int] = mapped_column(
        Integer,
        default=0,
        nullable=False,
    )

    last_selected_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )

    sense: Mapped["Sense"] = relationship(
        back_populates="selection_stat",
    )


class SenseSelectionEvent(Base):
    __tablename__ = "sense_selection_events"

    id: Mapped[int] = mapped_column(primary_key=True)

    sense_id: Mapped[int] = mapped_column(
        ForeignKey("senses.id", ondelete="CASCADE"),
        nullable=False,
    )

    query_text: Mapped[str] = mapped_column(
        Text,
        nullable=False,
        default="",
    )

    selected_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
    )

    sense: Mapped["Sense"] = relationship()

    __table_args__ = (
        Index(
            "ix_sense_selection_events_sense_id",
            "sense_id",
        ),
    )


class WordSearchStat(Base):
    __tablename__ = "word_search_stats"

    id: Mapped[int] = mapped_column(primary_key=True)

    language_id: Mapped[int] = mapped_column(
        ForeignKey("languages.id"),
        nullable=False,
    )

    normalized_lemma: Mapped[str] = mapped_column(
        String(300),
        nullable=False,
    )

    search_count: Mapped[int] = mapped_column(
        Integer,
        default=0,
        nullable=False,
    )

    last_searched_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )

    language: Mapped["Language"] = relationship()

    __table_args__ = (
        UniqueConstraint(
            "language_id",
            "normalized_lemma",
            name="uq_word_search_stats_language_lemma",
        ),
        Index(
            "ix_word_search_stats_language_lemma",
            "language_id",
            "normalized_lemma",
        ),
        Index(
            "ix_word_search_stats_count",
            "search_count",
        ),
    )


class WordSearchEvent(Base):
    __tablename__ = "word_search_events"

    id: Mapped[int] = mapped_column(primary_key=True)

    language_id: Mapped[int] = mapped_column(
        ForeignKey("languages.id"),
        nullable=False,
    )

    query_text: Mapped[str] = mapped_column(
        Text,
        nullable=False,
        default="",
    )

    normalized_query: Mapped[str] = mapped_column(
        String(300),
        nullable=False,
    )

    searched_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
    )

    language: Mapped["Language"] = relationship()

    __table_args__ = (
        Index(
            "ix_word_search_events_language_query",
            "language_id",
            "normalized_query",
        ),
    )


class SenseAdminOverride(Base):
    __tablename__ = "sense_admin_overrides"

    sense_id: Mapped[int] = mapped_column(
        ForeignKey("senses.id", ondelete="CASCADE"),
        primary_key=True,
    )

    is_hidden: Mapped[bool] = mapped_column(
        Boolean,
        default=False,
        nullable=False,
    )

    pinned_rank: Mapped[int | None] = mapped_column(
        Integer,
        nullable=True,
    )

    label_override: Mapped[str | None] = mapped_column(
        String(300),
        nullable=True,
    )

    definition_override: Mapped[str | None] = mapped_column(
        Text,
        nullable=True,
    )

    notes: Mapped[str] = mapped_column(
        Text,
        default="",
        nullable=False,
    )

    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )

    sense: Mapped["Sense"] = relationship(
        back_populates="admin_override",
    )


class SemanticTag(Base):
    __tablename__ = "semantic_tags"

    id: Mapped[int] = mapped_column(primary_key=True)

    label: Mapped[str] = mapped_column(
        String(120),
        nullable=False,
    )

    normalized_label: Mapped[str] = mapped_column(
        String(120),
        nullable=False,
    )

    category: Mapped[str] = mapped_column(
        String(80),
        default="general",
        nullable=False,
    )

    __table_args__ = (
        UniqueConstraint(
            "normalized_label",
            name="uq_semantic_tags_normalized_label",
        ),
    )


class SenseTag(Base):
    __tablename__ = "sense_tags"

    id: Mapped[int] = mapped_column(primary_key=True)

    sense_id: Mapped[int] = mapped_column(
        ForeignKey("senses.id", ondelete="CASCADE"),
        nullable=False,
    )

    tag_id: Mapped[int] = mapped_column(
        ForeignKey("semantic_tags.id", ondelete="CASCADE"),
        nullable=False,
    )

    weight: Mapped[float] = mapped_column(
        Float,
        default=1.0,
        nullable=False,
    )

    source: Mapped[str] = mapped_column(
        String(40),
        default="manual",
        nullable=False,
    )

    sense: Mapped["Sense"] = relationship(
        back_populates="tags",
    )

    tag: Mapped["SemanticTag"] = relationship()

    __table_args__ = (
        UniqueConstraint(
            "sense_id",
            "tag_id",
            name="uq_sense_tags_pair",
        ),
        CheckConstraint(
            "weight >= 0 AND weight <= 1",
            name="ck_sense_tags_weight",
        ),
    )


class SenseEmbedding(Base):
    __tablename__ = "sense_embeddings"

    sense_id: Mapped[int] = mapped_column(
        ForeignKey("senses.id", ondelete="CASCADE"),
        primary_key=True,
    )

    embedding_model: Mapped[str] = mapped_column(
        String(200),
        nullable=False,
    )

    embedding_dimensions: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
    )

    embedded_text: Mapped[str] = mapped_column(
        Text,
        nullable=False,
    )

    embedding: Mapped[list[float]] = mapped_column(
        Vector(768),
        nullable=False,
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
    )

    sense: Mapped["Sense"] = relationship(
        back_populates="embedding",
    )

    __table_args__ = (
        Index(
            "ix_sense_embeddings_model",
            "embedding_model",
        ),
    )


class SenseRelation(Base):
    """
    Lexical relation harvested automatically from Kaikki (Stage 2) and OEWN
    (Stage 3). 'from' side is a stored Sense; 'to' side is a surface string
    plus an optional resolved target lexeme. NOT human-reviewed — curated
    upstream by Wiktionary editors / WordNet lexicographers.
    """
    __tablename__ = "sense_relations"

    id: Mapped[int] = mapped_column(primary_key=True)
    from_sense_id: Mapped[int] = mapped_column(
        ForeignKey("senses.id", ondelete="CASCADE"), nullable=False
    )
    relation_type: Mapped[str] = mapped_column(String(40), nullable=False)
    provenance: Mapped[str] = mapped_column(String(20), nullable=False)
    # 'kaikki' | 'oewn' | 'omw-ja' | 'omw-arb' | 'awn4'
    # awn4 is a DISTINCT tier deliberately: it is AI-translated (Gemini+Claude
    # from OEWN 2024) and ranking must be able to discount it independently.
    target_text: Mapped[str] = mapped_column(String(300), nullable=False)
    target_normalized: Mapped[str] = mapped_column(String(300), nullable=False)
    target_sense_hint: Mapped[str | None] = mapped_column(Text, nullable=True)
    target_lexeme_id: Mapped[int | None] = mapped_column(
        ForeignKey("lexemes.id", ondelete="SET NULL"), nullable=True
    )
    source_id: Mapped[int] = mapped_column(ForeignKey("sources.id"), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    from_sense: Mapped["Sense"] = relationship(back_populates="relations")
    target_lexeme: Mapped["Lexeme | None"] = relationship()
    source: Mapped["Source"] = relationship()

    __table_args__ = (
        Index("ix_sense_relations_from_sense", "from_sense_id"),
        Index("ix_sense_relations_target_normalized", "target_normalized"),
        Index("ix_sense_relations_type", "relation_type"),
        UniqueConstraint(
            "from_sense_id", "relation_type", "provenance", "target_normalized",
            name="uq_sense_relations_edge",
        ),
        CheckConstraint(
            "relation_type IN ('synonym','near_synonym','antonym','hypernym',"
            "'hyponym','derived','related','coordinate')",
            name="ck_sense_relations_type",
        ),
        CheckConstraint(
            "provenance IN ('kaikki','oewn','omw-ja','omw-arb','awn4',"
            "'omw-es','omw-el','omw-pl','omw-he','omw-cmn','odenet','lsg')",
            name="ck_sense_relations_provenance",
        ),
    )


class SenseSynset(Base):
    """
    Wordnet synset membership for a sense, keyed by CILI identifier.

    This is the cross-source join surface: an English sense and a Japanese
    sense that share an `ili` value are members of the same interlingual
    synset. Root corroboration (Stage 5b) is an equality join on `ili`.
    Cross-source joins happen ONLY via `ili` or the canonical normalize_lemma
    key -- never via source-internal synset IDs, which are recorded solely
    for provenance/debugging (`source_synset_id`).

    Only indexed CILI values (`i` + digits) are stored. OEWN's ~16K proposed
    synsets carry ili="in" (unindexed) and are correctly excluded: they can
    corroborate nothing because no other source can reference them.
    """
    __tablename__ = "sense_synsets"

    id: Mapped[int] = mapped_column(primary_key=True)
    sense_id: Mapped[int] = mapped_column(
        ForeignKey("senses.id", ondelete="CASCADE"), nullable=False
    )
    ili: Mapped[str] = mapped_column(String(20), nullable=False)
    source_synset_id: Mapped[str | None] = mapped_column(String(120), nullable=True)
    source_id: Mapped[int] = mapped_column(ForeignKey("sources.id"), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    sense: Mapped["Sense"] = relationship()
    source: Mapped["Source"] = relationship()

    __table_args__ = (
        Index("ix_sense_synsets_ili", "ili"),
        Index("ix_sense_synsets_sense", "sense_id"),
        UniqueConstraint(
            "sense_id", "ili", "source_id",
            name="uq_sense_synsets_membership",
        ),
    )


class SenseTranslation(Base):
    """
    Sense-scoped translation link: an ENGLISH sense -> one target-language
    lemma, from the Kaikki `translations` arrays. Root selection's primary
    signal (MULTILINGUAL_EXPANSION_MODEL.md 2a) and the pivot's bridge (2c,
    read in reverse via target_lexeme_id).

    attachment records HOW the item was sense-scoped:
      'sense' -- lived on senses[].translations (placement IS scoping)
      'dis1'  -- entry-level, routed by wiktextract's _dis1 argmax
      'hint'  -- entry-level, routed by exact hint-vs-gloss match
    Unroutable entry-level items are NOT stored (Breakdown 4, Step 1b):
    a mis-scoped translation is a wrong-root risk, not harmless breadth.
      'llm'  -- root selection's LLM proposal (Breakdown 4.5, decision 1d)

    target_normalized uses normalize_lemma(word, target_code) -- the
    canonical key; target_lexeme_id resolves within the target language
    only, and is re-resolvable (SET NULL on lexeme delete).
    """
    __tablename__ = "sense_translations"

    id: Mapped[int] = mapped_column(primary_key=True)
    sense_id: Mapped[int] = mapped_column(
        ForeignKey("senses.id", ondelete="CASCADE"), nullable=False
    )
    language_id: Mapped[int] = mapped_column(
        ForeignKey("languages.id"), nullable=False
    )
    target_text: Mapped[str] = mapped_column(String(300), nullable=False)
    target_normalized: Mapped[str] = mapped_column(String(300), nullable=False)
    target_lexeme_id: Mapped[int | None] = mapped_column(
        ForeignKey("lexemes.id", ondelete="SET NULL"), nullable=True
    )
    roman: Mapped[str | None] = mapped_column(String(300), nullable=True)
    attachment: Mapped[str] = mapped_column(String(12), nullable=False)
    source_id: Mapped[int] = mapped_column(ForeignKey("sources.id"), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    sense: Mapped["Sense"] = relationship()
    language: Mapped["Language"] = relationship()
    target_lexeme: Mapped["Lexeme | None"] = relationship()

    __table_args__ = (
        Index("ix_sense_translations_sense", "sense_id"),
        Index("ix_sense_translations_lang_norm", "language_id", "target_normalized"),
        Index("ix_sense_translations_target_lexeme", "target_lexeme_id"),
        CheckConstraint(
            "attachment IN ('sense','dis1','hint')",
            name="ck_sense_translations_attachment",
        ),
        UniqueConstraint(
            "sense_id", "language_id", "target_normalized",
            name="uq_sense_translations_link",
        ),
    )


class RootLlmAttempt(Base):
    """
    Resolve-once ledger for the `llm` root rung (Breakdown 4.5, decision 1d).
    One row per (english sense, target language) ever ASKED:
      'resolved'    a proposal resolved; the link lives in sense_translations
                    (attachment 'llm', or a repaired curated row per 1e)
      'unresolved'  proposals returned but none resolved to a real lexeme
                    with a viable display sense -- permanent negative cache
      'error'       transport/parse failure -- retryable
    `proposed` keeps the raw candidate list for audit; `model` records which
    model answered (source stays the stable 'llm-root' slug across upgrades).
    """
    __tablename__ = "root_llm_attempts"

    id: Mapped[int] = mapped_column(primary_key=True)
    sense_id: Mapped[int] = mapped_column(
        ForeignKey("senses.id", ondelete="CASCADE"), nullable=False
    )
    language_id: Mapped[int] = mapped_column(
        ForeignKey("languages.id"), nullable=False
    )
    status: Mapped[str] = mapped_column(String(12), nullable=False)
    model: Mapped[str] = mapped_column(String(120), nullable=False)
    proposed: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    resolved_lexeme_id: Mapped[int | None] = mapped_column(
        ForeignKey("lexemes.id", ondelete="SET NULL"), nullable=True
    )
    # Stage 16a. Exception class + message from the LAST failed attempt on
    # this pair; NULL on every non-error status, so a retry that resolves
    # CLEARS it rather than leaving a stale cause attached to a good row.
    # Without this the cause is printed to stderr and lost with the session,
    # which is how 240 rows became undiagnosable. Batched (Step 3) the same
    # column also carries the batch-wide transport failure, written
    # identically to every language in the failed call.
    error_detail: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    __table_args__ = (
        CheckConstraint(
            "status IN ('resolved','unresolved','error')",
            name="ck_root_llm_attempts_status",
        ),
        UniqueConstraint(
            "sense_id", "language_id", name="uq_root_llm_attempts_pair",
        ),
    )


# ---------------------------------------------------------------------------
# Green cards -- established (attested) names.
#
# These four tables REPLACE the dead curated EstablishedName/NameMeaning/
# NameRelationship trio (original DDL 847e777b2da9). That pipeline hung off
# `Concept` and required per-item review; this one hangs off the live
# Lexeme/Sense world and is derived entirely from category-level predicates,
# so it satisfies the zero-review constraint.
#
# EVERY ROW HERE IS DERIVED. There is no user or curator data in these
# tables, which is what makes the delete-and-rebuild population strategy in
# scripts/populate_established_names.py safe.
# ---------------------------------------------------------------------------

NAME_TYPES: tuple[str, ...] = ("given", "surname", "patronymic")
NAME_GENDERS: tuple[str, ...] = ("m", "f", "x", "u")

# Stage 3 writes only the first three. HOMOGRAPH and EQUIV_PROPAGATED are
# Stage 6's inheritance channels; they are admitted by the CHECK now so
# Stage 6 needs no second migration.
MEANING_CHANNELS: tuple[str, ...] = (
    "GLOSS_MEANING", "ETYM_MARKER", "ETYM_QUOTED",
    "HOMOGRAPH", "EQUIV_PROPAGATED",
)

NAME_EDGE_RELATIONS: tuple[str, ...] = (
    "VARIANT_OF", "DIMINUTIVE_OF", "FEM_EQUIV", "MASC_EQUIV", "EQUIV_EN",
)


# Stage 18. Provenance of the origin actually DISPLAYED on a green card.
#   category        parsed from a Wiktionary category (the Stage-14 pair)
#   llm_native      the model returned this row's own language
#   llm_foreign     the model returned a different language
#   llm_twin        as llm_foreign, and a cross-language romanization twin
#                   corroborated it (Stage 19a). Stored, NOT gated on --
#                   tightening to a strict bar later is then a display-time
#                   change over data already held, with no re-run and no
#                   additional quota.
#   llm_unknown     the model declined; 'unknown' is licensed and PREFERRED
#                   over a low-confidence guess
#   llm_error       transport/parse failure on that row
#   gradient_exempt homograph row, English by construction, never asked
NAME_ORIGIN_SOURCES: tuple[str, ...] = (
    "category", "llm_native", "llm_foreign", "llm_twin",
    "llm_unknown", "llm_error", "gradient_exempt",
)

NAME_ORIGIN_STATUSES: tuple[str, ...] = ("resolved", "unknown", "error")


def _sql_in(column: str, values: tuple[str, ...]) -> str:
    joined = ", ".join(f"'{v}'" for v in values)
    return f"{column} IN ({joined})"


class EstablishedName(Base):
    """
    One attested name, at the grain (language, normalized_lemma, name_type).

    NOT one row per sense. A lemma routinely carries several name senses in
    one language ("a male given name" / "a male given name, variant of X"),
    and shipping one card per sense would show the user the same name three
    times. `source_sense_id` points at the sense that WON the meaning
    waterfall, so provenance stays exact even though the grain is collapsed.

    Classification is per SENSE, so a lemma with a given-name sense AND a
    separate surname sense produces TWO rows, one per type -- which is what
    Stage 5e's "two graphs, never unioned" requires, and what makes the row
    count directly comparable to N1's per-bucket distinct-lemma figures.
    `is_also_surname` records the WITHIN-sense overlap only ("a surname,
    also a given name", 1,374 senses in en), where type selection is
    exclusive and GIVEN wins.
    """

    __tablename__ = "established_names"

    id: Mapped[int] = mapped_column(primary_key=True)

    language_id: Mapped[int] = mapped_column(
        ForeignKey("languages.id"), nullable=False
    )

    lemma: Mapped[str] = mapped_column(String(300), nullable=False)
    normalized_lemma: Mapped[str] = mapped_column(String(300), nullable=False)

    # Copied from Lexeme.romanization (Phase D), NOT re-derived. Copying is
    # what guarantees the green card and the yellow card show the SAME
    # romanization for the same lemma -- which the Stage 7c gradient merge
    # renders side by side.
    romanization: Mapped[str | None] = mapped_column(String(400), nullable=True)

    name_type: Mapped[str] = mapped_column(String(12), nullable=False)
    gender: Mapped[str] = mapped_column(String(1), nullable=False, default="u")
    is_also_surname: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False
    )

    source_lexeme_id: Mapped[int] = mapped_column(
        ForeignKey("lexemes.id", ondelete="CASCADE"), nullable=False
    )
    source_sense_id: Mapped[int] = mapped_column(
        ForeignKey("senses.id", ondelete="CASCADE"), nullable=False
    )

    meaning_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    meaning_channel: Mapped[str | None] = mapped_column(
        String(20), nullable=True
    )

    # GLOSS_EQUIV_EN is an EQUIVALENCE, not a meaning, so it gets its own
    # column and never occupies meaning_text. Kept ORTHOGONAL to the
    # waterfall rather than sitting inside it: a name whose gloss says
    # "equivalent to English John" AND whose etymology carries a real quoted
    # gloss should keep both facts, not lose the gloss to the higher-priority
    # equivalence. Stage 5a reads this to build EQUIV_EN edges; Stage 6b
    # reads it to propagate a meaning in.
    equiv_en_target: Mapped[str | None] = mapped_column(
        String(120), nullable=True
    )

    # Mechanism 2, in one column: the same-language non-name lexeme this name
    # is spelled identically to. A real FK, deliberately -- it is what lets
    # the card inherit that word's sense, embedding and expansion behaviour
    # instead of copying a string.
    homograph_lexeme_id: Mapped[int | None] = mapped_column(
        ForeignKey("lexemes.id", ondelete="SET NULL"), nullable=True
    )

    # Stage 6b. WHICH row a propagated meaning came from. `equiv_en_target`
    # is the raw extracted string; this is the row it actually resolved to,
    # which is what Stage 10's precision sample and Stage 8's label need.
    meaning_source_name_id: Mapped[int | None] = mapped_column(
        ForeignKey("established_names.id", ondelete="SET NULL"), nullable=True
    )

    # Stage 6a. 'corroborated' when the name's own etymology names the
    # homograph's lemma; 'spelling_only' when all we know is that the
    # spellings match. IMPORT_PREP_FINDINGS.md 5.1: `Lucius` shares a key
    # with `lucius` ("a fish, probably the pike") but descends from *lux*.
    # Sharing a key is not sharing a meaning, and 12,598 rows carry a link.
    homograph_confidence: Mapped[str | None] = mapped_column(
        String(16), nullable=True
    )

    # Stage 14. WHERE this name came from, when the source categories say
    # so. Findings 19.4 (F-2) falsified the assumption that a `from <Lang>`
    # tail would cover it: three of four sampled English-filed foreign names
    # carry no such tail. `origin_shape` distinguishes the two shapes that
    # DO occur, because they are different claims -- "English surnames from
    # Old French" says borrowed; "English renderings of Ukrainian female
    # given names" says this spelling is an English way of writing a
    # Ukrainian name. Collapsing them would make the chip lie about one of
    # them.
    origin_language_name: Mapped[str | None] = mapped_column(
        String(80), nullable=True
    )
    origin_shape: Mapped[str | None] = mapped_column(
        String(12), nullable=True
    )

    # Stage 18c. The origin actually RENDERED, and where it came from.
    # SEPARATE from the Stage-14 pair above, deliberately: an LLM origin has
    # no shape, so writing it into origin_language_name with a null
    # origin_shape would violate ck_established_names_origin_pair. Widening
    # that CHECK would have collapsed a real distinction -- "from Old
    # French", "Ukrainian rendering" and "a model asserts Arabic" are three
    # different claims -- and would have moved §20.6's census invariants.
    # The cost of two columns is one column; the cost of one column is that
    # derived and asserted origins stop being separable.
    #
    # DERIVED CACHE, not source of truth. name_origin_attempts is the
    # ledger; populate_established_names.py --pass origin rebuilds these two
    # after every --pass names, exactly as established_name_tokens is
    # rebuilt from meaning_text.
    display_origin_language: Mapped[str | None] = mapped_column(
        String(80), nullable=True
    )
    origin_source: Mapped[str | None] = mapped_column(
        String(24), nullable=True
    )

    # Wiktionary's OWN flag that the entry is filed under the wrong language
    # header ("English entries with incorrect language header"). An
    # editorial backlog marker, not a linguistic classification -- which is
    # exactly why it is stored and surfaced rather than acted on. It is the
    # closest thing to derivable evidence available under the zero-review
    # constraint, and it is not proof.
    language_header_warning: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=text("false")
    )

    cluster_id: Mapped[int | None] = mapped_column(
        ForeignKey("established_name_clusters.id", ondelete="SET NULL"),
        nullable=True,
    )

    popularity_rank: Mapped[int | None] = mapped_column(Integer, nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    language: Mapped["Language"] = relationship()
    source_lexeme: Mapped["Lexeme"] = relationship(
        foreign_keys=[source_lexeme_id]
    )
    source_sense: Mapped["Sense"] = relationship()
    homograph_lexeme: Mapped["Lexeme | None"] = relationship(
        foreign_keys=[homograph_lexeme_id]
    )
    meaning_source_name: Mapped["EstablishedName | None"] = relationship(
        foreign_keys=[meaning_source_name_id], remote_side=[id]
    )
    cluster: Mapped["EstablishedNameCluster | None"] = relationship(
        foreign_keys=[cluster_id]
    )

    tokens: Mapped[list["EstablishedNameToken"]] = relationship(
        back_populates="name",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )

    __table_args__ = (
        # Also serves Stage 7c's (language_id, normalized_lemma) lookup:
        # a btree index on (a, b, c) answers a prefix query on (a, b), so no
        # second index is created for it.
        UniqueConstraint(
            "language_id", "normalized_lemma", "name_type",
            name="uq_established_names_key",
        ),
        Index("ix_established_names_homograph", "homograph_lexeme_id"),
        Index("ix_established_names_cluster", "cluster_id"),
        Index("ix_established_names_meaning_source", "meaning_source_name_id"),
        CheckConstraint(
            "homograph_confidence IS NULL OR homograph_confidence IN "
            "('corroborated', 'spelling_only')",
            name="ck_established_names_homograph_confidence",
        ),
        CheckConstraint(
            "origin_shape IS NULL OR origin_shape IN ('from', 'rendering')",
            name="ck_established_names_origin_shape",
        ),
        CheckConstraint(
            "(origin_language_name IS NULL AND origin_shape IS NULL) OR "
            "(origin_language_name IS NOT NULL AND origin_shape IS NOT NULL)",
            name="ck_established_names_origin_pair",
        ),
        CheckConstraint(
            f"origin_source IS NULL OR "
            f"{_sql_in('origin_source', NAME_ORIGIN_SOURCES)}",
            name="ck_established_names_origin_source",
        ),
        # A displayed origin must name its provenance. Blank-over-wrong, in
        # the schema, same reasoning as ck_established_names_meaning_pair.
        # NO inverse constraint: gradient_exempt, llm_unknown and llm_error
        # are exactly the states with a provenance and NO display value.
        CheckConstraint(
            "display_origin_language IS NULL OR origin_source IS NOT NULL",
            name="ck_established_names_display_origin_pair",
        ),
        # A source row without the matching channel is a provenance claim
        # with nothing behind it -- the same blank-over-wrong reasoning that
        # produced ck_established_names_meaning_pair.
        CheckConstraint(
            "meaning_source_name_id IS NULL OR "
            "meaning_channel = 'EQUIV_PROPAGATED'",
            name="ck_established_names_meaning_source_channel",
        ),
        CheckConstraint(
            "meaning_source_name_id IS NULL OR meaning_source_name_id <> id",
            name="ck_established_names_no_self_propagation",
        ),
        Index("ix_established_names_source_lexeme", "source_lexeme_id"),
        CheckConstraint(
            _sql_in("name_type", NAME_TYPES),
            name="ck_established_names_name_type",
        ),
        CheckConstraint(
            _sql_in("gender", NAME_GENDERS),
            name="ck_established_names_gender",
        ),
        CheckConstraint(
            f"meaning_channel IS NULL OR "
            f"{_sql_in('meaning_channel', MEANING_CHANNELS)}",
            name="ck_established_names_meaning_channel",
        ),
        # Blank-over-wrong, enforced in the schema: a meaning without a
        # recorded provenance channel cannot be labelled honestly in Stage
        # 6c, and a channel without text is a claim with nothing behind it.
        CheckConstraint(
            "(meaning_text IS NULL AND meaning_channel IS NULL) OR "
            "(meaning_text IS NOT NULL AND meaning_channel IS NOT NULL)",
            name="ck_established_names_meaning_pair",
        ),
    )


class EstablishedNameToken(Base):
    """
    Materialized mechanism-1 join surface: one row per content token of a
    name's meaning_text.

    `token` is the JOIN KEY and is stored as normalize_lemma(tok, "en") --
    the same canonical key Lexeme.normalized_lemma uses -- because Stage 7b
    joins yellow-card English lemmas against it directly.

    `token_lexeme_id` is a RESOLVABILITY witness, not the join key. It is
    deliberately not load-bearing: a token like "light" is a noun, a verb and
    an adjective lexeme in English, and a yellow card may surface any of
    them, so pinning one lexeme id would silently drop the other two.
    """

    __tablename__ = "established_name_tokens"

    id: Mapped[int] = mapped_column(primary_key=True)

    established_name_id: Mapped[int] = mapped_column(
        ForeignKey("established_names.id", ondelete="CASCADE"), nullable=False
    )
    token: Mapped[str] = mapped_column(String(80), nullable=False)
    token_lexeme_id: Mapped[int | None] = mapped_column(
        ForeignKey("lexemes.id", ondelete="SET NULL"), nullable=True
    )

    name: Mapped["EstablishedName"] = relationship(back_populates="tokens")

    __table_args__ = (
        UniqueConstraint(
            "established_name_id", "token",
            name="uq_established_name_tokens_pair",
        ),
        Index("ix_established_name_tokens_token", "token"),
        Index("ix_established_name_tokens_lexeme", "token_lexeme_id"),
    )


class NameOriginAttempt(Base):
    """
    Resolve-once ledger for the LLM origin pass (Stages 18-22).

    KEYED ON THE NATURAL GRAIN (language_id, normalized_lemma, name_type),
    matching uq_established_names_key -- NOT on established_names.id.
    populate_established_names.py:213 runs
    `DELETE FROM established_names WHERE language_id = :lid` and re-inserts,
    so every row gets a NEW id on every rebuild and an id-keyed ledger would
    be orphaned the first time anyone ran `--pass names`. The natural grain
    is stable across rebuilds by construction.

    This ledger holds ONLY paid-for verdicts. Gradient exemptions are NOT
    stored here: they are derivable from homograph_lexeme_id on the same
    row, so storing them would buy nothing and introduce a staleness class
    (a rebuild that changes a row's homograph status would leave a wrong
    ledger row behind). --pass origin re-derives them each time.

    source_sense_id is REQUIRED, not optional. G-5 measured 37.1% of
    given-name rows as multi-sense; storing which sense was asked about
    makes the eventual move to sense-grain origin a migration rather than a
    full re-run at 1.65x quota. ON DELETE SET NULL rather than CASCADE: a
    re-import that replaces senses must not destroy the verdicts.
    """

    __tablename__ = "name_origin_attempts"

    id: Mapped[int] = mapped_column(primary_key=True)

    language_id: Mapped[int] = mapped_column(
        ForeignKey("languages.id"), nullable=False
    )
    normalized_lemma: Mapped[str] = mapped_column(String(300), nullable=False)
    name_type: Mapped[str] = mapped_column(String(12), nullable=False)

    status: Mapped[str] = mapped_column(String(12), nullable=False)

    # Both passes' RAW verdict strings, kept whatever they say. Stage 20h's
    # vocabulary census reads these to close the open->closed decision with
    # evidence; a column that stored only in-vocabulary values could not.
    pass_a_raw: Mapped[str | None] = mapped_column(String(80), nullable=True)
    pass_b_raw: Mapped[str | None] = mapped_column(String(80), nullable=True)

    # Normalized to the closed vocabulary, or 'other'.
    origin: Mapped[str | None] = mapped_column(String(80), nullable=True)
    confidence: Mapped[str | None] = mapped_column(String(8), nullable=True)
    is_coined: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    in_vocabulary: Mapped[bool | None] = mapped_column(Boolean, nullable=True)

    # Which twin language was SENT as evidence (Stage 19a). Recorded on the
    # ledger and not only in name_origin_twins because this is provenance
    # about the paid call: what the model was actually shown.
    twin_language: Mapped[str | None] = mapped_column(
        String(16), nullable=True
    )

    source_sense_id: Mapped[int | None] = mapped_column(
        ForeignKey("senses.id", ondelete="SET NULL"), nullable=True
    )

    model: Mapped[str] = mapped_column(String(120), nullable=False)
    error_detail: Mapped[str | None] = mapped_column(Text, nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(),
        onupdate=func.now(), nullable=False
    )

    __table_args__ = (
        UniqueConstraint(
            "language_id", "normalized_lemma", "name_type",
            name="uq_name_origin_attempts_key",
        ),
        CheckConstraint(
            _sql_in("status", NAME_ORIGIN_STATUSES),
            name="ck_name_origin_attempts_status",
        ),
        CheckConstraint(
            _sql_in("name_type", NAME_TYPES),
            name="ck_name_origin_attempts_name_type",
        ),
    )


class NameOriginTwin(Base):
    """
    Materialized cross-language romanization lookup (Stage 19a).

    One row per (English name row, twin language): does a same-name_type
    name exist in another language whose COALESCE(romanization, lemma)
    normalizes to the same key?

    MATERIALIZED, NOT JOINED LIVE. The lower()/normalize step on the join
    key defeats every index, and the recorded gotcha about correlated
    subqueries hanging on large tables applies directly.

    WHY IT EXISTS DESPITE G-3. At 7.8% it fails as a GATE, but succeeds as
    three other things: evidence in the prompt (it is close to a mechanical
    implementation of the adaptation criterion -- Amal has a twin, Abigail
    does not), a stratification axis for the Stage-20 precision sample, and
    a provenance value (llm_twin) that permits tightening to a strict bar
    later without re-running a single call.

    Keyed on the natural grain for the same reason the ledger is: rows are
    re-inserted with new ids on every --pass names.
    """

    __tablename__ = "name_origin_twins"

    id: Mapped[int] = mapped_column(primary_key=True)

    language_id: Mapped[int] = mapped_column(
        ForeignKey("languages.id"), nullable=False
    )
    normalized_lemma: Mapped[str] = mapped_column(String(300), nullable=False)
    name_type: Mapped[str] = mapped_column(String(12), nullable=False)

    twin_language_id: Mapped[int] = mapped_column(
        ForeignKey("languages.id"), nullable=False
    )
    twin_lemma: Mapped[str] = mapped_column(String(300), nullable=False)
    # The key both sides matched on, kept for audit: a twin that looks wrong
    # is almost always a normalization question, and re-deriving the key by
    # hand to check is exactly the friction this column removes.
    match_key: Mapped[str] = mapped_column(String(300), nullable=False)

    __table_args__ = (
        UniqueConstraint(
            "language_id", "normalized_lemma", "name_type",
            "twin_language_id",
            name="uq_name_origin_twins_key",
        ),
        CheckConstraint(
            _sql_in("name_type", NAME_TYPES),
            name="ck_name_origin_twins_name_type",
        ),
    )


class EstablishedNameEdge(Base):
    """
    Variant / equivalence edge between two names. Populated in Stage 5a;
    created here so the whole green-card skeleton lands in one migration.

    Relation types stay DISTINCT and are never collapsed: Stage 5b's chosen
    containment fix treats cross-language EQUIV_EN as a non-transitive leaf
    while DIMINUTIVE_OF chains freely, which is only expressible if the
    relation survives to the row.
    """

    __tablename__ = "established_name_edges"

    id: Mapped[int] = mapped_column(primary_key=True)

    source_name_id: Mapped[int] = mapped_column(
        ForeignKey("established_names.id", ondelete="CASCADE"), nullable=False
    )
    target_name_id: Mapped[int] = mapped_column(
        ForeignKey("established_names.id", ondelete="CASCADE"), nullable=False
    )
    relation_type: Mapped[str] = mapped_column(String(20), nullable=False)
    is_cross_language: Mapped[bool] = mapped_column(Boolean, nullable=False)

    source_name: Mapped["EstablishedName"] = relationship(
        foreign_keys=[source_name_id]
    )
    target_name: Mapped["EstablishedName"] = relationship(
        foreign_keys=[target_name_id]
    )

    __table_args__ = (
        UniqueConstraint(
            "source_name_id", "target_name_id", "relation_type",
            name="uq_established_name_edges_edge",
        ),
        Index("ix_established_name_edges_target", "target_name_id"),
        Index("ix_established_name_edges_relation", "relation_type"),
        CheckConstraint(
            _sql_in("relation_type", NAME_EDGE_RELATIONS),
            name="ck_established_name_edges_relation_type",
        ),
        CheckConstraint(
            "source_name_id <> target_name_id",
            name="ck_established_name_edges_no_self_loop",
        ),
    )


class EstablishedNameCluster(Base):
    """
    Materialized connected component over established_name_edges. Populated
    in Stage 5c; empty until then.

    `name_type` is on the cluster, not just on its members, because Stage 5e
    keeps the given-name and surname graphs as two graphs that are never
    unioned -- and Stage 10c's component-size ceiling (55-65 nodes) has to be
    checkable per type.

    `head_name_id` carries use_alter=True: established_names.cluster_id points
    here and this points back, so one of the two FKs must be added after both
    tables exist.
    """

    __tablename__ = "established_name_clusters"

    id: Mapped[int] = mapped_column(primary_key=True)

    name_type: Mapped[str] = mapped_column(String(12), nullable=False)
    head_name_id: Mapped[int | None] = mapped_column(
        ForeignKey(
            "established_names.id",
            ondelete="SET NULL",
            use_alter=True,
            name="fk_established_name_clusters_head",
        ),
        nullable=True,
    )
    size: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    is_cross_language_merged: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False
    )

    head_name: Mapped["EstablishedName | None"] = relationship(
        foreign_keys=[head_name_id]
    )

    __table_args__ = (
        Index("ix_established_name_clusters_type", "name_type"),
        CheckConstraint(
            _sql_in("name_type", NAME_TYPES),
            name="ck_established_name_clusters_name_type",
        ),
    )


# Blue-card semantic lookup association
generated_name_concepts = Table(
    "generated_name_concepts",
    Base.metadata,
    Column(
        "generated_name_id",
        ForeignKey("generated_names.id", ondelete="CASCADE"),
        primary_key=True,
    ),
    Column(
        "concept_id",
        ForeignKey("concepts.id", ondelete="CASCADE"),
        primary_key=True,
    ),
)