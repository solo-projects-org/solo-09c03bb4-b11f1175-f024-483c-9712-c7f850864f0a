import struct

import pytest

from app.jcs import (
    MAX_SAFE_INTEGER,
    CanonicalizationError,
    canonical_string,
    canonicalize,
)

# RFC 8785 Appendix B, Table 1: IEEE-754 bit pattern -> ECMAScript JSON text.
NUMBER_VECTORS = [
    ("0000000000000000", "0"),
    ("8000000000000000", "0"),
    ("0000000000000001", "5e-324"),
    ("8000000000000001", "-5e-324"),
    ("7fefffffffffffff", "1.7976931348623157e+308"),
    ("ffefffffffffffff", "-1.7976931348623157e+308"),
    ("4340000000000000", "9007199254740992"),
    ("c340000000000000", "-9007199254740992"),
    ("4430000000000000", "295147905179352830000"),
    ("44b52d02c7e14af5", "9.999999999999997e+22"),
    ("44b52d02c7e14af6", "1e+23"),
    ("44b52d02c7e14af7", "1.0000000000000001e+23"),
    ("444b1ae4d6e2ef4e", "999999999999999700000"),
    ("444b1ae4d6e2ef4f", "999999999999999900000"),
    ("444b1ae4d6e2ef50", "1e+21"),
    ("3eb0c6f7a0b5ed8c", "9.999999999999997e-7"),
    ("3eb0c6f7a0b5ed8d", "0.000001"),
    ("41b3de4355555553", "333333333.3333332"),
    ("41b3de4355555554", "333333333.33333325"),
    ("41b3de4355555555", "333333333.3333333"),
    ("41b3de4355555556", "333333333.3333334"),
    ("41b3de4355555557", "333333333.33333343"),
    ("becbf647612f3696", "-0.0000033333333333333333"),
    ("43143ff3c1cb0959", "1424953923781206.2"),
]


@pytest.mark.parametrize("bits,expected", NUMBER_VECTORS)
def test_rfc8785_number_vectors(bits, expected):
    value = struct.unpack(">d", bytes.fromhex(bits))[0]
    assert canonicalize(value).decode() == expected


def test_rfc8785_sample_document():
    sample = {
        "numbers": [333333333.3333333, 1e30, 4.5, 0.002, 1e-27],
        "string": "€$\u000f\nA'B\"\\\\\"/",
        "literals": [None, True, False],
    }
    assert canonical_string(sample) == (
        '{"literals":[null,true,false],"numbers":[333333333.3333333,'
        "1e+30,4.5,0.002,1e-27],\"string\":\"€$\\u000f\\nA'B\\\"\\\\\\\\\\\"/\"}"
    )


def test_property_names_sort_by_utf16_code_units():
    document = {
        "€": 1,
        "\r": 2,
        "דּ": 3,
        "1": 4,
        "\U0001f600": 5,
        "\u0080": 6,
        "ö": 7,
    }
    # Order per RFC 8785 Section 3.2.3: \r, "1", U+0080, ö, €, emoji (surrogate
    # pair D83D DE00) and the Hebrew ligature (FB33) last.
    expected_keys = ["\\r", "1", "\u0080", "ö", "€", "\U0001f600", "דּ"]
    canonical = canonical_string(document)
    positions = [canonical.index(f'"{key}"') for key in expected_keys]
    assert positions == sorted(positions)
    assert canonical.index("1") < canonical.index("\u0080")


def test_nested_objects_are_sorted_and_arrays_kept():
    assert canonical_string({"b": [{"z": 1, "a": 2}], "a": 0}) == '{"a":0,"b":[{"a":2,"z":1}]}'


def test_string_escapes():
    assert canonical_string("a\u0000b\tc\nd\u001fe") == '"a\\u0000b\\tc\\nd\\u001fe"'
    assert canonical_string('"\\') == '"\\"\\\\"'


def test_rejects_unsafe_integers():
    with pytest.raises(CanonicalizationError):
        canonicalize(MAX_SAFE_INTEGER + 1)


def test_rejects_nan_and_infinity():
    for value in (float("nan"), float("inf"), float("-inf")):
        with pytest.raises(CanonicalizationError):
            canonicalize(value)


def test_rejects_lone_surrogate():
    with pytest.raises(CanonicalizationError):
        canonicalize("\ud800")


def test_output_is_utf8_bytes():
    assert canonicalize("菜价") == b'"\xe8\x8f\x9c\xe4\xbb\xb7"'
