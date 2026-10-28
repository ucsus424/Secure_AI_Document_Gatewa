import io
import json
import re
import sqlite3
import tempfile
import unittest
from pathlib import Path

import app
from core.database import init_db, load_mapping, save_document
from core.detector import DetectorError
from core.models import Entity, Mapping


class FakeDetector:
    def detect_text(self, text):
        return [
            Entity(type="PERSON", value="Петров А.В.", confidence=0.99),
            Entity(type="IP", value="10.74.16.1/24", confidence=0.99),
        ]


class ChunkAwareDetector:
    def __init__(self):
        self.calls = []

    def detect_text(self, text):
        self.calls.append(text)
        entities = []
        if "Петров А.В." in text:
            entities.append(Entity(type="PERSON", value="Петров А.В.", confidence=0.99))
        if "10.74.16.1/24" in text:
            entities.append(Entity(type="IP", value="10.74.16.1/24", confidence=0.99))
        return entities


class FailingDetector:
    def detect_text(self, text):
        raise DetectorError("invalid model response")


class EmptyDetector:
    def detect_text(self, text):
        return []


def document_id_from(stdout_text):
    match = re.search(r"Document ID: ([0-9A-F]{8})", stdout_text)
    return match.group(1) if match else None


def count_documents(db_path):
    if not db_path.exists():
        return 0
    with sqlite3.connect(db_path) as connection:
        return connection.execute("SELECT COUNT(*) FROM documents").fetchone()[0]


class CliTestCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.db_path = self.tmp / "test.db"

    def tearDown(self):
        self._tmp.cleanup()

    def run_app(self, argv, detector=None):
        stdout = io.StringIO()
        stderr = io.StringIO()
        command, rest = argv[0], argv[1:]
        full_argv = [command]
        if command in ("anonymize", "restore", "ui"):
            full_argv += ["--db", str(self.db_path)]
        full_argv += rest
        code = app.run(
            full_argv,
            detector_factory=(lambda args: detector) if detector is not None else None,
            stdout=stdout,
            stderr=stderr,
        )
        return code, stdout.getvalue(), stderr.getvalue()


