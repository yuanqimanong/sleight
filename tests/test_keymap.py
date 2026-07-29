"""QWERTY 键位表与 chord 解析。"""

from __future__ import annotations

import string

import pytest

from sleight.core.keymap import (
    CHAR_MAP,
    MOD_ALT,
    MOD_CTRL,
    MOD_META,
    MOD_SHIFT,
    NAMED_KEYS,
    UNSHIFTED,
    Digraph,
    Hand,
    classify,
    parse_chord,
)


def test_full_printable_ascii_is_covered():
    """漏一个字符就是"打字打到一半静默丢字"。"""
    missing = [c for c in string.printable[:95] if c not in CHAR_MAP]
    assert missing == [], missing


def test_every_entry_is_internally_consistent():
    for char, kd in CHAR_MAP.items():
        assert kd.key == char
        assert kd.text == char
        assert kd.keycode > 0
        assert kd.code
        assert kd.modifiers == (MOD_SHIFT if kd.shift else 0)


def test_shifted_and_unshifted_share_a_physical_key():
    for upper, lower in UNSHIFTED.items():
        assert CHAR_MAP[upper].code == CHAR_MAP[lower].code
        assert CHAR_MAP[upper].keycode == CHAR_MAP[lower].keycode
        assert CHAR_MAP[upper].shift and not CHAR_MAP[lower].shift
        # 手别/手指也必须一致，否则 digraph 分类会随大小写漂移
        assert CHAR_MAP[upper].hand == CHAR_MAP[lower].hand
        assert CHAR_MAP[upper].finger == CHAR_MAP[lower].finger


def test_letters_use_standard_virtual_key_codes():
    for letter in string.ascii_lowercase:
        assert CHAR_MAP[letter].keycode == ord(letter.upper())
        assert CHAR_MAP[letter].code == f"Key{letter.upper()}"


def test_digits_use_standard_codes():
    for digit in string.digits:
        assert CHAR_MAP[digit].keycode == ord(digit)
        assert CHAR_MAP[digit].code == f"Digit{digit}"


# --------------------------------------------------------------------------- #
# digraph 分类
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("pair", "expected"),
    [
        ("th", Digraph.ALTERNATE),     # 左 index / 右 index
        ("he", Digraph.ALTERNATE),
        ("er", Digraph.SAME_HAND),     # 左 middle / 左 index
        ("as", Digraph.SAME_HAND),
        ("de", Digraph.SAME_FINGER),   # 左 middle 两次
        ("ed", Digraph.SAME_FINGER),
        ("ll", Digraph.DOUBLE),
        ("ss", Digraph.DOUBLE),
    ],
)
def test_digraph_classification(pair: str, expected: Digraph):
    assert classify(pair[0], pair[1]) is expected


def test_space_is_thumb_and_never_constrains_the_hand():
    assert CHAR_MAP[" "].hand is Hand.EITHER
    assert classify("a", " ") is Digraph.ALTERNATE
    assert classify(" ", "a") is Digraph.ALTERNATE


def test_unknown_characters_classify_as_unknown():
    assert classify("你", "好") is Digraph.UNKNOWN
    assert classify("a", "好") is Digraph.UNKNOWN


def test_case_does_not_change_the_digraph_class():
    assert classify("D", "E") is classify("d", "e")


# --------------------------------------------------------------------------- #
# chord 解析
# --------------------------------------------------------------------------- #


def test_bare_named_key():
    kd, mods = parse_chord("Enter")
    assert kd.keycode == 13 and mods == 0


def test_named_keys_are_case_insensitive():
    assert parse_chord("enter")[0].key == "Enter"
    assert parse_chord("ESC")[0].key == "Escape"
    assert parse_chord("del")[0].key == "Delete"


def test_bare_uppercase_letter_implies_shift():
    """press("A") 应该打出大写 A。"""
    kd, mods = parse_chord("A")
    assert kd.text == "A" and mods == MOD_SHIFT


def test_ctrl_a_is_select_all_not_ctrl_shift_a():
    """按约定 Ctrl+A 指 Ctrl 加 A 这个**键**。

    弄错的后果很实际：Ctrl+Shift+a 在多数编辑器里不是全选，
    ``type(clear=True)`` 会清不掉内容。
    """
    kd, mods = parse_chord("Ctrl+A")
    assert mods == MOD_CTRL, f"expected plain Ctrl, got {mods}"
    assert kd.code == "KeyA"


def test_explicit_shift_is_honoured():
    kd, mods = parse_chord("Ctrl+Shift+A")
    assert mods == MOD_CTRL | MOD_SHIFT and kd.code == "KeyA"


def test_shifted_symbol_with_a_modifier_falls_back_to_the_base_key():
    kd, mods = parse_chord("Ctrl+!")
    assert kd.code == "Digit1" and mods == MOD_CTRL


def test_all_modifier_aliases():
    for alias, expected in (
        ("ctrl", MOD_CTRL), ("control", MOD_CTRL),
        ("alt", MOD_ALT), ("option", MOD_ALT),
        ("meta", MOD_META), ("cmd", MOD_META), ("win", MOD_META),
        ("shift", MOD_SHIFT),
    ):
        assert parse_chord(f"{alias}+k")[1] == expected, alias


def test_multiple_modifiers_combine():
    _, mods = parse_chord("ctrl+alt+shift+k")
    assert mods == MOD_CTRL | MOD_ALT | MOD_SHIFT


def test_errors_are_actionable():
    with pytest.raises(ValueError, match="empty key chord"):
        parse_chord("")
    with pytest.raises(ValueError, match="unknown modifier"):
        parse_chord("hyper+k")
    with pytest.raises(ValueError, match="known named keys"):
        parse_chord("Wingding")
    with pytest.raises(ValueError, match=r"Session\.type"):
        parse_chord("你")


def test_named_keys_do_not_carry_stray_text():
    """给功能键塞 text 会插入垃圾字符。"""
    for name, kd in NAMED_KEYS.items():
        if name != "Enter":
            assert kd.text == "", name
