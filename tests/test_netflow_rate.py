"""Regression tests for NetFlow per-device rate computation.

These pin the fix for the "end-of-download spike". This Vigor holds a long flow
and exports its ENTIRE byte count in one record when the flow ends, stamped with
flow_start/flow_end spanning the whole transfer (e.g. 1.6 GB over 283 s). The old
code credited those bytes to the instant the record arrived, dumping minutes of
traffic into one window → a multi-hundred-Mbps phantom spike. The collector now
SPREADS each flow's bytes across the time buckets it actually spanned, so the
rate reads ~45 Mbps placed across the 283 s it really happened.

No third-party deps — pure stdlib unittest. Run from the repo root:

    python -m unittest tests.test_netflow_rate

or inside the container:

    docker compose exec draymon python -m unittest tests.test_netflow_rate
"""
import os
import sys
import tempfile
import time
import unittest

# Allow running both as `python -m unittest tests.test_netflow_rate` from the
# repo root and as `python tests/test_netflow_rate.py`.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import app.db as db
from app.collectors import netflow as nf
from app.parsers.netflow import FlowRecord

LAN_IP = "192.168.1.10"
LAN_MAC = "AA:BB:CC:00:00:10"
# A realistic arrival wall-clock time (when the end-of-flow record lands).
ARRIVAL_TS = 1_780_969_166


class FakeClock:
    """Deterministic stand-in for the module's `time`. _flush uses monotonic()
    only; _attribute reads the collector's last_packet_ts, which we set."""

    def __init__(self, mono: float = 1_000.0) -> None:
        self._mono = mono

    def monotonic(self) -> float:
        return self._mono

    def time(self) -> int:
        return ARRIVAL_TS


def _lan_record(out_bytes: int = 0, in_bytes: int = 0,
                flow_start_ms: int = 0, flow_end_ms: int = 0,
                src_port: int = 40_000) -> FlowRecord:
    """An 'outbound'-classified record (src is the LAN device), the shape the
    Vigor exports for a LAN-initiated connection: in_bytes = LAN upload (TX),
    out_bytes = LAN download (RX). flow_start/end_ms are router-clock ms; only
    their difference matters. src_mac is set so attribution needs no DB."""
    return FlowRecord(
        src=LAN_IP, dst="185.125.190.40", src_mac=LAN_MAC,
        src_port=src_port, dst_port=443, protocol=6,
        in_bytes=in_bytes, out_bytes=out_bytes,
        flow_start_ms=flow_start_ms, flow_end_ms=flow_end_ms,
    )


class SpreadTest(unittest.TestCase):
    def setUp(self) -> None:
        self.clock = FakeClock()
        self._real_time = nf.time
        nf.time = self.clock
        self.rows: list[tuple] = []
        self._real_add = db.add_samples
        db.add_samples = lambda rows: self.rows.extend(rows)
        self.c = nf.NetflowCollector()
        self.c.last_packet_ts = ARRIVAL_TS
        self.bucket = nf.SAMPLE_BUCKET_S

    def tearDown(self) -> None:
        nf.time = self._real_time
        db.add_samples = self._real_add

    def _rx_at(self):
        """rate values written, by bucket ts -> rx_bps (after _flush)."""
        return {ts: rx for (_mac, ts, _tx, rx) in self.rows}

    def test_long_download_is_spread_not_spiked(self) -> None:
        # 1.586 GB downloaded over 283 s, exported as ONE record at flow end.
        total = 1_586_000_000
        dur_s = 283
        self.c._attribute(_lan_record(
            out_bytes=total, flow_start_ms=1_000_000, flow_end_ms=1_000_000 + dur_s * 1000,
        ))
        self.c._flush()

        rx = self._rx_at()
        self.assertGreater(len(rx), 25)  # ~29 buckets of 10 s across 283 s

        # True average rate: 1.586 GB × 8 / 283 s ≈ 44.8 Mbps. Interior buckets
        # read that; partial edge buckets read a bit less. Nothing reads the
        # phantom value the old code produced (all bytes in one 10 s bucket =
        # 1.586e9 × 8 / 10 ≈ 1.27 Gbps; or in one 60 s window ≈ 211 Mbps).
        peak = max(rx.values())
        self.assertLess(peak, 55_000_000)          # not a spike
        self.assertGreater(peak, 40_000_000)        # but the right magnitude
        self.assertAlmostEqual(peak, total * 8 / dur_s, delta=peak * 0.02)

        # Conservation: Σ(rate × bucket ÷ 8) must equal the bytes downloaded.
        bytes_back = sum(v * self.bucket / 8 for v in rx.values())
        self.assertAlmostEqual(bytes_back, total, delta=total * 0.001)

        # Every bucket sits within the flow's real wall-clock span.
        self.assertTrue(all(ARRIVAL_TS - dur_s - self.bucket <= ts <= ARRIVAL_TS
                            for ts in rx))

    def test_zero_duration_record_lands_in_one_bucket(self) -> None:
        # No usable timestamps (the common small control flow) → all bytes in
        # the single bucket at the arrival time.
        self.c._attribute(_lan_record(out_bytes=5000))  # flow_start/end_ms = 0
        self.c._flush()
        rx = self._rx_at()
        self.assertEqual(len(rx), 1)
        ts, val = next(iter(rx.items()))
        self.assertEqual(ts % self.bucket, 0)
        self.assertAlmostEqual(val, 5000 * 8 / self.bucket, delta=1.0)

    def test_tx_and_rx_split(self) -> None:
        # in_bytes → TX (upload), out_bytes → RX (download), zero duration.
        self.c._attribute(_lan_record(in_bytes=1000, out_bytes=4000))
        self.c._flush()
        self.assertEqual(len(self.rows), 1)
        _mac, _ts, tx, rx = self.rows[0]
        self.assertAlmostEqual(tx, 1000 * 8 / self.bucket, delta=1.0)
        self.assertAlmostEqual(rx, 4000 * 8 / self.bucket, delta=1.0)

    def test_diagnostic_mode_writes_nothing(self) -> None:
        self.c.write_samples = False
        self.c._attribute(_lan_record(out_bytes=1_000_000,
                                      flow_start_ms=1_000, flow_end_ms=51_000))
        self.c._flush()
        self.assertEqual(self.rows, [])
        self.assertEqual(self.c._pending, {})  # cleared, can't grow unbounded


