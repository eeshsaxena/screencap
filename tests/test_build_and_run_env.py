"""`.env` parsing in ``script/build_and_run.sh`` (``load_local_env``).

Regression coverage for the silent-skip bug: a shell-style ``export KEY=VALUE``
line — the conventional way developers write a ``.env`` — parsed as the key
``"export DEVELOPMENT_TEAM"`` (with an embedded space), failed the identifier
check, and was dropped. With ``DEVELOPMENT_TEAM`` never exported, xcodebuild
fell back to ad-hoc signing, so macOS TCC orphaned the Screen Recording grant
on every rebuild and recordings died with "permission revoked".
"""

from __future__ import annotations

import shlex
import shutil
import subprocess
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "script" / "build_and_run.sh"


def _run_load_local_env(env_body: str, tmp_path: Path) -> dict[str, str]:
    """Drive the real ``load_local_env`` against a temp ``.env``.

    Extracts only the function from the script so sourcing it does not run
    ``main`` (which would build the app), then reports the resulting exported
    values for the keys under test.
    """
    (tmp_path / ".env").write_text(env_body)

    lines = SCRIPT.read_text().splitlines()
    start = next(i for i, ln in enumerate(lines) if ln.startswith("load_local_env()"))
    end = next(i for i in range(start + 1, len(lines)) if lines[i] == "}")
    func = "\n".join(lines[start : end + 1])

    harness = f"""
set -uo pipefail
ROOT_DIR={shlex.quote(str(tmp_path))}
{func}
load_local_env
for k in DEVELOPMENT_TEAM FOO QUOTED; do
  printf '%s=%s\\n' "$k" "${{!k:-<unset>}}"
done
"""
    result = subprocess.run(
        ["bash", "-c", harness], capture_output=True, text=True
    )
    assert result.returncode == 0, result.stderr
    out: dict[str, str] = {}
    for line in result.stdout.splitlines():
        key, _, value = line.partition("=")
        out[key] = value
    return out


@pytest.mark.skipif(shutil.which("bash") is None, reason="bash required")
def test_export_prefixed_env_line_is_parsed(tmp_path):
    env = 'export DEVELOPMENT_TEAM=YL664A67R4\nFOO=bar\nQUOTED="hi there"\n'
    out = _run_load_local_env(env, tmp_path)

    # The regression: this was "<unset>" because the `export ` prefix made the
    # key fail the identifier check.
    assert out["DEVELOPMENT_TEAM"] == "YL664A67R4"
    # Plain KEY=VALUE lines (no prefix) must keep working unchanged.
    assert out["FOO"] == "bar"
    # Quoted values are still unquoted.
    assert out["QUOTED"] == "hi there"
