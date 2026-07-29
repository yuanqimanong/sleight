"""拟人算法。**纯函数，只依赖标准库** —— 不 import transport / provider / lease。

产物是 ``(method, params, sleep_after)`` 三元组序列，谁来发、发到哪它一概不知。
所以这一层可以不开浏览器单测、可以把轨迹画成 PNG 调参、也可以整套喂给 Playwright
的 CDPSession。
"""

from .curves import bezier, ease, landing_point, mouse_path, overshoot_point
from .engine import (
    BUTTONS,
    Event,
    chord_events,
    click_events,
    insert_text_event,
    key_events,
    move_events,
    press_events,
    release_events,
    scroll_events,
    type_events,
)
from .presets import CAREFUL, DEFAULT, FAST, HumanProfile
from .timing import (
    DIGRAPH_MS,
    digraph_delay,
    key_dwell,
    move_duration,
    point_count,
    scroll_steps,
    type_delays,
)
from .wind import wind_mouse, wind_offsets

__all__ = [
    "BUTTONS",
    "CAREFUL",
    "DEFAULT",
    "DIGRAPH_MS",
    "FAST",
    "Event",
    "HumanProfile",
    "bezier",
    "chord_events",
    "click_events",
    "digraph_delay",
    "ease",
    "insert_text_event",
    "key_dwell",
    "key_events",
    "landing_point",
    "mouse_path",
    "move_duration",
    "move_events",
    "overshoot_point",
    "point_count",
    "press_events",
    "release_events",
    "scroll_events",
    "scroll_steps",
    "type_delays",
    "type_events",
    "wind_mouse",
    "wind_offsets",
]