class UpsertAddTest(unittest.TestCase):
    """db.add_samples must ADD into an existing (mac, ts) bucket, so a later
    flow that overlaps an already-written past bucket accumulates rather than
    overwrites."""

    def setUp(self) -> None:
        self.tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.tmp.close()
        self._real_path = db.settings.db_path
        db.settings.db_path = self.tmp.name
        db.init_db()

    def tearDown(self) -> None:
        db.settings.db_path = self._real_path
        os.unlink(self.tmp.name)

    def test_add_accumulates_per_bucket(self) -> None:
        db.add_samples([("AA", 1000, 10.0, 100.0)])
        db.add_samples([("AA", 1000, 5.0, 50.0)])   # same bucket → adds
        db.add_samples([("AA", 1010, 1.0, 2.0)])     # different bucket
        pts = db.history_for("AA", 0)
        by_ts = {p["ts"]: (p["tx_bps"], p["rx_bps"]) for p in pts}
        self.assertEqual(by_ts[1000], (15.0, 150.0))
        self.assertEqual(by_ts[1010], (1.0, 2.0))


class VolumeTest(unittest.TestCase):
    """The volume queries must exactly invert the rate buckets: each sample row
    is a bps rate over one SAMPLE_BUCKET_S bucket, so bytes = bps × bucket ÷ 8.
    These are the numbers the UI presents as exact, so they must conserve."""

    def setUp(self) -> None:
        self.tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.tmp.close()
        self._real_path = db.settings.db_path
        db.settings.db_path = self.tmp.name
        db.init_db()

    def tearDown(self) -> None:
        db.settings.db_path = self._real_path
        os.unlink(self.tmp.name)

    def test_usage_for_bins_and_inverts_rates(self) -> None:
        b = db.SAMPLE_BUCKET_S
        tx_bytes = 80.0 * b / 8   # one bucket's worth at 80 bps
        rx_bytes = 800.0 * b / 8
        db.add_samples([("AA", 3600, 80.0, 800.0)])
        db.add_samples([("AA", 3600 + b, 80.0, 800.0)])  # same hour bin
        db.add_samples([("AA", 7200, 80.0, 0.0)])        # next hour bin
        pts = db.usage_for("AA", 0, 3600)
        by = {p["ts"]: (p["tx_bytes"], p["rx_bytes"]) for p in pts}
        self.assertEqual(set(by), {3600, 7200})
        self.assertAlmostEqual(by[3600][0], 2 * tx_bytes)
        self.assertAlmostEqual(by[3600][1], 2 * rx_bytes)
        self.assertAlmostEqual(by[7200][0], tx_bytes)
        self.assertAlmostEqual(by[7200][1], 0.0)

    def test_device_volume_windows(self) -> None:
        b = db.SAMPLE_BUCKET_S
        db.upsert_device("AA", "192.168.1.10", "thing")
        now = int(time.time())
        db.add_samples([("AA", now - 120, 8.0, 16.0)])    # inside 1h and 24h
        db.add_samples([("AA", now - 7200, 8.0, 0.0)])    # inside 24h only
        db.add_samples([("AA", now - 90000, 8.0, 0.0)])   # >24h old — excluded
        d = {r["mac"]: r for r in db.list_devices_with_current()}["AA"]
        self.assertAlmostEqual(d["vol_tx_1h"], 8.0 * b / 8)
        self.assertAlmostEqual(d["vol_rx_1h"], 16.0 * b / 8)
        self.assertAlmostEqual(d["vol_tx_24h"], 2 * 8.0 * b / 8)
        self.assertAlmostEqual(d["vol_rx_24h"], 16.0 * b / 8)

    def test_device_without_samples_has_zero_volumes(self) -> None:
        db.upsert_device("BB", "192.168.1.11", "idle-thing")
        d = {r["mac"]: r for r in db.list_devices_with_current()}["BB"]
        self.assertEqual(d["vol_tx_1h"], 0.0)
        self.assertEqual(d["vol_rx_1h"], 0.0)
        self.assertEqual(d["vol_tx_24h"], 0.0)
        self.assertEqual(d["vol_rx_24h"], 0.0)

    def test_usage_total_sums_all_devices(self) -> None:
        b = db.SAMPLE_BUCKET_S
        db.add_samples([("AA", 3600, 80.0, 0.0)])
        db.add_samples([("BB", 3600 + b, 80.0, 0.0)])  # same hour bin, other device
        db.add_samples([("AA", 7200, 80.0, 0.0)])      # next hour bin
        pts = db.usage_total(0, 3600)
        by = {p["ts"]: p["tx_bytes"] for p in pts}
        self.assertEqual(set(by), {3600, 7200})
        self.assertAlmostEqual(by[3600], 2 * 80.0 * b / 8)  # AA + BB
        self.assertAlmostEqual(by[7200], 80.0 * b / 8)


if __name__ == "__main__":
    unittest.main()
