"""Post-recording LLM-powered auto-naming.

Assembles context from a recording (screenshots, action events, window titles,
transcript) and queries an LLM provider chain to generate a kebab-case slug
and human-readable description. Falls through providers on any failure.
"""

from __future__ import annotations

import base64
import io
import json
import re
import shutil
import sqlite3
import subprocess
from pathlib import Path

from rich.console import Console

console = Console()

# ---------------------------------------------------------------------------
# Prompt
# ---------------------------------------------------------------------------

_PROMPT = """\
You are naming a screen recording. Based on the provided context (audio transcript,
screenshots, application activity, window titles), generate:

1. A short kebab-case slug for the directory name (e.g., "stripe-webhook-debugging")
2. A one-sentence description of what the user was doing

Rules for the slug:
- Lowercase letters, numbers, and hyphens only: [a-z0-9-]+
- 3-60 characters long
- Descriptive but concise (3-5 words)
- No leading/trailing hyphens

Respond with ONLY this JSON (no markdown, no explanation):
{"slug": "example-name", "description": "User was doing X in Y application"}"""

_SLUG_RE = re.compile(r"^[a-z0-9]+(-[a-z0-9]+)*$")


# ---------------------------------------------------------------------------
# Context assembly
# ---------------------------------------------------------------------------

def _sample_screenshots_from_db(db_path: Path, max_count: int = 5) -> list[str]:
    """Extract screenshot PNGs from DB, resize, and return as base64 JPEG strings.

    Samples first, last, and evenly-spaced screenshots.
    """
    try:
        conn = sqlite3.connect(str(db_path))
        cur = conn.cursor()

        # Check which tables exist
        cur.execute("SELECT name FROM sqlite_master WHERE type='table'")
        tables = {row[0] for row in cur.fetchall()}

        if "screenshot" not in tables:
            conn.close()
            return []

        cur.execute("SELECT COUNT(*) FROM screenshot WHERE png_data IS NOT NULL")
        total = cur.fetchone()[0]
        if total == 0:
            conn.close()
            return []

        # Pick indices: first, last, and evenly-spaced
        if total <= max_count:
            indices = list(range(total))
        else:
            indices = [0, total - 1]
            step = (total - 1) / (max_count - 1)
            for i in range(1, max_count - 1):
                idx = int(round(step * i))
                if idx not in indices:
                    indices.append(idx)
            indices = sorted(set(indices))[:max_count]

        # Fetch by ROWID offset
        screenshots_b64 = []
        for idx in indices:
            cur.execute(
                "SELECT png_data FROM screenshot WHERE png_data IS NOT NULL "
                "ORDER BY timestamp LIMIT 1 OFFSET ?",
                (idx,),
            )
            row = cur.fetchone()
            if not row or not row[0]:
                continue

            try:
                from PIL import Image

                img = Image.open(io.BytesIO(row[0]))
                # Resize to 1024px wide, maintaining aspect ratio
                if img.width > 1024:
                    ratio = 1024 / img.width
                    img = img.resize(
                        (1024, int(img.height * ratio)),
                        Image.LANCZOS,
                    )
                # Convert to JPEG
                buf = io.BytesIO()
                img.convert("RGB").save(buf, format="JPEG", quality=80)
                screenshots_b64.append(base64.b64encode(buf.getvalue()).decode())
            except Exception:
                continue

        conn.close()
        return screenshots_b64
    except Exception:
        return []


def _summarize_action_events(db_path: Path, limit: int = 50) -> list[dict]:
    """Extract deduplicated (name, window_title, app) tuples from action events."""
    try:
        conn = sqlite3.connect(str(db_path))
        cur = conn.cursor()

        cur.execute("SELECT name FROM sqlite_master WHERE type='table'")
        tables = {row[0] for row in cur.fetchall()}

        events = []
        seen = set()

        if "action_event" in tables and "window_event" in tables:
            cur.execute(
                "SELECT ae.name, we.title "
                "FROM action_event ae "
                "LEFT JOIN window_event we ON ae.window_event_id = we.id "
                "ORDER BY ae.timestamp "
                "LIMIT 500"
            )
            for row in cur.fetchall():
                key = (row[0], row[1])
                if key not in seen:
                    seen.add(key)
                    events.append({"event_type": row[0], "window_title": row[1]})
                    if len(events) >= limit:
                        break
        elif "action_event" in tables:
            cur.execute(
                "SELECT DISTINCT name FROM action_event ORDER BY timestamp LIMIT ?",
                (limit,),
            )
            for row in cur.fetchall():
                events.append({"event_type": row[0]})

        conn.close()
        return events
    except Exception:
        return []


