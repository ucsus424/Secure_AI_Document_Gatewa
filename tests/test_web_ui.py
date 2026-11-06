import io
import json
from contextlib import closing
import sqlite3
import tempfile
import unittest
from pathlib import Path

from core.database import init_db, load_mapping, save_document
from core.models import Entity, Mapping
from web_ui import GatewayWebApp


try:
    from docx import Document
except ImportError:  # pragma: no cover - exercised only without the optional test dependency
    Document = None


DOCX_TYPE = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"


class FakeDetector:
    def detect_text(self, text):
        return [Entity(type="PERSON", value="Петров А.В.", confidence=0.99)]


class EmptyDetector:
    def detect_text(self, text):
        return []


class RecordingDetector(FakeDetector):
    def __init__(self):
        self.calls = []

    def detect_text(self, text):
        self.calls.append(text)
        return super().detect_text(text)


class WebUiTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.db_path = Path(self._tmp.name) / "test.db"
        self.app = self.make_app(FakeDetector())

    def tearDown(self):
        self._tmp.cleanup()

    def make_app(self, detector):
        return GatewayWebApp(detector_factory=lambda: detector, db_path=self.db_path)

    def save_values(self, document_id, entities):
        init_db(self.db_path)
        save_document(
            document_id,
            Mapping.create(source_name="input.docx", entities=entities),
            self.db_path,
        )

    def count_documents(self):
        with closing(sqlite3.connect(self.db_path)) as connection:
            return connection.execute("SELECT COUNT(*) FROM documents").fetchone()[0]

    def test_home_page_exposes_local_anonymize_and_restore_workflows(self):
        status, headers, body = _request(self.app, "GET", "/")

        html = body.decode("utf-8")
        self.assertEqual(status, "200 OK")
        self.assertIn("text/html", headers["Content-Type"])
        self.assertIn("Создать безопасную копию", html)
        self.assertIn("Восстановить документ", html)
        self.assertIn('type="hidden" name="document_id"', html)
        self.assertNotIn("Номер документа", html)
        self.assertNotIn("mapping.json", html)
        self.assertIn("127.0.0.1", html)

    def test_local_mode_page_and_health(self):
        status, headers, body = _request(self.app, "GET", "/")
        html = body.decode("utf-8")
        self.assertIn("Только 127.0.0.1", html)
        self.assertNotIn("Сетевой режим", html)
        self.assertNotIn("__MODE_", html)

        status, headers, body = _request(self.app, "GET", "/health")
        self.assertEqual(json.loads(body.decode("utf-8")), {"status": "ok", "local": True})

    def test_network_mode_page_and_health(self):
        app = GatewayWebApp(detector_factory=lambda: FakeDetector(), db_path=self.db_path, network_mode=True)

        status, headers, body = _request(app, "GET", "/")
        html = body.decode("utf-8")
        self.assertEqual(status, "200 OK")
        self.assertIn("Сетевой режим", html)
        self.assertIn("хранятся на сервере", html)
        self.assertNotIn("Только 127.0.0.1", html)
        self.assertNotIn("слушает только локальный адрес", html)
        self.assertNotIn("__MODE_", html)

        status, headers, body = _request(app, "GET", "/health")
        self.assertEqual(json.loads(body.decode("utf-8")), {"status": "ok", "local": False})

    def test_network_mode_anonymize_and_restore_still_work(self):
        app = GatewayWebApp(detector_factory=lambda: FakeDetector(), db_path=self.db_path, network_mode=True)
        original = "Инженер Петров А.В. на объекте."

        status, headers, safe_body = _request(
            app, "POST", "/anonymize",
            files={"document": ("input.txt", original.encode("utf-8"), "text/plain")},
        )
        self.assertEqual(status, "200 OK")
        document_id = headers["X-Document-Id"]

        status, headers, body = _request(
            app, "POST", "/restore",
            files={"document": (f"input_SAFE_{document_id}.txt", safe_body, "text/plain")},
        )
        self.assertEqual(status, "200 OK")
        self.assertEqual(body.decode("utf-8"), original)

    def test_anonymize_returns_only_safe_file_and_saves_values_to_database(self):
        status, headers, body = _request(
            self.app,
            "POST",
            "/anonymize",
            files={
                "document": ("input.txt", "Инженер Петров А.В. на объекте.".encode("utf-8"), "text/plain"),
            },
        )

        self.assertEqual(status, "200 OK")
        self.assertIn("text/plain", headers["Content-Type"])
        document_id = headers["X-Document-Id"]
        self.assertRegex(document_id, r"^[0-9A-F]{8}$")
        self.assertIn(f"input_SAFE_{document_id}.txt", headers["Content-Disposition"])
        self.assertEqual(body.decode("utf-8"), "Инженер ⟦PERSON_001⟧ на объекте.")

        mapping = load_mapping(document_id, self.db_path)
        self.assertEqual(mapping.entities["PERSON_001"], "Петров А.В.")

    def test_anonymize_automatically_chunks_large_upload(self):
        detector = RecordingDetector()
        app = self.make_app(detector)
        source = (
            ("обычная строка без чувствительных данных\n" * 400)
            + "Инженер Петров А.В. на объекте."
        ).encode("utf-8")

        status, headers, body = _request(
            app,
            "POST",
            "/anonymize",
            files={"document": ("large.txt", source, "text/plain")},
        )

        self.assertEqual(status, "200 OK")
        self.assertGreater(len(detector.calls), 1)
        self.assertTrue(all(len(chunk) <= 8000 for chunk in detector.calls))
        self.assertIn("⟦PERSON_001⟧", body.decode("utf-8"))

    def test_anonymize_rejects_empty_detection_result_and_saves_nothing(self):
        app = self.make_app(EmptyDetector())

        status, headers, body = _request(
            app,
            "POST",
            "/anonymize",
            files={
                "document": ("input.txt", "Контакт: user@example.com".encode("utf-8"), "text/plain"),
            },
        )

        payload = json.loads(body.decode("utf-8"))
        self.assertEqual(status, "502 Bad Gateway")
        self.assertIn("application/json", headers["Content-Type"])
        self.assertNotIn("user@example.com", payload["error"])
        self.assertEqual(self.count_documents(), 0)

    def test_restore_txt_uses_number_from_file_name(self):
        self.save_values("AB12CD34", {"PERSON_001": "Петров А.В."})
        self.save_values("EF56AB78", {"PERSON_001": "Иванов С.П."})

        status, headers, body = _request(
            self.app,
            "POST",
            "/restore",
            files={"document": ("result_SAFE_AB12CD34.txt", "⟦PERSON_001⟧ на объекте.".encode("utf-8"), "text/plain")},
        )

        self.assertEqual(status, "200 OK")
        self.assertEqual(headers["X-Document-Id"], "AB12CD34")
        self.assertEqual(headers["X-Document-Source"], "filename")
        self.assertEqual(headers["X-Missing-Count"], "0")
        self.assertIn("result_FINAL.txt", headers["Content-Disposition"])
        self.assertEqual(body.decode("utf-8"), "Петров А.В. на объекте.")

    def test_restore_without_number_asks_to_choose_even_one_document(self):
        self.save_values("AB12CD34", {"PERSON_001": "Петров А.В.", "IP_001": "10.74.16.1/24"})
        self.save_values("EF56AB78", {"PERSON_001": "Иванов С.П."})
        upload = {"document": ("answer.txt", "⟦PERSON_001⟧ настроил ⟦IP_001⟧.".encode("utf-8"), "text/plain")}

        status, headers, body = _request(self.app, "POST", "/restore", files=upload)

        payload = json.loads(body.decode("utf-8"))
        self.assertEqual(status, "409 Conflict")
        self.assertEqual(payload["code"], "choose_document")
        self.assertEqual([item["id"] for item in payload["candidates"]], ["AB12CD34"])
        self.assertIn("0 из 2", payload["candidates"][0]["hint"])

        status, headers, body = _request(
            self.app, "POST", "/restore", files=upload, fields={"document_id": "AB12CD34"}
        )

        self.assertEqual(status, "200 OK")
        self.assertEqual(headers["X-Document-Source"], "explicit")
        self.assertEqual(body.decode("utf-8"), "Петров А.В. настроил 10.74.16.1/24.")

    def test_task_scenario_edited_file_is_not_restored_with_wrong_person(self):
        # Иванов и Петров оба имеют ⟦PERSON_001⟧; из файла Петрова удалили ⟦IP_001⟧ и переименовали.
        self.save_values("AB12CD34", {"PERSON_001": "Иванов С.П."})
        self.save_values("EF56AB78", {"PERSON_001": "Петров А.В.", "IP_001": "10.74.16.1/24"})
        upload = {"document": ("renamed.txt", "⟦PERSON_001⟧ на объекте.".encode("utf-8"), "text/plain")}

        status, headers, body = _request(self.app, "POST", "/restore", files=upload)

        payload = json.loads(body.decode("utf-8"))
        self.assertEqual(status, "409 Conflict")
        self.assertEqual({item["id"] for item in payload["candidates"]}, {"AB12CD34", "EF56AB78"})
        self.assertNotIn("Иванов", body.decode("utf-8"))
        self.assertNotIn("Петров", body.decode("utf-8"))

    def test_restore_offers_choice_when_several_documents_match(self):
        self.save_values("AB12CD34", {"PERSON_001": "Петров А.В."})
        self.save_values("EF56AB78", {"PERSON_001": "Иванов С.П."})
        upload = {"document": ("answer.txt", "⟦PERSON_001⟧ на объекте.".encode("utf-8"), "text/plain")}

        status, headers, body = _request(self.app, "POST", "/restore", files=upload)

        payload = json.loads(body.decode("utf-8"))
        self.assertEqual(status, "409 Conflict")
        self.assertEqual(payload["code"], "choose_document")
        self.assertEqual({item["id"] for item in payload["candidates"]}, {"AB12CD34", "EF56AB78"})
        self.assertNotIn("Петров", body.decode("utf-8"))

        status, headers, body = _request(
            self.app, "POST", "/restore", files=upload, fields={"document_id": "EF56AB78"}
        )

        self.assertEqual(status, "200 OK")
        self.assertEqual(body.decode("utf-8"), "Иванов С.П. на объекте.")

    def test_restore_file_without_known_placeholders_returns_not_found(self):
        status, headers, body = _request(
            self.app,
            "POST",
            "/restore",
            files={"document": ("answer.txt", "⟦PERSON_001⟧".encode("utf-8"), "text/plain")},
        )

        self.assertEqual(status, "404 Not Found")
        self.assertIn("не найден", json.loads(body.decode("utf-8"))["error"])

    def test_anonymize_then_restore_round_trip(self):
        original = "Инженер Петров А.В. на объекте."
        _, headers, safe_body = _request(
            self.app,
            "POST",
            "/anonymize",
            files={"document": ("input.txt", original.encode("utf-8"), "text/plain")},
        )

        document_id = headers["X-Document-Id"]
        upload = {"document": ("answer.txt", safe_body, "text/plain")}

        status, _, body = _request(self.app, "POST", "/restore", files=upload)
        self.assertEqual(status, "409 Conflict")

        status, headers, body = _request(
            self.app, "POST", "/restore", files=upload, fields={"document_id": document_id}
        )

        self.assertEqual(status, "200 OK")
        self.assertEqual(body.decode("utf-8"), original)

    @unittest.skipUnless(Document is not None, "python-docx is unavailable")
    def test_restore_returns_final_docx(self):
        self.save_values("AB12CD34", {"PERSON_001": "Петров А.В."})
        safe_bytes = io.BytesIO()
        document = Document()
        document.add_paragraph("Инженер ⟦PERSON_001⟧ на объекте.")
        document.save(safe_bytes)

        status, headers, body = _request(
            self.app,
            "POST",
            "/restore",
            files={"document": ("result_SAFE_AB12CD34.docx", safe_bytes.getvalue(), DOCX_TYPE)},
        )

        self.assertEqual(status, "200 OK")
        self.assertEqual(headers["Content-Type"], DOCX_TYPE)
        self.assertIn("result_FINAL.docx", headers["Content-Disposition"])
        restored = Document(io.BytesIO(body))
        self.assertEqual(restored.paragraphs[0].text, "Инженер Петров А.В. на объекте.")

    @unittest.skipUnless(Document is not None, "python-docx is unavailable")
    def test_restore_removed_placeholder_is_a_warning(self):
        self.save_values(
            "AB12CD34",
            {"PERSON_001": "Петров А.В.", "OBJECT_001": "ВРН-Север-17"},
        )
        safe_bytes = io.BytesIO()
        document = Document()
        document.add_paragraph("Инженер ⟦PERSON_001⟧ на объекте.")
        document.save(safe_bytes)

        status, headers, body = _request(
            self.app,
            "POST",
            "/restore",
            files={"document": ("result_SAFE_AB12CD34.docx", safe_bytes.getvalue(), DOCX_TYPE)},
        )

        self.assertEqual(status, "200 OK")
        self.assertEqual(headers["X-Missing-Count"], "1")
        self.assertEqual(headers["X-Missing-Placeholders"], "OBJECT_001")
        restored = Document(io.BytesIO(body))
        self.assertEqual(restored.paragraphs[0].text, "Инженер Петров А.В. на объекте.")

    def test_restore_placeholder_of_another_document_returns_error(self):
        self.save_values("AB12CD34", {"PERSON_001": "Петров А.В."})

        status, headers, body = _request(
            self.app,
            "POST",
            "/restore",
            files={"document": ("result_SAFE_AB12CD34.txt", "⟦PERSON_001⟧ и ⟦IP_001⟧".encode("utf-8"), "text/plain")},
        )

        payload = json.loads(body.decode("utf-8"))
        self.assertEqual(status, "422 Unprocessable Entity")
        self.assertEqual(payload["code"], "unknown_placeholders")
        self.assertIn("IP_001", payload["error"])
        self.assertNotIn("Петров", payload["error"])

    def test_restore_unknown_chosen_number_does_not_fall_back(self):
        self.save_values("AB12CD34", {"PERSON_001": "Петров А.В."})

        status, headers, body = _request(
            self.app,
            "POST",
            "/restore",
            files={"document": ("result_SAFE_AB12CD34.txt", "⟦PERSON_001⟧".encode("utf-8"), "text/plain")},
            fields={"document_id": "FFFFFFFF"},
        )

        payload = json.loads(body.decode("utf-8"))
        self.assertEqual(status, "404 Not Found")
        self.assertIn("FFFFFFFF", payload["error"])

    def test_restore_unknown_number_in_file_name_does_not_fall_back(self):
        self.save_values("AB12CD34", {"PERSON_001": "Петров А.В."})

        status, headers, body = _request(
            self.app,
            "POST",
            "/restore",
            files={"document": ("result_SAFE_FFFFFFFF.txt", "⟦PERSON_001⟧".encode("utf-8"), "text/plain")},
        )

        self.assertEqual(status, "404 Not Found")
        self.assertIn("FFFFFFFF", json.loads(body.decode("utf-8"))["error"])

    @unittest.skipUnless(Document is not None, "python-docx is unavailable")
    def test_restore_reports_number_mismatch(self):
        from core.office import write_document_id

        self.save_values("AB12CD34", {"PERSON_001": "Петров А.В."})
        self.save_values("EF56AB78", {"PERSON_001": "Иванов С.П."})
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "doc.docx"
            document = Document()
            document.add_paragraph("⟦PERSON_001⟧")
            document.save(path)
            write_document_id(path, "EF56AB78")
            data = path.read_bytes()

        status, headers, body = _request(
            self.app,
            "POST",
            "/restore",
            files={"document": ("result_SAFE_AB12CD34.docx", data, DOCX_TYPE)},
        )

        payload = json.loads(body.decode("utf-8"))
        self.assertEqual(status, "409 Conflict")
        self.assertEqual(payload["code"], "id_conflict")
        self.assertIn("AB12CD34", payload["error"])
        self.assertIn("EF56AB78", payload["error"])
        self.assertEqual({item["id"] for item in payload["candidates"]}, {"AB12CD34", "EF56AB78"})

    @unittest.skipUnless(Document is not None, "python-docx is unavailable")
    def test_anonymize_title_page_split_into_lines(self):
        class UniversityDetector:
            def detect_text(self, text):
                return [Entity(type="COMPANY", value="Российский государственный университет нефти и газа имени И. М. Губкина")]

        source = io.BytesIO()
        document = Document()
        for line in ("«Российский государственный университет нефти и газа", "имени И. М. Губкина»"):
            document.add_paragraph(line)
        document.save(source)

        status, headers, body = _request(
            self.make_app(UniversityDetector()),
            "POST",
            "/anonymize",
            files={"document": ("title.docx", source.getvalue(), DOCX_TYPE)},
        )

        self.assertEqual(status, "200 OK")
        texts = [paragraph.text for paragraph in Document(io.BytesIO(body)).paragraphs]
        self.assertEqual(texts, ["«⟦COMPANY_001⟧»", ""])

    @unittest.skipUnless(Document is not None, "python-docx is unavailable")
    def test_anonymize_stops_when_value_would_stay_in_safe_file(self):
        class SplitAcrossCellsDetector:
            def detect_text(self, text):
                return [Entity(type="PERSON", value="Петров Алексей")]

        source = io.BytesIO()
        document = Document()
        table = document.add_table(rows=1, cols=2)
        table.cell(0, 0).text = "Петров"
        table.cell(0, 1).text = "Алексей"
        document.save(source)

        status, headers, body = _request(
            self.make_app(SplitAcrossCellsDetector()),
            "POST",
            "/anonymize",
            files={"document": ("table.docx", source.getvalue(), DOCX_TYPE)},
        )

        payload = json.loads(body.decode("utf-8"))
        self.assertEqual(status, "422 Unprocessable Entity")
        self.assertEqual(payload["code"], "residual_values")
        self.assertIn("PERSON_001", payload["error"])
        self.assertNotIn("Петров", payload["error"])
        self.assertEqual(self.count_documents(), 0)

    def test_unsupported_formats_are_rejected(self):
        for name in ("plan.xlsx", "deck.pptx"):
            for endpoint in ("/anonymize", "/restore"):
                status, headers, body = _request(
                    self.app,
                    "POST",
                    endpoint,
                    files={"document": (name, b"PK", "application/octet-stream")},
                )
                self.assertEqual(status, "415 Unsupported Media Type", (name, endpoint))
                self.assertIn("TXT и DOCX", json.loads(body.decode("utf-8"))["error"])

    def test_home_page_lists_only_txt_and_docx(self):
        status, headers, body = _request(self.app, "GET", "/")

        html = body.decode("utf-8")
        self.assertNotIn(".xlsx", html)
        self.assertNotIn(".pptx", html)
        self.assertIn("TXT, DOCX", html)


