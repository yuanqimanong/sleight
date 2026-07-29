"""贝塞尔骨架、缓动、overshoot、落点采样。

纯函数，只依赖标准库。注入固定 ``rng`` 就能断言统计特征，不需要浏览器。

轨迹的组装分工：贝塞尔定宏观形状，Fitts 定点数，WindMouse 风力项定微观抖动，
overshoot 单独接一段回修。入口是 :func:`mouse_path`。
"""

from __future__ import annotations

from itertools import pairwise
from math import ceil, cos, floor, hypot, pi
from random import Random

from ..types import Box, Point
from .presets import HumanProfile
from .wind import wind_offsets

__all__ = ["bezier", "ease", "landing_point", "mouse_path", "overshoot_point"]


def ease(t: float) -> float:
    """余弦缓动：加速 → 巡航 → 减速。

    把均匀的 ``t`` 映射成非均匀的弧长比例，于是等时间间隔采样出来的点距先疏后密
    再疏 —— 速度曲线单峰。匀速直线之所以假，一半原因就是缺这个。

    :param t: 归一化进度，超出 ``[0, 1]`` 会先夹进区间
    :returns: 已走过的弧长比例，单调递增，``ease(0) == 0``、``ease(1) == 1``
    """
    return 0.5 * (1.0 - cos(pi * min(max(t, 0.0), 1.0)))


#: 弧长重参数化时的内部采样密度
_ARC_RESOLUTION = 256


def _cubic(
    p0: tuple[float, float],
    p1: tuple[float, float],
    p2: tuple[float, float],
    p3: tuple[float, float],
    t: float,
) -> tuple[float, float]:
    u = 1.0 - t
    b0, b1, b2, b3 = u * u * u, 3 * u * u * t, 3 * u * t * t, t * t * t
    return (
        b0 * p0[0] + b1 * p1[0] + b2 * p2[0] + b3 * p3[0],
        b0 * p0[1] + b1 * p1[1] + b2 * p2[1] + b3 * p3[1],
    )


def bezier(
    start: tuple[float, float],
    end: tuple[float, float],
    n: int,
    *,
    rng: Random,
    bow: float,
) -> list[tuple[float, float]]:
    """三阶贝塞尔，两个控制点偏在直线的**同一侧**，按**弧长**等时重采样。

    同侧才会得到一条干净的弧；分居两侧会拧成 S 形，那不是手臂的运动方式。

    弧长重参数化不是可有可无的细节。把缓动直接作用在贝塞尔参数 ``t`` 上看着能跑，
    但 ``t`` 与弧长只在接近直线时才成比例；弓形一大，同样的 Δt 在曲线不同位置对应
    的实际位移差很多，速度包络就被扭出第二个峰。实测：bow=0.26 的 CAREFUL 直接把
    非 overshoot 移动的单峰率压到 0.66，而弓形最小的 FAST 有 0.99 —— 症状恰好随 bow
    单调，这就是它的指纹。

    :param start: 起点 ``(x, y)``，浮点
    :param end: 终点 ``(x, y)``，会被精确钉死，不受浮点误差影响
    :param n: 采样点数。``< 2`` 直接返回 ``[start, end]``
    :param rng: 随机源。决定弓形朝哪一侧、控制点偏多少
    :param bow: 弓形幅度，按路径长度的比例。0 = 直线
    :returns: ``n`` 个浮点坐标，按缓动后的弧长比例等分
    """
    if n < 2:
        return [start, end]

    x0, y0 = start
    x3, y3 = end
    dx, dy = x3 - x0, y3 - y0
    length = hypot(dx, dy)
    if length < 1e-9:
        return [start] * n

    # 单位法向量；符号随机，决定这次从哪边绕
    nx, ny = -dy / length, dx / length
    side = 1.0 if rng.random() < 0.5 else -1.0
    span = length * bow

    # 控制点沿路径分布在 ~1/3 和 ~2/3，横向偏移各自随机
    def ctrl(at: float) -> tuple[float, float]:
        jitter = rng.uniform(0.35, 1.0) * span * side
        along = at + rng.uniform(-0.08, 0.08)
        return (x0 + dx * along + nx * jitter, y0 + dy * along + ny * jitter)

    p0, p3 = (x0, y0), (x3, y3)
    p1, p2 = ctrl(1.0 / 3.0), ctrl(2.0 / 3.0)

    # 稠密采样一遍，建累积弧长表
    dense = [_cubic(p0, p1, p2, p3, i / (_ARC_RESOLUTION - 1)) for i in range(_ARC_RESOLUTION)]
    cumulative = [0.0]
    for a, b in pairwise(dense):
        cumulative.append(cumulative[-1] + hypot(b[0] - a[0], b[1] - a[1]))
    total = cumulative[-1]
    if total < 1e-9:
        return [start] * n

    # 缓动作用在**弧长比例**上，于是等时采样得到的位移就是真正的速度曲线
    points: list[tuple[float, float]] = []
    cursor = 0
    for i in range(n):
        target = ease(i / (n - 1)) * total
        while cursor < len(cumulative) - 2 and cumulative[cursor + 1] < target:
            cursor += 1
        lo, hi = cumulative[cursor], cumulative[cursor + 1]
        frac = 0.0 if hi <= lo else (target - lo) / (hi - lo)
        a, b = dense[cursor], dense[cursor + 1]
        points.append((a[0] + (b[0] - a[0]) * frac, a[1] + (b[1] - a[1]) * frac))
    points[-1] = p3          # 钉死终点，浮点误差不许把落点推走
    return points


