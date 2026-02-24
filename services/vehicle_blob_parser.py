"""
Vehicle BLOB parser (read-only, best-effort).

支持翻译：
- 载具部件名称翻译
- 载具类型翻译
"""
from __future__ import annotations

from typing import Dict, Optional, Tuple, List

from utils.bytebuffer_reader import ByteBufferReader
from utils.pz_string_codec import read_string, read_string_utf
from services.inventory_parser import (
    parse_inventory_item_summary,
    parse_item_container_summary,
    set_offset_collector,
)
from services.kahlua_skip import skip_kahlua_table
from services.parse_debug_log import log_parse_exception
from services.i18n_archive import archive_i18n


def parse_vehicle_blob_summary(
    blob: object,
    world_version: Optional[int],
    *,
    dictionary: Optional[Dict[int, str]] = None,
    max_parts: int = 12,
) -> Dict[str, object]:
    if not isinstance(blob, (bytes, bytearray, memoryview)):
        return {}
    try:
        data = bytes(blob)
    except Exception:
        return {}
    if not data:
        return {}

    # --- Phase 3: BLOB cache lookup ---
    _cache = None
    _cache_key = None
    try:
        from services.blob_cache import get_blob_cache

        _cache = get_blob_cache()
        _cache_key = _cache.compute_vehicle_key(data, int(world_version or 0))
        cached = _cache.get_vehicle_summary(_cache_key)
        if cached is not None:
            return cached
    except Exception:
        pass
    # --- End Phase 3 cache lookup ---

    reader = ByteBufferReader(data)
    version = int(world_version or 0)
    try:
        serialize_flag = reader.read_u8()
        class_id = reader.read_u8()
        _skip_iso_moving_object(reader, version)
        if version >= 173:
            reader.read_f32()
        reader.read_f32()
        reader.read_f32()
        reader.read_f32()
        reader.read_f32()
        script_name = read_string_utf(reader)
        skin_index = reader.read_i32()
        engine_running = reader.read_u8() == 1
        summary: Dict[str, object] = {
            "serialize_flag": serialize_flag,
            "class_id": class_id,
            "script_name": script_name,
            "skin_index": skin_index,
            "engine_running": engine_running,
        }
        try:
            details_start = reader.tell()
            summary.update(_read_vehicle_details(reader, version, dictionary, max_parts))
        except Exception as exc:
            summary["details_error"] = True
            log_parse_exception(
                "vehicle_details_parse_failed",
                exc,
                source="vehicle_blob_parser",
                pos=reader.tell(),
                start=details_start,
                remaining=reader.remaining(),
                world_version=version,
            )
        # --- Phase 3: Save to BLOB cache ---
        if _cache is not None and _cache_key is not None and summary:
            try:
                _cache.set_vehicle_summary(_cache_key, summary, version)
            except Exception:
                pass
        # --- End Phase 3 cache save ---
        return summary
    except Exception as exc:
        log_parse_exception(
            "vehicle_blob_parse_failed",
            exc,
            source="vehicle_blob_parser",
            pos=reader.tell(),
            remaining=reader.remaining(),
            world_version=version,
        )
        return {}


def scan_vehicle_registry_offsets(
    blob: object,
    world_version: Optional[int],
    *,
    dictionary: Optional[Dict[int, str]] = None,
) -> List[Tuple[int, int]]:
    if not isinstance(blob, (bytes, bytearray, memoryview)):
        return []
    try:
        data = bytes(blob)
    except Exception:
        return []
    if not data:
        return []
    offsets: List[Tuple[int, int]] = []

    def _collect(pos: int, registry_id: int) -> None:
        offsets.append((pos, registry_id))

    reader = ByteBufferReader(data)
    version = int(world_version or 0)
    set_offset_collector(_collect)
    try:
        reader.read_u8()  # serialize_flag
        reader.read_u8()  # class_id
        _skip_iso_moving_object(reader, version)
        if version >= 173:
            reader.read_f32()
        reader.read_f32()
        reader.read_f32()
        reader.read_f32()
        reader.read_f32()
        read_string_utf(reader)
        reader.read_i32()
        reader.read_u8()
        try:
            _read_vehicle_details(reader, version, dictionary, max_parts=10000)
        except Exception:
            pass
        return offsets
    finally:
        set_offset_collector(None)


