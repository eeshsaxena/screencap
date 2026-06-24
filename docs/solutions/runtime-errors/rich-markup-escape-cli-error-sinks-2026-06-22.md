---
title: "Unescaped dynamic text in CLI Rich-markup sinks garbles output or crashes with MarkupError"
slug: rich-markup-escape-cli-error-sinks-2026-06-22
date: 2026-06-22
category: runtime-errors
severity: medium
problem_type: runtime-error
modules:
  - src/screencap/cli/__init__.py
tags:
  - rich
  - markup
  - console-print
  - escape
  - cli
  - error-handling
  - markuperror
symptoms:
  - "A bracketed run in dynamic text (e.g. `connection to [db-host:5432] failed`) is silently stripped, so the user sees a garbled message like `connection to  failed`."
  - "Dynamic text containing an unbalanced tag such as `[/]` raises `rich.errors.MarkupError: closing tag '[/]' ... has nothing to close`."
  - "Because `MarkupError` is not the type the surrounding `except` catches, it propagates past the handler and crashes the command with a traceback instead of the intended clean exit-1."
root_cause: >
  Rich's `Console.print` parses its ENTIRE rendered string for `[...]` markup
  tags, including any interpolated dynamic text. CLI sinks shaped like
  `console.print(f"[red]Error:[/red] {e}")` fed exception, daemon, file, and
  user-CLI-input text straight into that markup parser without escaping. When
  that text contained markup metacharacters (`[`, `]`, `[/]`) the parser either
  stripped the bracketed run (garble) or, on an unbalanced tag, raised
  `rich.errors.MarkupError`. The error type does not match the domain
  exceptions the `try/except` was written for (e.g. `ReviewPrepareError`,
  `auth.AuthError`), so it escaped the handler and surfaced as a raw traceback.
  The pattern was systemic: ~48 sinks across the CLI shared it.
---

# Unescaped dynamic text in CLI Rich-markup sinks garbles output or crashes with MarkupError

## Problem

Every `console.print(f"...{dynamic}...")` sink in the CLI renders through Rich's markup parser. Any `[`/`]`/`[/]` inside the interpolated dynamic value is treated as markup: balanced-looking brackets get stripped (garbled message), and an unbalanced tag raises `MarkupError`, which bypasses the command's domain-specific `except` clause and crashes with a traceback. Surfaced in review of PR #216, which made the `login` sink newly reachable by surfacing Google's free-text OAuth `error_description`.

## Symptoms

- Garble: `console.print(f"[red]Error:[/red] {e}")` with `e = "connection to [db-host:5432] failed"` prints `Error: connection to  failed`.
- Crash: with `e = "unexpected token [/] in response"` it raises `rich.errors.MarkupError`, which propagates and crashes the command.

## Solution

Wrap the dynamic segment of every Rich-markup sink with `rich.markup.escape` (already imported in the module). `escape()` backslash-protects `[` so the value renders verbatim and never reaches the markup parser as a tag.

```python
from rich.markup import escape

# Before — dynamic text parsed as markup
console.print(f"[red]Error:[/red] {e}")
console.print(f"      {entry!r}")          # repr of a list literally contains [ ]

# After — dynamic text escaped, literal brackets preserved
console.print(f"[red]Error:[/red] {escape(str(e))}")
console.print(f"      {escape(repr(entry))}")
```

Scope rule applied across the sweep (PR #261): escape the dynamic value in every red-error / yellow-warning sink, every echo of raw user CLI input (e.g. `settings set`, `privacy add/remove`), and every `repr()` of external data. Leave untouched: the hardcoded markup literals themselves (`[red]...[/red]`), enum/state labels that are module constants, and joins of internal constant identifiers (`Available: {', '.join(all_keys)}`). Never escape a value that is also used for control flow (e.g. `result.detail` is escaped only for display; the raw value still feeds the `pid=` remediation regex). The `--json` output paths are unaffected — they emit via `click.echo`/`json.dumps`, not Rich.

## Why This Works

The crash and the garble are the same bug: dynamic text reaching Rich's markup parser. `escape()` neutralizes the metacharacters before the parser runs, so the only markup Rich interprets is the static `[red]`/`[/red]` the developer wrote intentionally. Plain text without brackets passes through `escape()` unchanged, so output is byte-for-byte identical for the common case — making the sweep safe and behavior-preserving.

## Prevention

- New rule of thumb: if an f-string passed to `console.print` / `err_console.print` interpolates anything that is not a hardcoded literal, wrap that segment in `escape(str(...))` (or `escape(repr(...))` for `!r`). Static markup tags stay outside the `escape()` call.
- Regression test ([tests/test_cli_review_data.py](../../../tests/test_cli_review_data.py) `test_cli_human_error_escapes_rich_markup`): drives the human error path with `[/]`-laden exception text and asserts no `MarkupError`, a clean exit-1, and verbatim bracket preservation. To force the human (non-JSON) path under `CliRunner` (whose stdout is non-TTY), patch `screencap.cli._should_default_to_json` to return `False`.
- `MarkupError` is a `rich.errors` type, not a subclass of the domain exceptions handlers catch — a `try/except DomainError` around a `console.print` does NOT contain it. Escaping at the sink is the fix, not broadening the `except`.
- The deferred info/success-path gap (SCR-169) — sinks that print dynamic-but-non-error text — covered the same bug class on the *success* path that SCR-117 left on the *error* path. Closed by escaping the dynamic segment in each of these families:
  - `login` / `whoami` success — the provider-controlled account email/uid (the same untrusted source class as PR #216, just on the success path).
  - `view` success — the user-chosen recording name in the "Opening …/viewer.html" line.
  - `info` — recording name, `.recording_intent` values, and `metrics.json` / OS-derived values: running-app names + bundle IDs + versions, wifi SSID, and locale strings (the structural metric *keys* stay unescaped — internal constants).
  - `apps` human listing — OS-derived `display_name` / `bundle_id`.
  - `status` — daemon-reported `recording_name` (user-chosen) + `claimant`.
  - `transcribe` — the transcript `preview` text plus the name-derived saved paths.
  - `settings --set` echo — the (validated) key/value, for symmetry with the already-escaped error paths in the same command.
  - `review_data` human success — recording name + envelope `video_path` / `events_path`.
- Remaining same-class siblings outside SCR-169's enumerated scope (e.g. the `export` "Exported N events to <path>" success lines) are left for the SCR-168 AST guard, which is meant to enforce escaping across *all* dynamic interpolations rather than only the error/warning sinks.

## Related

- PR https://github.com/proteus-computer-use/screencap/pull/261 (this sweep), Linear SCR-117
- PR https://github.com/proteus-computer-use/screencap/pull/216 (established the `escape(str(e))` precedent on the `login` sink)
- Linear SCR-169 (the deferred info/success-path sweep documented in Prevention above), SCR-168 (the AST guard that will enforce the invariant across all dynamic sinks)
