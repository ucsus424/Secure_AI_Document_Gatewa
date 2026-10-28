from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Sequence, Tuple
from xml.etree import ElementTree
from zipfile import BadZipFile, ZipFile

from core.models import Mapping, format_placeholder
from core.restore import RestoreResult, restore_text


class OfficeAnonymizationError(RuntimeError):
    """Raised when an Office file cannot be anonymized safely."""


WORD_NS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
DRAWING_NS = "http://schemas.openxmlformats.org/drawingml/2006/main"
SPREADSHEET_NS = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
XML_SPACE_ATTR = "{http://www.w3.org/XML/1998/namespace}space"


@dataclass(frozen=True)
class OfficeFormat:
    suffix: str
    part_prefixes: Tuple[str, ...]
    group_tags: Tuple[Tuple[str, str], ...]
    text_tags: Tuple[Tuple[str, str], ...]


OFFICE_FORMATS = {
    ".docx": OfficeFormat(
        suffix=".docx",
        part_prefixes=("word/",),
        group_tags=((WORD_NS, "p"), (DRAWING_NS, "p")),
        text_tags=((WORD_NS, "t"), (DRAWING_NS, "t")),
    ),
    ".xlsx": OfficeFormat(
        suffix=".xlsx",
        part_prefixes=("xl/",),
        group_tags=((SPREADSHEET_NS, "si"), (SPREADSHEET_NS, "is"), (DRAWING_NS, "p")),
        text_tags=((SPREADSHEET_NS, "t"), (DRAWING_NS, "t")),
    ),
    ".pptx": OfficeFormat(
        suffix=".pptx",
        part_prefixes=("ppt/",),
        group_tags=((DRAWING_NS, "p"),),
        text_tags=((DRAWING_NS, "t"),),
    ),
}


def is_supported_office_file(path: Path) -> bool:
    return path.suffix.lower() in OFFICE_FORMATS


def extract_office_text(path: Path) -> str:
    office_format = _format_for(path)
    text_blocks: List[str] = []

    try:
        with ZipFile(path) as archive:
            for name in archive.namelist():
                if not _should_process_part(name, office_format):
                    continue
                data = archive.read(name)
                text_blocks.extend(_extract_xml_text(data, office_format))
    except (BadZipFile, OSError, ElementTree.ParseError) as exc:
        raise OfficeAnonymizationError("Office file text could not be extracted") from exc

    return "\n".join(block for block in text_blocks if block)


def anonymize_office_file(input_path: Path, output_path: Path, mapping: Mapping) -> None:
    office_format = _format_for(input_path)
    replacements = _mapping_replacements(mapping)
    temporary_path = output_path.with_name(f".{output_path.stem}.tmp{output_path.suffix}")

    try:
        with ZipFile(input_path) as source_archive, ZipFile(temporary_path, "w") as output_archive:
            for info in source_archive.infolist():
                data = source_archive.read(info.filename)
                if replacements and _should_process_part(info.filename, office_format):
                    data = _replace_xml_text(data, office_format, replacements)
                output_archive.writestr(info, data)

        _validate_no_residual_values(temporary_path, mapping)
        temporary_path.replace(output_path)
    except (BadZipFile, OSError, ElementTree.ParseError) as exc:
        _remove_if_exists(temporary_path)
        raise OfficeAnonymizationError("Office file could not be anonymized") from exc
    except OfficeAnonymizationError:
        _remove_if_exists(temporary_path)
        raise


def restore_docx_file(input_path: Path, output_path: Path, mapping: Mapping) -> RestoreResult:
    if input_path.suffix.lower() != ".docx":
        raise OfficeAnonymizationError("Office restore currently supports DOCX only")

    office_format = _format_for(input_path)
    source_text = extract_office_text(input_path)
    restore_result = restore_text(source_text, mapping)
    replacements = _restore_replacements(mapping)
    temporary_path = output_path.with_name(f".{output_path.stem}.tmp{output_path.suffix}")

    try:
        with ZipFile(input_path) as source_archive, ZipFile(temporary_path, "w") as output_archive:
            for info in source_archive.infolist():
                data = source_archive.read(info.filename)
                if replacements and _should_process_part(info.filename, office_format):
                    data = _replace_xml_text(data, office_format, replacements)
                output_archive.writestr(info, data)

        _validate_no_residual_placeholders(temporary_path, mapping)
        temporary_path.replace(output_path)
    except (BadZipFile, OSError, ElementTree.ParseError) as exc:
        _remove_if_exists(temporary_path)
        raise OfficeAnonymizationError("DOCX file could not be restored") from exc
    except OfficeAnonymizationError:
        _remove_if_exists(temporary_path)
        raise

    return restore_result


def _format_for(path: Path) -> OfficeFormat:
    office_format = OFFICE_FORMATS.get(path.suffix.lower())
    if office_format is None:
        raise OfficeAnonymizationError("unsupported Office file type")
    return office_format


def _should_process_part(name: str, office_format: OfficeFormat) -> bool:
    return name.endswith(".xml") and name.startswith(office_format.part_prefixes)


