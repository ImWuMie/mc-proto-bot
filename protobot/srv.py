"""Minecraft ``SRV`` record lookup.

Vanilla clients resolve ``_minecraft._tcp.<host>`` whenever the user did not
type an explicit port, which is how most public servers publish a backend host
and port that differ from what players type. Python's standard library exposes
no SRV lookup, so this module implements one without third-party dependencies:
the Windows resolver API where available, and plain DNS over UDP elsewhere.

Every failure path returns ``None`` so a missing or broken resolver can never
turn a working direct connection into an error.
"""

from __future__ import annotations

import ipaddress
import secrets
import socket
import struct
import sys

SRV_RECORD_TYPE = 33
_CLASS_IN = 1
_SERVICE_PREFIX = "_minecraft._tcp."
_MAX_UDP_RESPONSE = 4096


def _is_ip_literal(host: str) -> bool:
    try:
        ipaddress.ip_address(host)
    except ValueError:
        return False
    return True


def _encode_name(name: str) -> bytes:
    encoded = bytearray()
    for label in name.rstrip(".").split("."):
        raw = label.encode("idna") if any(ord(ch) > 127 for ch in label) else label.encode("ascii")
        if not 1 <= len(raw) <= 63:
            raise ValueError(f"invalid DNS label {label!r}")
        encoded.append(len(raw))
        encoded += raw
    encoded.append(0)
    return bytes(encoded)


def _decode_name(data: bytes, offset: int) -> tuple[str, int]:
    """Decode a possibly compressed name, returning it and the offset after it."""

    labels: list[str] = []
    end = offset
    followed_pointer = False
    hops = 0
    while True:
        if offset >= len(data):
            raise ValueError("truncated DNS name")
        length = data[offset]
        if length & 0xC0 == 0xC0:
            if offset + 1 >= len(data):
                raise ValueError("truncated DNS compression pointer")
            pointer = ((length & 0x3F) << 8) | data[offset + 1]
            if not followed_pointer:
                end = offset + 2
                followed_pointer = True
            hops += 1
            if hops > 64 or pointer >= len(data):
                raise ValueError("malformed DNS compression pointer")
            offset = pointer
            continue
        if length == 0:
            if not followed_pointer:
                end = offset + 1
            break
        offset += 1
        if offset + length > len(data):
            raise ValueError("truncated DNS label")
        labels.append(data[offset : offset + length].decode("ascii", errors="replace"))
        offset += length
    return ".".join(labels), end


def parse_srv_response(payload: bytes, transaction_id: int) -> list[tuple[int, int, int, str]]:
    """Extract ``(priority, weight, port, target)`` tuples from a DNS answer."""

    if len(payload) < 12:
        raise ValueError("DNS response is too short")
    reply_id, flags, questions, answers, _authority, _additional = struct.unpack(
        ">HHHHHH", payload[:12]
    )
    if reply_id != transaction_id:
        raise ValueError("DNS transaction ID mismatch")
    if flags & 0x000F:  # RCODE: NXDOMAIN and friends simply have no records.
        return []

    offset = 12
    for _ in range(questions):
        _, offset = _decode_name(payload, offset)
        offset += 4

    records: list[tuple[int, int, int, str]] = []
    for _ in range(answers):
        _, offset = _decode_name(payload, offset)
        if offset + 10 > len(payload):
            raise ValueError("truncated DNS answer header")
        record_type, record_class, _ttl, data_length = struct.unpack(
            ">HHIH", payload[offset : offset + 10]
        )
        offset += 10
        if offset + data_length > len(payload):
            raise ValueError("truncated DNS answer data")
        if record_type == SRV_RECORD_TYPE and record_class == _CLASS_IN and data_length >= 7:
            priority, weight, port = struct.unpack(">HHH", payload[offset : offset + 6])
            target, _ = _decode_name(payload, offset + 6)
            records.append((priority, weight, port, target))
        offset += data_length
    return records


def select_srv_target(records: list[tuple[int, int, int, str]]) -> tuple[str, int] | None:
    """Pick a target per RFC 2782: lowest priority wins, higher weight breaks ties.

    A single ``.`` target means the service is explicitly unavailable.
    """

    usable = [
        record
        for record in records
        if record[3] and record[3].rstrip(".") and record[2]
    ]
    if not usable:
        return None
    priority, _weight, port, target = min(usable, key=lambda item: (item[0], -item[1]))
    return target.rstrip("."), port


