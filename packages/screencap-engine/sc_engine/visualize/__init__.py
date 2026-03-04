"""Visualization tools for capture recordings.

Provides tools to visualize and explore recorded GUI interactions:
- create_demo: Generate animated GIF/video with event overlays
- create_html: Generate interactive HTML viewer with audio playback
"""

from sc_engine.visualize.demo import create_demo
from sc_engine.visualize.html import create_html

__all__ = ["create_demo", "create_html"]
