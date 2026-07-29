"""拟人算法层：轨迹、抖动、Fitts、打字节奏、滚动分步。

统计断言一律**跨多组 seed**。单条固定 seed 的严格断言会被抖动天然打破，那种测试
不是在保护性质，是在随机 flake。
"""

from __future__ import annotations

from itertools import pairwise
from math import ceil, hypot
from random import Random
from statistics import mean, pstdev

import pytest

from sleight.core.human.curves import bezier, ease, landing_point, mouse_path, overshoot_point
from sleight.core.human.presets import CAREFUL, DEFAULT, FAST, HumanProfile
from sleight.core.human.timing import (
    DIGRAPH_MS,
    digraph_delay,
    move_duration,
    point_count,
    scroll_steps,
    type_delays,
)
from sleight.core.human.wind import TREMOR_DECAY, wind_mouse, wind_offsets
from sleight.core.keymap import Digraph
from sleight.core.types import Box, Point

SEEDS = range(120)


# --------------------------------------------------------------------------- #
# 统计工具
# --------------------------------------------------------------------------- #


def speeds(path: list[Point]) -> list[float]:
    return [hypot(b.x - a.x, b.y - a.y) for a, b in pairwise(path)]


def smooth(xs: list[float], k: int = 7) -> list[float]:
    if len(xs) < k:
        return xs
    return [mean(xs[max(0, i - k // 2) : i + k // 2 + 1]) for i in range(len(xs))]


def peak_count(xs: list[float]) -> int:
    return sum(1 for i in range(1, len(xs) - 1) if xs[i] > xs[i - 1] and xs[i] >= xs[i + 1])


def max_deviation_from_line(path: list[Point]) -> float:
    (x0, y0), (x1, y1) = (path[0].x, path[0].y), (path[-1].x, path[-1].y)
    dx, dy = x1 - x0, y1 - y0
    norm = hypot(dx, dy) or 1.0
    return max(abs((p.x - x0) * dy - (p.y - y0) * dx) / norm for p in path)


def autocorr(xs: list[float], lag: int = 1) -> float:
    m = mean(xs)
    num = sum((xs[i] - m) * (xs[i + lag] - m) for i in range(len(xs) - lag))
    den = sum((x - m) ** 2 for x in xs)
    return num / den if den else 0.0


NEAR = Box(x=200.0, y=150.0, w=120.0, h=40.0)     # 约 219 px，不触发 overshoot
FAR = Box(x=600.0, y=400.0, w=120.0, h=40.0)      # 约 749 px，触发 overshoot
START_NEAR = Point(60, 80)
START_FAR = Point(20, 30)


def paths(profile: HumanProfile, start: Point, box: Box) -> list[list[Point]]:
    return [mouse_path(start, box, rng=Random(s), profile=profile) for s in SEEDS]


# --------------------------------------------------------------------------- #
# 轨迹
# --------------------------------------------------------------------------- #


def test_path_is_not_a_straight_line():
    for profile in (FAST, DEFAULT, CAREFUL):
        devs = sorted(max_deviation_from_line(p) for p in paths(profile, START_NEAR, NEAR))
        assert devs[len(devs) // 10] > 3.0, f"{profile.bow=} produced near-straight paths"


def test_coordinates_are_integers():
    """真实鼠标是整数像素。用小数位当"抖动"是一眼可辨的伪造。"""
    for p in paths(DEFAULT, START_NEAR, NEAR):
        assert all(type(pt.x) is int and type(pt.y) is int for pt in p)


def test_no_duplicate_consecutive_points():
    """真实鼠标不上报没动过的位置。"""
    for p in paths(DEFAULT, START_NEAR, NEAR):
        assert all(a != b for a, b in pairwise(p))


@pytest.mark.parametrize(("profile", "floor"), [(FAST, 0.90), (DEFAULT, 0.85), (CAREFUL, 0.70)])
def test_speed_profile_is_unimodal_without_overshoot(profile: HumanProfile, floor: float):
    """加速 → 巡航 → 减速，单峰。

    阈值取实测值下方（FAST 1.00 / DEFAULT 0.95 / CAREFUL 0.84）。CAREFUL 偏低是因为
    它采样最密、步长最小，手抖相对更显著 —— 这是真实性质，不是缺陷。

    这条断言直接保护 :func:`bezier` 的**弧长重参数化**：把缓动作用在贝塞尔参数 t 上
    而不是弧长上，弓形一大就会扭出第二个峰，CAREFUL 会掉到 0.66。
    """
    fraction = mean(
        peak_count(smooth(speeds(p))) <= 1 for p in paths(profile, START_NEAR, NEAR)
    )
    assert fraction >= floor, f"only {fraction:.2f} unimodal"


def test_overshoot_produces_two_ballistic_phases():
    """冲过头再回修，速度曲线**应该**是双峰的 —— 那是两段独立的弹道式移动。"""
    counts = [peak_count(smooth(speeds(p))) for p in paths(DEFAULT, START_FAR, FAR)]
    assert mean(c >= 2 for c in counts) > 0.85


def test_short_moves_never_overshoot():
    """人不会为 20 px 的移动冲过头。"""
    start, target = Point(500, 500), Point(520, 505)
    for s in SEEDS:
        assert overshoot_point(start, target, rng=Random(s), profile=DEFAULT) is None


def test_overshoot_can_be_disabled():
    profile = DEFAULT.replace(overshoot=0.0)
    assert overshoot_point(
        Point(0, 0), Point(900, 900), rng=Random(1), profile=profile
    ) is None


def test_path_ends_exactly_on_the_landing_point():
    """抖动 taper 之后必须精确落在算好的点上，否则命中校验白做。"""
    for s in SEEDS:
        rng = Random(s)
        box = NEAR
        path = mouse_path(START_NEAR, box, rng=rng, profile=DEFAULT)
        last = path[-1]
        assert box.x <= last.x <= box.x + box.w
        assert box.y <= last.y <= box.y + box.h


def test_point_count_grows_from_fast_to_careful():
    """快 = 事件少。把 step_delay 调大来"变慢"会得到相反的结果。"""
    counts = [
        mean(len(p) for p in paths(profile, START_FAR, FAR))
        for profile in (FAST, DEFAULT, CAREFUL)
    ]
    assert counts[0] < counts[1] < counts[2], counts


def test_point_count_is_capped():
    huge = mouse_path(Point(0, 0), Point(100_000, 100_000), rng=Random(1), profile=CAREFUL)
    assert len(huge) <= CAREFUL.max_points + 2


def test_degenerate_geometry_does_not_explode():
    assert mouse_path(Point(5, 5), Point(5, 5), rng=Random(1), profile=DEFAULT) == [Point(5, 5)]
    tiny = mouse_path(Point(0, 0), Box(10.0, 10.0, 1.0, 1.0), rng=Random(1), profile=DEFAULT)
    assert len(tiny) >= 1


def _arc_fractions(points: list[tuple[float, float]]) -> list[float]:
    """折线上每个采样点的累计弧长占比。"""
    cum = [0.0]
    for a, b in pairwise(points):
        cum.append(cum[-1] + hypot(b[0] - a[0], b[1] - a[1]))
    total = cum[-1] or 1.0
    return [c / total for c in cum]


@pytest.mark.parametrize("bow", [0.05, 0.18, 0.35])
def test_bezier_samples_land_on_eased_arc_length_fractions(bow: float):
    """第 i 个采样点的累计**弧长**占比必须等于 ``ease(i/(n-1))``。

    这是直接断言性质本身。之前那版测的是中段线段长度的变异系数 —— 那个指标在退化时
    **朝错误的方向移动**（采样越退化 CV 越低），所以它对它自己声称保护的回归完全免疫。
    弓形越大越能区分：ease-on-t 在 bow=0.35 上误差会大一个量级。
    """
    n = 60
    points = bezier((0.0, 0.0), (400.0, 0.0), n, rng=Random(3), bow=bow)
    actual = _arc_fractions(points)
    expected = [ease(i / (n - 1)) for i in range(n)]
    worst = max(abs(a - e) for a, e in zip(actual, expected, strict=True))
    assert worst < 0.02, f"arc-length reparameterisation is off by {worst:.3f} at bow={bow}"


def test_ease_is_monotonic_and_bounded():
    values = [ease(i / 50) for i in range(51)]
    assert values[0] == pytest.approx(0.0) and values[-1] == pytest.approx(1.0)
    assert all(a <= b + 1e-12 for a, b in pairwise(values))


# --------------------------------------------------------------------------- #
# 落点
# --------------------------------------------------------------------------- #


def test_landing_is_dispersed_and_never_dead_centre():
    box = Box(x=100.0, y=100.0, w=120.0, h=40.0)
    pts = [landing_point(box, rng=Random(s), profile=DEFAULT) for s in SEEDS]
    assert pstdev([p.x for p in pts]) > 4.0
    assert mean(p == box.center for p in pts) < 0.15


def test_landing_always_inside_the_box():
    for w, h in ((120.0, 40.0), (10.0, 10.0), (4.0, 4.0), (1.0, 1.0)):
        box = Box(x=50.0, y=60.0, w=w, h=h)
        for s in range(60):
            p = landing_point(box, rng=Random(s), profile=DEFAULT)
            assert box.x <= p.x <= box.x + box.w, (w, h, p)
            assert box.y <= p.y <= box.y + box.h, (w, h, p)


def test_tiny_elements_still_get_a_landing_point():
    """固定 inset=3 会给 4x4 的元素算出空区间；内缩必须自适应。"""
    assert landing_point(Box(0.0, 0.0, 4.0, 4.0), rng=Random(1), profile=DEFAULT) is not None


def test_landing_never_lands_on_the_next_element_first_pixel():
    """``origin + size`` 是**邻居的第一个像素**，落在那里就点到别人身上了。

    元素占据的整数像素是 ``[ceil(origin), ceil(origin+size)-1]``。≤3px 的元素 inset
    会算成 0，截断区间的上界是闭的，round 就能吐出正好 origin+size。
    """
    for w in (1.0, 2.0, 3.0, 3.9, 4.0):
        for origin in (0.0, 10.0, 10.5, 10.9):
            box = Box(origin, origin, w, w)
            last = ceil(origin + w) - 1
            for s in range(80):
                p = landing_point(box, rng=Random(s), profile=DEFAULT)
                assert ceil(origin) <= p.x <= last, (origin, w, p.x)
                assert ceil(origin) <= p.y <= last, (origin, w, p.y)


def test_overshoot_is_clamped_into_the_viewport():
    """贴边元素的冲程不该跑到窗口外面。

    越界的 mouseMoved 会被 Chrome 在 hit test 阶段整个丢掉 —— 页面收不到，但我们照样
    为每一个付掉 step_delay 的 sleep，而且落点附近最有信息量的减速与回修被截断。
    """
    viewport = (1280, 720)
    corner = Box(1150.0, 690.0, 110.0, 24.0)          # 右下角页脚链接
    for profile in (DEFAULT, CAREFUL):
        outside = 0
        for s in range(200):
            path = mouse_path(Point(60, 60), corner, rng=Random(s), profile=profile,
                              viewport=viewport)
            if any(not (0 <= p.x < viewport[0] and 0 <= p.y < viewport[1]) for p in path):
                outside += 1
        assert outside == 0, f"{profile.overshoot=} left the viewport in {outside}/200 paths"


def test_without_a_viewport_nothing_is_clamped():
    """纯算法层不该凭空假设一个视口尺寸。"""
    corner = Box(1150.0, 690.0, 110.0, 24.0)
    escaped = any(
        any(p.y >= 720 for p in mouse_path(Point(60, 60), corner, rng=Random(s), profile=DEFAULT))
        for s in range(200)
    )
    assert escaped, "clamping must be opt-in via the viewport argument"


def test_overshoot_does_not_cause_a_speed_jump_at_the_threshold():
    """带回修子运动的移动**不该更快** —— 那与 Meyer 的子运动模型正好相反。

    时间预算要按实际走过的弧长算，而不是 start→landing 的直线距离。
    """
    def mean_speed(distance: float) -> float:
        speeds_seen = []
        for s in range(300):
            target = Box(distance / 2**0.5, distance / 2**0.5, 8.0, 8.0)
            path = mouse_path(Point(0, 0), target, rng=Random(s), profile=DEFAULT)
            arc = sum(hypot(b.x - a.x, b.y - a.y) for a, b in pairwise(path))
            speeds_seen.append(arc / max(len(path) - 1, 1))     # px per sample
        return mean(speeds_seen)

    below = mean_speed(DEFAULT.overshoot_threshold - 20)
    above = mean_speed(DEFAULT.overshoot_threshold + 20)
    assert abs(above - below) / below < 0.12, (
        f"speed steps {below:.1f} -> {above:.1f} px/sample across the overshoot threshold"
    )


# --------------------------------------------------------------------------- #
# 抖动
# --------------------------------------------------------------------------- #


def test_tremor_is_correlated_not_white_noise():
    """每点独立采样的白噪声在功率谱上一眼可辨，不像手抖。"""
    tremor = [
        autocorr([x for x, _ in wind_offsets(200, rng=Random(s), magnitude=3.0, taper=False)])
        for s in range(40)
    ]
    white = [autocorr([Random(s * 7 + i).uniform(-1, 1) for i in range(200)]) for s in range(40)]
    assert mean(tremor) > 0.55, mean(tremor)
    assert mean(white) < 0.15
    assert min(tremor) > max(white)


def test_tremor_amplitude_is_independent_of_decay():
    """调 decay 只改频率，不改幅度 —— 归一化补偿必须生效。"""
    def amplitude(decay: float) -> float:
        return pstdev(
            [x for s in range(30)
             for x, _ in wind_offsets(150, rng=Random(s), magnitude=3.0, decay=decay, taper=False)]
        )

    assert amplitude(0.75) == pytest.approx(amplitude(0.577), rel=0.15)


def test_tremor_tapers_to_zero():
    offsets = wind_offsets(50, rng=Random(2), magnitude=4.0, taper=True)
    assert offsets[-1] == (0.0, 0.0)


def test_zero_wind_means_no_tremor():
    assert wind_offsets(20, rng=Random(1), magnitude=0.0) == [(0.0, 0.0)] * 20


def test_wind_mouse_converges_and_terminates():
    """原版算法保留作参照物。参数被调坏时也不许挂死。"""
    pts = list(wind_mouse(0, 0, 400, 300, rng=Random(5)))
    assert pts and hypot(pts[-1][0] - 400, pts[-1][1] - 300) < 5
    assert len(list(wind_mouse(0, 0, 400, 300, rng=Random(5), gravity=0.0))) <= 10_000


# --------------------------------------------------------------------------- #
# Fitts
# --------------------------------------------------------------------------- #


def test_fitts_boundaries_do_not_blow_up():
    """Shannon 形式的存在理由：log2(2D/W) 在这三处分别是 -inf、负数、除零。"""
    assert move_duration(0.0, 100.0, profile=DEFAULT) >= DEFAULT.min_duration
    assert move_duration(500.0, 0.0, profile=DEFAULT) > 0
    assert move_duration(10.0, 500.0, profile=DEFAULT) >= DEFAULT.min_duration


def test_fitts_is_monotonic_in_distance_and_target_size():
    assert move_duration(800, 20, profile=DEFAULT) > move_duration(100, 20, profile=DEFAULT)
    assert move_duration(400, 20, profile=DEFAULT) > move_duration(400, 300, profile=DEFAULT)


def test_far_small_target_needs_more_points_than_near_big():
    far = [len(mouse_path(Point(0, 0), Box(800, 600, 20, 20), rng=Random(s), profile=DEFAULT))
           for s in SEEDS]
    near = [len(mouse_path(Point(0, 0), Box(100, 100, 300, 80), rng=Random(s), profile=DEFAULT))
            for s in SEEDS]
    assert sum(a <= b for a, b in zip(far, near, strict=True)) == 0


def test_duration_is_clamped():
    assert move_duration(10**9, 1.0, profile=DEFAULT) <= DEFAULT.max_duration
    assert point_count(10**9, 1.0, profile=DEFAULT) <= DEFAULT.max_points


# --------------------------------------------------------------------------- #
# 打字
# --------------------------------------------------------------------------- #


def test_digraph_ordering_matches_the_research():
    """异手 < 同手异指 < 同指。这是击键动力学最稳的分层。"""
    assert DIGRAPH_MS[Digraph.ALTERNATE][0] < DIGRAPH_MS[Digraph.SAME_HAND][0]
    assert DIGRAPH_MS[Digraph.SAME_HAND][0] < DIGRAPH_MS[Digraph.SAME_FINGER][0]


def test_alternating_hands_type_faster_than_same_finger():
    alt = mean(mean(type_delays("thth", rng=Random(s), profile=DEFAULT)[1:]) for s in SEEDS)
    same = mean(mean(type_delays("dede", rng=Random(s), profile=DEFAULT)[1:]) for s in SEEDS)
    assert alt < same, (alt, same)


def test_delays_stay_inside_the_measured_envelope():
    for digraph, (_, lo, hi) in DIGRAPH_MS.items():
        if digraph is Digraph.UNKNOWN:
            continue
        pair = {"alternate": "th", "same_hand": "er", "same_finger": "de", "double": "ll"}[digraph]
        for s in range(60):
            d = digraph_delay(pair[0], pair[1], rng=Random(s), profile=DEFAULT) * 1000
            assert lo - 1e-6 <= d <= hi + 1e-6, (digraph, d)


def test_fast_preset_lowers_both_mean_and_variance():
    """熟练度体现在**方差**而不只是均值。只调快不收窄方差反而更像机器。"""
    text = "the quick brown fox jumps over the lazy dog"
    fast = [d for s in range(40) for d in type_delays(text, rng=Random(s), profile=FAST)[1:]]
    slow = [d for s in range(40) for d in type_delays(text, rng=Random(s), profile=CAREFUL)[1:]]
    assert mean(fast) < mean(slow)
    assert pstdev(fast) < pstdev(slow)


def test_first_character_has_no_leading_delay():
    assert type_delays("hello", rng=Random(1), profile=DEFAULT)[0] == 0.0


def test_empty_text_yields_no_delays():
    assert type_delays("", rng=Random(1), profile=DEFAULT) == []


#: 任何单纯的 digraph 间隔都不可能超过这个值，超过就一定含思考停顿
_MAX_DIGRAPH = max(hi for _, _, hi in DIGRAPH_MS.values()) / 1000.0


def _pause_rate(text: str, profile: HumanProfile, samples: int = 20) -> float:
    delays = [
        d for s in range(samples) for d in type_delays(text, rng=Random(s), profile=profile)[1:]
    ]
    return sum(d > _MAX_DIGRAPH for d in delays) / len(delays)


def test_think_pauses_are_actually_injected():
    """判据必须高于**任何** digraph 的上界，否则普通击键会冒充思考停顿。

    旧版用 ``think_pause[0]``（0.20 s）当阈值，而 DOUBLE 类的间隔上界是 0.24 s ——
    于是把整段注入逻辑删掉测试照样绿。
    """
    rate = _pause_rate("a" * 400, DEFAULT)
    lo, hi = DEFAULT.think_every
    assert 0.4 / hi < rate < 2.5 / lo, rate

    # 关掉注入必须归零 —— 这条让"删掉 think pause"的变异必挂
    assert _pause_rate("a" * 400, DEFAULT.replace(think_every=(10**6, 10**6))) == 0.0


def test_punctuation_raises_the_pause_rate():
    """人在句读处停顿，不在词中间。"""
    plain = _pause_rate("aaaa" * 100, DEFAULT)
    punct = _pause_rate("a.a." * 100, DEFAULT)
    assert punct > plain * 1.3, (plain, punct)


def test_tremor_decay_is_pinned_by_its_autocorrelation():
    """常数本身要有护栏。

    审计发现把 TREMOR_DECAY 改回 WindMouse 原版的 1/√3 时全部测试照绿 —— 也就是说
    这个决定完全没有被保护。断言自相关落在一个原版够不到的带里。
    """
    def mean_autocorr(decay: float) -> float:
        return mean(
            autocorr([x for x, _ in wind_offsets(200, rng=Random(s), magnitude=3.0,
                                                 decay=decay, taper=False)])
            for s in range(40)
        )

    shipped = mean_autocorr(TREMOR_DECAY)
    original = mean_autocorr(1.0 / 3.0**0.5)
    assert 0.65 < shipped < 0.85, shipped
    assert original < 0.65, original          # 改回原版会掉出带外，测试变红


# --------------------------------------------------------------------------- #
# 滚动
# --------------------------------------------------------------------------- #


def test_scroll_is_broken_into_wheel_sized_steps():
    steps = scroll_steps(1000, rng=Random(1), profile=DEFAULT)
    assert sum(steps) == 1000
    assert len(steps) > 5, "one giant deltaY is an obvious signature"
    assert all(abs(s) <= DEFAULT.scroll_step[1] for s in steps)


def test_scroll_preserves_direction_and_handles_zero():
    assert all(s < 0 for s in scroll_steps(-500, rng=Random(1), profile=DEFAULT))
    assert scroll_steps(0, rng=Random(1), profile=DEFAULT) == []
    assert sum(scroll_steps(-500, rng=Random(1), profile=DEFAULT)) == -500


def test_tiny_scroll_still_moves():
    assert sum(scroll_steps(3, rng=Random(1), profile=DEFAULT)) == 3


# --------------------------------------------------------------------------- #
# HumanProfile
# --------------------------------------------------------------------------- #


def test_profile_rejects_inverted_ranges():
    with pytest.raises(ValueError, match="low bound"):
        HumanProfile(step_delay=(0.05, 0.01))
    with pytest.raises(ValueError, match="min_duration"):
        HumanProfile(min_duration=5.0, max_duration=1.0)


def test_replace_returns_a_new_profile():
    tweaked = DEFAULT.replace(overshoot=0.0)
    assert DEFAULT.overshoot == 1.0 and tweaked.overshoot == 0.0
