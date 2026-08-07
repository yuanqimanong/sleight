# sleight

**Drive any CDP browser like a human.** Bezier trajectories with real hand tremor,
typing rhythm modelled on keystroke-dynamics research, and exclusive leasing for
browser instance pools.

Python ≥ 3.11 · one runtime dependency (`websocket-client`) · MIT

📖 **[中文文档手册](docs/详细文档手册.md)** — 安装、快速开始、实战场景、CloakBrowser Manager 部署
🚀 **[部署与运维 CLI](docs/部署与运维%20CLI.md)** — 一条命令把 Manager 部署到本机或远程

```bash
pip install sleight
```

## 30 seconds

```python
from sleight import connect, Text

with connect("http://127.0.0.1:9222") as s:      # opens its own tab, closes it on exit
    s.open("https://example.com", wait=Text("Example Domain"))
    print(s.title(), len(s.content()))
```

With a browser pool that has real profiles behind it:

```python
from sleight.providers import CloakBrowserManager

mgr = CloakBrowserManager("http://127.0.0.1:19000", token="…")

with mgr.lease() as inst:                    # exclusive lease, released on exit
    with inst.session(human=True) as s:      # every action gets a human trajectory
        s.open("https://example.com")
        s.click("#login")
        s.type("#email", "user@example.com")
        s.click("#submit", human=False)      # …except this one, speed matters here
```

See what the page actually loaded — the library gives you structured data, you decide
what to print:

```python
with s.capture_resources(types={"Script", "Stylesheet"}) as capture:
    s.open(url, wait=Load())
    s.pump_events(10)          # the async batch that arrives after `load`

for r in capture.snapshot():
    print(r.resource_type, r.status, r.url)
```

Target one specific profile — by id, by name, or by tag. A name that matches nothing
fails immediately with the visible names listed, rather than blocking until timeout:

```python
with mgr.lease(instance_id="5edcc28a-…") as inst:              ...
with mgr.lease(name="Win-US-02") as inst:                      ...
with mgr.lease(where=lambda i: "us" in i.tags) as inst:        ...

handles = pool.lease_many(4, names=NAMES, timeout=60)   # rolls back on partial failure
```

Drag a slider — the buttons mask stays down for the whole haul, the trajectory
overshoots and comes back, and there is a pause before the release, because
*releasing the instant you arrive* is the most reliable machine tell there is:

```python
s.drag("#captcha-knob", by=(212, 0), human=CAREFUL)
s.drag_and_drop("#card", "#done-column")     # HTML5 native drag, or a JS one — both
```

Rotate the exit IP. The tunnel hands out addresses per TCP connection and Chrome
reuses keep-alive sockets, so a whole run pins to one IP. A fresh browser context is
the only thing that reliably breaks that — clearing cache, unique query strings, and
`emulateNetworkConditions` all do nothing ([why](docs/详细文档手册.md)):

```python
with inst.context() as ctx, ctx.session() as s:   # own socket pool → new exit
    print(ctx.exit_ip())
    s.open(url)
```

Stop paying for bytes you throw away, and shed the tracking cookie afterwards:

```python
with s.block(types=["Image", "Media", "Font"]) as blocked:
    s.open(url)
print(blocked.by_type)                       # {'Image': 34, 'Font': 6}

report = s.clear_site_data("https://example.com")
print(report.cookies)                        # ('datadome',) — what actually went away
```

Three providers' worth of instances, one logical pool:

```python
from sleight import Pool
from sleight.providers import CloakBrowserManager, Plain

pool = Pool([
    CloakBrowserManager("http://10.0.0.1:9000", token=T1, name="hk"),
    CloakBrowserManager("http://10.0.0.2:9000", token=T2, name="sg"),
    Plain("http://127.0.0.1:9222", name="local"),
])

with pool.lease(where=lambda i: "us" in i.tags) as inst:
    ...
```

## Getting a fleet to drive