def _collect_window_titles(db_path: Path) -> list[str]:
    """Extract unique window titles from window_event table."""
    try:
        conn = sqlite3.connect(str(db_path))
        cur = conn.cursor()

        cur.execute("SELECT name FROM sqlite_master WHERE type='table'")
        tables = {row[0] for row in cur.fetchall()}

        titles = []
        if "window_event" in tables:
            cur.execute(
                "SELECT DISTINCT title FROM window_event "
                "WHERE title IS NOT NULL AND title != '' "
                "LIMIT 100"
            )
            titles = [row[0] for row in cur.fetchall()]

        conn.close()
        return titles
    except Exception:
        return []


def _load_transcript(capture_dir: Path, max_words: int = 2000) -> str | None:
    """Load transcript text, truncated to max_words."""
    json_path = capture_dir / "transcript.json"
    txt_path = capture_dir / "transcript.txt"

    text = None
    if json_path.exists():
        try:
            data = json.loads(json_path.read_text())
            text = data.get("text", "")
        except Exception:
            pass

    if not text and txt_path.exists():
        try:
            text = txt_path.read_text().strip()
        except Exception:
            pass

    if not text:
        return None

    words = text.split()
    if len(words) > max_words:
        text = " ".join(words[:max_words]) + " [truncated]"
    return text


def _load_app_versions(capture_dir: Path) -> list[str]:
    """Load app names from app_versions.json if it exists."""
    path = capture_dir / "app_versions.json"
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text())
        return [app.get("name", "") for app in data if app.get("name")]
    except Exception:
        return []


def assemble_context(capture_dir: Path) -> dict:
    """Assemble all available context for the LLM from a recording directory."""
    from screencap.catalog import find_db

    db_path = find_db(capture_dir)

    context: dict = {}

    if db_path:
        screenshots = _sample_screenshots_from_db(db_path)
        if screenshots:
            context["screenshots_b64"] = screenshots

        events = _summarize_action_events(db_path)
        if events:
            context["action_events"] = events

        titles = _collect_window_titles(db_path)
        if titles:
            context["window_titles"] = titles

    transcript = _load_transcript(capture_dir)
    if transcript:
        context["transcript"] = transcript

    apps = _load_app_versions(capture_dir)
    if apps:
        context["running_apps"] = apps

    return context


# ---------------------------------------------------------------------------
# Output validation
# ---------------------------------------------------------------------------

def _parse_llm_response(text: str) -> dict | None:
    """Parse JSON from LLM response. Handles markdown fences."""
    text = text.strip()

    # Try direct JSON parse
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass

    # Try extracting from markdown fences
    match = re.search(r"```(?:json)?\s*\n?(.*?)\n?```", text, re.DOTALL)
    if match:
        try:
            return json.loads(match.group(1).strip())
        except json.JSONDecodeError:
            pass

    # Try finding a JSON object in the text
    match = re.search(r"\{[^}]+\}", text)
    if match:
        try:
            return json.loads(match.group(0))
        except json.JSONDecodeError:
            pass

    return None


def validate_slug(slug: str) -> bool:
    """Validate that a slug matches the required format."""
    if not isinstance(slug, str):
        return False
    if len(slug) < 3 or len(slug) > 60:
        return False
    return bool(_SLUG_RE.match(slug))


# ---------------------------------------------------------------------------
# LLM Providers
# ---------------------------------------------------------------------------

def _build_text_prompt(context: dict) -> str:
    """Build a text-only prompt from context (for CLI providers and non-vision APIs)."""
    parts = [_PROMPT, ""]

    if context.get("transcript"):
        parts.append(f"## Audio Transcript\n{context['transcript']}")

    if context.get("window_titles"):
        parts.append("## Window Titles\n" + "\n".join(f"- {t}" for t in context["window_titles"]))

    if context.get("action_events"):
        lines = []
        for e in context["action_events"][:30]:
            line = e.get("event_type", "")
            if e.get("window_title"):
                line += f" (in: {e['window_title']})"
            lines.append(f"- {line}")
        parts.append("## Action Events\n" + "\n".join(lines))

    if context.get("running_apps"):
        parts.append("## Running Apps\n" + ", ".join(context["running_apps"]))

    if context.get("screenshots_b64"):
        parts.append(f"\n[{len(context['screenshots_b64'])} screenshots provided as images]")

    return "\n\n".join(parts)


