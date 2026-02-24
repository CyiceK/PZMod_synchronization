"""
Byte buffer reader helpers for Project Zomboid formats.
"""
from __future__ import annotations

import struct
from typing import Optional


class ByteBufferReader:
    """Minimal big-endian byte buffer reader."""

    def __init__(self, data: bytes) -> None:
        self._data = memoryview(data)
        self._pos = 0

    def tell(self) -> int:
        return self._pos

    def remaining(self) -> int:
        return len(self._data) - self._pos

    def seek(self, pos: int) -> None:
        if pos < 0 or pos > len(self._data):
            raise ValueError("seek out of range")
        self._pos = pos

    def skip(self, size: int) -> None:
        self._require(size)
        self._pos += size

    def read_bytes(self, size: int) -> bytes:
        self._require(size)
        start = self._pos
        self._pos += size
        return self._data[start:start + size].tobytes()

    def read_u8(self) -> int:
        self._require(1)
        value = self._data[self._pos]
        self._pos += 1
        return int(value)

    def read_i8(self) -> int:
        value = self.read_u8()
        return value - 256 if value > 127 else value

    def read_u16(self) -> int:
        self._require(2)
        value = int.from_bytes(self._data[self._pos:self._pos + 2], "big", signed=False)
        self._pos += 2
        return value

    def read_i16(self) -> int:
        self._require(2)
        value = int.from_bytes(self._data[self._pos:self._pos + 2], "big", signed=True)
        self._pos += 2
        return value

    def read_u32(self) -> int:
        self._require(4)
        value = int.from_bytes(self._data[self._pos:self._pos + 4], "big", signed=False)
        self._pos += 4
        return value

    def read_i32(self) -> int:
        self._require(4)
        value = int.from_bytes(self._data[self._pos:self._pos + 4], "big", signed=True)
        self._pos += 4
        return value

    def read_f32(self) -> float:
        self._require(4)
        value = struct.unpack_from(">f", self._data, self._pos)[0]
        self._pos += 4
        return float(value)

    def read_f64(self) -> float:
        self._require(8)
        value = struct.unpack_from(">d", self._data, self._pos)[0]
        self._pos += 8
        return float(value)

    def read_i64(self) -> int:
        self._require(8)
        value = int.from_bytes(self._data[self._pos:self._pos + 8], "big", signed=True)
        self._pos += 8
        return value

    def _require(self, size: int) -> None:
        if size < 0 or self._pos + size > len(self._data):
            raise ValueError("buffer underflow")
