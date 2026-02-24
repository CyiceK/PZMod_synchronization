"""
Kahlua table skip helpers for read-only parsing.
"""
from __future__ import annotations

from services.parse_debug_log import log_parse_debug
from utils.bytebuffer_reader import ByteBufferReader
from utils.pz_string_codec import read_string_utf


def skip_kahlua_table(reader: ByteBufferReader, world_version: int, depth: int = 0) -> None:
    if depth >= 6:
        raise ValueError("kahlua table depth too deep")
    count = reader.read_i32()
    if count < 0:
        raise ValueError("kahlua table count invalid")
    if world_version >= 25:
        for _ in range(count):
            key_type = reader.read_i8()
            _skip_kahlua_value(reader, world_version, key_type, depth + 1)
            value_type = reader.read_i8()
            _skip_kahlua_value(reader, world_version, value_type, depth + 1)
    else:
        for _ in range(count):
            value_type = reader.read_i8()
            read_string_utf(reader)
            _skip_kahlua_value(reader, world_version, value_type, depth + 1)


def _skip_kahlua_value(
    reader: ByteBufferReader, world_version: int, value_type: int, depth: int
) -> None:
    # Basic types
    if value_type == 0:  # nil (string in old format)
        read_string_utf(reader)
        return
    if value_type == 1:  # number (double)
        reader.read_f64()
        return
    if value_type == 3:  # boolean
        reader.read_u8()
        return
    if value_type == 2:  # table
        skip_kahlua_table(reader, world_version, depth)
        return
    
    # B42 new types based on observed values
    # type 17 (0x11): likely integer/int64
    if value_type == 17:
        reader.read_i64()
        log_parse_debug(
            "kahlua_type_17_int64",
            source="kahlua_skip",
            value_type=value_type,
            pos=reader.tell() - 8,
            world_version=world_version,
        )
        return
    
    # type 97 ('a'): likely array or annotation marker - skip 4 bytes
    if value_type == 97:
        # Try reading as length-prefixed data
        try:
            arr_len = reader.read_i32()
            if arr_len > 0 and arr_len < 1000000:  # sanity check
                reader.skip(arr_len)
            log_parse_debug(
                "kahlua_type_97_array",
                source="kahlua_skip",
                value_type=value_type,
                arr_len=arr_len,
                pos=reader.tell() - 4 - max(0, arr_len),
                world_version=world_version,
            )
        except Exception:
            # fallback: just skip 4 bytes
            reader.read_i32()
        return
    
    # type 100 ('d'): likely double variant or date
    if value_type == 100:
        reader.read_f64()
        log_parse_debug(
            "kahlua_type_100_double",
            source="kahlua_skip",
            value_type=value_type,
            pos=reader.tell() - 8,
            world_version=world_version,
        )
        return
    
    # type -5: negative marker, likely special control or error
    if value_type == -5:
        # Skip 1 byte as placeholder
        reader.read_u8()
        log_parse_debug(
            "kahlua_type_neg5_special",
            source="kahlua_skip",
            value_type=value_type,
            pos=reader.tell() - 1,
            world_version=world_version,
        )
        return
    
    # Extended types for B42 (based on Java serialization patterns)
    # type 4: function (skip bytecode)
    if value_type == 4:
        # Function: string name + int upvalues + bytecode
        read_string_utf(reader)  # name
        upvalues = reader.read_i32()
        for _ in range(upvalues):
            reader.read_u8()  # upvalue type
            read_string_utf(reader)  # upvalue name
        # Skip bytecode (length + bytes)
        code_len = reader.read_i32()
        if code_len > 0:
            reader.skip(code_len * 4)  # instructions are 4 bytes
        return
    
    # type 5: userdata
    if value_type == 5:
        # Userdata: metatable ref + data
        reader.read_i32()  # metatable reference
        data_len = reader.read_i32()
        if data_len > 0:
            reader.skip(data_len)
        return
    
    # type 6: thread/coroutine
    if value_type == 6:
        # Coroutine: just a reference
        reader.read_i32()
        return
    
    # type 7: long (int64)
    if value_type == 7:
        reader.read_i64()
        return
    
    # type 8: short string (inline)
    if value_type == 8:
        length = reader.read_u8()
        reader.skip(length)
        return
    
    # type 9: long string (with length prefix)
    if value_type == 9:
        read_string_utf(reader)
        return
    
    # type 10: integer (32-bit)
    if value_type == 10:
        reader.read_i32()
        return
    
    log_parse_debug(
        "kahlua_unknown_type",
        source="kahlua_skip",
        value_type=value_type,
        world_version=world_version,
        depth=depth,
        pos=reader.tell(),
        remaining=reader.remaining(),
    )
    raise ValueError(f"unknown kahlua type: {value_type}")
