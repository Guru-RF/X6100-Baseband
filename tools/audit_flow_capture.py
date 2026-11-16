#!/usr/bin/env python3
"""Audit a saved X6100 UART byte stream without opening a radio or serial port.

Protocol references (this is an independent implementation):
  fventuri/x6100-bbfw, 57c5ce98a2202fabc86c5517f3ec8553eb4b98b7,
  firmwares/phase4-linux/ttys1_flow_reader.py (framing, CRC and BF16).
  OpenX6100/X6100Control, 7a5bb8e5367f59003ef03af221361e186da10bf0,
  include/aether_radio/x6100_control/low/flow.h and src/low/crc32.c
  (independently checked common field offsets and CRC).

Select oem for stock firmware and X6100-Baseband alpha.2. It ignores the
reserved flow-info bytes. extended-r8 explicitly opts into the r8-style
flow-info layout; selecting it does not detect or identify firmware.
Every decoded frame must pass its recorded CRC. No previous-hkey CRC repair
is attempted, even though X6100Control can accept some such packets.
"""

import argparse
import hashlib
import json
import os
import stat
import struct
import sys


MAGIC = b"\xaa\x55\x55\xaa"
FRAME_BYTES = 0x1028
CRC_OFFSET = 0x1024
PROFILES = ("oem", "extended-r8")


def _crc_table():
    table = []
    for byte in range(256):
        value = byte << 24
        for _ in range(8):
            value = ((value << 1) ^ (0x04C11DB7 if value & 0x80000000 else 0)) & 0xFFFFFFFF
        table.append(value)
    return tuple(table)


_CRC_TABLE = _crc_table()


def stm32_crc32(data):
    """CRC of little-endian uint32 words, init FFFFFFFF, no reflection/xorout."""
    if len(data) % 4:
        raise ValueError("STM32 CRC input must contain whole 32-bit words")
    crc = 0xFFFFFFFF
    for (word,) in struct.iter_unpack("<I", data):
        crc ^= word
        for _ in range(4):
            crc = ((crc << 8) & 0xFFFFFFFF) ^ _CRC_TABLE[crc >> 24]
    return crc


def _decode(frame, profile):
    fmt = frame[0x1018] if profile == "extended-r8" else 0
    if fmt not in (0, 1):
        return None
    flags = struct.unpack_from("<I", frame, 0x1004)[0]
    alc = frame[0x100B]
    # These are radio-reported values, not calibrated RF or envelope samples.
    decoded = {
        "flags_raw": flags,
        "flags": {name: bool(flags & (1 << bit)) for bit, name in enumerate(
            ("resync", "tx", "atu_status", "external_power", "charging"))},
        "telemetry": {
            "indicated_tx_power_w": frame[0x1009] / 10,
            "indicated_vswr": frame[0x100A] / 10,
            "alc_raw": alc,
            "gui_alc_value": max(0, 100 - alc) / 10,
            "external_voltage_v": frame[0x100C] / 10,
            "battery_voltage_v": frame[0x100D] / 10,
            "battery_percent_raw": frame[0x100E],
        },
        "atu_params_raw": struct.unpack_from("<I", frame, 0x1010)[0],
        "hkey_raw": struct.unpack_from("<I", frame, 0x1020)[0],
    }
    # IEEE exponent checks work for both FP32 and BF16 (upper 16 FP32 bits),
    # without converting NaNs into JSON numbers or making RF measurements.
    encoding, word_fmt, exponent = ("fp32", "<I", 0x7F800000) if fmt == 0 else (
        "bf16", "<H", 0x7F80)
    decoded["iq"] = {
        "encoding": encoding,
        "complex_samples": 512 if fmt == 0 else 1024,
        "nonfinite_components": sum(
            (word & exponent) == exponent
            for (word,) in struct.iter_unpack(word_fmt, frame[4:0x1004])),
    }
    if profile == "extended-r8":
        packed = struct.unpack_from("<H", frame, 0x101A)[0]
        decoded["flow_info"] = {
            "lo_frequency_raw": struct.unpack_from("<I", frame, 0x1014)[0],
            "format_raw": fmt,
            "sequence_index_raw": frame[0x1019] & 15,
            "sequence_total_raw": frame[0x1019] >> 4,
            "vary_frequency": bool(packed & 1),
            "fft_decimation_raw": (packed >> 1) & 7,
            "audio_input_level_raw": (packed >> 4) & 127,
        }
    return decoded


