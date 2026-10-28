from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Dict, Iterable, List, Set

from core.models import Entity, Mapping, format_placeholder


FLEXIBLE_SURFACE_TYPES = {"PERSON", "COMPANY", "OBJECT"}
WORD_TOKEN_RE = re.compile(r"[0-9A-Za-zА-Яа-яЁё]+")


@dataclass
class AnonymizationResult:
    text: str
    mapping: Mapping


def anonymize_text(text: str, entities: Iterable[Entity], source_name: str) -> AnonymizationResult:
    sorted_entities = _replacement_order(entities)
    counters: Dict[str, int] = {}
    value_to_key: Dict[str, str] = {}
    mapping_entities: Dict[str, str] = {}
    mapping_aliases: Dict[str, Set[str]] = {}
    sanitized = text

    for entity in sorted_entities:
        variants = [
            variant
            for variant in _surface_variants(text, entity)
            if variant in sanitized
        ]
        if not variants:
            continue

        key = value_to_key.get(entity.value)
        if key is None:
            counters[entity.type] = counters.get(entity.type, 0) + 1
            key = f"{entity.type}_{counters[entity.type]:03d}"
            value_to_key[entity.value] = key
            mapping_entities[key] = entity.value

        for variant in sorted(variants, key=len, reverse=True):
            previous = sanitized
            sanitized = sanitized.replace(variant, format_placeholder(key))
            if sanitized != previous and variant != entity.value:
                mapping_aliases.setdefault(key, set()).add(variant)

    return AnonymizationResult(
        text=sanitized,
        mapping=Mapping.create(
            source_name=source_name,
            entities=mapping_entities,
            aliases={
                key: sorted(values, key=lambda value: (-len(value), value))
                for key, values in mapping_aliases.items()
            },
        ),
    )


def _replacement_order(entities: Iterable[Entity]) -> List[Entity]:
    seen: Set[tuple[str, str]] = set()
    unique_entities: List[tuple[int, Entity]] = []
    for index, entity in enumerate(entities):
        key = (entity.type, entity.value)
        if key in seen:
            continue
        seen.add(key)
        unique_entities.append((index, entity))

    unique_entities.sort(key=lambda item: (-len(item[1].value), item[0]))
    return [entity for _, entity in unique_entities]


def _surface_variants(text: str, entity: Entity) -> List[str]:
    variants: List[str] = []
    seen: Set[str] = set()

    def add(value: str) -> None:
        if value and value not in seen:
            seen.add(value)
            variants.append(value)

    add(entity.value)

    if entity.type not in FLEXIBLE_SURFACE_TYPES:
        return variants

    tokens = WORD_TOKEN_RE.findall(entity.value)
    if len(tokens) < 2:
        return variants

    pattern = _surface_pattern(tokens)
    for match in pattern.finditer(text):
        add(match.group(0))

    return variants


def _surface_pattern(tokens: List[str]) -> re.Pattern[str]:
    escaped_tokens = [re.escape(token) for token in tokens]
    pattern = r"(?<!\w)" + r"[\W_]{0,8}".join(escaped_tokens) + r"(?!\w)"
    return re.compile(pattern)
