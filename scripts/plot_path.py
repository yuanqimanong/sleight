#!/usr/bin/env python
"""把生成的轨迹画成 PNG。**调参时肉眼看图**是唯一靠谱的办法。

    python scripts/plot_path.py                       # 三档预设对比
    python scripts/plot_path.py --profile CAREFUL -n 12
    python scripts/plot_path.py --distance 900        # 强制触发 overshoot
    python scripts/plot_path.py --tweak wind=0 --tweak bow=0.4

matplotlib 只在 dev extras 里：``pip install -e ".[dev]"``。
"""

from __future__ import annotations

import argparse
import sys
from itertools import pairwise
from math import hypot
from pathlib import Path
from random import Random
from statistics import mean

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sleight.core.human.curves import mouse_path
from sleight.core.human.presets import CAREFUL, DEFAULT, FAST, HumanProfile
from sleight.core.human.timing import type_delays
from sleight.core.types import Box, Point

PRESETS = {"FAST": FAST, "DEFAULT": DEFAULT, "CAREFUL": CAREFUL}


def smooth(xs: list[float], k: int = 7) -> list[float]:
    if len(xs) < k:
        return xs
    return [mean(xs[max(0, i - k // 2) : i + k // 2 + 1]) for i in range(len(xs))]


def speeds(path: list[Point]) -> list[float]:
    return [hypot(b.x - a.x, b.y - a.y) for a, b in pairwise(path)]


def apply_tweaks(profile: HumanProfile, tweaks: list[str]) -> HumanProfile:
    changes: dict[str, object] = {}
    for raw in tweaks:
        key, _, value = raw.partition("=")
        if not _:
            raise SystemExit(f"--tweak needs key=value, got {raw!r}")
        current = getattr(profile, key, None)
        if current is None and not hasattr(profile, key):
            raise SystemExit(f"HumanProfile has no field {key!r}")
        changes[key] = int(value) if isinstance(current, int) else float(value)
    return profile.replace(**changes) if changes else profile


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--profile", choices=[*PRESETS, "ALL"], default="ALL")
    ap.add_argument("-n", "--samples", type=int, default=8, help="每档画几条轨迹")
    ap.add_argument("--distance", type=float, default=420.0, help="到目标中心的距离，px")
    ap.add_argument("--width", type=float, default=120.0, help="目标宽度，px")
    ap.add_argument("--height", type=float, default=40.0, help="目标高度，px")
    ap.add_argument("--seed", type=int, default=0)
    ap.add_argument("--tweak", action="append", default=[], metavar="FIELD=VALUE")
    ap.add_argument("-o", "--out", default="trajectory.png")
    args = ap.parse_args()

    try:
        import matplotlib

        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
    except ImportError:
        raise SystemExit('matplotlib is a dev dependency: pip install -e ".[dev]"') from None

    names = list(PRESETS) if args.profile == "ALL" else [args.profile]
    start = Point(60, 60)
    # 目标放在与 start 成 45° 的方向上，斜向移动最能暴露轨迹问题
    offset = args.distance / (2**0.5)
    box = Box(
        x=start.x + offset - args.width / 2,
        y=start.y + offset - args.height / 2,
        w=args.width,
        h=args.height,
    )

    fig, axes = plt.subplots(2, len(names), figsize=(6 * len(names), 9), squeeze=False)

    for col, name in enumerate(names):
        profile = apply_tweaks(PRESETS[name], args.tweak)
        top, bottom = axes[0][col], axes[1][col]

        counts, landings = [], []
        for i in range(args.samples):
            path = mouse_path(start, box, rng=Random(args.seed + i), profile=profile)
            counts.append(len(path))
            landings.append(path[-1])
            top.plot([p.x for p in path], [p.y for p in path], lw=1.0, alpha=0.75)
            top.plot([p.x for p in path], [p.y for p in path], ".", ms=2.2, alpha=0.45)
            bottom.plot(smooth(speeds(path)), lw=1.0, alpha=0.75)

        # 直线参照：偏离它多远就是弓形有多大
        top.plot([start.x, box.center.x], [start.y, box.center.y], "k--", lw=0.8, alpha=0.4,
                 label="straight line")
        top.add_patch(
            plt.Rectangle((box.x, box.y), box.w, box.h, fill=False, ec="crimson", lw=1.4)
        )
        top.plot(*zip(*[(p.x, p.y) for p in landings], strict=True), "x", color="crimson", ms=7,
                 label="landings")
        top.plot(box.center.x, box.center.y, "+", color="black", ms=10, label="dead centre")

        top.set_title(
            f"{name}  ·  {mean(counts):.0f} pts avg  ·  "
            f"{'overshoot' if args.distance >= profile.overshoot_threshold else 'no overshoot'}"
        )
        top.invert_yaxis()          # 屏幕坐标：y 向下
        top.set_aspect("equal")
        top.legend(fontsize=7, loc="lower right")
        top.grid(alpha=0.2)

        bottom.set_title("speed (smoothed) — should rise then fall, once")
        bottom.set_xlabel("sample #")
        bottom.set_ylabel("px per step")
        bottom.grid(alpha=0.2)

    fig.suptitle(
        f"sleight trajectories · distance {args.distance:.0f}px · target {args.width:.0f}×{args.height:.0f}"
        + (f" · {' '.join(args.tweak)}" if args.tweak else ""),
        fontsize=11,
    )
    fig.tight_layout()
    fig.savefig(args.out, dpi=130)
    print(f"wrote {args.out}")

    # 打字节奏顺带也看一眼 —— 一条平坦的线说明 digraph 分层没生效
    sample = "the quick brown fox"
    delays = type_delays(sample, rng=Random(args.seed), profile=PRESETS[names[0]])
    print(f"\ntyping rhythm for {sample!r} ({names[0]}):")
    for ch, d in zip(sample, delays, strict=True):
        print(f"  {ch!r:5} {d * 1000:6.1f} ms {'#' * int(d * 300)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
