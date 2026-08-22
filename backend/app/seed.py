from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.session import SessionLocal
from app.models.generated_name import (
    GeneratedName,
    GenerationFlavorModel,
    Language,
    NamePart,
)
from app.models.semantic import (
    Concept,
    ConceptAlias,
    ConceptRelationship,
    Source,
    Word,
    WordSense,
)

from app.utils.text import normalize_text


def add_once(collection: list, item: object) -> None:
    """
    Append an ORM object to a relationship collection only if it
    has not already been linked.
    """
    if item not in collection:
        collection.append(item)


# -------------------------------------------------------------------
# Shared lookup helpers
# -------------------------------------------------------------------


def get_or_create_language(db: Session, name: str) -> Language:
    language = db.scalar(
        select(Language).where(Language.name == name)
    )

    if language is None:
        language = Language(name=name)
        db.add(language)
        db.flush()

    return language


def get_or_create_flavor(
    db: Session,
    name: str,
) -> GenerationFlavorModel:
    flavor = db.scalar(
        select(GenerationFlavorModel).where(
            GenerationFlavorModel.name == name
        )
    )

    if flavor is None:
        flavor = GenerationFlavorModel(name=name)
        db.add(flavor)
        db.flush()

    return flavor


def get_or_create_source(
    db: Session,
    *,
    name: str,
    source_type: str,
    url: str | None = None,
    license_name: str | None = None,
    notes: str | None = None,
) -> Source:
    source = db.scalar(
        select(Source).where(
            Source.name == name,
            Source.source_type == source_type,
        )
    )

    if source is None:
        source = Source(
            name=name,
            source_type=source_type,
            url=url,
            license=license_name,
            notes=notes,
        )
        db.add(source)
        db.flush()

    return source


# -------------------------------------------------------------------
# Concept helpers
# -------------------------------------------------------------------


def get_or_create_concept(
    db: Session,
    *,
    slug: str,
    label: str,
    description: str | None = None,
) -> Concept:
    concept = db.scalar(
        select(Concept).where(Concept.slug == slug)
    )

    if concept is None:
        concept = Concept(
            slug=slug,
            label=label,
            description=description,
        )
        db.add(concept)
        db.flush()

    return concept


def get_or_create_alias(
    db: Session,
    *,
    concept: Concept,
    text: str,
) -> ConceptAlias:
    normalized_text = normalize_text(text)

    alias = db.scalar(
        select(ConceptAlias).where(
            ConceptAlias.concept_id == concept.id,
            ConceptAlias.normalized_text == normalized_text,
        )
    )

    if alias is None:
        alias = ConceptAlias(
            concept=concept,
            text=text,
            normalized_text=normalized_text,
        )
        db.add(alias)
        db.flush()

    return alias


def get_or_create_concept_relationship(
    db: Session,
    *,
    source_concept: Concept,
    target_concept: Concept,
    relationship_type: str,
    weight: float,
) -> ConceptRelationship:
    concept_relationship = db.scalar(
        select(ConceptRelationship).where(
            ConceptRelationship.source_concept_id == source_concept.id,
            ConceptRelationship.target_concept_id == target_concept.id,
            ConceptRelationship.relationship_type == relationship_type,
        )
    )

    if concept_relationship is None:
        concept_relationship = ConceptRelationship(
            source_concept=source_concept,
            target_concept=target_concept,
            relationship_type=relationship_type,
            weight=weight,
        )
        db.add(concept_relationship)
        db.flush()
    else:
        concept_relationship.weight = weight

    return concept_relationship


# -------------------------------------------------------------------
# Yellow-card helpers
# -------------------------------------------------------------------


def get_or_create_word(
    db: Session,
    *,
    language: Language,
    text: str,
    transliteration: str | None,
    part_of_speech: str | None,
    notes: str | None,
    source: Source,
) -> Word:
    normalized_text = normalize_text(text)

    word = db.scalar(
        select(Word).where(
            Word.language_id == language.id,
            Word.normalized_text == normalized_text,
        )
    )

    if word is None:
        word = Word(
            language=language,
            text=text,
            normalized_text=normalized_text,
            transliteration=transliteration,
            part_of_speech=part_of_speech,
            notes=notes,
            source=source,
        )
        db.add(word)
        db.flush()

    return word


def get_or_create_word_sense(
    db: Session,
    *,
    word: Word,
    concept: Concept,
    gloss: str,
    is_primary: bool = True,
) -> WordSense:
    sense = db.scalar(
        select(WordSense).where(
            WordSense.word_id == word.id,
            WordSense.concept_id == concept.id,
        )
    )

    if sense is None:
        sense = WordSense(
            word=word,
            concept=concept,
            gloss=gloss,
            is_primary=is_primary,
        )
        db.add(sense)
        db.flush()

    return sense