def _request(app, method, path, files=None, fields=None):
    body = b""
    content_type = ""
    if files or fields:
        body, content_type = _multipart_body(files or {}, fields or {})

    environ = {
        "REQUEST_METHOD": method,
        "PATH_INFO": path,
        "CONTENT_LENGTH": str(len(body)),
        "CONTENT_TYPE": content_type,
        "wsgi.input": io.BytesIO(body),
        "wsgi.url_scheme": "http",
        "SERVER_NAME": "127.0.0.1",
        "SERVER_PORT": "8765",
    }
    captured = {}

    def start_response(status, headers):
        captured["status"] = status
        captured["headers"] = dict(headers)

    response_body = b"".join(app(environ, start_response))
    return captured["status"], captured["headers"], response_body


def _multipart_body(files, fields):
    boundary = "----SecureGatewayTestBoundary"
    chunks = []
    for field_name, value in fields.items():
        chunks.extend(
            [
                f"--{boundary}\r\n".encode("ascii"),
                f'Content-Disposition: form-data; name="{field_name}"\r\n\r\n'.encode("utf-8"),
                value.encode("utf-8"),
                b"\r\n",
            ]
        )
    for field_name, (filename, data, content_type) in files.items():
        chunks.extend(
            [
                f"--{boundary}\r\n".encode("ascii"),
                (
                    f'Content-Disposition: form-data; name="{field_name}"; '
                    f'filename="{filename}"\r\n'
                ).encode("utf-8"),
                f"Content-Type: {content_type}\r\n\r\n".encode("ascii"),
                data,
                b"\r\n",
            ]
        )
    chunks.append(f"--{boundary}--\r\n".encode("ascii"))
    return b"".join(chunks), f"multipart/form-data; boundary={boundary}"


if __name__ == "__main__":
    unittest.main()