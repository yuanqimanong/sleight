"""WindMouse 物理抖动 —— 纯函数，只依赖标准库。

算法原文：https://ben.land/post/2021/04/25/windmouse-human-mouse-movement/

把光标建模成受两个力作用的质点：指向目标的**引力**，和随机游走的**风力**。

sleight 默认**不用它生成整条路径**（形状不可控、步数无法预定、不做 overshoot），
只取其风力项叠加到贝塞尔采样点上。风力的递推 ``w ← w/√3 + noise`` 是一个 AR(1) 过程，
衰减系数 1/√3 ≈ 0.577 —— 这正是"手抖"与"白噪声"的区别所在，也是
:func:`sleight.core.human.curves` 那条自相关断言要保护的性质。

完整的 :func:`wind_mouse` 仍然保留：它是参数标定的参照物，也方便和原文逐行对拍。
"""

from __future__ import annotations

from collections.abc import Iterator
from math import hypot, sqrt
from random import Random

__all__ = ["SQRT3", "SQRT5", "wind_mouse", "wind_offsets"]

SQRT3 = sqrt(3.0)
SQRT5 = sqrt(5.0)


def wind_mouse(
    x0: float,
    y0: float,
    x1: float,
    y1: float,
    *,
    rng: Random,
    gravity: float = 9.0,
    wind: float = 3.0,
    max_step: float = 15.0,
    damp_dist: float = 12.0,
    max_iter: int = 10_000,
) -> Iterator[tuple[int, int]]:
    """原版 WindMouse，yield 从起点走到终点的整数坐标序列。

    :param x0: 起点 x，viewport CSS 像素
    :param y0: 起点 y
    :param x1: 终点 x
    :param y1: 终点 y
    :param rng: 随机源。传固定 seed 的 :class:`random.Random` 可完全复现轨迹
    :param gravity: G₀ —— 指向目标的引力大小。原作者标定值 9
    :param wind: W₀ —— 风力扰动大小。原作者标定值 3
    :param max_step: M₀ —— 单步速度上限。原作者标定值 15
    :param damp_dist: D₀ —— 进入该距离后风力衰减、限速收紧。原作者标定值 12
    :param max_iter: 迭代上限。参数被调坏时（比如引力为 0）循环可能不收敛，
        宁可截断也不要挂死调用方。
    :returns: 迭代器，逐个产出 ``(x, y)`` 整数坐标；连续重复的点已去掉
    """
    x, y = float(x0), float(y0)
    vx = vy = wx = wy = 0.0
    m = float(max_step)
    last: tuple[int, int] | None = None

    for _ in range(max_iter):
        dist = hypot(x1 - x, y1 - y)
        if dist < 1.0:
            break

        w_mag = min(wind, dist)
        if dist >= damp_dist:
            # 远离目标：注入随机风
            wx = wx / SQRT3 + (2.0 * rng.random() - 1.0) * w_mag / SQRT5
            wy = wy / SQRT3 + (2.0 * rng.random() - 1.0) * w_mag / SQRT5
        else:
            # 接近目标：风衰减 + 限速收紧
            wx, wy = wx / SQRT3, wy / SQRT3
            m = rng.random() * 3.0 + 3.0 if m < 3.0 else m / SQRT5

        vx += wx + gravity * (x1 - x) / dist
        vy += wy + gravity * (y1 - y) / dist

        v_mag = hypot(vx, vy)
        if v_mag > m:
            clip = m / 2.0 + rng.random() * m / 2.0
            vx, vy = vx / v_mag * clip, vy / v_mag * clip

        x += vx
        y += vy

        point = (round(x), round(y))
        if point != last:          # 真实鼠标不会上报没动过的位置
            last = point
            yield point


#: 抖动 AR(1) 的保持系数。
#:
#: 原版 WindMouse 用 1/√3 ≈ 0.577，相关时间约 1.8 个采样点。但那个递推是**每个采样点
#: 走一步**的，采样越密、抖动频率越高 —— 而真实手抖是 8–12 Hz 的生理震颤，跟鼠标回报率
#: 没关系。用 0.577 的后果是：CAREFUL 这种采样更密的预设，抖动会把速度包络整个淹掉
#: （实测非 overshoot 移动的单峰率从 0.90 掉到 0.47）。
#:
#: 0.75 对应约 3.5 个采样点的相关时间，在 8–20 ms 的回报间隔下落在 10 Hz 量级。
TREMOR_DECAY = 0.75


def wind_offsets(
    n: int,
    *,
    rng: Random,
    magnitude: float,
    decay: float = TREMOR_DECAY,
    taper: bool = True,
) -> list[tuple[float, float]]:
    """``n`` 个相关的抖动偏移量，取 WindMouse 的风力递推。

    偏移是**相关**的，不是独立高斯 —— 每点独立采样的白噪声在功率谱上一眼可辨，
    不像手抖（实测 lag-1 自相关：本函数 ≈ 0.75，白噪声 ≈ 0.03）。

    :param n: 要生成多少个偏移量，通常等于轨迹采样点数。``n <= 0`` 返回空列表
    :param rng: 随机源
    :param magnitude: 抖动幅度，相当于 W₀。``<= 0`` 返回全零
        （``profile.wind == 0`` 时轨迹退化成纯贝塞尔）
    :param decay: AR(1) 保持系数，见 :data:`TREMOR_DECAY`。振幅会做归一化补偿，
        所以调 ``decay`` 只改变抖动的**频率**，不改变幅度
    :param taper: 按 ``1 - t`` 线性收敛到 0，让最后一个点落在精确的目标上。
        没有它，落点会被抖动推离刚算好的位置，命中校验也就白做了
    :returns: 长度为 ``n`` 的 ``(dx, dy)`` 列表，可直接逐点加到贝塞尔采样点上
    """
    if n <= 0:
        return []
    if magnitude <= 0.0:
        return [(0.0, 0.0)] * n

    # 平稳方差 = 新息方差 / (1 - decay²)。补偿掉 decay 对幅度的影响，
    # 使 decay = 1/√3 时正好退化成原版的 magnitude/√5。
    innovation = magnitude / SQRT5 * sqrt((1.0 - decay * decay) / (2.0 / 3.0))

    out: list[tuple[float, float]] = []
    wx = wy = 0.0
    for i in range(n):
        wx = wx * decay + (2.0 * rng.random() - 1.0) * innovation
        wy = wy * decay + (2.0 * rng.random() - 1.0) * innovation
        if taper:
            scale = 1.0 - (i / (n - 1)) if n > 1 else 0.0   # i = n-1 时为 0
            out.append((wx * scale, wy * scale))
        else:
            out.append((wx, wy))
    return out
