"""Predicate taxonomy + multiplicity policy + alias normalizer.

Why a closed list at all? Because supersession needs a stable `key`
(`subject::predicate`). If the LLM is free to emit `works_at` on Monday and
`employer` on Wednesday for the same fact, the reconciler can't find the
existing memory and we get two parallel actives.

Why an escape hatch? Because real conversations talk about things you can't
predict. The closed list covers the common cases (employment, location, family,
pets, dietary, preferences); anything else becomes `other:<short_topic>` with
conservative `multiplicity=one`.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

MemoryType = Literal["fact", "preference", "opinion", "event"]
Multiplicity = Literal["one", "many"]


@dataclass(frozen=True)
class PredicateSpec:
    predicate: str
    type: MemoryType
    multiplicity: Multiplicity
    description: str               # human description, fed into the LLM prompt


# Order is the order the prompt will see — keeps it readable for the LLM.
PREDICATES: tuple[PredicateSpec, ...] = (
    # employment / professional
    PredicateSpec("employer",         "fact",       "one",  "Current employer / company"),
    PredicateSpec("job_title",        "fact",       "one",  "Current role or job title"),
    PredicateSpec("work_field",       "fact",       "one",  "Broad professional field (engineering, design, finance)"),
    PredicateSpec("previous_employer","fact",       "many", "A past employer (for history; never overwrites employer)"),

    # location
    PredicateSpec("lives_in",         "fact",       "one",  "Current city/place of residence"),
    PredicateSpec("lived_in",         "fact",       "many", "Past place of residence"),
    PredicateSpec("from",             "fact",       "one",  "Where the user is from / hometown"),
    PredicateSpec("timezone",         "fact",       "one",  "User's timezone"),

    # personal identifiers (rare in chat but worth supporting)
    PredicateSpec("name",             "fact",       "one",  "User's preferred name"),
    PredicateSpec("age",              "fact",       "one",  "User's age, if stated"),

    # relationships
    PredicateSpec("partner",          "fact",       "one",  "Spouse/partner"),
    PredicateSpec("family_member",    "fact",       "many", "Named family members"),
    PredicateSpec("friend",           "fact",       "many", "Named friends"),
    PredicateSpec("coworker",         "fact",       "many", "Named coworkers"),

    # pets
    PredicateSpec("owns_pet",         "fact",       "many", "Has a pet — object is the pet's identifier ('dog:Biscuit' or just 'Biscuit')"),
    PredicateSpec("pet_name",         "fact",       "many", "A pet's name"),
    PredicateSpec("pet_type",         "fact",       "many", "A pet's species (dog, cat, …)"),

    # dietary / health
    PredicateSpec("dietary_restriction","fact",     "many", "Vegetarian, vegan, kosher, halal, etc."),
    PredicateSpec("allergic_to",      "fact",       "many", "Allergy or intolerance"),
    PredicateSpec("medical_condition","fact",       "many", "Stable medical condition the user mentions"),

    # preferences
    PredicateSpec("likes",            "preference", "many", "Things the user likes"),
    PredicateSpec("dislikes",         "preference", "many", "Things the user dislikes"),
    PredicateSpec("prefers",          "preference", "many", "Stated preference between options"),
    PredicateSpec("avoids",           "preference", "many", "Things the user actively avoids"),
    PredicateSpec("hobby",            "preference", "many", "Hobbies / pastimes"),
    PredicateSpec("communication_style","preference","one", "How they want to be talked to (concise, formal, etc.)"),

    # opinions
    PredicateSpec("opinion",          "opinion",    "many", "Stated viewpoint or take on something — object is the topic"),

    # events
    PredicateSpec("attended",         "event",      "many", "Attended an event"),
    PredicateSpec("did",              "event",      "many", "Did/experienced something noteworthy"),
)

PREDICATE_INDEX: dict[str, PredicateSpec] = {p.predicate: p for p in PREDICATES}


# Alias mapping: LLM (or rule extractor) may emit these — we collapse them into
# the canonical predicate. Add aggressively as you observe miscalls.
_ALIASES: dict[str, str] = {
    "works_at": "employer",
    "work_at": "employer",
    "works_for": "employer",
    "employed_by": "employer",
    "company": "employer",
    "current_company": "employer",
    "current_employer": "employer",

    "job": "job_title",
    "role": "job_title",
    "position": "job_title",
    "title": "job_title",

    "lives": "lives_in",
    "lives_at": "lives_in",
    "live_in": "lives_in",
    "current_city": "lives_in",
    "city": "lives_in",
    "location": "lives_in",
    "based_in": "lives_in",

    "moved_from": "lived_in",
    "used_to_live_in": "lived_in",

    "originally_from": "from",
    "hometown": "from",

    "spouse": "partner",
    "husband": "partner",
    "wife": "partner",
    "boyfriend": "partner",
    "girlfriend": "partner",

    "has_pet": "owns_pet",
    "pet": "owns_pet",
    "dog": "owns_pet",
    "cat": "owns_pet",

    "diet": "dietary_restriction",
    "is_vegetarian": "dietary_restriction",
    "is_vegan": "dietary_restriction",

    "allergy": "allergic_to",

    "loves": "likes",
    "love": "likes",
    "enjoys": "likes",
    "fan_of": "likes",

    "hates": "dislikes",
    "dislike": "dislikes",
    "not_a_fan_of": "dislikes",

    "prefer": "prefers",

    "thinks": "opinion",
    "believes": "opinion",
    "view_on": "opinion",
    "opinion_on": "opinion",
}


def normalize_predicate(raw: str) -> str:
    """Map an arbitrary predicate string into a canonical form.

    Returns either a key from PREDICATE_INDEX or `other:<sanitized>` for things
    we don't recognize.
    """
    if not raw:
        return "other:unknown"
    p = raw.strip().lower().replace(" ", "_").replace("-", "_")
    p = p.removeprefix("other:")
    if p in PREDICATE_INDEX:
        return p
    if p in _ALIASES:
        return _ALIASES[p]
    return f"other:{p[:48]}"


def spec_for(predicate: str) -> PredicateSpec:
    """Lookup a spec, falling back to a conservative default for `other:*`."""
    if predicate in PREDICATE_INDEX:
        return PREDICATE_INDEX[predicate]
    # 'other:*' → treat as a fact, single-valued (so contradictions trigger supersession).
    return PredicateSpec(
        predicate=predicate,
        type="fact",
        multiplicity="one",
        description="open-ended fact",
    )


def predicates_prompt_block() -> str:
    """Render the predicate list as a markdown block for the LLM prompt."""
    lines = ["Preferred predicates (use one of these whenever the fact fits):"]
    for p in PREDICATES:
        lines.append(f"- `{p.predicate}` ({p.type}, multiplicity={p.multiplicity}): {p.description}")
    lines.append("")
    lines.append("If the fact doesn't fit any predicate above, use `other:<short_snake_case_topic>`.")
    return "\n".join(lines)
