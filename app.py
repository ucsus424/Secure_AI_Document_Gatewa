from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Callable, List, Optional, TextIO

from core.anonymizer import anonymize_text
from core.database import (
    DEFAULT_DB_PATH,
    DatabaseError,
    DocumentNotFoundError,
    init_db,
    load_mapping,
    new_document_id,
    save_document,
)
from core.detector import (
    DEFAULT_MODEL,
    DEFAULT_OLLAMA_URL,
    DEFAULT_TIMEOUT_SECONDS,
    DetectorError,
    OllamaDetector,
)
from core.detection import detect_entities
from core.document_lookup import AmbiguousDocumentError, resolve_document_id
from core.heuristics import require_no_obvious_entities
from core.office import (
    OfficeAnonymizationError,
    anonymize_office_file,
    extract_office_text,
    is_supported_office_file,
    restore_docx_file,
    write_document_id,
)
from core.restore import RestoreError, restore_text


DetectorFactory = Callable[[argparse.Namespace], object]

SAFE_SUFFIX_RE = re.compile(r"_SAFE(?:_[0-9A-Fa-f]{8})?")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Secure AI Document Gateway local anonymization MVP",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    scan = subparsers.add_parser("scan", help="detect sensitive entities in a supported file")
    _add_detector_options(scan)
    _add_chunk_options(scan)
    scan.add_argument("input", type=Path)

    anonymize = subparsers.add_parser("anonymize", help="create a sanitized copy and save values to the database")
    _add_detector_options(anonymize)
    _add_chunk_options(anonymize)
    _add_db_option(anonymize)
    anonymize.add_argument("--output-dir", type=Path, default=None)
    anonymize.add_argument("input", type=Path)

    restore = subparsers.add_parser("restore", help="restore placeholders in a TXT or DOCX file")
    _add_db_option(restore)
    restore.add_argument("input", type=Path)
    restore.add_argument(
        "document_id",
        nargs="?",
        default=None,
        help="optional: document number; found automatically when omitted",
    )

    ui = subparsers.add_parser("ui", help="start the local visual interface")
    _add_detector_options(ui)
    _add_chunk_options(ui)
    _add_db_option(ui)
    ui.add_argument("--host", choices=("127.0.0.1", "localhost"), default="127.0.0.1")
    ui.add_argument("--port", type=int, default=8765)

    return parser


def run(
    argv: Optional[List[str]] = None,
    detector_factory: Optional[DetectorFactory] = None,
    stdout: TextIO = sys.stdout,
    stderr: TextIO = sys.stderr,
) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    detector_factory = detector_factory or _default_detector_factory

    if args.command == "scan":
        return _scan(args, detector_factory, stdout, stderr)
    if args.command == "anonymize":
        return _anonymize(args, detector_factory, stdout, stderr)
    if args.command == "restore":
        return _restore(args, stdout, stderr)
    if args.command == "ui":
        return _ui(args, detector_factory, stdout, stderr)

    stderr.write("Unsupported command.\n")
    return 2


def main() -> int:
    return run()


def _scan(
    args: argparse.Namespace,
    detector_factory: DetectorFactory,
    stdout: TextIO,
    stderr: TextIO,
) -> int:
    try:
        text = _read_input_text(args.input)
        detector = detector_factory(args)
        entities = detect_entities(detector, text, chunk_lines=args.chunk_lines)
    except DetectorError:
        stderr.write("Detection failed; no output written.\n")
        return 1
    except (OSError, OfficeAnonymizationError, UnicodeDecodeError):
        stderr.write("Input file could not be read.\n")
        return 1

    stdout.write(json.dumps([entity.to_json() for entity in entities], ensure_ascii=False, indent=2))
    stdout.write("\n")
    return 0


def _anonymize(
    args: argparse.Namespace,
    detector_factory: DetectorFactory,
    stdout: TextIO,
    stderr: TextIO,
) -> int:
    document_id = new_document_id()
    safe_path = _safe_output_path(args.input, document_id, args.output_dir)

    try:
        init_db(args.db)
        text = _read_input_text(args.input)
        detector = detector_factory(args)
        entities = detect_entities(detector, text, chunk_lines=args.chunk_lines)
        result = anonymize_text(text, entities, source_name=args.input.name)
        if text.strip() and not result.mapping.entities:
            raise DetectorError("detector returned no replaceable entities")
        require_no_obvious_entities(result.text)
        if is_supported_office_file(args.input):
            anonymize_office_file(args.input, safe_path, result.mapping)
            write_document_id(safe_path, document_id)
        else:
            safe_path.write_text(result.text, encoding="utf-8")
        save_document(document_id, result.mapping, args.db)
    except DetectorError:
        stderr.write("Detection failed; no output written.\n")
        return 1
    except (OSError, DatabaseError, OfficeAnonymizationError, UnicodeDecodeError):
        _remove_if_exists(safe_path)
        stderr.write("Anonymization failed; no sensitive content was printed.\n")
        return 1

    stdout.write(f"SAFE document: {safe_path}\n")
    stdout.write(f"Document ID: {document_id}\n")
    return 0


