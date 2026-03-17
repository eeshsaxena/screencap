---
title: "Fix PyInstaller frozen binary smoke test failures caused by Presidio/spaCy bundling issues"
date: 2026-03-17
problem_type: build-and-packaging
component: pyinstaller-spec, ci-workflows, cli-smoke-test
symptoms:
  - "CI smoke test exits with code 2: 'Error: No such option: -m' when Presidio AnalyzerEngine triggers spacy.cli.download() subprocess"
  - "spacy.load('en_core_web_sm') raises [E050] Can't find model 'en_core_web_sm' inside frozen binary"
  - "ModuleNotFoundError: No module named 'en_core_web_sm' at runtime in PyInstaller bundle"
  - "collect_all('en_core_web_sm') silently fails in .spec file because model was never installed in CI"
root_causes:
  - "Presidio AnalyzerEngine() fallback spawns sys.executable -m pip; in frozen binary sys.executable is the Click entrypoint which rejects -m, raising SystemExit(2) not caught by except Exception"
  - "spacy.load() relies on importlib.util.find_spec() which cannot resolve packages bundled as PyInstaller hidden imports"
  - "en_core_web_sm was never installed in CI before PyInstaller build, so collect_all silently collected nothing"
tags:
  - pyinstaller
  - frozen-binary
  - ci
  - github-actions
  - spacy
  - presidio
  - smoke-test
  - bundling
severity: high
time_to_resolve: medium
---

# Fix PyInstaller frozen binary smoke test failures

## Context

ScreenCap is a macOS screen recording CLI distributed as a PyInstaller frozen binary (a self-contained executable that bundles Python, all dependencies, and data files into a single directory). The `_smoke-test` hidden CLI command validates that 9 critical subsystems (Presidio PII detection, GLiNER NER, detect-secrets, spaCy NLP, ffmpeg/av, pynput, sounddevice, UT1 domain data, and onnxruntime exclusion) actually load inside the frozen binary. It runs in CI on every PR and release build.

These three issues were discovered during the first CI runs of the smoke test — each fix revealed the next failure, peeling back layers of PyInstaller/frozen-binary incompatibilities.

## Root Cause Analysis

Three distinct issues compound to make Python packages fail in PyInstaller frozen binaries:

### Issue 1: Presidio AnalyzerEngine subprocess crash

`AnalyzerEngine()` with no arguments creates a default `NlpEngineProvider()` which reads `conf/default.yaml` specifying `en_core_web_lg` as the spaCy model. When the model isn't found, spaCy falls back to `spacy.cli.download()` which spawns `sys.executable -m pip install en_core_web_lg`. In a frozen binary, `sys.executable` is the `screencap` binary itself. Click receives `-m` as an unknown option and raises `SystemExit(2)`. Critically, `SystemExit` inherits from `BaseException`, not `Exception`, so `except Exception` does not catch it.

### Issue 2: spacy.load() can't find bundled model

`spacy.load("en_core_web_sm")` calls `importlib.util.find_spec("en_core_web_sm")` to locate the model package. In a PyInstaller frozen binary, `find_spec()` returns `None` even when the model is bundled and importable via a normal `import` statement (PyInstaller's `FrozenImporter` doesn't implement the full `importlib` metadata protocol).

### Issue 3: Model never installed in CI

The `.spec` file has `collect_all('en_core_web_sm')` wrapped in `try/except Exception: pass`. Without the model installed in CI, `collect_all` silently fails and the binary ships without any model data. The CI workflows never ran `python -m spacy download en_core_web_sm` before the build.

## Investigation Steps

1. First CI run: process crashed with `Error: No such option: -m` (exit code 2), no PASS/FAIL lines printed
2. Traced call chain: `AnalyzerEngine()` -> `SpacyNlpEngine.load()` -> `spacy.cli.download()` -> `subprocess(sys.executable, "-m", "pip")`
3. Fixed presidio check with no-op NLP engine, caught `BaseException` in runner
4. Second CI run: `[E050] Can't find model 'en_core_web_sm'` — model data not found by `spacy.load()`
5. Pre-imported `en_core_web_sm` before `spacy.load()` to populate `sys.modules`
6. Third CI run: `ModuleNotFoundError: No module named 'en_core_web_sm'` — module not even importable
7. Discovered `en_core_web_sm` is downloaded at runtime by `screencap setup`, not a pip dependency
8. Added `python -m spacy download en_core_web_sm` to CI workflows

