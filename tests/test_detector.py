import json
import unittest
from unittest import mock

from core.detector import DetectorError, OllamaDetector, parse_entities_json


class DetectorParsingTests(unittest.TestCase):
    def test_parses_valid_json_array(self):
        entities = parse_entities_json(
            """
            [
              {"type": "PERSON", "value": "Петров А.В.", "confidence": 0.99},
              {"type": "IP", "value": "10.74.16.1/24", "confidence": 0.99}
            ]
            """
        )

        self.assertEqual([entity.type for entity in entities], ["PERSON", "IP"])
        self.assertEqual(entities[0].value, "Петров А.В.")

    def test_parses_json_inside_code_fence(self):
        entities = parse_entities_json(
            """```json
            [{"type": "EMAIL", "value": "user@example.com", "confidence": 0.98}]
            ```"""
        )

        self.assertEqual(len(entities), 1)
        self.assertEqual(entities[0].type, "EMAIL")

    def test_parses_last_valid_array_after_thinking_text(self):
        entities = parse_entities_json(
            """
            Thinking...
            The input mentioned an example empty array: []
            Final answer:
            [{"type": "HOSTNAME", "value": "vrn-core-rtr-02", "confidence": 0.99}]
            """
        )

        self.assertEqual(len(entities), 1)
        self.assertEqual(entities[0].type, "HOSTNAME")
        self.assertEqual(entities[0].value, "vrn-core-rtr-02")

    def test_invalid_json_fails_closed(self):
        with self.assertRaises(DetectorError):
            parse_entities_json("Петров А.В. -> PERSON")

    def test_parses_single_entity_object(self):
        entities = parse_entities_json('{"type": "PERSON", "value": "Петров А.В."}')

        self.assertEqual(len(entities), 1)
        self.assertEqual(entities[0].type, "PERSON")

    def test_parses_json_object_with_entities_array(self):
        entities = parse_entities_json(
            '{"entities": [{"type": "PERSON", "value": "Петров А.В.", "confidence": 0.99}]}'
        )

        self.assertEqual(len(entities), 1)
        self.assertEqual(entities[0].type, "PERSON")

    def test_parses_json_object_with_common_result_array_key(self):
        entities = parse_entities_json(
            '{"sensitive_entities": [{"type": "IP", "value": "10.0.0.1", "confidence": 0.9}]}'
        )

        self.assertEqual(len(entities), 1)
        self.assertEqual(entities[0].type, "IP")

    def test_parses_type_to_values_object(self):
        entities = parse_entities_json(
            '{"PERSON": ["Петров А.В."], "IP": ["10.0.0.1"]}'
        )

        self.assertEqual([entity.type for entity in entities], ["PERSON", "IP"])
        self.assertEqual([entity.value for entity in entities], ["Петров А.В.", "10.0.0.1"])

    def test_unknown_ascii_type_is_still_maskable(self):
        entities = parse_entities_json(
            '[{"type": "DATE", "value": "15.06.2026", "confidence": 0.75}]'
        )

        self.assertEqual(entities[0].type, "DATE")

    def test_non_numeric_confidence_does_not_drop_entity(self):
        entities = parse_entities_json(
            '[{"type": "PERSON", "value": "Петров А.В.", "confidence": "high"}]'
        )

        self.assertEqual(len(entities), 1)
        self.assertIsNone(entities[0].confidence)

    def test_parses_common_entity_field_aliases(self):
        entities = parse_entities_json(
            """
            [
              {"category": "PERSON", "text": "Петров А.В."},
              {"entity_type": "IP", "original": "10.0.0.1"}
            ]
            """
        )

        self.assertEqual([entity.type for entity in entities], ["PERSON", "IP"])
        self.assertEqual([entity.value for entity in entities], ["Петров А.В.", "10.0.0.1"])

    def test_ollama_detector_requests_json_without_thinking(self):
        captured = {}

        class FakeResponse:
            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, tb):
                return False

            def read(self):
                return json.dumps({"response": "[]"}).encode("utf-8")

        def fake_urlopen(http_request, timeout):
            captured["payload"] = json.loads(http_request.data.decode("utf-8"))
            captured["timeout"] = timeout
            return FakeResponse()

        detector = OllamaDetector(prompt="prompt", timeout_seconds=123)
        with mock.patch("core.detector.request.urlopen", side_effect=fake_urlopen):
            result = detector.detect_text("text")

        self.assertEqual(result, [])
        self.assertEqual(captured["payload"]["format"]["type"], "object")
        self.assertIn("entities", captured["payload"]["format"]["properties"])
        self.assertIs(captured["payload"]["think"], False)
        self.assertEqual(captured["timeout"], 123)


if __name__ == "__main__":
    unittest.main()
