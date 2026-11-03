from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


TEST_DB = Path(__file__).parent / "test_sulnet.db"
for suffix in ("", "-shm", "-wal"):
    try:
        Path(str(TEST_DB) + suffix).unlink()
    except FileNotFoundError:
        pass

os.environ["APP_ENV"] = "development"
os.environ.setdefault("AI_SDR_DEBOUNCE_SECONDS", "0")
os.environ["DATABASE_URL"] = f"sqlite:///{TEST_DB.as_posix()}"
os.environ["SECRET_KEY"] = "test-secret-key-that-is-long-enough"
os.environ["BOOTSTRAP_ADMIN_EMAIL"] = "admin@sulnet.local"
os.environ["BOOTSTRAP_ADMIN_PASSWORD"] = "Sulnet@2026!"
