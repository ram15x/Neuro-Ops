import os
import sys
import tempfile

# tests run with sqlite and in-memory redis, so no services are needed
_db = os.path.join(tempfile.mkdtemp(), "test.db")
os.environ.setdefault("DB_URL", f"sqlite:///{_db}")
os.environ.setdefault("REDIS_FAKE", "true")
os.environ.setdefault("JWT_SECRET", "test-secret-not-for-real-use")
os.environ.setdefault("AUTO_HEALING_ENABLED", "false")

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import pytest


@pytest.fixture(scope="session", autouse=True)
def _tables():
    # create the schema once, the app only does this in its startup hook
    from backend.models.database import init_db
    init_db()
