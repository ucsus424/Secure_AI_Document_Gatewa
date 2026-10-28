from __future__ import annotations

from dataclasses import dataclass, field
import re
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional
from uuid import uuid4


PLACEHOLDER_PREFIX = "⟦"
PLACEHOLDER_SUFFIX = "⟧"

SENSITIVE_TYPES = {
    "PERSON",
    "COMPANY",
    "OBJECT",
    "ADDRESS",
    "IP",
    "SUBNET",
    "HOSTNAME",
    "MAC",
    "PHONE",
    "EMAIL",
    "CONTRACT",
    "INTERNAL_ID",
    "LOGIN",
    "SECRET",
    "MISC",
}


@dataclass(frozen=True)
class Entity:
    type: str
    value: str
    confidence: Optional[float] = None

    @classmethod
    def from_json(cls, payload: Dict[str, Any]) -> "Entity":
        entity_type = _first_present(payload, ("type", "category", "entity_type", "label", "kind"))
        value = _first_present(payload, ("value", "text", "original", "entity", "string", "match"))
        confidence = payload.get("confidence")

        if not isinstance(entity_type, str) or not entity_type.strip():
            raise ValueError("entity type is required")
        if not isinstance(value, str) or not value:
            raise ValueError("entity value is required")

        normalized_type = normalize_entity_type(entity_type)

        parsed_confidence: Optional[float]
        if confidence is None:
            parsed_confidence = None
        elif isinstance(confidence, (int, float)):
            parsed_confidence = float(confidence)
        else:
            parsed_confidence = None

        return cls(
            type=normalized_type,
            value=value,
            confidence=parsed_confidence,
        )

    def to_json(self) -> Dict[str, Any]:
        payload: Dict[str, Any] = {
            "type": self.type,
            "value": self.value,
        }
        if self.confidence is not None:
            payload["confidence"] = self.confidence
        return payload


@dataclass
class Mapping:
    document_id: str
    source_name: str
    entities: Dict[str, str]
    aliases: Dict[str, List[str]] = field(default_factory=dict)
    created_at_utc: Optional[str] = None

    @classmethod
    def create(
        cls,
        source_name: str,
        entities: Dict[str, str],
        aliases: Optional[Dict[str, List[str]]] = None,
    ) -> "Mapping":
        return cls(
            document_id=str(uuid4()),
            source_name=source_name,
            entities=entities,
            aliases=aliases or {},
            created_at_utc=datetime.now(timezone.utc).isoformat(),
        )

    @classmethod
    def from_json(cls, payload: Dict[str, Any]) -> "Mapping":
        document_id = payload.get("document_id")
        source_name = payload.get("source_name")
        entities = payload.get("entities")
        aliases = payload.get("aliases", {})
        created_at_utc = payload.get("created_at_utc")

        if not isinstance(document_id, str) or not document_id:
            raise ValueError("document_id is required")
        if not isinstance(source_name, str):
            raise ValueError("source_name is required")
        if not isinstance(entities, dict):
            raise ValueError("entities must be an object")
        if not isinstance(aliases, dict):
            raise ValueError("aliases must be an object")
        if created_at_utc is not None and not isinstance(created_at_utc, str):
            raise ValueError("created_at_utc must be a string")

        parsed_entities: Dict[str, str] = {}
        for key, value in entities.items():
            if not isinstance(key, str) or not key:
                raise ValueError("placeholder id must be a non-empty string")
            if not isinstance(value, str):
                raise ValueError("original value must be a string")
            parsed_entities[key] = value

        parsed_aliases: Dict[str, List[str]] = {}
        for key, values in aliases.items():
            if not isinstance(key, str) or not key:
                raise ValueError("alias placeholder id must be a non-empty string")
            if not isinstance(values, list) or not all(isinstance(value, str) for value in values):
                raise ValueError("alias values must be a string array")
            parsed_aliases[key] = values

        return cls(
            document_id=document_id,
            source_name=source_name,
            entities=parsed_entities,
            aliases=parsed_aliases,
            created_at_utc=created_at_utc,
        )

    def to_json(self) -> Dict[str, Any]:
        payload: Dict[str, Any] = {
            "document_id": self.document_id,
            "source_name": self.source_name,
            "entities": self.entities,
        }
        if self.aliases:
            payload["aliases"] = self.aliases
        if self.created_at_utc is not None:
            payload["created_at_utc"] = self.created_at_utc
        return payload

    def expected_placeholders(self) -> List[str]:
        return [format_placeholder(key) for key in self.entities]


def format_placeholder(key: str) -> str:
    return f"{PLACEHOLDER_PREFIX}{key}{PLACEHOLDER_SUFFIX}"


def normalize_entity_type(entity_type: str) -> str:
    normalized = re.sub(r"[^A-Z0-9]+", "_", entity_type.strip().upper()).strip("_")
    return normalized or "MISC"


def _first_present(payload: Dict[str, Any], keys: tuple[str, ...]) -> Any:
    for key in keys:
        if key in payload:
            return payload[key]
    return None
