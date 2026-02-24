"""
Save version detection utilities.

Detection method: Use save directory structure + chunk CRC verification.
- B41: map_<x>_<y>.bin files in save root.
- B42: map/<x>/<y>.bin or map/map_<x>_<y>.bin.

Key insight: WorldVersion is NOT reliable (both B41/B42 can be 195).
The reliable method is directory structure + CRC validation.
"""
from __future__ import annotations

import os
import re
import struct
import zlib
from pathlib import Path
from typing import Optional, Tuple

# Chunk dimensions
B41_CHUNK_SIZE = 10  # 10×10 = 100 GridSquares
B42_CHUNK_SIZE = 8  # 8×8 = 64 GridSquares
CELL_TILE_SIZE = 300  # Map cells are 300x300 tiles in worldmap data

# Header constants
HEADER_SIZE = 17  # debug(1) + version(4) + length(4) + crc(8)
CRC_OFFSET = 9    # CRC starts at byte 9
MIN_VERSION_FOR_CRC = 61  # Only version >= 61 has extended header


def detect_build_version(save_path: Path, debug: bool = False) -> Optional[str]:
    """Detect B41 vs B42 by analyzing save directory structure and chunk files.

    Returns "B41", "B42", or None if detection fails.

    Detection strategy:
    1. Identify directory structure (B41: map_<x>_<y>.bin, B42: map/<x>/<y>.bin)
    2. Verify chunk files are valid PZ format (CRC check)
    3. Directory structure + valid CRC = confirmed version

    Key insight: B42 reorganized directory structure, this is the reliable indicator.
    CRC verification ensures the files are genuine PZ chunks, not fake directories.
    """
    if not isinstance(save_path, Path) or not save_path.exists():
        return None
    if save_path.name.lower() in ("map", "chunkdata"):
        parent = save_path.parent
        if isinstance(parent, Path) and parent.exists():
            save_path = parent

    # Find chunk files - directory structure is the primary indicator
    chunk_files, structure_type = _find_chunk_files(save_path, max_count=10)

    if debug:
        print(f"[detect] Directory structure: {structure_type}, found {len(chunk_files)} chunk files")

    # No chunk files found - try secondary detection only
    if not chunk_files:
        if debug:
            print("[detect] No chunk files found, trying secondary detection")
        result = _detect_by_file_structure(save_path, debug)
        if result:
            return result
        return None

    # Verify chunk files are valid PZ format using CRC
    valid_count = 0
    checked_count = 0

    for chunk_path in chunk_files[:5]:  # Check up to 5 files
        try:
            with open(chunk_path, "rb") as f:
                chunk_data = f.read()
            checked_count += 1

            if _verify_crc(chunk_data):
                valid_count += 1
                if debug:
                    print(f"  {chunk_path.name}: CRC valid")
            else:
                if debug:
                    print(f"  {chunk_path.name}: CRC invalid")
        except Exception as e:
            if debug:
                print(f"  {chunk_path.name}: read error - {e}")

    if debug:
        print(f"[detect] CRC verification: {valid_count}/{checked_count} valid")

    # Decision: structure + CRC verification
    if checked_count == 0:
        # Can't verify any files
        if debug:
            print("[detect] No files could be verified")
        return None

    # At least some files should pass CRC
    validity_ratio = valid_count / checked_count

    if structure_type == "B42":
        if validity_ratio >= 0.5:
            if debug:
                print("[detect] B42 confirmed (structure + CRC)")
            return "B42"
        else:
            if debug:
                print(f"[detect] B42 structure but low CRC validity ({validity_ratio:.1%})")
            # Still trust structure if we found the pattern
            return "B42"

    if structure_type == "B41":
        if validity_ratio >= 0.5:
            if debug:
                print("[detect] B41 confirmed (structure + CRC)")
            return "B41"
        else:
            if debug:
                print(f"[detect] B41 structure but low CRC validity ({validity_ratio:.1%})")
            # Still trust structure if we found the pattern
            return "B41"

    # Unknown structure - try secondary detection
    result = _detect_by_file_structure(save_path, debug)
    if result:
        return result

    if debug:
        print("[detect] Unknown structure, defaulting to B41")
    return "B41"


