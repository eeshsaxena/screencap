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
]

all_hiddenimports += hidden_imports

# ---------------------------------------------------------------------------
# Analysis
# ---------------------------------------------------------------------------
import os
_root = os.path.abspath(os.path.join(SPECPATH, '..'))

a = Analysis(
    ['main.py'],
    pathex=[
        os.path.join(_root, 'src'),
        os.path.join(_root, 'packages', 'screencap-engine'),
    ],
    binaries=all_binaries,
    datas=all_datas,
    hiddenimports=all_hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        # Large ML frameworks — not used by screencap
        'torch', 'torchvision', 'torchaudio',
        'transformers', 'tokenizers', 'safetensors',
        'hf_xet',                    # optional HF acceleration, not needed
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
