#!/usr/bin/env python3
"""Parse Marvell PSMv2 records and reproduce the myQ TEA key unwrap.

This is a read-only tool.  It documents the 12-byte PSM record header observed
in the dump and implements the exact 32-round, two-block decryption routine at
runtime address 0x1F06D17C.
"""

from __future__ import annotations

import argparse
import struct
from dataclasses import dataclass
from pathlib import Path

from fwtool import find_partition_tables


MYQ_TEA_KEY = bytes.fromhex("ff10b54a43b61ab933fdcba30b071c57")
DELTA = 0x9E3779B9
MASK = 0xFFFFFFFF


@dataclass(frozen=True)
class Record:
    offset: int
    state: int
    crc: int
    data_len: int
    object_id: int
    name_len: int
    name: bytes
    value: bytes

    @property
    def end(self) -> int:
        return self.offset + 12 + self.name_len + self.data_len


def parse_records(data: bytes, start: int, size: int) -> list[Record]:
    records: list[Record] = []
    cursor, end = start, start + size
    while cursor + 12 <= end:
        if data[cursor : cursor + 2] != b"\x55\xaa":
            found = data.find(b"\x55\xaa", cursor + 1, end)
            if found < 0:
                break
            cursor = found
        state = data[cursor + 2]
        crc = struct.unpack_from("<I", data, cursor + 3)[0]
        data_len, object_id = struct.unpack_from("<HH", data, cursor + 7)
        name_len = data[cursor + 11]
        rec_end = cursor + 12 + name_len + data_len
        if name_len == 0 or rec_end > end:
            cursor += 1
            continue
        name = data[cursor + 12 : cursor + 12 + name_len]
        if not all(0x20 <= byte < 0x7F for byte in name):
            cursor += 1
            continue
        value = data[cursor + 12 + name_len : rec_end]
        records.append(
            Record(cursor, state, crc, data_len, object_id, name_len, name, value)
        )
        cursor = rec_end
    return records


def _mix(value: int) -> int:
    return ((((value << 4) & MASK) ^ (value >> 5)) + value) & MASK


def decrypt_block(block: bytes, key: bytes = MYQ_TEA_KEY) -> bytes:
    """Reproduce 0x1F06D114, including its cyclic key-index schedule."""
    if len(block) != 8 or len(key) != 16:
        raise ValueError("TEA block/key sizes must be 8/16 bytes")
    v0, v1 = struct.unpack("<II", block)
    words = struct.unpack("<IIII", key)
    total = (DELTA * 32) & MASK
    key_index = 0
    for _ in range(32):
        v1 = (v1 - (_mix(v0) ^ ((total + words[key_index]) & MASK))) & MASK
        key_index = (key_index - 1) & 3
        total = (total - DELTA) & MASK
        v0 = (v0 - (_mix(v1) ^ ((total + words[key_index]) & MASK))) & MASK
    return struct.pack("<II", v0, v1)


def encrypt_block(block: bytes, key: bytes = MYQ_TEA_KEY) -> bytes:
    """Inverse of decrypt_block, matching 0x1F06D070."""
    if len(block) != 8 or len(key) != 16:
        raise ValueError("TEA block/key sizes must be 8/16 bytes")
    v0, v1 = struct.unpack("<II", block)
    words = struct.unpack("<IIII", key)
    total = 0
    key_index = 0
    for _ in range(32):
        v0 = (v0 + (_mix(v1) ^ ((total + words[key_index]) & MASK))) & MASK
        key_index = (key_index + 1) & 3
        total = (total + DELTA) & MASK
        v1 = (v1 + (_mix(v0) ^ ((total + words[key_index]) & MASK))) & MASK
    return struct.pack("<II", v0, v1)


def unwrap_myq_aes(value: bytes) -> bytes:
    if len(value) != 16:
        raise ValueError(f"myq_aes value must be 16 bytes, got {len(value)}")
    plain = decrypt_block(value[:8]) + decrypt_block(value[8:])
    if encrypt_block(plain[:8]) + encrypt_block(plain[8:]) != value:
        raise AssertionError("TEA round-trip failed")
    return plain


def printable(value: bytes) -> str:
    return "".join(chr(x) if 0x20 <= x < 0x7F else "." for x in value)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("image", type=Path)
    parser.add_argument("--partition", default="psm_seed")
    parser.add_argument("--name")
    parser.add_argument("--include-obsolete", action="store_true")
    parser.add_argument("--show-values", action="store_true")
    parser.add_argument("--unwrap-myq-aes", action="store_true")
    args = parser.parse_args()

    data = args.image.read_bytes()
    tables = find_partition_tables(data)
    if not tables:
        raise SystemExit("no WMPT partition table found")
    table = max(tables, key=lambda item: item.generation)
    partition = next((entry for entry in table.entries if entry.name == args.partition), None)
    if partition is None:
        raise SystemExit(f"partition not found: {args.partition}")
    records = parse_records(data, partition.start, partition.size)
    print(
        f"partition={partition.name} start={partition.start:#x} size={partition.size:#x} "
        f"records={len(records)}"
    )
    for record in records:
        name = record.name.decode("ascii")
        if args.name is not None and name != args.name:
            continue
        if not args.include_obsolete and record.state != 0xFF:
            continue
        line = (
            f"offset={record.offset:#010x} end={record.end:#010x} state={record.state:#04x} "
            f"crc={record.crc:#010x} id={record.object_id} name={name!r} "
            f"name_len={record.name_len} data_len={record.data_len}"
        )
        if args.show_values:
            line += f" value_hex={record.value.hex()} value_ascii={printable(record.value)!r}"
        print(line)
        if args.unwrap_myq_aes and name == "myq_aes" and record.state == 0xFF:
            plain = unwrap_myq_aes(record.value)
            print(f"  unwrapped_hex={plain.hex()} unwrapped_ascii={printable(plain)!r}")


if __name__ == "__main__":
    main()