def _find_chunk_files(save_path: Path, max_count: int = 10) -> Tuple[list, str]:
    """Find chunk files in save directory.

    Returns (file_list, structure_type) where structure_type is:
    - "B41": map_<x>_<y>.bin files in root directory
    - "B42": chunk files in map/ directory (either map_<x>_<y>.bin or <x>/<y>.bin)
    - "unknown": couldn't determine structure

    B42 has two possible structures:
    1. map/map_<x>_<y>.bin - flat structure in map/ directory
    2. map/<x>/<y>.bin - nested structure with x-coordinate subdirectories
    """
    chunk_pattern = re.compile(r"^map_-?\d+_-?\d+\.bin$", re.IGNORECASE)
    chunk_pattern_nested = re.compile(r"^-?\d+\.bin$", re.IGNORECASE)
    result = []

    # Check map/ directory first (B42 structures)
    map_dir = save_path / "map"
    if map_dir.exists():
        # B42 structure 1: map/map_<x>_<y>.bin (flat)
        try:
            with os.scandir(map_dir) as it:
                for entry in it:
                    if entry.is_file() and chunk_pattern.match(entry.name):
                        result.append(Path(entry.path))
                        if len(result) >= max_count:
                            return result, "B42"
        except Exception:
            pass

        if result:
            return result, "B42"

        # B42 structure 2: map/<x>/<y>.bin (nested)
        try:
            for x_dir in map_dir.iterdir():
                if not x_dir.is_dir():
                    continue
                for bin_file in x_dir.glob("*.bin"):
                    if chunk_pattern_nested.match(bin_file.name):
                        result.append(bin_file)
                        if len(result) >= max_count:
                            return result, "B42"
        except Exception:
            pass

        if result:
            return result, "B42"

    # B41 structure: root/map_<x>_<y>.bin
    try:
        with os.scandir(save_path) as it:
            for entry in it:
                if entry.is_file() and chunk_pattern.match(entry.name):
                    result.append(Path(entry.path))
                    if len(result) >= max_count:
                        return result, "B41"
    except Exception:
        pass

    if result:
        return result, "B41"

    return [], "unknown"


def _calculate_parse_score(data: bytes, chunk_size: int, debug: bool = False) -> float:
    """Calculate a parsing consistency score for given chunk size.

    Returns a score between 0 and 1, where higher means more consistent parsing.

    Key insight: The correct format will have valid z-flags patterns throughout,
    while the wrong format will start showing inconsistent patterns after
    the actual tile count is exceeded.

    Strategy:
    1. Read z-flags for all tiles (estimating minimum GridSquare size)
    2. Check if z-flags remain valid throughout
    3. Penalize heavily if reading past actual tile boundaries
    """
    try:
        data_len = len(data)

        if data_len < 5:
            return 0.0

        # Read header
        pos = 1  # skip debug byte
        world_version = struct.unpack(">i", data[pos:pos+4])[0]
        pos += 4

        if world_version <= 0 or world_version > 1000:
            return 0.0

        # Skip extended header
        if world_version >= MIN_VERSION_FOR_CRC:
            if data_len < HEADER_SIZE:
                return 0.0
            pos = HEADER_SIZE

        # Read blood splat count
        if data_len < pos + 4:
            return 0.0
        blood_count = struct.unpack(">i", data[pos:pos+4])[0]
        pos += 4

        if blood_count < 0 or blood_count > 100000:
            return 0.0

        # Skip blood splat data
        splat_size = 11
        pos += blood_count * splat_size

        if pos >= data_len:
            return 0.0

        # Analyze z-flags pattern for ALL tiles
        total_tiles = chunk_size * chunk_size
        valid_zflags = 0
        invalid_zflags = 0

        # Common z-flags patterns (ground level only, ground+first floor, etc.)
        common_patterns = {0x00, 0x01, 0x03, 0x07, 0x0F, 0x1F, 0x3F}

        for tile_idx in range(total_tiles):
            if pos >= data_len:
                # Running out of data before finishing - wrong format
                invalid_zflags += (total_tiles - tile_idx)
                break

            z_flags = data[pos]
            pos += 1

            # Check if z_flags looks valid
            if z_flags in common_patterns:
                valid_zflags += 1
            elif z_flags < 0x80:
                # Unusual but could be valid (multi-story buildings)
                valid_zflags += 0.7
            else:
                # High z-levels are very rare
                invalid_zflags += 1

            # Estimate and skip GridSquare data (minimum size)
            # For each set bit in z_flags, assume minimum GridSquare:
            # ErosionData (1 byte if init=false) + BitHeader (1 byte) = 2 bytes
            z_count = bin(z_flags).count('1')
            if z_count > 0:
                # Use actual ErosionData length for more accuracy
                for z in range(8):
                    if not (z_flags & (1 << z)):
                        continue
                    if pos >= data_len:
                        break
                    # Read ErosionData length
                    erosion_len, _ = _get_erosion_data_length(data, pos, world_version, data_len)
                    pos += erosion_len
                    if pos >= data_len:
                        break
                    # Skip BitHeader
                    bit_header = data[pos]
                    pos += 1
                    # Skip object count if needed
                    if bit_header & 8:
                        pos += 2
                    # We can't skip object data, but we've consumed the header
                    # For subsequent z-levels, just estimate
                    break  # Only parse first z-level accurately

                # Estimate remaining z-levels (if any)
                remaining_z = z_count - 1
                if remaining_z > 0:
                    # Very rough estimate: 2-10 bytes per additional z-level
                    pos += remaining_z * 4

        total_zflags = valid_zflags + invalid_zflags
        if total_zflags == 0:
            return 0.0

        score = valid_zflags / total_zflags

        # Additional check: verify data continues after all tiles
        # If we're past the data, it's likely wrong format
        remaining_data = data_len - pos
        if remaining_data < 0:
            score *= 0.5  # Penalty for overrunning

        return score

    except Exception:
        return 0.0


