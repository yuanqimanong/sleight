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

Target one specific profile — by id, by name, or by tag:

```python
with mgr.lease(instance_id="5edcc28a-…") as inst:            ...
with mgr.lease(where=lambda i: i.name == "Win-US-02") as inst: ...
with mgr.lease(where=lambda i: "us" in i.tags) as inst:        ...
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

Parameters are not invented. They come from the
[WindMouse](https://ben.land/post/2021/04/25/windmouse-human-mouse-movement/) physical
model, [ghost-cursor](https://github.com/Xetera/ghost-cursor)'s Fitts-law point
budgeting, and published keystroke-dynamics measurements (alternating-hand digraphs
average 114 ms, same-hand-different-finger 131 ms, same-finger slowest and most
variable).

## Scope

**Does:** navigation and typed wait conditions · rendered-DOM reads · CSS queries ·
human mouse / keyboard / wheel · structured network-resource capture · instance
discovery across providers · cooperative exclusive leasing with TTL renewal
(in-memory, or Redis-backed across processes) · idempotent recovery · deploying and
operating CloakBrowser Manager over local docker or SSH, extensions included.

**Does not:** data extraction · scheduling and queues · fingerprint spoofing (that is
the browser's job) · iframe / OOPIF / Shadow DOM piercing · strict fencing · WebDriver
BiDi · Firefox.

The deploy layer lives in its own subpackage and is never imported by `import sleight`,
so the driver stays a one-dependency library.

## Status

`0.x` — alpha, the API will move. Every release documents its breaking changes.
Releases are published from a git tag by
[`.github/workflows/publish.yml`](.github/workflows/publish.yml) via PyPI Trusted
Publishing — no token is stored in this repository.

## License

MIT
