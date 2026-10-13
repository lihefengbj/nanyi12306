"""Read a bounded subset of JavaScript data literals; never execute code."""
import math
import re


class LiteralParseError(ValueError):
    """Safe diagnostics: a fixed reason and offset, without source values."""

    def __init__(self, reason: str, offset: int) -> None:
        self.reason = reason
        self.offset = offset
        super().__init__(f"{reason} at offset {offset}")


_IDENTIFIER = re.compile(r"(?:[^\W\d]|[$_])(?:\w|[$])*", re.UNICODE)
_NUMBER = re.compile(
    r"[+-]?(?:0[xX][0-9a-fA-F]+|0[bB][01]+|0[oO][0-7]+|"
    r"(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)(?:[eE][+-]?[0-9]+)?)"
)
_ESCAPES = {"b": "\b", "f": "\f", "n": "\n", "r": "\r",
            "t": "\t", "v": "\v", "'": "'", '"': '"', "\\": "\\", "/": "/"}


class LiteralReader:
    MAX_CHARS = 1024 * 1024
    MAX_DEPTH = 64
    MAX_VALUES = 20000

    def __init__(self, source: str, start: int = 0) -> None:
        self.source = source
        self.start = start
        self.pos = start
        self.values = 0
        self.limit = min(len(source), start + self.MAX_CHARS)

    def _error(self, reason: str) -> None:
        raise LiteralParseError(reason, self.pos - self.start)

    def _peek(self) -> str:
        if self.pos >= self.limit:
            if self.limit < len(self.source):
                self._error("size_limit")
            return ""
        return self.source[self.pos]

    def _skip(self) -> None:
        while True:
            ch = self._peek()
            if ch and (ch.isspace() or ch == "\ufeff"):
                self.pos += 1
                continue
            if self.source.startswith("//", self.pos):
                self.pos += 2
                while self._peek() and self._peek() not in "\r\n\u2028\u2029":
                    self.pos += 1
                continue
            if self.source.startswith("/*", self.pos):
                end = self.source.find("*/", self.pos + 2, self.limit)
                if end < 0:
                    self._error("unterminated_comment")
                self.pos = end + 2
                continue
            return

    def _expect(self, ch: str) -> None:
        self._skip()
        if self._peek() != ch:
            self._error("unexpected_token")
        self.pos += 1

    def _string(self) -> str:
        quote = self._peek()
        self.pos += 1
        parts = []
        while True:
            ch = self._peek()
            if not ch:
                self._error("unterminated_string")
            self.pos += 1
            if ch == quote:
                # Combine paired JS UTF-16 escapes without dropping lone
                # surrogates or changing token characters.
                value = "".join(parts)
                return value.encode("utf-16-le", "surrogatepass").decode(
                    "utf-16-le", "surrogatepass")
            if ch in "\r\n\u2028\u2029":
                self._error("newline_in_string")
            if ch != "\\":
                parts.append(ch)
                continue
            escape = self._peek()
            if not escape:
                self._error("unterminated_escape")
            self.pos += 1
            if escape in _ESCAPES:
                parts.append(_ESCAPES[escape])
            elif escape in "\r\n\u2028\u2029":
                if escape == "\r" and self._peek() == "\n":
                    self.pos += 1
            elif escape == "0":
                if self._peek().isdigit():
                    self._error("unsupported_octal_escape")
                parts.append("\0")
            elif escape in ("x", "u"):
                if escape == "u" and self._peek() == "{":
                    self.pos += 1
                    end = self.source.find("}", self.pos, min(self.limit, self.pos + 7))
                    if end < 0:
                        self._error("invalid_unicode_escape")
                    digits = self.source[self.pos:end]
                    self.pos = end + 1
                else:
                    size = 2 if escape == "x" else 4
                    digits = self.source[self.pos:min(self.pos + size, self.limit)]
                    if len(digits) != size:
                        self._error("invalid_hex_escape")
                    self.pos += size
                if not re.fullmatch(r"[0-9a-fA-F]+", digits):
                    self._error("invalid_hex_escape")
                number = int(digits, 16)
                if number > 0x10ffff:
                    self._error("invalid_unicode_escape")
                parts.append(chr(number))
            elif escape.isdigit():
                self._error("unsupported_octal_escape")
            else:
                # JS non-escape characters (\', \>, etc.) retain the character.
                parts.append(escape)

    def _value(self, depth: int):
        if depth > self.MAX_DEPTH:
            self._error("depth_limit")
        self.values += 1
        if self.values > self.MAX_VALUES:
            self._error("value_limit")
        self._skip()
        ch = self._peek()
        if ch in ("'", '"'):
            return self._string()
        if ch == "{":
            self.pos += 1
            result = {}
            self._skip()
            if self._peek() == "}":
                self.pos += 1
                return result
            while True:
                self._skip()
                if self._peek() in ("'", '"'):
                    key = self._string()
                else:
                    match = _IDENTIFIER.match(self.source, self.pos, self.limit)
                    if match:
                        key = match.group(0)
                        self.pos = match.end()
                    else:
                        number = re.compile(
                            r"0[xX][0-9a-fA-F]+|0[bB][01]+|0[oO][0-7]+|[0-9]+"
                        ).match(self.source, self.pos, self.limit)
                        if not number or len(number.group(0)) > 128:
                            self._error("invalid_object_key")
                        raw = number.group(0)
                        base = 0 if raw.lower().startswith(("0x", "0b", "0o")) else 10
                        key = str(int(raw, base))
                        self.pos = number.end()
                if key == "__proto__":
                    self._error("unsupported_prototype_key")
                self._expect(":")
                result[key] = self._value(depth + 1)
                self._skip()
                if self._peek() == "}":
                    self.pos += 1
                    return result
                self._expect(",")
                self._skip()
                if self._peek() == "}":  # trailing comma
                    self.pos += 1
                    return result
        if ch == "[":
            self.pos += 1
            result = []
            self._skip()
            if self._peek() == "]":
                self.pos += 1
                return result
            while True:
                result.append(self._value(depth + 1))
                self._skip()
                if self._peek() == "]":
                    self.pos += 1
                    return result
                self._expect(",")
                self._skip()
                if self._peek() == "]":
                    self.pos += 1
                    return result
        for word, value in (("true", True), ("false", False), ("null", None)):
            if self.source.startswith(word, self.pos):
                self.pos += len(word)
                return value
        number = _NUMBER.match(self.source, self.pos, self.limit)
        if number:
            raw = number.group(0)
            self.pos = number.end()
            if len(raw) > 128:
                self._error("number_limit")
            unsigned = raw.lstrip("+-")
            if unsigned.lower().startswith(("0x", "0b", "0o")):
                value = int(unsigned, 0) * (-1 if raw.startswith("-") else 1)
            elif "." in raw or "e" in raw.lower():
                value = float(raw)
                if not math.isfinite(value):
                    self._error("nonfinite_number")
            else:
                value = int(raw)
            return value
        self._error("unsupported_expression")

    def read_object(self) -> dict:
        self._skip()
        if self._peek() != "{":
            self._error("expected_object")
        result = self._value(0)
        self._skip()
        # The object must end at an assignment boundary, not an expression
        # involving a function call, property lookup, or arithmetic.
        if self._peek() not in ("", ";", ",", "<"):
            self._error("unsupported_expression")
        return result