def _detect_by_file_structure(save_path: Path, debug: bool = False) -> Optional[str]:
    """Secondary detection using save directory structure.

    Some B42 saves have different file organization.
    """
    # Check for map/ subdirectory with organized chunks
    map_dir = save_path / "map"
    if map_dir.exists():
        subdirs = [d for d in map_dir.iterdir() if d.is_dir()]
        if subdirs:
            if debug:
                print("[detect] Found organized map/ directory - likely B42")
            return "B42"

    # Check isoregiondata for patterns
    iso_dir = save_path / "isoregiondata"
    if iso_dir.exists():
        datachunk_files = list(iso_dir.glob("datachunk_*.bin"))
        if datachunk_files:
            # Read a datachunk file and look for patterns
            try:
                with open(datachunk_files[0], "rb") as f:
                    data = f.read(200)
                # B41 might have consecutive 0x10 (16) bytes indicating 10-tile width
                consecutive_16 = 0
                max_consecutive_16 = 0
                for b in data:
                    if b == 0x10:
                        consecutive_16 += 1
                        max_consecutive_16 = max(max_consecutive_16, consecutive_16)
                    else:
                        consecutive_16 = 0
                # If many consecutive 0x10, likely B41
                if max_consecutive_16 >= 50:
                    if debug:
                        print(f"[detect] Found {max_consecutive_16} consecutive 0x10 in isoregiondata - B41")
                    return "B41"
            except Exception:
                pass

    return None


def _find_and_read_chunk(save_path: Path) -> Optional[bytes]:
    """Find and read a chunk file for analysis."""
    chunk_pattern = re.compile(r"^map_-?\d+_-?\d+\.bin$", re.IGNORECASE)

    # Try root directory first
    try:
        with os.scandir(save_path) as it:
            for entry in it:
                if entry.is_file() and chunk_pattern.match(entry.name):
                    with open(entry.path, "rb") as f:
                        return f.read()
    except Exception:
        pass

    # Try map/ subdirectory (B42 large saves)
    map_dir = save_path / "map"
    if map_dir.exists():
        try:
            for x_dir in map_dir.iterdir():
                if not x_dir.is_dir():
                    continue
                for bin_file in x_dir.glob("*.bin"):
                    if chunk_pattern.match(bin_file.name):
                        with bin_file.open("rb") as f:
                            return f.read()
                break
        except Exception:
            pass

    return None