def _try_claude_cli(context: dict) -> dict | None:
    """Try naming via the `claude` CLI."""
    if not shutil.which("claude"):
        return None

    prompt = _build_text_prompt(context)
    try:
        result = subprocess.run(
            ["claude", "--print", "--model", "claude-haiku-4-5-20251001", prompt],
            capture_output=True,
            text=True,
            timeout=30,
        )
        if result.returncode != 0:
            return None
        parsed = _parse_llm_response(result.stdout)
        if parsed and validate_slug(parsed.get("slug", "")):
            return parsed
    except (subprocess.TimeoutExpired, OSError):
        pass
    return None


def _try_chatgpt_cli(context: dict) -> dict | None:
    """Try naming via the `chatgpt` CLI."""
    if not shutil.which("chatgpt"):
        return None

    prompt = _build_text_prompt(context)
    try:
        result = subprocess.run(
            ["chatgpt", prompt],
            capture_output=True,
            text=True,
            timeout=30,
        )
        if result.returncode != 0:
            return None
        parsed = _parse_llm_response(result.stdout)
        if parsed and validate_slug(parsed.get("slug", "")):
            return parsed
    except (subprocess.TimeoutExpired, OSError):
        pass
    return None


def _try_anthropic_api(context: dict) -> dict | None:
    """Try naming via the Anthropic API using requests."""
    import os

    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        return None

    import requests

    # Build messages with vision support
    content = []

    # Add screenshots as images
    for b64_img in context.get("screenshots_b64", []):
        content.append({
            "type": "image",
            "source": {
                "type": "base64",
                "media_type": "image/jpeg",
                "data": b64_img,
            },
        })

    # Add text prompt
    content.append({"type": "text", "text": _build_text_prompt(context)})

    try:
        resp = requests.post(
            "https://api.anthropic.com/v1/messages",
            headers={
                "x-api-key": api_key,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json",
            },
            json={
                "model": "claude-haiku-4-5-20251001",
                "max_tokens": 256,
                "messages": [{"role": "user", "content": content}],
            },
            timeout=30,
        )
        if resp.status_code != 200:
            return None
        data = resp.json()
        text = data.get("content", [{}])[0].get("text", "")
        parsed = _parse_llm_response(text)
        if parsed and validate_slug(parsed.get("slug", "")):
            return parsed
    except Exception:
        pass
    return None


def _try_openai_api(context: dict) -> dict | None:
    """Try naming via the OpenAI API."""
    import os

    api_key = os.environ.get("OPENAI_API_KEY")
    if not api_key:
        return None

    try:
        from openai import OpenAI

        client = OpenAI(api_key=api_key)

        # Build messages with vision support
        content = []

        for b64_img in context.get("screenshots_b64", []):
            content.append({
                "type": "image_url",
                "image_url": {
                    "url": f"data:image/jpeg;base64,{b64_img}",
                    "detail": "low",
                },
            })

        content.append({"type": "text", "text": _build_text_prompt(context)})

        response = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[{"role": "user", "content": content}],
            max_tokens=256,
            timeout=30,
        )
        text = response.choices[0].message.content or ""
        parsed = _parse_llm_response(text)
        if parsed and validate_slug(parsed.get("slug", "")):
            return parsed
    except Exception:
        pass
    return None


def _try_ollama(context: dict) -> dict | None:
    """Try naming via Ollama's OpenAI-compatible API."""
    import os

    import requests

    host = os.environ.get("OLLAMA_HOST", "http://localhost:11434")

    # Check if Ollama is running
    try:
        resp = requests.get(f"{host}/api/tags", timeout=5)
        if resp.status_code != 200:
            return None
    except Exception:
        return None

    # Find a vision model
    models = resp.json().get("models", [])
    vision_model = None
    # Prefer known vision models
    vision_keywords = ["qwen3-vl", "qwen2-vl", "llava", "bakllava", "moondream"]
    for model in models:
        name = model.get("name", "")
        for kw in vision_keywords:
            if kw in name.lower():
                vision_model = name
                break
        if vision_model:
            break

    if not vision_model:
        # Check if any model is available (fall back to text-only)
        if models:
            vision_model = models[0]["name"]
        else:
            console.print(
                "[dim]Tip: run 'ollama pull qwen3-vl:4b' for local auto-naming[/dim]"
            )
            return None

    # Build request
    messages_content = []

    # Only include images if we have a known vision model
    has_vision = any(kw in vision_model.lower() for kw in vision_keywords)
    if has_vision:
        for b64_img in context.get("screenshots_b64", []):
            messages_content.append({
                "type": "image_url",
                "image_url": {
                    "url": f"data:image/jpeg;base64,{b64_img}",
                },
            })

    messages_content.append({"type": "text", "text": _build_text_prompt(context)})

    try:
        resp = requests.post(
            f"{host}/v1/chat/completions",
            json={
                "model": vision_model,
                "messages": [{"role": "user", "content": messages_content}],
                "max_tokens": 256,
            },
            timeout=60,
        )
        if resp.status_code != 200:
            return None
        data = resp.json()
        text = data.get("choices", [{}])[0].get("message", {}).get("content", "")
        parsed = _parse_llm_response(text)
        if parsed and validate_slug(parsed.get("slug", "")):
            return parsed
    except Exception:
        pass
    return None