def _audit_stream(stream, profile, detail_limit, chunk_bytes=65536):
    """Internal bounded-memory scanner; the public entry point requires a file."""
    report = {
        "profile": profile,
        "frame_bytes": FRAME_BYTES,
        "input_bytes": 0,
        "crc_valid_frames": 0,
        "decoded_frames": 0,
        "crc_failed_candidates": 0,
        "unsupported_format_frames": 0,
        "nonfinite_iq_frames": 0,
        "resync_events": 0,
        "first_valid_offset": None,
        "last_valid_offset": None,
        "incomplete_tail": None,
        "telemetry_ranges": {},
        "flag_counts": {},
        "iq_format_counts": {},
        "iq_complex_samples": 0,
        "iq_nonfinite_components": 0,
        "details_limit": detail_limit,
        "frames": [],
        "events": [],
        "event_count": 0,
        "limitations": [
            "Offsets are file byte offsets; no arrival times or processing times are measured.",
            "Indicated power and ALC are telemetry, not RF-envelope or overshoot measurements.",
            "CRC-valid frames do not establish gap-free IQ or a sample rate.",
            "CRC-failed candidates may be false magic; recorded CRC is never repaired.",
            "The profile is supplied by the operator, not detected from these bytes.",
        ],
    }
    digest = hashlib.sha256()
    buffer = bytearray()
    base = 0
    last_end = 0

    def event(kind, offset, **fields):
        report["event_count"] += 1
        if len(report["events"]) < detail_limit:
            report["events"].append({"kind": kind, "offset": offset, **fields})

    while True:
        chunk = stream.read(chunk_bytes)
        digest.update(chunk)
        report["input_bytes"] += len(chunk)
        buffer.extend(chunk)
        cursor = 0
        while True:
            start = buffer.find(MAGIC, cursor)
            if start < 0:
                cursor = max(cursor, len(buffer) - 3)
                break
            if len(buffer) - start < FRAME_BYTES:
                cursor = start
                break
            frame = buffer[start:start + FRAME_BYTES]
            offset = base + start
            expected = struct.unpack_from("<I", frame, CRC_OFFSET)[0]
            actual = stm32_crc32(frame[:CRC_OFFSET])
            if actual != expected:
                report["crc_failed_candidates"] += 1
                event("crc_failed_candidate", offset, recorded_crc=expected, computed_crc=actual)
                cursor = start + 1  # Another genuine packet may start inside this candidate.
                continue
            if offset > last_end:
                event("skipped_bytes", last_end, length=offset - last_end)
                report["resync_events"] += 1
            last_end = offset + FRAME_BYTES
            if report["first_valid_offset"] is None:
                report["first_valid_offset"] = offset
            report["last_valid_offset"] = offset
            report["crc_valid_frames"] += 1
            decoded = _decode(frame, profile)
            if decoded is None:
                report["unsupported_format_frames"] += 1
                event("unsupported_format", offset, format_raw=frame[0x1018])
            else:
                report["decoded_frames"] += 1
                iq = decoded["iq"]
                if iq["nonfinite_components"]:
                    report["nonfinite_iq_frames"] += 1
                    event("nonfinite_iq", offset, components=iq["nonfinite_components"])
                report["iq_nonfinite_components"] += iq["nonfinite_components"]
                report["iq_complex_samples"] += iq["complex_samples"]
                counts = report["iq_format_counts"]
                counts[iq["encoding"]] = counts.get(iq["encoding"], 0) + 1
                for flag, enabled in decoded["flags"].items():
                    counts = report["flag_counts"]
                    counts[flag] = counts.get(flag, 0) + int(enabled)
                for name, value in decoded["telemetry"].items():
                    bounds = report["telemetry_ranges"].setdefault(
                        name, {"min": value, "max": value})
                    bounds["min"] = min(bounds["min"], value)
                    bounds["max"] = max(bounds["max"], value)
                if len(report["frames"]) < detail_limit:
                    report["frames"].append({"offset": offset, **decoded})
            cursor = start + FRAME_BYTES
        if not chunk:
            break
        del buffer[:cursor]
        base += cursor

    start = buffer.find(MAGIC, cursor)
    # At EOF cursor may already point to an incomplete candidate.
    if start >= 0:
        offset = base + start
        report["incomplete_tail"] = {"offset": offset, "bytes": report["input_bytes"] - offset,
                                     "kind": "frame_candidate"}
    else:
        for size in (3, 2, 1):
            if buffer.endswith(MAGIC[:size]):
                report["incomplete_tail"] = {
                    "offset": report["input_bytes"] - size, "bytes": size, "kind": "magic_prefix"}
                break
    if report["incomplete_tail"] is not None:
        tail = report["incomplete_tail"]
        event("incomplete_tail", tail["offset"], length=tail["bytes"], tail_kind=tail["kind"])
    report["trailing_unframed_bytes"] = report["input_bytes"] - last_end
    if report["trailing_unframed_bytes"]:
        event("trailing_unframed_bytes", last_end, length=report["trailing_unframed_bytes"])
    report["unframed_bytes"] = report["input_bytes"] - report["crc_valid_frames"] * FRAME_BYTES
    report["sha256"] = digest.hexdigest()
    report["omitted_frame_details"] = report["decoded_frames"] - len(report["frames"])
    report["omitted_event_details"] = report["event_count"] - len(report["events"])
    report["clean"] = bool(report["decoded_frames"]) and not any(report[name] for name in (
        "unframed_bytes", "crc_failed_candidates", "unsupported_format_frames", "nonfinite_iq_frames"))
    return report


