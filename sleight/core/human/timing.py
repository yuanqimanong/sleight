"""Fitts 定律、digraph 打字节奏、思考停顿、滚动分步。

纯函数。所有随机都通过显式的 ``rng`` 参数，固定 seed 即可复现。
"""

from __future__ import annotations

from math import log2
from random import Random

from ..keymap import Digraph, classify
from .presets import HumanProfile

__all__ = [
    "DIGRAPH_MS",
    "digraph_delay",
    "key_dwell",
    "move_duration",
    "point_count",
    "scroll_steps",
    "span",
    "type_delays",
]

#: digraph 类型 → (均值, 下界, 上界)，毫秒。数据来自击键动力学文献的实测分布。
#:
#: 关键是**分层**而不是具体数字：异手比同手快 30–60 ms，同指最慢且方差最大。
#: 用一个固定区间随机打所有键，是最容易被击键动力学识别的做法。
DIGRAPH_MS: dict[Digraph, tuple[float, float, float]] = {
    Digraph.ALTERNATE: (114.0, 90.0, 157.0),
    Digraph.SAME_HAND: (131.0, 99.0, 215.0),
    Digraph.SAME_FINGER: (176.0, 120.0, 290.0),
    Digraph.DOUBLE: (145.0, 95.0, 240.0),
    Digraph.UNKNOWN: (135.0, 95.0, 230.0),
}

_PUNCTUATION = frozenset(".,;:!?)]}\"'")


def span(bounds: tuple[float, float], rng: Random) -> float:
    """从闭区间里均匀取一个值。

    :param bounds: ``(下界, 上界)``。上界不大于下界时直接返回下界
    :param rng: 随机源
    """
    lo, hi = bounds
    return rng.uniform(lo, hi) if hi > lo else lo


# --------------------------------------------------------------------------- #
# 鼠标
# --------------------------------------------------------------------------- #


def move_duration(distance: float, width: float, *, profile: HumanProfile) -> float:
    """Fitts 定律，**Shannon 形式**：``MT = a + b·log2(1 + D/W)``。

    教科书形式 ``log2(2D/W)`` 有三个退化点：``D=0`` 时 ``log2(0) = -inf`` 直接抛，
    目标比距离大时为负，``W=0`` 时除零。Shannon 形式全部消除，而且它才是
    MacKenzie 1992 之后的标准写法。

    :param distance: 移动距离 D，px。负数按 0 处理
    :param width: 目标宽度 W，px。会先抬到 ``profile.min_target_width`` 防除零
    :param profile: 取 ``fitts_a``（截距，秒）、``fitts_b``（斜率，秒/bit）和
        ``min_duration`` / ``max_duration`` 夹取区间
    :returns: 这段移动应该花的秒数。远处的小按钮比近处的大按钮慢
    """
    w = max(width, profile.min_target_width)
    index = log2(1.0 + max(distance, 0.0) / w)
    duration = profile.fitts_a + profile.fitts_b * index
    return min(max(duration, profile.min_duration), profile.max_duration)


def point_count(distance: float, width: float, *, profile: HumanProfile) -> int:
    """轨迹采样点数 —— 由时长和步进间隔反推，不是拍一个固定 steps=N。

    结果对目标大小敏感：远处的小按钮点数多于近处的大按钮。

    :param distance: 实际要走的路径长度，px。有 overshoot 时应传两段之和，
        而不是起点到落点的直线距离
    :param width: 目标宽度，px
    :param profile: 取 ``step_delay`` 的均值反推点数，并用 ``max_points`` 封顶
    :returns: 采样点数，至少 2
    """
    duration = move_duration(distance, width, profile=profile)
    mean_step = max(sum(profile.step_delay) / 2.0, 1e-4)
    return max(2, min(round(duration / mean_step), profile.max_points))


# --------------------------------------------------------------------------- #
# 键盘
# --------------------------------------------------------------------------- #


def key_dwell(rng: Random, profile: HumanProfile) -> float:
    """按下到抬起的时长，秒。

    独立于击键间隔的第二维特征，恒为 0 一眼可辨。

    :param rng: 随机源
    :param profile: 取 ``key_dwell`` 区间
    """
    return span(profile.key_dwell, rng)


def digraph_delay(prev: str, cur: str, *, rng: Random, profile: HumanProfile) -> float:
    """两次击键之间的间隔，按 digraph 类型取对应的实测分布。

    :param prev: 前一个字符
    :param cur: 当前字符
    :param rng: 随机源
    :param profile: 取 ``typing_scale``（均值倍数）和 ``typing_spread``（方差倍数）
    :returns: 秒。截断在该 digraph 类型的实测区间内
    """
    mean_ms, lo_ms, hi_ms = DIGRAPH_MS[classify(prev, cur)]

    mean = mean_ms * profile.typing_scale
    lo = lo_ms * profile.typing_scale
    hi = hi_ms * profile.typing_scale
    # 区间宽度按 ~4.5σ 反推，再用 typing_spread 调档。
    # FAST 同时压低均值和方差 —— 只调快而不收窄方差反而更不像熟练打字员。
    sigma = max((hi - lo) / 4.5 * profile.typing_spread, 1e-4)

    for _ in range(8):                       # 截断重采，不要夹到边界上堆积
        value = rng.gauss(mean, sigma)
        if lo <= value <= hi:
            return value / 1000.0
    return min(max(rng.uniform(lo, hi), lo), hi) / 1000.0


def type_delays(text: str, *, rng: Random, profile: HumanProfile) -> list[float]:
    """每个字符**之前**的等待，秒。第一个恒为 0。

    思考停顿按概率折进来：平均每 ``think_every`` 个字符一次，标点之后概率翻倍
    （``punctuation_think_bonus``）—— 人在句读处停顿，不在词中间。

    :param text: 要输入的整段文本
    :param rng: 随机源
    :param profile: 取打字相关的全部字段
    :returns: 与 ``text`` 等长的秒数列表；``delays[i]`` 是敲下第 i 个字符**之前**
        要等的时间，``delays[0]`` 恒为 0
    """
    if not text:
        return []

    lo, hi = profile.think_every
    base_p = 1.0 / max((lo + hi) / 2.0, 1.0)

    delays = [0.0]
    for i in range(1, len(text)):
        prev, cur = text[i - 1], text[i]
        delay = digraph_delay(prev, cur, rng=rng, profile=profile)
        p = base_p * (profile.punctuation_think_bonus if prev in _PUNCTUATION else 1.0)
        if rng.random() < min(p, 0.9):
            delay += span(profile.think_pause, rng)
        delays.append(delay)
    return delays


# --------------------------------------------------------------------------- #
# 滚动
# --------------------------------------------------------------------------- #


def scroll_steps(dy: int, *, rng: Random, profile: HumanProfile) -> list[int]:
    """把一次滚动切成多个 wheel delta。

    真实滚轮一格约 100 px；一次 ``deltaY=2000`` 是明显特征。

    :param dy: 总滚动距离，px。正数向下，负数向上，0 返回空列表
    :param rng: 随机源
    :param profile: 取 ``scroll_step`` 区间决定每步幅度
    :returns: 各步的 deltaY，符号与 ``dy`` 一致，总和精确等于 ``dy``
    """
    if dy == 0:
        return []
    sign = 1 if dy > 0 else -1
    remaining = abs(dy)
    steps: list[int] = []
    while remaining > 0:
        step = min(round(span(profile.scroll_step, rng)), remaining)
        step = max(step, 1)
        steps.append(sign * step)
        remaining -= step
    return steps
