"""Tests for the OGG file-boundary splitter in Kokoro provider.

The splitter handles the Windows loopback TCP coalescing case: when
`requests.iter_content` returns multiple OGG files concatenated into one
yield (which Win does aggressively, Linux rarely), the splitter detects
EOS pages and emits each file separately so the browser's <audio> element
and sf.read can decode them all.
"""
import struct

import pytest

from core.tts.providers.kokoro import _split_complete_oggs


def _make_ogg_page(serial: int, page_seq: int, eos: bool, data: bytes = b"PAYLOAD") -> bytes:
    """Build a minimal valid OGG page with the EOS bit set or not.

    Page header (27 bytes fixed + segment_table[num_segments]):
      4   capture_pattern "OggS"
      1   version (0)
      1   header_type_flag (bit 0=continued, bit 1=BOS, bit 2=EOS)
      8   granule_position
      4   serial_number
      4   page_sequence
      4   crc (ignored here — we don't validate)
      1   num_segments
      N   segment_table (lacing values; sum = data length)
    """
    flag = 0x04 if eos else 0x00
    # Encode `len(data)` as one or more 255-byte lacing values + remainder
    segment_table = []
    remaining = len(data)
    while remaining >= 255:
        segment_table.append(255)
        remaining -= 255
    segment_table.append(remaining)
    num_segments = len(segment_table)
    header = (
        b"OggS"
        + b"\x00"  # version
        + bytes([flag])  # header_type_flag
        + struct.pack("<q", 0)  # granule_position
        + struct.pack("<I", serial)  # serial_number
        + struct.pack("<I", page_seq)  # page_sequence
        + struct.pack("<I", 0)  # crc (not validated by splitter)
        + bytes([num_segments])  # num_segments
        + bytes(segment_table)
    )
    return header + data


def _make_ogg_file(serial: int, num_pages: int = 2) -> bytes:
    """Build a complete fake OGG file with `num_pages` pages, last one EOS."""
    pages = []
    for i in range(num_pages):
        eos = (i == num_pages - 1)
        pages.append(_make_ogg_page(serial=serial, page_seq=i, eos=eos, data=f"PAGE_{serial}_{i}".encode()))
    return b"".join(pages)


def test_split_single_complete_ogg():
    """A single complete OGG returns one file + no remaining."""
    blob = _make_ogg_file(serial=42, num_pages=2)
    files, remaining = _split_complete_oggs(blob)
    assert len(files) == 1
    assert files[0] == blob
    assert remaining == b""


def test_split_two_concatenated_oggs():
    """Two concatenated OGGs (Win loopback case) get split into two files."""
    f1 = _make_ogg_file(serial=1, num_pages=2)
    f2 = _make_ogg_file(serial=2, num_pages=2)
    combined = f1 + f2
    files, remaining = _split_complete_oggs(combined)
    assert len(files) == 2
    assert files[0] == f1
    assert files[1] == f2
    assert remaining == b""


def test_split_partial_page_returned_as_remaining():
    """A partial OGG (no EOS yet) is returned in `remaining` for next round."""
    f1 = _make_ogg_file(serial=1, num_pages=2)
    # Add a partial second OGG — just a page header with no EOS
    partial = _make_ogg_page(serial=2, page_seq=0, eos=False, data=b"INCOMPLETE")
    blob = f1 + partial
    files, remaining = _split_complete_oggs(blob)
    assert len(files) == 1
    assert files[0] == f1
    assert remaining == partial


def test_split_truncated_header_returned_as_remaining():
    """A truncated page header (less than 27 bytes) stays in remaining."""
    f1 = _make_ogg_file(serial=1, num_pages=2)
    blob = f1 + b"OggS\x00"  # only 5 bytes of next page
    files, remaining = _split_complete_oggs(blob)
    assert len(files) == 1
    assert remaining == b"OggS\x00"


def test_split_stray_OggS_in_payload_skipped():
    """A non-page 'OggS' sequence in payload bytes (version byte != 0) is skipped."""
    # Build a page whose data contains 'OggS' followed by a non-zero byte
    poison_data = b"PRE-OggS\x99-POISONED"
    page1 = _make_ogg_page(serial=1, page_seq=0, eos=False, data=poison_data)
    page2 = _make_ogg_page(serial=1, page_seq=1, eos=True, data=b"END")
    blob = page1 + page2
    files, remaining = _split_complete_oggs(blob)
    # Splitter should NOT treat the poison as a new file
    assert len(files) == 1
    assert files[0] == blob
    assert remaining == b""


def test_split_empty_input():
    files, remaining = _split_complete_oggs(b"")
    assert files == []
    assert remaining == b""


def test_split_progressive_streaming():
    """Simulate streaming: feed bytes in small chunks, accumulate, split each round."""
    f1 = _make_ogg_file(serial=1, num_pages=3)
    f2 = _make_ogg_file(serial=2, num_pages=2)
    all_bytes = f1 + f2

    buffer = bytearray()
    yielded_files = []
    # Feed 10 bytes at a time
    for i in range(0, len(all_bytes), 10):
        buffer.extend(all_bytes[i:i + 10])
        complete, remaining = _split_complete_oggs(bytes(buffer))
        yielded_files.extend(complete)
        buffer = bytearray(remaining)
    # All bytes consumed except possibly final partial — but both files are EOS-terminated
    assert buffer == b""
    assert len(yielded_files) == 2
    assert yielded_files[0] == f1
    assert yielded_files[1] == f2