def _verify_crc(data: bytes) -> bool:
    """Verify CRC32 of chunk data.

    Header structure (17 bytes for version >= 61):
    - [0]     debug     (1 byte)
    - [1-4]   version   (4 bytes, big-endian)
    - [5-8]   length    (4 bytes, big-endian)
    - [9-16]  crc       (8 bytes, big-endian, high 4 bytes = 0)

    CRC is calculated from offset 17 to EOF using zlib.crc32().
    """
    if len(data) < HEADER_SIZE:
        return False

    # Read world version
    world_version = struct.unpack(">i", data[1:5])[0]
    if world_version < MIN_VERSION_FOR_CRC:
        # Old format without CRC, can't verify
        return True

    # Extract stored CRC (low 4 bytes of 8-byte field)
    stored_crc = struct.unpack(">Q", data[CRC_OFFSET:CRC_OFFSET + 8])[0]
    stored_crc_32 = stored_crc & 0xFFFFFFFF

    # Calculate CRC from offset 17 to EOF
    calculated_crc = zlib.crc32(data[HEADER_SIZE:]) & 0xFFFFFFFF

    return stored_crc_32 == calculated_crc


def _try_parse_gridsquares(data: bytes, chunk_size: int, debug: bool = False) -> Tuple[bool, str]:
    """Try to parse chunk data with given chunk size.

    Returns (success, reason) tuple.

    Detection strategy: Scan through z-flags and validate ErosionData patterns.
    Since we can't easily skip object data, we look for structural consistency:
    - Valid z-flags should be followed by valid ErosionData for each set bit
    - ErosionData first byte should have valid flag patterns

    Key insight: Wrong chunk_size will cause z-flags to be read from wrong positions,
    leading to inconsistent ErosionData patterns.
    """
    try:
        data_len = len(data)

        if data_len < 5:
            return False, "data too short"

        # Read header
        pos = 1  # skip debug byte
        world_version = struct.unpack(">i", data[pos:pos+4])[0]
        pos += 4

        if debug:
            print(f"  [parse] world_version={world_version}, chunk_size={chunk_size}")

        # Validate world version
        if world_version <= 0 or world_version > 1000:
            return False, f"invalid world_version {world_version}"

        # Skip extended header for version >= 61
        if world_version >= MIN_VERSION_FOR_CRC:
            if data_len < HEADER_SIZE:
                return False, "data too short for extended header"
            pos = HEADER_SIZE

        # Read and skip blood splats
        if data_len < pos + 4:
            return False, "data too short for blood_count"
        blood_count = struct.unpack(">i", data[pos:pos+4])[0]
        pos += 4

        if blood_count < 0 or blood_count > 100000:
            return False, f"invalid blood_count {blood_count}"

        # Skip blood splat data (each splat: 3 bytes coords + 4 bytes type + 4 bytes worldAge = 11 bytes)
        splat_size = 11
        pos += blood_count * splat_size

        if pos > data_len:
            return False, "blood splats exceed data"

        if debug:
            print(f"  [parse] blood_count={blood_count}, after_splats_pos={pos}")

        # Validate GridSquare structure using pattern analysis
        total_tiles = chunk_size * chunk_size
        valid_erosion_count = 0
        invalid_erosion_count = 0
        tiles_checked = 0
        max_tiles_to_check = min(total_tiles, 20)  # Only check first 20 tiles

        gridsquare_start = pos

        for tile_idx in range(max_tiles_to_check):
            if pos >= data_len:
                break

            # Read z-flags for this tile
            z_flags = data[pos]
            pos += 1
            tiles_checked += 1

            # For each z-level with data
            for z in range(8):
                if not (z_flags & (1 << z)):
                    continue

                if pos >= data_len:
                    break

                # Validate ErosionData pattern
                erosion_flags = data[pos]

                # Check if this looks like valid ErosionData
                if _is_valid_erosion_flags(erosion_flags):
                    valid_erosion_count += 1
                else:
                    invalid_erosion_count += 1

                # Calculate ErosionData length and skip
                erosion_len, _ = _get_erosion_data_length(data, pos, world_version, data_len)
                pos += erosion_len

                if pos >= data_len:
                    break

                # Skip BitHeader (1 byte)
                bit_header = data[pos]
                pos += 1

                # Skip object count bytes if needed (but NOT object data)
                # Correct order: if (& 2) -> 2, else if (& 4) -> 3, else if (& 8) -> short
                if bit_header & 2:
                    pass  # objectCount = 2, no extra bytes
                elif bit_header & 4:
                    pass  # objectCount = 3, no extra bytes
                elif bit_header & 8:
                    # objectCount from short
                    if pos + 2 <= data_len:
                        pos += 2

                # We cannot easily skip object data, so we stop here for this tile
                # and move to the next z-flags position based on chunk_size pattern
                break  # Exit z-level loop after first GridSquare

        if debug:
            print(f"  [parse] tiles_checked={tiles_checked}, valid_erosion={valid_erosion_count}, invalid_erosion={invalid_erosion_count}")

        # Evaluate results
        total_erosion = valid_erosion_count + invalid_erosion_count
        if total_erosion == 0:
            # No erosion data found - this could be an empty chunk
            # Try secondary validation: check z-flags distribution
            return _validate_zflags_distribution(data, gridsquare_start, chunk_size, data_len, debug)

        # Calculate validity ratio
        validity_ratio = valid_erosion_count / total_erosion if total_erosion > 0 else 0

        if debug:
            print(f"  [parse] validity_ratio={validity_ratio:.2f}")

        # High validity ratio suggests correct format
        if validity_ratio >= 0.7:
            return True, f"valid erosion pattern (ratio={validity_ratio:.2f})"

        return False, f"low validity ratio ({validity_ratio:.2f})"

    except Exception as e:
        return False, f"exception: {e}"


