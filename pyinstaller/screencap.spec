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

# ---------------------------------------------------------------------------
# spaCy + NLP dependency metadata
# PyInstaller doesn't collect dist-info by default; spacy.util and catalogue
# need it for entry-point discovery.
# ---------------------------------------------------------------------------
metadata_packages = [
    'spacy', 'spacy_legacy', 'thinc',
    'cymem', 'preshed', 'murmurhash', 'srsly',
    'catalogue', 'langcodes',
]

for pkg in metadata_packages:
    try:
        all_datas += copy_metadata(pkg)
    except Exception:
        pass

# Also collect spacy data files (language data, etc.)
try:
    d, b, h = collect_all('spacy')
    all_datas += d
    all_binaries += b
    all_hiddenimports += h
except Exception:
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
# Hidden imports not auto-detected by PyInstaller
# ---------------------------------------------------------------------------
hidden_imports = [
    # pynput macOS backend
    'pynput.keyboard._darwin',
    'pynput.mouse._darwin',
    'pynput._util.darwin',
    # spaCy language data
    'spacy.lang.en',
    # Cython extensions used by spaCy/thinc
    'cymem.cymem',
    'preshed.maps',
    'murmurhash.mrmr',
    'srsly.msgpack.util',
    # SQLAlchemy dialect
    'sqlalchemy.dialects.sqlite',
    # tomli for Python 3.10 (stdlib tomllib in 3.11+)
    'tomli',
    # Presidio (optional, but include if installed)
    'presidio_analyzer',
    'presidio_anonymizer',
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
        os.path.join(_root, 'packages', 'openadapt-capture'),
        os.path.join(_root, 'packages', 'openadapt-privacy'),
    ],
    binaries=all_binaries,
    datas=all_datas,
    hiddenimports=all_hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        # Exclude heavy ML frameworks not needed by screencap at runtime.
        # torch/torchvision are transitive deps of presidio but presidio
        # works fine with just spaCy (CPU) for NER.
        'torch', 'torchvision', 'torchaudio',
        # transformers pulled in transitively but not used
        'transformers', 'huggingface_hub', 'tokenizers', 'safetensors',
        'hf_xet',
        # OpenCV headless is only needed for presidio-image-redactor
        # which is imported lazily — exclude to save ~90 MB
        'cv2',
        # Other heavy transitive deps not needed
        'matplotlib', 'sympy', 'IPython', 'notebook', 'jupyter',
        'scipy', 'sklearn', 'faiss',
        'botocore', 'boto3', 'awscrt',
        'google.cloud', 'google.auth', 'google.api_core',
        'grpc', 'grpcio',
        'timm',
        '_gdcm', 'gdcm', 'pydicom',
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
