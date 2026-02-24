"""
Read-only inventory parsing helpers.

支持翻译：
- 容器类型翻译
- 物品名称翻译
"""
from __future__ import annotations

from collections import Counter
import threading
from typing import Dict, List, Optional, Tuple, Callable

from services.kahlua_skip import skip_kahlua_table
from services.parse_debug_log import log_parse_exception
from utils.bytebuffer_reader import ByteBufferReader
from utils.pz_string_codec import read_string, read_string_utf
from services.i18n_archive import archive_i18n
from services.item_translation_service import translate_item_list


_OFFSET_LOCAL = threading.local()


def set_offset_collector(collector: Optional[Callable[[int, int], None]]) -> None:
    _OFFSET_LOCAL.collector = collector


def get_offset_collector() -> Optional[Callable[[int, int], None]]:
    return getattr(_OFFSET_LOCAL, "collector", None)


def _is_probable_container_type(text: str) -> bool:
    if not text:
        return False
    ascii_hits = 0
    for ch in text:
        if ch.isascii() and (ch.isalnum() or ch in "._-"):
            ascii_hits += 1
    return ascii_hits >= max(1, len(text) // 4)


def parse_item_container_summary(
    reader: ByteBufferReader,
    world_version: int,
    dictionary: Optional[Dict[int, str]],
    *,
    max_top: int = 6,
    include_item_details: bool = False,
    include_item_counts: bool = False,
    detail_limit: int = 200,
    offset_collector: Optional[Callable[[int, int], None]] = None,
) -> Dict[str, object]:
    try:
        if offset_collector is None:
            offset_collector = get_offset_collector()
        container_type = read_string(reader)
        if container_type and not _is_probable_container_type(container_type):
            container_type = ""
        explored = reader.read_u8() == 1
        counts, total_items, samples, item_details = _parse_compressed_items(
            reader,
            world_version,
            dictionary,
            max_top=max_top,
            include_item_details=include_item_details,
            detail_limit=detail_limit,
            offset_collector=offset_collector,
        )
        has_looted = reader.read_u8() == 1
        capacity = reader.read_i32()
        unique_items = len(counts)
        top_items = counts.most_common(max_top) if counts else []
        
        # 翻译容器类型
        container_type_translated = None
        if container_type:
            container_type_translated = archive_i18n.translate_container(container_type)
        
        summary = {
            "type": container_type,
            "type_translated": container_type_translated,
            "explored": explored,
            "has_looted": has_looted,
            "capacity": capacity,
            "total_items": total_items,
            "unique_items": unique_items,
            "top_items": top_items,
            "samples": samples,
        }
        if include_item_details:
            # 翻译物品详情
            translated_details = []
            for detail in item_details:
                item_type = detail.get("type", "")
                translated_detail = dict(detail)
                if item_type:
                    translated_names = translate_item_list([item_type])
                    # 从翻译结果中提取纯翻译名称（去除原始ID）
                    translated_name = translated_names[0] if translated_names else item_type
                    if " (" in translated_name and translated_name.endswith(")"):
                        # 格式: "翻译名称 (原始ID)" -> 提取翻译名称
                        translated_name = translated_name.rsplit(" (", 1)[0]
                    translated_detail["type_translated"] = translated_name
                translated_details.append(translated_detail)
            summary["item_details"] = translated_details
        if include_item_counts:
            summary["item_counts"] = counts.most_common()
        return summary
    except Exception as exc:
        log_parse_exception(
            "item_container_parse_failed",
            exc,
            source="inventory_parser",
            pos=reader.tell(),
            remaining=reader.remaining(),
            world_version=world_version,
        )
        raise


def parse_inventory_item_summary(
    reader: ByteBufferReader,
    world_version: int,
    dictionary: Optional[Dict[int, str]],
    *,
    strict: bool = True,
    include_details: bool = False,
    offset_collector: Optional[Callable[[int, int], None]] = None,
) -> Dict[str, object]:
    start = reader.tell()
    size = None
    payload_start = None
    registry_id = None
    save_type = None
    item_id = None
    buffer_end = None
    calc_end = None
    end = None
    try:
        size = reader.read_i32()
        if size is None or size <= 0:
            raise ValueError("invalid item size")
        payload_start = reader.tell()
        if include_details:
            buffer_end = reader.tell() + reader.remaining()
            end = _resolve_inventory_item_end(payload_start, size, buffer_end)
            payload_len = end - payload_start
            payload = reader.read_bytes(payload_len)
            payload_reader = ByteBufferReader(payload)
            registry_pos = payload_start + payload_reader.tell()
            registry_id = payload_reader.read_u16()
            if offset_collector is None:
                offset_collector = get_offset_collector()
            if offset_collector is not None:
                try:
                    offset_collector(registry_pos, registry_id)
                except Exception:
                    pass
            save_type = payload_reader.read_i8()
            item_id = payload_reader.read_i32()
            full_type = dictionary.get(registry_id) if dictionary else ""
            details: Dict[str, object] = {}
            try:
                details = _read_inventory_item_details(payload_reader, world_version)
                container_tail = _try_read_inventory_container_tail(
                    payload_reader,
                    world_version,
                    dictionary,
                    detail_limit=200,
                )
                if container_tail:
                    details.update(container_tail)
            except Exception as exc:
                details = {"detail_error": True}
                log_parse_exception(
                    "inventory_item_detail_parse_failed",
                    exc,
                    source="inventory_parser",
                    pos=payload_reader.tell(),
                    start=start,
                    world_version=world_version,
                    size=size,
                    registry_id=registry_id,
                    save_type=save_type,
                    item_id=item_id,
                )
            summary = {
                "size": size,
                "registry_id": registry_id,
                "save_type": save_type,
                "full_type": full_type,
                "item_id": item_id,
            }
            summary.update(details)
            return summary
        registry_pos = reader.tell()
        registry_id = reader.read_u16()
        if offset_collector is None:
            offset_collector = get_offset_collector()
        if offset_collector is not None:
            try:
                offset_collector(registry_pos, registry_id)
            except Exception:
                pass
        save_type = reader.read_i8()
        full_type = dictionary.get(registry_id) if dictionary else ""
        buffer_end = reader.tell() + reader.remaining()
        calc_end = payload_start + size
        end = calc_end
        if end < reader.tell() or end > buffer_end:
            alt_end = reader.tell() + size
            if alt_end < reader.tell() or alt_end > buffer_end:
                raise ValueError("item payload out of bounds")
            end = alt_end
        reader.seek(end)
        return {
            "size": size,
            "registry_id": registry_id,
            "save_type": save_type,
            "full_type": full_type,
        }
    except Exception as exc:
        log_parse_exception(
            "inventory_item_parse_failed",
            exc,
            source="inventory_parser",
            pos=reader.tell(),
            start=start,
            world_version=world_version,
            remaining=reader.remaining(),
            size=size,
            registry_id=registry_id,
            save_type=save_type,
            item_id=item_id,
            payload_start=payload_start,
            buffer_end=buffer_end,
            calc_end=calc_end,
            end=end,
        )
        if strict:
            raise
        _recover_inventory_item(reader, size, payload_start)
        return {
            "size": size,
            "registry_id": registry_id,
            "save_type": save_type,
            "full_type": dictionary.get(registry_id) if dictionary else "",
            "error": True,
        }


def _parse_compressed_items(
    reader: ByteBufferReader,
    world_version: int,
    dictionary: Optional[Dict[int, str]],
    *,
    max_top: int,
    include_item_details: bool,
    detail_limit: int,
    offset_collector: Optional[Callable[[int, int], None]] = None,
) -> Tuple[Counter, int, List[Dict[str, object]], List[Dict[str, object]]]:
    group_count = reader.read_i16()
    if group_count < 0:
        group_count = 0
    counts: Counter = Counter()
    samples: List[Dict[str, object]] = []
    details: List[Dict[str, object]] = []
    total_items = 0
    unlimited = detail_limit <= 0
    for _ in range(group_count):
        count = _read_group_count(reader, world_version)
        if count < 1:
            count = 1
        item_summary = parse_inventory_item_summary(
            reader,
            world_version,
            dictionary,
            strict=False,
            include_details=include_item_details,
            offset_collector=offset_collector,
        )
        key = item_summary.get("full_type") or f"id:{item_summary.get('registry_id')}"
        if key:
            counts[key] += count
        total_items += count
        if len(samples) < max_top:
            samples.append({**item_summary, "count": count})
        if include_item_details and (unlimited or len(details) < detail_limit):
            details.append({**item_summary, "count": count})
        if world_version >= 128 and count > 1:
            for _ in range(count - 1):
                reader.read_i32()
    return counts, total_items, samples, details


def _recover_inventory_item(
    reader: ByteBufferReader,
    size: Optional[int],
    payload_start: Optional[int],
) -> None:
    buffer_end = reader.tell() + reader.remaining()
    if size is not None and size > 0 and payload_start is not None:
        end = payload_start + size
        if end <= buffer_end:
            reader.seek(end)
            return
    reader.seek(buffer_end)


def _read_group_count(reader: ByteBufferReader, world_version: int) -> int:
    if world_version >= 149:
        return reader.read_i32()
    if world_version >= 128:
        return reader.read_i16()
    return 1


def _resolve_inventory_item_end(
    payload_start: int,
    size: int,
    buffer_end: int,
) -> int:
    min_end = payload_start + 7
    end = payload_start + size
    if end < min_end or end > buffer_end:
        end = payload_start + 3 + size
        if end < min_end or end > buffer_end:
            raise ValueError("item payload out of bounds")
    return end


def _read_kahlua_value(
    reader: ByteBufferReader,
    world_version: int,
    value_type: int,
    depth: int,
) -> object:
    if value_type == 0:
        return read_string_utf(reader)
    if value_type == 1:
        return reader.read_f64()
    if value_type == 3:
        return reader.read_u8() == 1
    if value_type == 2:
        skip_kahlua_table(reader, world_version, depth=depth)
        return None
    raise ValueError("unknown kahlua type")


def _normalize_kahlua_key(key: object) -> str:
    if key is None:
        return ""
    if isinstance(key, (str, int, float, bool)):
        return str(key)
    return ""


def _read_kahlua_table_summary(
    reader: ByteBufferReader,
    world_version: int,
    depth: int = 0,
) -> Dict[str, object]:
    start = reader.tell()
    try:
        if depth >= 4:
            skip_kahlua_table(reader, world_version, depth=depth)
            return {}
        count = reader.read_i32()
        if count < 0:
            raise ValueError("kahlua table count invalid")
        values: Dict[str, object] = {}
        if world_version >= 25:
            for _ in range(count):
                key_type = reader.read_i8()
                key_value = _read_kahlua_value(reader, world_version, key_type, depth + 1)
                value_type = reader.read_i8()
                value = _read_kahlua_value(reader, world_version, value_type, depth + 1)
                key_name = _normalize_kahlua_key(key_value)
                if not key_name or value is None:
                    continue
                if isinstance(value, (str, int, float, bool)):
                    values[key_name] = value
            return values
        for _ in range(count):
            value_type = reader.read_i8()
            key_name = read_string_utf(reader)
            value = _read_kahlua_value(reader, world_version, value_type, depth + 1)
            if not key_name or value is None:
                continue
            if isinstance(value, (str, int, float, bool)):
                values[key_name] = value
        return values
    except Exception as exc:
        reader.seek(start)
        try:
            skip_kahlua_table(reader, world_version, depth=depth)
        except Exception:
            pass
        log_parse_exception(
            "inventory_item_kahlua_parse_failed",
            exc,
            source="inventory_parser",
            pos=start,
            world_version=world_version,
        )
        return {}


def _extract_food_details(kahlua_values: Dict[str, object]) -> Dict[str, object]:
    if not kahlua_values:
        return {}
    lowered = {str(key).lower(): value for key, value in kahlua_values.items()}

    def _as_float(value: object) -> Optional[float]:
        if isinstance(value, (int, float)):
            return float(value)
        return None

    def _as_bool(value: object) -> Optional[bool]:
        if isinstance(value, bool):
            return value
        if isinstance(value, (int, float)):
            return bool(int(value))
        return None

    age = _as_float(lowered.get("age") or lowered.get("foodage"))
    off_age = _as_float(lowered.get("offage"))
    off_age_max = _as_float(lowered.get("offagemax"))
    rotten = _as_bool(lowered.get("isrotten") or lowered.get("rotten"))
    cooked = _as_bool(lowered.get("iscooked") or lowered.get("cooked"))
    burnt = _as_bool(lowered.get("isburnt") or lowered.get("burnt"))

    expired: Optional[bool] = None
    if rotten is not None:
        expired = rotten
    elif age is not None and off_age is not None:
        expired = age >= off_age
    elif age is not None and off_age_max is not None:
        expired = age >= off_age_max

    details: Dict[str, object] = {}
    if age is not None:
        details["food_age"] = age
    if off_age is not None:
        details["food_off_age"] = off_age
    if off_age_max is not None:
        details["food_off_age_max"] = off_age_max
    if expired is not None:
        details["food_expired"] = expired
    if cooked is not None:
        details["food_cooked"] = cooked
    if burnt is not None:
        details["food_burnt"] = burnt
    return details


def _read_inventory_item_details(
    reader: ByteBufferReader,
    world_version: int,
) -> Dict[str, object]:
    details: Dict[str, object] = {}
    header_flags = reader.read_u8()
    if header_flags & 1:
        if world_version >= 200:
            reader.read_i32()
        else:
            reader.read_i16()
    if header_flags & 2:
        raw_delta = reader.read_i8()
        details["used_delta"] = (raw_delta + 128) / 255.0
    if header_flags & 4:
        details["condition"] = reader.read_u8()
    if header_flags & 8:
        _skip_item_visual_save(reader)
    if header_flags & 16:
        reader.skip(4)
    if header_flags & 32:
        details["item_capacity"] = reader.read_f32()
    if header_flags & 64:
        details.update(_read_inventory_item_details_header2(reader, world_version))
    return details


def _read_inventory_item_details_header2(
    reader: ByteBufferReader,
    world_version: int,
) -> Dict[str, object]:
    details: Dict[str, object] = {}
    flags = reader.read_i32()
    if flags & 1:
        kahlua_values = _read_kahlua_table_summary(reader, world_version, depth=0)
        details.update(_extract_food_details(kahlua_values))
    if flags & 4:
        reader.read_i16()
    if flags & 8:
        details["custom_name"] = read_string(reader)
    if flags & 16:
        byte_len = reader.read_i32()
        if byte_len < 0:
            raise ValueError("item byteData length invalid")
        if byte_len:
            reader.skip(byte_len)
    if flags & 32:
        extra_count = reader.read_i32()
        if extra_count < 0:
            raise ValueError("item extraItems count invalid")
        for _ in range(extra_count):
            reader.read_u16()
    if flags & 128:
        details["actual_weight"] = reader.read_f32()
    if flags & 256:
        reader.read_i32()
    if flags & 1024:
        reader.read_i32()
        reader.read_i32()
    if flags & 2048:
        reader.skip(3)
    if flags & 4096:
        read_string(reader)
    if flags & 8192:
        reader.read_f32()
    if flags & 32768:
        read_string(reader)
    if flags & 131072:
        reader.read_i32()
    if flags & 262144:
        reader.read_i32()
    if flags & 524288:
        read_string(reader)
    if flags & 1048576:
        read_string(reader)
    if flags & 2097152:
        details["max_capacity"] = reader.read_i32()
    return details


def _try_read_inventory_container_tail(
    reader: ByteBufferReader,
    world_version: int,
    dictionary: Optional[Dict[int, str]],
    *,
    detail_limit: int,
) -> Dict[str, object]:
    if reader.remaining() < 8:
        return {}
    start = reader.tell()
    try:
        container_id = reader.read_i32()
        weight_reduction = reader.read_i32()
        container_summary = parse_item_container_summary(
            reader,
            world_version,
            dictionary,
            max_top=8,
            include_item_details=True,
            include_item_counts=True,
            detail_limit=detail_limit,
        )
        if reader.remaining() != 0:
            raise ValueError("container tail not fully consumed")
        return {
            "container_id": container_id,
            "container_weight_reduction": weight_reduction,
            "container_summary": container_summary,
        }
    except Exception:
        reader.seek(start)
        return {}


def _skip_item_visual_save(reader: ByteBufferReader) -> None:
    flags = reader.read_u8()
    read_string(reader)
    read_string(reader)
    read_string(reader)
    if flags & 1:
        reader.skip(3)
    if flags & 2:
        reader.read_u8()
    if flags & 4:
        reader.read_u8()
    if flags & 8:
        reader.read_f32()
    if flags & 16:
        read_string(reader)
    blood_count = reader.read_i8()
    if blood_count > 0:
        reader.skip(blood_count)
    dirt_count = reader.read_i8()
    if dirt_count > 0:
        reader.skip(dirt_count)
    holes_count = reader.read_i8()
    if holes_count > 0:
        reader.skip(holes_count)
    basic_count = reader.read_i8()
    if basic_count > 0:
        reader.skip(basic_count)
    denim_count = reader.read_i8()
    if denim_count > 0:
        reader.skip(denim_count)
    leather_count = reader.read_i8()
    if leather_count > 0:
        reader.skip(leather_count)
