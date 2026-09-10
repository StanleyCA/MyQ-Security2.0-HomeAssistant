#!/usr/bin/env python3
"""Thumb-2 disassembly and literal-xref helper for the myQ firmware dump."""

from __future__ import annotations

import argparse
import struct
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).with_name("_vendor")))
from capstone import (  # type: ignore  # installed under analysis/_vendor
    CS_ARCH_ARM,
    CS_MODE_LITTLE_ENDIAN,
    CS_MODE_MCLASS,
    CS_MODE_THUMB,
    Cs,
)
from capstone.arm import ARM_OP_IMM, ARM_OP_MEM, ARM_REG_PC  # type: ignore

from fwtool import Image, find_images, flash_to_runtime, runtime_to_flash


def make_cs() -> Cs:
    md = Cs(CS_ARCH_ARM, CS_MODE_THUMB | CS_MODE_MCLASS | CS_MODE_LITTLE_ENDIAN)
    md.detail = True
    return md


def selected_image(data: bytes, slot: int) -> Image:
    for image in find_images(data):
        if image.flash_offset == slot:
            return image
    raise SystemExit(f"MRVL image not found at {slot:#x}")


def read_u32_runtime(data: bytes, image: Image, address: int) -> int | None:
    off = runtime_to_flash(image, address)
    if off is None or off + 4 > len(data):
        return None
    return struct.unpack_from("<I", data, off)[0]


def disasm_range(data: bytes, image: Image, start: int, end: int) -> None:
    off = runtime_to_flash(image, start)
    if off is None:
        raise SystemExit(f"runtime address not mapped: {start:#x}")
    md = make_cs()
    for insn in md.disasm(data[off : off + end - start], start):
        suffix = ""
        if insn.mnemonic.startswith("ldr") and len(insn.operands) >= 2:
            mem = insn.operands[1]
            if mem.type == ARM_OP_MEM and mem.mem.base == ARM_REG_PC:
                literal = ((insn.address + 4) & ~3) + mem.mem.disp
                value = read_u32_runtime(data, image, literal)
                suffix = f" ; [{literal:#010x}]={value:#010x}" if value is not None else ""
        elif insn.mnemonic in ("bl", "blx", "b", "b.w") and insn.operands:
            if insn.operands[0].type == ARM_OP_IMM:
                suffix = f" ; target={insn.operands[0].imm:#010x}"
        raw = insn.bytes.hex(" ")
        print(f"{insn.address:#010x}  {raw:<12} {insn.mnemonic:<9} {insn.op_str}{suffix}")


def find_word(data: bytes, value: int) -> list[int]:
    needle = struct.pack("<I", value)
    out = []
    cursor = 0
    while True:
        found = data.find(needle, cursor)
        if found < 0:
            return out
        out.append(found)
        cursor = found + 1


def literal_xrefs(data: bytes, image: Image, value: int) -> list[tuple[int, int]]:
    """Return (instruction address, literal address) for PC-relative LDRs of value."""
    literal_addresses = set()
    for off in find_word(data, value):
        address = flash_to_runtime(image, off)
        if address is not None:
            literal_addresses.add(address)
    md = make_cs()
    xrefs: set[tuple[int, int]] = set()
    for segment in image.segments:
        # Only the XIP code/rodata segment is a useful instruction source here.
        if not (0x1F000000 <= segment.address < 0x20000000):
            continue
        block_off = image.flash_offset + segment.file_offset
        block = data[block_off : block_off + segment.size]
        for byte_off in range(0, len(block) - 2, 2):
            insns = list(md.disasm(block[byte_off : byte_off + 4], segment.address + byte_off, 1))
            if not insns:
                continue
            insn = insns[0]
            if not insn.mnemonic.startswith("ldr") or len(insn.operands) < 2:
                continue
            mem = insn.operands[1]
            if mem.type != ARM_OP_MEM or mem.mem.base != ARM_REG_PC:
                continue
            literal = ((insn.address + 4) & ~3) + mem.mem.disp
            if literal in literal_addresses:
                xrefs.add((insn.address, literal))
    return sorted(xrefs)


