import io
import json
import re
from contextlib import closing
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
    with closing(sqlite3.connect(db_path)) as connection:
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

    def test_ui_accepts_network_host(self):
        args = app.build_parser().parse_args(["ui", "--host", "0.0.0.0", "--port", "9000"])

        self.assertEqual(args.host, "0.0.0.0")
        self.assertEqual(args.port, 9000)

    def test_loopback_hosts_are_local(self):
        for host in ("127.0.0.1", "localhost", "::1", "[::1]", "127.0.0.5"):
            self.assertTrue(app.is_loopback_host(host), host)
        for host in ("0.0.0.0", "::", "192.168.1.25", "server01"):
            self.assertFalse(app.is_loopback_host(host), host)

    def test_startup_lines_for_local_mode(self):
        lines = app.startup_lines("127.0.0.1", 8765)

        self.assertEqual(lines[0], "Local UI: http://127.0.0.1:8765")
        self.assertNotIn("Network", "\n".join(lines))

    def test_startup_lines_for_all_interfaces_list_server_addresses(self):
        original = app.network_addresses
        app.network_addresses = lambda: ["192.168.1.25", "10.0.0.7"]
        try:
            text = "\n".join(app.startup_lines("0.0.0.0", 8765))
        finally:
            app.network_addresses = original

        self.assertIn("Network UI", text)
        self.assertIn("http://192.168.1.25:8765", text)
        self.assertIn("http://10.0.0.7:8765", text)
        self.assertNotIn("http://0.0.0.0", text)
        self.assertIn("firewall", text)

    def test_startup_lines_for_specific_server_address(self):
        text = "\n".join(app.startup_lines("192.168.1.25", 8765))

        self.assertIn("http://192.168.1.25:8765", text)

    def test_network_addresses_are_ipv4_and_not_loopback(self):
        for address in app.network_addresses():
            self.assertRegex(address, r"^\d+\.\d+\.\d+\.\d+$")
            self.assertFalse(address.startswith("127."))

    def test_ui_passes_network_mode_and_prints_addresses(self):
        import web_ui

        captured = {}
        original_serve = web_ui.serve

        def fake_serve(host, port, application):
            captured["host"] = host
            captured["network_mode"] = application.network_mode

        web_ui.serve = fake_serve
        try:
            code, stdout, stderr = self.run_app(["ui", "--host", "0.0.0.0", "--port", "8765"])
        finally:
            web_ui.serve = original_serve

        self.assertEqual(code, 0, stderr)
        self.assertEqual(captured, {"host": "0.0.0.0", "network_mode": True})
        self.assertIn("Network UI", stdout)

    def test_ui_reports_port_that_cannot_be_opened(self):
        import web_ui

        def failing_serve(host, port, application):
            raise OSError("address in use")

        original_serve = web_ui.serve
        web_ui.serve = failing_serve
        try:
            code, stdout, stderr = self.run_app(["ui", "--port", "8765"])
        finally:
            web_ui.serve = original_serve

        self.assertEqual(code, 1)
        self.assertIn("could not start", stderr)

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

    def test_restore_removed_placeholder_warns_and_writes_final(self):
        init_db(self.db_path)
        save_document(
            "AB12CD34",
            Mapping.create(
                source_name="input.txt",
                entities={"PERSON_001": "Петров А.В.", "OBJECT_001": "ВРН-Север-17"},
            ),
            self.db_path,
        )
        safe_path = self.tmp / "result_SAFE_AB12CD34.txt"
        safe_path.write_text("⟦PERSON_001⟧ настроил сеть.", encoding="utf-8")

        code, stdout, stderr = self.run_app(["restore", str(safe_path)])

        self.assertEqual(code, 0, stderr)
        final_path = self.tmp / "result_FINAL.txt"
        self.assertEqual(final_path.read_text(encoding="utf-8"), "Петров А.В. настроил сеть.")
        self.assertIn("missing=1", stdout)
        self.assertIn("Warning", stdout)
        self.assertIn("OBJECT_001", stdout)
        self.assertNotIn("ВРН-Север-17", stdout)

    def test_restore_placeholder_of_another_document_fails_without_final(self):
        init_db(self.db_path)
        save_document(
            "AB12CD34",
            Mapping.create(source_name="input.txt", entities={"PERSON_001": "Петров А.В."}),
            self.db_path,
        )
        safe_path = self.tmp / "result_SAFE_AB12CD34.txt"
        safe_path.write_text("⟦PERSON_001⟧ настроил ⟦IP_001⟧.", encoding="utf-8")

        code, stdout, stderr = self.run_app(["restore", str(safe_path)])

        self.assertEqual(code, 1)
        self.assertFalse((self.tmp / "result_FINAL.txt").exists())
        self.assertIn("IP_001", stderr)
        self.assertIn("AB12CD34", stderr)
        self.assertNotIn("Петров", stderr)

    def test_restore_unknown_explicit_number_does_not_fall_back(self):
        init_db(self.db_path)
        save_document(
            "AB12CD34",
            Mapping.create(source_name="input.txt", entities={"PERSON_001": "Петров А.В."}),
            self.db_path,
        )
        safe_path = self.tmp / "result_SAFE_AB12CD34.txt"
        safe_path.write_text("⟦PERSON_001⟧ настроил сеть.", encoding="utf-8")

        code, stdout, stderr = self.run_app(["restore", str(safe_path), "FFFFFFFF"])

        self.assertEqual(code, 1)
        self.assertFalse((self.tmp / "result_FINAL.txt").exists())
        self.assertIn("FFFFFFFF", stderr)
        self.assertIn("No other document is chosen automatically", stderr)

    def test_restore_invalid_explicit_number_fails(self):
        answer_path = self.tmp / "answer.txt"
        answer_path.write_text("⟦PERSON_001⟧", encoding="utf-8")

        code, stdout, stderr = self.run_app(["restore", str(answer_path), "12345"])

        self.assertEqual(code, 1)
        self.assertIn("Invalid document number", stderr)

    def test_restore_single_candidate_still_asks_for_number(self):
        init_db(self.db_path)
        save_document(
            "AB12CD34",
            Mapping.create(source_name="input.txt", entities={"PERSON_001": "Петров А.В."}),
            self.db_path,
        )
        answer_path = self.tmp / "answer.txt"
        answer_path.write_text("⟦PERSON_001⟧ настроил сеть.", encoding="utf-8")

        code, stdout, stderr = self.run_app(["restore", str(answer_path)])

        self.assertEqual(code, 1)
        self.assertIn("AB12CD34", stderr)
        self.assertFalse((self.tmp / "answer_FINAL.txt").exists())

    def test_anonymize_stops_when_value_would_stay_in_safe_docx(self):
        try:
            from docx import Document
        except ImportError:
            self.skipTest("python-docx is unavailable")

        class SplitAcrossCellsDetector:
            def detect_text(self, text):
                return [Entity(type="PERSON", value="Петров Алексей")]

        path = self.tmp / "table.docx"
        document = Document()
        table = document.add_table(rows=1, cols=2)
        table.cell(0, 0).text = "Петров"
        table.cell(0, 1).text = "Алексей"
        document.save(path)

        code, stdout, stderr = self.run_app(
            ["anonymize", "--output-dir", str(self.tmp / "out"), str(path)], SplitAcrossCellsDetector()
        )

        self.assertEqual(code, 1)
        self.assertIn("PERSON_001", stderr)
        self.assertNotIn("Петров", stderr)
        self.assertEqual(list((self.tmp / "out").glob("*")), [])
        self.assertEqual(count_documents(self.db_path), 0)

    def test_unsupported_formats_are_rejected(self):
        for name in ("plan.xlsx", "deck.pptx"):
            path = self.tmp / name
            path.write_bytes(b"PK")
            for command in (["anonymize", str(path)], ["restore", str(path)], ["scan", str(path)]):
                code, stdout, stderr = self.run_app(command, FakeDetector())
                self.assertEqual(code, 1, command)
                self.assertIn("Only TXT and DOCX", stderr)

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

        # Ответ модели сохранён под другим именем: номера в нём нет,
        # поэтому программа просит указать документ и ничего не угадывает.
        answer_path = self.tmp / "claude_answer.txt"
        answer_path.write_text(safe_path.read_text(encoding="utf-8"), encoding="utf-8")

        code, _, stderr = self.run_app(["restore", str(answer_path)])
        self.assertEqual(code, 1)
        self.assertIn(document_id, stderr)

        code, _, stderr = self.run_app(["restore", str(answer_path), document_id])

        self.assertEqual(code, 0, stderr)
        self.assertEqual((self.tmp / "claude_answer_FINAL.txt").read_text(encoding="utf-8"), original)


if __name__ == "__main__":
    unittest.main()