def _skip_iso_moving_object(reader: ByteBufferReader, world_version: int) -> None:
    reader.read_f32()
    reader.read_f32()
    reader.read_f32()
    reader.read_f32()
    reader.read_f32()
    reader.read_i32()
    has_table = reader.read_u8()
    if has_table:
        skip_kahlua_table(reader, world_version, depth=0)


def _read_vehicle_details(
    reader: ByteBufferReader,
    world_version: int,
    dictionary: Optional[Dict[int, str]],
    max_parts: int,
) -> Dict[str, object]:
    front_end = reader.read_i32()
    rear_end = reader.read_i32()
    current_front_end = reader.read_i32()
    current_rear_end = reader.read_i32()
    engine_loudness = reader.read_i32()
    engine_quality = reader.read_i32()
    key_id = reader.read_i32()
    key_spawned = reader.read_u8() == 1
    headlights_on = reader.read_u8() == 1
    created = reader.read_u8() == 1
    sound_horn = reader.read_u8() == 1
    sound_back = reader.read_u8() == 1
    lightbar_lights = reader.read_u8()
    lightbar_siren = reader.read_u8()
    parts_count = reader.read_i16()
    if parts_count < 0:
        parts_count = 0
    parts_limit = min(parts_count, 512)

    parts = []
    counts = {
        "with_item": 0,
        "with_container": 0,
        "with_device": 0,
        "with_light": 0,
        "with_door": 0,
        "with_window": 0,
        "with_entity": 0,
    }
    for part_index in range(parts_limit):
        part_start = reader.tell()
        try:
            flags, summary = _parse_vehicle_part(
                reader,
                world_version,
                dictionary,
                capture=len(parts) < max(0, max_parts),
            )
        except Exception as exc:
            log_parse_exception(
                "vehicle_part_parse_failed",
                exc,
                source="vehicle_blob_parser",
                pos=reader.tell(),
                start=part_start,
                remaining=reader.remaining(),
                part_index=part_index,
                world_version=world_version,
            )
            raise
        for key, value in flags.items():
            if value:
                counts[key] += 1
        if summary:
            parts.append(summary)

    key_is_on_door = None
    hotwired = None
    hotwired_broken = None
    keys_in_ignition = None
    if world_version >= 112:
        key_is_on_door = reader.read_u8() == 1
        hotwired = reader.read_u8() == 1
        hotwired_broken = reader.read_u8() == 1
        keys_in_ignition = reader.read_u8() == 1

    rust = None
    color_hue = None
    color_saturation = None
    color_value = None
    if world_version >= 116:
        rust = reader.read_f32()
        color_hue = reader.read_f32()
        color_saturation = reader.read_f32()
        color_value = reader.read_f32()

    engine_power = None
    if world_version >= 117:
        engine_power = reader.read_i32()
    if world_version >= 120:
        reader.read_i16()

    mechanical_name = None
    mechanical_id = None
    if world_version >= 122:
        if world_version < 229:
            mechanical_name = read_string(reader)
        mechanical_id = reader.read_i32()

    alarmed = None
    alarm_start_time = None
    chosen_alarm_sound = None
    if world_version >= 124:
        alarmed = reader.read_u8() == 1
    if world_version >= 229:
        alarm_start_time = reader.read_f64()
        chosen_alarm_sound = read_string(reader)

    siren_start_time = None
    if world_version >= 129:
        siren_start_time = reader.read_f64()

    current_key = None
    if world_version >= 133:
        has_current_key = reader.read_u8() == 1
        if has_current_key:
            current_key = parse_inventory_item_summary(
                reader,
                world_version,
                dictionary,
                strict=False,
            )

    blood_intensity = None
    if world_version >= 165:
        blood_intensity = _read_blood_intensity(reader)

    towing_id = None
    towing_attach_self = None
    towing_attach_other = None
    towing_offset = None
    towing_present = None
    if world_version >= 174:
        towing_present = reader.read_u8() == 1
        if towing_present:
            towing_id = reader.read_i32()
            towing_attach_self = read_string_utf(reader)
            towing_attach_other = read_string_utf(reader)
            towing_offset = reader.read_f32()
    elif world_version >= 172:
        towing_id = reader.read_i32()

    regulator_speed = None
    if world_version >= 188:
        regulator_speed = reader.read_f32()

    previously_entered = None
    if world_version >= 195:
        previously_entered = reader.read_u8() == 1

    previously_moved = None
    if world_version >= 196:
        previously_moved = reader.read_u8() == 1

    animals_trailer_bytes = None
    animals_trailer_flag = None
    animals_trailer_count = None
    if world_version >= 212:
        animals_trailer_bytes = reader.read_i32()
        if animals_trailer_bytes < 0 or animals_trailer_bytes > reader.remaining():
            raise ValueError("animals trailer data length invalid")
        block_start = reader.tell()
        block_end = block_start + animals_trailer_bytes
        if animals_trailer_bytes > 0 and block_end - reader.tell() >= 1:
            animals_trailer_flag = reader.read_u8()
            if animals_trailer_flag == 1 and block_end - reader.tell() >= 4:
                animals_trailer_count = reader.read_i32()
        if block_end < reader.tell():
            raise ValueError("animals trailer data length underflow")
        reader.seek(block_end)

    summary = {
        "front_end_durability": front_end,
        "rear_end_durability": rear_end,
        "current_front_end_durability": current_front_end,
        "current_rear_end_durability": current_rear_end,
        "engine_loudness": engine_loudness,
        "engine_quality": engine_quality,
        "engine_power": engine_power,
        "key_id": key_id,
        "key_spawned": key_spawned,
        "headlights_on": headlights_on,
        "created": created,
        "sound_horn": sound_horn,
        "sound_back": sound_back,
        "lightbar_lights": lightbar_lights,
        "lightbar_siren": lightbar_siren,
        "parts_count": parts_count,
        "parts_parsed": len(parts),
        "parts_with_item": counts["with_item"],
        "parts_with_container": counts["with_container"],
        "parts_with_device": counts["with_device"],
        "parts_with_light": counts["with_light"],
        "parts_with_door": counts["with_door"],
        "parts_with_window": counts["with_window"],
        "parts_with_entity": counts["with_entity"],
        "parts": parts,
    }
    if key_is_on_door is not None:
        summary["key_is_on_door"] = key_is_on_door
        summary["hotwired"] = hotwired
        summary["hotwired_broken"] = hotwired_broken
        summary["keys_in_ignition"] = keys_in_ignition
    if rust is not None:
        summary["rust"] = rust
        summary["color_hue"] = color_hue
        summary["color_saturation"] = color_saturation
        summary["color_value"] = color_value
    if mechanical_id is not None or mechanical_name:
        summary["mechanical_id"] = mechanical_id
        summary["mechanical_name"] = mechanical_name
    if alarmed is not None:
        summary["alarmed"] = alarmed
    if alarm_start_time is not None:
        summary["alarm_start_time"] = alarm_start_time
        summary["chosen_alarm_sound"] = chosen_alarm_sound
    if siren_start_time is not None:
        summary["siren_start_time"] = siren_start_time
    if current_key:
        summary["current_key"] = current_key
    if blood_intensity:
        summary["blood_intensity"] = blood_intensity
    if towing_id is not None or towing_present is not None:
        summary["towing_id"] = towing_id
        summary["towing_attach_self"] = towing_attach_self
        summary["towing_attach_other"] = towing_attach_other
        summary["towing_offset"] = towing_offset
        summary["towing_present"] = towing_present
    if regulator_speed is not None:
        summary["regulator_speed"] = regulator_speed
    if previously_entered is not None:
        summary["previously_entered"] = previously_entered
    if previously_moved is not None:
        summary["previously_moved"] = previously_moved
    if animals_trailer_bytes is not None:
        summary["animals_trailer_bytes"] = animals_trailer_bytes
    if animals_trailer_flag is not None:
        summary["animals_trailer_flag"] = animals_trailer_flag
    if animals_trailer_count is not None:
        summary["animals_trailer_count"] = animals_trailer_count
    return summary