def _restore(args: argparse.Namespace, stdout: TextIO, stderr: TextIO) -> int:
    final_path = _final_output_path(args.input)

    try:
        init_db(args.db)
        document_id = resolve_document_id(
            args.input,
            _read_input_text(args.input),
            args.db,
            explicit_id=args.document_id,
        )
        mapping = load_mapping(document_id, args.db)
        if args.input.suffix.lower() == ".docx":
            result = restore_docx_file(args.input, final_path, mapping)
        elif is_supported_office_file(args.input):
            raise OfficeAnonymizationError("Office restore currently supports DOCX only")
        else:
            text = _read_text_file(args.input)
            result = restore_text(text, mapping)
            final_path.write_text(result.text, encoding="utf-8")
    except RestoreError as exc:
        stderr.write(f"{exc}\n")
        return 1
    except AmbiguousDocumentError as exc:
        stderr.write("Several documents match this file. Run restore again with one of the numbers:\n")
        for candidate in exc.candidates:
            stderr.write(f"  {candidate.id}  {candidate.source_name}  {candidate.created_at}\n")
        return 1
    except DocumentNotFoundError:
        stderr.write("Document was not found in the database.\n")
        return 1
    except (OSError, DatabaseError, OfficeAnonymizationError, UnicodeDecodeError):
        stderr.write("Restore failed; no sensitive content was printed.\n")
        return 1

    stdout.write(f"Document ID: {document_id}\n")
    stdout.write(f"FINAL document: {final_path}\n")
    stdout.write(
        "Placeholders: "
        f"expected={result.expected_count}, found={result.found_count}, missing=0\n"
    )
    return 0


def _ui(
    args: argparse.Namespace,
    detector_factory: DetectorFactory,
    stdout: TextIO,
    stderr: TextIO,
) -> int:
    if not 1 <= args.port <= 65535:
        stderr.write("UI port must be between 1 and 65535.\n")
        return 2

    from web_ui import GatewayWebApp, serve

    try:
        application = GatewayWebApp(
            detector_factory=lambda: detector_factory(args),
            chunk_lines=args.chunk_lines,
            db_path=args.db,
        )
    except DatabaseError:
        stderr.write("Database could not be opened.\n")
        return 1

    stdout.write(f"Local UI: http://{args.host}:{args.port}\n")
    stdout.flush()
    try:
        serve(args.host, args.port, application)
    except OSError:
        stderr.write("Local UI could not start.\n")
        return 1
    except KeyboardInterrupt:
        stdout.write("Local UI stopped.\n")
    return 0


def _default_detector_factory(args: argparse.Namespace) -> OllamaDetector:
    return OllamaDetector(
        model=args.model,
        base_url=args.ollama_url,
        prompt_path=args.prompt,
        timeout_seconds=args.timeout_seconds,
    )


def _read_text_file(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _read_input_text(path: Path) -> str:
    if is_supported_office_file(path):
        return extract_office_text(path)
    return _read_text_file(path)


def _add_detector_options(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--model", default=DEFAULT_MODEL)
    parser.add_argument("--ollama-url", default=DEFAULT_OLLAMA_URL)
    parser.add_argument("--prompt", type=Path, default=None)
    parser.add_argument("--timeout-seconds", type=int, default=DEFAULT_TIMEOUT_SECONDS)


def _add_chunk_options(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--chunk-lines",
        type=int,
        default=None,
        help="split input text into chunks with this many non-empty lines before detection",
    )


def _add_db_option(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--db",
        type=Path,
        default=DEFAULT_DB_PATH,
        help="path to the local SQLite database (default: data/gateway.db)",
    )


def _safe_output_path(
    input_path: Path,
    document_id: str,
    output_dir: Optional[Path] = None,
) -> Path:
    name = f"{input_path.stem}_SAFE_{document_id}{input_path.suffix or '.txt'}"
    if output_dir is not None:
        output_dir.mkdir(parents=True, exist_ok=True)
        return output_dir / name
    return input_path.with_name(name)


def _final_output_path(input_path: Path) -> Path:
    stem = SAFE_SUFFIX_RE.sub("", input_path.stem)
    return input_path.with_name(f"{stem}_FINAL{input_path.suffix or '.txt'}")


def _remove_if_exists(path: Path) -> None:
    try:
        path.unlink()
    except FileNotFoundError:
        pass


if __name__ == "__main__":
    raise SystemExit(main())