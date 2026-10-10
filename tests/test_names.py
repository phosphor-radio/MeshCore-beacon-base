import pytest

from beacon_base import names


@pytest.mark.parametrize(
    "raw, expected",
    [
        (b"beacon-001", "beacon-001"),
        (b"  spaced   out  ", "spaced out"),
        ("Café \U0001f50b".encode(), "Café \U0001f50b"),
        (b"line1\nline2", "line1 line2"),
        (b"tab\tand\rreturn", "tab and return"),
        (b"\x1b[31mred\x1b[0m", "[31mred [0m"),  # the escape byte is gone, so the terminal sees plain text
        (b"nul\x00in\x07bell", "nul in bell"),
        ("rtl‮override".encode(), "rtl override"),
        ("zero​width".encode(), "zero width"),
        ("line sep".encode(), "line sep"),
        (b"bad \xff\xfe utf8", "bad �� utf8"),
        (b"", None),
        (b"   ", None),
        (b"\x1b\x07\n", None),
    ],
)
def test_sanitize_name(raw, expected):
    assert names.sanitize_name(raw) == expected


def test_long_names_are_cut_on_a_character_boundary():
    assert names.sanitize_name(b"x" * 100) == "x" * 32
    out = names.sanitize_name(("a" * 31 + "é").encode())  # 32 bytes of ASCII-ish ending in a 2-byte character
    assert out == "a" * 31 and len(out.encode()) <= 32
    emoji = names.sanitize_name(("\U0001f50b" * 10).encode())
    assert emoji == "\U0001f50b" * 8 and len(emoji.encode()) == 32


def test_clean_name_for_operator_text():
    assert names.clean_name("North\tRidge\n") == "North Ridge"
    assert names.clean_name("\x1b") is None
    assert names.clean_name("x" * 100, limit=64) == "x" * 64


def test_labels():
    p = bytes.fromhex("f5b165224a58b791")
    assert names.prefix_label(p) == "f5b165"
    assert names.label("Roof", p) == "Roof (f5b165)"
    assert names.label(None, p) == "f5b165224a58"
    assert names.label("", p) == "f5b165224a58"
