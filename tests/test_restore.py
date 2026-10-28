import unittest

from core.models import Mapping
from core.restore import RestoreError, restore_text


class RestoreTests(unittest.TestCase):
    def test_restores_placeholders_deterministically(self):
        mapping = Mapping(
            document_id="doc-1",
            source_name="result_SAFE.txt",
            entities={
                "PERSON_001": "Петров А.В.",
                "OBJECT_001": "ВРН-Север-17",
            },
        )
        text = "⟦PERSON_001⟧ прибыл на ⟦OBJECT_001⟧."

        result = restore_text(text, mapping)

        self.assertEqual(result.text, "Петров А.В. прибыл на ВРН-Север-17.")
        self.assertEqual(result.expected_count, 2)
        self.assertEqual(result.found_count, 2)
        self.assertEqual(result.missing, [])

    def test_fails_when_expected_placeholder_is_missing(self):
        mapping = Mapping(
            document_id="doc-1",
            source_name="result_SAFE.txt",
            entities={
                "PERSON_001": "Петров А.В.",
                "OBJECT_001": "ВРН-Север-17",
            },
        )
        text = "⟦PERSON_001⟧ прибыл на объект."

        with self.assertRaises(RestoreError) as raised:
            restore_text(text, mapping)

        error = raised.exception
        self.assertEqual(error.expected_count, 2)
        self.assertEqual(error.found_count, 1)
        self.assertEqual(error.missing, ["OBJECT_001"])
        self.assertIn("OBJECT_001", str(error))


if __name__ == "__main__":
    unittest.main()
