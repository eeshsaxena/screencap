# Contributing to ScreenCap

Thanks for your interest in ScreenCap.

## License and the CLA

ScreenCap is **source-available, not open source**. It is distributed under the
[PolyForm Noncommercial License 1.0.0](LICENSE), and licensed separately on
commercial terms to paying users.

Because of that dual arrangement, **outside contributors must sign the
[Contributor License Agreement](CLA.md) before their first pull request is
merged.** The CLA lets you keep the copyright in your work while granting Ivy
Research LLC the right to distribute it under both the noncommercial license and
paid commercial licenses. Without a signed CLA we cannot merge your contribution.

**Ivy Research LLC employees do not need to sign.** Work created within the scope
of your employment is already owned by the company, so there is nothing left for
a CLA to grant.

### How to sign

1. Read [CLA.md](CLA.md).
2. Copy the signing block at the bottom, fill it in, and email it to
   aayushgupta5000@gmail.com with the subject `ScreenCap CLA — <your name>`.
3. Mention in your pull request that you have sent it.

One signature covers all of your past and future contributions. If you are
contributing as part of your job, check whether your employer needs to sign
instead — Section 4 of the CLA covers this.

## Development setup

```bash
pip install -e ".[dev]"
```

Python >= 3.10, macOS only.

## Running tests

```bash
pytest tests/
```

A single file or test:

```bash
pytest tests/test_cli.py::test_list_recordings_empty
```

With coverage:

```bash
pytest tests/ -v --cov
```

Lint the recording engine:

```bash
ruff check src/screencap/engine/
```

### Privacy-bearing changes

CI runs the privacy lane specifically. If your change touches
`src/screencap/privacy/`, `enforcement/`, or `redaction/`, mark the covering
tests with `@pytest.mark.privacy` and keep them free of Apple Vision
dependencies — otherwise they will not run on CI.

```bash
pytest -m privacy
```

### Chrome extension

The extension under `extension/` is a self-contained npm sub-project with its own
toolchain. Run these from `extension/`:

```bash
npm install && npm test && npm run typecheck && npm run build
```

## Code conventions

These are enforced by review; see `CLAUDE.md` for the full architecture map.

- All user-facing output goes through `rich.console.Console` — no bare `print()`.
- Defer heavy imports inside CLI command bodies so `screencap --help` stays fast.
- SQLite access in the `screencap` layer uses raw `sqlite3`, not SQLAlchemy.
- Child processes re-import `recorder.py` under spawn mode — avoid module-level
  side effects.

## Third-party code

If your contribution includes code you did not write, say so explicitly in the
pull request along with its origin and license. Note that ScreenCap ships under
a proprietary-compatible license, so **copyleft-licensed code (GPL, AGPL, and
similar) cannot be accepted.** Weak-copyleft dependencies such as LGPL are
handled case by case and carry ongoing obligations — see
[THIRD-PARTY-NOTICES.md](THIRD-PARTY-NOTICES.md).

## Security

Please do not open public issues for security vulnerabilities. See
[SECURITY.md](SECURITY.md) for the threat model and reporting guidance.