# -------------------------------------------------------------------
# Green-card helpers
# -------------------------------------------------------------------


# -------------------------------------------------------------------
# Blue-card helpers
# -------------------------------------------------------------------


def get_or_create_generated_name(
    db: Session,
    *,
    slug: str,
    name: str,
    meaning: str,
    explanation: str,
    source_languages: list[Language],
    flavors: list[GenerationFlavorModel],
) -> GeneratedName:
    generated_name = db.scalar(
        select(GeneratedName).where(
            GeneratedName.slug == slug
        )
    )

    if generated_name is None:
        generated_name = GeneratedName(
            slug=slug,
            name=name,
            meaning=meaning,
            explanation=explanation,
        )
        db.add(generated_name)
        db.flush()
    else:
        # Keep the test data synchronized if you edit the seed later.
        generated_name.name = name
        generated_name.meaning = meaning
        generated_name.explanation = explanation

    for language in source_languages:
        add_once(generated_name.source_languages, language)

    for flavor in flavors:
        add_once(generated_name.flavors, flavor)

    db.flush()

    return generated_name


def get_or_create_name_part(
    db: Session,
    *,
    generated_name: GeneratedName,
    position: int,
    text: str,
    meaning: str,
    language: str,
    kind: str,
    note: str | None,
) -> NamePart:
    part = db.scalar(
        select(NamePart).where(
            NamePart.generated_name_id == generated_name.id,
            NamePart.position == position,
        )
    )

    if part is None:
        part = NamePart(
            generated_name=generated_name,
            position=position,
            text=text,
            meaning=meaning,
            language=language,
            kind=kind,
            note=note,
        )
        db.add(part)
        db.flush()
    else:
        part.text = text
        part.meaning = meaning
        part.language = language
        part.kind = kind
        part.note = note

    return part


def link_generated_name_to_concept(
    *,
    generated_name: GeneratedName,
    concept: Concept,
) -> None:
    add_once(generated_name.concepts, concept)


# -------------------------------------------------------------------
# Seed data
# -------------------------------------------------------------------