The hard part of running CloakBrowser is not the driving — it is the deployment, the
extension rollout, and the "why does this profile behave differently" archaeology.
So the same package ships a CLI for it. Local docker or a remote host over SSH is
the same code path, only the runner differs:

```bash
sleight hosts add hk-01 --ssh deploy@10.0.0.12 --dir /srv/cloakbrowser-manager --sudo
sleight deploy --host hk-01
sleight deployments add hk-01 second --dir /srv/cbm-2 --port 9001   # same box, second manager
sleight ext push ./plugins/bypass-paywalls --host hk-01   # MV3 check + permissions
sleight ext apply --host hk-01                            # every profile, then restart
sleight ext verify --host hk-01                           # did the browser really load it
sleight ui                                                # the same, in a browser
```

Hosts, the managers on each of them, and a deploy/backup/upgrade audit trail live in a
local SQLite database (`~/.sleight/sleight.db`) that the CLI and the web UI share.

`sleight ui` walks you through it: connect a host (with a real connection test before
anything is saved), pick a sizing template, preflight, deploy. Every option carries a
one-line explanation of what breaks if you get it wrong plus a recommended value —
defined once on the backend, rendered by both the CLI (`sleight templates`) and the UI.

Deploys are idempotent, `--dry-run` prints the exact bytes it would write, and the
things you must not do are refused rather than documented: no `latest` tag, no
`down -v`, no silently rotating an in-use `AUTH_TOKEN`, no second manager on the same
`/data`. The engine is stdlib-only (SSH is the system `ssh` binary); only
`sleight ui` needs `pip install "sleight[ui]"`.

## Why this exists

Fingerprint-level anti-detection is a solved problem — CloakBrowser patches Chromium
at the source level, Camoufox patches Firefox. They fix **what the browser looks like**.
Nothing fixes **how it moves**.

- Playwright and Puppeteer teleport the mouse. `mouse.move(steps=N)` interpolates a
  **straight line at constant speed** — zero jitter, zero acceleration. That is itself
  a signature.
- The browser will not fill in the trajectory for you. Even with a humanize feature
  enabled browser-side, an external CDP client produces **zero** `mousemove` events
  between press and release. Measured, not assumed.
- The good trajectory work lives in JavaScript (`ghost-cursor`). Python ports are
  thinly maintained.
- Crawlee for Python's `BrowserPool` [does not support remote browsers](https://github.com/apify/crawlee-python/issues/1743).

sleight fills exactly that gap: **Python + remote CDP + human behaviour + instance leasing.**

## Relationship to Playwright

**Not a replacement — a complement.** sleight is a driver layer, not a framework.
It deliberately does not do iframes/OOPIF, downloads, video, tracing, or a full
locator DSL. When you need those, use Playwright.

