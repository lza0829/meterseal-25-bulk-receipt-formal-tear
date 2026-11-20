from __future__ import annotations
import os
from pathlib import Path

PROJECT_NAME = "抄表簿"
ROOT = Path(__file__).resolve().parents[2]
DATA_DIR = Path(os.environ.get("METALSEAL_TEST_DIR", ROOT / "data"))
DB_PATH = DATA_DIR / "meterseal.db"
