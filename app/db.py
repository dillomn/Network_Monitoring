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

-- Live per-device rates from the router's Data Flow Monitor (`show traffic
-- <ip>` over SSH), bucketed like `samples`. This is the fallback layer that
-- makes in-progress transfers visible: the NetFlow exporter reports a flow
-- only when it ENDS, so mid-download these rows are the only signal. Queries
-- merge the two with per-bucket MAX — NetFlow's exact figures win wherever
-- they exist, DFM estimates fill the holes (in-flight flows, or flows the
-- exporter never reported at all).
CREATE TABLE IF NOT EXISTS live_samples (
    mac TEXT NOT NULL,
    ts INTEGER NOT NULL,
    tx_bps REAL NOT NULL,
    rx_bps REAL NOT NULL,
    PRIMARY KEY (mac, ts)
) WITHOUT ROWID;

CREATE INDEX IF NOT EXISTS idx_samples_mac_ts ON samples(mac, ts);
CREATE INDEX IF NOT EXISTS idx_samples_ts ON samples(ts);
CREATE INDEX IF NOT EXISTS idx_wan_samples_wan_ts ON wan_samples(wan, ts);
CREATE INDEX IF NOT EXISTS idx_wan_samples_ts ON wan_samples(ts);
"""


# A device's "current" rate is the value of its most recent sample bucket, but
# only if that bucket is fresh. Long flows are exported by the router only when
# they end, so once a device falls quiet no new buckets arrive — past this many
# seconds with no sample we report it as idle (0) instead of a stale rate.
CURRENT_STALE_S = 30

# Width of one sample bucket, in seconds. Canonical definition — the NetFlow
# collector spreads each flow's bytes across buckets of this width and stores a
# bps rate per (mac, bucket); the volume queries below invert that
# (bytes = bps × SAMPLE_BUCKET_S ÷ 8). Keep the two sides in lockstep or
# volume totals will silently scale wrong.
SAMPLE_BUCKET_S = 10


def init_db() -> None:
    Path(settings.db_path).parent.mkdir(parents=True, exist_ok=True)
    with conn() as c:
        c.executescript(SCHEMA)
        _migrate_unique_samples(c)


def _migrate_unique_samples(c) -> None:
    """The collector upserts one sample row per (mac, ts) bucket and ADDS each
    flow's contribution to it (a flow's bytes are spread across the buckets it
    spanned, so several flows and flush cycles touch the same bucket). That
    needs a UNIQUE(mac, ts) index for the ON CONFLICT clause.

    Runs once. Older DBs hold samples from the previous model, whose values are
    both wrong (the end-of-download spike) and semantically incompatible — they
    stored an absolute per-flush rate, not an additive per-bucket one, so an
    upsert-add onto a surviving old row would corrupt it. Clear the table for a
    clean slate before adding the unique index."""
    have = c.execute(
        "SELECT 1 FROM sqlite_master WHERE type='index' "
        "AND name='idx_samples_mac_ts_unique'"
    ).fetchone()
    if have:
        return
    c.execute("DELETE FROM samples")
    c.execute("CREATE UNIQUE INDEX idx_samples_mac_ts_unique ON samples(mac, ts)")


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


def add_samples(rows: list[tuple[str, int, float, float]]) -> None:
    """Upsert per-(mac, ts) sample rows, ADDING each call's rate onto whatever
    is already stored for that bucket. The collector spreads one flow's bytes
    across the buckets it spanned, and many flows (across many flush cycles)
    land in the same bucket — adding accumulates them into the true aggregate
    rate. `rows` is (mac, ts, tx_bps, rx_bps)."""
    if not rows:
        return
    with conn() as c:
        c.executemany(
            """
            INSERT INTO samples (mac, ts, tx_bps, rx_bps) VALUES (?, ?, ?, ?)
            ON CONFLICT(mac, ts) DO UPDATE SET
                tx_bps = samples.tx_bps + excluded.tx_bps,
                rx_bps = samples.rx_bps + excluded.rx_bps
            """,
            rows,
        )


def list_devices_with_current() -> list[dict]:
    now = int(time.time())
    h1, h24 = now - 3600, now - 86400
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
        # Transfer volumes over the trailing 1h/24h windows, from the merged
        # series (see _merged_series_sql): NetFlow-exact where flow records
        # landed, DFM-estimated where only live readings exist. Each row is a
        # rate over one SAMPLE_BUCKET_S bucket, so bytes = rate × bucket ÷ 8.
        vols = c.execute(
            f"""
            SELECT mac,
                   SUM(CASE WHEN ts >= :h1 THEN tx_bps ELSE 0 END) * :w / 8.0 AS vol_tx_1h,
                   SUM(CASE WHEN ts >= :h1 THEN rx_bps ELSE 0 END) * :w / 8.0 AS vol_rx_1h,
                   SUM(tx_bps) * :w / 8.0 AS vol_tx_24h,
                   SUM(rx_bps) * :w / 8.0 AS vol_rx_24h
            FROM ({_merged_series_sql(per_mac=False)}) GROUP BY mac
            """,
            {"h1": h1, "since": h24, "w": SAMPLE_BUCKET_S},
        ).fetchall()
    vol_by_mac = {v["mac"]: v for v in vols}
    out = []
    for r in rows:
        d = dict(r)
        # Report a rate only if the latest sample bucket is fresh; otherwise the
        # device is idle and its last-known rate would read as a stale "current".
        ls = d.get("last_sample")
        if ls is None or now - ls > CURRENT_STALE_S:
            d["tx_bps"] = 0.0
            d["rx_bps"] = 0.0
        v = vol_by_mac.get(d["mac"])
        d["vol_tx_1h"] = v["vol_tx_1h"] if v else 0.0
        d["vol_rx_1h"] = v["vol_rx_1h"] if v else 0.0
        d["vol_tx_24h"] = v["vol_tx_24h"] if v else 0.0
        d["vol_rx_24h"] = v["vol_rx_24h"] if v else 0.0
        out.append(d)
    return out


def table_counts() -> dict:
    """Row counts per table — used by the diagnostics panel to confirm the
    DB is readable/writable and show how much history exists."""
    with conn() as c:
        return {
            "devices": c.execute("SELECT COUNT(*) AS n FROM devices").fetchone()["n"],
            "samples": c.execute("SELECT COUNT(*) AS n FROM samples").fetchone()["n"],
            "live_samples": c.execute("SELECT COUNT(*) AS n FROM live_samples").fetchone()["n"],
            "wan_samples": c.execute("SELECT COUNT(*) AS n FROM wan_samples").fetchone()["n"],
        }


def mac_for_ip(ip: str) -> str | None:
    """Reverse-lookup IP → MAC from the devices table. Used by the
    NetFlow collector to attribute flow records back to a known device."""
    with conn() as c:
        row = c.execute("SELECT mac FROM devices WHERE ip = ?", (ip,)).fetchone()
    return row["mac"] if row else None


def upsert_live_sample(mac: str, bucket_ts: int, tx_bps: float, rx_bps: float) -> None:
    """Store one Data Flow Monitor reading for a (mac, bucket). REPLACE: a
    newer reading within the same bucket supersedes the older one. These rows
    are the live fallback layer — merged queries take the per-bucket MAX of
    samples vs live_samples, so NetFlow's exact figures win once they arrive."""
    with conn() as c:
        c.execute(
            "INSERT OR REPLACE INTO live_samples (mac, ts, tx_bps, rx_bps) VALUES (?, ?, ?, ?)",
            (mac, bucket_ts, tx_bps, rx_bps),
        )


def _merged_series_sql(per_mac: bool) -> str:
    """Subquery merging NetFlow `samples` with the live DFM fallback, one row
    per (mac, ts) bucket, MAX per direction. Why MAX and not COALESCE-prefer-
    NetFlow: while a long flow is still open, NetFlow only has the device's
    background chatter for those buckets (tiny), and the DFM reading carries
    the real rate — preferring any NetFlow row would zero out the download.
    MAX keeps the live estimate until the flow-end record backfills a larger
    (exact) figure into the same buckets. Callers bind :since (and :mac)."""
    mac_filter = "AND mac = :mac" if per_mac else ""
    return f"""
        SELECT mac, ts, MAX(tx_bps) AS tx_bps, MAX(rx_bps) AS rx_bps FROM (
            SELECT mac, ts, tx_bps, rx_bps FROM samples
                WHERE ts >= :since {mac_filter}
            UNION ALL
            SELECT mac, ts, tx_bps, rx_bps FROM live_samples
                WHERE ts >= :since {mac_filter}
        ) GROUP BY mac, ts
    """


def history_for(mac: str, since_ts: int) -> list[dict]:
    """Per-bucket rate series for one device, merged from NetFlow samples and
    live DFM readings (see _merged_series_sql)."""
    with conn() as c:
        rows = c.execute(
            f"SELECT ts, tx_bps, rx_bps FROM ({_merged_series_sql(per_mac=True)}) ORDER BY ts ASC",
            {"mac": mac, "since": since_ts},
        ).fetchall()
    return [dict(r) for r in rows]


def usage_for(mac: str, since_ts: int, bucket_s: int) -> list[dict]:
    """Transfer volume per `bucket_s`-wide bin: [{ts, tx_bytes, rx_bytes}].
    `ts` is the bin start (epoch, aligned to bucket_s). Bins with no samples
    are omitted. Built on the merged series: NetFlow-exact where flow records
    landed, DFM-estimated where only live readings exist."""
    with conn() as c:
        rows = c.execute(
            f"""
            SELECT (ts / :b) * :b AS bucket,
                   SUM(tx_bps) * :w / 8.0 AS tx_bytes,
                   SUM(rx_bps) * :w / 8.0 AS rx_bytes
            FROM ({_merged_series_sql(per_mac=True)})
            GROUP BY bucket ORDER BY bucket ASC
            """,
            {"b": int(bucket_s), "w": SAMPLE_BUCKET_S, "mac": mac, "since": since_ts},
        ).fetchall()
    return [{"ts": r["bucket"], "tx_bytes": r["tx_bytes"], "rx_bytes": r["rx_bytes"]} for r in rows]


def usage_total(since_ts: int, bucket_s: int) -> list[dict]:
    """Network-wide transfer volume per bin, all devices summed — the home
    page's "when was the network busy" chart."""
    with conn() as c:
        rows = c.execute(
            f"""
            SELECT (ts / :b) * :b AS bucket,
                   SUM(tx_bps) * :w / 8.0 AS tx_bytes,
                   SUM(rx_bps) * :w / 8.0 AS rx_bytes
            FROM ({_merged_series_sql(per_mac=False)})
            GROUP BY bucket ORDER BY bucket ASC
            """,
            {"b": int(bucket_s), "w": SAMPLE_BUCKET_S, "since": since_ts},
        ).fetchall()
    return [{"ts": r["bucket"], "tx_bytes": r["tx_bytes"], "rx_bytes": r["rx_bytes"]} for r in rows]


def prune_old_samples(retention_days: int) -> int:
    cutoff = int(time.time()) - retention_days * 86400
    with conn() as c:
        a = c.execute("DELETE FROM samples WHERE ts < ?", (cutoff,)).rowcount
        b = c.execute("DELETE FROM wan_samples WHERE ts < ?", (cutoff,)).rowcount
        d = c.execute("DELETE FROM live_samples WHERE ts < ?", (cutoff,)).rowcount
        return a + b + d


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