def landing_point(box: Box, *, rng: Random, profile: HumanProfile) -> Point:
    """box 内的落点：截断高斯，内缩自适应。

    固定 ``inset=3`` 对 4×4 px 的元素会算出空区间，所以内缩按元素尺寸收窄。
    截断采样保证落点一定可点，同时保留"不点正中心"的分布特征。

    :param box: 目标元素的几何，viewport CSS 像素
    :param rng: 随机源
    :param profile: 取 ``landing_sigma``（相对半宽的标准差）和 ``edge_inset``（内缩像素）
    :returns: 一定落在元素**自己**占据的整数像素区间内的点
    """
    def axis(origin: float, size: float) -> int:
        # 元素占据的整数像素是 [ceil(origin), ceil(origin+size)-1]。origin+size 本身是
        # **下一个元素的第一个像素** —— 落在那里会点到邻居身上，而 require_hit 只会
        # 报一句莫名其妙的"被遮挡"。
        first, last = ceil(origin), ceil(origin + size) - 1
        if last < first:                   # 元素窄到不占满一个像素
            return first

        inset = min(profile.edge_inset, floor(size / 4.0))
        lo, hi = origin + inset, origin + size - inset
        if hi <= lo:                       # 小到没有内缩余地
            return min(max(round(origin + size / 2.0), first), last)

        mid = (lo + hi) / 2.0
        sigma = max((hi - lo) / 2.0 * profile.landing_sigma, 1e-6)
        for _ in range(8):                 # 截断：越界就重采，别夹到边上堆积
            v = rng.gauss(mid, sigma)
            if lo <= v <= hi:
                return min(max(round(v), first), last)
        return min(max(round(rng.uniform(lo, hi)), first), last)

    return Point(axis(box.x, box.w), axis(box.y, box.h))


def overshoot_point(
    start: Point,
    target: Point,
    *,
    rng: Random,
    profile: HumanProfile,
    viewport: tuple[int, int] | None = None,
) -> Point | None:
    """越过目标的那个点；距离不够则返回 None。

    人不会为 20 px 的移动冲过头，所以短距离不触发（``overshoot_threshold``）。

    :param start: 起点
    :param target: 真正的落点
    :param rng: 随机源。同时决定"这次要不要冲过头"和冲多远
    :param profile: 取 ``overshoot``（幅度系数，0 = 关闭）、``overshoot_threshold``
        （触发下限，px）和 ``overshoot_ramp``（概率渐变区间）
    :param viewport: 给了就把冲过头的点夹回视口内。贴边元素（页脚按钮、右上角关闭叉）
        本来会让整段冲程跑到窗口外面 —— 那些 ``mouseMoved`` 会被 Chrome 在 hit test
        阶段整个丢掉，页面根本收不到。代价不是被识别，而是**白白付掉 step_delay 的
        sleep，并且落点附近最有信息量的减速和回修细节被截断**。夹的是**冲过头那个点**
        而不是逐点夹取：后者会沿着窗口边缘造出一段轴对齐直线，那才是真没有的形态。
    :returns: 越过目标的那个点；距离不够、``overshoot=0``、或概率未命中时返回 ``None``
    """
    if profile.overshoot <= 0.0:
        return None
    dx, dy = target.x - start.x, target.y - start.y
    dist = hypot(dx, dy)
    if dist < profile.overshoot_threshold or dist < 1.0:
        return None

    # **概率随距离渐变，不是过了阈值就必然发生。**
    #
    # 硬阈值会在恰好 threshold 处造出一道速度悬崖：阈值下方路径就是直线距离，上方
    # 突然长出 10%–27% 的冲程，而 Fitts 时长只按 log2 增长、补不回来，于是平均速度
    # 在一个固定像素值处阶跃约 25%。把「移动距离」对「平均速度」画出来，那道台阶
    # 是个干净利落的指纹。真人也不是过了某个距离就一定冲过头 —— 距离越远越容易而已。
    span = profile.overshoot_threshold * profile.overshoot_ramp
    if span > 0 and rng.random() > min((dist - profile.overshoot_threshold) / span, 1.0):
        return None

    # 幅度随距离缩放，再加一点横向漂移 —— 冲过头很少是正对着冲的
    magnitude = profile.overshoot * rng.uniform(0.04, 0.11) * dist
    ux, uy = dx / dist, dy / dist
    lateral = rng.uniform(-0.4, 0.4) * magnitude
    x = round(target.x + ux * magnitude - uy * lateral)
    y = round(target.y + uy * magnitude + ux * lateral)

    if viewport is not None:
        w, h = viewport
        x = min(max(x, 0), max(w - 1, 0))
        y = min(max(y, 0), max(h - 1, 0))
        if Point(x, y) == target:      # 夹完就是目标本身，那就等于没冲过头
            return None
    return Point(x, y)


