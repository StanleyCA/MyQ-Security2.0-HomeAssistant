#!/usr/bin/env python3
"""Decrypt this device's TLS 1.2 PSK/AES-CBC MQTT captures.

The implementation is deliberately narrow: plain PSK key exchange,
TLS_PSK_WITH_AES_128_CBC_SHA (0x008c), and RFC 7366 Encrypt-then-MAC.
It verifies every protected record before emitting plaintext.
"""

from __future__ import annotations

import argparse
import hashlib
import hmac
import socket
import struct
import sys
from dataclasses import dataclass
from pathlib import Path

VENDOR = Path(__file__).resolve().parent / "_vendor"
if VENDOR.is_dir():
    sys.path.insert(0, str(VENDOR))

import dpkt  # type: ignore
from Crypto.Cipher import AES  # type: ignore


TLS_HANDSHAKE = 22
TLS_CHANGE_CIPHER_SPEC = 20
TLS_APPLICATION_DATA = 23


@dataclass
class TLSRecord:
    content_type: int
    version: int
    fragment: bytes


def p_hash(secret: bytes, seed: bytes, length: int) -> bytes:
    result = bytearray()
    a = seed
    while len(result) < length:
        a = hmac.new(secret, a, hashlib.sha256).digest()
        result.extend(hmac.new(secret, a + seed, hashlib.sha256).digest())
    return bytes(result[:length])


def tls12_prf(secret: bytes, label: bytes, seed: bytes, length: int) -> bytes:
    return p_hash(secret, label + seed, length)


def parse_tls_records(stream: bytes) -> list[TLSRecord]:
    records: list[TLSRecord] = []
    offset = 0
    while offset < len(stream):
        if offset + 5 > len(stream):
            raise ValueError(f"truncated TLS header at stream offset {offset:#x}")
        content_type, version, length = struct.unpack("!BHH", stream[offset : offset + 5])
        end = offset + 5 + length
        if end > len(stream):
            raise ValueError(f"truncated TLS record at stream offset {offset:#x}")
        records.append(TLSRecord(content_type, version, stream[offset + 5 : end]))
        offset = end
    return records


def reassemble_tcp(pcap_path: Path, port: int) -> dict[tuple[str, int, str, int], bytes]:
    segments: dict[tuple[str, int, str, int], list[tuple[int, bytes]]] = {}
    with pcap_path.open("rb") as handle:
        magic = handle.read(4)
        handle.seek(0)
        if magic == b"\x0a\x0d\x0d\x0a":
            reader = dpkt.pcapng.Reader(handle)
        else:
            reader = dpkt.pcap.Reader(handle)
        for _timestamp, frame in reader:
            eth = dpkt.ethernet.Ethernet(frame)
            if not isinstance(eth.data, dpkt.ip.IP):
                continue
            ip = eth.data
            if not isinstance(ip.data, dpkt.tcp.TCP):
                continue
            tcp = ip.data
            if not tcp.data or (tcp.sport != port and tcp.dport != port):
                continue
            key = (
                socket.inet_ntoa(ip.src),
                tcp.sport,
                socket.inet_ntoa(ip.dst),
                tcp.dport,
            )
            segments.setdefault(key, []).append((tcp.seq, bytes(tcp.data)))

    streams: dict[tuple[str, int, str, int], bytes] = {}
    for key, parts in segments.items():
        parts.sort(key=lambda item: item[0])
        wanted = parts[0][0]
        result = bytearray()
        for sequence, data in parts:
            if sequence + len(data) <= wanted:  # retransmission
                continue
            if sequence > wanted:
                raise ValueError(f"TCP gap in {key}: {wanted:#x}..{sequence:#x}")
            overlap = max(0, wanted - sequence)
            result.extend(data[overlap:])
            wanted = sequence + len(data)
        streams[key] = bytes(result)
    return streams


