"""Generate interactive HTML viewer for capture recordings.

Creates a self-contained HTML file with timeline navigation,
frame viewing, event list, and audio playback.
"""

from __future__ import annotations

import base64
import json
from io import BytesIO
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from PIL import Image

    from openadapt_capture.capture import CaptureSession


def create_html(
    capture_or_path: "CaptureSession | str | Path",
    output: str | Path | None = None,
    max_events: int | None = None,
    include_audio: bool = True,
    frame_scale: float = 1.0,
    frame_quality: int = 85,
) -> str:
    """Generate an interactive HTML viewer for a capture recording.

    Args:
        capture_or_path: CaptureSession object or path to capture directory.
        output: Output path for HTML file. If None, returns HTML string.
        max_events: Maximum events to include (None for all).
        include_audio: Whether to include audio playback.
        frame_scale: Scale factor for embedded frames.
        frame_quality: JPEG quality for embedded frames (1-100).

    Returns:
        HTML string if output is None, otherwise None after writing file.
    """
    from openadapt_capture.capture import CaptureSession

    # Load capture if path provided
    if isinstance(capture_or_path, (str, Path)):
        capture = CaptureSession.load(capture_or_path)
        capture_path = Path(capture_or_path)
    else:
        capture = capture_or_path
        capture_path = capture.capture_dir

    # Get capture metadata
    capture_id = capture.id
    duration = capture.duration or 0
    screen_width, screen_height = capture.screen_size
    pixel_ratio = getattr(capture, "pixel_ratio", 1.0)
    audio_start_time = getattr(capture, "audio_start_time", None)

    # Get all actions including moves for timeline coverage.
    # We keep ALL non-move actions (clicks, scrolls, keys) and sample moves
    # at ~3/second to fill gaps and provide smoother playback.
    all_actions = list(capture.actions(include_moves=True))
    actions = []
    last_move_time = -999
    for a in all_actions:
        evt_type = a.type if isinstance(a.type, str) else a.type.value
        if "move" in evt_type.lower():
            # Keep at most 3 moves per second for smoother visual coverage
            if a.timestamp - last_move_time >= 0.33:
                actions.append(a)
                last_move_time = a.timestamp
        else:
            actions.append(a)

    if max_events is not None and len(actions) > max_events:
        # Sample evenly
        step = len(actions) / max_events
        indices = [int(i * step) for i in range(max_events)]
        actions = [actions[i] for i in indices]

    # Prepare frame data
    frames_data = []
    events_data = []

    # Use the actual recording start as time reference, not the first action.
    # The recorder has a startup delay (3-10s) before the first event, but the
    # video starts at recording time so we must use the same reference.
    recording_start = capture._recording.timestamp
    video_start = getattr(capture._recording, "video_start_time", None)

    if audio_start_time:
        start_time = audio_start_time
    elif video_start:
        start_time = video_start
    else:
        start_time = recording_start

    # Add a "start" marker with the earliest available video frame
    if capture.video_path:
        # Try to get the very first frame from the video
        first_frame = capture.get_frame_at(start_time, tolerance=10.0)
        if first_frame:
            frame_b64 = _image_to_base64(first_frame, scale=frame_scale, quality=frame_quality)
            frames_data.append({
                "index": 0,
                "time": 0.0,
                "image": frame_b64,
            })
            events_data.append({
                "index": 0,
                "time": 0.0,
                "type": "recording.start",
            })

    # Offset for indices if we added start marker
    idx_offset = len(frames_data)

    for i, action in enumerate(actions):
        idx = i + idx_offset
        rel_time = action.timestamp - start_time
        event_type = action.type if isinstance(action.type, str) else action.type.value

        # Encode screenshot
        screenshot = action.screenshot
        if screenshot is not None:
            frame_b64 = _image_to_base64(screenshot, scale=frame_scale, quality=frame_quality)
        else:
            frame_b64 = ""

        frames_data.append({
            "index": idx,
            "time": rel_time,
            "image": frame_b64,
        })

        # Event data
        event_dict = {
            "index": idx,
            "time": rel_time,
            "type": event_type,
            "x": getattr(action, "x", None),
            "y": getattr(action, "y", None),
        }

        # Add type-specific fields
        if hasattr(action, "text"):
            event_dict["text"] = action.text
        if hasattr(action, "keys"):
            keys = action.keys
            if keys:
                event_dict["keys"] = "+".join(keys)
        # For raw key events (e.g. media keys), include key_name directly
        if event_dict.get("text") is None and event_dict.get("keys") is None:
            key_name = getattr(action.event, "key_name", None)
            key_char = getattr(action.event, "key_char", None)
            key_vk = getattr(action.event, "key_vk", None)
            key_label = key_name or key_char or key_vk
            if key_label:
                event_dict["keys"] = str(key_label)
        if hasattr(action, "button"):
            event_dict["button"] = str(action.button)
        # dx/dy for drags and scrolls
        if hasattr(action.event, "dx"):
            event_dict["dx"] = action.event.dx
            event_dict["dy"] = action.event.dy
        # Gesture-specific fields
        if hasattr(action.event, "magnification"):
            event_dict["magnification"] = action.event.magnification
        if hasattr(action.event, "rotation"):
            event_dict["rotation"] = action.event.rotation
        # Pressure data
        if hasattr(action.event, "pressure") and action.event.pressure is not None:
            event_dict["pressure"] = action.event.pressure
        # Modifier flags (human-readable in JS)
        mf = getattr(action.event, "modifier_flags", None)
        if mf is not None and mf != 0:
            event_dict["modifiers"] = mf
        # Scroll enrichment
        if hasattr(action.event, "scroll_phase") and action.event.scroll_phase is not None:
            event_dict["scroll_phase"] = action.event.scroll_phase
        if hasattr(action.event, "momentum_phase") and action.event.momentum_phase is not None:
            event_dict["momentum_phase"] = action.event.momentum_phase
        if hasattr(action.event, "is_continuous") and action.event.is_continuous is not None:
            event_dict["is_continuous"] = action.event.is_continuous
        # Drag path with per-point pressure for variable-thickness rendering
        if event_type == "mouse.drag" and hasattr(action.event, "children"):
            path = []
            for child in action.event.children:
                if hasattr(child, "x") and hasattr(child, "y"):
                    point = {"x": child.x, "y": child.y}
                    if hasattr(child, "pressure") and child.pressure is not None:
                        point["p"] = child.pressure
                    path.append(point)
            if path:
                event_dict["path"] = path

        # Accessibility data
        if getattr(action, "window_title", None):
            event_dict["window"] = action.window_title
        if getattr(action, "window_data", None):
            # Extract window document/tab title from the state data
            wd = action.window_data
            data = wd.get("data", {}) if isinstance(wd, dict) else {}
            if isinstance(data, dict) and data.get("AXTitle"):
                event_dict["windowTitle"] = data["AXTitle"]
        if getattr(action, "element_state", None):
            event_dict["element"] = action.element_state

        events_data.append(event_dict)

    # Prepare audio data and get audio duration
    audio_b64 = ""
    audio_type = ""
    audio_duration = 0.0
    transcript = ""
    if include_audio:
        audio_path = capture_path / "audio.flac"
        if audio_path.exists():
            with open(audio_path, "rb") as f:
                audio_b64 = base64.b64encode(f.read()).decode("utf-8")
            audio_type = "audio/flac"
            # Get audio duration using soundfile
            try:
                import soundfile as sf
                info = sf.info(str(audio_path))
                audio_duration = info.duration
            except Exception:
                pass
            # Load transcript if exists (prefer JSON with timestamps)
            transcript_json_path = capture_path / "transcript.json"
            transcript_path = capture_path / "transcript.txt"
            if transcript_json_path.exists():
                transcript = transcript_json_path.read_text(encoding="utf-8")
            elif transcript_path.exists():
                # Wrap plain text in JSON format
                plain_text = transcript_path.read_text(encoding="utf-8")
                transcript = json.dumps({"text": plain_text, "segments": []})
        else:
            # Try other formats
            for ext, mime in [(".mp3", "audio/mpeg"), (".wav", "audio/wav"), (".ogg", "audio/ogg")]:
                alt_path = capture_path / f"audio{ext}"
                if alt_path.exists():
                    with open(alt_path, "rb") as f:
                        audio_b64 = base64.b64encode(f.read()).decode("utf-8")
                    audio_type = mime
                    break

    # Calculate effective duration as max of audio duration and last event time
    last_event_time = events_data[-1]["time"] if events_data else 0
    effective_duration = max(audio_duration, duration, last_event_time)

    # Add an "end" marker at the end of the recording
    if effective_duration > 0 and capture.video_path:
        # Get the last frame from the video
        end_timestamp = start_time + effective_duration
        last_frame = capture.get_frame_at(end_timestamp, tolerance=5.0)
        if last_frame:
            end_idx = len(frames_data)
            frame_b64 = _image_to_base64(last_frame, scale=frame_scale, quality=frame_quality)
            frames_data.append({
                "index": end_idx,
                "time": effective_duration,
                "image": frame_b64,
            })
            events_data.append({
                "index": end_idx,
                "time": effective_duration,
                "type": "recording.end",
            })

    # Generate HTML
    html = _generate_html(
        capture_id=capture_id,
        duration=effective_duration,
        frames_data=frames_data,
        events_data=events_data,
        audio_b64=audio_b64,
        audio_type=audio_type,
        screen_width=screen_width,
        screen_height=screen_height,
        pixel_ratio=pixel_ratio,
        transcript=transcript,
    )

    if output is not None:
        output = Path(output)
        output.write_text(html, encoding="utf-8")
        return None
    else:
        return html


