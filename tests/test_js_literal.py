import json
import unittest
from unittest.mock import patch

from core.js_literal import LiteralReader
from core.order import OrderError, _js_object


class JsLiteralTests(unittest.TestCase):
    def read(self, literal):
        return _js_object(f"var ticketInfoForPassengerForm = {literal};",
                          "ticketInfoForPassengerForm")

    def test_unquoted_keys_nested_objects_arrays_comments_and_trailing_commas(self):
        result = self.read("""
        {
            key_check_isChange: 'test-key',
            leftTicketStr: 'test-ticket',
            train_location: 'Q7',
            isAsync: '1', // this } is not the end of the object
            queryLeftTicketRequestDTO: {
                ypInfoDetail: 'test-detail',
                count: 12,
            },
            tickets: [
                {seat_type: '1', selected: true,},
                {seat_type: 'O', selected: false,},
            ],
            /* braces { } and 'quotes' in comments must be ignored */
            optional: null,
        }
        """)
        self.assertEqual(result, {
            "key_check_isChange": "test-key", "leftTicketStr": "test-ticket",
            "train_location": "Q7", "isAsync": "1",
            "queryLeftTicketRequestDTO": {"ypInfoDetail": "test-detail", "count": 12},
            "tickets": [{"seat_type": "1", "selected": True},
                        {"seat_type": "O", "selected": False}], "optional": None,
        })

    def test_javascript_string_escapes_preserve_literal_values(self):
        result = self.read(r"""{
            apostrophe: 'O\'Brien',
            quote: 'a"b',
            backslash: 'a\\b',
            newline: 'line\nbreak',
            ticket: 'a\x2Bb\u002Fc\x3D',
            unicode: '\u4E2D\u6587',
            emoji: '\uD83D\uDE00\u{1F600}',
            control: '\0\t\b\f\r\v',
            unknownEscape: '\>',
        }""")
        self.assertEqual(result, {
            "apostrophe": "O'Brien", "quote": 'a"b', "backslash": "a\\b",
            "newline": "line\nbreak", "ticket": "a+b/c=", "unicode": "中文",
            "emoji": "😀😀", "control": "\0\t\b\f\r\v", "unknownEscape": ">",
        })

    def test_escaped_line_continuation_is_removed_without_losing_value(self):
        self.assertEqual(self.read("{value: 'line\\\r\ncontinued',}"),
                         {"value": "linecontinued"})

    def test_json_literals_remain_compatible(self):
        expected = {"token": "ab\\cd\"ef'g", "date": {"time": 1792080000000},
                    "values": [None, False, 0, -1, 1.25], "name": "中文"}
        self.assertEqual(self.read(json.dumps(expected)), expected)

    def test_supported_numbers_and_identifier_keys(self):
        result = self.read(
            "{$key: +1, _key: -2, 零: 0, hex: 0x1a, binary: 0b10, "
            "octal: 0o17, fraction: .25, exponent: -1.2e3,}")
        self.assertEqual(result, {"$key": 1, "_key": -2, "零": 0, "hex": 26,
                                 "binary": 2, "octal": 15, "fraction": .25,
                                 "exponent": -1200.0})

    def test_integer_keys_in_seat_maps_are_converted_to_string_keys(self):
        self.assertEqual(self.read("{seatTypes: {1: '硬座', 0x9: '商务座',},}"),
                         {"seatTypes": {"1": "硬座", "9": "商务座"}})

    def test_comment_before_value_and_multiline_strings_containing_braces(self):
        self.assertEqual(self.read(
            """/* before */ { /* block } */ value: 'http://host/{a}', """
            """second: "/* not a comment */",} // end"""),
            {"value": "http://host/{a}", "second": "/* not a comment */"})

    def test_calls_expressions_getters_spreads_and_templates_are_never_executed(self):
        forbidden = (
            "{value: dangerousFunction()}",
            "{value: new Date(123)}",
            "{value: window.token}",
            "{value: 'a' + 'b'}",
            "{get value() {return 'test'}}",
            "{...otherObject}",
            "{value: `template`}",
            "{value: undefined}",
            "{value: NaN}",
            "{value: true}.value",
            "{value: true} || dangerousFunction()",
            "{value: true}()",
        )
        for literal in forbidden:
            with self.subTest(literal=literal), self.assertRaises(OrderError):
                self.read(literal)

    def test_malformed_input_fails_without_leaking_source_text(self):
        invalid = (
            "{value: 'PRIVATE_PASSENGER}",
            "{value: 'PRIVATE_TICKET\\xQQ'}",
            "{value: 'PRIVATE_TICKET\\u11QQ'}",
            "{value: 'PRIVATE_TICKET\\u{110000}'}",
            "{value: 'PRIVATE_TICKET\\01'}",
            "{value: 'PRIVATE_TICKET\nraw newline'}",
            "{value: true /* PRIVATE_TICKET }",
            "{value: [1,,2]}",
            "{__proto__: {}, __proto__: {}}",
            "{value: 1e999}",
        )
        for literal in invalid:
            with self.subTest(literal=literal), \
                    self.assertLogs("ny12306.order", level="WARNING") as captured:
                with self.assertRaises(OrderError) as raised:
                    self.read(literal)
                diagnostic = str(raised.exception) + "\n".join(captured.output)
                self.assertIn("offset=", diagnostic)
                self.assertIn("reason=", diagnostic)
                self.assertNotIn("PRIVATE_PASSENGER", diagnostic)
                self.assertNotIn("PRIVATE_TICKET", diagnostic)

    def test_bounded_depth_size_and_value_count(self):
        with patch.object(LiteralReader, "MAX_DEPTH", 3):
            with self.assertRaisesRegex(OrderError, "depth_limit"):
                self.read("{value: [[[[[0]]]]]}")
        with patch.object(LiteralReader, "MAX_CHARS", 20):
            with self.assertRaisesRegex(OrderError, "size_limit"):
                self.read("{value: '" + "x" * 40 + "'}")
        with patch.object(LiteralReader, "MAX_VALUES", 3):
            with self.assertRaisesRegex(OrderError, "value_limit"):
                self.read("{value: [1,2,3,4]}")

    def test_repeated_data_keys_follow_javascript_last_value_semantics(self):
        self.assertEqual(self.read("{seat: '1', seat: 'O',}"), {"seat": "O"})

    def test_unicode_escape_is_four_digits_followed_by_ordinary_characters(self):
        self.assertEqual(self.read(r"{value: '\u1100QQ'}"), {"value": "\u1100QQ"})


if __name__ == "__main__":
    unittest.main()
