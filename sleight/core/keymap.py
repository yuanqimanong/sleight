"""QWERTY 键位表 —— 一表两用的纯数据。

**一表两用**：

- ``key`` / ``code`` / ``keycode`` / ``shift`` 四列喂 ``Input.dispatchKeyEvent``
- ``hand`` / ``finger`` 两列给打字节奏做 digraph 分类

单独维护两张表迟早会对不上，所以合成一张。
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Final

__all__ = [
    "CHAR_MAP",
    "MODIFIER_KEYS",
    "MOD_ALT",
    "MOD_CTRL",
    "MOD_META",
    "MOD_SHIFT",
    "NAMED_KEYS",
    "UNSHIFTED",
    "Digraph",
    "Finger",
    "Hand",
    "KeyDef",
    "classify",
    "modifier_keydefs",
    "parse_chord",
]

#: ``Input.dispatchKeyEvent`` 的 modifiers 位掩码
MOD_ALT: Final = 1
MOD_CTRL: Final = 2
MOD_META: Final = 4
MOD_SHIFT: Final = 8


class Hand(StrEnum):
    LEFT = "L"
    RIGHT = "R"
    EITHER = "*"        # 空格：拇指，不参与手别对比


class Finger(StrEnum):
    PINKY = "pinky"
    RING = "ring"
    MIDDLE = "middle"
    INDEX = "index"
    THUMB = "thumb"


class Digraph(StrEnum):
    """相邻两次击键的关系。

    击键间隔的分布按它分层取样，实测数据在
    :data:`~sleight.core.human.timing.DIGRAPH_MS`。
    """

    ALTERNATE = "alternate"          # 双手交替，最快
    SAME_HAND = "same_hand"          # 同手不同指
    SAME_FINGER = "same_finger"      # 同指非双字母，最慢且方差最大
    DOUBLE = "double"                # 双字母
    UNKNOWN = "unknown"              # 至少一端不在表里


@dataclass(frozen=True, slots=True)
class KeyDef:
    key: str            # KeyboardEvent.key   —— "a" / "A" / "Enter"
    code: str           # 物理键位            —— "KeyA" / "Digit1" / "Enter"
    keycode: int        # windowsVirtualKeyCode
    shift: bool         # 打出该字符是否需要 Shift
    hand: Hand
    finger: Finger
    text: str = ""      # 实际插入的字符；功能键为空

    @property
    def modifiers(self) -> int:
        return MOD_SHIFT if self.shift else 0


# --------------------------------------------------------------------------- #
# 表的构建
#
# 每项：(下档字符, 上档字符 | None, code, keycode, hand, finger)
# --------------------------------------------------------------------------- #

_L, _R, _E = Hand.LEFT, Hand.RIGHT, Hand.EITHER
_PK, _RG, _MD, _IX, _TH = (
    Finger.PINKY, Finger.RING, Finger.MIDDLE, Finger.INDEX, Finger.THUMB,
)

_LAYOUT: Final[tuple[tuple[str, str | None, str, int, Hand, Finger], ...]] = (
    # 数字行
    ("`", "~", "Backquote", 192, _L, _PK),
    ("1", "!", "Digit1", 49, _L, _PK),
    ("2", "@", "Digit2", 50, _L, _RG),
    ("3", "#", "Digit3", 51, _L, _MD),
    ("4", "$", "Digit4", 52, _L, _IX),
    ("5", "%", "Digit5", 53, _L, _IX),
    ("6", "^", "Digit6", 54, _R, _IX),
    ("7", "&", "Digit7", 55, _R, _IX),
    ("8", "*", "Digit8", 56, _R, _MD),
    ("9", "(", "Digit9", 57, _R, _RG),
    ("0", ")", "Digit0", 48, _R, _PK),
    ("-", "_", "Minus", 189, _R, _PK),
    ("=", "+", "Equal", 187, _R, _PK),
    # 上排
    ("q", "Q", "KeyQ", 81, _L, _PK),
    ("w", "W", "KeyW", 87, _L, _RG),
    ("e", "E", "KeyE", 69, _L, _MD),
    ("r", "R", "KeyR", 82, _L, _IX),
    ("t", "T", "KeyT", 84, _L, _IX),
    ("y", "Y", "KeyY", 89, _R, _IX),
    ("u", "U", "KeyU", 85, _R, _IX),
    ("i", "I", "KeyI", 73, _R, _MD),
    ("o", "O", "KeyO", 79, _R, _RG),
    ("p", "P", "KeyP", 80, _R, _PK),
    ("[", "{", "BracketLeft", 219, _R, _PK),
    ("]", "}", "BracketRight", 221, _R, _PK),
    ("\\", "|", "Backslash", 220, _R, _PK),
    # 中排
    ("a", "A", "KeyA", 65, _L, _PK),
    ("s", "S", "KeyS", 83, _L, _RG),
    ("d", "D", "KeyD", 68, _L, _MD),
    ("f", "F", "KeyF", 70, _L, _IX),
    ("g", "G", "KeyG", 71, _L, _IX),
    ("h", "H", "KeyH", 72, _R, _IX),
    ("j", "J", "KeyJ", 74, _R, _IX),
    ("k", "K", "KeyK", 75, _R, _MD),
    ("l", "L", "KeyL", 76, _R, _RG),
    (";", ":", "Semicolon", 186, _R, _PK),
    ("'", '"', "Quote", 222, _R, _PK),
    # 下排
    ("z", "Z", "KeyZ", 90, _L, _PK),
    ("x", "X", "KeyX", 88, _L, _RG),
    ("c", "C", "KeyC", 67, _L, _MD),
    ("v", "V", "KeyV", 86, _L, _IX),
    ("b", "B", "KeyB", 66, _L, _IX),
    ("n", "N", "KeyN", 78, _R, _IX),
    ("m", "M", "KeyM", 77, _R, _IX),
    (",", "<", "Comma", 188, _R, _MD),
    (".", ">", "Period", 190, _R, _RG),
    ("/", "?", "Slash", 191, _R, _PK),
    # 空格
    (" ", None, "Space", 32, _E, _TH),
)


def _build() -> tuple[dict[str, KeyDef], dict[str, str]]:
    table: dict[str, KeyDef] = {}
    unshifted: dict[str, str] = {}
    for lower, upper, code, keycode, hand, finger in _LAYOUT:
        table[lower] = KeyDef(lower, code, keycode, False, hand, finger, lower)
        if upper is not None:
            # 上档字符共用物理键与 keycode，只是多按了 Shift
            table[upper] = KeyDef(upper, code, keycode, True, hand, finger, upper)
            unshifted[upper] = lower
    return table, unshifted


#: 可打印 ASCII → 键位定义
#: 上档字符 → 同一物理键的下档字符（``"A"`` → ``"a"``，``"!"`` → ``"1"``）
CHAR_MAP: Final[dict[str, KeyDef]]
UNSHIFTED: Final[dict[str, str]]
CHAR_MAP, UNSHIFTED = _build()


#: 功能键。``text`` 为空 —— 给功能键塞 text 会插入垃圾字符
NAMED_KEYS: Final[dict[str, KeyDef]] = {
    "Enter": KeyDef("Enter", "Enter", 13, False, _R, _PK, "\r"),
    "Tab": KeyDef("Tab", "Tab", 9, False, _L, _PK),
    "Backspace": KeyDef("Backspace", "Backspace", 8, False, _R, _PK),
    "Delete": KeyDef("Delete", "Delete", 46, False, _R, _PK),
    "Escape": KeyDef("Escape", "Escape", 27, False, _L, _PK),
    "ArrowLeft": KeyDef("ArrowLeft", "ArrowLeft", 37, False, _R, _IX),
    "ArrowUp": KeyDef("ArrowUp", "ArrowUp", 38, False, _R, _MD),
    "ArrowRight": KeyDef("ArrowRight", "ArrowRight", 39, False, _R, _RG),
    "ArrowDown": KeyDef("ArrowDown", "ArrowDown", 40, False, _R, _MD),
    "Home": KeyDef("Home", "Home", 36, False, _R, _PK),
    "End": KeyDef("End", "End", 35, False, _R, _PK),
    "PageUp": KeyDef("PageUp", "PageUp", 33, False, _R, _PK),
    "PageDown": KeyDef("PageDown", "PageDown", 34, False, _R, _PK),
}

#: 修饰键自己的键位定义。**必须真的发出去** —— 只设 modifiers 位掩码的话，页面会看到
#: ``shiftKey=true`` 却从来没有 Shift 的 keydown，物理上不可能，一查就露。
MODIFIER_KEYS: Final[tuple[tuple[int, KeyDef], ...]] = (
    (MOD_CTRL, KeyDef("Control", "ControlLeft", 17, False, _L, _PK)),
    (MOD_ALT, KeyDef("Alt", "AltLeft", 18, False, _L, _TH)),
    (MOD_SHIFT, KeyDef("Shift", "ShiftLeft", 16, False, _L, _PK)),
    (MOD_META, KeyDef("Meta", "MetaLeft", 91, False, _L, _TH)),
)


def modifier_keydefs(mask: int) -> list[KeyDef]:
    """位掩码 → 要按下的修饰键，按真人的习惯顺序（Ctrl → Alt → Shift → Meta）。"""
    return [kd for bit, kd in MODIFIER_KEYS if mask & bit]


_MODIFIER_ALIASES: Final[dict[str, int]] = {
    "alt": MOD_ALT, "option": MOD_ALT,
    "ctrl": MOD_CTRL, "control": MOD_CTRL,
    "meta": MOD_META, "cmd": MOD_META, "command": MOD_META, "super": MOD_META, "win": MOD_META,
    "shift": MOD_SHIFT,
}

# 大小写不敏感的别名，让 press("enter") / press("ESC") 也能用
_NAMED_LOOKUP: Final[dict[str, str]] = {n.lower(): n for n in NAMED_KEYS}
_NAMED_LOOKUP.update({"esc": "Escape", "del": "Delete", "return": "Enter", "space": " "})


# --------------------------------------------------------------------------- #
# 查询
# --------------------------------------------------------------------------- #


def classify(prev: str, cur: str) -> Digraph:
    """两个相邻字符的 digraph 类型 —— 间隔分布查表就靠它。

    :param prev: 前一个字符
    :param cur: 当前字符
    :returns: 两个字符里只要有一个不在键位表里，就返回 :attr:`Digraph.UNKNOWN`
    """
    if prev == cur:
        return Digraph.DOUBLE
    a, b = CHAR_MAP.get(prev), CHAR_MAP.get(cur)
    if a is None or b is None:
        return Digraph.UNKNOWN
    # 空格用拇指，与任何手都不构成"同手"约束
    if Hand.EITHER in (a.hand, b.hand):
        return Digraph.ALTERNATE
    if a.hand != b.hand:
        return Digraph.ALTERNATE
    # 同手：同指最慢（没有为下一击预备的余地），异指次之
    return Digraph.SAME_FINGER if a.finger == b.finger else Digraph.SAME_HAND


def parse_chord(chord: str) -> tuple[KeyDef, int]:
    """``"Enter"`` / ``"Ctrl+A"`` / ``"ctrl+shift+k"`` → ``(KeyDef, modifiers)``。

    **Shift 只在没有其它修饰键时隐含。** ``press("A")`` 是 Shift+a，打出大写 A；
    但 ``press("Ctrl+A")`` 是全选 —— 按约定它指 Ctrl 加 A 这个**键**，不是
    Ctrl+Shift+a。要后者得写全：``press("Ctrl+Shift+A")``。

    弄错的后果很实际：``Ctrl+Shift+a`` 在多数编辑器里根本不是全选，
    :meth:`Session.type(clear=True)` 会清不掉内容。
    """
    parts = [p for p in chord.split("+") if p]
    if not parts:
        raise ValueError("empty key chord")

    modifiers = 0
    explicit_shift = False
    for part in parts[:-1]:
        mod = _MODIFIER_ALIASES.get(part.lower())
        if mod is None:
            raise ValueError(f"unknown modifier {part!r} in {chord!r}")
        modifiers |= mod
        explicit_shift |= mod == MOD_SHIFT

    name = parts[-1]
    if (canonical := _NAMED_LOOKUP.get(name.lower())) is not None:
        if canonical in NAMED_KEYS:
            return NAMED_KEYS[canonical], modifiers
        # "space" 映射到字符 " "，它在 CHAR_MAP 而不是 NAMED_KEYS 里。
        # 原来这里直接跳过，于是 press("Space") 拿 "Space" 去查 CHAR_MAP 必然落空报错。
        name = canonical

    if (keydef := CHAR_MAP.get(name)) is None:
        if len(name) == 1:
            raise ValueError(f"{name!r} is not on the QWERTY layout; use Session.type() for text")
        raise ValueError(
            f"unknown key {name!r} in {chord!r}; "
            f"known named keys: {', '.join(sorted(NAMED_KEYS))}"
        )

    if keydef.shift and (modifiers & ~MOD_SHIFT) and not explicit_shift:
        # "Ctrl+A" → Ctrl 加 A 键，不是 Ctrl+Shift+a
        keydef = CHAR_MAP[UNSHIFTED[name]]

    return keydef, modifiers | keydef.modifiers