## Working Solution

### Fix 1: No-op NLP engine for Presidio smoke test

Replace `AnalyzerEngine()` with a stub NLP engine that validates YAML recognizer config loading without triggering the spaCy subprocess chain:

```python
from presidio_analyzer import AnalyzerEngine
from presidio_analyzer.nlp_engine import NlpEngine, NlpArtifacts

class _NoOpNlpEngine(NlpEngine):
    def load(self): pass
    def is_loaded(self): return True
    def process_text(self, text, language):
        return NlpArtifacts(
            entities=[], tokens=[], lemmas=[],
            tokens_indices=[], dependencies=[],
            keywords=[], language=language,
        )
    def process_batch(self, texts, language, **kwargs):
        for text in texts:
            yield text, self.process_text(text, language)
    def is_stopword(self, word, language): return False
    def is_punct(self, word, language): return False
    def get_supported_entities(self): return []
    def get_supported_languages(self): return ["en"]

analyzer = AnalyzerEngine(nlp_engine=_NoOpNlpEngine())
recognizers = analyzer.registry.get_recognizers(language="en", all_fields=True)
```

Also catch `BaseException` (not just `Exception`) in any runner loop to handle `SystemExit`:

```python
try:
    result = check_fn()
except BaseException:
    result = (name, False, traceback.format_exc())
```

### Fix 2: Pre-import spaCy model in frozen binary

Import the model package before `spacy.load()` to populate `sys.modules`, which spaCy checks before `find_spec()`:

```python
import en_core_web_sm  # noqa: F401 — populates sys.modules for frozen binary
import spacy
spacy.load("en_core_web_sm")
```

### Fix 3: Install spaCy model in CI before build

Add to both `binary-test.yml` and `release.yml`, after pip install but before PyInstaller:

```yaml
- name: Download spaCy model
  run: python -m spacy download en_core_web_sm
```

## Prevention Strategies

### Core principle: test what you bundle, bundle what you test

Every `collect_all` in the spec should have a corresponding smoke test check that exercises the collected package's runtime behavior against the frozen binary.

### Patterns to watch

| Warning Sign | Risk | Action |
|---|---|---|
| `except Exception: pass` around `collect_all` | Silent missing data | Fail loudly or log explicitly |
| Dependency uses `sys.executable` in subprocess calls | Subprocess crash in frozen binary | Pre-bundle artifacts; eliminate runtime download paths |
| Dependency uses `find_spec()` or `pkg_resources` | Feature silently disabled in bundle | Use `try: import` or pre-populate `sys.modules` |
| Dependency has `data_files` or `importlib.resources` usage | Data not collected by PyInstaller | Add explicit `datas` entries + smoke test |
| Any `collect_all` without a corresponding smoke test | Untested bundle content | Add a smoke test for the collected package |

### Checklist for adding new smoke test checks

1. Can the check run without macOS permissions or network access?
2. Does `import the_package` work, or does it need a pre-import workaround?
3. Does initialization trigger any subprocess calls via `sys.executable`?
4. Does the package use `find_spec()`, `pkg_resources`, or `importlib.metadata` for discovery?
5. Are data files (YAML, models, configs) loaded from package-relative paths?

## Related Documentation

- `docs/solutions/build-errors/macos-pre14-binary-install-failure.md` — related: binary fails on macOS < 14 due to `minos` deployment target and ONNX Runtime issues
- `pyinstaller/screencap.spec` — the PyInstaller spec file where `collect_all` and excludes are configured
- `src/screencap/cli.py` — the `_smoke-test` command implementation (search for `_SMOKE_CHECKS`)
- `.github/workflows/binary-test.yml` — PR workflow that builds and smoke-tests on both architectures
- `.github/workflows/release.yml` — release workflow with smoke test + verify-install jobs