def direct_branches(data: bytes, image: Image, target: int, calls_only: bool) -> list[tuple[int, str]]:
    """Find aligned immediate branch instructions whose resolved target matches."""
    md = make_cs()
    refs: set[tuple[int, str]] = set()
    target &= ~1
    for segment in image.segments:
        if not (segment.address < 0x00200000 or 0x1F000000 <= segment.address < 0x20000000):
            continue
        block_off = image.flash_offset + segment.file_offset
        block = data[block_off : block_off + segment.size]
        for byte_off in range(0, len(block) - 4, 2):
            insns = list(md.disasm(block[byte_off : byte_off + 4], segment.address + byte_off, 1))
            if not insns:
                continue
            insn = insns[0]
            allowed = ("bl", "blx") if calls_only else ("bl", "blx", "b", "b.w")
            if insn.mnemonic not in allowed or not insn.operands:
                continue
            if insn.operands[0].type == ARM_OP_IMM and (insn.operands[0].imm & ~1) == target:
                refs.add((insn.address, insn.mnemonic))
    return sorted(refs)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("image", type=Path)
    parser.add_argument("--slot", type=lambda x: int(x, 0), default=0xC0000)
    parser.add_argument("--word", type=lambda x: int(x, 0))
    parser.add_argument("--xref-value", type=lambda x: int(x, 0))
    parser.add_argument("--xref-string-flash", type=lambda x: int(x, 0))
    parser.add_argument("--calls-to", type=lambda x: int(x, 0))
    parser.add_argument("--branches-to", type=lambda x: int(x, 0))
    parser.add_argument("--disasm", metavar="START:END")
    parser.add_argument("--bytes-runtime", metavar="START:LENGTH")
    parser.add_argument("--cstr-runtime", type=lambda x: int(x, 0))
    parser.add_argument("--around", type=lambda x: int(x, 0))
    parser.add_argument("--radius", type=lambda x: int(x, 0), default=0x80)
    args = parser.parse_args()

    data = args.image.read_bytes()
    image = selected_image(data, args.slot)
    if args.word is not None:
        for off in find_word(data, args.word):
            print(f"word {args.word:#010x} at flash {off:#08x}")
    value = args.xref_value
    if args.xref_string_flash is not None:
        value = flash_to_runtime(image, args.xref_string_flash)
        if value is None:
            raise SystemExit(f"string flash offset is outside slot mappings: {args.xref_string_flash:#x}")
        print(f"string flash {args.xref_string_flash:#x} -> runtime {value:#010x}")
    if value is not None:
        words = find_word(data, value)
        print(f"raw little-endian words for {value:#010x}: {', '.join(hex(x) for x in words) or 'none'}")
        for insn, literal in literal_xrefs(data, image, value):
            print(f"literal xref instruction={insn:#010x} literal={literal:#010x}")
    if args.calls_to is not None:
        words = sorted(set(find_word(data, args.calls_to) + find_word(data, args.calls_to | 1)))
        print(
            f"raw function-pointer words for {args.calls_to:#010x}: "
            f"{', '.join(hex(x) for x in words) or 'none'}"
        )
        for address, mnemonic in direct_branches(data, image, args.calls_to, True):
            print(f"direct call instruction={address:#010x} mnemonic={mnemonic}")
    if args.branches_to is not None:
        for address, mnemonic in direct_branches(data, image, args.branches_to, False):
            print(f"direct branch instruction={address:#010x} mnemonic={mnemonic}")
    if args.disasm:
        start_s, end_s = args.disasm.split(":", 1)
        disasm_range(data, image, int(start_s, 0), int(end_s, 0))
    if args.bytes_runtime:
        start_s, length_s = args.bytes_runtime.split(":", 1)
        start, length = int(start_s, 0), int(length_s, 0)
        off = runtime_to_flash(image, start)
        if off is None:
            raise SystemExit(f"runtime address not mapped: {start:#x}")
        block = data[off : off + length]
        for cursor in range(0, len(block), 16):
            row = block[cursor : cursor + 16]
            chars = "".join(chr(x) if 32 <= x < 127 else "." for x in row)
            print(f"{start + cursor:#010x}  {row.hex(' '):<47}  {chars}")
    if args.cstr_runtime is not None:
        off = runtime_to_flash(image, args.cstr_runtime)
        if off is None:
            raise SystemExit(f"runtime address not mapped: {args.cstr_runtime:#x}")
        end = data.find(b"\0", off, off + 0x1000)
        if end < 0:
            end = min(off + 0x1000, len(data))
        print(data[off:end].decode("utf-8", "backslashreplace"))
    if args.around is not None:
        start = (args.around - args.radius) & ~1
        disasm_range(data, image, start, args.around + args.radius)


if __name__ == "__main__":
    main()
