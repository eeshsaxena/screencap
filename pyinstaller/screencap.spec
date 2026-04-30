# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec for screencap CLI binary.

Builds a --onedir bundle. Key decisions:
- onedir (not onefile): avoids multiprocessing spawn re-extracting ~100 MB per child process
- All PyObjC hooks inlined here (no built-in hooks exist — pyinstaller/pyinstaller#833)
- ApplicationServices requires CoreText (undocumented — pyobjc/pyobjc#581)
- upx=False: UPX breaks macOS codesigning
"""

from PyInstaller.utils.hooks import collect_all, copy_metadata

# ---------------------------------------------------------------------------
# PyObjC framework collections
# No built-in hooks exist for pyobjc-framework-* packages.
# collect_all() gathers binaries, datas, and hidden imports for each.
# ---------------------------------------------------------------------------
pyobjc_packages = [
    'objc',
    'Quartz',
    'CoreGraphics',
    'QuartzCore',
    'AppKit',
    'Foundation',          # imported independently in vendored packages
    'ApplicationServices',
    'CoreText',            # undocumented dep of ApplicationServices
    'CoreWLAN',
]

all_datas = []
all_binaries = []
all_hiddenimports = []

for pkg in pyobjc_packages:
    try:
        d, b, h = collect_all(pkg)
        all_datas += d
        all_binaries += b
        all_hiddenimports += h
    except Exception:
        # Package may not be installed (e.g. CoreWLAN on non-mac CI)
        pass

# rich._unicode_data uses hyphenated module names loaded via importlib
# (e.g. "unicode17-0-0.py") which PyInstaller can't detect.
try:
    d, b, h = collect_all('rich._unicode_data')
    all_datas += d
    all_binaries += b
    all_hiddenimports += h
except Exception:
    pass

# ---------------------------------------------------------------------------
# Privacy pipeline — GLiNER (fast-gliner) + Presidio + detect-secrets
# ---------------------------------------------------------------------------
privacy_packages = [
    'presidio_analyzer',  # ships conf/*.yml recognizer configs loaded at runtime
    'en_core_web_sm',     # spaCy model for tokenization (+ NER fallback)
]

for pkg in privacy_packages:
    try:
        d, b, h = collect_all(pkg)
        all_datas += d
        all_binaries += b
        all_hiddenimports += h
    except Exception:
        pass

# Presidio + en_core_web_sm need .dist-info for importlib_metadata lookups
# (spacy.util.is_package() and catalogue entry points)
for pkg in privacy_packages:
    try:
        all_datas += copy_metadata(pkg)
    except Exception:
        pass

# ---------------------------------------------------------------------------
# Network proxy logging — mitmproxy + cryptography + transitive HTTP/WS deps
#
# Per the institutional learning in the network proxy plan: do NOT wrap
# each collect_all in a bare try/except — that pattern silently swallows
# missing packages and is exactly the bundling failure mode Unit 7 aims
# to catch. Errors propagate; a missing wheel must fail the build.
# ---------------------------------------------------------------------------
network_packages = [
    'mitmproxy',         # core proxy event loop + Options + DumpMaster
    'mitmproxy_rs',      # native Rust extension (TLS / IO primitives)
    'cryptography',      # native dylibs (libcrypto, libssl) for CA + TLS
    'wsproto',           # WebSocket protocol (mitmproxy WS support)
    'h2',                # HTTP/2 frame parsing
    'h11',               # HTTP/1.1 parsing
    'kaitaistruct',      # binary protocol parser used by mitmproxy
    # V1.5: keyring backs the Keychain-stored KEK for body encryption.
    # collect_all picks up the macOS backend submodule (`keyring.backends.macOS`)
    # which PyInstaller cannot trace through `keyring.get_keyring()` discovery.
    'keyring',
]

for pkg in network_packages:
    d, b, h = collect_all(pkg)
    all_datas += d
    all_binaries += b
    all_hiddenimports += h

# ---------------------------------------------------------------------------
# Hidden imports not auto-detected by PyInstaller
# ---------------------------------------------------------------------------
hidden_imports = [
    # pynput macOS backend
    'pynput.keyboard._darwin',
    'pynput.mouse._darwin',
    'pynput._util.darwin',
    # SQLAlchemy dialect
    'sqlalchemy.dialects.sqlite',
    # tomli for Python 3.10 (stdlib tomllib in 3.11+)
    'tomli',
    # Privacy pipeline
    'fast_gliner',
    'detect_secrets',
    'detect_secrets.plugins',
    # V1.5 keyring macOS backend — collect_all('keyring') above picks up
    # most of it, but the backend module is loaded by string lookup
    # in keyring.backend.get_all_keyring() and PyInstaller's static
    # analysis cannot trace that. Pin it here so frozen builds find
    # the macOS Keychain backend at runtime.
    'keyring.backends.macOS',
]

all_hiddenimports += hidden_imports

# ---------------------------------------------------------------------------
# Analysis
# ---------------------------------------------------------------------------
import os
_root = os.path.abspath(os.path.join(SPECPATH, '..'))

# UT1 domain blocklist data files (privacy context classification)
import glob as _glob
_ut1_files = _glob.glob(os.path.join(_root, 'src', 'screencap', 'privacy', 'data', 'ut1', '*.txt'))
all_datas += [(f, os.path.join('screencap', 'privacy', 'data', 'ut1')) for f in _ut1_files]

a = Analysis(
    ['main.py'],
    pathex=[
        os.path.join(_root, 'src'),
    ],
    binaries=all_binaries,
    datas=all_datas,
    hiddenimports=all_hiddenimports + [
        # Engine sub-package: dynamic imports not traced by PyInstaller
        'screencap.engine.window._macos',
        'screencap.engine.platform.darwin',
        # Menu bar subprocess (spawned via multiprocessing.Process)
        'screencap.menubar',
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        # Large ML frameworks — not used by screencap
        'torch', 'torchvision', 'torchaudio',
        'transformers', 'tokenizers', 'safetensors',
        'cv2',
        'matplotlib', 'sympy', 'IPython', 'notebook', 'jupyter',
        'scipy', 'sklearn', 'faiss',
        'botocore', 'boto3', 'awscrt',
        'google.cloud', 'google.auth', 'google.api_core',
        'grpc', 'grpcio',
        'timm',
        '_gdcm', 'gdcm', 'pydicom',
        # ONNX Runtime standalone package — minos 14.0, fails CI verification.
        # fast-gliner statically links its own ONNX Runtime (minos 11.0).
        'onnxruntime',
        # Python GLiNER package (distinct from fast_gliner) — pulls in onnxruntime
        'gliner',
    ],
    noarchive=False,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name='screencap',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,       # UPX breaks macOS codesigning
    console=True,    # CLI, not GUI
    target_arch=None,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    name='screencap',
)