def _parse_vehicle_part(
    reader: ByteBufferReader,
    world_version: int,
    dictionary: Optional[Dict[int, str]],
    *,
    capture: bool,
) -> Tuple[Dict[str, bool], Optional[Dict[str, object]]]:
    stage = "part_id"
    start = reader.tell()
    try:
        part_id = read_string(reader)
        stage = "created"
        created = reader.read_u8() == 1
        stage = "last_updated"
        last_updated = reader.read_f32()

        stage = "has_item"
        has_item = reader.read_u8() == 1
        item_summary = None
        if has_item:
            stage = "item"
            if capture:
                item_summary = parse_inventory_item_summary(
                    reader,
                    world_version,
                    dictionary,
                    strict=False,
                )
            else:
                _skip_inventory_item(reader)

        stage = "has_container"
        has_container = reader.read_u8() == 1
        container_summary = None
        if has_container:
            stage = "container"
            container_summary = parse_item_container_summary(
                reader, world_version, dictionary, max_top=3
            )

        stage = "has_mod_data"
        has_mod_data = reader.read_u8() == 1
        if has_mod_data:
            stage = "mod_data"
            skip_kahlua_table(reader, world_version, depth=0)

        stage = "has_device"
        has_device = reader.read_u8() == 1
        device_summary = None
        if has_device:
            stage = "device"
            if capture:
                device_summary = _read_device_data_summary(reader, world_version)
            else:
                _skip_device_data(reader, world_version)

        stage = "has_light"
        has_light = reader.read_u8() == 1
        light_summary = None
        if has_light:
            stage = "light"
            if capture:
                light_summary = _read_vehicle_light_summary(reader, world_version)
            else:
                _skip_vehicle_light(reader, world_version)

        stage = "has_door"
        has_door = reader.read_u8() == 1
        door_summary = None
        if has_door:
            stage = "door"
            if capture:
                door_summary = _read_vehicle_door_summary(reader)
            else:
                _skip_vehicle_door(reader)

        stage = "has_window"
        has_window = reader.read_u8() == 1
        window_summary = None
        if has_window:
            stage = "window"
            if capture:
                window_summary = _read_vehicle_window_summary(reader)
            else:
                _skip_vehicle_window(reader)

        condition = None
        wheel_friction = None
        mechanic_installer = None
        suspension_compression = None
        suspension_damping = None
        has_entity = None
        entity_info = None
        stage = "condition"
        if world_version >= 116:
            condition = reader.read_i32()
        stage = "wheel_friction"
        if world_version >= 118:
            wheel_friction = reader.read_f32()
            mechanic_installer = reader.read_i32()
        stage = "suspension"
        if world_version >= 119:
            suspension_compression = reader.read_f32()
            suspension_damping = reader.read_f32()
        stage = "has_entity"
        if world_version >= 200:
            has_entity = reader.read_u8() == 1
            if has_entity:
                stage = "entity"
                entity_info = _skip_vehicle_entity_data(reader)

        flags = {
            "with_item": has_item,
            "with_container": has_container,
            "with_device": has_device,
            "with_light": has_light,
            "with_door": has_door,
            "with_window": has_window,
            "with_entity": bool(has_entity),
        }
        summary = None
        if capture:
            summary = {
                "id": part_id,
                "part_name": archive_i18n.translate_vehicle_part(part_id),
                "created": created,
                "last_updated": last_updated,
                "has_item": has_item,
                "has_container": has_container,
                "has_device": has_device,
                "has_light": has_light,
                "has_door": has_door,
                "has_window": has_window,
            }
            if has_entity is not None:
                summary["has_entity"] = has_entity
            if entity_info:
                summary.update(entity_info)
            if condition is not None:
                summary["condition"] = condition
            if wheel_friction is not None:
                summary["wheel_friction"] = wheel_friction
            if mechanic_installer is not None:
                summary["mechanic_installer"] = mechanic_installer
            if suspension_compression is not None:
                summary["suspension_compression"] = suspension_compression
            if suspension_damping is not None:
                summary["suspension_damping"] = suspension_damping
            if item_summary:
                summary["item"] = item_summary
            if container_summary:
                summary["container"] = container_summary
            if device_summary:
                summary["device"] = device_summary
            if light_summary:
                summary["light"] = light_summary
            if door_summary:
                summary["door"] = door_summary
            if window_summary:
                summary["window"] = window_summary
        return flags, summary
    except Exception as exc:
        log_parse_exception(
            "vehicle_part_stage_failed",
            exc,
            source="vehicle_blob_parser",
            pos=reader.tell(),
            start=start,
            stage=stage,
            remaining=reader.remaining(),
            world_version=world_version,
        )
        raise