def audit_file(path, profile, detail_limit=16):
    """Read only a regular, non-symlink file; never open a known device/FIFO."""
    if profile not in PROFILES:
        raise ValueError("an explicit oem or extended-r8 profile is required")
    if not 0 <= detail_limit <= 1000:
        raise ValueError("detail limit must be between 0 and 1000")
    before = os.lstat(path)
    if not stat.S_ISREG(before.st_mode):
        raise ValueError("input must be a regular file, not a device, FIFO, directory or symlink")
    flags = os.O_RDONLY | os.O_NONBLOCK | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(path, flags)
    try:
        after = os.fstat(fd)
        if not stat.S_ISREG(after.st_mode) or (before.st_dev, before.st_ino) != (after.st_dev, after.st_ino):
            raise ValueError("input changed while opening or is not a regular file")
        with os.fdopen(fd, "rb") as stream:
            fd = None
            return _audit_stream(stream, profile, detail_limit)
    finally:
        if fd is not None:
            os.close(fd)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("capture", help="saved binary capture (regular file; no stdin, symlinks or devices)")
    parser.add_argument("--profile", choices=PROFILES, required=True,
                        help="oem for stock/alpha.2; extended-r8 only for known extended metadata")
    parser.add_argument("--detail-limit", type=int, default=16,
                        help="maximum saved frame details and event details, separately (0..1000; default 16)")
    args = parser.parse_args(argv)
    try:
        report = audit_file(args.capture, args.profile, args.detail_limit)
    except (OSError, ValueError) as error:
        parser.exit(2, f"error: {error}\n")
    json.dump(report, sys.stdout, indent=2, allow_nan=False)
    print()
    return 0 if report["clean"] else 1


if __name__ == "__main__":
    sys.exit(main())
