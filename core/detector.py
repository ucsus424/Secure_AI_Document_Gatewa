from __future__ import annotations

import json
from pathlib import Path
from typing import List, Optional
from urllib import error, request

from core.models import Entity


DEFAULT_MODEL = "gemma4:12b-it-qat"
DEFAULT_OLLAMA_URL = "http://localhost:11434"
DEFAULT_PROMPT_PATH = Path(__file__).resolve().parent.parent / "prompts" / "dlp_system_prompt.txt"
DEFAULT_TIMEOUT_SECONDS = 300
ENTITIES_JSON_SCHEMA = {
    "type": "object",
    "properties": {
        "entities": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "type": {"type": "string"},
                    "value": {"type": "string"},
                    "confidence": {"type": "number"},
                },
                "required": ["type", "value"],
            },
        }
    },
    "required": ["entities"],
}


class DetectorError(RuntimeError):
    """Raised when sensitive-entity detection cannot be trusted."""


def load_prompt(path: Optional[Path] = None) -> str:
    prompt_path = path or DEFAULT_PROMPT_PATH
    try:
        return prompt_path.read_text(encoding="utf-8")
    except OSError as exc:
        raise DetectorError("detector prompt is unavailable") from exc


def parse_entities_json(raw_response: str) -> List[Entity]:
    cleaned = _strip_code_fence(raw_response).strip()
    try:
        payload = json.loads(cleaned)
    except json.JSONDecodeError as exc:
        payload = _find_last_json_array(cleaned, exc)

    payload = _normalize_payload(payload)

    if not isinstance(payload, list):
        raise DetectorError("model response must be a JSON array")

    entities: List[Entity] = []
    try:
        for item in payload:
            if not isinstance(item, dict):
                raise ValueError("entity item must be an object")
            entities.append(Entity.from_json(item))
    except ValueError as exc:
        raise DetectorError("model response contains invalid entity data") from exc

    return entities


def _normalize_payload(payload: object) -> object:
    if isinstance(payload, list):
        return payload

    if not isinstance(payload, dict):
        return payload

    if "type" in payload and "value" in payload:
        return [payload]

    for key in ("entities", "sensitive_entities", "results", "items", "data"):
        value = payload.get(key)
        if isinstance(value, list):
            return value

    typed_entities = []
    for entity_type, values in payload.items():
        if isinstance(values, str):
            typed_entities.append({"type": entity_type, "value": values})
        elif isinstance(values, list):
            for value in values:
                if isinstance(value, str):
                    typed_entities.append({"type": entity_type, "value": value})
                elif isinstance(value, dict) and "value" in value:
                    entity = dict(value)
                    entity.setdefault("type", entity_type)
                    typed_entities.append(entity)

    if typed_entities:
        return typed_entities

    return payload


def _find_last_json_array(text: str, original_error: json.JSONDecodeError) -> object:
    decoder = json.JSONDecoder()
    candidates = []

    for index, character in enumerate(text):
        if character != "[":
            continue
        try:
            payload, _ = decoder.raw_decode(text[index:])
        except json.JSONDecodeError:
            continue
        if isinstance(payload, list):
            candidates.append(payload)

    if not candidates:
        raise DetectorError("model response is not valid JSON") from original_error

    return candidates[-1]


class OllamaDetector:
    def __init__(
        self,
        model: str = DEFAULT_MODEL,
        base_url: str = DEFAULT_OLLAMA_URL,
        prompt: Optional[str] = None,
        prompt_path: Optional[Path] = None,
        timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS,
    ) -> None:
        self.model = model
        self.base_url = base_url.rstrip("/")
        self.prompt = prompt if prompt is not None else load_prompt(prompt_path)
        self.timeout_seconds = timeout_seconds

    def detect_text(self, text: str) -> List[Entity]:
        full_prompt = (
            f"{self.prompt}\n\n"
            "Проанализируй следующий текст и верни только JSON-массив.\n\n"
            f"Текст:\n{text}"
        )
        payload = {
            "model": self.model,
            "prompt": full_prompt,
            "stream": False,
            "format": ENTITIES_JSON_SCHEMA,
            "think": False,
            "options": {"num_ctx": 8192},
        }
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        http_request = request.Request(
            f"{self.base_url}/api/generate",
            data=data,
            headers={"Content-Type": "application/json"},
            method="POST",
        )

        try:
            with request.urlopen(http_request, timeout=self.timeout_seconds) as response:
                response_payload = json.loads(response.read().decode("utf-8"))
        except (OSError, error.URLError, json.JSONDecodeError) as exc:
            raise DetectorError("local Ollama detector request failed") from exc

        raw_model_text = response_payload.get("response")
        if not isinstance(raw_model_text, str):
            raise DetectorError("local Ollama detector returned an invalid payload")

        return parse_entities_json(raw_model_text)


def _strip_code_fence(raw_response: str) -> str:
    text = raw_response.strip()
    if not text.startswith("```"):
        return text

    lines = text.splitlines()
    if len(lines) >= 2 and lines[-1].strip() == "```":
        return "\n".join(lines[1:-1])
    return text
