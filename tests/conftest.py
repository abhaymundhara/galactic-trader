"""Test bootstrap to prioritize this repo's local packages."""

from pathlib import Path
import sys


REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

# Avoid collisions with an installed third-party package named "cli".
if "cli" in sys.modules:
    mod = sys.modules["cli"]
    mod_file = getattr(mod, "__file__", "") or ""
    if mod_file and str(REPO_ROOT) not in mod_file:
        del sys.modules["cli"]
