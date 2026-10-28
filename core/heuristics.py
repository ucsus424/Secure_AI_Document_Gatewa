from __future__ import annotations

import re
from typing import Iterable, List, Match, Pattern, Tuple

from core.detector import DetectorError
from core.models import Entity


PLACEHOLDER_RE = re.compile(r"⟦[A-Z0-9_]+⟧")

DIRECT_PATTERNS: Tuple[Tuple[str, Pattern[str]], ...] = (
    (
        "EMAIL",
        re.compile(r"(?i)\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b"),
    ),
    (
        "PHONE",
        re.compile(r"(?<!\d)(?:\+7|8)(?:[ \t().-]*\d){10}(?!\d)"),
    ),
    (
        "IP",
        re.compile(r"(?<![\d.])(?:\d{1,3}\.){3}\d{1,3}(?:/\d{1,2})?(?![\d.])"),
    ),
    (
        "MAC",
        re.compile(r"(?i)(?<![0-9A-F])(?:[0-9A-F]{2}[:-]){5}[0-9A-F]{2}(?![0-9A-F])"),
    ),
)

LABELED_VALUE_PATTERNS: Tuple[Tuple[str, Pattern[str]], ...] = (
    (
        "INTERNAL_ID",
        re.compile(
            r"(?i)\b(?:ИНН|КПП|ОГРН(?:ИП)?|БИК|СНИЛС)\b[ \t]*[:№]?[ \t]*(?P<value>[0-9][0-9 \t-]{7,19})"
        ),
    ),
    (
        "INTERNAL_ID",
        re.compile(r"(?i)(?:\bр/с\b|\bк/с\b)\s*[:№]?\s*(?P<value>\d{15,25})"),
    ),
    (
        "SECRET",
        re.compile(
            r"(?i)\b(?:пароль|password|токен|token|api[ _-]?key|секрет)\b\s*[:=]\s*(?P<value>\S+)"
        ),
    ),
)

ADDRESS_LABEL_RE = re.compile(
    r"(?i)^\s*(?:(?:юридический|почтовый|фактический)\s+адрес|"
    r"адрес\s+(?:объекта|местонахождения))\s*:\s*(?P<value>.+?)\s*$"
)
ADDRESS_CONTINUATION_RE = re.compile(
    r"(?i)(?:\b\d{6}\b|\b(?:область|край|республика|район)\b|"
    r"\bг\.?\s*о\.?\b|\bг\.|\bгород\b|\bул\.|\bулица\b|"
    r"\bд\.|\bдом\b|\bстр\.|\bстроение\b|\bкорпус\b|"
    r"\bпомещ(?:ение|\.)\b|\bофис\b)"
)
FIELD_LABEL_RE = re.compile(
    r"(?i)^\s*(?:e-?mail|телефон|тел\.|ИНН|КПП|ОГРН(?:ИП)?|БИК|"
    r"р/с|к/с|расч[её]тный\s+сч[её]т|корреспондентский\s+сч[её]т)\b"
)


def find_obvious_entities(text: str) -> List[Entity]:
    entities: List[Entity] = []

    for entity_type, pattern in DIRECT_PATTERNS:
        for match in pattern.finditer(text):
            _append_candidate(entities, entity_type, match.group(0))

    for entity_type, pattern in LABELED_VALUE_PATTERNS:
        for match in pattern.finditer(text):
            _append_candidate(entities, entity_type, _match_value(match))

    entities.extend(_find_structured_addresses(text.splitlines()))
    return _deduplicate(entities)


def require_no_obvious_entities(text: str) -> None:
    if find_obvious_entities(text):
        raise DetectorError("anonymized document still contains obvious sensitive data")


def _find_structured_addresses(lines: List[str]) -> List[Entity]:
    entities: List[Entity] = []

    for index, line in enumerate(lines):
        match = ADDRESS_LABEL_RE.match(line)
        if match is None:
            continue

        _append_candidate(entities, "ADDRESS", match.group("value"))
        for continuation in lines[index + 1:index + 5]:
            candidate = continuation.strip()
            if not candidate or FIELD_LABEL_RE.match(candidate):
                break
            if not ADDRESS_CONTINUATION_RE.search(candidate):
                break
            _append_candidate(entities, "ADDRESS", candidate)

    return entities


def _match_value(match: Match[str]) -> str:
    value = match.groupdict().get("value")
    return value if value is not None else match.group(0)


def _append_candidate(entities: List[Entity], entity_type: str, value: str) -> None:
    candidate = value.strip()
    if not candidate or PLACEHOLDER_RE.search(candidate):
        return
    entities.append(Entity(type=entity_type, value=candidate, confidence=1.0))


def _deduplicate(entities: Iterable[Entity]) -> List[Entity]:
    result: List[Entity] = []
    seen = set()
    for entity in entities:
        key = (entity.type, entity.value)
        if key in seen:
            continue
        seen.add(key)
        result.append(entity)
    return result
