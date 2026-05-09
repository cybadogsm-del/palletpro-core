import os
import sqlite3
import uuid
from datetime import UTC, datetime


DB = os.environ.get("PALLET_PRO_DB", "pallet_pro.db")


def get_conn():
    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row
    return conn


def make_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


def now_iso() -> str:
    return datetime.now(UTC).replace(tzinfo=None).isoformat()