def _skip_inventory_item(reader: ByteBufferReader) -> None:
    size = reader.read_i32()
    if size <= 0:
        return
    payload_start = reader.tell()
    reader.read_u16()
    reader.read_i8()
    buffer_end = reader.tell() + reader.remaining()
    end = payload_start + size
    if end < reader.tell() or end > buffer_end:
        alt_end = reader.tell() + size
        if alt_end < reader.tell() or alt_end > buffer_end:
            raise ValueError("item payload out of bounds")
        end = alt_end
    reader.seek(end)


def _skip_device_data(reader: ByteBufferReader, world_version: int) -> None:
    if world_version < 69:
        return
    read_string(reader)
    reader.read_u8()
    reader.read_i32()
    reader.read_i32()
    reader.read_u8()
    reader.read_f32()
    reader.read_f32()
    reader.read_u8()
    reader.read_u8()
    reader.read_u8()
    reader.read_u8()
    reader.read_i32()
    reader.read_i32()
    reader.read_i32()
    reader.read_u8()
    reader.read_u8()
    reader.read_f32()
    reader.read_f32()
    reader.read_i32()
    has_presets = reader.read_u8() == 1
    if has_presets:
        _skip_device_presets(reader)
    if world_version >= 181:
        reader.read_i16()
        reader.read_u8()
        has_media_item = reader.read_u8() == 1
        if has_media_item:
            read_string(reader)
        reader.read_u8()


