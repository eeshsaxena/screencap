"""Whisper transcription: backend selection and execution.

Cached-model discovery + ranking, interactive backend resolution, and the
OpenAI-API / local-Whisper transcription drivers. Extracted from the CLI
(SCR-32) so backend resolution is unit-testable without a CliRunner round
trip. Heavy imports (openai, faster_whisper, whisper, screencap.engine.cli)
stay deferred inside function bodies to keep importing this module cheap.
"""

from __future__ import annotations

import json
import sys

import click
from rich.console import Console

console = Console()


_WHISPER_MODELS = {
    "1": ("tiny", "~39 MB", "Fast, lower accuracy"),
    "2": ("base", "~140 MB", "Good balance (recommended)"),
    "3": ("small", "~466 MB", "Better accuracy"),
    "4": ("medium", "~1.5 GB", "High accuracy"),
    "5": ("large", "~2.9 GB", "Best accuracy"),
}


def _resolve_backend_interactive() -> tuple[str, str | None]:
    """Discover API key / prompt user and return (backend, api_key_or_model).

    Returns:
        ("api", api_key) — use OpenAI API with this key
        ("local", model_name) — use local whisper
    """
    import os

    # 1. Check for existing API key
    api_key = os.environ.get("OPENAI_API_KEY")
    if not api_key:
        try:
            from screencap.engine.config import settings
            api_key = settings.openai_api_key
        except Exception:
            pass

    if api_key:
        console.print("[green]Found OpenAI API key[/green]")
        if click.confirm(
            "Use OpenAI API for transcription? (faster, costs ~$0.006/min)",
            default=True,
        ):
            return "api", api_key
        # user declined — fall through to local
        return _select_local_model()

    # 2. No key found — offer choices
    console.print("[dim]No OpenAI API key found.[/dim]")
    choice = click.prompt(
        "Choose an option\n"
        "  1. Enter OpenAI API key\n"
        "  2. Transcribe locally with Whisper\n"
        "Choice",
        type=click.IntRange(1, 2),
        default=2,
    )

    if choice == 1:
        key = click.prompt("OpenAI API key", hide_input=True)
        return "api", key

    return _select_local_model()


_MODEL_RANK = ["large", "medium", "small", "base", "tiny"]


def _detect_cached_models() -> list[str]:
    """Return whisper model names already downloaded, best-quality first."""
    import os
    from pathlib import Path

    found = set()

    # faster-whisper: ~/.cache/huggingface/hub/models--Systran--faster-whisper-{name}/
    hf_cache = Path(os.environ.get("HF_HUB_CACHE", "")) if os.environ.get("HF_HUB_CACHE") else (
        Path(os.environ.get("HF_HOME", Path.home() / ".cache" / "huggingface")) / "hub"
    )
    if hf_cache.is_dir():
        for entry in hf_cache.iterdir():
            if entry.is_dir() and entry.name.startswith("models--Systran--faster-whisper-"):
                model = entry.name.split("faster-whisper-", 1)[1]
                if model in _MODEL_RANK:
                    found.add(model)

    # openai-whisper: ~/.cache/whisper/{name}.pt
    whisper_cache = Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache")) / "whisper"
    if whisper_cache.is_dir():
        for entry in whisper_cache.iterdir():
            if entry.suffix == ".pt":
                model = entry.stem.split(".")[0]  # handles "base.en.pt" → "base"
                if model in _MODEL_RANK:
                    found.add(model)

    return [m for m in _MODEL_RANK if m in found]


def _ensure_whisper_backend() -> None:
    """Make sure at least one local whisper backend is installed."""
    try:
        import faster_whisper  # noqa: F401
        return
    except ImportError:
        pass
    try:
        import whisper  # noqa: F401
        return
    except ImportError:
        pass

    if getattr(sys, 'frozen', False):
        console.print("[red]Whisper dependencies missing from binary. Reinstall screencap.[/red]")
    else:
        console.print("[red]Local transcription requires whisper dependencies.[/red]")
        console.print("Run: pip install faster-whisper")
    raise SystemExit(1)


