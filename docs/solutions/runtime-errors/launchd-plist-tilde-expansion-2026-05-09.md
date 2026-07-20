---
title: "launchd does not expand ~ or $HOME in StandardErrorPath/StandardOutPath plist keys"
date: 2026-05-09
track: bug
module: src/screencap/daemon/launchagent.py
problem_type: runtime_error
component: tooling
symptoms:
  - "Daemon exits immediately with EX_CONFIG (exit code 78) on every spawn — `launchctl print gui/$UID/<label>` shows `last exit code = 78: EX_CONFIG`, `state = spawn scheduled`, never reaches `running`"
  - "`launchctl print` reveals the resolved `stderr path` / `stdout path` is the LITERAL unexpanded string (e.g., `~/Library/Logs/Screencap/daemon.err.log` or `$HOME/Library/Logs/Screencap/daemon.err.log`)"
  - "No log files are created at the intended path; no Python-level error is raised — the failure is entirely inside launchd's plist validation on spawn"
  - "`launchctl bootstrap gui/$UID <plist>` returns success at bootstrap time; the failure surfaces only on the first (and every subsequent) spawn"
  - "The same EX_CONFIG fires when an absolute path is correct but the parent log directory does not yet exist at bootstrap time"
root_cause: config_error
resolution_type: code_fix
severity: high
related_components:
  - daemon
  - launch-agent
tags:
  - launchd
  - plist
  - macos
  - launchagent
  - ex-config
  - path-expansion
  - smappservice
---

# launchd does not expand `~` or `$HOME` in StandardErrorPath/StandardOutPath plist keys

## Problem

LaunchAgent plists generated for the Screencap daemon embedded user-relative
paths in `StandardErrorPath` / `StandardOutPath`. Three sequential variants
all produced `EX_CONFIG` (exit 78) on every spawn — the daemon never started,
no log files were ever created, and `launchctl bootstrap` returned success
which made the failure invisible until the user noticed nothing was running.

## Symptoms

- `screencap serve --install` returns `install_failed_daemon_did_not_start`
  even though `launchctl bootstrap` returned 0.
- `launchctl print gui/$UID/com.screencap.daemon` shows:
  - `state = spawn scheduled` (not `running`)
  - `last exit code = 78: EX_CONFIG`
  - `stderr path = <literal-unexpanded-string>`
  - `runs = N` (climbing — KeepAlive keeps re-spawning, each attempt fails)
- `~/.screencap/run/api.sock` never appears.
- `~/Library/Logs/Screencap/daemon.{out,err}.log` never appears.
- `curl --unix-socket ~/.screencap/run/api.sock http://x/v0/daemon.info`
  fails with connection refused.

## What Didn't Work

**Attempt 1 — hardcoded developer home.** The original render baked the
developer's expanded path (`/Users/rutefigueiredo/Library/Logs/...`) into
the bundled static plist via `Path.home()` at parity-test time. Worked on
the developer's machine, broke for every other user. This was the original
P0 ship-blocker that triggered the investigation.

**Attempt 2 — `$HOME/...` literal.** Assumption: launchd expands `$HOME`
in path keys the way it expands env-var references in `EnvironmentVariables`
values. Empirical result on macOS 26.3 (build 25D125): launchd does NOT
expand `$HOME` in path keys. `launchctl print` showed the literal string
`$HOME/Library/Logs/Screencap/daemon.err.log` as the resolved `stderr path`.
Daemon exited EX_CONFIG. `runs = 11` after a few seconds (KeepAlive cycling).

**Attempt 3 — `~/...` literal.** Assumption: `launchd.plist(5)` performs
tilde expansion as many POSIX tools do. Empirical result on the same macOS
build: launchd does NOT expand `~` in path keys either. `launchctl print`
showed `stderr path = ~/Library/Logs/Screencap/daemon.err.log` (literal).
Daemon STILL exited EX_CONFIG. The parent directory `~/Library/Logs/Screencap/`
was confirmed to exist and be writable at this point — ruling out a missing-
directory cause and isolating the failure to launchd's path resolution
specifically.

**Verification that absolute paths work.** A hand-crafted plist with literal
`/Users/rutefigueiredo/Library/Logs/Screencap/daemon.{err,out}.log` paths
loaded via `launchctl bootstrap gui/$UID` produced `state = running`, pid
assigned, socket created, log files written — all on the same machine and
plist label as the failed attempts. This isolated launchd's tilde/`$HOME`
non-expansion as the sole cause.

## Solution

Two-mode `render_plist()` in `src/screencap/daemon/launchagent.py`. The
caller resolves the absolute path at install time when it can; the bundled
SMAppService case omits the path keys entirely.

```python
def render_plist(*, program, label=DAEMON_LABEL, args=("serve",),
                  log_dir=None, env_vars=None, bundle_program=None) -> bytes:
    program_path = str(Path(program).expanduser())
    if env_vars is None:
        env_vars = {"PATH": DEFAULT_PATH}
    plist: dict[str, Any] = {
        "Label": label,
        "ProgramArguments": [program_path, *list(args)],
        "RunAtLoad": True,
        "KeepAlive": {"SuccessfulExit": False, "Crashed": True},
        "ProcessType": "Adaptive",
        "ExitTimeOut": 30,
        "EnvironmentVariables": dict(env_vars),
    }
    if log_dir is not None:
        log_dir_str = str(log_dir)
        plist["StandardErrorPath"] = f"{log_dir_str}/daemon.err.log"
        plist["StandardOutPath"] = f"{log_dir_str}/daemon.out.log"
    if bundle_program is not None:
        plist["BundleProgram"] = bundle_program
    return plistlib.dumps(plist, fmt=plistlib.FMT_XML)
```

