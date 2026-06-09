"""Regression tests for NetFlow per-device rate computation.

These pin the fix for the multi-Gbps "phantom spike" bug. A flow record whose
router-reported duration (FIRST_SWITCHED/LAST_SWITCHED) collapsed to near-zero
used to make `bytes / duration` explode into Gbps — a steady 10 Mbps download
read as ~7 Gbps. Rates are now a sliding wall-clock byte average
(Σbytes in RATE_WINDOW_S × 8 ÷ window), which is physically bounded by the link
no matter what timestamps the router sends.

No third-party deps — pure stdlib unittest. Run from the repo root:

    python -m unittest tests.test_netflow_rate

or inside the container:

    docker compose exec draymon python -m unittest tests.test_netflow_rate
"""
import os
import sys
import unittest

# Allow running both as `python -m unittest tests.test_netflow_rate` from the
# repo root and as `python tests/test_netflow_rate.py`.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import app.db as db
from app.collectors import netflow as nf
from app.parsers.netflow import FlowRecord

LAN_IP = "192.168.1.10"
LAN_MAC = "AA:BB:CC:00:00:10"


class FakeClock:
    """Deterministic stand-in for the module's `time`, exposing the two calls
    the collector makes: monotonic() (rates/eviction) and time() (packet ts)."""

    def __init__(self, mono: float = 1_000.0, wall: int = 1_700_000_000) -> None:
        self._mono = mono
        self._wall = wall

    def monotonic(self) -> float:
        return self._mono

    def time(self) -> int:
        return int(self._wall)

    def advance(self, secs: float) -> None:
        self._mono += secs
        self._wall += secs


def _lan_record(out_bytes: int = 0, in_bytes: int = 0,
                first_ms: int = 1_000, last_ms: int = 1_086,
                src_port: int = 40_000) -> FlowRecord:
    """An 'outbound'-classified record (src is the LAN device), the shape the
    DrayTek exports for a connection initiated from the LAN: in_bytes = LAN
    upload (TX), out_bytes = LAN download (RX). A tiny first→last gap is the
    collapsed-duration bug trigger. src_mac is set so attribution needs no DB."""
    return FlowRecord(
        src=LAN_IP, dst="8.8.8.8", src_mac=LAN_MAC,
        src_port=src_port, dst_port=443, protocol=6,
        in_bytes=in_bytes, out_bytes=out_bytes,
        first_switched=first_ms, last_switched=last_ms,
    )


class NetflowRateTest(unittest.TestCase):
    def setUp(self) -> None:
        # Deterministic clock and a fixed 60s window, independent of any local
        # .env override of NETFLOW_RATE_WINDOW_S.
        self.clock = FakeClock()
        self._real_time = nf.time
        nf.time = self.clock
        self._real_window = nf.RATE_WINDOW_S
        nf.RATE_WINDOW_S = 60.0

        # Capture samples instead of writing SQLite.
        self.samples: list[tuple[str, float, float]] = []
        self._real_insert = db.insert_sample
        db.insert_sample = (
            lambda mac, tx, rx, sessions=None: self.samples.append((mac, tx, rx))
        )

        self.c = nf.NetflowCollector()

    def tearDown(self) -> None:
        nf.time = self._real_time
        nf.RATE_WINDOW_S = self._real_window
        db.insert_sample = self._real_insert

    def test_collapsed_duration_does_not_spike(self) -> None:
        # 75 MB downloaded over a ~60s active-timeout export, but the router
        # reports the flow window as 86 ms — the exact shape that produced the
        # ~7 Gbps spike when rate was bytes / reported-duration.
        self.c._attribute(_lan_record(out_bytes=75_000_000, first_ms=1_000, last_ms=1_086))
        self.c._flush()

        self.assertEqual(len(self.samples), 1)
        mac, tx, rx = self.samples[-1]
        self.assertEqual(mac, LAN_MAC)
        # Correct rate: 75 MB × 8 / 60s window = 10 Mbps.
        self.assertAlmostEqual(rx, 10_000_000, delta=1.0)
        # What the old duration-based denominator would have produced:
        phantom = 75_000_000 * 8 / 0.086  # ≈ 6.98 Gbps
        self.assertLess(rx, phantom / 100)  # we're >100x below the phantom spike

    def test_reexports_sum_without_double_count(self) -> None:
        # Two delta-semantics exports of the same flow (same 5-tuple) within one
        # window total 75 MB; they must sum to 10 Mbps, not double-count.
        self.c._attribute(_lan_record(out_bytes=37_500_000, first_ms=1_000, last_ms=1_005))
        self.c._attribute(_lan_record(out_bytes=37_500_000, first_ms=6_000, last_ms=6_005))
        self.c._flush()

        _, _, rx = self.samples[-1]
        self.assertAlmostEqual(rx, 10_000_000, delta=1.0)

    def test_upload_and_download_split(self) -> None:
        # in_bytes → TX (upload), out_bytes → RX (download), each averaged over
        # the window independently.
        self.c._attribute(_lan_record(in_bytes=1_200_000, out_bytes=6_000_000))
        self.c._flush()

        _, tx, rx = self.samples[-1]
        self.assertAlmostEqual(tx, 1_200_000 * 8 / 60.0, delta=1.0)  # 160 kbps
        self.assertAlmostEqual(rx, 6_000_000 * 8 / 60.0, delta=1.0)  # 800 kbps

    def test_rate_decays_to_zero_after_window(self) -> None:
        self.c._attribute(_lan_record(out_bytes=75_000_000))
        self.c._flush()
        self.assertAlmostEqual(self.samples[-1][2], 10_000_000, delta=1.0)

        # Advance past the window with no new traffic: the deposit ages out of
        # the sliding window and the device gets exactly one explicit zero so
        # the UI drops it to idle (instead of holding the stale rate forever).
        self.clock.advance(nf.RATE_WINDOW_S + nf.FLUSH_INTERVAL_S)
        self.c._flush()
        self.assertEqual(self.samples[-1], (LAN_MAC, 0.0, 0.0))

        # A further idle flush writes nothing more (device already idle).
        before = len(self.samples)
        self.clock.advance(nf.FLUSH_INTERVAL_S)
        self.c._flush()
        self.assertEqual(len(self.samples), before)

    def test_diagnostic_mode_writes_nothing_but_stays_bounded(self) -> None:
        self.c.write_samples = False
        self.c._attribute(_lan_record(out_bytes=75_000_000))
        self.c._flush()
        self.assertEqual(self.samples, [])  # nothing persisted

        # History is still evicted so the accumulators can't grow unbounded.
        self.clock.advance(nf.RATE_WINDOW_S + nf.FLUSH_INTERVAL_S)
        self.c._flush()
        self.assertEqual(self.c._history, {})


if __name__ == "__main__":
    unittest.main()