def _select_local_model(model: str | None = None) -> tuple[str, str]:
    """Pick a local whisper model. Auto-detects cached models to skip prompts.

    Args:
        model: Explicit model name (from --model flag). Skips all detection/prompts.

    Returns ("local", model_name).
    """
    _ensure_whisper_backend()

    # Explicit --model flag: use it directly
    if model:
        return "local", model

    # Auto-detect cached models
    cached = _detect_cached_models()
    if cached:
        best = cached[0]
        console.print(f"[green]Using cached Whisper model:[/green] {best}")
        if len(cached) > 1:
            others = ", ".join(cached[1:])
            console.print(f"[dim]Also available locally: {others}. Use --model to switch.[/dim]")
        return "local", best

    # Nothing cached — show selection menu
    console.print("\nAvailable models:")
    for num, (name, size, desc) in _WHISPER_MODELS.items():
        console.print(f"  {num}. {name:8s} ({size:8s}) — {desc}")

    choice = click.prompt(
        "Select model",
        type=click.IntRange(1, 5),
        default=2,
    )
    model_name = _WHISPER_MODELS[str(choice)][0]
    return "local", model_name


def _transcribe_api_inline(api_key, audio_path, transcript_path, transcript_json_path):
    """Transcribe using OpenAI Whisper API with an explicit api_key.

    Avoids delegating to screencap.engine (whose pydantic-settings singleton
    ignores os.environ changes after import).
    """
    from openai import OpenAI

    client = OpenAI(api_key=api_key)

    with open(audio_path, "rb") as audio_file:
        result = client.audio.transcriptions.create(
            model="whisper-1",
            file=audio_file,
            response_format="verbose_json",
            timestamp_granularities=["segment"],
        )

    transcript = result.text.strip()

    segments = []
    for segment in getattr(result, "segments", []) or []:
        segments.append({
            "start": segment.start,
            "end": segment.end,
            "text": segment.text.strip(),
        })

    # Save files directly (no print() calls — caller controls output)
    transcript_path.write_text(transcript, encoding="utf-8")
    transcript_json_path.write_text(
        json.dumps({"text": transcript, "segments": segments}, indent=2),
        encoding="utf-8",
    )


def _run_api_transcription(api_key, audio_path, transcript_path, transcript_json_path):
    """Try API transcription, retry with new key on auth failure, or fall back to local."""
    while True:
        try:
            with console.status("[bold]Transcribing with OpenAI API ...[/bold]"):
                _transcribe_api_inline(
                    api_key, audio_path, transcript_path, transcript_json_path
                )
            return  # success
        except Exception as e:
            is_auth_error = "401" in str(e) or "invalid_api_key" in str(e)
            if not is_auth_error:
                console.print(f"[red]Transcription failed:[/red] {e}")
                sys.exit(1)

            console.print("[red]Invalid API key.[/red]")
            choice = click.prompt(
                "What would you like to do?\n"
                "  1. Enter a different API key\n"
                "  2. Transcribe locally with Whisper instead\n"
                "  3. Cancel\n"
                "Choice",
                type=click.IntRange(1, 3),
                default=1,
            )

            if choice == 1:
                api_key = click.prompt("OpenAI API key", hide_input=True).strip()
                continue
            elif choice == 2:
                _, model = _select_local_model()
                try:
                    try:
                        import faster_whisper  # noqa: F401
                        from screencap.engine.cli import _transcribe_faster_whisper
                        local_fn = _transcribe_faster_whisper
                    except ImportError:
                        from screencap.engine.cli import _transcribe_local
                        local_fn = _transcribe_local

                    console.print(f"[dim]Using Whisper ({model} model)...[/dim]")
                    local_fn(audio_path, transcript_path, transcript_json_path, model)
                except Exception as exc:
                    console.print(f"[red]Transcription failed:[/red] {exc}")
                    sys.exit(1)
                return
            else:
                console.print("[dim]Transcription cancelled.[/dim]")
                sys.exit(0)