def _query_udp(name: str, server: str, timeout: float) -> list[tuple[int, int, int, str]]:
    transaction_id = secrets.randbelow(0x10000)
    question = _encode_name(name) + struct.pack(">HH", SRV_RECORD_TYPE, _CLASS_IN)
    request = struct.pack(">HHHHHH", transaction_id, 0x0100, 1, 0, 0, 0) + question

    family = socket.AF_INET6 if ":" in server else socket.AF_INET
    with socket.socket(family, socket.SOCK_DGRAM) as sock:
        sock.settimeout(timeout)
        sock.sendto(request, (server, 53))
        payload, _address = sock.recvfrom(_MAX_UDP_RESPONSE)
    return parse_srv_response(payload, transaction_id)


def _system_nameservers() -> list[str]:
    servers: list[str] = []
    try:
        with open("/etc/resolv.conf", encoding="utf-8", errors="replace") as handle:
            for line in handle:
                line = line.partition("#")[0].strip()
                if line.startswith("nameserver"):
                    parts = line.split()
                    if len(parts) >= 2:
                        servers.append(parts[1])
    except OSError:
        pass
    return servers


def _query_windows(name: str) -> list[tuple[int, int, int, str]]:
    """Query through the Windows resolver so VPN and split-horizon DNS apply."""

    import ctypes

    class DnsSrvDataW(ctypes.Structure):
        _fields_ = [
            ("pNameTarget", ctypes.c_wchar_p),
            ("wPriority", ctypes.c_ushort),
            ("wWeight", ctypes.c_ushort),
            ("wPort", ctypes.c_ushort),
            ("Pad", ctypes.c_ushort),
        ]

    class DnsRecordW(ctypes.Structure):
        pass

    DnsRecordW._fields_ = [
        ("pNext", ctypes.POINTER(DnsRecordW)),
        ("pName", ctypes.c_wchar_p),
        ("wType", ctypes.c_ushort),
        ("wDataLength", ctypes.c_ushort),
        ("Flags", ctypes.c_uint32),
        ("dwTtl", ctypes.c_uint32),
        ("dwReserved", ctypes.c_uint32),
        ("Data", DnsSrvDataW),
    ]

    dnsapi = ctypes.WinDLL("dnsapi.dll")
    query = dnsapi.DnsQuery_W
    query.argtypes = [
        ctypes.c_wchar_p,
        ctypes.c_ushort,
        ctypes.c_uint32,
        ctypes.c_void_p,
        ctypes.POINTER(ctypes.POINTER(DnsRecordW)),
        ctypes.c_void_p,
    ]
    query.restype = ctypes.c_int32
    free_list = dnsapi.DnsRecordListFree
    free_list.argtypes = [ctypes.c_void_p, ctypes.c_int]
    free_list.restype = None

    head = ctypes.POINTER(DnsRecordW)()
    if query(name, SRV_RECORD_TYPE, 0, None, ctypes.byref(head), None) != 0 or not head:
        return []

    records: list[tuple[int, int, int, str]] = []
    try:
        node = head
        while node:
            entry = node.contents
            if entry.wType == SRV_RECORD_TYPE and entry.Data.pNameTarget:
                records.append(
                    (
                        entry.Data.wPriority,
                        entry.Data.wWeight,
                        entry.Data.wPort,
                        entry.Data.pNameTarget,
                    )
                )
            node = entry.pNext
    finally:
        free_list(head, 1)  # DnsFreeRecordList
    return records


def resolve_minecraft_srv(host: str, *, timeout: float = 5.0) -> tuple[str, int] | None:
    """Resolve ``_minecraft._tcp.<host>`` to ``(host, port)``, or ``None``.

    ``None`` covers every "just connect directly" case: an IP literal, no SRV
    record, an unreachable resolver, or a malformed answer. This performs
    blocking DNS I/O, so call it from a thread inside async code.
    """

    if not host or _is_ip_literal(host):
        return None

    name = _SERVICE_PREFIX + host.rstrip(".")
    try:
        if sys.platform == "win32":
            records = _query_windows(name)
        else:
            records = []
            for server in _system_nameservers():
                try:
                    records = _query_udp(name, server, timeout)
                except (OSError, ValueError):
                    continue
                if records:
                    break
    except (OSError, ValueError, AttributeError):
        return None
    return select_srv_target(records)
