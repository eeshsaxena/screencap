"""Visualization tools for capture recordings.

Only ``create_html`` is re-exported here so ``import screencap.engine``
does not transitively pull ``demo.py`` (and its Pillow / matplotlib
neighbourhood) into the eager import chain. Programmatic demo generation
lives at ``screencap.engine.visualize.demo.create_demo``.
"""

from screencap.engine.visualize.html import create_html

__all__ = ["create_html"]
