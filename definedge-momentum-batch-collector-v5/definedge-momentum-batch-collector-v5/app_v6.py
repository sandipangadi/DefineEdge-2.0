"""Compatibility shim for the existing Render V6 start command.

Production V6 code now lives in:
    /definedge-momentum-collector-v6-production/app_v6.py

The old V5 directory remains only because its app.py is the frozen,
proven reconstruction engine and Render currently starts from this path.
"""

import importlib.util
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
PRODUCTION_APP = (
    REPO_ROOT
    / "definedge-momentum-collector-v6-production"
    / "app_v6.py"
)

if not PRODUCTION_APP.exists():
    raise RuntimeError(f"V6 production application not found: {PRODUCTION_APP}")

spec = importlib.util.spec_from_file_location(
    "definedge_v6_production",
    PRODUCTION_APP,
)
if spec is None or spec.loader is None:
    raise RuntimeError("Could not load V6 production application.")

production = importlib.util.module_from_spec(spec)
sys.modules["definedge_v6_production"] = production
spec.loader.exec_module(production)

app = production.app
