#!/usr/bin/env python3
"""Read-only helpers for the MX25L6433 myQ flash investigation.

The tool never writes to the input image.  It parses Marvell MRVL containers,
summarizes byte regions, compares firmware slots, and translates between flash
offsets and segment runtime addresses.
"""

from __future__ import annotations

import argparse
import hashlib
import math
import struct
from collections import Counter
from dataclasses import dataclass
from pathlib import Path


MAGIC = b"MRVL"


@dataclass(frozen=True)
class Segment:
    kind: int
    file_offset: int
    size: int
    address: int
    checksum: int


@dataclass(frozen=True)
class Image:
    flash_offset: int
    magic2: int
    timestamp: int
    entry: int
    segments: tuple[Segment, ...]

    @property
    def extent(self) -> int:
        return max((s.file_offset + s.size for s in self.segments), default=0)


@dataclass(frozen=True)
class PartitionEntry:
    kind: int
    device: int
    name: str
    start: int
    size: int
    generation: int


@dataclass(frozen=True)
class PartitionTable:
    flash_offset: int
    version: int
    generation: int
    crc: int
    entries: tuple[PartitionEntry, ...]


def u32(data: bytes, offset: int) -> int:
    return struct.unpack_from("<I", data, offset)[0]


def parse_image(data: bytes, flash_offset: int) -> Image:
    if data[flash_offset : flash_offset + 4] != MAGIC:
        raise ValueError(f"no MRVL magic at {flash_offset:#x}")
    magic2, timestamp, count, entry = struct.unpack_from(
        "<IIIII", data, flash_offset
    )[1:]
    if count > 64:
        raise ValueError(f"implausible segment count {count} at {flash_offset:#x}")
    segments = []
    cursor = flash_offset + 0x14
    for _ in range(count):
        segments.append(Segment(*struct.unpack_from("<IIIII", data, cursor)))
        cursor += 0x14
    return Image(flash_offset, magic2, timestamp, entry, tuple(segments))


def find_images(data: bytes) -> list[Image]:
    images: list[Image] = []
    start = 0
    while True:
        found = data.find(MAGIC, start)
        if found < 0:
            return images
        try:
            image = parse_image(data, found)
            if image.segments and all(
                s.kind == 2
                and s.file_offset < len(data)
                and s.size <= len(data)
                and found + s.file_offset + s.size <= len(data)
                for s in image.segments
            ):
                images.append(image)
        except (ValueError, struct.error):
            pass
        start = found + 1


def find_partition_tables(data: bytes) -> list[PartitionTable]:
    tables: list[PartitionTable] = []
    cursor = 0
    while True:
        found = data.find(b"WMPT", cursor)
        if found < 0:
            return tables
        cursor = found + 1
        try:
            version, count, generation, crc = struct.unpack_from("<HHII", data, found + 4)
            if version != 1 or not (1 <= count <= 32):
                continue
            entries = []
            entry_off = found + 16
            for _ in range(count):
                kind, device = struct.unpack_from("<BB", data, entry_off)
                name_raw = data[entry_off + 2 : entry_off + 10]
                name = name_raw.split(b"\0", 1)[0].decode("ascii", "replace")
                start, size, gen = struct.unpack_from("<III", data, entry_off + 12)
                entries.append(PartitionEntry(kind, device, name, start, size, gen))
                entry_off += 24
            if all(e.size and e.start + e.size <= len(data) for e in entries):
                tables.append(PartitionTable(found, version, generation, crc, tuple(entries)))
        except struct.error:
            pass


def entropy(block: bytes) -> float:
    if not block:
        return 0.0
    n = len(block)
    return -sum((c / n) * math.log2(c / n) for c in Counter(block).values())


def sha256(block: bytes) -> str:
    return hashlib.sha256(block).hexdigest()


def flash_to_runtime(image: Image, flash_offset: int) -> int | None:
    rel = flash_offset - image.flash_offset
    for segment in image.segments:
        if segment.file_offset <= rel < segment.file_offset + segment.size:
            return segment.address + rel - segment.file_offset
    return None


def runtime_to_flash(image: Image, address: int) -> int | None:
    for segment in image.segments:
        if segment.address <= address < segment.address + segment.size:
            return image.flash_offset + segment.file_offset + address - segment.address
    return None


def extract_segments(data: bytes, image: Image, destination: Path) -> None:
    """Copy container payloads to new files; never replace an existing file."""
    destination.mkdir(parents=True, exist_ok=True)
    for idx, segment in enumerate(image.segments):
        start = image.flash_offset + segment.file_offset
        block = data[start : start + segment.size]
        name = (
            f"slot_{image.flash_offset:06x}_segment{idx}_"
            f"load_{segment.address:08x}_size_{segment.size:x}.bin"
        )
        output = destination / name
        with output.open("xb") as stream:
            stream.write(block)
        print(f"wrote {output} sha256={sha256(block)}")


def print_image(data: bytes, image: Image) -> None:
    print(
        f"MRVL @{image.flash_offset:#08x}: magic2={image.magic2:#010x} "
        f"timestamp={image.timestamp:#010x} segments={len(image.segments)} "
        f"entry={image.entry:#010x} extent={image.extent:#x}"
    )
    print("  # kind rel_off   flash_off  size      runtime     entropy sha256")
    for idx, segment in enumerate(image.segments):
        start = image.flash_offset + segment.file_offset
        block = data[start : start + segment.size]
        print(
            f"  {idx} {segment.kind:4d} {segment.file_offset:#08x} "
            f"{start:#08x} {segment.size:#08x} {segment.address:#010x} "
            f"{entropy(block):7.4f} {sha256(block)} checksum={segment.checksum:#010x}"
        )