# ---------------------------------------------------------------------------
# Provider chain
# ---------------------------------------------------------------------------

_CLOUD_PROVIDERS = {"claude_cli", "chatgpt_cli", "anthropic_api", "openai_api"}

_PROVIDER_ORDER = [
    ("claude_cli", "claude CLI"),
    ("chatgpt_cli", "chatgpt CLI"),
    ("anthropic_api", "Anthropic API"),
    ("openai_api", "OpenAI API"),
    ("ollama", "Ollama (local)"),
]

_PROVIDER_FNS = {
    "claude_cli": "_try_claude_cli",
    "chatgpt_cli": "_try_chatgpt_cli",
    "anthropic_api": "_try_anthropic_api",
    "openai_api": "_try_openai_api",
    "ollama": "_try_ollama",
}


def _run_provider_chain(context: dict, local_only: bool = False) -> dict | None:
    """Try each provider in order until one succeeds."""
    import screencap.namer as _self

    _cloud_notice_shown = False

    for provider_id, provider_name in _PROVIDER_ORDER:
        if local_only and provider_id in _CLOUD_PROVIDERS:
            continue

        if not local_only and provider_id in _CLOUD_PROVIDERS and not _cloud_notice_shown:
            console.print(
                f"[dim]Note: sending recording context to {provider_name} for naming. "
                "Use --local-only to restrict to Ollama.[/dim]"
            )
            _cloud_notice_shown = True

        fn = getattr(_self, _PROVIDER_FNS[provider_id])
        result = fn(context)
        if result:
            return result

    return None


# ---------------------------------------------------------------------------
# DB update
# ---------------------------------------------------------------------------

def _update_task_description(db_path: Path, description: str) -> None:
    """Update task_description in the recording table."""
    try:
        conn = sqlite3.connect(str(db_path))
        cur = conn.cursor()

        cur.execute("SELECT name FROM sqlite_master WHERE type='table'")
        tables = {row[0] for row in cur.fetchall()}

        if "recording" in tables:
            # Check if task_description column exists
            cur.execute("PRAGMA table_info(recording)")
            columns = {row[1] for row in cur.fetchall()}
            if "task_description" in columns:
                cur.execute(
                    "UPDATE recording SET task_description = ?",
                    (description,),
                )
        elif "capture" in tables:
            cur.execute("PRAGMA table_info(capture)")
            columns = {row[1] for row in cur.fetchall()}
            if "task_description" in columns:
                cur.execute(
                    "UPDATE capture SET task_description = ?",
                    (description,),
                )

        conn.commit()
        conn.close()
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def auto_name(
    capture_dir: Path,
    local_only: bool = False,
    skip_rename: bool = False,
) -> Path:
    """Auto-name a recording directory using LLM.

    Args:
        capture_dir: Path to the recording directory.
        local_only: If True, only use local LLM providers (Ollama).
        skip_rename: If True, don't rename the directory (e.g., --output was used).

    Returns:
        The final directory path (may be renamed).
    """
    context = assemble_context(capture_dir)

    if not context:
        console.print("[dim]No context available for auto-naming.[/dim]")
        return capture_dir

    result = _run_provider_chain(context, local_only=local_only)

    if not result:
        console.print("[dim]Auto-naming skipped — no LLM provider available.[/dim]")
        return capture_dir

    slug = result["slug"]
    description = result.get("description", "")

    # Update DB with description
    from screencap.catalog import find_db

    db_path = find_db(capture_dir)
    if db_path and description:
        _update_task_description(db_path, description)

    if skip_rename:
        return capture_dir

    # Rename directory
    recordings_dir = capture_dir.parent
    final_name = slug

    # Handle collision
    if (recordings_dir / final_name).exists():
        counter = 2
        while (recordings_dir / f"{final_name}-{counter}").exists():
            counter += 1
        final_name = f"{final_name}-{counter}"

    final_dir = recordings_dir / final_name
    capture_dir.rename(final_dir)

    console.print(f"[green]Named:[/green] {final_name}")
    if description:
        console.print(f"[dim]{description}[/dim]")

    return final_dir
