"""NetFlow v9 packet parser (RFC 3954).

A v9 datagram has a 20-byte header followed by one or more FlowSets:
  - Template FlowSet (id=0): defines field layout of upcoming data records
  - Options Template (id=1): vendor metadata — we ignore these
  - Data FlowSet (id>=256): packed records matching template id == FlowSet id

The router emits templates periodically (typically every ~30 packets or
every 30s). Data records arriving before their template has been seen are
silently dropped — they'll arrive again next cycle.
"""
from __future__ import annotations

import socket
import struct
from dataclasses import dataclass

NETFLOW_V9 = 9
HEADER_SIZE = 20

# Field type IDs we extract (RFC 3954 §8). Anything else is parsed-and-skipped.
F_IN_BYTES = 1
F_IN_PKTS = 2
F_PROTOCOL = 4
F_L4_SRC_PORT = 7
F_IPV4_SRC_ADDR = 8
F_L4_DST_PORT = 11
F_IPV4_DST_ADDR = 12
F_LAST_SWITCHED = 21
F_FIRST_SWITCHED = 22
F_OUT_BYTES = 23
F_OUT_PKTS = 24
F_IPV6_SRC_ADDR = 27
F_IPV6_DST_ADDR = 28
F_IN_SRC_MAC = 56
F_OUT_DST_MAC = 57
F_IN_DST_MAC = 80
F_OUT_SRC_MAC = 81


@dataclass
class FlowRecord:
    src: str | None = None
    dst: str | None = None
    src_mac: str | None = None
    dst_mac: str | None = None
    src_port: int | None = None
    dst_port: int | None = None
    protocol: int | None = None
    in_bytes: int = 0
    out_bytes: int = 0
    in_packets: int = 0
    out_packets: int = 0


# A template is a list of (field_type, field_length_bytes) tuples.
Template = list[tuple[int, int]]


def _format_mac(chunk: bytes) -> str | None:
    if all(b == 0 for b in chunk):
        return None  # all-zero MAC is a placeholder, not a real device
    return ":".join(f"{b:02X}" for b in chunk)


def parse_packet(data: bytes, templates: dict[int, Template]) -> list[FlowRecord]:
    """Parse a v9 datagram. `templates` is mutated with any new templates."""
    if len(data) < HEADER_SIZE:
        return []
    if struct.unpack_from("!H", data, 0)[0] != NETFLOW_V9:
        return []
    records: list[FlowRecord] = []
    offset = HEADER_SIZE
    n = len(data)
    while offset + 4 <= n:
        fset_id, fset_len = struct.unpack_from("!HH", data, offset)
        if fset_len < 4 or offset + fset_len > n:
            break
        body = data[offset + 4:offset + fset_len]
        if fset_id == 0:
            _read_templates(body, templates)
        elif fset_id >= 256:
            tmpl = templates.get(fset_id)
            if tmpl is not None:
                records.extend(_read_data_records(body, tmpl))
        # fset_id == 1 (options template) — ignored
        offset += fset_len
    return records


def _read_templates(body: bytes, templates: dict[int, Template]) -> None:
    offset = 0
    n = len(body)
    while offset + 4 <= n:
        tmpl_id, field_count = struct.unpack_from("!HH", body, offset)
        offset += 4
        if tmpl_id < 256:
            return  # invalid; data flowset IDs >=256
        fields: Template = []
        for _ in range(field_count):
            if offset + 4 > n:
                return
            ftype, flen = struct.unpack_from("!HH", body, offset)
            offset += 4
            fields.append((ftype, flen))
        templates[tmpl_id] = fields


def _read_data_records(body: bytes, fields: Template) -> list[FlowRecord]:
    rec_size = sum(flen for _, flen in fields)
    if rec_size <= 0:
        return []
    records: list[FlowRecord] = []
    pos = 0
    n = len(body)
    while pos + rec_size <= n:
        rec = FlowRecord()
        fpos = pos
        for ftype, flen in fields:
            chunk = body[fpos:fpos + flen]
            fpos += flen
            if ftype == F_IPV4_SRC_ADDR and flen == 4:
                rec.src = "%d.%d.%d.%d" % tuple(chunk)
            elif ftype == F_IPV4_DST_ADDR and flen == 4:
                rec.dst = "%d.%d.%d.%d" % tuple(chunk)
            elif ftype == F_IPV6_SRC_ADDR and flen == 16:
                rec.src = socket.inet_ntop(socket.AF_INET6, bytes(chunk))
            elif ftype == F_IPV6_DST_ADDR and flen == 16:
                rec.dst = socket.inet_ntop(socket.AF_INET6, bytes(chunk))
            elif ftype in (F_IN_SRC_MAC, F_OUT_SRC_MAC) and flen == 6:
                rec.src_mac = _format_mac(chunk)
            elif ftype in (F_IN_DST_MAC, F_OUT_DST_MAC) and flen == 6:
                rec.dst_mac = _format_mac(chunk)
            elif ftype == F_L4_SRC_PORT:
                rec.src_port = int.from_bytes(chunk, "big")
            elif ftype == F_L4_DST_PORT:
                rec.dst_port = int.from_bytes(chunk, "big")
            elif ftype == F_PROTOCOL and flen >= 1:
                rec.protocol = chunk[0]
            elif ftype == F_IN_BYTES:
                rec.in_bytes = int.from_bytes(chunk, "big")
            elif ftype == F_OUT_BYTES:
                rec.out_bytes = int.from_bytes(chunk, "big")
            elif ftype == F_IN_PKTS:
                rec.in_packets = int.from_bytes(chunk, "big")
            elif ftype == F_OUT_PKTS:
                rec.out_packets = int.from_bytes(chunk, "big")
            # Other field types are silently skipped (still consume bytes via flen).
        records.append(rec)
        pos += rec_size
    return records