The CLI install path resolves the absolute log dir at the moment the user
invokes `screencap serve --install` and `mkdir -p`s it BEFORE bootstrapping:

```python
log_dir = Path.home() / "Library" / "Logs" / "Screencap"
try:
    log_dir.mkdir(parents=True, exist_ok=True)
except OSError as exc:
    return InstallResult(state="install_failed_plist_write_failed", ...)
content = render_plist(program=resolved_program, args=args, log_dir=str(log_dir))
```

The bundled SMAppService plist (which ships inside the `.app` bundle and
must work for any user) is rendered with `log_dir=None`. The path keys
are absent entirely; daemon stdout/stderr is captured by launchd into the
unified system log, accessible via:

```
log show --predicate 'process == "screencap"' --last 1h
```

## Why This Works

`StandardErrorPath` and `StandardOutPath` are passed to `open(2)` directly
by launchd with no shell preprocessing. There is no PATH lookup, no tilde
expansion, no environment-variable substitution. Three corollaries:

1. Any value containing `~` or `$HOME` is invalid because those are not
   path separators — launchd treats the whole string as a literal path,
   which doesn't exist.
2. The log directory must exist BEFORE `launchctl bootstrap` is called.
   Even a syntactically correct absolute path produces EX_CONFIG when
   launchd's `open(2)` fails on a missing parent.
3. Omitting both keys is always safe. launchd captures stdout/stderr into
   the unified system log instead, and that capture is independent of any
   per-user filesystem state — making it the right choice for any plist
   that ships in an `.app` bundle and can't know the user's home at
   bundle-build time.

The same non-expansion likely applies to other path-valued keys (`WorkingDirectory`,
`RootDirectory`) but was not directly tested. Treat all path-valued plist
keys as requiring absolute, pre-resolved paths until proven otherwise on
the target macOS version.

## Prevention

Checklist for any agent generating or modifying a LaunchAgent plist:

1. **Never write `~` or `$HOME` into path-valued plist keys.** Not in
   `StandardErrorPath`, `StandardOutPath`, `WorkingDirectory`,
   `RootDirectory`, or any other path key. Use absolute paths only.
2. **Resolve the absolute path in the generating code, not via runtime
   expansion.** Use `Path.home()` / `os.path.expanduser()` at render time.
3. **`mkdir -p` the parent of every path-valued key BEFORE
   `launchctl bootstrap`.** Even valid absolute paths fail with EX_CONFIG
   if the directory doesn't exist when launchd's `open(2)` runs.
4. **When the absolute path isn't knowable at build time (bundled `.app`
   plists for SMAppService), omit the path keys entirely.** Unified system
   log captures stdout/stderr; access via
   `log show --predicate 'process == "<label>"'`.
5. **Verify with `launchctl print gui/$UID/<label>` after every install.**
   Read these fields:
   - `state` should reach `running` (not `spawn scheduled`)
   - `stderr path` / `stdout path` should be a fully resolved absolute path
     (no `~` or `$HOME` substring)
   - `last exit code` should be absent or 0; `last exit code = 78`
     (EX_CONFIG) is the path-key smoking gun
   - `runs` should plateau at 1 — a climbing counter means KeepAlive is
     re-spawning a job that fails immediately
6. **Add a pinning test** that asserts:
   - `render_plist(log_dir=<abs>)` produces absolute paths in both keys
   - `render_plist()` (no log_dir) produces a plist where `StandardErrorPath`
     and `StandardOutPath` are absent
   - The bundled-resource plist (committed to the repo) byte-matches the
     `render_plist()` output for the bundled-mode call
7. **Don't trust the man page on path-key expansion.** `launchd.plist(5)`
   does not document the expansion behavior of these keys clearly, and
   empirical results on macOS 26.3 contradict the common assumption.
   Verify on the target macOS version before relying on any expansion.

## Related Issues

- The Phase 1 daemon-architecture plan
  (`docs/plans/2026-05-08-001-feat-daemon-architecture-phase-1-plan.md`,
  U6 Approach, lines 522–524) recommends the unsafe pattern
  (`StandardErrorPath: ~/Library/Logs/Screencap/daemon.err.log` etc.)
  in its plist-content bullet. The implementation has diverged correctly,
  but the plan doc still carries the broken recommendation. Consider
  updating the plan with a correction note pointing at this learning.
- Adjacent macOS launch/TCC learnings (low overlap, related context):
  - `runtime-errors/macos-tcc-per-process-cache-quit-and-relaunch.md`
  - `build-errors/macos-ad-hoc-signing-tcc-rebuild-treadmill.md`
  - `runtime-errors/sigint-handler-timing-and-recording-stop-methods.md`
- Fixed in commit `fc537e1b` on branch `feat/daemon-architecture-phase-1`.
