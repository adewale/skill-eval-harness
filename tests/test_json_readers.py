"""Every JSON reader refuses the same strictness violations.

Three readers decided "duplicate key or NaN, so stop" by matching error-message
text, and the Gemini adapter carried its own copy of the strict parser. The
violation is now a type, and each reader is checked here against it."""
import json
import unittest
from pathlib import Path

import skill_benchmark as sb
from gemini_contracts import GeminiJsonResponse, GeminiStream
from json_contracts import StrictJSONViolation, strict_json_loads

GEMINI = Path(__file__).resolve().parent / "fixtures" / "gemini"
VIOLATIONS = {
    "duplicate key": '{"type": "a", "type": "b"}',
    "non-finite number": '{"score": NaN}',
}


class StrictReadersTests(unittest.TestCase):
    def test_the_violation_is_a_type_and_still_a_decode_error(self):
        for name, text in VIOLATIONS.items():
            with self.subTest(violation=name), self.assertRaises(StrictJSONViolation) as caught:
                strict_json_loads(text)
            self.assertIsInstance(caught.exception, json.JSONDecodeError)

    def test_stream_readers_skip_prose_but_stop_on_a_violation(self):
        self.assertEqual(list(sb.iter_json_objects('not json\n{"ok": 1}')), [{"ok": 1}])
        for name, text in VIOLATIONS.items():
            with self.subTest(reader="iter_json_objects", violation=name), \
                    self.assertRaises(ValueError):
                list(sb.iter_json_objects(f"prose\n{text}"))
            with self.subTest(reader="extract_json_object", violation=name), \
                    self.assertRaises(ValueError):
                sb.extract_json_object(f"verdict: {text}")

    def test_the_gemini_adapter_refuses_the_same_violations(self):
        # Start from captured responses that parse cleanly, so a rejection can
        # only come from the injected violation, not from a malformed shape.
        response = (GEMINI / "judge.json").read_text(encoding="utf-8")
        stream = (GEMINI / "tool-answer.stream.jsonl").read_text(encoding="utf-8")
        self.assertIsNone(GeminiJsonResponse.parse(response).protocol_error)
        self.assertIsNone(GeminiStream.parse(stream).protocol_error)
        corrupted = {
            "duplicate key": lambda text: text.replace("{", '{"type": "x", "type": "y", ', 1),
            "non-finite number": lambda text: text.replace("{", '{"nan_probe": NaN, ', 1),
        }
        for name, corrupt in corrupted.items():
            with self.subTest(violation=name):
                self.assertIn("malformed", GeminiJsonResponse.parse(corrupt(response)).protocol_error or "")
                self.assertIn("malformed", GeminiStream.parse(corrupt(stream)).protocol_error or "")


if __name__ == "__main__":
    unittest.main()
