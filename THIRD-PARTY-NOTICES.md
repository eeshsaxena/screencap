# Third-Party Notices

ScreenCap is distributed under the [PolyForm Noncommercial License 1.0.0](LICENSE).
The components listed here are **not** covered by that license — each keeps its
own terms, reproduced or referenced below.

Last audited: 2026-07-31, against the dependency set declared in `pyproject.toml`.

---

## ✅ RESOLVED: `oa-atomacos` (GPL-2.0-only) removed

**Status: resolved. No GPL-licensed code remains in the distributed work.**

`oa-atomacos` was licensed **GPL-2.0-only** — its source headers elected the
version explicitly ("Software Foundation version 2 and no later version"), with
no upgrade path. ScreenCap imported it directly and in-process in
`src/screencap/engine/window/_macos.py`, forming a combined work that could not
be conveyed under PolyForm Noncommercial. It was also incompatible with the
previous AGPL-3.0 license, so this was a pre-existing defect rather than one the
relicense introduced.

It has been replaced with direct PyObjC calls against the same underlying
Accessibility API, matching the pattern already used by `get_active_window()` in
the same module and by `ax_browser_url.py`:

| Removed (`oa-atomacos`) | Replacement (PyObjC) |
|---|---|
| `AXUIElement.from_pid(pid)` | `AXUIElementCreateApplication(pid)` |
| `.set_timeout(t)` | `AXUIElementSetMessagingTimeout(app_ref, t)` |
| `.get_element_at_position(x, y)` | `AXUIElementCopyElementAtPosition(app_ref, x, y, None)` |
| `.ref` | the returned element *is* the ref |
| `_converter.Converter().convert_value(v)` | dropped — `deepconvert_objc` had already converted the value |

The dependency is gone from `pyproject.toml`; `tests/engine/test_macos_ax_element.py`
covers the replacement.

---

## `pynput` — LGPL-3.0 (bundled into the frozen daemon)

`pynput` (>= 1.7.0) is licensed **LGPL-3.0**. It is bundled into the PyInstaller
daemon shipped inside `Screencap.app` — `pyinstaller/screencap.spec` collects its
macOS backends as hidden imports:

```
'pynput.keyboard._darwin', 'pynput.mouse._darwin', 'pynput._util.darwin'
```

LGPL-3.0 permits use from a proprietary-licensed application, but **conditionally**:
section 4 requires that recipients be able to substitute a modified version of the
library and have the combined work still function.

**How ScreenCap satisfies this:**

The daemon is built as a PyInstaller `--onedir` bundle (not `--onefile`), so the
library remains replaceable in the shipped product. To substitute your own build
of `pynput`:

1. Locate the bundled daemon inside the app at
   `Screencap.app/Contents/Resources/daemon/`.
2. Replace the `pynput` modules in the bundle's `_internal` directory with your
   own build of the same version.
3. Re-run the daemon. Note that replacing files inside a signed `.app` invalidates
   its code signature; re-sign locally or run the daemon binary directly.

**Written offer of source:** the complete corresponding source for the version of
`pynput` bundled in any ScreenCap release is available on request from
aayushgupta5000@gmail.com, and upstream at https://github.com/moses-palmer/pynput.

This obligation is ongoing and applies to every release. It did not apply under the
previous AGPL-3.0 license, which absorbed LGPL-3.0 automatically.

---

## `PyInstaller` — GPL-2.0-or-later WITH the bootloader exception (not a blocker)

`PyInstaller` is GPL-2.0-or-later, but carries an explicit exception, quoted from
its own distribution metadata:

> GPLv2-or-later with a special exception which allows to use PyInstaller to build
> and distribute non-free programs (including commercial ones)

It is a build-time tool and the exception expressly covers this use. No obligation
flows to ScreenCap's license. Listed here only to preempt the question — the PyPI
classifier says "GPLv2" and looks alarming in automated scans.

---

## UT1 blocklist data — CC BY-SA 4.0

`src/screencap/privacy/data/ut1/` contains domain lists from the UT1 blocklist
project (Université Toulouse Capitole), used for privacy-aware browser domain
classification.

- License: Creative Commons Attribution-ShareAlike 4.0 International
- https://creativecommons.org/licenses/by-sa/4.0/
- Mirror: https://github.com/olbat/ut1-blacklists

Attribution is required and is preserved in
`src/screencap/privacy/data/ut1/LICENSE`, which must ship with any distribution.
ShareAlike applies to adaptations **of the data**, not to ScreenCap's own code.
Only the `bank`, `financial`, `webmail`, `social_networks`, `chat`, and `vpn`
categories are included; IP addresses and non-domain entries were stripped.

---

## Permissively licensed dependencies

The remaining direct dependencies carry permissive licenses imposing no
restriction on ScreenCap's own terms. Attribution notices must still be preserved
in distributed builds.

| License | Dependencies |
|---|---|
| MIT | `rich`, `keyring`, `mcp`, `mitmproxy`, `mss`, `presidio-analyzer`, `pydantic`, `pydantic-settings`, `pyfiglet`, `tomlkit`, `sqlalchemy`, `loguru`, `sounddevice`, `faster-whisper`, `pyobjc-framework-*` |
| BSD-3-Clause | `click`, `httpx`, `python-dotenv`, `starlette`, `uvicorn`, `av`, `psutil`, `soundfile`, `numpy` |
| Apache-2.0 | `requests`, `openai`, `fire`, `detect-secrets`, `fast-gliner`, `huggingface_hub`, `packaging`, `pympler`, `pyinstaller-hooks-contrib` |
| Apache-2.0 OR BSD-3-Clause | `cryptography` |
| HPND | `Pillow` |
| MPL-2.0 AND MIT | `tqdm` |
| zlib/libpng | `pysqlcipher3` |

`tqdm`'s MPL-2.0 portion is file-level weak copyleft: modifications to MPL-licensed
files must be published, but it imposes nothing on ScreenCap's own source. It is
used unmodified.

---

## Maintaining this file

Re-audit whenever `pyproject.toml` dependencies change. Any new GPL- or
AGPL-licensed dependency is incompatible with ScreenCap's license and must be
rejected — see [CONTRIBUTING.md](CONTRIBUTING.md).
