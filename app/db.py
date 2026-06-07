import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path

from .config import settings


SCHEMA = """
CREATE TABLE IF NOT EXISTS devices (
    mac TEXT PRIMARY KEY,
    ip TEXT,
    hostname TEXT,
    vendor TEXT,
    notes TEXT,
    first_seen INTEGER NOT NULL,
    last_seen INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS samples (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    mac TEXT NOT NULL,
    ts INTEGER NOT NULL,
    tx_bps REAL NOT NULL,
    rx_bps REAL NOT NULL,
    sessions INTEGER
);

-- Per-WAN rate computed from cumulative byte-counter deltas in
-- `show statistic` between polls — independent of the NetFlow per-device path.
CREATE TABLE IF NOT EXISTS wan_samples (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    wan TEXT NOT NULL,
    ts INTEGER NOT NULL,
    tx_bps REAL NOT NULL,
    rx_bps REAL NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_samples_mac_ts ON samples(mac, ts);
CREATE INDEX IF NOT EXISTS idx_samples_ts ON samples(ts);
CREATE INDEX IF NOT EXISTS idx_wan_samples_wan_ts ON wan_samples(wan, ts);
CREATE INDEX IF NOT EXISTS idx_wan_samples_ts ON wan_samples(ts);
"""


def init_db() -> None:
    Path(settings.db_path).parent.mkdir(parents=True, exist_ok=True)
    with conn() as c:
        c.executescript(SCHEMA)


@contextmanager
def conn():
    c = sqlite3.connect(settings.db_path)
    c.row_factory = sqlite3.Row
    try:
        yield c
        c.commit()
    finally:
        c.close()


def upsert_device(mac: str, ip: str, hostname: str | None, vendor: str | None = None) -> None:
    now = int(time.time())
    with conn() as c:
        c.execute(
            """
            INSERT INTO devices (mac, ip, hostname, vendor, first_seen, last_seen)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(mac) DO UPDATE SET
                ip=excluded.ip,
                hostname=COALESCE(excluded.hostname, devices.hostname),
                vendor=COALESCE(excluded.vendor, devices.vendor),
                last_seen=excluded.last_seen
            """,
            (mac, ip, hostname, vendor, now, now),
        )


def insert_sample(mac: str, tx_bps: float, rx_bps: float, sessions: int | None = None) -> None:
    with conn() as c:
        c.execute(
            "INSERT INTO samples (mac, ts, tx_bps, rx_bps, sessions) VALUES (?, ?, ?, ?, ?)",
            (mac, int(time.time()), tx_bps, rx_bps, sessions),
        )


def list_devices_with_current() -> list[dict]:
    with conn() as c:
        rows = c.execute(
            """
            SELECT d.mac, d.ip, d.hostname, d.vendor, d.notes,
                   d.first_seen, d.last_seen,
                   (SELECT tx_bps FROM samples s WHERE s.mac = d.mac ORDER BY ts DESC LIMIT 1) AS tx_bps,
                   (SELECT rx_bps FROM samples s WHERE s.mac = d.mac ORDER BY ts DESC LIMIT 1) AS rx_bps,
                   (SELECT ts     FROM samples s WHERE s.mac = d.mac ORDER BY ts DESC LIMIT 1) AS last_sample
            FROM devices d
            ORDER BY (tx_bps + rx_bps) DESC NULLS LAST, d.hostname
            """
        ).fetchall()
    return [dict(r) for r in rows]


def table_counts() -> dict:
    """Row counts per table — used by the diagnostics panel to confirm the
    DB is readable/writable and show how much history exists."""
    with conn() as c:
        return {
            "devices": c.execute("SELECT COUNT(*) AS n FROM devices").fetchone()["n"],
            "samples": c.execute("SELECT COUNT(*) AS n FROM samples").fetchone()["n"],
            "wan_samples": c.execute("SELECT COUNT(*) AS n FROM wan_samples").fetchone()["n"],
        }


def mac_for_ip(ip: str) -> str | None:
    """Reverse-lookup IP → MAC from the devices table. Used by the
    NetFlow collector to attribute flow records back to a known device."""
    with conn() as c:
        row = c.execute("SELECT mac FROM devices WHERE ip = ?", (ip,)).fetchone()
    return row["mac"] if row else None


def history_for(mac: str, since_ts: int) -> list[dict]:
    with conn() as c:
        rows = c.execute(
            "SELECT ts, tx_bps, rx_bps, sessions FROM samples WHERE mac = ? AND ts >= ? ORDER BY ts ASC",
            (mac, since_ts),
        ).fetchall()
    return [dict(r) for r in rows]


def prune_old_samples(retention_days: int) -> int:
    cutoff = int(time.time()) - retention_days * 86400
    with conn() as c:
        a = c.execute("DELETE FROM samples WHERE ts < ?", (cutoff,)).rowcount
        b = c.execute("DELETE FROM wan_samples WHERE ts < ?", (cutoff,)).rowcount
        return a + b


def set_device_note(mac: str, note: str) -> None:
    with conn() as c:
        c.execute("UPDATE devices SET notes = ? WHERE mac = ?", (note, mac))


def insert_wan_sample(wan: str, tx_bps: float, rx_bps: float) -> None:
    with conn() as c:
        c.execute(
            "INSERT INTO wan_samples (wan, ts, tx_bps, rx_bps) VALUES (?, ?, ?, ?)",
            (wan, int(time.time()), tx_bps, rx_bps),
        )


def list_wan_current() -> list[dict]:
    """Latest tx_bps/rx_bps per WAN."""
    with conn() as c:
        rows = c.execute(
            """
            SELECT wans.wan,
                   (SELECT tx_bps FROM wan_samples w WHERE w.wan = wans.wan ORDER BY ts DESC LIMIT 1) AS tx_bps,
                   (SELECT rx_bps FROM wan_samples w WHERE w.wan = wans.wan ORDER BY ts DESC LIMIT 1) AS rx_bps,
                   (SELECT ts     FROM wan_samples w WHERE w.wan = wans.wan ORDER BY ts DESC LIMIT 1) AS ts
            FROM (SELECT DISTINCT wan FROM wan_samples) wans
            ORDER BY wans.wan
            """
        ).fetchall()
    return [dict(r) for r in rows]


def wan_history(wan: str, since_ts: int) -> list[dict]:
    with conn() as c:
        rows = c.execute(
            "SELECT ts, tx_bps, rx_bps FROM wan_samples WHERE wan = ? AND ts >= ? ORDER BY ts ASC",
            (wan, since_ts),
        ).fetchall()
    return [dict(r) for r in rows]