def hello_random(records: list[TLSRecord], handshake_type: int) -> bytes:
    for record in records:
        body = record.fragment
        if record.content_type == TLS_HANDSHAKE and len(body) >= 38 and body[0] == handshake_type:
            return body[6:38]
    raise ValueError(f"handshake message {handshake_type} not found")


def psk_identity(records: list[TLSRecord]) -> str:
    for record in records:
        body = record.fragment
        if record.content_type != TLS_HANDSHAKE or len(body) < 6 or body[0] != 16:
            continue
        msg_len = int.from_bytes(body[1:4], "big")
        msg = body[4 : 4 + msg_len]
        identity_len = int.from_bytes(msg[:2], "big")
        return msg[2 : 2 + identity_len].decode("ascii", errors="replace")
    return "<not found>"


def decrypt_direction(records: list[TLSRecord], key: bytes, mac_key: bytes) -> bytes:
    active = False
    sequence = 0
    application = bytearray()
    for record in records:
        if record.content_type == TLS_CHANGE_CIPHER_SPEC and record.fragment == b"\x01":
            active = True
            sequence = 0
            continue
        if not active:
            continue

        # RFC 7366 TLS 1.1+ form: IV || CBC ciphertext || HMAC-SHA1.
        if len(record.fragment) < 16 + 16 + 20:
            raise ValueError(f"protected record {sequence} is too short")
        encrypted, received_mac = record.fragment[:-20], record.fragment[-20:]
        mac_input = (
            sequence.to_bytes(8, "big")
            + bytes([record.content_type])
            + record.version.to_bytes(2, "big")
            + len(encrypted).to_bytes(2, "big")
            + encrypted
        )
        calculated_mac = hmac.new(mac_key, mac_input, hashlib.sha1).digest()
        if not hmac.compare_digest(calculated_mac, received_mac):
            raise ValueError(f"bad record MAC at protected sequence {sequence}")

        iv, ciphertext = encrypted[:16], encrypted[16:]
        padded = AES.new(key, AES.MODE_CBC, iv).decrypt(ciphertext)
        padding_byte = padded[-1]
        padding = bytes([padding_byte]) * (padding_byte + 1)
        if padding_byte >= 16 or not padded.endswith(padding):
            raise ValueError(f"bad CBC padding at protected sequence {sequence}")
        plaintext = padded[: -padding_byte - 1]
        if record.content_type == TLS_APPLICATION_DATA:
            application.extend(plaintext)
        sequence += 1
    return bytes(application)


def mqtt_remaining_length(data: bytes, offset: int) -> tuple[int, int]:
    value = 0
    multiplier = 1
    for _ in range(4):
        byte = data[offset]
        offset += 1
        value += (byte & 0x7F) * multiplier
        if not byte & 0x80:
            return value, offset
        multiplier *= 128
    raise ValueError("invalid MQTT remaining length")


def mqtt_utf8(body: bytes, offset: int) -> tuple[str, int]:
    length = int.from_bytes(body[offset : offset + 2], "big")
    offset += 2
    return body[offset : offset + length].decode("utf-8", errors="replace"), offset + length