def compare(data: bytes, left: Image, right: Image) -> None:
    print(f"compare slots {left.flash_offset:#x} and {right.flash_offset:#x}")
    for idx in range(max(len(left.segments), len(right.segments))):
        if idx >= len(left.segments) or idx >= len(right.segments):
            print(f"  segment {idx}: exists in only one slot")
            continue
        a, b = left.segments[idx], right.segments[idx]
        ab = data[
            left.flash_offset + a.file_offset : left.flash_offset + a.file_offset + a.size
        ]
        bb = data[
            right.flash_offset + b.file_offset : right.flash_offset + b.file_offset + b.size
        ]
        common = min(len(ab), len(bb))
        diff = sum(x != y for x, y in zip(ab[:common], bb[:common]))
        prefix = next((i for i, (x, y) in enumerate(zip(ab, bb)) if x != y), common)
        print(
            f"  segment {idx}: size {len(ab):#x}/{len(bb):#x}; "
            f"common-byte differences {diff:#x}/{common:#x}; "
            f"identical prefix {prefix:#x}; sha256_equal={sha256(ab) == sha256(bb)}"
        )


def print_partition_table(table: PartitionTable) -> None:
    print(
        f"WMPT @{table.flash_offset:#08x}: version={table.version} "
        f"generation={table.generation} crc={table.crc:#010x} entries={len(table.entries)}"
    )
    print("  # type dev name     start      size       end        generation")
    for idx, entry in enumerate(table.entries):
        print(
            f"  {idx:2d} {entry.kind:4d} {entry.device:3d} {entry.name:<8} "
            f"{entry.start:#010x} {entry.size:#010x} {entry.start + entry.size:#010x} "
            f"{entry.generation}"
        )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("image", type=Path)
    parser.add_argument(
        "--summary",
        action="store_true",
        help="print MRVL image and WMPT summaries alongside any selected action",
    )
    parser.add_argument("--compare", action="store_true")
    parser.add_argument("--flash-to-runtime", type=lambda x: int(x, 0))
    parser.add_argument("--runtime-to-flash", type=lambda x: int(x, 0))
    parser.add_argument("--slot", type=lambda x: int(x, 0), default=0xC0000)
    parser.add_argument("--bytes-flash", metavar="START:LENGTH")
    parser.add_argument("--find-ascii")
    parser.add_argument("--partition-stats", action="store_true")
    parser.add_argument(
        "--extract-dir",
        type=Path,
        help="copy the selected slot's payload segments into a new directory (no overwrite)",
    )
    args = parser.parse_args()
    data = args.image.read_bytes()
    images = find_images(data)
    tables = find_partition_tables(data)
    has_action = any(
        (
            args.compare,
            args.flash_to_runtime is not None,
            args.runtime_to_flash is not None,
            args.bytes_flash is not None,
            args.find_ascii is not None,
            args.partition_stats,
            args.extract_dir is not None,
        )
    )
    if args.summary or not has_action:
        for image in images:
            print_image(data, image)
        for table in tables:
            print_partition_table(table)
    if args.partition_stats and tables:
        table = max(tables, key=lambda item: item.generation)
        print("partition byte statistics (highest-generation WMPT)")
        for entry in table.entries:
            block = data[entry.start : entry.start + entry.size]
            populated = [idx for idx, byte in enumerate(block) if byte != 0xFF]
            first = entry.start + populated[0] if populated else None
            last = entry.start + populated[-1] if populated else None
            first_text = f"{first:#x}" if first is not None else "none"
            last_text = f"{last:#x}" if last is not None else "none"
            print(
                f"  {entry.name:<8} entropy={entropy(block):.4f} "
                f"ff={block.count(0xFF) / len(block):.3%} "
                f"zero={block.count(0) / len(block):.3%} "
                f"non_ff={len(populated):#x} first_non_ff={first_text} "
                f"last_non_ff={last_text}"
            )
    if args.compare and len(images) >= 2:
        compare(data, images[0], images[1])
    selected = next((x for x in images if x.flash_offset == args.slot), None)
    if selected is None and (
        args.flash_to_runtime is not None
        or args.runtime_to_flash is not None
        or args.extract_dir is not None
    ):
        raise SystemExit(f"slot not found: {args.slot:#x}")
    if args.extract_dir is not None:
        extract_segments(data, selected, args.extract_dir)
    if args.flash_to_runtime is not None:
        translated = flash_to_runtime(selected, args.flash_to_runtime)
        translated_text = f"{translated:#x}" if translated is not None else "unmapped"
        print(
            f"flash {args.flash_to_runtime:#x} -> runtime "
            f"{translated_text}"
        )
    if args.runtime_to_flash is not None:
        translated = runtime_to_flash(selected, args.runtime_to_flash)
        translated_text = f"{translated:#x}" if translated is not None else "unmapped"
        print(
            f"runtime {args.runtime_to_flash:#x} -> flash "
            f"{translated_text}"
        )
    if args.bytes_flash:
        start_s, length_s = args.bytes_flash.split(":", 1)
        start, length = int(start_s, 0), int(length_s, 0)
        block = data[start : start + length]
        for cursor in range(0, len(block), 16):
            row = block[cursor : cursor + 16]
            chars = "".join(chr(x) if 32 <= x < 127 else "." for x in row)
            print(f"{start + cursor:#010x}  {row.hex(' '):<47}  {chars}")
    if args.find_ascii is not None:
        needle = args.find_ascii.encode("utf-8")
        cursor = 0
        while True:
            found = data.find(needle, cursor)
            if found < 0:
                break
            print(f"{args.find_ascii!r} at flash {found:#010x}")
            cursor = found + 1


if __name__ == "__main__":
    main()