def _extract_xml_text(data: bytes, office_format: OfficeFormat) -> List[str]:
    root = ElementTree.fromstring(data)
    blocks: List[str] = []
    for group in _iter_matching(root, office_format.group_tags):
        text = "".join(node.text or "" for node in _text_nodes(group, office_format))
        if text:
            blocks.append(text)
    return blocks


def _replace_xml_text(
    data: bytes,
    office_format: OfficeFormat,
    replacements: Sequence[Tuple[str, str]],
) -> bytes:
    root = ElementTree.fromstring(data)
    changed = False

    for group in _iter_matching(root, office_format.group_tags):
        nodes = _text_nodes(group, office_format)
        if not nodes:
            continue
        if _replace_text_nodes(nodes, replacements):
            changed = True

    if not changed:
        return data

    return ElementTree.tostring(root, encoding="utf-8", xml_declaration=True)


def _text_nodes(root: ElementTree.Element, office_format: OfficeFormat) -> List[ElementTree.Element]:
    return list(_iter_matching(root, office_format.text_tags))


def _iter_matching(
    root: ElementTree.Element,
    tags: Iterable[Tuple[str, str]],
) -> Iterable[ElementTree.Element]:
    wanted = set(tags)
    for element in root.iter():
        if _split_tag(element.tag) in wanted:
            yield element


def _split_tag(tag: str) -> Tuple[Optional[str], str]:
    if tag.startswith("{"):
        namespace, _, local_name = tag[1:].partition("}")
        return namespace, local_name
    return None, tag


def _mapping_replacements(mapping: Mapping) -> List[Tuple[str, str]]:
    replacements: Dict[str, str] = {}
    for key, value in mapping.entities.items():
        replacements[value] = format_placeholder(key)
        for alias in mapping.aliases.get(key, []):
            replacements[alias] = format_placeholder(key)
    return sorted(replacements.items(), key=lambda item: len(item[0]), reverse=True)


def _restore_replacements(mapping: Mapping) -> List[Tuple[str, str]]:
    return sorted(
        (
            (format_placeholder(key), value)
            for key, value in mapping.entities.items()
        ),
        key=lambda item: len(item[0]),
        reverse=True,
    )


def _replace_text_nodes(
    nodes: Sequence[ElementTree.Element],
    replacements: Sequence[Tuple[str, str]],
) -> bool:
    changed = False

    for original, replacement in replacements:
        if not original:
            continue

        text = "".join(node.text or "" for node in nodes)
        starts = _replacement_starts(text, original)
        for start in reversed(starts):
            _replace_node_range(nodes, start, start + len(original), replacement)
            changed = True

    return changed


def _replacement_starts(text: str, original: str) -> List[int]:
    starts: List[int] = []
    offset = 0
    while True:
        index = text.find(original, offset)
        if index < 0:
            return starts
        starts.append(index)
        offset = index + len(original)


def _replace_node_range(
    nodes: Sequence[ElementTree.Element],
    start: int,
    end: int,
    replacement: str,
) -> None:
    start_index, start_offset = _locate_character(nodes, start)
    end_index, end_offset = _locate_character(nodes, end - 1)
    end_offset += 1

    if start_index == end_index:
        text = nodes[start_index].text or ""
        _set_node_text(
            nodes[start_index],
            text[:start_offset] + replacement + text[end_offset:],
        )
        return

    start_text = nodes[start_index].text or ""
    end_text = nodes[end_index].text or ""
    _set_node_text(nodes[start_index], start_text[:start_offset] + replacement)
    for index in range(start_index + 1, end_index):
        _set_node_text(nodes[index], "")
    _set_node_text(nodes[end_index], end_text[end_offset:])


def _locate_character(
    nodes: Sequence[ElementTree.Element],
    character_index: int,
) -> Tuple[int, int]:
    offset = 0
    for index, node in enumerate(nodes):
        text = node.text or ""
        if character_index < offset + len(text):
            return index, character_index - offset
        offset += len(text)
    raise OfficeAnonymizationError("replacement range is outside the Office text group")


def _set_node_text(node: ElementTree.Element, value: str) -> None:
    node.text = value
    if value and (value[0].isspace() or value[-1].isspace()):
        node.set(XML_SPACE_ATTR, "preserve")


def _validate_no_residual_values(path: Path, mapping: Mapping) -> None:
    text = extract_office_text(path)
    residual_keys = [
        key
        for key, value in mapping.entities.items()
        if value and value in text
    ]
    if residual_keys:
        raise OfficeAnonymizationError(
            "Office anonymization left residual sensitive placeholders: "
            + ", ".join(residual_keys)
        )


def _validate_no_residual_placeholders(path: Path, mapping: Mapping) -> None:
    text = extract_office_text(path)
    residual_keys = [
        key
        for key in mapping.entities
        if format_placeholder(key) in text
    ]
    if residual_keys:
        raise OfficeAnonymizationError(
            "DOCX restore left residual placeholders: "
            + ", ".join(residual_keys)
        )


def _remove_if_exists(path: Path) -> None:
    try:
        path.unlink()
    except FileNotFoundError:
        pass