class CliTests(CliTestCase):
    def test_ui_defaults_to_localhost(self):
        args = app.build_parser().parse_args(["ui"])

        self.assertEqual(args.host, "127.0.0.1")
        self.assertEqual(args.port, 8765)

    def test_detector_options_accept_timeout_seconds(self):
        args = app.build_parser().parse_args(
            ["scan", "--timeout-seconds", "360", "input.txt"]
        )

        self.assertEqual(args.timeout_seconds, 360)

    def test_scan_outputs_json_without_real_ollama(self):
        input_path = self.tmp / "input.txt"
        input_path.write_text("Петров А.В. 10.74.16.1/24", encoding="utf-8")

        code, stdout, stderr = self.run_app(["scan", str(input_path)], FakeDetector())

        self.assertEqual(code, 0)
        payload = json.loads(stdout)
        self.assertEqual(payload[0]["value"], "Петров А.В.")
        self.assertEqual(stderr, "")

    def test_anonymize_writes_safe_text_and_saves_values_to_database(self):
        input_path = self.tmp / "input.txt"
        input_path.write_text(
            "Петров А.В. настроил 10.74.16.1/24. Петров А.В. подтвердил.",
            encoding="utf-8",
        )

        code, stdout, stderr = self.run_app(["anonymize", str(input_path)], FakeDetector())

        self.assertEqual(code, 0)
        self.assertEqual(stderr, "")
        document_id = document_id_from(stdout)
        self.assertIsNotNone(document_id)

        safe_path = self.tmp / f"input_SAFE_{document_id}.txt"
        self.assertTrue(safe_path.exists())
        self.assertIn(str(safe_path), stdout)
        safe_text = safe_path.read_text(encoding="utf-8")
        self.assertNotIn("Петров А.В.", safe_text)
        self.assertIn("⟦PERSON_001⟧", safe_text)

        self.assertEqual(list(self.tmp.glob("*.mapping.json")), [])
        mapping = load_mapping(document_id, self.db_path)
        self.assertEqual(mapping.source_name, "input.txt")
        self.assertEqual(mapping.entities["PERSON_001"], "Петров А.В.")

    def test_anonymize_writes_outputs_to_output_dir(self):
        input_path = self.tmp / "input.txt"
        output_dir = self.tmp / "out"
        input_path.write_text("Петров А.В. настроил 10.74.16.1/24.", encoding="utf-8")

        code, stdout, stderr = self.run_app(
            ["anonymize", "--output-dir", str(output_dir), str(input_path)],
            FakeDetector(),
        )

        self.assertEqual(code, 0)
        document_id = document_id_from(stdout)
        safe_path = output_dir / f"input_SAFE_{document_id}.txt"
        self.assertTrue(safe_path.exists())
        self.assertEqual(list(self.tmp.glob("input_SAFE_*")), [])
        self.assertIn(str(safe_path), stdout)
        self.assertEqual(stderr, "")

    def test_anonymize_can_detect_in_line_chunks(self):
        input_path = self.tmp / "input.txt"
        input_path.write_text("Петров А.В.\nобычная строка\n10.74.16.1/24\n", encoding="utf-8")
        detector = ChunkAwareDetector()

        code, stdout, stderr = self.run_app(
            ["anonymize", "--chunk-lines", "1", str(input_path)],
            detector,
        )

        self.assertEqual(code, 0)
        self.assertEqual(len(detector.calls), 3)
        safe_path = self.tmp / f"input_SAFE_{document_id_from(stdout)}.txt"
        self.assertEqual(
            safe_path.read_text(encoding="utf-8"),
            "⟦PERSON_001⟧\nобычная строка\n⟦IP_001⟧\n",
        )
        self.assertEqual(stderr, "")

    def test_anonymize_automatically_chunks_large_input(self):
        input_path = self.tmp / "large.txt"
        input_path.write_text(
            ("обычная строка без чувствительных данных\n" * 400)
            + "Петров А.В. подтвердил результат.\n",
            encoding="utf-8",
        )
        detector = ChunkAwareDetector()

        code, stdout, stderr = self.run_app(["anonymize", str(input_path)], detector)

        self.assertEqual(code, 0)
        self.assertGreater(len(detector.calls), 1)
        self.assertTrue(all(len(chunk) <= 8000 for chunk in detector.calls))
        safe_path = self.tmp / f"large_SAFE_{document_id_from(stdout)}.txt"
        self.assertIn("⟦PERSON_001⟧", safe_path.read_text(encoding="utf-8"))
        self.assertEqual(stderr, "")

    def test_anonymize_empty_detection_writes_nothing(self):
        input_path = self.tmp / "input.txt"
        input_path.write_text("Контакт: user@example.com", encoding="utf-8")

        code, stdout, stderr = self.run_app(["anonymize", str(input_path)], EmptyDetector())

        self.assertNotEqual(code, 0)
        self.assertEqual(list(self.tmp.glob("input_SAFE_*")), [])
        self.assertEqual(count_documents(self.db_path), 0)
        self.assertEqual(stdout, "")
        self.assertNotIn("user@example.com", stderr)

    def test_anonymize_masks_structured_address_fields_missed_by_model(self):
        input_path = self.tmp / "input.txt"
        input_path.write_text(
            "\n".join(
                [
                    "Ответственный: Петров А.В.",
                    "Юридический адрес: 123456, Московская область,",
                    "г. Примерск, ул. Тестовая, д. 1,",
                    "помещение 2, офис 3",
                    "e-mail: user@example.com",
                ]
            ),
            encoding="utf-8",
        )

        code, stdout, stderr = self.run_app(["anonymize", str(input_path)], FakeDetector())

        self.assertEqual(code, 0)
        safe_text = (self.tmp / f"input_SAFE_{document_id_from(stdout)}.txt").read_text(encoding="utf-8")
        self.assertNotIn("123456", safe_text)
        self.assertNotIn("Примерск", safe_text)
        self.assertNotIn("Тестовая", safe_text)
        self.assertNotIn("user@example.com", safe_text)
        self.assertIn("⟦ADDRESS_", safe_text)
        self.assertIn("⟦EMAIL_", safe_text)
        self.assertEqual(stderr, "")

    def test_anonymize_detector_failure_writes_nothing(self):
        input_path = self.tmp / "input.txt"
        input_path.write_text("Секрет Qwerty-2026!", encoding="utf-8")

        code, stdout, stderr = self.run_app(["anonymize", str(input_path)], FailingDetector())

        self.assertNotEqual(code, 0)
        self.assertEqual(list(self.tmp.glob("input_SAFE_*")), [])
        self.assertEqual(count_documents(self.db_path), 0)
        self.assertNotIn("Qwerty-2026!", stderr)
        self.assertEqual(stdout, "")

    def test_restore_writes_final_text_using_database(self):
        init_db(self.db_path)
        save_document(
            "AB12CD34",
            Mapping.create(source_name="input.txt", entities={"PERSON_001": "Петров А.В."}),
            self.db_path,
        )
        safe_path = self.tmp / "result_SAFE_AB12CD34.txt"
        safe_path.write_text("⟦PERSON_001⟧ настроил сеть.", encoding="utf-8")

        code, stdout, stderr = self.run_app(["restore", str(safe_path)])

        self.assertEqual(code, 0)
        self.assertIn("Document ID: AB12CD34", stdout)
        final_path = self.tmp / "result_FINAL.txt"
        self.assertEqual(final_path.read_text(encoding="utf-8"), "Петров А.В. настроил сеть.")
        self.assertIn(str(final_path), stdout)
        self.assertEqual(stderr, "")

    def test_restore_missing_placeholder_fails_without_writing_final(self):
        init_db(self.db_path)
        save_document(
            "AB12CD34",
            Mapping.create(
                source_name="input.txt",
                entities={"PERSON_001": "Петров А.В.", "OBJECT_001": "ВРН-Север-17"},
            ),
            self.db_path,
        )
        safe_path = self.tmp / "result_SAFE.txt"
        safe_path.write_text("⟦PERSON_001⟧ настроил сеть.", encoding="utf-8")

        code, stdout, stderr = self.run_app(["restore", str(safe_path)])

        self.assertNotEqual(code, 0)
        self.assertFalse((self.tmp / "result_FINAL.txt").exists())
        self.assertIn("OBJECT_001", stderr)
        self.assertNotIn("Петров А.В.", stderr)

    def test_restore_unknown_document_fails(self):
        safe_path = self.tmp / "result_SAFE.txt"
        safe_path.write_text("⟦PERSON_001⟧ настроил сеть.", encoding="utf-8")

        code, stdout, stderr = self.run_app(["restore", str(safe_path)])

        self.assertNotEqual(code, 0)
        self.assertFalse((self.tmp / "result_FINAL.txt").exists())
        self.assertIn("not found", stderr)

    def test_restore_lists_candidates_when_several_documents_match(self):
        init_db(self.db_path)
        for document_id, name in (("AB12CD34", "Петров А.В."), ("EF56AB78", "Иванов С.П.")):
            save_document(
                document_id,
                Mapping.create(source_name=f"{document_id}.txt", entities={"PERSON_001": name}),
                self.db_path,
            )
        answer_path = self.tmp / "answer.txt"
        answer_path.write_text("⟦PERSON_001⟧ настроил сеть.", encoding="utf-8")

        code, stdout, stderr = self.run_app(["restore", str(answer_path)])

        self.assertNotEqual(code, 0)
        self.assertIn("AB12CD34", stderr)
        self.assertIn("EF56AB78", stderr)
        self.assertNotIn("Петров", stderr)

        code, stdout, stderr = self.run_app(["restore", str(answer_path), "EF56AB78"])

        self.assertEqual(code, 0, stderr)
        self.assertEqual((self.tmp / "answer_FINAL.txt").read_text(encoding="utf-8"), "Иванов С.П. настроил сеть.")

    def test_anonymize_then_restore_round_trip(self):
        input_path = self.tmp / "input.txt"
        original = "Петров А.В. настроил 10.74.16.1/24."
        input_path.write_text(original, encoding="utf-8")

        code, stdout, _ = self.run_app(["anonymize", str(input_path)], FakeDetector())
        self.assertEqual(code, 0)
        document_id = document_id_from(stdout)
        safe_path = self.tmp / f"input_SAFE_{document_id}.txt"

        answer_path = self.tmp / "claude_answer.txt"
        answer_path.write_text(safe_path.read_text(encoding="utf-8"), encoding="utf-8")

        code, _, stderr = self.run_app(["restore", str(answer_path)])

        self.assertEqual(code, 0, stderr)
        self.assertEqual((self.tmp / "claude_answer_FINAL.txt").read_text(encoding="utf-8"), original)


if __name__ == "__main__":
    unittest.main()