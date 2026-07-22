"""Deployment-specific values, kept out of the code and the repository.

ROOT is derived from this file's own location, so the tree can live anywhere
without a hardcoded path. Everything else comes from environment variables,
falling back to a `config.env` at the repository root (gitignored). See
`config.env.example` for the keys.
"""

import os
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _file_config():
    cfg = {}
    f = ROOT / "config.env"
    if f.exists():
        for line in f.read_text().splitlines():
            line = line.split("#", 1)[0].strip()
            if "=" in line:
                k, v = line.split("=", 1)
                cfg[k.strip()] = v.strip().strip('"').strip("'")
    return cfg


_FILE = _file_config()


def get(key, default=""):
    # A real environment variable wins over the file.
    return os.environ.get(key) or _FILE.get(key) or default


DEEZER_USER = int(get("DEEZER_USER", "0") or "0")
NAVIDROME_URL = get("NAVIDROME_URL", "")
DEEMIX_URL = get("DEEMIX_URL", "")
