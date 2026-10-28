from __future__ import annotations

from typing import List, Optional

from core.detector import DetectorError
from core.heuristics import find_obvious_entities
from core.models import Entity


DEFAULT_MAX_CHUNK_CHARS = 8000
DEFAULT_CHUNK_OVERLAP_CHARS = 400


def detect_entities(
    detector: object,
    text: str,
    chunk_lines: Optional[int] = None,
) -> List[Entity]:
    detect_text = getattr(detector, "detect_text", None)
    if not callable(detect_text):
        raise DetectorError("detector does not support text detection")

    entities: List[Entity] = []
    for chunk in split_text_chunks(text, chunk_lines):
        chunk_entities = detect_text(chunk)
        if not isinstance(chunk_entities, list) or not all(
            isinstance(item, Entity) for item in chunk_entities
        ):
            raise DetectorError("detector returned invalid entity data")
        entities.extend(chunk_entities)

    model_result = _deduplicate_entities(entities)
    if text.strip() and not model_result:
        raise DetectorError("detector returned no entities for a non-empty document")
    return _deduplicate_entities(model_result + find_obvious_entities(text))


def split_text_chunks(
    text: str,
    chunk_lines: Optional[int] = None,
    max_chunk_chars: int = DEFAULT_MAX_CHUNK_CHARS,
) -> List[str]:
    if max_chunk_chars < 1:
        raise DetectorError("maximum chunk size must be a positive integer")
    if chunk_lines is not None and chunk_lines < 1:
        raise DetectorError("chunk-lines must be a positive integer")

    if chunk_lines is None:
        return _split_by_char_limit(text, max_chunk_chars)

    lines = [line for line in text.splitlines() if line.strip()]
    line_chunks = [
        "\n".join(lines[index:index + chunk_lines])
        for index in range(0, len(lines), chunk_lines)
    ]
    chunks: List[str] = []
    for line_chunk in line_chunks or [""]:
        chunks.extend(_split_by_char_limit(line_chunk, max_chunk_chars))
    return chunks


def _split_by_char_limit(text: str, max_chunk_chars: int) -> List[str]:
    if len(text) <= max_chunk_chars:
        return [text]

    overlap = min(DEFAULT_CHUNK_OVERLAP_CHARS, max_chunk_chars // 4)
    chunks: List[str] = []
    start = 0

    while start < len(text):
        hard_end = min(start + max_chunk_chars, len(text))
        end = hard_end

        if hard_end < len(text):
            preferred_start = start + max_chunk_chars // 2
            newline = text.rfind("\n", preferred_start, hard_end)
            space = text.rfind(" ", preferred_start, hard_end)
            boundary = max(newline, space)
            if boundary > start:
                end = boundary + 1

        chunk = text[start:end]
        if chunk.strip():
            chunks.append(chunk)
        if end >= len(text):
            break

        next_start = end - overlap
        start = next_start if next_start > start else end

    return chunks or [""]


def _deduplicate_entities(entities: List[Entity]) -> List[Entity]:
    result: List[Entity] = []
    seen = set()
    for entity in entities:
        key = (entity.type, entity.value)
        if key in seen:
            continue
        seen.add(key)
        result.append(entity)
    return result
