"""
Unified file signature validation framework.

Provides fast and deep signature strategies for cache invalidation:
- Fast: mtime_ns + size (sub-millisecond, suitable for frequent checks)
- Deep: mtime_ns + size + MD5 hash (50-100ms, suitable for critical data)

@author: Phase 3 optimization
"""
from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Dict, Optional

_SIG_SAMPLE_BYTES = 64 * 1024


def compute_signature(
    file_path: Path,
    strategy: str = "fast",
) -> Dict[str, object]:
    """
    Compute file signature for cache invalidation.

    Args:
        file_path: Path to file
        strategy:
            - "fast": mtime_ns + size only (sub-ms)
            - "deep": mtime_ns + size + sampled MD5 hash (50-100ms)

    Returns:
        Signature dict with keys: mtime_ns, size, and optionally hash
    """
    if not file_path.exists():
        return {}

    try:
        stat = file_path.stat()
    except OSError:
        return {}

    signature: Dict[str, object] = {
        "mtime_ns": getattr(stat, "st_mtime_ns", int(stat.st_mtime * 1_000_000_000)),
        "size": stat.st_size,
    }

    if strategy == "deep":
        md5 = hashlib.md5()
        try:
            with file_path.open("rb") as f:
                file_size = stat.st_size
                if file_size <= _SIG_SAMPLE_BYTES * 2:
                    md5.update(f.read())
                else:
                    md5.update(f.read(_SIG_SAMPLE_BYTES))
                    f.seek(max(file_size - _SIG_SAMPLE_BYTES, 0))
                    md5.update(f.read(_SIG_SAMPLE_BYTES))
            signature["hash"] = md5.hexdigest()
        except OSError:
            signature["hash"] = ""

    return signature


def validate_signature(
    file_path: Path,
    cached_signature: Dict[str, object],
    strategy: str = "fast",
) -> bool:
    """
    Validate file signature against cached signature.

    Args:
        file_path: Path to file
        cached_signature: Previously computed signature
        strategy: "fast" or "deep"

    Returns:
        True if signature matches (file unchanged)
    """
    if not cached_signature:
        return False

    current_sig = compute_signature(file_path, strategy)
    if not current_sig:
        return False

    if strategy == "fast":
        return (
            current_sig.get("mtime_ns") == cached_signature.get("mtime_ns")
            and current_sig.get("size") == cached_signature.get("size")
        )
    else:
        return (
            current_sig.get("mtime_ns") == cached_signature.get("mtime_ns")
            and current_sig.get("size") == cached_signature.get("size")
            and current_sig.get("hash") == cached_signature.get("hash")
        )


def compute_multi_file_signature(
    file_paths: Dict[str, Path],
    strategy: str = "fast",
) -> Dict[str, Dict[str, object]]:
    """
    Compute signatures for multiple files.

    Args:
        file_paths: Mapping of label → file path
        strategy: "fast" or "deep"

    Returns:
        Mapping of label → signature dict
    """
    result: Dict[str, Dict[str, object]] = {}
    for label, path in file_paths.items():
        result[label] = compute_signature(path, strategy)
    return result


def validate_multi_file_signature(
    file_paths: Dict[str, Path],
    cached_signatures: Dict[str, Dict[str, object]],
    strategy: str = "fast",
) -> bool:
    """
    Validate multiple file signatures at once.

    Returns True only if ALL files match their cached signatures.

    Args:
        file_paths: Mapping of label → file path
        cached_signatures: Mapping of label → cached signature
        strategy: "fast" or "deep"

    Returns:
        True if all signatures match
    """
    if not cached_signatures:
        return False

    for label, path in file_paths.items():
        cached_sig = cached_signatures.get(label, {})
        if not validate_signature(path, cached_sig, strategy):
            return False

    return True
