"""HumanProfile 与三档预设。

frozen dataclass，可安全跨线程共享；``replace()`` 返回新实例。
"""

from __future__ import annotations

import dataclasses
from dataclasses import dataclass
from typing import Any

__all__ = ["CAREFUL", "DEFAULT", "FAST", "HumanProfile"]


@dataclass(frozen=True, slots=True)
class HumanProfile:
    """一整套拟人参数。

    frozen dataclass —— 可以安全地跨线程共享，改参数用 :meth:`replace`。
    每个字段的取值依据写在它自己的文档串里。
    """

    # —— 轨迹 ——
    step_delay: tuple[float, float] = (0.008, 0.020)
    """每个轨迹点后的停顿，也就是鼠标的**上报间隔**。

    **本地 sleep 才是拟人节奏的来源** —— 远程链路的 RTT 恰好被它掩盖掉。

    这个值对应硬件回报率（8–20 ms ≈ 50–125 Hz），各预设之间只做小幅浮动 —— 快慢由
    :attr:`fitts_b` 的时长控制，不是靠稀疏采样。把它调大来"变慢"会得到一个反直觉的
    结果：谨慎模式发出的事件反而比快速模式**少**，轨迹更粗糙也更容易被识别。
    """

    wind: float = 3.0
    """WindMouse 风力 W₀。0 = 纯贝塞尔，没有手抖。

    手抖幅度在**空间上**大致固定（1–2 px 量级），不随"谨慎程度"放大 —— 谨慎的人动作
    更慢，不是更抖。各预设之间只做小幅浮动，代表不同的人和状态。调得过大会淹掉速度
    包络：CAREFUL 采样最密，步长最小，对这个值最敏感。
    """

    gravity: float = 9.0
    """WindMouse 引力 G₀。仅 :func:`wind_mouse` 用；抖动叠加模式不用。"""

    bow: float = 0.18
    """贝塞尔控制点的横向偏移，按路径长度的比例。0 = 直线。"""

    max_points: int = 220
    """点数上限。跨屏拖拽也不该发出上千个事件。"""

    # —— Fitts（Shannon 形式）——
    fitts_a: float = 0.10
    fitts_b: float = 0.10
    min_target_width: float = 8.0
    """Fitts 的 W 下限，防除零。"""
    min_duration: float = 0.05
    max_duration: float = 2.50

    # —— overshoot ——
    overshoot: float = 1.0
    """幅度系数。0 = 关闭。"""
    overshoot_threshold: float = 500.0
    """px。短距离不 overshoot —— 人不会为 20px 的移动冲过头。"""

    overshoot_ramp: float = 0.5
    """概率从 0 涨到 1 的区间，按 ``overshoot_threshold`` 的倍数算。

    0 = 硬阈值（过了就必然发生）。**不要设成 0**：那会在恰好 threshold 处造出一道约
    25% 的平均速度阶跃，把「距离 → 速度」画出来就是一道干净的台阶。
    """

    # —— 落点 ——
    landing_sigma: float = 0.25
    """相对 box 半宽的高斯标准差。"""
    edge_inset: int = 3
    """落点内缩像素，会按元素尺寸自适应收窄。"""

    # —— 按压 ——
    reaction: tuple[float, float] = (0.060, 0.180)
    """到位后按下前的反应时间。"""
    dwell: tuple[float, float] = (0.060, 0.150)
    """鼠标按压时长。"""

    inter_click: tuple[float, float] = (0.040, 0.120)
    """双击的两下之间。**不能复用 ``dwell``** —— 那是按住的时长，两下之间是另一个分布，
    而且必须落在浏览器的双击判定窗口内（Chrome 约 500 ms）。"""

    # —— 拖拽 ——
    drag_settle: tuple[float, float] = (0.080, 0.240)
    """拖到位之后、松手之前的迟滞。

    人放手前会先停一下确认位置。滑块验证码的风控明确盯这一段 —— **到位即松手**是
    机器最稳定的特征之一。和 ``dwell`` 是两回事：``dwell`` 挂在按下之后、轨迹开始
    之前，这个挂在轨迹结束之后、抬起之前。
    """

    drag_overshoot_threshold: float = 60.0
    """px。拖拽单列的过冲阈值，语义同 ``overshoot_threshold``。

    指针移动那档是 500 px —— 滑块总共才一两百像素宽，套那个阈值等于**永不过冲**。
    而人拖滑块几乎总是冲过头再拉回来；一条严格单调、终点恰好就是极值的轨迹，在
    滑块场景里反而是异常值。
    """

    # —— 打字 ——
    key_dwell: tuple[float, float] = (0.060, 0.120)
    """按下到抬起。独立于间隔的第二维特征，不要恒为 0。"""
    typing_scale: float = 1.0
    """击键间隔均值的乘数。"""
    typing_spread: float = 1.0
    """击键间隔标准差的乘数。**熟练度体现在方差**，FAST 要同时压低两者。"""
    think_every: tuple[int, int] = (8, 15)
    think_pause: tuple[float, float] = (0.20, 0.60)
    punctuation_think_bonus: float = 2.0
    """标点后插入思考停顿的概率倍数。"""

    # —— 滚动 ——
    scroll_step: tuple[int, int] = (80, 160)
    """单次 mouseWheel 的 deltaY。真实滚轮一格约 100 px。"""
    scroll_delay: tuple[float, float] = (0.030, 0.080)

    def replace(self, **kw: Any) -> HumanProfile:
        """派生一个改了若干字段的新 profile，原对象不变。

        :param kw: 任意字段名 → 新值，例如 ``DEFAULT.replace(overshoot=0)``
        :returns: 新的 :class:`HumanProfile`
        :raises TypeError: 传了不存在的字段名
        :raises ValueError: 改出了非法区间（区间下界大于上界等）
        """
        return dataclasses.replace(self, **kw)

    def __post_init__(self) -> None:
        for name in ("step_delay", "reaction", "dwell", "drag_settle", "key_dwell",
                     "think_pause", "scroll_delay", "scroll_step", "think_every"):
            lo, hi = getattr(self, name)
            if lo > hi:
                raise ValueError(f"HumanProfile.{name}: low bound {lo} exceeds high bound {hi}")
        if self.min_duration > self.max_duration:
            raise ValueError("HumanProfile: min_duration exceeds max_duration")
        if self.max_points < 2:
            raise ValueError("HumanProfile: max_points must be at least 2")


#: 抢时间。均值和方差同时压低 —— 只调快而不收窄方差反而更像机器
FAST = HumanProfile(
    step_delay=(0.006, 0.014),
    wind=1.5,
    bow=0.10,
    fitts_b=0.05,
    overshoot=0.3,
    drag_settle=(0.050, 0.140),
    key_dwell=(0.040, 0.075),
    typing_scale=0.70,
    typing_spread=0.60,
    think_every=(14, 26),
    think_pause=(0.10, 0.30),
    scroll_delay=(0.018, 0.045),
)

#: 日常默认
DEFAULT = HumanProfile()

#: 高风险页面：慢、抖、犹豫
CAREFUL = HumanProfile(
    step_delay=(0.010, 0.024),
    wind=3.5,
    bow=0.26,
    fitts_b=0.16,
    overshoot=1.4,
    overshoot_threshold=320.0,
    drag_overshoot_threshold=40.0,
    reaction=(0.120, 0.320),
    drag_settle=(0.150, 0.450),
    key_dwell=(0.070, 0.150),
    typing_scale=1.35,
    typing_spread=1.30,
    think_every=(5, 11),
    think_pause=(0.30, 0.90),
    scroll_delay=(0.050, 0.130),
)
