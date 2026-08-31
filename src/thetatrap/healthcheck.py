"""Container health check based only on the shared SQLite heartbeat."""

from __future__ import annotations

import os
import sys
from datetime import UTC, datetime

from thetatrap.storage import Store


def main() -> int:
    path = os.environ.get("THETATRAP_DATABASE_PATH", "/data/thetatrap.sqlite3")
    health = Store(path).latest_health()
    if not health or health.get("status") != "healthy":
        return 1
    observed = datetime.fromisoformat(str(health["observed_at"]))
    age = (datetime.now(UTC) - observed.astimezone(UTC)).total_seconds()
    return 0 if age <= 120 else 1


if __name__ == "__main__":
    sys.exit(main())