def _skip_device_presets(reader: ByteBufferReader) -> None:
    max_presets = reader.read_i32()
    entries = reader.read_i32()
    if max_presets < 0 or entries < 0:
        raise ValueError("device presets count invalid")
    for _ in range(entries):
        read_string(reader)
        reader.read_i32()


def _skip_vehicle_light(reader: ByteBufferReader, world_version: int) -> None:
    reader.read_u8()
    if _should_read_vehicle_light_extended(world_version):
        reader.read_f32()
        reader.read_f32()
        reader.read_f32()
        reader.read_f32()
        reader.read_i32()


def _skip_vehicle_door(reader: ByteBufferReader) -> None:
    reader.read_u8()
    reader.read_u8()
    reader.read_u8()


def _skip_vehicle_window(reader: ByteBufferReader) -> None:
    reader.read_u8()
    reader.read_u8()


def _read_vehicle_door_summary(reader: ByteBufferReader) -> Dict[str, object]:
    return {
        "open": reader.read_u8() == 1,
        "locked": reader.read_u8() == 1,
        "lock_broken": reader.read_u8() == 1,
    }


def _read_vehicle_window_summary(reader: ByteBufferReader) -> Dict[str, object]:
    return {
        "condition": reader.read_u8(),
        "open": reader.read_u8() == 1,
    }