def describe_mqtt(stream: bytes) -> list[str]:
    names = {
        1: "CONNECT", 2: "CONNACK", 3: "PUBLISH", 8: "SUBSCRIBE",
        9: "SUBACK", 12: "PINGREQ", 13: "PINGRESP", 14: "DISCONNECT",
    }
    lines: list[str] = []
    offset = 0
    while offset < len(stream):
        start = offset
        fixed = stream[offset]
        offset += 1
        remaining, body_start = mqtt_remaining_length(stream, offset)
        end = body_start + remaining
        if end > len(stream):
            raise ValueError(f"truncated MQTT packet at plaintext offset {start:#x}")
        body = stream[body_start:end]
        packet_type = fixed >> 4
        name = names.get(packet_type, f"TYPE_{packet_type}")

        if packet_type == 3:
            topic, pos = mqtt_utf8(body, 0)
            qos = (fixed >> 1) & 3
            packet_id = ""
            if qos:
                packet_id = f" packet_id={int.from_bytes(body[pos:pos+2], 'big')}"
                pos += 2
            payload = body[pos:]
            lines.append(
                f"{name} topic={topic!r} qos={qos}{packet_id} "
                f"payload_len={len(payload)} payload={payload.hex()}"
            )
        elif packet_type == 8:
            packet_id = int.from_bytes(body[:2], "big")
            topic, pos = mqtt_utf8(body, 2)
            qos = body[pos] if pos < len(body) else None
            lines.append(f"{name} packet_id={packet_id} topic={topic!r} requested_qos={qos}")
        elif packet_type == 1:
            proto, pos = mqtt_utf8(body, 0)
            level, flags = body[pos], body[pos + 1]
            keepalive = int.from_bytes(body[pos + 2 : pos + 4], "big")
            pos += 4
            client_id, _ = mqtt_utf8(body, pos)
            lines.append(
                f"{name} protocol={proto}/{level} flags={flags:#04x} "
                f"keepalive={keepalive} client_id={client_id!r}"
            )
        else:
            lines.append(f"{name} body={body.hex()}")
        offset = end
    return lines


def decrypt_flow(
    client_records: list[TLSRecord], server_records: list[TLSRecord], psk: bytes
) -> tuple[bytes, bytes, str]:
    client_random = hello_random(client_records, 1)
    server_random = hello_random(server_records, 2)
    psk_len = len(psk)
    premaster = struct.pack("!H", psk_len) + bytes(psk_len) + struct.pack("!H", psk_len) + psk
    master = tls12_prf(premaster, b"master secret", client_random + server_random, 48)
    key_block = tls12_prf(master, b"key expansion", server_random + client_random, 72)
    client_mac, server_mac = key_block[:20], key_block[20:40]
    client_key, server_key = key_block[40:56], key_block[56:72]
    return (
        decrypt_direction(client_records, client_key, client_mac),
        decrypt_direction(server_records, server_key, server_mac),
        psk_identity(client_records),
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("pcap", type=Path)
    parser.add_argument("--psk", required=True, help="PSK as hexadecimal bytes")
    parser.add_argument("--port", type=int, default=8883)
    args = parser.parse_args()
    try:
        psk = bytes.fromhex(args.psk)
    except ValueError as exc:
        parser.error(f"invalid --psk: {exc}")

    streams = reassemble_tcp(args.pcap, args.port)
    client_flows = sorted(key for key in streams if key[1] != args.port and key[3] == args.port)
    if not client_flows:
        raise SystemExit("no client-to-server flows found")

    for index, client in enumerate(client_flows, 1):
        server = (client[2], client[3], client[0], client[1])
        if server not in streams:
            raise ValueError(f"reverse stream not found for {client}")
        client_records = parse_tls_records(streams[client])
        server_records = parse_tls_records(streams[server])
        try:
            hello_random(client_records, 1)
            hello_random(server_records, 2)
        except ValueError:
            print(
                f"Flow {index}: {client[0]}:{client[1]} -> "
                f"{client[2]}:{client[3]} (skipped: capture starts after TLS handshake)"
            )
            if index != len(client_flows):
                print()
            continue
        c_plain, s_plain, identity = decrypt_flow(client_records, server_records, psk)
        print(f"Flow {index}: {client[0]}:{client[1]} -> {client[2]}:{client[3]}")
        print(f"  TLS: 1.2, 0x008c TLS_PSK_WITH_AES_128_CBC_SHA, identity={identity}")
        print("  All protected record MACs and CBC padding: valid")
        for direction, plaintext in (("C -> S", c_plain), ("S -> C", s_plain)):
            print(f"  {direction}:")
            for description in describe_mqtt(plaintext):
                print(f"    {description}")
        if index != len(client_flows):
            print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
