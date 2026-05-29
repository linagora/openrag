"""Tests for the reverse block file reader."""

from __future__ import annotations

from core.utils.log_tail import iter_file_lines_reversed


def test_reads_lines_newest_first_across_blocks(tmp_path):
    f = tmp_path / "log.txt"
    f.write_text("l1\nl2\nl3\nl4")
    # small block size forces multi-block reads
    assert list(iter_file_lines_reversed(f, block_size=4)) == ["l4", "l3", "l2", "l1"]


def test_skips_blank_lines(tmp_path):
    f = tmp_path / "log.txt"
    f.write_text("a\n\n\nb\n")
    assert list(iter_file_lines_reversed(f)) == ["b", "a"]


def test_empty_file(tmp_path):
    f = tmp_path / "log.txt"
    f.write_text("")
    assert list(iter_file_lines_reversed(f)) == []