def _read_vehicle_light_summary(
    reader: ByteBufferReader, world_version: int
) -> Dict[str, object]:
    summary = {"active": reader.read_u8() == 1}
    if _should_read_vehicle_light_extended(world_version):
        summary.update(
            {
                "offset_x": reader.read_f32(),
                "offset_y": reader.read_f32(),
                "intensity": reader.read_f32(),
                "distance": reader.read_f32(),
                "focusing": reader.read_i32(),
            }
        )
    return summary


def _should_read_vehicle_light_extended(world_version: int) -> bool:
    return world_version >= 135 or world_version <= 0


def _read_device_data_summary(
    reader: ByteBufferReader, world_version: int
) -> Dict[str, object]:
    if world_version < 69:
        return {}
    summary = {
        "name": read_string(reader),
        "two_way": reader.read_u8() == 1,
        "transmit_range": reader.read_i32(),
        "mic_range": reader.read_i32(),
        "mic_muted": reader.read_u8() == 1,
        "base_volume": reader.read_f32(),
        "device_volume": reader.read_f32(),
        "portable": reader.read_u8() == 1,
        "television": reader.read_u8() == 1,
        "high_tier": reader.read_u8() == 1,
        "is_on": reader.read_u8() == 1,
        "channel": reader.read_i32(),
        "min_channel": reader.read_i32(),
        "max_channel": reader.read_i32(),
        "battery_powered": reader.read_u8() == 1,
        "has_battery": reader.read_u8() == 1,
        "power_delta": reader.read_f32(),
        "use_delta": reader.read_f32(),
        "headphone_type": reader.read_i32(),
    }
    has_presets = reader.read_u8() == 1
    if has_presets:
        summary.update(_read_device_presets_summary(reader))
    if world_version >= 181:
        summary["media_index"] = reader.read_i16()
        summary["media_type"] = reader.read_u8()
        has_media_item = reader.read_u8() == 1
        summary["media_item"] = read_string(reader) if has_media_item else None
        summary["no_transmit"] = reader.read_u8() == 1
    return summary


def _read_device_presets_summary(reader: ByteBufferReader) -> Dict[str, object]:
    max_presets = reader.read_i32()
    entries = reader.read_i32()
    if max_presets < 0 or entries < 0:
        raise ValueError("device presets count invalid")
    sample = []
    for _ in range(entries):
        name = read_string(reader)
        freq = reader.read_i32()
        if len(sample) < 3:
            sample.append({"name": name, "freq": freq})
    return {"presets_max": max_presets, "presets_entries": entries, "presets_sample": sample}


def _read_blood_intensity(reader: ByteBufferReader) -> Dict[str, object]:
    count = reader.read_u8()
    items = []
    for _ in range(count):
        name = read_string_utf(reader)
        value = reader.read_u8()
        if len(items) < 6:
            items.append({"name": name, "value": value})
    return {"count": count, "items": items}


def _skip_vehicle_entity_data(reader: ByteBufferReader) -> Dict[str, object]:
    component_count = reader.read_i8()
    if component_count <= 0:
        return {"entity_component_count": 0, "entity_component_bytes": 0}
    total_bytes = 0
    for _ in range(component_count):
        block_length = reader.read_i32()
        if block_length < 2:
            raise ValueError("entity block length invalid")
        if block_length > reader.remaining():
            raise ValueError("entity block length exceeds buffer")
        reader.read_i16()
        remaining = block_length - 2
        if remaining:
            reader.skip(remaining)
        total_bytes += block_length
    return {
        "entity_component_count": component_count,
        "entity_component_bytes": total_bytes,
    }
