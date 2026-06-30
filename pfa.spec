# -*- mode: python ; coding: utf-8 -*-
# PyInstaller spec for Personal Finance Analyser
#
# Build with:
#   pyinstaller pfa.spec
#
# Output: dist/PersonalFinanceAnalyser/PersonalFinanceAnalyser.exe
#
# The exe is the entry point; Data/ and config.yaml live alongside it
# in the install directory, not inside the bundle.

import sys
from pathlib import Path

block_cipher = None
ROOT = Path(SPECPATH)

a = Analysis(
    [str(ROOT / "server.py")],
    pathex=[str(ROOT)],
    binaries=[],
    datas=[
        # Read-only assets bundled inside the exe's _internal folder
        (str(ROOT / "templates"), "templates"),
        (str(ROOT / "static"),    "static"),
        (str(ROOT / "docs"),      "docs"),
        (str(ROOT / "src"),       "src"),
        # finance_analyser.py is invoked as a subprocess from the running server;
        # include it so the packaged exe can launch it via sys.executable.
        (str(ROOT / "finance_analyser.py"), "."),
    ],
    hiddenimports=[
        # Flask / Werkzeug internals that PyInstaller may miss
        "flask",
        "flask.templating",
        "werkzeug",
        "werkzeug.routing",
        "werkzeug.exceptions",
        "jinja2",
        "jinja2.ext",
        # Data / reporting
        "pandas",
        "pandas._libs.tslibs.np_datetime",
        "pandas._libs.tslibs.nattype",
        "pandas._libs.tslibs.timedeltas",
        "pandas._libs.missing",
        "pandas._libs.hashtable",
        "numpy",
        "plotly",
        "plotly.graph_objects",
        "plotly.express",
        # PDF parsing
        "pdfplumber",
        "pdfminer",
        "pdfminer.high_level",
        "pdfminer.layout",
        "pdfminer.converter",
        # YAML / config
        "yaml",
        # HTTP / API
        "anthropic",
        "httpx",
        "httpcore",
        "certifi",
        # SQLite is stdlib but ensure it's included
        "sqlite3",
        "_sqlite3",
        # Optional: yfinance (portfolio live prices) — graceful ImportError in src/portfolio.py
        # "yfinance",
        # Stdlib modules sometimes missed
        "csv",
        "zipfile",
        "io",
        "threading",
        "email",
        "email.mime",
        "email.mime.text",
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        # Dev/test tools — not needed at runtime
        "pytest",
        "hypothesis",
        "IPython",
        "notebook",
        "matplotlib",
        "tkinter",
    ],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="PersonalFinanceAnalyser",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=True,          # keep console for log output; set False for silent-background mode
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=None,             # add icon=str(ROOT / "static/favicon.ico") if you have one
)

coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name="PersonalFinanceAnalyser",
)