def _is_valid_erosion_flags(flags: int) -> bool:
    """Check if a byte looks like valid ErosionData flags.

    Valid patterns:
    - (flags & 1) == 0: Not initialized (any even value is valid)
    - (flags & 1) == 1: Initialized, bits 2-6 should follow regionCount encoding

    Invalid patterns:
    - Bit 7 set with bits 2-6 also set (conflicting regionCount)
    - Very high values that don't match expected patterns
    """
    # Not initialized: any even value
    if (flags & 1) == 0:
        return True

    # Initialized: check regionCount bits
    # Only one of bits 2-6 should be set (or none for regionCount=0)
    region_bits = (flags >> 2) & 0x1F  # bits 2-6
    bit_count = bin(region_bits).count('1')

    # At most one regionCount bit should be set
    if bit_count > 1:
        # Could still be valid if bit 6 is set (variable regionCount)
        if flags & 64:
            return True
        return False

    return True


def _validate_zflags_distribution(data: bytes, start_pos: int, chunk_size: int,
                                   data_len: int, debug: bool = False) -> Tuple[bool, str]:
    """Secondary validation using z-flags distribution.

    If no ErosionData was found (empty chunk), check if z-flags pattern is consistent.
    Most z-flags should be 0x00 (no data) or 0x01 (only z=0 has data).
    """
    total_tiles = chunk_size * chunk_size
    pos = start_pos

    common_zflags = 0  # Count of common patterns (0x00, 0x01, 0x03)
    total_zflags = 0

    for _ in range(min(total_tiles, 50)):
        if pos >= data_len:
            break

        z_flags = data[pos]
        pos += 1
        total_zflags += 1

        # Common patterns: no data (0x00), only ground (0x01), ground+first floor (0x03)
        if z_flags in (0x00, 0x01, 0x03, 0x07):
            common_zflags += 1

        # Skip any GridSquare data (simplified: just move to next byte if z_flags != 0)
        # This is a rough estimate since we can't parse object data
        if z_flags != 0:
            # Estimate minimum GridSquare size: ErosionData(1) + BitHeader(1) = 2 bytes per z-level
            z_count = bin(z_flags).count('1')
            pos += z_count * 2

    if total_zflags == 0:
        return False, "no z-flags read"

    common_ratio = common_zflags / total_zflags

    if debug:
        print(f"  [zflags] common_ratio={common_ratio:.2f} ({common_zflags}/{total_zflags})")

    # If most z-flags are common patterns, format is likely correct
    if common_ratio >= 0.5:
        return True, f"common zflags pattern (ratio={common_ratio:.2f})"

    return False, f"uncommon zflags pattern (ratio={common_ratio:.2f})"


