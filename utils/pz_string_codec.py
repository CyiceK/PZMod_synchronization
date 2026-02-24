"""
String codec helpers for Project Zomboid ByteBuffer formats.
"""
from __future__ import annotations

from utils.bytebuffer_reader import ByteBufferReader


def _decode_modified_utf8(data: bytes) -> str:
    """Decode Java Modified UTF-8 bytes (used by PZ save buffers)."""
    code_units = []
    i = 0
    length = len(data)
    while i < length:
        b0 = data[i]
        if b0 < 0x80:
            code_units.append(b0)
            i += 1
            continue
        if (b0 & 0xE0) == 0xC0:
            if i + 1 >= length:
                raise ValueError("truncated modified utf-8 sequence")
            b1 = data[i + 1]
            if b0 == 0xC0 and b1 == 0x80:
                code_units.append(0)
            else:
                code_units.append(((b0 & 0x1F) << 6) | (b1 & 0x3F))
            i += 2
            continue
        if (b0 & 0xF0) == 0xE0:
            if i + 2 >= length:
                raise ValueError("truncated modified utf-8 sequence")
            b1 = data[i + 1]
            b2 = data[i + 2]
            code_units.append(
                ((b0 & 0x0F) << 12) | ((b1 & 0x3F) << 6) | (b2 & 0x3F)
            )
            i += 3
            continue
        raise ValueError("invalid modified utf-8 lead byte")
    chars = []
    idx = 0
    total = len(code_units)
    while idx < total:
        unit = code_units[idx]
        if 0xD800 <= unit <= 0xDBFF and idx + 1 < total:
            low = code_units[idx + 1]
            if 0xDC00 <= low <= 0xDFFF:
                codepoint = 0x10000 + ((unit - 0xD800) << 10) + (low - 0xDC00)
                chars.append(chr(codepoint))
                idx += 2
                continue
        chars.append(chr(unit))
        idx += 1
    return "".join(chars)


def _sanitize_text(text: str) -> str:
    if not text:
        return ""
    cleaned = []
    for ch in text:
        code = ord(ch)
        if ch in ("\ufeff", "\ufffd"):
            continue
        if code < 32 and ch not in ("\t", "\n", "\r"):
            continue
        if code == 0x7F:
            continue
        cleaned.append(ch)
    return "".join(cleaned)


def _score_text(text: str) -> int:
    if not text:
        return -10
    score = 0
    for ch in text:
        code = ord(ch)
        if code < 32 and ch not in ("\t", "\n", "\r"):
            score -= 3
            continue
        if ch.isprintable():
            score += 1
        if 0x4E00 <= code <= 0x9FFF:
            score += 2
    return score


def _decode_best(data: bytes) -> str:
    candidates = []

    def _add_candidate(text: str) -> None:
        cleaned = _sanitize_text(text)
        candidates.append((_score_text(cleaned), cleaned))

    try:
        _add_candidate(data.decode("utf-8"))
    except Exception:
        pass
    try:
        _add_candidate(_decode_modified_utf8(data))
    except Exception:
        pass
    for encoding in ("gb18030", "cp1252", "latin-1"):
        try:
            _add_candidate(data.decode(encoding))
        except Exception:
            continue
    if not candidates:
        return ""
    candidates.sort(key=lambda item: item[0], reverse=True)
    return candidates[0][1]


def decode_text_bytes(data: bytes) -> str:
    return _decode_best(data)


def decode_utf8_bytes(data: bytes) -> str:
    try:
        return data.decode("utf-8")
    except Exception:
        return data.decode("utf-8", errors="replace")


def read_string_utf(reader: ByteBufferReader) -> str:
    length = reader.read_i16()
    if length <= 0:
        return ""
    if length > reader.remaining():
        raise ValueError("string length exceeds buffer")
    data = reader.read_bytes(length)
    return decode_text_bytes(data)


def read_string_utf8(reader: ByteBufferReader) -> str:
    length = reader.read_i16()
    if length <= 0:
        return ""
    if length > reader.remaining():
        raise ValueError("string length exceeds buffer")
    data = reader.read_bytes(length)
    return decode_utf8_bytes(data)


def read_string(reader: ByteBufferReader) -> str:
    return read_string_utf(reader)
