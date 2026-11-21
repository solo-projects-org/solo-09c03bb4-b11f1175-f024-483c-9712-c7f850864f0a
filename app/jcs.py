"""RFC 8785 (JSON Canonicalization Scheme) canonicalization.

Only the subset needed by this project is implemented, but the pieces that
matter for interoperability are handled explicitly:

* object members are sorted by their raw UTF-16 code units,
* strings use the ECMAScript escaping rules,
* numbers use the ECMAScript Number-to-String algorithm,
* duplicate object names, NaN/Infinity, lone surrogates and unsafe integers
  are rejected instead of being silently normalized.
"""

from __future__ import annotations

import math
from typing import Any

MAX_SAFE_INTEGER = 9007199254740991  # 2**53 - 1


class CanonicalizationError(ValueError):
    """Raised when a value cannot be represented as canonical JSON."""


def canonicalize(value: Any) -> bytes:
    """Return the RFC 8785 UTF-8 representation of *value*."""

    out: list[str] = []
    _serialize(value, out)
    try:
        return "".join(out).encode("utf-8")
    except UnicodeEncodeError as exc:  # pragma: no cover - guarded per string
        raise CanonicalizationError(f"invalid Unicode string: {exc}") from exc


def canonical_string(value: Any) -> str:
    return canonicalize(value).decode("utf-8")


def _serialize(value: Any, out: list[str]) -> None:
    if value is None:
        out.append("null")
    elif value is True:
        out.append("true")
    elif value is False:
        out.append("false")
    elif isinstance(value, str):
        out.append(_string(value))
    elif isinstance(value, int):
        # bool is handled above; JSON numbers are IEEE-754 doubles in JCS.
        if abs(value) > MAX_SAFE_INTEGER:
            raise CanonicalizationError(
                f"integer {value} is outside the I-JSON safe range"
            )
        out.append(str(value))
    elif isinstance(value, float):
        out.append(_number(value))
    elif isinstance(value, (list, tuple)):
        out.append("[")
        for index, item in enumerate(value):
            if index:
                out.append(",")
            _serialize(item, out)
        out.append("]")
    elif isinstance(value, dict):
        out.append("{")
        items = list(value.items())
        for key, _ in items:
            if not isinstance(key, str):
                raise CanonicalizationError("object names must be strings")
        # RFC 8785 compares raw property names as arrays of UTF-16 code units.
        items.sort(key=lambda item: _utf16_key(item[0]))
        for index, (key, item) in enumerate(items):
            if index:
                out.append(",")
            out.append(_string(key))
            out.append(":")
            _serialize(item, out)
        out.append("}")
    else:
        raise CanonicalizationError(f"unsupported JSON value type: {type(value)!r}")


def _utf16_key(value: str) -> bytes:
    _reject_surrogates(value)
    return value.encode("utf-16-be")


_ESCAPES = {
    '"': '\\"',
    "\\": "\\\\",
    "\b": "\\b",
    "\t": "\\t",
    "\n": "\\n",
    "\f": "\\f",
    "\r": "\\r",
}


def _string(value: str) -> str:
    _reject_surrogates(value)
    pieces = ['"']
    for char in value:
        escaped = _ESCAPES.get(char)
        if escaped is not None:
            pieces.append(escaped)
        elif ord(char) < 0x20:
            pieces.append(f"\\u{ord(char):04x}")
        else:
            pieces.append(char)
    pieces.append('"')
    return "".join(pieces)


def _reject_surrogates(value: str) -> None:
    for char in value:
        code = ord(char)
        if 0xD800 <= code <= 0xDFFF:
            raise CanonicalizationError("lone UTF-16 surrogate is not valid JSON")


def _number(value: float) -> str:
    if not math.isfinite(value):
        raise CanonicalizationError("NaN and Infinity are not valid JSON numbers")
    if value == 0:
        # ECMAScript JSON.stringify(-0) is "0".
        return "0"

    # repr() produces the shortest round-trippable decimal for a Python float.
    # Its digits match ECMAScript's shortest representation; only the layout
    # rules (fixed vs. scientific, exponent formatting) differ.
    text = repr(value)
    sign = ""
    if text.startswith("-"):
        sign = "-"
        text = text[1:]

    if "e" in text or "E" in text:
        mantissa, exponent_text = text.lower().split("e")
        exponent = int(exponent_text)
    else:
        mantissa, exponent = text, 0

    if "." in mantissa:
        integer_part, fraction_part = mantissa.split(".", 1)
    else:
        integer_part, fraction_part = mantissa, ""

    digits = integer_part + fraction_part
    # "n" is the position of the decimal point counted from the first digit
    # in `digits` (value == 0.digits * 10**n).
    n = len(integer_part) + exponent

    # Drop leading zeros, shifting the decimal point accordingly.
    leading = len(digits) - len(digits.lstrip("0"))
    digits = digits[leading:]
    n -= leading

    # Drop trailing zeros; they do not change the value and shortest output
    # never needs them.
    digits = digits.rstrip("0")
    if not digits:
        return "0"

    k = len(digits)
    if k <= n <= 21:
        body = digits + "0" * (n - k)
    elif 0 < n <= 21:
        body = digits[:n] + "." + digits[n:]
    elif -6 < n <= 0:
        body = "0." + "0" * (-n) + digits
    else:
        exponent_value = n - 1
        exponent_sign = "+" if exponent_value >= 0 else "-"
        body = (
            digits[0]
            + ("." + digits[1:] if k > 1 else "")
            + "e"
            + exponent_sign
            + str(abs(exponent_value))
        )
    return sign + body
