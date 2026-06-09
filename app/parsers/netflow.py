"""NetFlow v9 (RFC 3954) and IPFIX / NetFlow v10 (RFC 7011) packet parser.

Both formats are template-based and share IANA field IDs, so one set of
field handlers covers both. The differences we handle:
  - Header: v9 is 20 bytes (version, count, ...); IPFIX is 16 bytes
    (version, length, exporttime, seq, observation-domain).
  - Set IDs: v9 template=0 / options=1; IPFIX template=2 / options=3;
    data >=256 in both.
  - IPFIX field definitions set the high bit (0x8000) of the field type
    for enterprise-specific fields, followed by a 4-byte enterprise
    number; we consume those bytes but never match such fields.

The router emits templates periodically; data records arriving before
their template has been seen are silently dropped and re-arrive next cycle.
"""
from __future__ import annotations

import socket
import struct
from dataclasses import dataclass

NETFLOW_V9 = 9
IPFIX = 10           # a.k.a. NetFlow v10
HEADER_SIZE = 20     # v9 header; IPFIX header is 16 (see _HEADER_SIZE)
_HEADER_SIZE = {NETFLOW_V9: 20, IPFIX: 16}
# (template_set_id, options_set_id) per version.
_TEMPLATE_SET = {NETFLOW_V9: 0, IPFIX: 2}

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
# Absolute flow timestamps (IPFIX). The Vigor 2765 exports these INSTEAD of the
# sysUptime-relative FIRST/LAST_SWITCHED (21/22) — which is why those came back
# 0 and every record read as duration 0. Seconds and millisecond variants both
# exist; we normalise everything to epoch-milliseconds. 323 is a single
# observation timestamp (NAT-event template) with no separate start/end.
F_FLOW_START_SECONDS = 150
F_FLOW_END_SECONDS = 151
F_FLOW_START_MS = 152
F_FLOW_END_MS = 153
F_OBSERVATION_TIME_MS = 323


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
    # sysUpTime (ms since router boot) at the first/last packet of the flow
    # (NetFlow v9, fields 21/22). 0 = not supplied.
    first_switched: int = 0
    last_switched: int = 0
    # Absolute flow start/end in epoch-milliseconds, when the exporter supplies
    # them (IPFIX 150-153 / 323). Preferred over first/last_switched because
    # they need no sysUptime reference to interpret and pin the bytes to a real
    # wall-clock interval. 0 = not supplied.
    flow_start_ms: int = 0
    flow_end_ms: int = 0


# A template is a list of (field_type, field_length_bytes) tuples.
Template = list[tuple[int, int]]


def _format_mac(chunk: bytes) -> str | None:
    if all(b == 0 for b in chunk):
        return None  # all-zero MAC is a placeholder, not a real device
    return ":".join(f"{b:02X}" for b in chunk)


def parse_packet(data: bytes, templates: dict[int, Template]) -> list[FlowRecord]:
    """Parse a NetFlow v9 or IPFIX (v10) datagram. `templates` is mutated
    with any new templates seen."""
    if len(data) < 16:
        return []
    version = struct.unpack_from("!H", data, 0)[0]
    header_size = _HEADER_SIZE.get(version)
    if header_size is None or len(data) < header_size:
        return []
    template_set_id = _TEMPLATE_SET[version]
    is_ipfix = version == IPFIX
    records: list[FlowRecord] = []
    offset = header_size
    n = len(data)
    while offset + 4 <= n:
        set_id, set_len = struct.unpack_from("!HH", data, offset)
        if set_len < 4 or offset + set_len > n:
            break
        body = data[offset + 4:offset + set_len]
        if set_id == template_set_id:
            _read_templates(body, templates, is_ipfix)
        elif set_id >= 256:
            tmpl = templates.get(set_id)
            if tmpl is not None:
                records.extend(_read_data_records(body, tmpl))
        # options template (v9 set 1 / IPFIX set 3) — ignored
        offset += set_len
    return records


def _read_templates(body: bytes, templates: dict[int, Template], is_ipfix: bool = False) -> None:
    offset = 0
    n = len(body)
    while offset + 4 <= n:
        tmpl_id, field_count = struct.unpack_from("!HH", body, offset)
        offset += 4
        if tmpl_id < 256:
            return  # invalid; data set IDs are >=256
        fields: Template = []
        for _ in range(field_count):
            if offset + 4 > n:
                return
            ftype, flen = struct.unpack_from("!HH", body, offset)
            offset += 4
            if is_ipfix and (ftype & 0x8000):
                # Enterprise-specific field: a 4-byte enterprise number
                # follows. Consume it and mark the field so it's skipped
                # (its numeric ID is in a vendor namespace, not IANA).
                if offset + 4 > n:
                    return
                offset += 4
                ftype = -1
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
            elif ftype == F_FIRST_SWITCHED:
                rec.first_switched = int.from_bytes(chunk, "big")
            elif ftype == F_LAST_SWITCHED:
                rec.last_switched = int.from_bytes(chunk, "big")
            elif ftype == F_FLOW_START_MS:
                rec.flow_start_ms = int.from_bytes(chunk, "big")
            elif ftype == F_FLOW_END_MS:
                rec.flow_end_ms = int.from_bytes(chunk, "big")
            elif ftype == F_FLOW_START_SECONDS:
                rec.flow_start_ms = int.from_bytes(chunk, "big") * 1000
            elif ftype == F_FLOW_END_SECONDS:
                rec.flow_end_ms = int.from_bytes(chunk, "big") * 1000
            elif ftype == F_OBSERVATION_TIME_MS:
                # Single timestamp (NAT-event template): no separate start/end.
                rec.flow_start_ms = rec.flow_end_ms = int.from_bytes(chunk, "big")
            # Other field types are silently skipped (still consume bytes via flen).
        records.append(rec)
        pos += rec_size
    return records
