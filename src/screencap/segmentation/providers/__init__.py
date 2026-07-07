"""Concrete :class:`~screencap.segmentation.provider.LLMProvider` backends.

Each backend owns its heavy/vendor imports **lazily** inside its methods, so
importing this package (or any backend module) stays cloud-free — the interface
in ``screencap.segmentation.provider`` never drags in a provider SDK.

Backends land incrementally:

- ``gemini`` — Google Gemini Flash (the cloud path selects it explicitly).
- ``ondevice`` — Apple Foundation Models via a bundled Swift-helper subprocess
  (U5); the zero-config default backend.

Import backend modules directly (or go through
``screencap.segmentation.provider.get_provider``).
"""