def _image_to_base64(image: "Image.Image", scale: float = 1.0, quality: int = 85) -> str:
    """Convert PIL Image to base64 JPEG string."""
    from PIL import Image

    if scale != 1.0:
        new_size = (int(image.width * scale), int(image.height * scale))
        image = image.resize(new_size, Image.Resampling.LANCZOS)

    # Convert to RGB if needed
    if image.mode in ("RGBA", "P"):
        bg = Image.new("RGB", image.size, (255, 255, 255))
        if image.mode == "RGBA":
            bg.paste(image, mask=image.split()[3])
        else:
            bg.paste(image)
        image = bg
    elif image.mode != "RGB":
        image = image.convert("RGB")

    buf = BytesIO()
    image.save(buf, format="JPEG", quality=quality)
    buf.seek(0)
    return base64.b64encode(buf.read()).decode("utf-8")


def _generate_html(
    capture_id: str,
    duration: float,
    frames_data: list[dict],
    events_data: list[dict],
    audio_b64: str,
    audio_type: str,
    screen_width: int,
    screen_height: int,
    pixel_ratio: float,
    transcript: str = "",
) -> str:
    """Generate the complete HTML viewer content."""
    minutes = int(duration // 60)
    seconds = duration % 60
    duration_str = f"{minutes}:{seconds:05.2f}"

    frames_json = json.dumps(frames_data)
    events_json = json.dumps(events_data)

    # Conditional HTML snippets (plain strings, not f-strings)
    audio_html = ""
    if audio_b64:
        audio_html = (
            '<div class="audio-ctrl">'
            '<label>Vol</label>'
            '<input type="range" id="volume" min="0" max="1" step="0.1" value="0.5">'
            '<label style="margin-left:4px"><input type="checkbox" id="mute"> Mute</label>'
            '</div>'
        )

    transcript_html = ""
    if transcript:
        transcript_html = (
            '<div class="card">'
            '<div class="card-label">Transcript</div>'
            '<div class="transcript-body" id="transcript-content"></div>'
            '</div>'
        )

    html = f'''<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>ScreenCap \u2014 {capture_id}</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Outfit:wght@300;400;500;600;700&family=JetBrains+Mono:wght@400;500&display=swap" rel="stylesheet">
<style>
:root {{
    --bg-0:#080c1c; --bg-1:#0b1121; --bg-sidebar:#090e1e; --bg-2:#111827; --bg-3:#1e293b;
    --border:rgba(255,255,255,0.06); --border-hi:rgba(255,255,255,0.12);
    --text-1:#f0f4ff; --text-2:rgba(210,225,255,0.7); --text-3:rgba(170,195,235,0.55);
    --accent:#60a5fa; --accent-dim:rgba(96,165,250,0.14); --accent-hover:#3b82f6;
    --ev-click:#f472b6; --ev-drag:#22d3ee; --ev-scroll:#a78bfa; --ev-type:#60a5fa; --ev-move:rgba(170,195,235,0.4); --ev-magnify:#22d3ee; --ev-rotate:#818cf8; --ev-smart-magnify:#22d3ee;
    --glass-bg:rgba(255,255,255,0.07); --glass-border:rgba(255,255,255,0.12); --glass-bg-hover:rgba(255,255,255,0.11);
    --accent-cyan:#22d3ee; --accent-cyan-dim:rgba(34,211,238,0.12); --accent-indigo:#818cf8; --accent-indigo-dim:rgba(129,140,248,0.12); --accent-violet:#a78bfa; --accent-violet-dim:rgba(167,139,250,0.1); --accent-rose:#f472b6; --accent-rose-dim:rgba(244,114,182,0.1);
    --radius:12px; --radius-sm:8px; --radius-lg:16px;
    --shadow-1:0 2px 16px rgba(0,0,0,0.4); --shadow-2:0 4px 24px rgba(0,0,0,0.5),0 0 0 1px rgba(255,255,255,0.03);
    --ease:cubic-bezier(0.22,1,0.36,1);
}}
*{{ box-sizing:border-box; margin:0; padding:0; }}
body {{ font-family:'Outfit',-apple-system,BlinkMacSystemFont,sans-serif; background:var(--bg-0); color:var(--text-1); height:100vh; overflow:hidden; display:flex; flex-direction:column; line-height:1.4; }}

/* Layout */
.app {{ flex:1; display:flex; min-height:0; }}

/* Left Panel */
.panel-left {{ width:260px; background:var(--bg-sidebar); border-radius:0 var(--radius-lg) var(--radius-lg) 0; display:flex; flex-direction:column; flex-shrink:0; box-shadow:var(--shadow-1); overflow:hidden; }}
.panel-header {{ padding:14px 16px; display:flex; justify-content:space-between; align-items:center; border-bottom:1px solid var(--border); }}
.brand {{ font-weight:600; font-size:0.85rem; letter-spacing:-0.01em; color:var(--text-2); }}
.rec-id {{ font-family:'JetBrains Mono',monospace; font-size:0.65rem; color:var(--text-3); margin-top:2px; }}
.ev-count {{ font-family:'JetBrains Mono',monospace; font-size:0.7rem; color:var(--text-3); background:var(--bg-3); padding:2px 10px; border-radius:20px; font-variant-numeric:tabular-nums; }}
.events-list {{ flex:1; overflow-y:auto; padding:6px; scrollbar-width:thin; scrollbar-color:rgba(255,255,255,0.1) transparent; -webkit-mask-image:linear-gradient(to bottom,transparent 0px,#000 12px,#000 calc(100% - 12px),transparent 100%); mask-image:linear-gradient(to bottom,transparent 0px,#000 12px,#000 calc(100% - 12px),transparent 100%); }}
.events-list::-webkit-scrollbar {{ width:6px; }}
.events-list::-webkit-scrollbar-track {{ background:transparent; }}
.events-list::-webkit-scrollbar-thumb {{ background:rgba(255,255,255,0.1); border-radius:3px; }}
.ev-item {{ display:flex; align-items:center; gap:10px; padding:7px 10px; border-radius:var(--radius-sm); cursor:pointer; transition:all 0.2s var(--ease); margin-bottom:1px; }}
.ev-item:hover {{ background:rgba(255,255,255,0.04); transform:translateX(2px); }}
.ev-item.active {{ background:var(--accent-dim); box-shadow:0 0 0 1px rgba(96,165,250,0.2); }}
.ev-icon {{ width:24px; height:24px; border-radius:var(--radius-sm); flex-shrink:0; display:flex; align-items:center; justify-content:center; font-size:0.72rem; background:rgba(255,255,255,0.03); }}
.ev-body {{ flex:1; min-width:0; }}
.ev-label {{ font-size:0.8rem; font-weight:500; display:block; white-space:nowrap; overflow:hidden; text-overflow:ellipsis; }}
.ev-desc {{ font-family:'JetBrains Mono',monospace; font-size:0.65rem; color:var(--text-3); display:block; white-space:nowrap; overflow:hidden; text-overflow:ellipsis; }}
.ev-time {{ font-family:'JetBrains Mono',monospace; font-size:0.65rem; color:var(--text-3); flex-shrink:0; }}
.ev-item.active .ev-label {{ color:var(--accent); }}
.ev-item.active .ev-time {{ color:var(--accent); }}
.ev-item.active .ev-desc {{ color:var(--text-2); }}

/* Center */
.center {{ flex:1; display:flex; flex-direction:column; align-items:center; justify-content:center; background:transparent; min-width:0; padding:12px; gap:10px; }}
.frame-container {{ position:relative; flex:1; min-height:0; max-width:100%; display:flex; align-items:center; justify-content:center; }}
.frame-container img {{ max-width:100%; max-height:100%; object-fit:contain; border-radius:var(--radius); display:block; }}
.frame-container canvas {{ position:absolute; top:0; left:0; pointer-events:none; border-radius:var(--radius); }}
.frame-badge {{ position:absolute; top:12px; right:12px; background:rgba(0,0,0,0.4); backdrop-filter:blur(20px); -webkit-backdrop-filter:blur(20px); padding:6px 14px; border-radius:var(--radius-sm); font-family:'JetBrains Mono',monospace; font-size:0.72rem; color:rgba(255,255,255,0.7); border:1px solid rgba(255,255,255,0.06); font-variant-numeric:tabular-nums; }}

/* Right Panel */
.panel-right {{ width:300px; background:var(--bg-sidebar); border-radius:var(--radius-lg) 0 0 var(--radius-lg); display:flex; flex-direction:column; flex-shrink:0; overflow-y:auto; padding:10px; gap:8px; box-shadow:var(--shadow-1); scrollbar-width:thin; scrollbar-color:rgba(255,255,255,0.1) transparent; }}
.panel-right::-webkit-scrollbar {{ width:6px; }}
.panel-right::-webkit-scrollbar-track {{ background:transparent; }}
.panel-right::-webkit-scrollbar-thumb {{ background:rgba(255,255,255,0.1); border-radius:3px; }}
.ctx-header {{ display:flex; justify-content:space-between; align-items:center; padding:12px 14px; background:var(--bg-2); border:none; border-radius:var(--radius); }}
.ctx-type {{ font-weight:800; font-size:1rem; text-transform:uppercase; letter-spacing:0.02em; }}
.ctx-time {{ font-family:'JetBrains Mono',monospace; font-size:0.72rem; color:var(--text-2); }}
.card {{ background:var(--glass-bg); border:1px solid var(--glass-border); border-radius:var(--radius); overflow:hidden; }}
.card-label {{ font-size:0.65rem; font-weight:600; text-transform:uppercase; letter-spacing:0.06em; color:var(--text-3); padding:10px 14px 6px; }}
.card-head {{ display:flex; justify-content:space-between; align-items:center; padding:8px 14px 6px; }}
.card-head .card-label {{ padding:0; }}
.card-body {{ padding:6px 14px 12px; }}
#app-card {{ background:var(--accent-indigo-dim); }}
.app-name {{ font-size:1rem; font-weight:600; color:var(--text-1); }}
.app-window {{ font-size:0.75rem; color:var(--text-2); margin-top:1px; white-space:nowrap; overflow:hidden; text-overflow:ellipsis; }}
#element-card {{ background:var(--accent-rose-dim); }}
.el-field {{ display:flex; align-items:baseline; padding:3px 0; }}
.el-key {{ font-size:0.65rem; text-transform:uppercase; letter-spacing:0.04em; color:var(--text-3); min-width:50px; flex-shrink:0; }}
.el-val {{ font-family:'JetBrains Mono',monospace; font-size:0.75rem; color:var(--text-1); word-break:break-word; }}
.el-val.role {{ color:var(--accent-cyan); font-weight:600; }}
.el-val.muted {{ color:var(--text-2); font-weight:400; }}
.more-toggle {{ display:flex; align-items:center; gap:6px; padding-top:6px; margin-top:4px; border-top:1px solid var(--border); cursor:pointer; font-size:0.65rem; color:var(--text-3); user-select:none; transition:color 0.12s; }}
.more-toggle:hover {{ color:var(--accent); }}
.more-toggle .chevron {{ display:inline-block; transition:transform 0.2s; font-size:0.5rem; }}
.more-toggle.expanded .chevron {{ transform:rotate(90deg); }}
.more-content {{ display:none; margin-top:6px; font-family:'JetBrains Mono',monospace; font-size:0.65rem; color:var(--text-2); max-height:140px; overflow-y:auto; padding:6px; background:var(--bg-3); border-radius:var(--radius-sm); }}
.more-content.visible {{ display:block; }}
.more-content .a11y-key {{ color:var(--accent); }}
.more-content .a11y-val {{ color:var(--accent-cyan); }}
.detail-row {{ display:flex; padding:3px 0; line-height:1.6; }}
.detail-key {{ font-size:0.65rem; text-transform:uppercase; letter-spacing:0.03em; color:var(--text-3); min-width:55px; flex-shrink:0; font-weight:500; }}
.detail-val {{ font-family:'JetBrains Mono',monospace; font-size:0.75rem; color:var(--text-2); }}
.detail-val.type-val {{ color:var(--text-1); font-weight:500; }}
.detail-val.time-val {{ color:var(--text-3); }}
.transcript-body {{ padding:8px 14px; font-size:0.75rem; line-height:1.8; color:var(--text-2); max-height:120px; overflow-y:auto; }}
.transcript-segment {{ display:inline; cursor:pointer; padding:1px 4px; border-radius:3px; transition:all 0.12s; }}
.transcript-segment:hover {{ background:var(--bg-3); color:var(--text-1); }}
.transcript-segment.active {{ background:var(--accent-dim); color:var(--accent); }}
.transcript-time {{ font-family:'JetBrains Mono',monospace; font-size:0.58rem; color:var(--text-3); margin-right:3px; }}
.empty-msg {{ font-size:0.75rem; color:var(--text-3); font-style:italic; padding:4px 0; }}
.empty-state {{ display:flex; flex-direction:column; align-items:center; justify-content:center; padding:16px 0; gap:6px; }}
.empty-state-icon {{ font-size:1.2rem; opacity:0.3; }}
.empty-state-text {{ font-size:0.72rem; color:var(--text-3); }}

/* Player Bar */
.player-bar {{ background:var(--bg-1); border-radius:var(--radius); padding:10px 16px; flex-shrink:0; width:100%; border:1px solid rgba(255,255,255,0.04); }}
.player-main {{ display:flex; align-items:center; gap:12px; }}
.player-nav {{ display:flex; align-items:center; gap:2px; background:rgba(255,255,255,0.03); border-radius:var(--radius); padding:3px; }}
.player-nav button {{ background:transparent; border:none; color:var(--text-2); width:30px; height:30px; border-radius:var(--radius-sm); cursor:pointer; font-size:0.75rem; display:flex; align-items:center; justify-content:center; transition:all 0.2s var(--ease); }}
.player-nav button:hover {{ background:rgba(255,255,255,0.06); color:var(--text-1); }}
.player-nav button:active {{ transform:scale(0.92); }}
.player-nav button:disabled {{ opacity:0.25; cursor:not-allowed; pointer-events:none; }}
.player-nav .play-btn {{ background:var(--accent); border:none; color:var(--bg-0); width:36px; height:36px; border-radius:50%; font-weight:700; font-size:0.85rem; }}
.player-nav .play-btn:hover {{ background:var(--accent-hover); transform:scale(1.08); box-shadow:0 0 20px rgba(96,165,250,0.3); }}
.player-nav .play-btn:active {{ transform:scale(0.94); }}
.step-display {{ font-family:'JetBrains Mono',monospace; font-size:0.72rem; color:var(--text-2); font-weight:500; min-width:90px; text-align:center; line-height:36px; font-variant-numeric:tabular-nums; }}
.timeline {{ flex:1; height:4px; background:rgba(255,255,255,0.06); border-radius:2px; cursor:pointer; position:relative; transition:height 0.15s var(--ease); }}
.timeline:hover {{ height:6px; }}
.timeline-progress {{ height:100%; background:linear-gradient(90deg,var(--accent),var(--accent-hover)); border-radius:2px; width:0%; transition:width 0.06s linear; }}
.timeline-markers {{ position:absolute; top:50%; left:0; right:0; transform:translateY(-50%); height:6px; }}
.timeline-marker {{ position:absolute; width:3px; height:3px; border-radius:50%; background:rgba(255,255,255,0.18); transform:translateX(-50%); top:50%; margin-top:-1.5px; }}
.time-display {{ font-family:'JetBrains Mono',monospace; font-size:0.72rem; color:var(--text-2); min-width:105px; text-align:right; font-variant-numeric:tabular-nums; }}
.time-sep {{ color:var(--text-3); margin:0 2px; }}
.player-secondary {{ display:flex; align-items:center; gap:14px; margin-top:8px; padding-top:8px; border-top:1px solid var(--border); }}
.toggle {{ display:flex; align-items:center; gap:7px; cursor:pointer; user-select:none; }}
.toggle input {{ display:none; }}
.toggle-track {{ width:28px; height:16px; background:var(--bg-3); border:1px solid rgba(255,255,255,0.06); border-radius:8px; position:relative; transition:all 0.2s var(--ease); }}
.toggle-track::after {{ content:''; width:10px; height:10px; background:var(--text-3); border-radius:50%; position:absolute; top:2px; left:2px; transition:all 0.2s var(--ease); }}
.toggle input:checked+.toggle-track {{ background:rgba(52,199,89,0.2); border-color:rgba(52,199,89,0.4); }}
.toggle input:checked+.toggle-track::after {{ background:#34c759; left:16px; }}
.toggle-label {{ font-size:0.72rem; color:var(--text-2); }}
.audio-ctrl {{ display:flex; align-items:center; gap:6px; }}
.audio-ctrl label {{ font-size:0.72rem; color:var(--text-2); }}
.audio-ctrl input[type="range"] {{ width:70px; accent-color:var(--accent); }}
.player-spacer {{ flex:1; }}
.copy-btn {{ background:transparent; border:1px solid transparent; color:var(--text-3); padding:4px 10px; border-radius:var(--radius-sm); cursor:pointer; font-size:0.65rem; font-family:'Outfit',sans-serif; text-transform:uppercase; letter-spacing:0.05em; font-weight:500; transition:all 0.15s var(--ease); }}
.copy-btn:hover {{ background:rgba(255,255,255,0.04); border-color:var(--border-hi); color:var(--text-1); }}
.copy-btn.copied {{ background:rgba(52,199,89,0.1); color:#34c759; border-color:rgba(52,199,89,0.3); }}
.kbd-hint {{ font-size:0.6rem; color:var(--text-3); font-family:'JetBrains Mono',monospace; }}
.kbd-hint kbd {{ display:inline-block; background:rgba(255,255,255,0.06); border:1px solid rgba(255,255,255,0.08); border-bottom-width:2px; border-radius:4px; padding:1px 5px; font-size:0.58rem; color:var(--text-2); font-family:inherit; line-height:1.4; }}

*:focus {{ outline:none; }}
*:focus-visible {{ outline:2px solid rgba(96,165,250,0.5); outline-offset:2px; }}

@media (max-width:1100px) {{ .panel-left {{ width:210px; }} .panel-right {{ width:260px; }} }}
@media (max-width:800px) {{ .app {{ flex-direction:column; }} .panel-left,.panel-right {{ width:100%; max-height:200px; border-radius:0; }} .center {{ min-height:280px; }} }}
@media (prefers-reduced-motion:reduce) {{ *,*::before,*::after {{ animation-duration:0.01ms !important; transition-duration:0.01ms !important; }} }}
</style>
</head>
<body>
<div class="app">
    <aside class="panel-left">
        <div class="panel-header">
            <div>
                <div class="brand">ScreenCap</div>
                <div class="rec-id">{capture_id} &middot; {duration_str}</div>
            </div>
            <span class="ev-count">{len(events_data)}</span>
        </div>
        <div class="events-list" id="events-list"></div>
    </aside>
    <main class="center">
        <div class="frame-container" id="frame-container">
            <img id="frame-image" src="" alt="Frame">
            <canvas id="overlay-canvas"></canvas>
            <div class="frame-badge" id="frame-time">0:00.00</div>
        </div>
    </main>
    <aside class="panel-right">
        <div class="ctx-header">
            <span class="ctx-type" id="ctx-type">&mdash;</span>
            <span class="ctx-time" id="ctx-time">0:00.00</span>
        </div>
        <div class="card" id="app-card">
            <div class="card-label">Application</div>
            <div class="card-body">
                <div class="app-name" id="app-name">&mdash;</div>
                <div class="app-window" id="app-window"></div>
            </div>
        </div>
        <div class="card" id="element-card">
            <div class="card-label">UI Element</div>
            <div class="card-body" id="element-body"><span class="empty-msg">No element data</span></div>
        </div>
        <div class="card">
            <div class="card-head">
                <span class="card-label">Event Data</span>
                <button class="copy-btn" id="copy-btn">Copy</button>
            </div>
            <div class="card-body" id="details-content"><span class="empty-msg">Select an event</span></div>
        </div>
        {transcript_html}
    </aside>
</div>
<div class="player-bar">
    <div class="player-main">
        <div class="player-nav">
            <button id="btn-first" title="First (Home)">\u23EE</button>
            <button id="btn-prev" title="Previous">\u25C0</button>
            <button id="btn-play" class="play-btn" title="Play/Pause (Space)">\u25B6</button>
            <button id="btn-next" title="Next">\u25B6</button>
            <button id="btn-last" title="Last (End)">\u23ED</button>
        </div>
        <div class="step-display" id="step-counter">Step 1 / {len(events_data)}</div>
        <div class="timeline" id="timeline">
            <div class="timeline-progress" id="timeline-progress"></div>
            <div class="timeline-markers" id="timeline-markers"></div>
        </div>
        <div class="time-display">
            <span id="current-time">0:00.00</span>
            <span class="time-sep">/</span>
            <span>{duration_str}</span>
        </div>
    </div>
    <div class="player-secondary">
        <label class="toggle" title="Toggle overlay (O)">
            <input type="checkbox" id="btn-overlay" checked>
            <span class="toggle-track"></span>
            <span class="toggle-label">Overlay</span>
        </label>
        {audio_html}
        <div class="player-spacer"></div>
        <button class="copy-btn" id="copy-all-btn">Copy All</button>
        <span class="kbd-hint">Space play &middot; \u2190 \u2192 step &middot; O overlay</span>
    </div>
</div>
{"" if not audio_b64 else f'<audio id="audio" src="data:{audio_type};base64,{audio_b64}"></audio>'}
<script>
const frames={frames_json};
const events={events_json};
const duration={duration};
const hasAudio={"true" if audio_b64 else "false"};
const screenWidth={screen_width};
const screenHeight={screen_height};
const pixelRatio={pixel_ratio};
const transcriptData={transcript if transcript else '{"text":"","segments":[]}'};

let currentIndex=0,isPlaying=false,playInterval=null,showOverlay=true,currentEvent=null;

const frameContainer=document.getElementById('frame-container');
const frameImage=document.getElementById('frame-image');
const overlayCanvas=document.getElementById('overlay-canvas');
const overlayCtx=overlayCanvas.getContext('2d');
const frameTime=document.getElementById('frame-time');
const currentTimeEl=document.getElementById('current-time');
const timelineProgress=document.getElementById('timeline-progress');
const timelineMarkers=document.getElementById('timeline-markers');
const eventsList=document.getElementById('events-list');
const detailsContent=document.getElementById('details-content');
const elementBody=document.getElementById('element-body');
const appNameEl=document.getElementById('app-name');
const appWindowEl=document.getElementById('app-window');
const ctxTypeEl=document.getElementById('ctx-type');
const ctxTimeEl=document.getElementById('ctx-time');
const btnPlay=document.getElementById('btn-play');
const btnOverlay=document.getElementById('btn-overlay');
const btnCopy=document.getElementById('copy-btn');
const btnCopyAll=document.getElementById('copy-all-btn');
const audio=document.getElementById('audio');
const transcriptContent=document.getElementById('transcript-content');
const stepCounter=document.getElementById('step-counter');

function formatTime(s){{ const m=Math.floor(s/60); const sc=(s%60).toFixed(2).padStart(5,'0'); return `${{m}}:${{sc}}`; }}

function getTypeColor(t){{
    t=t.toLowerCase();
    if(t.includes('smart_magnify'))return'var(--ev-smart-magnify)';
    if(t.includes('magnify'))return'var(--ev-magnify)';
    if(t.includes('rotate'))return'var(--ev-rotate)';
    if(t.includes('click'))return'var(--ev-click)';
    if(t.includes('drag'))return'var(--ev-drag)';
    if(t.includes('scroll'))return'var(--ev-scroll)';
    if(t.includes('type')||t==='key.down'||t==='key.up'||t==='key.special')return'var(--ev-type)';
    if(t.includes('start')||t.includes('end'))return'var(--accent)';
    return'var(--ev-move)';
}}

function getEventIcon(t){{
    const lo=t.toLowerCase();
    if(lo.includes('smart_magnify'))return'\u29BF';
    if(lo.includes('magnify'))return'\u2316';
    if(lo.includes('rotate'))return'\u21BB';
    if(lo.includes('click'))return'\u25CE';
    if(lo.includes('drag'))return'\u2197';
    if(lo.includes('scroll'))return'\u21D5';
    if(lo.includes('type')||lo==='key.down'||lo==='key.up')return'\u2328';
    if(lo==='key.special')return'\u2699';
    if(lo.includes('move'))return'\u2192';
    if(lo.includes('start'))return'\u25B6';
    if(lo.includes('end'))return'\u25A0';
    return'\u25CF';
}}

function getEventLabel(t){{
    const lo=t.toLowerCase();
    if(lo==='recording.start')return'Start';
    if(lo==='recording.end')return'End';
    if(lo.includes('smart_magnify'))return'Smart Zoom';
    if(lo.includes('magnify'))return'Zoom';
    if(lo.includes('rotate'))return'Rotate';
    if(lo.includes('doubleclick'))return'Double Click';
    if(lo.includes('singleclick')||lo.includes('click'))return'Click';
    if(lo.includes('drag'))return'Drag';
    if(lo.includes('scroll'))return'Scroll';
    if(lo.includes('move'))return'Move';
    if(lo.includes('type'))return'Type';
    if(lo==='key.special')return'Special Key';
    if(lo==='key.down')return'Key \u25BC';
    if(lo==='key.up')return'Key \u25B2';
    return t;
}}

function getEventDesc(ev){{
    if(ev.text)return ev.text.substring(0,22);
    if(ev.keys)return ev.keys;
    if(ev.magnification!=null)return(ev.magnification>0?'+':'')+Math.round(ev.magnification*100)+'% zoom';
    if(ev.rotation!=null)return Math.abs(Math.round(ev.rotation))+'\u00B0 '+(ev.rotation>=0?'CCW':'CW');
    if(ev.x!=null&&ev.y!=null)return`(${{Math.round(ev.x)}},${{Math.round(ev.y)}})`;
    return'';
}}

function escapeHtml(s){{ return s.replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;'); }}

const NOISE_KEYS=new Set(['AXChildren','children','AXChildrenInNavigationOrder','AXStartTextMarker','AXEndTextMarker','AXSelectedTextMarkerRange','AXPath','AXVisibleCharacterRange','AXSelectedTextRange','AXNumberOfCharacters','AXFrame','ChromeAXNodeId','AXDOMClassList','AXDOMIdentifier','AXActivationPoint','AXCloseButton','AXFullScreenButton','AXMinimizeButton','AXPopupValue','AXInvalid','type']);
const PRIMARY_KEYS=new Set(['AXRole','role','AXTitle','AXDescription','AXRoleDescription','AXHelp','AXValue','AXPosition','AXSize']);

function init(){{
    events.forEach((ev,i)=>{{
        const mk=document.createElement('div');
        mk.className='timeline-marker';
        mk.style.left=(ev.time/duration*100)+'%';
        timelineMarkers.appendChild(mk);
    }});
    events.forEach((ev,i)=>{{
        const item=document.createElement('div');
        item.className='ev-item';
        item.dataset.index=i;
        const color=getTypeColor(ev.type);
        const icon=getEventIcon(ev.type);
        const label=getEventLabel(ev.type);
        const desc=getEventDesc(ev);
        const ts=formatTime(ev.time);
        let dh='';
        if(desc)dh=`<span class="ev-desc">${{escapeHtml(desc)}}</span>`;
        item.innerHTML=`<div class="ev-icon" style="color:${{color}}">${{icon}}</div><div class="ev-body"><span class="ev-label">${{escapeHtml(label)}}</span>${{dh}}</div><span class="ev-time">${{ts}}</span>`;
        item.addEventListener('click',()=>goToIndex(i));
        eventsList.appendChild(item);
    }});
    if(transcriptContent&&transcriptData.segments&&transcriptData.segments.length>0){{
        transcriptData.segments.forEach((seg,i)=>{{
            const sp=document.createElement('span');
            sp.className='transcript-segment';
            sp.dataset.index=i;sp.dataset.start=seg.start;sp.dataset.end=seg.end;
            const ts2=document.createElement('span');
            ts2.className='transcript-time';ts2.textContent=formatTime(seg.start);
            sp.appendChild(ts2);sp.appendChild(document.createTextNode(seg.text+' '));
            sp.addEventListener('click',()=>seekToTranscript(seg.start));
            transcriptContent.appendChild(sp);
        }});
    }}else if(transcriptContent&&transcriptData.text){{
        transcriptContent.textContent=transcriptData.text;
    }}
    updateDisplay();
    if(hasAudio&&audio){{
        const vol=document.getElementById('volume');
        const mut=document.getElementById('mute');
        if(vol){{ vol.addEventListener('input',()=>{{ audio.volume=vol.value; }}); audio.volume=vol.value; }}
        if(mut){{ mut.addEventListener('change',()=>{{ audio.muted=mut.checked; }}); }}
    }}
}}

function updateDisplay(skipAudioSync){{
    const frame=frames[currentIndex];
    const ev=events[currentIndex];
    if(frame&&frame.image){{
        frameImage.src='data:image/jpeg;base64,'+frame.image;
        frameImage.onload=()=>drawOverlay(ev);
    }}
    if(frameImage.complete)setTimeout(()=>drawOverlay(ev),0);
    const ts=formatTime(ev.time);
    frameTime.textContent=ts;
    if(!isPlaying)currentTimeEl.textContent=ts;
    if(!isPlaying)timelineProgress.style.width=(ev.time/duration*100)+'%';
    document.querySelectorAll('.ev-item').forEach((item,i)=>{{ item.classList.toggle('active',i===currentIndex); }});
    const active=eventsList.querySelector('.ev-item.active');
    if(active)active.scrollIntoView({{block:'nearest',behavior:'smooth'}});
    updateDetails(ev);
    updateContext(ev);
    updateStepCounter();
    if(hasAudio&&audio&&!skipAudioSync&&!isPlaying)audio.currentTime=ev.time;
}}

function updateStepCounter(){{
    stepCounter.textContent=`Step ${{currentIndex+1}} / ${{frames.length}}`;
    document.getElementById('btn-prev').disabled=currentIndex===0;
    document.getElementById('btn-first').disabled=currentIndex===0;
    document.getElementById('btn-next').disabled=currentIndex===frames.length-1;
    document.getElementById('btn-last').disabled=currentIndex===frames.length-1;
}}

function updateContext(ev){{
    ctxTypeEl.textContent=getEventLabel(ev.type);
    ctxTypeEl.style.color=getTypeColor(ev.type);
    ctxTimeEl.textContent=formatTime(ev.time);
    if(ev.window){{
        appNameEl.textContent=ev.window;
        if(ev.windowTitle&&ev.windowTitle!==ev.window){{ appWindowEl.textContent=ev.windowTitle; appWindowEl.style.display=''; }}
        else{{ appWindowEl.textContent=''; appWindowEl.style.display='none'; }}
    }}else{{ appNameEl.textContent='\u2014'; appWindowEl.style.display='none'; }}
    const el=ev.element;
    if(!el||typeof el!=='object'||Object.keys(el).length===0){{ elementBody.innerHTML='<div class="empty-state"><span class="empty-state-icon">\u2B1A</span><span class="empty-state-text">No element data</span></div>'; return; }}
    let h='';
    const role=el.AXRole||'',roleDesc=el.AXRoleDescription||'';
    if(role||roleDesc){{
        let rs=role?escapeHtml(role):'';
        if(roleDesc)rs+=role?` <span class="el-val muted">(${{escapeHtml(roleDesc)}})</span>`:escapeHtml(roleDesc);
        h+=`<div class="el-field"><span class="el-key">Role</span><span class="el-val role">${{rs}}</span></div>`;
    }}
    const parts=[];
    if(el.AXTitle)parts.push(el.AXTitle);
    if(el.AXDescription&&el.AXDescription!==el.AXTitle)parts.push(el.AXDescription);
    if(el.AXHelp&&el.AXHelp!==el.AXTitle&&el.AXHelp!==el.AXDescription)parts.push(el.AXHelp);
    const lbl=parts.join(' \u2014 ');
    if(lbl)h+=`<div class="el-field"><span class="el-key">Label</span><span class="el-val">${{escapeHtml(lbl)}}</span></div>`;
    const val=el.AXValue;
    if(val!==undefined&&val!==null&&val!==''){{
        const vs=typeof val==='object'?JSON.stringify(val):String(val);
        const tr=vs.length>100?vs.substring(0,100)+'...':vs;
        h+=`<div class="el-field"><span class="el-key">Value</span><span class="el-val">${{escapeHtml(tr)}}</span></div>`;
    }}
    const fl=[];
    if(el.AXFocused===true)fl.push('Focused');
    if(el.AXEnabled===false)fl.push('Disabled');
    if(el.AXSubrole)fl.push(el.AXSubrole);
    if(fl.length>0)h+=`<div class="el-field"><span class="el-key">State</span><span class="el-val muted">${{escapeHtml(fl.join(', '))}}</span></div>`;
    const rem={{}};let hasRem=false;
    for(const[k,v]of Object.entries(el)){{
        if(PRIMARY_KEYS.has(k)||NOISE_KEYS.has(k))continue;
        if(v===null||v===undefined||v==='')continue;
        if(k==='AXFocused'||k==='AXEnabled'||k==='AXSubrole')continue;
        rem[k]=v;hasRem=true;
    }}
    if(hasRem){{
        h+=`<div class="more-toggle" id="info-toggle"><span class="chevron">\u25B6</span> More attributes</div><div class="more-content" id="info-more">`;
        for(const[k,v]of Object.entries(rem)){{
            const vs=typeof v==='object'?JSON.stringify(v):String(v);
            const tr=vs.length>100?vs.substring(0,100)+'...':vs;
            h+=`<div><span class="a11y-key">${{escapeHtml(k)}}</span>: <span class="a11y-val">${{escapeHtml(tr)}}</span></div>`;
        }}
        h+=`</div>`;
    }}
    elementBody.innerHTML=h;
    const tog=document.getElementById('info-toggle');
    if(tog)tog.addEventListener('click',()=>{{ tog.classList.toggle('expanded'); document.getElementById('info-more').classList.toggle('visible'); }});
}}

function formatModifiers(flags){{
    const parts=[];
    if(flags&0x20000)parts.push('Shift');
    if(flags&0x40000)parts.push('Ctrl');
    if(flags&0x80000)parts.push('Opt');
    if(flags&0x100000)parts.push('Cmd');
    if(flags&0x10000)parts.push('Caps');
    if(flags&0x800000)parts.push('Fn');
    return parts.join('+')||String(flags);
}}
function formatScrollPhase(p){{
    return {{1:'began',2:'changed',4:'stationary',8:'ended',128:'mayBegin'}}[p]||String(p);
}}
function formatMomentum(p){{
    return {{0:'none',1:'begin',2:'continue',3:'end'}}[p]||String(p);
}}

function updateDetails(ev){{
    currentEvent=ev;
    let h='';
    for(const[key,value]of Object.entries(ev)){{
        if(key==='index'||key==='element'||key==='window'||key==='windowTitle'||key==='path')continue;
        const dv=key==='time'?formatTime(value):key==='modifiers'?formatModifiers(value):key==='scroll_phase'?formatScrollPhase(value):key==='momentum_phase'?formatMomentum(value):key==='is_continuous'?(value?'trackpad':'wheel'):value;
        const cls=key==='type'?' type-val':key==='time'?' time-val':'';
        h+=`<div class="detail-row"><span class="detail-key">${{key}}</span><span class="detail-val${{cls}}">${{dv??'\u2014'}}</span></div>`;
    }}
    detailsContent.innerHTML=h;
}}

function drawOverlay(ev){{
    const imgR=frameImage.getBoundingClientRect();
    const conR=frameContainer.getBoundingClientRect();
    const nW=frameImage.naturalWidth||screenWidth;
    const nH=frameImage.naturalHeight||screenHeight;
    const iA=nW/nH,cA=imgR.width/imgR.height;
    let dW,dH,oX,oY;
    if(iA>cA){{ dW=imgR.width; dH=imgR.width/iA; oX=0; oY=(imgR.height-dH)/2; }}
    else{{ dH=imgR.height; dW=imgR.height*iA; oX=(imgR.width-dW)/2; oY=0; }}
    overlayCanvas.width=imgR.width; overlayCanvas.height=imgR.height;
    overlayCanvas.style.width=imgR.width+'px'; overlayCanvas.style.height=imgR.height+'px';
    overlayCanvas.style.left=(imgR.left-conR.left)+'px'; overlayCanvas.style.top=(imgR.top-conR.top)+'px';
    overlayCtx.clearRect(0,0,overlayCanvas.width,overlayCanvas.height);
    if(!showOverlay)return;
    const sX=(dW/nW)*pixelRatio, sY=(dH/nH)*pixelRatio;
    const type=ev.type;
    if(type.includes('click')||type==='mouse.down'||type==='mouse.up'){{
        const x=oX+(ev.x*sX),y=oY+(ev.y*sY),r=20;
        overlayCtx.beginPath();overlayCtx.arc(x,y,r+10,0,Math.PI*2);overlayCtx.fillStyle='rgba(255,100,100,0.3)';overlayCtx.fill();
        overlayCtx.beginPath();overlayCtx.arc(x,y,r,0,Math.PI*2);overlayCtx.strokeStyle='#ef5350';overlayCtx.lineWidth=3;overlayCtx.stroke();
        overlayCtx.beginPath();overlayCtx.arc(x,y,4,0,Math.PI*2);overlayCtx.fillStyle='#ef5350';overlayCtx.fill();
        overlayCtx.beginPath();
        overlayCtx.moveTo(x-r-5,y);overlayCtx.lineTo(x-r+10,y);
        overlayCtx.moveTo(x+r-10,y);overlayCtx.lineTo(x+r+5,y);
        overlayCtx.moveTo(x,y-r-5);overlayCtx.lineTo(x,y-r+10);
        overlayCtx.moveTo(x,y+r-10);overlayCtx.lineTo(x,y+r+5);
        overlayCtx.strokeStyle='#ef5350';overlayCtx.lineWidth=2;overlayCtx.stroke();
    }}else if(type.includes('drag')){{
        const sx=oX+(ev.x*sX),sy=oY+(ev.y*sY),ex=oX+((ev.x+ev.dx)*sX),ey=oY+((ev.y+ev.dy)*sY);
        overlayCtx.strokeStyle='#4caf50';
        if(ev.path&&ev.path.length>1){{
            /* Variable-thickness polyline when pressure data exists */
            const minW=1,maxW=6;
            for(let i=1;i<ev.path.length;i++){{
                const p0=ev.path[i-1],p1=ev.path[i];
                const pr=p1.p??0;
                overlayCtx.lineWidth=minW+pr*(maxW-minW);
                overlayCtx.beginPath();
                overlayCtx.moveTo(oX+(p0.x*sX),oY+(p0.y*sY));
                overlayCtx.lineTo(oX+(p1.x*sX),oY+(p1.y*sY));
                overlayCtx.stroke();
            }}
        }}else{{
            overlayCtx.beginPath();overlayCtx.moveTo(sx,sy);overlayCtx.lineTo(ex,ey);overlayCtx.lineWidth=3;overlayCtx.stroke();
        }}
        overlayCtx.beginPath();overlayCtx.arc(sx,sy,8,0,Math.PI*2);overlayCtx.fillStyle='#4caf50';overlayCtx.fill();
        const a=Math.atan2(ey-sy,ex-sx);
        overlayCtx.beginPath();overlayCtx.moveTo(ex,ey);overlayCtx.lineTo(ex-15*Math.cos(a-0.4),ey-15*Math.sin(a-0.4));overlayCtx.lineTo(ex-15*Math.cos(a+0.4),ey-15*Math.sin(a+0.4));overlayCtx.closePath();overlayCtx.fillStyle='#4caf50';overlayCtx.fill();
    }}else if(type.includes('scroll')){{
        const x=oX+(ev.x*sX),y=oY+(ev.y*sY);
        overlayCtx.beginPath();overlayCtx.arc(x,y,15,0,Math.PI*2);overlayCtx.strokeStyle='#ab47bc';overlayCtx.lineWidth=2;overlayCtx.stroke();
        const dy=ev.dy||0;
        if(dy!==0){{
            const aY=dy>0?-25:25;
            overlayCtx.beginPath();overlayCtx.moveTo(x,y+aY);overlayCtx.lineTo(x-8,y+aY+(dy>0?10:-10));overlayCtx.lineTo(x+8,y+aY+(dy>0?10:-10));overlayCtx.closePath();overlayCtx.fillStyle='#ab47bc';overlayCtx.fill();
        }}
    }}else if(type.includes('smart_magnify')){{
        const x=oX+(ev.x*sX),y=oY+(ev.y*sY);
        const r=25;
        /* Pulsing circle */
        overlayCtx.beginPath();overlayCtx.arc(x,y,r+15,0,Math.PI*2);overlayCtx.strokeStyle='rgba(38,166,154,0.3)';overlayCtx.lineWidth=2;overlayCtx.stroke();
        overlayCtx.beginPath();overlayCtx.arc(x,y,r,0,Math.PI*2);overlayCtx.strokeStyle='#26a69a';overlayCtx.lineWidth=3;overlayCtx.stroke();
        /* Crosshair */
        overlayCtx.beginPath();
        overlayCtx.moveTo(x-r-8,y);overlayCtx.lineTo(x-6,y);
        overlayCtx.moveTo(x+6,y);overlayCtx.lineTo(x+r+8,y);
        overlayCtx.moveTo(x,y-r-8);overlayCtx.lineTo(x,y-6);
        overlayCtx.moveTo(x,y+6);overlayCtx.lineTo(x,y+r+8);
        overlayCtx.strokeStyle='#26a69a';overlayCtx.lineWidth=2;overlayCtx.stroke();
        /* Center dot */
        overlayCtx.beginPath();overlayCtx.arc(x,y,4,0,Math.PI*2);overlayCtx.fillStyle='#26a69a';overlayCtx.fill();
        /* Label */
        overlayCtx.font='bold 13px sans-serif';overlayCtx.fillStyle='#26a69a';overlayCtx.fillText('Smart Zoom',x+r+12,y+5);
    }}else if(type.includes('magnify')){{
        const x=oX+(ev.x*sX),y=oY+(ev.y*sY);
        const mag=ev.magnification||0;
        const r1=20,r2=35,r3=50;
        const alpha=Math.min(Math.abs(mag)*3,0.6)+0.15;
        overlayCtx.beginPath();overlayCtx.arc(x,y,r3,0,Math.PI*2);overlayCtx.strokeStyle=`rgba(0,188,212,${{alpha*0.5}})`;overlayCtx.lineWidth=2;overlayCtx.stroke();
        overlayCtx.beginPath();overlayCtx.arc(x,y,r2,0,Math.PI*2);overlayCtx.strokeStyle=`rgba(0,188,212,${{alpha*0.7}})`;overlayCtx.lineWidth=2;overlayCtx.stroke();
        overlayCtx.beginPath();overlayCtx.arc(x,y,r1,0,Math.PI*2);overlayCtx.strokeStyle='#00bcd4';overlayCtx.lineWidth=3;overlayCtx.stroke();
        overlayCtx.beginPath();overlayCtx.arc(x,y,5,0,Math.PI*2);overlayCtx.fillStyle='#00bcd4';overlayCtx.fill();
        const sign=mag>=0?'+':'';
        overlayCtx.font='bold 13px sans-serif';overlayCtx.fillStyle='#00bcd4';overlayCtx.fillText(sign+Math.round(mag*100)+'%',x+r3+5,y+5);
    }}else if(type.includes('rotate')){{
        const x=oX+(ev.x*sX),y=oY+(ev.y*sY);
        const rot=ev.rotation||0;
        const r=30;
        const startA=-Math.PI/2;
        const sweep=(rot/180)*Math.PI;
        overlayCtx.beginPath();overlayCtx.arc(x,y,r,startA,startA+sweep,rot<0);overlayCtx.strokeStyle='#ff9800';overlayCtx.lineWidth=3;overlayCtx.stroke();
        const endA=startA+sweep;
        const ax=x+r*Math.cos(endA),ay=y+r*Math.sin(endA);
        const d=rot>=0?1:-1;
        overlayCtx.beginPath();overlayCtx.moveTo(ax,ay);overlayCtx.lineTo(ax+10*Math.cos(endA+d*2.5),ay+10*Math.sin(endA+d*2.5));overlayCtx.lineTo(ax+10*Math.cos(endA-d*0.5),ay+10*Math.sin(endA-d*0.5));overlayCtx.closePath();overlayCtx.fillStyle='#ff9800';overlayCtx.fill();
        overlayCtx.beginPath();overlayCtx.arc(x,y,4,0,Math.PI*2);overlayCtx.fillStyle='#ff9800';overlayCtx.fill();
        overlayCtx.font='bold 13px sans-serif';overlayCtx.fillStyle='#ff9800';overlayCtx.fillText(Math.abs(Math.round(rot))+'\u00B0',x+r+10,y+5);
    }}else if(type.includes('type')||type==='key.special'||type==='key.down'||type==='key.up'){{
        const txt=ev.text||ev.keys||'';
        if(txt){{
            const bx=20,by=overlayCanvas.height-60,pad=10;
            overlayCtx.font='16px monospace';
            const tw=Math.min(overlayCtx.measureText(txt).width,300);
            overlayCtx.fillStyle='rgba(66,165,245,0.9)';overlayCtx.beginPath();overlayCtx.roundRect(bx,by,tw+pad*2,36,8);overlayCtx.fill();
            overlayCtx.fillStyle='#fff';overlayCtx.fillText(txt.substring(0,30),bx+pad,by+24);
        }}
    }}
    let lb=ev.type.split('.').pop();
    if(ev.text)lb+=': '+ev.text.substring(0,20);
    else if(ev.keys)lb+=': '+ev.keys;
    overlayCtx.font='bold 14px sans-serif';
    const lm=overlayCtx.measureText(lb);
    overlayCtx.fillStyle='rgba(0,0,0,0.7)';overlayCtx.beginPath();overlayCtx.roundRect(5,12,lm.width+10,24,4);overlayCtx.fill();
    overlayCtx.fillStyle='#fff';overlayCtx.fillText(lb,10,30);
}}

function goToIndex(i){{
    currentIndex=Math.max(0,Math.min(frames.length-1,i));
    if(isPlaying)togglePlay();
    updateDisplay();
    if(hasAudio&&audio){{ audio.currentTime=events[currentIndex].time; updateActiveTranscript(events[currentIndex].time); }}
}}
function next(){{ if(currentIndex<frames.length-1){{ currentIndex++; updateDisplay(); }} else if(isPlaying)togglePlay(); }}
function prev(){{ if(currentIndex>0){{ currentIndex--; updateDisplay(); }} }}

function stopAtEnd(){{
    /* Guarantee we land on the last event (recording.end) before stopping. */
    const last=frames.length-1;
    if(currentIndex<last){{ currentIndex=last; updateDisplay(); }}
    timelineProgress.style.width='100%';
    currentTimeEl.textContent=formatTime(duration);
    isPlaying=false;
    btnPlay.textContent='\u25B6';
    if(hasAudio&&audio){{ audio.pause(); audio.ontimeupdate=null; audio.onended=null; }}
    clearInterval(playInterval);
}}

function togglePlay(){{
    isPlaying=!isPlaying;
    btnPlay.textContent=isPlaying?'\u23F8':'\u25B6';
    if(isPlaying){{
        if(hasAudio&&audio){{
            audio.currentTime=events[currentIndex].time; audio.play();
            audio.ontimeupdate=()=>{{
                if(!isPlaying)return;
                const ct=audio.currentTime;
                let ni=currentIndex;
                for(let i=0;i<events.length;i++){{ if(events[i].time<=ct)ni=i; else break; }}
                if(ni!==currentIndex){{ currentIndex=ni; updateDisplay(true); }}
                timelineProgress.style.width=(ct/duration*100)+'%';
                currentTimeEl.textContent=formatTime(ct);
                updateActiveTranscript(ct);
            }};
            audio.onended=()=>stopAtEnd();
        }}else{{
            const st=events[currentIndex].time, srt=Date.now();
            playInterval=setInterval(()=>{{
                const el=(Date.now()-srt)/1000, ct=st+el;
                let ni=currentIndex;
                for(let i=currentIndex;i<events.length;i++){{ if(events[i].time<=ct)ni=i; else break; }}
                if(ni!==currentIndex){{ currentIndex=ni; updateDisplay(); }}
                timelineProgress.style.width=(ct/duration*100)+'%';
                currentTimeEl.textContent=formatTime(ct);
                if(ct>=duration)stopAtEnd();
            }},50);
        }}
    }}else{{
        if(hasAudio&&audio){{ audio.pause(); audio.ontimeupdate=null; audio.onended=null; }}
        clearInterval(playInterval);
    }}
}}

function toggleOverlay(){{ showOverlay=!showOverlay; btnOverlay.checked=showOverlay; drawOverlay(events[currentIndex]); }}

function formatEventForCopy(ev){{
    const lines=[];
    for(const[k,v]of Object.entries(ev)){{
        if(k==='index')continue;
        lines.push(`${{k}}: ${{k==='time'?formatTime(v):(v??'-')}}`);
    }}
    return lines.join('\\n');
}}

function copyEventDetails(){{
    if(!currentEvent)return;
    navigator.clipboard.writeText(formatEventForCopy(currentEvent)).then(()=>{{
        btnCopy.textContent='Copied!';btnCopy.classList.add('copied');
        setTimeout(()=>{{ btnCopy.textContent='Copy';btnCopy.classList.remove('copied'); }},1500);
    }});
}}

function copyAllEvents(){{
    const t=events.map((ev,i)=>`--- Event ${{i+1}} ---\\n${{formatEventForCopy(ev)}}`).join('\\n\\n');
    navigator.clipboard.writeText(t).then(()=>{{
        btnCopyAll.textContent='Copied!';btnCopyAll.classList.add('copied');
        setTimeout(()=>{{ btnCopyAll.textContent='Copy All';btnCopyAll.classList.remove('copied'); }},1500);
    }});
}}

function seekToTranscript(time){{
    if(hasAudio&&audio){{
        audio.currentTime=time;
        let cl=0,md=Infinity;
        events.forEach((ev,i)=>{{ const d=Math.abs(ev.time-time); if(d<md){{ md=d;cl=i; }} }});
        currentIndex=cl; updateDisplay(true); updateActiveTranscript(time);
    }}
}}

function updateActiveTranscript(ct){{
    if(!transcriptContent)return;
    transcriptContent.querySelectorAll('.transcript-segment').forEach(seg=>{{
        const s=parseFloat(seg.dataset.start),e=parseFloat(seg.dataset.end);
        const active=ct>=s&&ct<e;
        seg.classList.toggle('active',active);
        if(active)seg.scrollIntoView({{block:'nearest',behavior:'smooth'}});
    }});
}}

document.getElementById('btn-play').addEventListener('click',togglePlay);
document.getElementById('btn-next').addEventListener('click',next);
document.getElementById('btn-prev').addEventListener('click',prev);
document.getElementById('btn-first').addEventListener('click',()=>goToIndex(0));
document.getElementById('btn-last').addEventListener('click',()=>goToIndex(frames.length-1));
btnCopy.addEventListener('click',copyEventDetails);
btnCopyAll.addEventListener('click',copyAllEvents);
btnOverlay.addEventListener('change',()=>{{ showOverlay=btnOverlay.checked; drawOverlay(events[currentIndex]); }});
document.getElementById('timeline').addEventListener('click',(e)=>{{
    const r=e.target.getBoundingClientRect(),x=e.clientX-r.left,p=x/r.width,tt=p*duration;
    let cl=0,md=Infinity;
    frames.forEach((f,i)=>{{ const d=Math.abs(f.time-tt); if(d<md){{ md=d;cl=i; }} }});
    goToIndex(cl);
}});
document.addEventListener('keydown',(e)=>{{
    if(e.target.tagName==='INPUT')return;
    switch(e.code){{
        case'Space':e.preventDefault();togglePlay();break;
        case'ArrowRight':e.preventDefault();next();break;
        case'ArrowLeft':e.preventDefault();prev();break;
        case'Home':e.preventDefault();goToIndex(0);break;
        case'End':e.preventDefault();goToIndex(frames.length-1);break;
        case'KeyO':e.preventDefault();toggleOverlay();break;
    }}
}});
init();
</script>
</body>
</html>'''

    return html
