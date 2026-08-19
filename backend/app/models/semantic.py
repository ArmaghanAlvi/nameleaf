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

    established_names: Mapped[list["EstablishedName"]] = relationship(
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

    name_meanings: Mapped[list["NameMeaning"]] = relationship(
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


# Green-card models
class EstablishedName(Base):
    __tablename__ = "established_names"

    id: Mapped[int] = mapped_column(primary_key=True)

    language_id: Mapped[int] = mapped_column(
        ForeignKey("languages.id"),
        nullable=False,
    )

    name: Mapped[str] = mapped_column(String(200), nullable=False)
    native_script: Mapped[str | None] = mapped_column(String(200))
    transliteration: Mapped[str | None] = mapped_column(String(200))
    notes: Mapped[str | None] = mapped_column(Text)
    source_id: Mapped[int | None] = mapped_column(ForeignKey("sources.id"))

    language: Mapped["Language"] = relationship(
        back_populates="established_names",
    )

    source: Mapped["Source | None"] = relationship(
        back_populates="established_names",
    )

    meanings: Mapped[list["NameMeaning"]] = relationship(
        back_populates="established_name",
        cascade="all, delete-orphan",
    )

    outgoing_relationships: Mapped[list["NameRelationship"]] = relationship(
        foreign_keys="NameRelationship.source_name_id",
        back_populates="source_name",
        cascade="all, delete-orphan",
    )

    incoming_relationships: Mapped[list["NameRelationship"]] = relationship(
        foreign_keys="NameRelationship.target_name_id",
        back_populates="target_name",
    )


class NameMeaning(Base):
    __tablename__ = "name_meanings"

    id: Mapped[int] = mapped_column(primary_key=True)

    established_name_id: Mapped[int] = mapped_column(
        ForeignKey("established_names.id", ondelete="CASCADE"),
        nullable=False,
    )

    concept_id: Mapped[int] = mapped_column(
        ForeignKey("concepts.id", ondelete="CASCADE"),
        nullable=False,
    )

    explanation: Mapped[str] = mapped_column(Text, nullable=False)
    native_form: Mapped[str | None] = mapped_column(String(200))
    is_primary: Mapped[bool] = mapped_column(default=True, nullable=False)

    established_name: Mapped["EstablishedName"] = relationship(
        back_populates="meanings",
    )

    concept: Mapped["Concept"] = relationship(
        back_populates="name_meanings",
    )


class NameRelationship(Base):
    __tablename__ = "name_relationships"

    id: Mapped[int] = mapped_column(primary_key=True)

    source_name_id: Mapped[int] = mapped_column(
        ForeignKey("established_names.id", ondelete="CASCADE"),
        nullable=False,
    )

    target_name_id: Mapped[int] = mapped_column(
        ForeignKey("established_names.id", ondelete="CASCADE"),
        nullable=False,
    )

    relationship_type: Mapped[str] = mapped_column(
        String(50),
        nullable=False,
    )

    notes: Mapped[str | None] = mapped_column(Text)

    source_name: Mapped["EstablishedName"] = relationship(
        foreign_keys=[source_name_id],
        back_populates="outgoing_relationships",
    )

    target_name: Mapped["EstablishedName"] = relationship(
        foreign_keys=[target_name_id],
        back_populates="incoming_relationships",
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