def seed_database() -> None:
    db = SessionLocal()

    try:
        print("Seeding source metadata...")

        seed_source = get_or_create_source(
            db,
            name="Namecraft vertical-slice seed data",
            source_type="development_seed",
            notes=(
                "Temporary development data for validating the light "
                "vertical slice. Verify linguistic entries and replace "
                "this source with authoritative references before "
                "production use."
            ),
        )

        print("Seeding languages...")

        english = get_or_create_language(db, "English")
        latin = get_or_create_language(db, "Latin")
        greek = get_or_create_language(db, "Greek")
        arabic = get_or_create_language(db, "Arabic")
        japanese = get_or_create_language(db, "Japanese")

        print("Seeding generation flavors...")

        default = get_or_create_flavor(db, "default")
        fantasy = get_or_create_flavor(db, "fantasy")
        ancient = get_or_create_flavor(db, "ancient-inspired")
        modern = get_or_create_flavor(db, "modern")

        print("Seeding concepts...")

        illumination = get_or_create_concept(
            db,
            slug="illumination",
            label="Light / illumination",
            description=(
                "Visible light, brightness, or the quality of "
                "providing illumination."
            ),
        )

        radiance = get_or_create_concept(
            db,
            slug="radiance",
            label="Radiance",
            description="A shining, glowing, or brilliant quality.",
        )

        clarity = get_or_create_concept(
            db,
            slug="clarity",
            label="Clarity",
            description=(
                "Clearness, lucidity, or freedom from obscurity."
            ),
        )

        dawn = get_or_create_concept(
            db,
            slug="dawn",
            label="Dawn",
            description=(
                "The beginning of daylight or the imagery of sunrise."
            ),
        )

        print("Seeding aliases...")

        for concept, aliases in [
            (
                illumination,
                ["light", "illumination", "brightness"],
            ),
            (
                radiance,
                ["radiance", "glow"],
            ),
            (
                clarity,
                ["clarity", "clearness"],
            ),
            (
                dawn,
                ["dawn", "sunrise"],
            ),
        ]:
            for alias in aliases:
                get_or_create_alias(
                    db,
                    concept=concept,
                    text=alias,
                )

        print("Seeding concept expansions...")

        get_or_create_concept_relationship(
            db,
            source_concept=illumination,
            target_concept=radiance,
            relationship_type="near_synonym",
            weight=0.95,
        )

        get_or_create_concept_relationship(
            db,
            source_concept=illumination,
            target_concept=clarity,
            relationship_type="symbolic",
            weight=0.70,
        )

        get_or_create_concept_relationship(
            db,
            source_concept=illumination,
            target_concept=dawn,
            relationship_type="symbolic",
            weight=0.65,
        )

        print("Seeding yellow-card words...")

        word_rows = [
            {
                "language": latin,
                "text": "lux",
                "transliteration": None,
                "part_of_speech": "noun",
                "notes": "Development seed entry.",
                "concept": illumination,
                "gloss": "light or illumination",
            },
            {
                "language": greek,
                "text": "φῶς",
                "transliteration": "phōs",
                "part_of_speech": "noun",
                "notes": "Development seed entry.",
                "concept": illumination,
                "gloss": "light",
            },
            {
                "language": arabic,
                "text": "نور",
                "transliteration": "nūr",
                "part_of_speech": "noun",
                "notes": "Development seed entry.",
                "concept": illumination,
                "gloss": "light",
            },
            {
                "language": japanese,
                "text": "光",
                "transliteration": "hikari",
                "part_of_speech": "noun",
                "notes": "Development seed entry.",
                "concept": illumination,
                "gloss": "light",
            },
            {
                "language": english,
                "text": "glow",
                "transliteration": None,
                "part_of_speech": "noun",
                "notes": "Development seed entry.",
                "concept": radiance,
                "gloss": "a steady radiance or soft light",
            },
            {
                "language": english,
                "text": "clarity",
                "transliteration": None,
                "part_of_speech": "noun",
                "notes": "Development seed entry.",
                "concept": clarity,
                "gloss": "clearness or lucidity",
            },
            {
                "language": english,
                "text": "dawn",
                "transliteration": None,
                "part_of_speech": "noun",
                "notes": "Development seed entry.",
                "concept": dawn,
                "gloss": "the beginning of daylight",
            },
        ]

        for row in word_rows:
            word = get_or_create_word(
                db,
                language=row["language"],
                text=row["text"],
                transliteration=row["transliteration"],
                part_of_speech=row["part_of_speech"],
                notes=row["notes"],
                source=seed_source,
            )

            get_or_create_word_sense(
                db,
                word=word,
                concept=row["concept"],
                gloss=row["gloss"],
            )

       
        print("Seeding blue-card generated names...")

        auravel = get_or_create_generated_name(
            db,
            slug="auravel",
            name="Auravel",
            meaning="Dawn and openness",
            explanation=(
                "A newly crafted name combining imagery of dawn "
                "with a light, open-sounding ending."
            ),
            source_languages=[latin],
            flavors=[default, fantasy, ancient],
        )

        get_or_create_name_part(
            db,
            generated_name=auravel,
            position=1,
            text="aur-",
            meaning="Dawn, glow, or golden light",
            language="Latin-inspired",
            kind="inspired",
            note="Inspired by words such as aurora.",
        )

        get_or_create_name_part(
            db,
            generated_name=auravel,
            position=2,
            text="-avel",
            meaning="Open, airy, flowing sound",
            language="Crafted",
            kind="crafted",
            note="An invented ending added for style and rhythm.",
        )

        link_generated_name_to_concept(
            generated_name=auravel,
            concept=dawn,
        )

        lucira = get_or_create_generated_name(
            db,
            slug="lucira",
            name="Lucira",
            meaning="Light and clarity",
            explanation=(
                "A newly crafted name built around a root "
                "associated with light."
            ),
            source_languages=[latin],
            flavors=[default, fantasy, modern],
        )

        get_or_create_name_part(
            db,
            generated_name=lucira,
            position=1,
            text="luc-",
            meaning="Light or brightness",
            language="Latin",
            kind="root",
            note="Related to the Latin word lux.",
        )

        get_or_create_name_part(
            db,
            generated_name=lucira,
            position=2,
            text="-ira",
            meaning="Soft, name-like ending",
            language="Crafted",
            kind="crafted",
            note="An invented ending added for rhythm.",
        )

        link_generated_name_to_concept(
            generated_name=lucira,
            concept=illumination,
        )

        link_generated_name_to_concept(
            generated_name=lucira,
            concept=clarity,
        )

        photel = get_or_create_generated_name(
            db,
            slug="photel",
            name="Photel",
            meaning="Light",
            explanation=(
                "A compact generated name built around "
                "a Greek root associated with light."
            ),
            source_languages=[greek],
            flavors=[ancient],
        )

        get_or_create_name_part(
            db,
            generated_name=photel,
            position=1,
            text="phot-",
            meaning="Light",
            language="Greek",
            kind="root",
            note=(
                "Connected to Greek-derived words associated "
                "with light."
            ),
        )

        get_or_create_name_part(
            db,
            generated_name=photel,
            position=2,
            text="-el",
            meaning="Compact name-like ending",
            language="Crafted",
            kind="crafted",
            note="An invented ending added for rhythm.",
        )

        link_generated_name_to_concept(
            generated_name=photel,
            concept=illumination,
        )

        db.commit()

        print("Seed data inserted successfully.")

    except Exception:
        db.rollback()
        raise

    finally:
        db.close()


if __name__ == "__main__":
    seed_database()