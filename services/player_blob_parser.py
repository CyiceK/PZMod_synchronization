"""
Player BLOB parser (read-only, best-effort).

支持翻译：
- 职业名称翻译
- 特性名称翻译
- 身体部位翻译
- 技能名称翻译
"""
from __future__ import annotations

import math
import struct
from typing import Dict, Optional, List, Tuple

from utils.bytebuffer_reader import ByteBufferReader
from utils.pz_string_codec import read_string, read_string_utf
from services.inventory_parser import parse_item_container_summary, set_offset_collector
from services.kahlua_skip import skip_kahlua_table
from services.parse_debug_log import log_parse_debug, log_parse_exception
from services.i18n_archive import archive_i18n


def parse_player_blob_summary(
    blob: object,
    world_version: Optional[int],
    *,
    include_inventory: bool = False,
    include_inventory_details: bool = False,
    dictionary: Optional[Dict[int, str]] = None,
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
        _cache_key = _cache.compute_player_key(
            data,
            int(world_version or 0),
            include_inventory,
            include_inventory_details,
        )
        cached = _cache.get_player_summary(_cache_key)
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
        offset_x = reader.read_f32()
        offset_y = reader.read_f32()
        x_val = reader.read_f32()
        y_val = reader.read_f32()
        z_val = reader.read_f32()
        direction = reader.read_i32()
        has_table = reader.read_u8()
        if has_table:
            skip_kahlua_table(reader, version, depth=0)
        summary: Dict[str, object] = {
            "serialize_flag": serialize_flag,
            "class_id": class_id,
            "offset_x": offset_x,
            "offset_y": offset_y,
            "x": x_val,
            "y": y_val,
            "z": z_val,
            "dir": direction,
            "has_table": bool(has_table),
        }
        if include_inventory:
            try:
                descriptor = None
                inventory_stage = "descriptor_flag"
                inventory_stage_start = reader.tell()
                if reader.read_u8() == 1:
                    inventory_stage = "descriptor"
                    inventory_stage_start = reader.tell()
                    descriptor = _read_survivor_desc(reader, version)
                inventory_stage = "human_visual"
                inventory_stage_start = reader.tell()
                _skip_human_visual(reader, version)
                inventory_stage = "inventory"
                inventory_stage_start = reader.tell()
                inventory = parse_item_container_summary(
                    reader,
                    version,
                    dictionary,
                    include_item_details=include_inventory_details,
                    detail_limit=0 if include_inventory_details else 200,
                )
                summary["inventory"] = inventory
                if descriptor:
                    summary["descriptor"] = descriptor
                status_stage = "status_flags"
                status_stage_start = reader.tell()
                try:
                    asleep = reader.read_u8() == 1
                    force_wakeup = reader.read_f32()
                    summary["status"] = {
                        "asleep": asleep,
                        "force_wakeup": force_wakeup,
                    }
                    try:
                        status_stage = "stats"
                        status_stage_start = reader.tell()
                        stats = _read_stats(reader, version)
                        summary["stats"] = stats
                        if version >= 200:
                            from_pos = reader.tell()
                            resync_pos = _find_body_damage_start_b42(
                                reader,
                                version,
                                max_scan=65536,
                            )
                            strategy = "strict"
                            if resync_pos is None:
                                resync_pos = _resync_body_part_prefix_b42(
                                    reader,
                                    max_scan=65536,
                                )
                                strategy = "scan"
                            if resync_pos is not None and resync_pos != from_pos:
                                log_parse_debug(
                                    "player_stats_resync_body_damage",
                                    source="player_blob_parser",
                                    from_pos=from_pos,
                                    to_pos=resync_pos,
                                    world_version=version,
                                    strategy=strategy,
                                )
                                reader.seek(resync_pos)
                    except Exception as exc:
                        summary["stats_error"] = True
                        log_parse_exception(
                            "player_stats_parse_failed",
                            exc,
                            source="player_blob_parser",
                            pos=reader.tell(),
                            start=status_stage_start,
                            remaining=reader.remaining(),
                            stage="stats",
                            world_version=version,
                        )
                    body_damage_ok = False
                    try:
                        status_stage = "body_damage"
                        status_stage_start = reader.tell()
                        body_damage = _read_body_damage_summary(reader, version)
                        summary["body_damage"] = body_damage
                        body_damage_ok = True
                    except Exception as exc:
                        summary["body_damage_error"] = True
                        log_parse_exception(
                            "player_body_damage_parse_failed",
                            exc,
                            source="player_blob_parser",
                            pos=reader.tell(),
                            start=status_stage_start,
                            remaining=reader.remaining(),
                            stage="body_damage",
                            world_version=version,
                        )
                    xp_ok = False
                    if body_damage_ok:
                        try:
                            status_stage = "xp"
                            status_stage_start = reader.tell()
                            xp_summary = _read_xp_summary(reader, version)
                            summary["xp"] = xp_summary
                            xp_ok = True
                        except Exception as exc:
                            summary["xp_error"] = True
                            log_parse_exception(
                                "player_xp_parse_failed",
                                exc,
                                source="player_blob_parser",
                                pos=reader.tell(),
                                start=status_stage_start,
                                remaining=reader.remaining(),
                                stage="xp",
                                world_version=version,
                            )
                    if xp_ok:
                        post_xp_failed = False
                        try:
                            status_stage = "post_xp"
                            status_stage_start = reader.tell()
                            post_summary = _read_post_xp_summary(reader, version)
                            summary["post_xp"] = post_summary
                        except Exception as exc:
                            post_xp_failed = True
                            summary["post_xp_error"] = True
                            log_parse_exception(
                                "player_post_xp_parse_failed",
                                exc,
                                source="player_blob_parser",
                                pos=reader.tell(),
                                start=status_stage_start,
                                remaining=reader.remaining(),
                                stage="post_xp",
                                world_version=version,
                            )
                        if post_xp_failed:
                            summary["player_extra_skipped"] = True
                        else:
                            if post_summary.get("invalid"):
                                summary["player_extra_skipped"] = True
                            else:
                                try:
                                    status_stage = "player_extra"
                                    status_stage_start = reader.tell()
                                    player_extra = _read_iso_player_summary(
                                        reader, version, dictionary
                                    )
                                    if include_inventory_details:
                                        _attach_worn_item_details(
                                            player_extra, inventory
                                        )
                                    summary["player_extra"] = player_extra
                                except Exception as exc:
                                    summary["player_extra_error"] = True
                                    log_parse_exception(
                                        "player_extra_parse_failed",
                                        exc,
                                        source="player_blob_parser",
                                        pos=reader.tell(),
                                        start=status_stage_start,
                                        remaining=reader.remaining(),
                                        stage="player_extra",
                                        world_version=version,
                                    )
                except Exception as exc:
                    summary["status_error"] = True
                    log_parse_exception(
                        "player_status_parse_failed",
                        exc,
                        source="player_blob_parser",
                        pos=reader.tell(),
                        start=status_stage_start,
                        remaining=reader.remaining(),
                        stage=status_stage,
                        world_version=version,
                    )
            except Exception as exc:
                summary["inventory_error"] = True
                log_parse_exception(
                    "player_inventory_parse_failed",
                    exc,
                    source="player_blob_parser",
                    pos=reader.tell(),
                    start=inventory_stage_start,
                    remaining=reader.remaining(),
                    stage=inventory_stage,
                    world_version=version,
                )
        # --- Phase 3: Save to BLOB cache ---
        if _cache is not None and _cache_key is not None and summary:
            try:
                _cache.set_player_summary(_cache_key, summary, version)
            except Exception:
                pass
        # --- End Phase 3 cache save ---
        return summary
    except Exception as exc:
        log_parse_exception(
            "player_blob_parse_failed",
            exc,
            source="player_blob_parser",
            pos=reader.tell(),
            remaining=reader.remaining(),
            world_version=version,
        )
        return {}


def scan_player_registry_offsets(
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
        reader.read_f32()
        reader.read_f32()
        reader.read_f32()
        reader.read_f32()
        reader.read_f32()
        reader.read_i32()
        has_table = reader.read_u8()
        if has_table:
            skip_kahlua_table(reader, version, depth=0)
        try:
            if reader.read_u8() == 1:
                _read_survivor_desc(reader, version)
        except Exception:
            return offsets
        try:
            _skip_human_visual(reader, version)
        except Exception:
            return offsets
        try:
            parse_item_container_summary(
                reader,
                version,
                dictionary,
                include_item_details=False,
                include_item_counts=False,
                detail_limit=0,
            )
        except Exception:
            pass
        return offsets
    finally:
        set_offset_collector(None)


def _read_survivor_desc(reader: ByteBufferReader, world_version: int) -> Dict[str, object]:
    desc_id = reader.read_i32()
    forename = read_string(reader)
    surname = read_string(reader)
    torso = read_string(reader)
    is_female = reader.read_i32() == 1
    profession = read_string(reader)
    extra_flag = reader.read_i32()
    if extra_flag == 1:
        extra_count = reader.read_i32()
        for _ in range(max(0, extra_count)):
            read_string(reader)
    perk_count = reader.read_i32()
    for _ in range(max(0, perk_count)):
        _skip_perk(reader, world_version)
        reader.read_i32()
    voice_prefix = None
    voice_pitch = None
    voice_type = None
    if world_version >= 208:
        voice_prefix = read_string(reader)
        voice_pitch = reader.read_f32()
        voice_type = reader.read_i32()
    summary = {
        "id": desc_id,
        "forename": forename,
        "surname": surname,
        "torso": torso,
        "profession": profession,
        "profession_translated": archive_i18n.translate_trait(profession) if profession else None,
        "is_female": is_female,
    }
    if voice_prefix is not None:
        summary["voice_prefix"] = voice_prefix
        summary["voice_pitch"] = voice_pitch
        summary["voice_type"] = voice_type
    return summary


def _skip_perk(reader: ByteBufferReader, world_version: int) -> None:
    _read_perk_name(reader, world_version)


def _read_perk_name(reader: ByteBufferReader, world_version: int) -> str:
    if world_version >= 152:
        return _read_string_safe(reader)
    return f"id:{reader.read_i32()}"


def _read_perk_name_bounded(
    reader: ByteBufferReader,
    world_version: int,
    *,
    min_after: int,
) -> str:
    if world_version < 152:
        if reader.remaining() < 4 + min_after:
            raise ValueError("perk id exceeds buffer")
        return f"id:{reader.read_i32()}"
    if reader.remaining() < 2 + min_after:
        raise ValueError("perk name exceeds buffer")
    pos = reader.tell()
    length = reader.read_i16()
    if length <= 0:
        reader.seek(pos)
        raise ValueError("perk name length invalid")
    remaining = reader.remaining()
    if length > 32767 or length > remaining - min_after:
        reader.seek(pos)
        raise ValueError("perk name exceeds buffer")
    data = reader.read_bytes(length)
    return data.decode("utf-8", errors="replace")


def _read_string_safe(reader: ByteBufferReader, *, max_len: int = 32767) -> str:
    if reader.remaining() < 2:
        return ""
    length = reader.read_i16()
    if length <= 0:
        return ""
    remaining = reader.remaining()
    if length > remaining or length > max_len:
        return ""
    data = reader.read_bytes(length)
    return data.decode("utf-8", errors="replace")


def _skip_human_visual(reader: ByteBufferReader, world_version: int) -> None:
    flags = reader.read_u8()
    if flags & 4:
        reader.skip(3)
    if flags & 2:
        reader.skip(3)
    if flags & 8:
        reader.skip(3)
    reader.read_u8()
    reader.read_u8()
    if world_version >= 156:
        reader.read_u8()
    if flags & 64:
        read_string(reader)
    if flags & 16:
        read_string(reader)
    if flags & 32:
        read_string(reader)
    blood_count = reader.read_i8()
    if blood_count > 0:
        reader.skip(blood_count)
    if world_version >= 163:
        dirt_count = reader.read_i8()
        if dirt_count > 0:
            reader.skip(dirt_count)
    holes_count = reader.read_i8()
    if holes_count > 0:
        reader.skip(holes_count)
    visuals_count = reader.read_i8()
    if visuals_count > 0:
        for _ in range(visuals_count):
            _skip_item_visual(reader, world_version)
    read_string(reader)
    if world_version >= 187:
        extra_flags = reader.read_u8()
        if extra_flags & 4:
            reader.skip(3)
        if extra_flags & 2:
            reader.skip(3)


def _skip_item_visual(reader: ByteBufferReader, world_version: int) -> None:
    flags = reader.read_u8()
    if world_version >= 164:
        read_string(reader)
        read_string(reader)
    read_string(reader)
    if flags & 1:
        reader.skip(3)
    if flags & 2:
        reader.read_u8()
    if flags & 4:
        reader.read_u8()
    if world_version >= 146:
        if flags & 8:
            reader.read_f32()
        if flags & 16:
            read_string(reader)
    blood_count = reader.read_i8()
    if blood_count > 0:
        reader.skip(blood_count)
    if world_version >= 163:
        dirt_count = reader.read_i8()
        if dirt_count > 0:
            reader.skip(dirt_count)
    holes_count = reader.read_i8()
    if holes_count > 0:
        reader.skip(holes_count)
    if world_version >= 154:
        basic_count = reader.read_i8()
        if basic_count > 0:
            reader.skip(basic_count)
    if world_version >= 155:
        denim_count = reader.read_i8()
        if denim_count > 0:
            reader.skip(denim_count)
    leather_count = reader.read_i8()
    if leather_count > 0:
        reader.skip(leather_count)


def _read_stats(reader: ByteBufferReader, world_version: int) -> Dict[str, float]:
    stats = {
        "anger": reader.read_f32(),
        "boredom": reader.read_f32(),
        "endurance": reader.read_f32(),
        "fatigue": reader.read_f32(),
        "fitness": reader.read_f32(),
        "hunger": reader.read_f32(),
        "morale": reader.read_f32(),
        "stress": reader.read_f32(),
        "fear": reader.read_f32(),
        "panic": reader.read_f32(),
        "sanity": reader.read_f32(),
        "sickness": reader.read_f32(),
        "boredom_level": reader.read_f32(),
        "pain": reader.read_f32(),
        "drunkenness": reader.read_f32(),
        "thirst": reader.read_f32(),
    }
    if world_version >= 97:
        stats["stress_from_cigarettes"] = reader.read_f32()
    return stats


def _skip_body_damage(reader: ByteBufferReader, world_version: int) -> None:
    _read_body_damage_summary(reader, world_version)


def _read_body_damage_summary(
    reader: ByteBufferReader, world_version: int
) -> Dict[str, object]:
    if world_version >= 200:
        return _read_body_damage_summary_b42(reader, world_version)
    return _read_body_damage_summary_legacy(reader, world_version)


def _health_all_zero(parts: list[dict[str, object]]) -> bool:
    if not parts:
        return False
    for part in parts:
        value = part.get("health")
        if not isinstance(value, (int, float)):
            return False
        if abs(float(value)) > 1.0e-6:
            return False
    return True


def _read_body_damage_summary_legacy(
    reader: ByteBufferReader, world_version: int, *, _allow_resync: bool = True
) -> Dict[str, object]:
    start = reader.tell()
    part_count = 17
    counts = {
        "bitten": 0,
        "scratched": 0,
        "bandaged": 0,
        "bleeding": 0,
        "deep_wounded": 0,
        "infected": 0,
        "fake_infected": 0,
    }
    parts: list[dict[str, object]] = []
    total_health = 0.0
    min_health: Optional[float] = None
    has_nonzero_health = False
    invalid_health = False
    
    # 身体部位 ID 列表（按顺序）
    body_part_ids = [
        "Head", "Neck", "Torso_Upper", "Torso_Lower",
        "UpperArm_L", "UpperArm_R", "ForeArm_L", "ForeArm_R",
        "Hand_L", "Hand_R", "Groin",
        "UpperLeg_L", "UpperLeg_R", "LowerLeg_L", "LowerLeg_R",
        "Foot_L", "Foot_R"
    ]
    
    for index in range(part_count):
        part = _read_body_part_prefix(reader, world_version)
        part_id = body_part_ids[index] if index < len(body_part_ids) else f"Unknown_{index}"
        parts.append({
            "index": index,
            "part_id": part_id,
            "part_name": archive_i18n.translate_body_part(part_id),
            **part
        })
        if part["bitten"]:
            counts["bitten"] += 1
        if part["scratched"]:
            counts["scratched"] += 1
        if part["bandaged"]:
            counts["bandaged"] += 1
        if part["bleeding"]:
            counts["bleeding"] += 1
        if part["deep_wounded"]:
            counts["deep_wounded"] += 1
        if part["infected"]:
            counts["infected"] += 1
        if part["fake_infected"]:
            counts["fake_infected"] += 1
        health = float(part["health"])
        if health < 0.0 or health > 100.0:
            invalid_health = True
        if health > 0.0:
            has_nonzero_health = True
        total_health += health
        if min_health is None or health < min_health:
            min_health = health
        _skip_body_part_tail(reader, world_version, part["bandaged"])
    infection_level = reader.read_f32()
    fake_infection_level = reader.read_f32()
    wetness = reader.read_f32()
    catch_cold = reader.read_f32()
    has_cold = reader.read_u8() == 1
    cold_strength = reader.read_f32()
    unhappyness = reader.read_f32()
    boredom = reader.read_f32()
    food_sickness = reader.read_f32()
    poison_level = reader.read_f32()
    temperature = reader.read_f32()
    reduce_fake_infection = reader.read_u8() == 1
    health_from_food_timer = reader.read_f32()
    pain_reduction = reader.read_f32()
    cold_reduction = reader.read_f32()
    infection_time = reader.read_f32()
    infection_mortality = reader.read_f32()
    cold_damage_stage = reader.read_f32()
    if world_version >= 153:
        has_thermo = reader.read_u8() == 1
        if has_thermo:
            _skip_thermoregulator(reader)
    avg_health = total_health / float(part_count)
    health_suspect = invalid_health or (not has_nonzero_health and sum(counts.values()) == 0)
    if health_suspect:
        avg_health = None
        min_health = None
    summary = {
        **counts,
        "parts": parts,
        "avg_health": avg_health,
        "min_health": min_health,
        "health_suspect": health_suspect,
        "infection_level": infection_level,
        "fake_infection_level": fake_infection_level,
        "wetness": wetness,
        "catch_cold": catch_cold,
        "has_cold": has_cold,
        "cold_strength": cold_strength,
        "unhappyness": unhappyness,
        "boredom": boredom,
        "food_sickness": food_sickness,
        "poison_level": poison_level,
        "temperature": temperature,
        "reduce_fake_infection": reduce_fake_infection,
        "health_from_food_timer": health_from_food_timer,
        "pain_reduction": pain_reduction,
        "cold_reduction": cold_reduction,
        "infection_time": infection_time,
        "infection_mortality": infection_mortality,
        "cold_damage_stage": cold_damage_stage,
    }
    if _allow_resync and _health_all_zero(parts):
        end_pos = reader.tell()
        reader.seek(start + 1)
        resync_pos = _resync_body_part_prefix_legacy(reader, max_scan=65536)
        if resync_pos is None or resync_pos == start:
            reader.seek(end_pos)
            return summary
        log_parse_debug(
            "body_damage_resync_legacy",
            source="player_blob_parser",
            from_pos=start,
            to_pos=resync_pos,
            world_version=world_version,
        )
        reader.seek(resync_pos)
        return _read_body_damage_summary_legacy(
            reader, world_version, _allow_resync=False
        )
    return summary


def _read_body_damage_summary_b42(
    reader: ByteBufferReader, world_version: int
) -> Dict[str, object]:
    def _try_parse(pos: int, part_count: int) -> Optional[Dict[str, object]]:
        reader.seek(pos)
        try:
            return _read_body_damage_summary_b42_impl(
                reader, world_version, part_count=part_count
            )
        except Exception:
            return None

    def _parse_from_pos(pos: int) -> Dict[str, object]:
        summary_17 = _try_parse(pos, 17)
        if summary_17:
            return summary_17
        summary_18 = _try_parse(pos, 18)
        if summary_18:
            log_parse_debug(
                "body_damage_part_count_fallback",
                source="player_blob_parser",
                pos=pos,
                world_version=world_version,
                part_count=18,
            )
            return summary_18
        reader.seek(pos)
        return _read_body_damage_summary_b42_impl(
            reader, world_version, part_count=17
        )

    start = reader.tell()
    summary = _parse_from_pos(start)
    suspect_health = _health_all_zero(summary.get("parts") or []) or bool(
        summary.get("invalid_health")
    )
    if suspect_health:
        end_pos = reader.tell()
        reader.seek(start + 1)
        resync_pos = _find_body_damage_start_b42(
            reader, world_version, max_scan=65536
        )
        if resync_pos is None or resync_pos == start:
            reader.seek(end_pos)
            return summary
        log_parse_debug(
            "body_damage_resync_b42",
            source="player_blob_parser",
            from_pos=start,
            to_pos=resync_pos,
            world_version=world_version,
        )
        summary = _parse_from_pos(resync_pos)
    return summary


def _read_body_damage_summary_b42_impl(
    reader: ByteBufferReader,
    world_version: int,
    *,
    part_count: int,
) -> Dict[str, object]:
    counts = {
        "bitten": 0,
        "scratched": 0,
        "bandaged": 0,
        "bleeding": 0,
        "deep_wounded": 0,
        "infected": 0,
        "fake_infected": 0,
    }
    parts: list[dict[str, object]] = []
    total_health = 0.0
    min_health: Optional[float] = None
    has_nonzero_health = False
    invalid_health = False
    for index in range(part_count):
        part = _read_body_part_prefix(reader, world_version)
        parts.append({"index": index, **part})
        if part["bitten"]:
            counts["bitten"] += 1
        if part["scratched"]:
            counts["scratched"] += 1
        if part["bandaged"]:
            counts["bandaged"] += 1
        if part["bleeding"]:
            counts["bleeding"] += 1
        if part["deep_wounded"]:
            counts["deep_wounded"] += 1
        if part["infected"]:
            counts["infected"] += 1
        if part["fake_infected"]:
            counts["fake_infected"] += 1
        health = float(part["health"])
        if health < 0.0 or health > 100.0:
            invalid_health = True
        if health > 0.0:
            has_nonzero_health = True
        total_health += health
        if min_health is None or health < min_health:
            min_health = health
        try:
            _skip_body_part_tail_b42(
                reader, world_version, part["bandaged"], part_index=index
            )
        except Exception as exc:
            log_parse_exception(
                "body_part_tail_b42_parse_failed",
                exc,
                source="player_blob_parser",
                pos=reader.tell(),
                world_version=world_version,
                part_index=index,
            )
            if index < part_count - 1:
                resync_pos = _resync_body_part_prefix_b42(reader, max_scan=65536)
                if resync_pos is not None:
                    log_parse_debug(
                        "body_part_tail_b42_resync_to_prefix",
                        source="player_blob_parser",
                        from_pos=reader.tell(),
                        to_pos=resync_pos,
                        world_version=world_version,
                        part_index=index,
                    )
                    reader.seek(resync_pos)
                    continue
            resync_pos = _find_body_damage_main_fields_b42(
                reader, world_version, max_scan=65536
            )
            if resync_pos is not None:
                log_parse_debug(
                    "body_part_tail_b42_resync_main_fields",
                    source="player_blob_parser",
                    from_pos=reader.tell(),
                    to_pos=resync_pos,
                    world_version=world_version,
                    part_index=index,
                )
                reader.seek(resync_pos)
                break
            raise

    catch_cold = reader.read_f32()
    has_cold = reader.read_u8() == 1
    cold_strength = reader.read_f32()
    time_to_sneeze = None
    if world_version >= 222:
        time_to_sneeze = float(reader.read_i32())
    reduce_fake_infection = reader.read_u8() == 1
    health_from_food_timer = reader.read_f32()
    pain_reduction = reader.read_f32()
    cold_reduction = reader.read_f32()
    infection_time = reader.read_f32()
    infection_mortality = reader.read_f32()
    cold_damage_stage = reader.read_f32()
    has_thermo = reader.read_u8() == 1
    if has_thermo:
        _skip_thermoregulator(reader)
    avg_health = total_health / float(part_count)
    health_suspect = invalid_health or (not has_nonzero_health and sum(counts.values()) == 0)
    if health_suspect:
        avg_health = None
        min_health = None
    return {
        **counts,
        "parts": parts,
        "part_count": part_count,
        "avg_health": avg_health,
        "min_health": min_health,
        "health_suspect": health_suspect,
        "invalid_health": invalid_health,
        "catch_cold": catch_cold,
        "has_cold": has_cold,
        "cold_strength": cold_strength,
        "time_to_sneeze_or_cough": time_to_sneeze,
        "reduce_fake_infection": reduce_fake_infection,
        "health_from_food_timer": health_from_food_timer,
        "pain_reduction": pain_reduction,
        "cold_reduction": cold_reduction,
        "infection_time": infection_time,
        "infection_mortality": infection_mortality,
        "cold_damage_stage": cold_damage_stage,
        "has_thermoregulator": has_thermo,
    }


def _read_body_part_prefix(
    reader: ByteBufferReader, world_version: int
) -> Dict[str, object]:
    if world_version >= 200:
        cut = reader.read_u8() == 1
        bitten = reader.read_u8() == 1
        scratched = reader.read_u8() == 1
        bandaged = reader.read_u8() == 1
        bleeding = reader.read_u8() == 1
        deep_wounded = reader.read_u8() == 1
        fake_infected = reader.read_u8() == 1
        infected = reader.read_u8() == 1
        health = reader.read_f32()
        return {
            "cut": cut,
            "bitten": bitten,
            "scratched": scratched,
            "bandaged": bandaged,
            "bleeding": bleeding,
            "deep_wounded": deep_wounded,
            "fake_infected": fake_infected,
            "infected": infected,
            "health": health,
        }

    bitten = reader.read_u8() == 1
    scratched = reader.read_u8() == 1
    bandaged = reader.read_u8() == 1
    bleeding = reader.read_u8() == 1
    deep_wounded = reader.read_u8() == 1
    fake_infected = reader.read_u8() == 1
    infected = reader.read_u8() == 1
    health = reader.read_f32()
    return {
        "cut": False,
        "bitten": bitten,
        "scratched": scratched,
        "bandaged": bandaged,
        "bleeding": bleeding,
        "deep_wounded": deep_wounded,
        "fake_infected": fake_infected,
        "infected": infected,
        "health": health,
    }


def _skip_body_part(reader: ByteBufferReader, world_version: int) -> None:
    part = _read_body_part_prefix(reader, world_version)
    _skip_body_part_tail(reader, world_version, part["bandaged"])


def _skip_body_part_tail(
    reader: ByteBufferReader, world_version: int, bandaged: bool
) -> None:
    if world_version >= 200:
        _skip_body_part_tail_b42(reader, world_version, bandaged)
        return
    if 37 <= world_version <= 43:
        reader.read_i32()
    if world_version < 44:
        return
    if bandaged:
        reader.read_f32()
    infected_wound = reader.read_u8() == 1
    if infected_wound:
        reader.read_f32()
    reader.read_f32()
    reader.read_f32()
    reader.read_f32()
    reader.read_f32()
    reader.read_f32()
    reader.read_f32()
    reader.read_u8()
    reader.read_u8()
    if world_version >= 48:
        reader.read_u8()
        reader.read_f32()
    reader.read_u8()
    reader.read_u8()
    reader.read_f32()
    splint = reader.read_u8() == 1
    if splint:
        reader.read_f32()
    reader.read_u8()
    reader.read_f32()
    reader.read_u8()
    reader.read_f32()
    read_string(reader)
    read_string(reader)
    reader.read_f32()
    if world_version >= 153:
        reader.read_f32()
    if world_version >= 167:
        reader.read_f32()


def _skip_body_part_tail_b42(
    reader: ByteBufferReader,
    world_version: int,
    bandaged: bool,
    *,
    part_index: Optional[int] = None,
) -> None:
    _skip_body_part_tail_b42_impl(
        reader,
        world_version,
        bandaged,
        part_index=part_index,
        gate_stitch_time=False,
        gate_splint_item=False,
        log_on_error=True,
    )


def _skip_body_part_tail_b42_impl(
    reader: ByteBufferReader,
    world_version: int,
    bandaged: bool,
    *,
    part_index: Optional[int],
    gate_stitch_time: bool,
    gate_splint_item: bool,
    log_on_error: bool,
) -> None:
    def _read_string_debug(label: str, *, allow_u16_fallback: bool = False) -> str:
        start = reader.tell()
        length_raw = reader.read_i16()
        length_u16 = length_raw if length_raw >= 0 else length_raw + 65536
        if length_raw <= 0:
            return ""
        remaining = reader.remaining()
        max_len = 32767
        if length_raw <= remaining and length_raw <= max_len:
            data = reader.read_bytes(length_raw)
            return data.decode("utf-8", errors="replace")
        for shift in (1, 2):
            candidate_pos = start + shift
            if candidate_pos + 2 > start + reader.remaining() + 2:
                continue
            reader.seek(candidate_pos)
            length = reader.read_i16()
            remaining = reader.remaining()
            if length <= 0 or length > remaining or length > max_len:
                continue
            if log_on_error:
                log_parse_debug(
                    "body_part_tail_b42_resync",
                    source="player_blob_parser",
                    label=label,
                    shift=shift,
                    length=length,
                    pos=reader.tell(),
                    world_version=world_version,
                    part_index=part_index,
                )
            data = reader.read_bytes(length)
            return data.decode("utf-8", errors="replace")
        reader.seek(start)
        raise ValueError(
            "string length exceeds buffer "
            f"label={label} len={length_raw} remaining={remaining} start={start}"
        )

    stage = "bandage_life"
    try:
        if bandaged:
            reader.read_f32()
        stage = "infected_wound_flag"
        infected_wound = reader.read_u8() == 1
        if infected_wound:
            stage = "wound_infection_level"
            reader.read_f32()
        stage = "cut_time"
        reader.read_f32()
        stage = "bite_time"
        reader.read_f32()
        stage = "scratch_time"
        reader.read_f32()
        stage = "bleeding_time"
        reader.read_f32()
        stage = "alcohol_level"
        reader.read_f32()
        stage = "additional_pain"
        reader.read_f32()
        stage = "deep_wound_time"
        reader.read_f32()
        stage = "have_glass"
        reader.read_u8()
        stage = "get_bandage_xp"
        reader.read_u8()
        stage = "stitched"
        stitched = reader.read_u8() == 1
        stage = "stitch_time"
        reader.read_f32()
        stage = "get_stitch_xp"
        reader.read_u8()
        stage = "get_splint_xp"
        reader.read_u8()
        stage = "fracture_time"
        reader.read_f32()
        stage = "splint_flag"
        splint = reader.read_u8() == 1
        if splint:
            stage = "splint_factor"
            reader.read_f32()
        stage = "have_bullet"
        reader.read_u8()
        stage = "burn_time"
        reader.read_f32()
        stage = "need_burn_wash"
        reader.read_u8()
        stage = "last_burn_wash"
        reader.read_f32()
        stage = "splint_item"
        _read_string_debug("splint_item")
        stage = "bandage_type"
        _read_string_debug("bandage_type")
        stage = "cut_time_2"
        reader.read_f32()
        stage = "wetness"
        reader.read_f32()
        stage = "stiffness"
        reader.read_f32()
        if world_version >= 227:
            stage = "comfrey_factor"
            reader.read_f32()
            stage = "garlic_factor"
            reader.read_f32()
            stage = "plantain_factor"
            reader.read_f32()
    except Exception as exc:
        if log_on_error:
            log_parse_exception(
                "body_part_tail_b42_failed",
                exc,
                source="player_blob_parser",
                pos=reader.tell(),
                world_version=world_version,
                part_index=part_index,
                stage=stage,
                stitch_time_gated=gate_stitch_time,
                bandaged=bandaged,
            )
        raise


def _looks_like_body_part_prefix_legacy_at(data: memoryview, pos: int) -> bool:
    if pos + 11 > len(data):
        return False
    bools = data[pos:pos + 7]
    if any(byte not in (0, 1) for byte in bools):
        return False
    health = struct.unpack_from(">f", data, pos + 7)[0]
    return 0.0 <= health <= 100.0


def _resync_body_part_prefix_legacy(
    reader: ByteBufferReader, *, max_scan: int
) -> Optional[int]:
    start = reader.tell()
    data = reader._data  # pylint: disable=protected-access
    end = min(len(data), start + max_scan)
    prefix_size = 11
    for pos in range(start, max(start, end - prefix_size + 1)):
        if _looks_like_body_part_prefix_legacy_at(data, pos):
            return pos
    return None


def _resync_body_part_prefix_b42(
    reader: ByteBufferReader, *, max_scan: int
) -> Optional[int]:
    start = reader.tell()
    data = reader._data  # pylint: disable=protected-access
    end = min(len(data), start + max_scan)
    prefix_size = 12
    for pos in range(start, max(start, end - prefix_size + 1)):
        if _looks_like_body_part_prefix_b42_at(data, pos):
            return pos
    return None


def _find_body_damage_start_b42(
    reader: ByteBufferReader,
    world_version: int,
    *,
    max_scan: int,
) -> Optional[int]:
    start = reader.tell()
    data = reader._data  # pylint: disable=protected-access
    end = min(len(data), start + max_scan)
    if start + 12 > len(data):
        return None
    blob = data.tobytes()
    for pos in range(start, max(start, end - 12 + 1)):
        if not _looks_like_body_part_prefix_b42_at(data, pos):
            continue
        for part_count in (17, 18):
            test_reader = ByteBufferReader(blob)
            test_reader.seek(pos)
            if _can_parse_body_damage_b42(
                test_reader, world_version, part_count=part_count
            ):
                return pos
    return None


def _looks_like_body_damage_main_fields_b42_at(
    data: memoryview, pos: int, world_version: int
) -> bool:
    min_size = 34
    if world_version >= 222:
        min_size += 4
    if pos + min_size > len(data):
        return False
    try:
        offset = pos
        catch_cold = struct.unpack_from(">f", data, offset)[0]
        if not math.isfinite(catch_cold) or abs(catch_cold) > 1.0e6:
            return False
        offset += 4
        has_cold = data[offset]
        if has_cold not in (0, 1):
            return False
        offset += 1
        cold_strength = struct.unpack_from(">f", data, offset)[0]
        if not math.isfinite(cold_strength) or abs(cold_strength) > 1.0e6:
            return False
        offset += 4
        if world_version >= 222:
            time_to_sneeze = struct.unpack_from(">i", data, offset)[0]
            if abs(time_to_sneeze) > 1_000_000_000:
                return False
            offset += 4
        reduce_fake = data[offset]
        if reduce_fake not in (0, 1):
            return False
        offset += 1
        for _ in range(6):
            value = struct.unpack_from(">f", data, offset)[0]
            if not math.isfinite(value) or abs(value) > 1.0e6:
                return False
            offset += 4
        if offset >= len(data):
            return False
        thermo_flag = data[offset]
        if thermo_flag not in (0, 1):
            return False
    except Exception:
        return False
    return True


def _find_body_damage_main_fields_b42(
    reader: ByteBufferReader,
    world_version: int,
    *,
    max_scan: int,
) -> Optional[int]:
    start = reader.tell()
    data = reader._data  # pylint: disable=protected-access
    end = min(len(data), start + max_scan)
    min_size = 34 + (4 if world_version >= 222 else 0)
    for pos in range(start, max(start, end - min_size + 1)):
        if _looks_like_body_damage_main_fields_b42_at(data, pos, world_version):
            return pos
    return None


def _can_parse_body_damage_b42(
    reader: ByteBufferReader, world_version: int, *, part_count: int
) -> bool:
    try:
        for _ in range(part_count):
            part = _read_body_part_prefix(reader, world_version)
            health = part.get("health")
            if isinstance(health, (int, float)):
                if health < 0.0 or health > 100.0:
                    return False
            _skip_body_part_tail_b42_impl(
                reader,
                world_version,
                part["bandaged"],
                part_index=None,
                gate_stitch_time=False,
                gate_splint_item=False,
                log_on_error=False,
            )
        data = reader._data  # pylint: disable=protected-access
        if not _looks_like_body_damage_main_fields_b42_at(
            data, reader.tell(), world_version
        ):
            return False
        reader.read_f32()
        reader.read_u8()
        reader.read_f32()
        if world_version >= 222:
            reader.read_i32()
        reader.read_u8()
        reader.read_f32()
        reader.read_f32()
        reader.read_f32()
        reader.read_f32()
        reader.read_f32()
        reader.read_f32()
        has_thermo = reader.read_u8() == 1
        if has_thermo:
            _skip_thermoregulator(reader)
    except Exception:
        return False
    return True


def _looks_like_body_part_prefix_b42_at(data: memoryview, pos: int) -> bool:
    if pos + 12 > len(data):
        return False
    bools = data[pos:pos + 8]
    if any(byte not in (0, 1) for byte in bools):
        return False
    health = struct.unpack_from(">f", data, pos + 8)[0]
    return 0.0 <= health <= 100.0


def _looks_like_body_part_prefix_b42(reader: ByteBufferReader) -> bool:
    pos = reader.tell()
    data = reader._data  # pylint: disable=protected-access
    return _looks_like_body_part_prefix_b42_at(data, pos)


def _iso_player_header_plausible(
    hours_survived: float,
    zombie_kills: int,
    worn_count: int,
) -> bool:
    if not math.isfinite(hours_survived):
        return False
    if hours_survived < 0.0 or hours_survived > 1.0e5:
        return False
    if zombie_kills < 0 or zombie_kills > 200_000:
        return False
    return 0 <= worn_count <= 40


def _resync_iso_player_start(
    reader: ByteBufferReader, world_version: int, *, max_scan: int
) -> Optional[int]:
    start = reader.tell()
    data = reader._data  # pylint: disable=protected-access
    end = min(len(data), start + max_scan)
    if end - start < 13:
        return None
    for pos in range(start, max(start, end - 12)):
        if _looks_like_iso_player_start(reader, pos, world_version):
            return pos
    for pos in range(start, max(start, end - 12)):
        if _looks_like_iso_player_start_loose(reader, pos, world_version):
            return pos
    return None


def _read_string_bounded(
    reader: ByteBufferReader, *, max_len: int, validate_utf8: bool = False
) -> bool:
    if reader.remaining() < 2:
        return False
    length = reader.read_i16()
    if length <= 0:
        return True
    if length > max_len or length > reader.remaining():
        return False
    data = reader.read_bytes(length)
    if validate_utf8:
        try:
            data.decode("utf-8")
        except UnicodeDecodeError:
            return False
    return True


def _find_next_iso_player_start(
    reader: ByteBufferReader,
    world_version: int,
    start_pos: int,
    *,
    max_scan: int,
) -> Optional[int]:
    data = reader._data  # pylint: disable=protected-access
    if start_pos < 0 or start_pos >= len(data):
        return None
    temp_reader = ByteBufferReader(data)
    temp_reader.seek(start_pos)
    return _resync_iso_player_start(temp_reader, world_version, max_scan=max_scan)


def _resync_iso_player_tag_prefix(
    reader: ByteBufferReader, world_version: int, *, max_scan: int
) -> Optional[int]:
    start = reader.tell()
    data = reader._data  # pylint: disable=protected-access
    end = min(len(data), start + max_scan)
    start_back = max(0, start - max_scan)
    if end - start_back < 2:
        return None
    for pos in range(start, max(start, end - 1)):
        if _looks_like_tag_prefix_block(reader, pos, world_version):
            return pos
    for pos in range(start - 1, start_back - 1, -1):
        if _looks_like_tag_prefix_block(reader, pos, world_version):
            return pos
    return None


def _looks_like_tag_prefix_block(
    reader: ByteBufferReader, pos: int, world_version: int
) -> bool:
    data = reader._data  # pylint: disable=protected-access
    if pos < 0 or pos + 2 > len(data):
        return False
    temp = ByteBufferReader(data)
    temp.seek(pos)
    try:
        if not _read_string_bounded(temp, max_len=4096):
            return False
        for _ in range(3):
            value = temp.read_f32()
            if not math.isfinite(value):
                return False
        if not _read_string_bounded(temp, max_len=4096):
            return False
        show_tag = temp.read_u8()
        if show_tag not in (0, 1):
            return False
        faction_pvp = temp.read_u8()
        if faction_pvp not in (0, 1):
            return False
        if world_version >= 239:
            auto_drink = temp.read_u8()
            if auto_drink not in (0, 1):
                return False
        if temp.remaining() < 1:
            return False
        temp.read_u8()
        if temp.remaining() < 1:
            return False
        has_vehicle = temp.read_u8()
        if has_vehicle not in (0, 1):
            return False
        if has_vehicle and temp.remaining() < 10:
            return False
        return True
    except Exception:
        return False


def _looks_like_iso_player_start(
    reader: ByteBufferReader, pos: int, world_version: int
) -> bool:
    data = reader._data  # pylint: disable=protected-access
    if pos < 0 or pos + 13 > len(data):
        return False
    temp = ByteBufferReader(data)
    temp.seek(pos)
    try:
        hours_survived = temp.read_f64()
        zombie_kills = temp.read_i32()
        worn_count = temp.read_u8()
        if not math.isfinite(hours_survived):
            return False
        if hours_survived < 0.0:
            return False
        if zombie_kills < 0:
            return False
        if worn_count > 120:
            return False
        for _ in range(worn_count):
            if not _read_string_bounded(
                temp, max_len=256, validate_utf8=True
            ):
                return False
            if temp.remaining() < 2:
                return False
            temp.read_i16()
        if temp.remaining() < 29:
            return False
        left_hand_index = temp.read_i16()
        right_hand_index = temp.read_i16()
        if left_hand_index < -1 or left_hand_index > 5000:
            return False
        if right_hand_index < -1 or right_hand_index > 5000:
            return False
        survivor_kills = temp.read_i32()
        if survivor_kills < 0 or survivor_kills > 200_000:
            return False
        nutrition = []
        for _ in range(5):
            value = temp.read_f32()
            if not math.isfinite(value):
                return False
            nutrition.append(value)
        weight = nutrition[4]
        if weight < 0.0 or weight > 500.0:
            return False
        all_chat_muted = temp.read_u8()
        if all_chat_muted not in (0, 1):
            return False
        if not _read_string_bounded(temp, max_len=256, validate_utf8=True):
            return False
        for _ in range(3):
            value = temp.read_f32()
            if not math.isfinite(value) or value < -0.1 or value > 255.1:
                return False
        if not _read_string_bounded(temp, max_len=256, validate_utf8=True):
            return False
        show_tag = temp.read_u8()
        if show_tag not in (0, 1):
            return False
        faction_pvp = temp.read_u8()
        if faction_pvp not in (0, 1):
            return False
        if world_version >= 239:
            auto_drink = temp.read_u8()
            if auto_drink not in (0, 1):
                return False
        if temp.remaining() < 1:
            return False
        temp.read_u8()
        if temp.remaining() < 1:
            return False
        has_vehicle = temp.read_u8()
        if has_vehicle not in (0, 1):
            return False
        if has_vehicle:
            if temp.remaining() < 10:
                return False
            temp.read_f32()
            temp.read_f32()
            temp.read_i8()
            running = temp.read_u8()
            if running not in (0, 1):
                return False
        if temp.remaining() < 4:
            return False
        mechanics_count = temp.read_i32()
        if mechanics_count < 0:
            return False
        remaining = temp.remaining()
        fitness_min = 0
        if world_version >= 169:
            fitness_min = 20
        elif world_version >= 167:
            fitness_min = 16
        tail_min = fitness_min
        if world_version >= 184:
            tail_min += 2
        if world_version >= 189:
            tail_min += 2
        tail_min += 1
        if remaining < mechanics_count * 16 + tail_min:
            return False
        if mechanics_count > remaining // 16:
            return False
        return True
    except Exception:
        return False


def _looks_like_iso_player_start_loose(
    reader: ByteBufferReader, pos: int, world_version: int
) -> bool:
    data = reader._data  # pylint: disable=protected-access
    if pos < 0 or pos + 13 > len(data):
        return False
    temp = ByteBufferReader(data)
    temp.seek(pos)
    try:
        hours_survived = temp.read_f64()
        zombie_kills = temp.read_i32()
        worn_count = temp.read_u8()
        if not _iso_player_header_plausible(
            hours_survived, zombie_kills, worn_count
        ):
            return False
        for _ in range(worn_count):
            if not _read_string_bounded(temp, max_len=4096):
                return False
            if temp.remaining() < 2:
                return False
            temp.read_i16()
        if temp.remaining() < 29:
            return False
        temp.read_i16()
        temp.read_i16()
        survivor_kills = temp.read_i32()
        if survivor_kills < 0 or survivor_kills > 1_000_000:
            return False
        for _ in range(5):
            value = temp.read_f32()
            if not math.isfinite(value):
                return False
        all_chat_muted = temp.read_u8()
        if all_chat_muted not in (0, 1):
            return False
        if not _read_string_bounded(temp, max_len=4096):
            return False
        for _ in range(3):
            value = temp.read_f32()
            if not math.isfinite(value):
                return False
        if not _read_string_bounded(temp, max_len=4096):
            return False
        show_tag = temp.read_u8()
        if show_tag not in (0, 1):
            return False
        faction_pvp = temp.read_u8()
        if faction_pvp not in (0, 1):
            return False
        if world_version >= 239:
            auto_drink = temp.read_u8()
            if auto_drink not in (0, 1):
                return False
        if temp.remaining() < 1:
            return False
        temp.read_u8()
        if temp.remaining() < 1:
            return False
        has_vehicle = temp.read_u8()
        if has_vehicle not in (0, 1):
            return False
        if has_vehicle:
            if temp.remaining() < 10:
                return False
            temp.read_f32()
            temp.read_f32()
            temp.read_i8()
            running = temp.read_u8()
            if running not in (0, 1):
                return False
        if temp.remaining() < 4:
            return False
        mechanics_count = temp.read_i32()
        if mechanics_count < 0:
            return False
        if mechanics_count > temp.remaining() // 16:
            return False
        return True
    except Exception:
        return False


def _skip_thermoregulator(reader: ByteBufferReader) -> None:
    reader.read_f32()
    reader.read_f32()
    reader.read_f32()
    reader.read_f32()
    reader.read_f32()
    reader.read_f32()
    reader.read_f32()
    count = reader.read_i32()
    if count < 0:
        raise ValueError("thermoregulator nodes invalid")
    for _ in range(count):
        reader.read_i32()
        reader.read_f32()
        reader.read_f32()
        reader.read_f32()
        reader.read_f32()
        reader.read_f32()


def _read_xp_summary(reader: ByteBufferReader, world_version: int) -> Dict[str, object]:
    start = reader.tell()
    try:
        summary_a = _read_xp_summary_impl(
            reader,
            world_version,
            traits_count_size="i32",
            xp_map_count_size="i32",
            perk_list_count_size="i32",
            multiplier_count_size="i32",
        )
        end_a = reader.tell()
    except Exception:
        reader.seek(start)
        summary_a = _read_xp_summary_impl(
            reader,
            world_version,
            traits_count_size="i16",
            xp_map_count_size="i32",
            perk_list_count_size="i32",
            multiplier_count_size="i32",
        )
        end_a = reader.tell()
        log_parse_debug(
            "xp_traits_count_fallback",
            source="player_blob_parser",
            pos=end_a,
            world_version=world_version,
        )
    summary = summary_a
    end_pos = end_a
    suspicious = _is_xp_summary_suspicious(summary_a)
    if suspicious:
        reader.seek(start)
        summary_b = _read_xp_summary_impl(
            reader,
            world_version,
            traits_count_size="i16",
            xp_map_count_size="u16",
            perk_list_count_size="u16",
            multiplier_count_size="u16",
        )
        end_b = reader.tell()
        score_a = _xp_summary_score(summary_a)
        score_b = _xp_summary_score(summary_b)
        if score_b > score_a:
            summary = summary_b
            end_pos = end_b
            log_parse_debug(
                "xp_counts_fallback",
                source="player_blob_parser",
                pos=end_b,
                world_version=world_version,
                score_a=score_a,
                score_b=score_b,
            )
        else:
            reader.seek(end_a)
    else:
        reader.seek(end_a)
    if suspicious or _xp_summary_implausible(summary):
        reader.seek(start)
        resync = _find_xp_payload_offset(reader, world_version, max_scan=4096)
        if resync is not None:
            reader.seek(resync["pos"])
            summary = _read_xp_payload_summary(
                reader,
                world_version,
                xp_map_count_size="i32",
                perk_list_count_size="i32",
                multiplier_count_size="i32",
                perk_name_reader=_read_perk_name_scan,
            )
            summary["traits"] = 0
            summary["traits_sample"] = []
            summary["traits_items"] = []
            summary["traits_skipped"] = True
            summary["traits_bytes"] = resync["offset"]
            end_pos = reader.tell()
            log_parse_debug(
                "xp_payload_resync",
                source="player_blob_parser",
                pos=end_pos,
                world_version=world_version,
                offset=resync["offset"],
                score=resync["score"],
            )
        else:
            reader.seek(end_pos)
    if _xp_summary_implausible(summary):
        _sanitize_xp_summary(summary)
    _log_xp_summary(reader, world_version, summary)
    return summary


def _read_xp_summary_impl(
    reader: ByteBufferReader,
    world_version: int,
    *,
    traits_count_size: str,
    xp_map_count_size: str,
    perk_list_count_size: str,
    multiplier_count_size: str,
) -> Dict[str, object]:
    stage = "traits"
    try:
        min_tail_bytes = 24 + (4 if world_version < 162 else 0)
        trait_count, trait_sample, trait_items = _read_character_traits_summary(
            reader,
            max_sample=6,
            count_size=traits_count_size,
            min_tail_bytes=min_tail_bytes,
        )
        payload = _read_xp_payload_summary(
            reader,
            world_version,
            xp_map_count_size=xp_map_count_size,
            perk_list_count_size=perk_list_count_size,
            multiplier_count_size=multiplier_count_size,
            perk_name_reader=_read_perk_name_bounded,
        )
        payload["traits"] = trait_count
        payload["traits_sample"] = trait_sample
        payload["traits_items"] = trait_items
        return payload
    except Exception as exc:
        raise ValueError(f"xp stage={stage}") from exc


_XP_PERK_SCAN_NAMES = {
    "Aiming",
    "Axe",
    "Blunt",
    "Blade",
    "Blacksmith",
    "Carpentry",
    "Carving",
    "Cooking",
    "Doctor",
    "Electricity",
    "Farming",
    "FirstAid",
    "Fishing",
    "Fitness",
    "Foraging",
    "Husbandry",
    "Lightfoot",
    "LongBlade",
    "LongBlunt",
    "Maintenance",
    "Mechanics",
    "MetalWelding",
    "Nimble",
    "Pottery",
    "Reloading",
    "ShortBlade",
    "ShortBlunt",
    "SmallBlade",
    "SmallBlunt",
    "Sneak",
    "Spear",
    "Sprinting",
    "Strength",
    "Tailoring",
    "Trapping",
    "Woodwork",
}


def _read_perk_name_scan(
    reader: ByteBufferReader, world_version: int, *, min_after: int
) -> str:
    del world_version
    length = reader.read_i16()
    if length <= 0:
        return ""
    remaining = reader.remaining()
    max_len = 64
    if length > max_len or length > remaining - min_after:
        raise ValueError("perk name length invalid")
    data = reader.read_bytes(length)
    name = data.decode("utf-8", errors="replace")
    if not name.isascii():
        raise ValueError("perk name non-ascii")
    if name and not name.replace("_", "").isalpha():
        raise ValueError("perk name invalid")
    return name


def _read_xp_payload_summary(
    reader: ByteBufferReader,
    world_version: int,
    *,
    xp_map_count_size: str,
    perk_list_count_size: str,
    multiplier_count_size: str,
    perk_name_reader,
) -> Dict[str, object]:
    stage = "total_xp"
    try:
        total_xp = reader.read_f32()
        stage = "level"
        level = reader.read_i32()
        stage = "last_level"
        last_level = reader.read_i32()
        stage = "xp_map_count"
        xp_map_count = _read_count(reader, xp_map_count_size)
        if xp_map_count < 0:
            xp_map_count = 0
        max_by_remaining = reader.remaining() // 6
        if xp_map_count > max_by_remaining:
            xp_map_count = max_by_remaining
        xp_map_items = []
        xp_map_sample = []
        xp_map_entries: list[dict[str, object]] = []
        xp_map_values: dict[str, float] = {}
        min_tail_after_map = 8 + (4 if world_version < 162 else 0)
        xp_map_actual = 0
        for _ in range(xp_map_count):
            stage = "xp_map_item"
            pos = reader.tell()
            try:
                perk_name = perk_name_reader(
                    reader,
                    world_version,
                    min_after=4 + min_tail_after_map,
                )
            except ValueError:
                reader.seek(pos)
                break
            if reader.remaining() < 4 + min_tail_after_map:
                reader.seek(pos)
                break
            stage = "xp_map_value"
            xp_value = reader.read_f32()
            xp_map_actual += 1
            if perk_name:
                xp_map_items.append(perk_name)
                if len(xp_map_sample) < 6:
                    xp_map_sample.append(perk_name)
                xp_map_entries.append({"name": perk_name, "xp": xp_value})
                prev = xp_map_values.get(perk_name)
                if prev is None or xp_value > prev:
                    xp_map_values[perk_name] = xp_value
        if xp_map_actual < xp_map_count:
            xp_map_count = xp_map_actual
        if world_version < 162:
            stage = "legacy_perk_count"
            legacy_count = reader.read_i32()
            if legacy_count < 0:
                legacy_count = 0
            max_by_remaining = reader.remaining() // 2
            if legacy_count > max_by_remaining:
                legacy_count = max_by_remaining
            for _ in range(legacy_count):
                stage = "legacy_perk_item"
                _skip_perk(reader, world_version)
        stage = "perk_list_count"
        perk_list_count = _read_count(reader, perk_list_count_size)
        if perk_list_count < 0:
            perk_list_count = 0
        max_by_remaining = reader.remaining() // 6
        if perk_list_count > max_by_remaining:
            perk_list_count = max_by_remaining
        perk_levels: list[dict[str, object]] = []
        perk_levels_sample: list[dict[str, object]] = []
        priority_names = [
            "Woodwork",
            "Electricity",
            "Mechanics",
            "Strength",
            "Fitness",
            "MetalWelding",
        ]
        priority_levels: dict[str, int] = {}
        min_tail_after_perk_list = 4
        perk_list_actual = 0
        for _ in range(perk_list_count):
            stage = "perk_list_item"
            pos = reader.tell()
            try:
                perk_name = perk_name_reader(
                    reader,
                    world_version,
                    min_after=4 + min_tail_after_perk_list,
                )
            except ValueError:
                reader.seek(pos)
                break
            if reader.remaining() < 4 + min_tail_after_perk_list:
                reader.seek(pos)
                break
            stage = "perk_list_level"
            perk_level = reader.read_i32()
            perk_list_actual += 1
            if perk_name:
                item = {"name": perk_name, "level": perk_level}
                perk_levels.append(item)
                if len(perk_levels_sample) < 12:
                    perk_levels_sample.append(item)
                if perk_name in priority_names:
                    priority_levels[perk_name] = perk_level
        if perk_list_actual < perk_list_count:
            perk_list_count = perk_list_actual
        perk_levels_priority = [
            {"name": name, "level": priority_levels[name]}
            for name in priority_names
            if name in priority_levels
        ]
        stage = "multiplier_count"
        multiplier_count = _read_count(reader, multiplier_count_size)
        if multiplier_count < 0:
            multiplier_count = 0
        max_by_remaining = reader.remaining() // 8
        if multiplier_count > max_by_remaining:
            multiplier_count = max_by_remaining
        min_tail_after_multiplier = 47
        multiplier_actual = 0
        for _ in range(multiplier_count):
            stage = "multiplier_item"
            pos = reader.tell()
            try:
                perk_name_reader(
                    reader,
                    world_version,
                    min_after=6 + min_tail_after_multiplier,
                )
            except ValueError:
                reader.seek(pos)
                break
            if reader.remaining() < 6 + min_tail_after_multiplier:
                reader.seek(pos)
                break
            stage = "multiplier_value"
            reader.read_f32()
            stage = "multiplier_min"
            reader.read_i8()
            stage = "multiplier_max"
            reader.read_i8()
            multiplier_actual += 1
        if multiplier_actual < multiplier_count:
            multiplier_count = multiplier_actual
        return {
            "total_xp": total_xp,
            "level": level,
            "last_level": last_level,
            "xp_map_count": xp_map_count,
            "xp_map_sample": xp_map_sample,
            "xp_map_items": xp_map_items,
            "xp_map_entries": xp_map_entries,
            "xp_map_values": xp_map_values,
            "perk_list_count": perk_list_count,
            "perk_levels": perk_levels,
            "perk_levels_sample": perk_levels_sample,
            "perk_levels_priority": perk_levels_priority,
            "multiplier_count": multiplier_count,
        }
    except Exception as exc:
        raise ValueError(f"xp payload stage={stage}") from exc


def _find_xp_payload_offset(
    reader: ByteBufferReader, world_version: int, *, max_scan: int
) -> Optional[dict[str, int]]:
    start = reader.tell()
    data = reader._data  # pylint: disable=protected-access
    end = min(len(data), start + max_scan)
    blob = data.tobytes()
    best_score = 0
    best_pos: Optional[int] = None
    for pos in range(start, max(start, end)):
        test_reader = ByteBufferReader(blob)
        test_reader.seek(pos)
        try:
            summary = _read_xp_payload_summary(
                test_reader,
                world_version,
                xp_map_count_size="i32",
                perk_list_count_size="i32",
                multiplier_count_size="i32",
                perk_name_reader=_read_perk_name_scan,
            )
        except Exception:
            continue
        score = _score_xp_perk_names(summary)
        if score > best_score:
            best_score = score
            best_pos = pos
    if best_pos is None or best_score < 2:
        return None
    return {"pos": best_pos, "offset": best_pos - start, "score": best_score}


def _read_count(reader: ByteBufferReader, size: str) -> int:
    if size == "i16":
        return reader.read_i16()
    if size == "u16":
        return reader.read_u16()
    return reader.read_i32()


def _is_xp_summary_suspicious(summary: Dict[str, object]) -> bool:
    traits = summary.get("traits")
    if isinstance(traits, int) and traits > 128:
        return True
    total_xp = summary.get("total_xp")
    if isinstance(total_xp, float) and not math.isfinite(total_xp):
        return True
    perks = summary.get("perk_list_count", 0)
    xp_map = summary.get("xp_map_count", 0)
    multipliers = summary.get("multiplier_count", 0)
    if perks == 0 and xp_map == 0 and multipliers == 0:
        if isinstance(total_xp, (int, float)) and total_xp > 0:
            return True
    return False


def _xp_summary_implausible(summary: Dict[str, object]) -> bool:
    level = summary.get("level")
    last_level = summary.get("last_level")
    if isinstance(level, int) and (level < 0 or level > 1000):
        return True
    if isinstance(last_level, int) and (last_level < 0 or last_level > 1000):
        return True
    total_xp = summary.get("total_xp")
    if isinstance(total_xp, (int, float)):
        total_xp_val = float(total_xp)
        if not math.isfinite(total_xp_val) or total_xp_val < 0.0 or total_xp_val > 1e8:
            return True
    return False


def _xp_summary_score(summary: Dict[str, object]) -> int:
    score = 0
    for key in ("perk_list_count", "xp_map_count", "multiplier_count"):
        value = summary.get(key)
        if isinstance(value, int) and value > 0:
            score += value
    traits = summary.get("traits")
    if isinstance(traits, int) and traits > 0:
        score += 1
    total_xp = summary.get("total_xp")
    if isinstance(total_xp, (int, float)) and total_xp > 0:
        score += 1
    return score


def _score_xp_perk_names(summary: Dict[str, object]) -> int:
    names: list[str] = []
    for key in ("perk_levels", "perk_levels_sample"):
        entries = summary.get(key) or []
        if isinstance(entries, list):
            for item in entries:
                if isinstance(item, dict):
                    name = item.get("name")
                    if isinstance(name, str):
                        names.append(name)
    xp_map_items = summary.get("xp_map_items") or summary.get("xp_map_sample") or []
    if isinstance(xp_map_items, list):
        for name in xp_map_items:
            if isinstance(name, str):
                names.append(name)
    score = 0
    for name in names:
        if name in _XP_PERK_SCAN_NAMES:
            score += 1
    return score


def _sanitize_xp_summary(summary: Dict[str, object]) -> None:
    total_xp = summary.get("total_xp")
    if (
        not isinstance(total_xp, (int, float))
        or not math.isfinite(float(total_xp))
        or float(total_xp) < 0.0
        or float(total_xp) > 1e8
    ):
        summary["total_xp"] = 0.0
    for key in ("level", "last_level"):
        value = summary.get(key)
        if not isinstance(value, int) or value < 0 or value > 1000:
            summary[key] = 0


def _log_xp_summary(
    reader: ByteBufferReader,
    world_version: int,
    summary: Dict[str, object],
) -> None:
    perk_sample = summary.get("perk_levels_sample") or []
    sample_names = []
    for item in perk_sample:
        name = item.get("name") if isinstance(item, dict) else None
        if name:
            sample_names.append(str(name))
        if len(sample_names) >= 8:
            break
    log_parse_debug(
        "xp_summary",
        source="player_blob_parser",
        pos=reader.tell(),
        world_version=world_version,
        total_xp=summary.get("total_xp"),
        level=summary.get("level"),
        last_level=summary.get("last_level"),
        traits=summary.get("traits"),
        xp_map=summary.get("xp_map_count"),
        perks=summary.get("perk_list_count"),
        multipliers=summary.get("multiplier_count"),
        perk_sample=",".join(sample_names),
    )


def _read_character_traits_summary(
    reader: ByteBufferReader,
    *,
    max_sample: int,
    count_size: str = "i32",
    min_tail_bytes: int = 24,
    collect_items: bool = True,
) -> tuple[int, list[str], list[str]]:
    if count_size == "i16":
        count = reader.read_i16()
    else:
        count = reader.read_i32()
    if count < 0:
        count = 0
    sample: list[str] = []
    items: list[str] = []
    actual = 0
    max_len = 32767
    for _ in range(count):
        if reader.remaining() < min_tail_bytes + 2:
            break
        pos = reader.tell()
        length = reader.read_i16()
        if length <= 0:
            reader.seek(pos)
            break
        remaining = reader.remaining()
        if length > max_len or length > remaining - min_tail_bytes:
            reader.seek(pos)
            break
        data = reader.read_bytes(length)
        name = data.decode("utf-8", errors="replace")
        actual += 1
        if collect_items:
            items.append(name)
        if len(sample) < max_sample:
            sample.append(name)
    return actual, sample, items


def _skip_character_traits(reader: ByteBufferReader) -> int:
    count, _, _ = _read_character_traits_summary(reader, max_sample=0, collect_items=False)
    return count


def _is_post_xp_summary_plausible(summary: Dict[str, object]) -> bool:
    left_hand = summary.get("left_hand")
    if isinstance(left_hand, int) and (left_hand < -1 or left_hand > 10000):
        return False
    right_hand = summary.get("right_hand")
    if isinstance(right_hand, int) and (right_hand < -1 or right_hand > 10000):
        return False
    on_fire = summary.get("on_fire")
    if on_fire is not None and not isinstance(on_fire, bool):
        return False
    for key in (
        "depress_effect",
        "depress_first",
        "beta_effect",
        "beta_delta",
        "pain_effect",
        "pain_delta",
        "sleep_effect",
        "sleep_delta",
        "reduce_infection_power",
        "time_since_last_smoke",
        "beard_grow",
        "hair_grow",
    ):
        value = summary.get(key)
        if value is None:
            continue
        if not isinstance(value, (int, float)):
            return False
        if not math.isfinite(float(value)) or abs(float(value)) > 1.0e6:
            return False
    return True


def _read_post_xp_summary(
    reader: ByteBufferReader, world_version: int
) -> Dict[str, object]:
    start = reader.tell()
    if not _looks_like_post_xp_start(reader, start, world_version):
        resync_pos = _resync_post_xp_start(reader, world_version, max_scan=4096)
        if resync_pos is not None and resync_pos != start:
            log_parse_debug(
                "post_xp_resync_start",
                source="player_blob_parser",
                from_pos=start,
                to_pos=resync_pos,
                world_version=world_version,
            )
            reader.seek(resync_pos)
            start = resync_pos
    try:
        summary = _read_post_xp_summary_impl(
            reader, world_version, read_books_count_size="i32"
        )
        _resync_iso_player_after_post_xp(reader, world_version, start)
        if not _is_post_xp_summary_plausible(summary):
            summary["invalid"] = True
        return summary
    except Exception:
        reader.seek(start)
        try:
            summary = _read_post_xp_summary_impl(
                reader, world_version, read_books_count_size="i16"
            )
            _resync_iso_player_after_post_xp(reader, world_version, start)
            log_parse_debug(
                "post_xp_read_books_count_fallback",
                source="player_blob_parser",
                pos=reader.tell(),
                world_version=world_version,
            )
            if not _is_post_xp_summary_plausible(summary):
                summary["invalid"] = True
            return summary
        except Exception as exc:
            reader.seek(start)
            summary = _read_post_xp_summary_loose(reader, world_version)
            _resync_iso_player_after_post_xp(reader, world_version, start)
            log_parse_debug(
                "post_xp_loose_fallback",
                source="player_blob_parser",
                pos=reader.tell(),
                world_version=world_version,
                error=repr(exc),
            )
            if not _is_post_xp_summary_plausible(summary):
                summary["invalid"] = True
            return summary


def _read_post_xp_summary_impl(
    reader: ByteBufferReader,
    world_version: int,
    *,
    read_books_count_size: str,
) -> Dict[str, object]:
    stage = "left_hand"
    try:
        log_parse_debug(
            "post_xp_start",
            source="player_blob_parser",
            pos=reader.tell(),
            world_version=world_version,
        )
        def _read_count_i32(label: str, min_item_size: int) -> int:
            count = reader.read_i32()
            if count < 0:
                count = 0
            max_by_remaining = 0
            if min_item_size > 0:
                max_by_remaining = reader.remaining() // min_item_size
            if count > max_by_remaining:
                count = max_by_remaining
            return count

        left_hand = reader.read_i32()
        stage = "right_hand"
        right_hand = reader.read_i32()
        stage = "on_fire"
        on_fire = reader.read_u8() == 1
        stage = "depress_effect"
        depress_effect = reader.read_f32()
        stage = "depress_first"
        depress_first = reader.read_f32()
        stage = "beta_effect"
        beta_effect = reader.read_f32()
        stage = "beta_delta"
        beta_delta = reader.read_f32()
        stage = "pain_effect"
        pain_effect = reader.read_f32()
        stage = "pain_delta"
        pain_delta = reader.read_f32()
        stage = "sleep_effect"
        sleep_effect = reader.read_f32()
        stage = "sleep_delta"
        sleep_delta = reader.read_f32()
        stage = "read_books_count"
        if read_books_count_size == "i16":
            read_books_count = reader.read_i16()
            if read_books_count < 0:
                read_books_count = 0
            max_by_remaining = reader.remaining() // 6
            if read_books_count > max_by_remaining:
                read_books_count = max_by_remaining
        else:
            read_books_count = _read_count_i32("read_books", 6)
            if read_books_count > 0:
                peek_pos = reader.tell()
                peek_len = None
                if reader.remaining() >= 2:
                    peek_len = reader.read_i16()
                    reader.seek(peek_pos)
                max_len = 4096
                if (
                    peek_len is None
                    or peek_len <= 0
                    or peek_len > reader.remaining()
                    or peek_len > max_len
                ):
                    raise ValueError("read_books_count size mismatch")
        read_books_read = 0
        for _ in range(read_books_count):
            stage = "read_books_item"
            item_pos = reader.tell()
            try:
                read_string(reader)
                stage = "read_books_pages"
                reader.read_i32()
            except Exception:
                reader.seek(item_pos)
                log_parse_debug(
                    "post_xp_read_books_truncated",
                    source="player_blob_parser",
                    read=read_books_read,
                    expected=read_books_count,
                    pos=reader.tell(),
                    world_version=world_version,
                )
                break
            read_books_read += 1
        read_books_count = read_books_read
        log_parse_debug(
            "post_xp_read_books",
            source="player_blob_parser",
            count=read_books_count,
            pos=reader.tell(),
            world_version=world_version,
        )
        stage = "reduce_infection_power"
        reduce_infection_power = reader.read_f32()
        log_parse_debug(
            "post_xp_reduce_infection_power",
            source="player_blob_parser",
            value=reduce_infection_power,
            pos=reader.tell(),
            world_version=world_version,
        )
        stage = "recipe_count"
        recipe_count_value = _read_count_i32("known_recipes", 2)
        recipe_count = recipe_count_value
        last_hour_sleeped: Optional[int] = None
        if recipe_count > 0:
            peek_pos = reader.tell()
            peek_len = None
            if reader.remaining() >= 2:
                peek_len = reader.read_i16()
                reader.seek(peek_pos)
            max_len = 4096
            if (
                peek_len is None
                or peek_len <= 0
                or peek_len > reader.remaining()
                or peek_len > max_len
            ):
                recipe_count = 0
                last_hour_sleeped = recipe_count_value
            else:
                recipe_read = 0
                for _ in range(recipe_count):
                    stage = "recipe_item"
                    item_pos = reader.tell()
                    try:
                        read_string(reader)
                    except Exception:
                        reader.seek(item_pos)
                        log_parse_debug(
                            "post_xp_recipes_truncated",
                            source="player_blob_parser",
                            read=recipe_read,
                            expected=recipe_count,
                            pos=reader.tell(),
                            world_version=world_version,
                        )
                        break
                    recipe_read += 1
                recipe_count = recipe_read
        log_parse_debug(
            "post_xp_recipes",
            source="player_blob_parser",
            count=recipe_count,
            pos=reader.tell(),
            world_version=world_version,
        )
        stage = "last_hour_sleeped"
        if last_hour_sleeped is None:
            last_hour_sleeped = reader.read_i32()
        log_parse_debug(
            "post_xp_last_hour_sleeped",
            source="player_blob_parser",
            value=last_hour_sleeped,
            pos=reader.tell(),
            world_version=world_version,
        )
        stage = "time_since_last_smoke"
        time_since_last_smoke = reader.read_f32()
        stage = "beard_grow"
        beard_grow = reader.read_f32()
        stage = "hair_grow"
        hair_grow = reader.read_f32()
        stage = "unlimited_carry"
        unlimited_carry = reader.read_u8() == 1
        stage = "build_cheat"
        build_cheat = reader.read_u8() == 1
        stage = "health_cheat"
        health_cheat = reader.read_u8() == 1
        stage = "mechanics_cheat"
        mechanics_cheat = reader.read_u8() == 1
        movables_cheat = None
        farming_cheat = None
        fishing_cheat = None
        can_use_brush_tool = None
        fast_move_cheat = None
        timed_action_instant = None
        unlimited_endurance = None
        if world_version >= 176:
            stage = "movables_cheat"
            movables_cheat = reader.read_u8() == 1
            stage = "farming_cheat"
            farming_cheat = reader.read_u8() == 1
            if world_version >= 202:
                stage = "fishing_cheat"
                fishing_cheat = reader.read_u8() == 1
            if world_version >= 217:
                stage = "can_use_brush_tool"
                can_use_brush_tool = reader.read_u8() == 1
                stage = "fast_move_cheat"
                fast_move_cheat = reader.read_u8() == 1
            stage = "timed_action_instant"
            timed_action_instant = reader.read_u8() == 1
            stage = "unlimited_endurance"
            unlimited_endurance = reader.read_u8() == 1
        unlimited_ammo = None
        know_all_recipes = None
        if world_version >= 230:
            stage = "unlimited_ammo"
            unlimited_ammo = reader.read_u8() == 1
            stage = "know_all_recipes"
            know_all_recipes = reader.read_u8() == 1
        sneaking = None
        death_drag_down = None
        if world_version >= 161:
            stage = "sneaking"
            sneaking = reader.read_u8() == 1
            stage = "death_drag_down"
            death_drag_down = reader.read_u8() == 1
        tail_pos = reader.tell()
        if (
            _looks_like_iso_player_start(reader, tail_pos, world_version)
            or _looks_like_iso_player_start_loose(reader, tail_pos, world_version)
        ):
            return {
                "left_hand": left_hand,
                "right_hand": right_hand,
                "on_fire": on_fire,
                "depress_effect": depress_effect,
                "depress_first": depress_first,
                "beta_effect": beta_effect,
                "beta_delta": beta_delta,
                "pain_effect": pain_effect,
                "pain_delta": pain_delta,
                "sleep_effect": sleep_effect,
                "sleep_delta": sleep_delta,
                "read_books": read_books_count,
                "reduce_infection_power": reduce_infection_power,
                "known_recipes": recipe_count,
                "last_hour_sleeped": last_hour_sleeped,
                "time_since_last_smoke": time_since_last_smoke,
                "beard_grow": beard_grow,
                "hair_grow": hair_grow,
                "unlimited_carry": unlimited_carry,
                "build_cheat": build_cheat,
                "health_cheat": health_cheat,
                "mechanics_cheat": mechanics_cheat,
                "movables_cheat": movables_cheat,
                "farming_cheat": farming_cheat,
                "fishing_cheat": fishing_cheat,
                "can_use_brush_tool": can_use_brush_tool,
                "fast_move_cheat": fast_move_cheat,
                "timed_action_instant": timed_action_instant,
                "unlimited_endurance": unlimited_endurance,
                "unlimited_ammo": unlimited_ammo,
                "know_all_recipes": know_all_recipes,
                "sneaking": sneaking,
                "death_drag_down": death_drag_down,
                "read_literature": 0,
                "read_print_media": 0,
                "last_animal_pet": 0,
            }
        stage = "read_literature_count"
        read_literature_count = reader.read_i32()
        if read_literature_count < 0:
            read_literature_count = 0
        if read_literature_count > 0:
            peek_pos = reader.tell()
            peek_len = None
            if reader.remaining() >= 2:
                peek_len = reader.read_i16()
                reader.seek(peek_pos)
            max_len = 4096
            if (
                peek_len is None
                or peek_len <= 0
                or peek_len > reader.remaining()
                or peek_len > max_len
            ):
                read_literature_count = 0
        log_parse_debug(
            "post_xp_read_literature",
            source="player_blob_parser",
            count=read_literature_count,
            pos=reader.tell(),
            world_version=world_version,
        )
        read_literature_read = 0
        for _ in range(read_literature_count):
            stage = "read_literature_item"
            item_pos = reader.tell()
            try:
                read_string(reader)
                stage = "read_literature_pages"
                reader.read_i32()
            except Exception:
                reader.seek(item_pos)
                log_parse_debug(
                    "post_xp_read_literature_truncated",
                    source="player_blob_parser",
                    read=read_literature_read,
                    expected=read_literature_count,
                    pos=reader.tell(),
                    world_version=world_version,
                )
                break
            read_literature_read += 1
        read_literature_count = read_literature_read
        read_print_media_count = 0
        if world_version >= 222:
            stage = "read_print_media_count"
            read_print_media_count = reader.read_i32()
            if read_print_media_count < 0:
                read_print_media_count = 0
            if read_print_media_count > 0:
                peek_pos = reader.tell()
                peek_len = None
                if reader.remaining() >= 2:
                    peek_len = reader.read_i16()
                    reader.seek(peek_pos)
                max_len = 4096
                if (
                    peek_len is None
                    or peek_len <= 0
                    or peek_len > reader.remaining()
                    or peek_len > max_len
                ):
                    read_print_media_count = 0
            log_parse_debug(
                "post_xp_read_print_media",
                source="player_blob_parser",
                count=read_print_media_count,
                pos=reader.tell(),
                world_version=world_version,
            )
            read_print_media_read = 0
            for _ in range(read_print_media_count):
                stage = "read_print_media_item"
                item_pos = reader.tell()
                try:
                    read_string(reader)
                except Exception:
                    reader.seek(item_pos)
                    log_parse_debug(
                        "post_xp_read_print_media_truncated",
                        source="player_blob_parser",
                        read=read_print_media_read,
                        expected=read_print_media_count,
                        pos=reader.tell(),
                        world_version=world_version,
                    )
                    break
                read_print_media_read += 1
            read_print_media_count = read_print_media_read
        stage = "last_animal_pet"
        last_animal_pet = reader.read_i64()
        log_parse_debug(
            "post_xp_last_animal_pet",
            source="player_blob_parser",
            value=last_animal_pet,
            pos=reader.tell(),
            world_version=world_version,
        )
        log_parse_debug(
            "post_xp_end",
            source="player_blob_parser",
            pos=reader.tell(),
            world_version=world_version,
        )
        return {
            "left_hand": left_hand,
            "right_hand": right_hand,
            "on_fire": on_fire,
            "depress_effect": depress_effect,
            "depress_first": depress_first,
            "beta_effect": beta_effect,
            "beta_delta": beta_delta,
            "pain_effect": pain_effect,
            "pain_delta": pain_delta,
            "sleep_effect": sleep_effect,
            "sleep_delta": sleep_delta,
            "read_books": read_books_count,
            "reduce_infection_power": reduce_infection_power,
            "known_recipes": recipe_count,
            "last_hour_sleeped": last_hour_sleeped,
            "time_since_last_smoke": time_since_last_smoke,
            "beard_grow": beard_grow,
            "hair_grow": hair_grow,
            "unlimited_carry": unlimited_carry,
            "build_cheat": build_cheat,
            "health_cheat": health_cheat,
            "mechanics_cheat": mechanics_cheat,
            "movables_cheat": movables_cheat,
            "farming_cheat": farming_cheat,
            "fishing_cheat": fishing_cheat,
            "can_use_brush_tool": can_use_brush_tool,
            "fast_move_cheat": fast_move_cheat,
            "timed_action_instant": timed_action_instant,
            "unlimited_endurance": unlimited_endurance,
            "unlimited_ammo": unlimited_ammo,
            "know_all_recipes": know_all_recipes,
            "sneaking": sneaking,
            "death_drag_down": death_drag_down,
            "read_literature": read_literature_count,
            "read_print_media": read_print_media_count,
            "last_animal_pet": last_animal_pet,
        }
    except Exception as exc:
        raise ValueError(f"post_xp stage={stage}") from exc


def _read_post_xp_summary_loose(
    reader: ByteBufferReader, world_version: int
) -> Dict[str, object]:
    summary: Dict[str, object] = {"partial": True}

    def _safe_read_i32() -> Optional[int]:
        if reader.remaining() < 4:
            return None
        try:
            return reader.read_i32()
        except Exception:
            return None

    def _safe_read_i16() -> Optional[int]:
        if reader.remaining() < 2:
            return None
        try:
            return reader.read_i16()
        except Exception:
            return None

    def _safe_read_i64() -> Optional[int]:
        if reader.remaining() < 8:
            return None
        try:
            return reader.read_i64()
        except Exception:
            return None

    def _safe_read_u8() -> Optional[int]:
        if reader.remaining() < 1:
            return None
        try:
            return reader.read_u8()
        except Exception:
            return None

    def _safe_read_f32() -> Optional[float]:
        if reader.remaining() < 4:
            return None
        try:
            return reader.read_f32()
        except Exception:
            return None

    def _peek_count(count_size: str, min_item_size: int) -> Optional[int]:
        pos = reader.tell()
        if count_size == "i32":
            count = _safe_read_i32()
        else:
            count = _safe_read_i16()
        if count is None:
            reader.seek(pos)
            return None
        if count < 0:
            count = 0
        max_by_remaining = 0
        if min_item_size > 0:
            max_by_remaining = reader.remaining() // min_item_size
        if count > max_by_remaining:
            count = max_by_remaining
        if count > 0:
            length = _safe_read_i16()
            max_len = 4096
            if (
                length is None
                or length <= 0
                or length > max_len
                or length > reader.remaining()
            ):
                reader.seek(pos)
                return None
        reader.seek(pos)
        return count

    def _read_count(count_size: str, min_item_size: int) -> int:
        if count_size == "i32":
            count = _safe_read_i32()
        else:
            count = _safe_read_i16()
        if count is None:
            return 0
        if count < 0:
            count = 0
        max_by_remaining = 0
        if min_item_size > 0:
            max_by_remaining = reader.remaining() // min_item_size
        if count > max_by_remaining:
            count = max_by_remaining
        return count

    def _skip_string_list(count: int, *, with_pages: bool) -> int:
        read_count = 0
        for _ in range(count):
            if not _read_string_bounded(reader, max_len=4096):
                break
            if with_pages:
                if reader.remaining() < 4:
                    break
                _ = _safe_read_i32()
                if _ is None:
                    break
            read_count += 1
        return read_count

    left_hand = _safe_read_i32()
    if left_hand is None:
        return summary
    right_hand = _safe_read_i32()
    if right_hand is None:
        return summary
    summary["left_hand"] = left_hand
    summary["right_hand"] = right_hand

    on_fire = _safe_read_u8()
    if on_fire is None:
        return summary
    summary["on_fire"] = on_fire == 1

    float_keys = [
        "depress_effect",
        "depress_first",
        "beta_effect",
        "beta_delta",
        "pain_effect",
        "pain_delta",
        "sleep_effect",
        "sleep_delta",
    ]
    for key in float_keys:
        value = _safe_read_f32()
        if value is None:
            return summary
        summary[key] = value

    count_i32 = _peek_count("i32", 6)
    count_i16 = _peek_count("i16", 6)
    if world_version >= 200:
        count_size = "i32" if count_i32 is not None else "i16"
    else:
        count_size = "i16" if count_i16 is not None else "i32"
    if count_size:
        read_books_count = _read_count(count_size, 6)
        read_books_count = _skip_string_list(read_books_count, with_pages=True)
    else:
        read_books_count = 0
    summary["read_books"] = read_books_count

    reduce_infection_power = _safe_read_f32()
    if reduce_infection_power is None:
        return summary
    summary["reduce_infection_power"] = reduce_infection_power

    recipe_count_value = _safe_read_i32()
    if recipe_count_value is None:
        return summary
    if recipe_count_value < 0:
        recipe_count_value = 0
    recipe_count = recipe_count_value
    last_hour_sleeped: Optional[int] = None
    if recipe_count > 0:
        peek_pos = reader.tell()
        peek_len = _safe_read_i16()
        reader.seek(peek_pos)
        max_len = 4096
        if (
            peek_len is None
            or peek_len <= 0
            or peek_len > max_len
            or peek_len > reader.remaining()
        ):
            recipe_count = 0
            last_hour_sleeped = recipe_count_value
    if recipe_count > 0:
        recipe_count = _skip_string_list(recipe_count, with_pages=False)
    summary["known_recipes"] = recipe_count

    if last_hour_sleeped is None:
        last_hour_sleeped = _safe_read_i32()
        if last_hour_sleeped is None:
            return summary
    summary["last_hour_sleeped"] = last_hour_sleeped

    time_since_last_smoke = _safe_read_f32()
    if time_since_last_smoke is None:
        return summary
    summary["time_since_last_smoke"] = time_since_last_smoke

    beard_grow = _safe_read_f32()
    if beard_grow is None:
        return summary
    summary["beard_grow"] = beard_grow

    hair_grow = _safe_read_f32()
    if hair_grow is None:
        return summary
    summary["hair_grow"] = hair_grow

    def _read_bool() -> Optional[bool]:
        value = _safe_read_u8()
        if value is None:
            return None
        return value == 1

    summary["unlimited_carry"] = _read_bool()
    summary["build_cheat"] = _read_bool()
    summary["health_cheat"] = _read_bool()
    summary["mechanics_cheat"] = _read_bool()

    if world_version >= 176:
        summary["movables_cheat"] = _read_bool()
        summary["farming_cheat"] = _read_bool()
        if world_version >= 202:
            summary["fishing_cheat"] = _read_bool()
        if world_version >= 217:
            summary["can_use_brush_tool"] = _read_bool()
            summary["fast_move_cheat"] = _read_bool()
        summary["timed_action_instant"] = _read_bool()
        summary["unlimited_endurance"] = _read_bool()
    if world_version >= 230:
        summary["unlimited_ammo"] = _read_bool()
        summary["know_all_recipes"] = _read_bool()
    if world_version >= 161:
        summary["sneaking"] = _read_bool()
        summary["death_drag_down"] = _read_bool()
    tail_pos = reader.tell()
    if (
        _looks_like_iso_player_start(reader, tail_pos, world_version)
        or _looks_like_iso_player_start_loose(reader, tail_pos, world_version)
    ):
        summary["read_literature"] = 0
        summary["read_print_media"] = 0
        summary["last_animal_pet"] = 0
        return summary

    read_literature_count = _safe_read_i32()
    if read_literature_count is None:
        return summary
    if read_literature_count < 0:
        read_literature_count = 0
    if read_literature_count > 0:
        peek_pos = reader.tell()
        peek_len = _safe_read_i16()
        reader.seek(peek_pos)
        max_len = 4096
        if (
            peek_len is None
            or peek_len <= 0
            or peek_len > max_len
            or peek_len > reader.remaining()
        ):
            read_literature_count = 0
    read_literature_count = _skip_string_list(read_literature_count, with_pages=True)
    summary["read_literature"] = read_literature_count

    read_print_media_count = 0
    if world_version >= 222:
        rpm_count = _safe_read_i32()
        if rpm_count is None:
            return summary
        if rpm_count < 0:
            rpm_count = 0
        if rpm_count > 0:
            peek_pos = reader.tell()
            peek_len = _safe_read_i16()
            reader.seek(peek_pos)
            max_len = 4096
            if (
                peek_len is None
                or peek_len <= 0
                or peek_len > max_len
                or peek_len > reader.remaining()
            ):
                rpm_count = 0
        read_print_media_count = _skip_string_list(rpm_count, with_pages=False)
    summary["read_print_media"] = read_print_media_count

    last_animal_pet = _safe_read_i64()
    if last_animal_pet is None:
        return summary
    summary["last_animal_pet"] = last_animal_pet

    return summary


def _resync_iso_player_after_post_xp(
    reader: ByteBufferReader, world_version: int, start_pos: int
) -> None:
    temp_reader = ByteBufferReader(reader._data)  # pylint: disable=protected-access
    temp_reader.seek(start_pos)
    resync_pos = _resync_iso_player_start(temp_reader, world_version, max_scan=65536)
    if resync_pos is None:
        return
    if resync_pos != reader.tell():
        log_parse_debug(
            "post_xp_resync_iso_player",
            source="player_blob_parser",
            from_pos=reader.tell(),
            to_pos=resync_pos,
            world_version=world_version,
        )
    reader.seek(resync_pos)


def _looks_like_post_xp_start(
    reader: ByteBufferReader, pos: int, world_version: int
) -> bool:
    data = reader._data  # pylint: disable=protected-access
    if pos < 0 or pos + 43 > len(data):
        return False
    temp = ByteBufferReader(data)
    temp.seek(pos)
    try:
        left_hand = temp.read_i32()
        right_hand = temp.read_i32()
        if left_hand < -1 or left_hand > 10000:
            return False
        if right_hand < -1 or right_hand > 10000:
            return False
        on_fire = temp.read_u8()
        if on_fire not in (0, 1):
            return False
        for _ in range(8):
            value = temp.read_f32()
            if not math.isfinite(value) or abs(value) > 1.0e6:
                return False

        def _peek_books_count(count_size: str) -> bool:
            peek_pos = temp.tell()
            if count_size == "i32":
                if temp.remaining() < 4:
                    temp.seek(peek_pos)
                    return False
                count = temp.read_i32()
            else:
                if temp.remaining() < 2:
                    temp.seek(peek_pos)
                    return False
                count = temp.read_i16()
            if count < 0:
                count = 0
            if count > 0:
                if temp.remaining() < 2:
                    temp.seek(peek_pos)
                    return False
                length = temp.read_i16()
                max_len = 4096
                if (
                    length <= 0
                    or length > max_len
                    or length > temp.remaining()
                ):
                    temp.seek(peek_pos)
                    return False
            temp.seek(peek_pos)
            return True

        ok = False
        if world_version >= 200:
            ok = _peek_books_count("i32") or _peek_books_count("i16")
        else:
            ok = _peek_books_count("i16") or _peek_books_count("i32")
        return ok
    except Exception:
        return False


def _looks_like_post_xp_start_loose(
    reader: ByteBufferReader, pos: int, world_version: int
) -> bool:
    data = reader._data  # pylint: disable=protected-access
    if pos < 0 or pos + 25 > len(data):
        return False
    temp = ByteBufferReader(data)
    temp.seek(pos)
    try:
        left_hand = temp.read_i32()
        right_hand = temp.read_i32()
        if left_hand < -1 or left_hand > 10000:
            return False
        if right_hand < -1 or right_hand > 10000:
            return False
        on_fire = temp.read_u8()
        if on_fire not in (0, 1):
            return False
        for _ in range(4):
            value = temp.read_f32()
            if not math.isfinite(value) or abs(value) > 1.0e6:
                return False

        def _peek_books_count(count_size: str) -> bool:
            peek_pos = temp.tell()
            if count_size == "i32":
                if temp.remaining() < 4:
                    temp.seek(peek_pos)
                    return False
                count = temp.read_i32()
            else:
                if temp.remaining() < 2:
                    temp.seek(peek_pos)
                    return False
                count = temp.read_i16()
            if count < 0:
                count = 0
            if count > 0:
                if temp.remaining() < 2:
                    temp.seek(peek_pos)
                    return False
                length = temp.read_i16()
                max_len = 4096
                if (
                    length <= 0
                    or length > max_len
                    or length > temp.remaining()
                ):
                    temp.seek(peek_pos)
                    return False
            temp.seek(peek_pos)
            return True

        ok = False
        if world_version >= 200:
            ok = _peek_books_count("i32") or _peek_books_count("i16")
        else:
            ok = _peek_books_count("i16") or _peek_books_count("i32")
        return ok
    except Exception:
        return False


def _resync_post_xp_start(
    reader: ByteBufferReader, world_version: int, *, max_scan: int
) -> Optional[int]:
    start = reader.tell()
    data = reader._data  # pylint: disable=protected-access
    end = min(len(data), start + max_scan)
    if end - start < 25:
        return None
    if end - start >= 43:
        for pos in range(start, max(start, end - 42)):
            if _looks_like_post_xp_start(reader, pos, world_version):
                return pos
    for pos in range(start, max(start, end - 24)):
        if _looks_like_post_xp_start_loose(reader, pos, world_version):
            return pos
    return None


def _read_iso_player_summary(
    reader: ByteBufferReader,
    world_version: int,
    dictionary: Optional[Dict[int, str]],
    _allow_retry: bool = True,
) -> Dict[str, object]:
    stage = "hours_survived"
    try:
        log_parse_debug(
            "iso_player_start",
            source="player_blob_parser",
            pos=reader.tell(),
            world_version=world_version,
        )
        start = reader.tell()
        hours_survived = reader.read_f64()
        stage = "zombie_kills"
        zombie_kills = reader.read_i32()
        stage = "worn_count"
        worn_count = reader.read_u8()
        if not _iso_player_header_plausible(hours_survived, zombie_kills, worn_count):
            reader.seek(start + 1)
            resync_pos = _resync_iso_player_start(
                reader, world_version, max_scan=2048
            )
            if resync_pos is None:
                reader.seek(start + 1)
                resync_pos = _resync_iso_player_start(
                    reader, world_version, max_scan=65536
                )
            if resync_pos is None:
                raise ValueError("iso_player header resync failed")
            log_parse_debug(
                "iso_player_resync",
                source="player_blob_parser",
                from_pos=start,
                to_pos=resync_pos,
                world_version=world_version,
            )
            reader.seek(resync_pos)
            start = resync_pos
            stage = "hours_survived"
            hours_survived = reader.read_f64()
            stage = "zombie_kills"
            zombie_kills = reader.read_i32()
            stage = "worn_count"
            worn_count = reader.read_u8()
            if not _iso_player_header_plausible(
                hours_survived, zombie_kills, worn_count
            ):
                raise ValueError("iso_player header still implausible after resync")
        log_parse_debug(
            "iso_player_hours_survived",
            source="player_blob_parser",
            value=hours_survived,
            pos=reader.tell(),
            world_version=world_version,
        )
        log_parse_debug(
            "iso_player_zombie_kills",
            source="player_blob_parser",
            value=zombie_kills,
            pos=reader.tell(),
            world_version=world_version,
        )
        log_parse_debug(
            "iso_player_worn_count",
            source="player_blob_parser",
            value=worn_count,
            pos=reader.tell(),
            world_version=world_version,
        )
        worn_items_start = reader.tell()
        worn_items: list[dict[str, object]] = []
        try:
            for _ in range(worn_count):
                stage = "worn_item_name"
                if world_version >= 228:
                    # B42: ResourceLocation格式 (namespace:path)
                    namespace = read_string(reader)
                    path = read_string(reader)
                    item_name = f"{namespace}:{path}"
                else:
                    # B41: 直接字符串格式
                    item_name = read_string(reader)
                stage = "worn_item_id"
                item_id = reader.read_i16()
                worn_items.append(
                    {
                        "name": item_name,
                        "id": item_id,
                    }
                )
        except Exception:
            worn_count = 0
            worn_items = []
            reader.seek(worn_items_start)
            log_parse_debug(
                "iso_player_worn_fallback",
                source="player_blob_parser",
                pos=reader.tell(),
                world_version=world_version,
            )
        stage = "left_hand_index"
        left_hand_index = reader.read_i16()
        stage = "right_hand_index"
        right_hand_index = reader.read_i16()
        stage = "survivor_kills"
        survivor_kills = reader.read_i32()
        stage = "nutrition"
        nutrition = _read_nutrition_summary(reader)
        stage = "all_chat_muted"
        all_chat_muted = reader.read_u8() == 1
        stage = "tag_prefix"
        tag_prefix_pos = reader.tell()
        try:
            tag_prefix = read_string(reader)
        except Exception:
            reader.seek(tag_prefix_pos)
            resync_pos = _resync_iso_player_tag_prefix(
                reader, world_version, max_scan=16384
            )
            if resync_pos is None:
                raise
            log_parse_debug(
                "iso_player_tag_prefix_resync",
                source="player_blob_parser",
                from_pos=tag_prefix_pos,
                to_pos=resync_pos,
                world_version=world_version,
            )
            reader.seek(resync_pos)
            tag_prefix = read_string(reader)
        stage = "tag_color"
        tag_color = (
            reader.read_f32(),
            reader.read_f32(),
            reader.read_f32(),
        )
        stage = "display_name"
        display_name = read_string(reader)
        stage = "show_tag"
        show_tag = reader.read_u8() == 1
        stage = "faction_pvp"
        faction_pvp = reader.read_u8() == 1
        auto_drink = None
        if world_version >= 239:
            stage = "auto_drink"
            auto_drink = reader.read_u8() == 1
        stage = "extra_info_flags"
        extra_info_flags_raw = reader.read_u8()
        extra_info_flags = extra_info_flags_raw if world_version >= 198 else None
        stage = "has_vehicle"
        has_vehicle = reader.read_u8() == 1
        vehicle: Dict[str, object] = {"has_vehicle": has_vehicle}
        if has_vehicle:
            stage = "vehicle_x"
            vehicle_x = reader.read_f32()
            stage = "vehicle_y"
            vehicle_y = reader.read_f32()
            stage = "vehicle_seat"
            seat = reader.read_i8()
            stage = "vehicle_running"
            running = reader.read_u8() == 1
            vehicle.update(
                {
                    "x": vehicle_x,
                    "y": vehicle_y,
                    "seat": seat,
                    "running": running,
                }
            )
        stage = "mechanics_count"
        mechanics_count = reader.read_i32()
        if mechanics_count < 0:
            mechanics_count = 0
        max_by_remaining = reader.remaining() // 16
        if mechanics_count > max_by_remaining:
            mechanics_count = max_by_remaining
        for _ in range(mechanics_count):
            stage = "mechanics_item"
            reader.read_i64()
            reader.read_i64()
        base_summary = {
            "hours_survived": hours_survived,
            "zombie_kills": zombie_kills,
            "worn_count": worn_count,
            "worn_items": worn_items,
            "left_hand_index": left_hand_index,
            "right_hand_index": right_hand_index,
            "survivor_kills": survivor_kills,
            "nutrition": nutrition,
            "all_chat_muted": all_chat_muted,
            "tag_prefix": tag_prefix,
            "tag_color": tag_color,
            "display_name": display_name,
            "show_tag": show_tag,
            "faction_pvp": faction_pvp,
            "auto_drink": auto_drink,
            "extra_info_flags": extra_info_flags,
            "vehicle": vehicle,
            "mechanics_count": mechanics_count,
        }
        stage = "fitness"
        try:
            fitness = _read_fitness_summary(reader, world_version)
        except Exception as exc:
            log_parse_exception(
                "iso_player_fitness_failed",
                exc,
                source="player_blob_parser",
                pos=reader.tell(),
                world_version=world_version,
            )
            base_summary["fitness_error"] = True
            base_summary["fitness"] = {"supported": False}
            base_summary["read_books"] = 0
            base_summary["read_books_items"] = []
            base_summary["known_media"] = 0
            base_summary["known_media_items"] = []
            return base_summary
        if reader.remaining() < 2:
            base_summary["fitness"] = fitness
            base_summary["read_books"] = 0
            base_summary["read_books_items"] = []
            base_summary["known_media"] = 0
            base_summary["known_media_items"] = []
            return base_summary
        read_books_count = 0
        read_books_items: list[str] = []
        if world_version >= 184:
            stage = "read_books_count_i16"
            read_books_count = reader.read_i16()
            if read_books_count < 0:
                read_books_count = 0
            max_by_remaining = reader.remaining() // 2
            if read_books_count > max_by_remaining:
                read_books_count = max_by_remaining
            read_books_read = 0
            for _ in range(read_books_count):
                stage = "read_books_id"
                item_pos = reader.tell()
                try:
                    book_id = reader.read_u16()
                except Exception:
                    reader.seek(item_pos)
                    log_parse_debug(
                        "iso_player_read_books_truncated",
                        source="player_blob_parser",
                        read=read_books_read,
                        expected=read_books_count,
                        pos=reader.tell(),
                        world_version=world_version,
                    )
                    break
                full_type = dictionary.get(int(book_id)) if dictionary else ""
                full_type = full_type or f"id:{book_id}"
                read_books_items.append(full_type)
                read_books_read += 1
            read_books_count = read_books_read
        elif world_version >= 182:
            stage = "read_books_count_i32"
            read_books_count = reader.read_i32()
            if read_books_count < 0:
                read_books_count = 0
            for _ in range(read_books_count):
                stage = "read_books_name"
                book_name = read_string(reader)
                read_books_items.append(book_name)
        known_media_count = 0
        known_media_items: list[str] = []
        if world_version >= 189:
            stage = "known_media"
            try:
                media_info = _read_known_media_lines(reader)
                known_media_count = int(media_info.get("count", 0))
                known_media_items = list(media_info.get("items") or [])
            except Exception as exc:
                log_parse_exception(
                    "iso_player_known_media_failed",
                    exc,
                    source="player_blob_parser",
                    pos=reader.tell(),
                    world_version=world_version,
                )
        base_summary["fitness"] = fitness
        base_summary["read_books"] = read_books_count
        base_summary["read_books_items"] = read_books_items
        base_summary["known_media"] = known_media_count
        base_summary["known_media_items"] = known_media_items
        return base_summary
    except Exception as exc:
        if _allow_retry:
            retry_pos = _find_next_iso_player_start(
                reader,
                world_version,
                start + 1,
                max_scan=65536,
            )
            if retry_pos is not None:
                log_parse_debug(
                    "iso_player_retry_resync",
                    source="player_blob_parser",
                    from_pos=start,
                    to_pos=retry_pos,
                    stage=stage,
                    world_version=world_version,
                )
                reader.seek(retry_pos)
                return _read_iso_player_summary(
                    reader,
                    world_version,
                    dictionary,
                    _allow_retry=False,
                )
        raise ValueError(f"iso_player stage={stage}") from exc


def _read_nutrition_summary(reader: ByteBufferReader) -> Dict[str, float]:
    calories = reader.read_f32()
    proteins = reader.read_f32()
    lipids = reader.read_f32()
    carbohydrates = reader.read_f32()
    weight = reader.read_f32()
    return {
        "calories": calories,
        "proteins": proteins,
        "lipids": lipids,
        "carbohydrates": carbohydrates,
        "weight": weight,
    }


def _read_fitness_summary(
    reader: ByteBufferReader, world_version: int
) -> Dict[str, object]:
    if world_version < 167:
        return {"supported": False}
    if reader.remaining() < 4:
        return {"supported": False}

    def _safe_read_string() -> bool:
        if reader.remaining() < 2:
            return False
        pos = reader.tell()
        try:
            read_string(reader)
        except Exception:
            reader.seek(pos)
            return False
        return True

    def _read_count(min_item_size: int) -> int:
        if reader.remaining() < 4:
            return 0
        count = reader.read_i32()
        if count < 0:
            count = 0
        max_by_remaining = 0
        if min_item_size > 0:
            max_by_remaining = reader.remaining() // min_item_size
        if count > max_by_remaining:
            count = max_by_remaining
        return count

    stiffness_inc = _read_count(6)
    stiffness_inc_read = 0
    for _ in range(stiffness_inc):
        if not _safe_read_string() or reader.remaining() < 4:
            break
        reader.read_f32()
        stiffness_inc_read += 1
    stiffness_inc = stiffness_inc_read

    stiffness_timer = _read_count(6)
    stiffness_timer_read = 0
    for _ in range(stiffness_timer):
        if not _safe_read_string() or reader.remaining() < 4:
            break
        reader.read_i32()
        stiffness_timer_read += 1
    stiffness_timer = stiffness_timer_read

    regularity = _read_count(6)
    regularity_read = 0
    for _ in range(regularity):
        if not _safe_read_string() or reader.remaining() < 4:
            break
        reader.read_f32()
        regularity_read += 1
    regularity = regularity_read

    bodypart_inc = _read_count(2)
    bodypart_inc_read = 0
    for _ in range(bodypart_inc):
        if not _safe_read_string():
            break
        bodypart_inc_read += 1
    bodypart_inc = bodypart_inc_read
    exe_timer = None
    if world_version >= 169:
        exe_timer = _read_count(10)
        exe_timer_read = 0
        for _ in range(exe_timer):
            if not _safe_read_string() or reader.remaining() < 8:
                break
            reader.read_i64()
            exe_timer_read += 1
        exe_timer = exe_timer_read
    return {
        "supported": True,
        "stiffness_inc": stiffness_inc,
        "stiffness_timer": stiffness_timer,
        "regularity": regularity,
        "bodypart_inc": bodypart_inc,
        "exe_timer": exe_timer,
    }


def _attach_worn_item_details(
    extra_summary: Dict[str, object],
    inventory_summary: Dict[str, object],
) -> None:
    worn_items = extra_summary.get("worn_items")
    item_details = inventory_summary.get("item_details")
    if not isinstance(worn_items, list) or not isinstance(item_details, list):
        return
    detail_pool = []
    for detail in item_details:
        count = detail.get("count", 1)
        if not isinstance(count, int) or count < 1:
            count = 1
        detail_pool.append({"detail": detail, "remaining": count})
    for worn in worn_items:
        if not isinstance(worn, dict):
            continue
        target_id = worn.get("id")
        target_name = worn.get("name") or ""
        match = _consume_worn_detail(detail_pool, target_id, target_name)
        if match:
            worn["detail"] = match


def _consume_worn_detail(
    detail_pool: list[Dict[str, object]],
    target_id: object,
    target_name: str,
) -> Optional[Dict[str, object]]:
    if isinstance(target_id, int):
        match = _consume_detail_entry(
            detail_pool,
            lambda detail: detail.get("registry_id") == target_id
            or detail.get("item_id") == target_id,
        )
        if match:
            return match
    if target_name:
        return _consume_detail_entry(
            detail_pool,
            lambda detail: _detail_name_matches(detail, target_name),
        )
    return None


def _consume_detail_entry(
    detail_pool: list[Dict[str, object]],
    matcher,
) -> Optional[Dict[str, object]]:
    for entry in detail_pool:
        if entry.get("remaining", 0) < 1:
            continue
        detail = entry.get("detail")
        if not isinstance(detail, dict):
            continue
        if matcher(detail):
            entry["remaining"] = entry.get("remaining", 1) - 1
            return detail
    return None


def _detail_name_matches(detail: Dict[str, object], target_name: str) -> bool:
    full_type = detail.get("full_type") or ""
    custom_name = detail.get("custom_name") or ""

    def _normalize(value: str) -> str:
        text = (value or "").strip().lower()
        if text.startswith("base."):
            text = text[5:]
        return text

    target_norm = _normalize(target_name)
    if not target_norm:
        return False
    if target_name == full_type or target_name == custom_name:
        return True
    if full_type and full_type.endswith(f".{target_name}"):
        return True
    if target_norm == _normalize(full_type):
        return True
    if custom_name and target_norm == _normalize(custom_name):
        return True
    if _normalize(full_type).endswith(f".{target_norm}"):
        return True
    return False


def _read_known_media_lines(reader: ByteBufferReader) -> Dict[str, object]:
    start = reader.tell()
    try:
        return _read_known_media_lines_impl(reader, count_size="i16")
    except Exception:
        reader.seek(start)
        return _read_known_media_lines_impl(reader, count_size="i32")


def _read_known_media_lines_impl(
    reader: ByteBufferReader, *, count_size: str
) -> Dict[str, object]:
    if count_size == "i32":
        count = reader.read_i32()
    else:
        count = reader.read_i16()
    if count < 0:
        count = 0
    if count > 0:
        peek_pos = reader.tell()
        peek_len = None
        if reader.remaining() >= 2:
            peek_len = reader.read_i16()
            reader.seek(peek_pos)
        max_len = 4096
        if (
            peek_len is None
            or peek_len <= 0
            or peek_len > reader.remaining()
            or peek_len > max_len
        ):
            raise ValueError("known_media_count mismatch")
    items = []
    read_count = 0
    for _ in range(count):
        item_pos = reader.tell()
        try:
            value = read_string_utf(reader)
        except Exception:
            reader.seek(item_pos)
            log_parse_debug(
                "iso_player_known_media_truncated",
                source="player_blob_parser",
                read=read_count,
                expected=count,
                pos=reader.tell(),
            )
            break
        items.append(value)
        read_count += 1
    return {"count": read_count, "items": items}