def mouse_path(
    start: Point,
    target: Box | Point,
    *,
    rng: Random,
    profile: HumanProfile,
    viewport: tuple[int, int] | None = None,
) -> list[Point]:
    """从 ``start`` 到目标的完整整数轨迹 —— 这个模块的入口。

    贝塞尔定宏观形状，Fitts 定点数，WindMouse 风力项定微观抖动，overshoot 单独接
    一段回修，最后量化成整数像素并去掉连续重复点。

    :param start: 光标当前位置
    :param target: 目标。给 :class:`~sleight.core.types.Box` 会在里面采一个落点；
        给 :class:`~sleight.core.types.Point` 则精确到这个点
    :param rng: 随机源。同一个 seed 得到同一条轨迹
    :param profile: 拟人参数，见 :class:`~sleight.core.human.presets.HumanProfile`
    :param viewport: ``(宽, 高)``，CSS 像素。传了就把 overshoot 夹在窗口内，
        见 :func:`overshoot_point`
    :returns: 至少一个点的整数坐标列表，最后一个元素**一定**是落点
    """
    from .timing import point_count  # 局部导入，避免 curves ↔ timing 成环

    landing = (
        landing_point(target, rng=rng, profile=profile) if isinstance(target, Box) else target
    )
    width = min(target.w, target.h) if isinstance(target, Box) else profile.min_target_width
    distance = hypot(landing.x - start.x, landing.y - start.y)
    if distance < 1.0:
        return [landing]

    over = overshoot_point(start, landing, rng=rng, profile=profile, viewport=viewport)

    # 时间预算按**实际要走的路**算。原来用的是 start→landing 的直线距离，可 overshoot
    # 的路径长 10%–27%，点数却一样多 —— 于是每点位移变大，速度在 overshoot_threshold
    # 处出现约 26% 的阶跃，而且方向和 Meyer 的子运动模型相反（带修正动作的移动反而更快）。
    travelled = (
        distance
        if over is None
        else hypot(over.x - start.x, over.y - start.y) + hypot(landing.x - over.x, landing.y - over.y)
    )
    total = point_count(travelled, width, profile=profile)

    raw: list[tuple[float, float]]
    if over is None:
        raw = bezier((start.x, start.y), (landing.x, landing.y), total, rng=rng, bow=profile.bow)
    else:
        # 冲过头那段占大头，回修段短而快
        n_out = max(2, int(total * 0.75))
        n_back = max(2, total - n_out)
        raw = bezier((start.x, start.y), (over.x, over.y), n_out, rng=rng, bow=profile.bow)
        raw += bezier(
            (over.x, over.y), (landing.x, landing.y), n_back, rng=rng, bow=profile.bow * 0.5
        )[1:]

    jitter = wind_offsets(len(raw), rng=rng, magnitude=profile.wind)
    # 兜底夹取：overshoot 点已经夹过了，但弓形和抖动仍可能把中间几个点顶出窗口。
    # 只剩几个点、每个越界几像素，夹住不会造出贴边直线段 —— 而真实光标撞到边缘
    # 本来就会停成一小段平台。
    clamp = _clamper(viewport)
    out: list[Point] = []
    for (px, py), (jx, jy) in zip(raw, jitter, strict=True):
        point = clamp(Point(round(px + jx), round(py + jy)))
        if not out or point != out[-1]:      # 真实鼠标不上报没动过的位置
            out.append(point)

    if out[-1] != landing:                    # taper 之后仍可能差 1px，钉死终点
        out.append(landing)
    return out


def _clamper(viewport: tuple[int, int] | None):
    if viewport is None:
        return lambda p: p
    w, h = max(viewport[0] - 1, 0), max(viewport[1] - 1, 0)
    return lambda p: (
        p if 0 <= p.x <= w and 0 <= p.y <= h
        else Point(min(max(p.x, 0), w), min(max(p.y, 0), h))
    )