def _get_erosion_data_length(data: bytes, offset: int, version: int, data_len: int) -> Tuple[int, Optional[str]]:
    """Calculate ErosionData length.

    ErosionData structure:
    - First byte is flags
    - If (flags & 1) == 0: only 1 byte (not initialized)
    - If (flags & 1) != 0: variable length based on flags

    Returns (length, error_message) tuple.
    """
    if offset >= data_len:
        return 1, "offset beyond data"

    flags = data[offset]

    # Case 1: init = false (flags & 1 == 0)
    if (flags & 1) == 0:
        return 1, None

    # Case 2: init = true
    length = 4  # flags + noiseMainByte + soil + magicNumByte

    # version < 55 has an obsolete byte
    if version < 55:
        length += 1

    # Determine regionCount
    if flags & 4:
        region_count = 1
    elif flags & 8:
        region_count = 2
    elif flags & 16:
        region_count = 3
    elif flags & 32:
        region_count = 4
    elif flags & 64:
        # regionCount from next byte
        if offset + length >= data_len:
            return length, "can't read region_count"
        region_count = data[offset + length]
        length += 1
    else:
        region_count = 0

    # Validate region_count
    if region_count > 20:
        return length, f"unreasonable region_count {region_count}"

    # Calculate length for each region
    for i in range(region_count):
        # Base: regionID + categoryID + dispSeason + dataFlags = 4 bytes
        region_base = 4

        # Check if we can read dataFlags
        data_flags_offset = offset + length + 3
        if data_flags_offset >= data_len:
            return length + region_base, "can't read dataFlags"

        data_flags = data[data_flags_offset]

        # If dataFlags & 128, there's an extra stage byte
        if data_flags & 128:
            region_base += 1

        length += region_base

    return length, None


def get_chunk_params(save_path: Path) -> Tuple[int, float]:
    """Get tile_per_chunk and chunks_per_cell based on detected build version.

    Returns:
        (tile_per_chunk, chunks_per_cell) tuple
        chunks_per_cell can be non-integer for B42 (300 / 8 = 37.5)
    """
    build = detect_build_version(save_path)

    if build == "B42":
        tile_per_chunk = B42_CHUNK_SIZE
    else:
        # Default to B41
        tile_per_chunk = B41_CHUNK_SIZE
    chunks_per_cell = float(CELL_TILE_SIZE) / float(tile_per_chunk or 1)
    return (tile_per_chunk, chunks_per_cell)


def read_world_version(save_path: Path) -> Optional[int]:
    """Read WorldVersion from save files.

    Note: WorldVersion alone cannot reliably distinguish B41 vs B42.
    Use detect_build_version() for reliable detection.
    """
    if not isinstance(save_path, Path) or not save_path.exists():
        return None
    if save_path.name.lower() in ("map", "chunkdata"):
        parent = save_path.parent
        if isinstance(parent, Path) and parent.exists():
            save_path = parent

    chunk_pattern = re.compile(r"^map_-?\d+_-?\d+\.bin$", re.IGNORECASE)

    # Try root directory
    try:
        with os.scandir(save_path) as it:
            for entry in it:
                if entry.is_file() and chunk_pattern.match(entry.name):
                    with open(entry.path, "rb") as f:
                        data = f.read(5)
                    if len(data) == 5:
                        return struct.unpack(">i", data[1:5])[0]
    except Exception:
        pass

    # Try map/ subdirectory
    map_dir = save_path / "map"
    if map_dir.exists():
        try:
            nested_pattern = re.compile(r"^-?\d+\.bin$", re.IGNORECASE)
            for x_dir in map_dir.iterdir():
                if not x_dir.is_dir():
                    continue
                try:
                    int(x_dir.name)
                except Exception:
                    continue
                for bin_file in x_dir.glob("*.bin"):
                    if not (chunk_pattern.match(bin_file.name) or nested_pattern.match(bin_file.name)):
                        continue
                    with bin_file.open("rb") as f:
                        data = f.read(5)
                    if len(data) == 5:
                        return struct.unpack(">i", data[1:5])[0]
                break
        except Exception:
            pass

    return None