DOCUMENT_ID_PROPERTY = "SecureGatewayDocumentId"
CUSTOM_PROPS_PART = "docProps/custom.xml"
CONTENT_TYPES_PART = "[Content_Types].xml"
ROOT_RELS_PART = "_rels/.rels"
CUSTOM_PROPS_NS = "http://schemas.openxmlformats.org/officeDocument/2006/custom-properties"
VT_NS = "http://schemas.openxmlformats.org/officeDocument/2006/docPropsVTypes"
CUSTOM_PROPS_FMTID = "{D5CDD505-2E9C-101B-9397-08002B2CF9AE}"
CUSTOM_PROPS_CONTENT_TYPE = "application/vnd.openxmlformats-officedocument.custom-properties+xml"
CUSTOM_PROPS_REL_TYPE = "http://schemas.openxmlformats.org/officeDocument/2006/relationships/custom-properties"


def write_document_id(path: Path, document_id: str) -> None:
    """Store the document number in the Office file's custom properties.

    The number is random and contains no sensitive data. It lets the restore
    step find the database record even if the file was renamed.
    """
    _format_for(path)
    temporary_path = path.with_name(f".{path.stem}.id{path.suffix}")

    try:
        with ZipFile(path) as source_archive:
            names = set(source_archive.namelist())
            custom_xml = _custom_props_xml(
                source_archive.read(CUSTOM_PROPS_PART) if CUSTOM_PROPS_PART in names else None,
                document_id,
            )
            with ZipFile(temporary_path, "w") as output_archive:
                for info in source_archive.infolist():
                    if info.filename == CUSTOM_PROPS_PART:
                        continue
                    data = source_archive.read(info.filename)
                    if info.filename == CONTENT_TYPES_PART:
                        data = _ensure_custom_props_content_type(data)
                    elif info.filename == ROOT_RELS_PART:
                        data = _ensure_custom_props_relationship(data)
                    output_archive.writestr(info, data)
                output_archive.writestr(CUSTOM_PROPS_PART, custom_xml)
        temporary_path.replace(path)
    except (BadZipFile, OSError, ElementTree.ParseError) as exc:
        _remove_if_exists(temporary_path)
        raise OfficeAnonymizationError("document id could not be written") from exc


def read_document_id(path: Path) -> Optional[str]:
    """Return the document number stored by write_document_id, if present."""
    if not is_supported_office_file(path):
        return None
    try:
        with ZipFile(path) as archive:
            if CUSTOM_PROPS_PART not in archive.namelist():
                return None
            root = ElementTree.fromstring(archive.read(CUSTOM_PROPS_PART))
    except (BadZipFile, OSError, ElementTree.ParseError):
        return None

    for prop in root.findall(f"{{{CUSTOM_PROPS_NS}}}property"):
        if prop.get("name") == DOCUMENT_ID_PROPERTY:
            value = prop.find(f"{{{VT_NS}}}lpwstr")
            if value is not None and value.text:
                return value.text.strip()
    return None


def _custom_props_xml(existing: Optional[bytes], document_id: str) -> bytes:
    ElementTree.register_namespace("", CUSTOM_PROPS_NS)
    ElementTree.register_namespace("vt", VT_NS)
    if existing:
        root = ElementTree.fromstring(existing)
    else:
        root = ElementTree.Element(f"{{{CUSTOM_PROPS_NS}}}Properties")

    for prop in list(root.findall(f"{{{CUSTOM_PROPS_NS}}}property")):
        if prop.get("name") == DOCUMENT_ID_PROPERTY:
            root.remove(prop)

    used_pids = [
        int(prop.get("pid", "1"))
        for prop in root.findall(f"{{{CUSTOM_PROPS_NS}}}property")
        if (prop.get("pid") or "").isdigit()
    ]
    prop = ElementTree.SubElement(
        root,
        f"{{{CUSTOM_PROPS_NS}}}property",
        {
            "fmtid": CUSTOM_PROPS_FMTID,
            "pid": str(max(used_pids, default=1) + 1),
            "name": DOCUMENT_ID_PROPERTY,
        },
    )
    ElementTree.SubElement(prop, f"{{{VT_NS}}}lpwstr").text = document_id
    return ElementTree.tostring(root, encoding="utf-8", xml_declaration=True)


def _ensure_custom_props_content_type(data: bytes) -> bytes:
    text = data.decode("utf-8")
    if f'PartName="/{CUSTOM_PROPS_PART}"' in text:
        return data
    override = f'<Override PartName="/{CUSTOM_PROPS_PART}" ContentType="{CUSTOM_PROPS_CONTENT_TYPE}"/>'
    return text.replace("</Types>", override + "</Types>", 1).encode("utf-8")


def _ensure_custom_props_relationship(data: bytes) -> bytes:
    text = data.decode("utf-8")
    if CUSTOM_PROPS_REL_TYPE in text:
        return data
    rel_id = "rIdSecureGatewayCustom"
    relationship = f'<Relationship Id="{rel_id}" Type="{CUSTOM_PROPS_REL_TYPE}" Target="{CUSTOM_PROPS_PART}"/>'
    return text.replace("</Relationships>", relationship + "</Relationships>", 1).encode("utf-8")