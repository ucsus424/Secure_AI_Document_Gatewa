import unittest

from core.models import Mapping
from core.restore import RestoreError, UnknownPlaceholdersError, restore_text


def petrov_mapping():
    return Mapping(
        document_id="AB12CD34",
        source_name="result_SAFE.txt",
        entities={
            "PERSON_001": "Петров А.В.",
            "OBJECT_001": "ВРН-Север-17",
        },
    )


class RestoreTests(unittest.TestCase):
    def test_restores_placeholders_deterministically(self):
        text = "⟦PERSON_001⟧ прибыл на ⟦OBJECT_001⟧."

        result = restore_text(text, petrov_mapping())

        self.assertEqual(result.text, "Петров А.В. прибыл на ВРН-Север-17.")
        self.assertEqual(result.expected_count, 2)
        self.assertEqual(result.found_count, 2)
        self.assertEqual(result.missing, [])

    def test_removed_placeholder_is_a_warning_not_an_error(self):
        # Абзац с ⟦OBJECT_001⟧ удалили при редактировании — это нормально.
        text = "⟦PERSON_001⟧ прибыл на объект."

        result = restore_text(text, petrov_mapping())

        self.assertEqual(result.text, "Петров А.В. прибыл на объект.")
        self.assertEqual(result.expected_count, 2)
        self.assertEqual(result.found_count, 1)
        self.assertEqual(result.missing, ["OBJECT_001"])

    def test_placeholder_of_another_document_fails(self):
        text = "⟦PERSON_001⟧ настроил ⟦IP_001⟧."

        with self.assertRaises(UnknownPlaceholdersError) as raised:
            restore_text(text, petrov_mapping())

        error = raised.exception
        self.assertIsInstance(error, RestoreError)
        self.assertEqual(error.document_id, "AB12CD34")
        self.assertEqual(error.unknown, ["IP_001"])
        self.assertIn("IP_001", str(error))
        self.assertNotIn("Петров", str(error))


if __name__ == "__main__":
    unittest.main()
    