The interesting part is that you can use both: sleight's `human` module is
[sans-io](https://sans-io.readthedocs.io/) — it emits `(method, params, sleep_after)`
tuples and never touches a socket — so it drives a Playwright `CDPSession` just as
happily as sleight's own transport.

## What makes the motion credible

| | sleight | typical automation |
|---|---|---|
| Path shape | cubic Bezier, control points offset to one side | straight line |
| Micro-motion | WindMouse wind term (correlated tremor) | none, or white noise |
| Point count | Fitts's law — far small targets take longer | fixed `steps=N` |
| Landing | truncated Gaussian inside the box | dead centre |
| Coordinates | integers | floats used as "jitter" |
| Overshoot | past the target then back, distance-scaled | exact arrival |
| Typing | per-character events, interval by digraph class | one `insertText` |
| Scrolling | repeated small `mouseWheel` deltas | one `scrollTo` |
| Dragging | buttons mask held the whole way, slider-grade overshoot, pause before release | teleport, or release on arrival |

Parameters are not invented. They come from the
[WindMouse](https://ben.land/post/2021/04/25/windmouse-human-mouse-movement/) physical
model, [ghost-cursor](https://github.com/Xetera/ghost-cursor)'s Fitts-law point
budgeting, and published keystroke-dynamics measurements (alternating-hand digraphs
average 114 ms, same-hand-different-finger 131 ms, same-finger slowest and most
variable).

## Scope

**Does:** navigation, reload and history · typed wait conditions · rendered-DOM reads ·
CSS queries · human mouse / keyboard / wheel / **drag** · element screenshots · forms
(`select_option`, `upload_file`) · isolated **browser contexts** for exit-IP rotation ·
origin-scoped **site-data clearing** · **request blocking** via the Fetch domain ·
`exit_ip()` · structured network-resource capture · instance discovery across providers ·
cooperative exclusive leasing with TTL renewal (in-memory, or Redis-backed across
processes) · idempotent recovery · deploying and operating CloakBrowser Manager over
local docker or SSH, extensions included.

**Does not:** data extraction · scheduling and queues · fingerprint spoofing (that is
the browser's job) · iframe / OOPIF / Shadow DOM piercing · strict fencing · WebDriver
BiDi · Firefox.

The deploy layer lives in its own subpackage and is never imported by `import sleight`,
so the driver stays a one-dependency library.

## Known limits

Measured, not assumed. Each of these cost someone a day to find out:

| | |
|---|---|
| **No extensions inside a browser context** | `Target.createBrowserContext` makes an off-the-record context, and Chrome does not enable extensions there. Same profile, same URL: `chrome-extension://<id>/…` opens in the default context and returns `ERR_BLOCKED_BY_CLIENT` in a fresh one. So **rotating the exit IP and using a plugin are mutually exclusive** — if your run depends on one, rotate by leasing different profiles with different upstream proxies instead. |
| **`reload()` on a redirecting URL can return early** | One redirect is two document commits, and the intermediate one may fire its own `DOMContentLoaded`. Measured 3/8 on `http://` → `https://`, 0/8 without the redirect. Not specific to sleight. Wait on something page-specific (`Selector`, `Text`) when it matters. |
| **`block()` only bites while sleight is talking to the browser** | Paused requests need the event pump, which runs inside `open` / `wait` / `pump_events` / every `call`. A plain `time.sleep()` stalls them. |
| **`Transport` belongs to the thread that created it** | Enforced, not documented-and-hoped: cross-thread use raises. Lease one instance per thread. `Pool` and the lease table are shared on purpose. |
| **`set_viewport()` does not change `screen.*`, and `clear_viewport()` may not resize anything** | The override is render-layer; screen dimensions are a profile fingerprint field fixed at launch. Clearing the override only guarantees *no override* — measured on Chromium 146 + Xvnc, the window bounced back on half the attempts and stayed at the overridden size on the other half. Set the size you want; do not rely on restoring. |

## Roadmap

Ordered by what actually blocks work, not by size.

- **iframe / frame support** — `s.frames()`, `with s.frame(sel) as fs:`. The one real
  gap. CAPTCHAs live in iframes (DataDome's does), so `drag` and element screenshots —
  both shipped — still cannot reach a slider inside one. Needs a frame tree,
  cross-frame coordinate mapping, and a separate session per OOPIF.
- **Context vs. lightweight-instance resource numbers** — memory, CPU, and time-to-ready
  for *instance with proxy+plugin* / *bare instance* / *N contexts in one instance*.
  Nobody should redesign their concurrency around contexts without this table, so the
  API stays an opt-in dimension until the numbers exist.
- **`launch_args_effective`** — `get_profile()` returns the *configured* launch args;
  the effective command line lives on the Manager side. Diagnosing proxy problems
  currently means reading `chrome://version`.

## Status

`0.x` — alpha, the API will move. Every release documents its breaking changes in its
git tag. Releases are published from that tag by
[`.github/workflows/publish.yml`](.github/workflows/publish.yml) via PyPI Trusted
Publishing — no token is stored in this repository.

## License

MIT
