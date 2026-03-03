"""
Cross-platform file system watch service.
"""
from __future__ import annotations

import os
import time
import struct
import threading
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Tuple

from PyQt6.QtCore import QObject, pyqtSignal, QTimer

from utils.windows_admin import is_windows


class UsnWatchError(RuntimeError):
    def __init__(self, code: str, detail: str = "") -> None:
        super().__init__(detail or code)
        self.code = code
        self.detail = detail or code


class BaseWatcher(QObject):
    changed = pyqtSignal()
    error = pyqtSignal(str)

    _event_received = pyqtSignal()

    def __init__(self, *, debounce_ms: int = 500) -> None:
        super().__init__()
        self._debounce_ms = max(0, int(debounce_ms))
        self._debounce_timer = QTimer(self)
        self._debounce_timer.setSingleShot(True)
        self._debounce_timer.timeout.connect(self._emit_changed)
        self._event_received.connect(self._on_event_received)
        self._running = False

    def start(self) -> None:
        raise NotImplementedError

    def stop(self) -> None:
        self._running = False

    def _emit_changed(self) -> None:
        self.changed.emit()

    def _on_event_received(self) -> None:
        if self._debounce_ms <= 0:
            self.changed.emit()
            return
        self._debounce_timer.start(self._debounce_ms)

    def _notify_event(self) -> None:
        self._event_received.emit()


class WatchfilesWatcher(BaseWatcher):
    def __init__(self, roots: Iterable[Path], *, debounce_ms: int = 500) -> None:
        super().__init__(debounce_ms=debounce_ms)
        self._roots = [Path(root).resolve() for root in roots if root]
        self._thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()

    def start(self) -> None:
        if self._running:
            return
        if not self._roots:
            raise ValueError("no watch roots")
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._running = True
        self._thread.start()

    def stop(self) -> None:
        super().stop()
        self._stop_event.set()
        if self._thread is not None:
            self._thread.join(timeout=1.0)
        self._thread = None

    def _run(self) -> None:
        try:
            from watchfiles import watch

            paths = [str(root) for root in self._roots]
            for changes in watch(*paths, stop_event=self._stop_event):
                if self._stop_event.is_set():
                    break
                if changes:
                    self._notify_event()
        except Exception as exc:
            self.error.emit(str(exc))


class UsnJournalWatcher(BaseWatcher):
    def __init__(self, roots: Iterable[Path], *, debounce_ms: int = 500) -> None:
        super().__init__(debounce_ms=debounce_ms)
        self._roots = [Path(root).resolve() for root in roots if root]
        self._threads: List[threading.Thread] = []
        self._stop_event = threading.Event()
        self._drive_states: Dict[str, dict] = {}

    def start(self) -> None:
        if not is_windows():
            raise UsnWatchError("unsupported", "not_windows")
        if self._running:
            return
        if not self._roots:
            raise UsnWatchError("no_roots", "no_watch_roots")

        self._stop_event.clear()
        self._drive_states = self._prepare_drives(self._roots)

        self._threads = []
        for drive, state in self._drive_states.items():
            thread = threading.Thread(
                target=self._run_drive,
                args=(drive, state),
                daemon=True,
            )
            self._threads.append(thread)
            thread.start()

        self._running = True

    def stop(self) -> None:
        super().stop()
        self._stop_event.set()
        for thread in self._threads:
            thread.join(timeout=1.0)
        self._threads = []
        self._drive_states.clear()

    def _prepare_drives(self, roots: List[Path]) -> Dict[str, dict]:
        if not roots:
            raise UsnWatchError("no_roots", "no_watch_roots")

        grouped: Dict[str, List[Path]] = {}
        for root in roots:
            drive = os.path.splitdrive(str(root))[0].upper()
            if not drive:
                raise UsnWatchError("invalid_drive", f"invalid_drive:{root}")
            grouped.setdefault(drive, []).append(root)

        states: Dict[str, dict] = {}
        for drive, drive_roots in grouped.items():
            if not _is_ntfs_drive(drive):
                raise UsnWatchError("not_ntfs", f"not_ntfs:{drive}")
            handle = _open_volume_handle(drive)
            if handle is None:
                raise UsnWatchError("open_failed", f"open_failed:{drive}")
            try:
                journal = _query_usn_journal(handle)
            except Exception:
                _close_handle(handle)
                raise
            states[drive] = {
                "handle": handle,
                "journal_id": journal["journal_id"],
                "next_usn": journal["next_usn"],
                "dir_map": _build_dir_frn_map(drive_roots),
                "roots": drive_roots,
                "root_prefixes": _build_root_prefixes(drive_roots),
            }
        return states

    def _run_drive(self, drive: str, state: dict) -> None:
        handle = state["handle"]
        try:
            while not self._stop_event.is_set():
                next_usn, records = _read_usn_records(
                    handle,
                    state["next_usn"],
                    state["journal_id"],
                )
                state["next_usn"] = next_usn
                if records:
                    self._process_records(state, records)
                else:
                    time.sleep(0.2)
        except Exception as exc:
            self.error.emit(str(exc))
        finally:
            _close_handle(handle)

    def _process_records(self, state: dict, records: List[bytes]) -> None:
        dir_map = state["dir_map"]
        prefixes = state["root_prefixes"]

        for record in records:
            parsed = _parse_usn_record(record)
            if parsed is None:
                continue
            frn, parent_frn, reason, attributes, name = parsed
            parent_path = dir_map.get(parent_frn)
            if parent_path is None:
                continue
            full_path = Path(parent_path) / name
            is_dir = bool(attributes & FILE_ATTRIBUTE_DIRECTORY)

            if is_dir:
                if reason & USN_REASON_FILE_DELETE:
                    dir_map.pop(frn, None)
                elif reason & USN_REASON_RENAME_OLD_NAME:
                    # keep existing mapping until new name arrives
                    pass
                else:
                    dir_map[frn] = full_path

            if _path_matches_roots(str(full_path), prefixes):
                self._notify_event()


FILE_ATTRIBUTE_DIRECTORY = 0x00000010
USN_REASON_FILE_CREATE = 0x00000100
USN_REASON_FILE_DELETE = 0x00000200
USN_REASON_RENAME_OLD_NAME = 0x00001000

FSCTL_QUERY_USN_JOURNAL = 0x000900F4
FSCTL_READ_USN_JOURNAL = 0x000900BB


def _build_root_prefixes(roots: Iterable[Path]) -> List[str]:
    prefixes = []
    for root in roots:
        root_str = str(root.resolve()).rstrip("\\/")
        prefixes.append(root_str.lower())
    return prefixes


def _path_matches_roots(path: str, prefixes: List[str]) -> bool:
    lowered = path.lower()
    for prefix in prefixes:
        if lowered == prefix or lowered.startswith(prefix + os.sep):
            return True
    return False


def _build_dir_frn_map(roots: Iterable[Path]) -> Dict[int, Path]:
    mapping: Dict[int, Path] = {}
    for root in roots:
        root = root.resolve()
        root_frn = _get_file_reference(root)
        if root_frn is not None:
            mapping[root_frn] = root
        for current, dirs, _files in os.walk(root):
            for name in dirs:
                path = Path(current) / name
                frn = _get_file_reference(path)
                if frn is not None:
                    mapping[frn] = path
    return mapping


def _parse_usn_record(buf: bytes) -> Optional[Tuple[int, int, int, int, str]]:
    if len(buf) < 60:
        return None
    try:
        record_length, major, minor = struct.unpack_from("<IHH", buf, 0)
        if record_length <= 0:
            return None
        frn, parent_frn = struct.unpack_from("<QQ", buf, 8)
        reason = struct.unpack_from("<I", buf, 40)[0]
        attributes = struct.unpack_from("<I", buf, 52)[0]
        name_len, name_offset = struct.unpack_from("<HH", buf, 56)
        name_bytes = buf[name_offset:name_offset + name_len]
        name = name_bytes.decode("utf-16le", errors="ignore")
        return frn, parent_frn, reason, attributes, name
    except Exception:
        return None


def _read_usn_records(handle: int, start_usn: int, journal_id: int) -> Tuple[int, List[bytes]]:
    read_data = struct.pack(
        "<qIIQQQ",
        int(start_usn),
        0xFFFFFFFF,
        0,
        0,
        0,
        int(journal_id),
    )
    output = _device_io_control(handle, FSCTL_READ_USN_JOURNAL, read_data, 65536)
    if len(output) < 8:
        return start_usn, []
    next_usn = struct.unpack_from("<q", output, 0)[0]
    records = []
    offset = 8
    data_len = len(output)
    while offset + 4 <= data_len:
        record_length = struct.unpack_from("<I", output, offset)[0]
        if record_length <= 0 or offset + record_length > data_len:
            break
        records.append(output[offset:offset + record_length])
        offset += record_length
    return next_usn, records


def _query_usn_journal(handle: int) -> dict:
    output = _device_io_control(handle, FSCTL_QUERY_USN_JOURNAL, b"", 56)
    if len(output) < 56:
        raise UsnWatchError("journal_query_failed", "journal_query_failed")
    journal_id, _first_usn, next_usn, _low, _max, _size, _delta = struct.unpack_from(
        "<QQQQQQQ",
        output,
        0,
    )
    return {"journal_id": journal_id, "next_usn": next_usn}


def _device_io_control(handle: int, code: int, in_buffer: bytes, out_size: int) -> bytes:
    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.windll.kernel32
    in_buf = ctypes.create_string_buffer(in_buffer, len(in_buffer))
    out_buf = ctypes.create_string_buffer(out_size)
    bytes_returned = wintypes.DWORD()
    ok = kernel32.DeviceIoControl(
        wintypes.HANDLE(handle),
        code,
        in_buf,
        len(in_buffer),
        out_buf,
        out_size,
        ctypes.byref(bytes_returned),
        None,
    )
    if not ok:
        raise UsnWatchError("device_io_failed", str(ctypes.get_last_error()))
    return out_buf.raw[:bytes_returned.value]


def _open_volume_handle(drive: str) -> Optional[int]:
    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.windll.kernel32
    path = r"\\.\%s" % drive.rstrip("\\/")
    handle = kernel32.CreateFileW(
        path,
        0x80000000,  # GENERIC_READ
        0x00000001 | 0x00000002 | 0x00000004,  # SHARE_READ|WRITE|DELETE
        None,
        3,  # OPEN_EXISTING
        0,
        None,
    )
    if handle == wintypes.HANDLE(-1).value:
        return None
    return handle


def _close_handle(handle: int) -> None:
    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.windll.kernel32
    try:
        kernel32.CloseHandle(wintypes.HANDLE(handle))
    except Exception:
        pass


def _get_file_reference(path: Path) -> Optional[int]:
    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.windll.kernel32
    FILE_FLAG_BACKUP_SEMANTICS = 0x02000000

    handle = kernel32.CreateFileW(
        str(path),
        0,
        0x00000001 | 0x00000002 | 0x00000004,
        None,
        3,
        FILE_FLAG_BACKUP_SEMANTICS,
        None,
    )
    if handle == wintypes.HANDLE(-1).value:
        return None

    class BY_HANDLE_FILE_INFORMATION(ctypes.Structure):
        _fields_ = [
            ("dwFileAttributes", wintypes.DWORD),
            ("ftCreationTime", wintypes.FILETIME),
            ("ftLastAccessTime", wintypes.FILETIME),
            ("ftLastWriteTime", wintypes.FILETIME),
            ("dwVolumeSerialNumber", wintypes.DWORD),
            ("nFileSizeHigh", wintypes.DWORD),
            ("nFileSizeLow", wintypes.DWORD),
            ("nNumberOfLinks", wintypes.DWORD),
            ("nFileIndexHigh", wintypes.DWORD),
            ("nFileIndexLow", wintypes.DWORD),
        ]

    info = BY_HANDLE_FILE_INFORMATION()
    ok = kernel32.GetFileInformationByHandle(
        wintypes.HANDLE(handle),
        ctypes.byref(info),
    )
    _close_handle(handle)
    if not ok:
        return None
    return (info.nFileIndexHigh << 32) + info.nFileIndexLow


def _is_ntfs_drive(drive: str) -> bool:
    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.windll.kernel32
    fs_name = ctypes.create_unicode_buffer(64)
    root_path = drive.rstrip("\\/") + "\\"
    ok = kernel32.GetVolumeInformationW(
        root_path,
        None,
        0,
        None,
        None,
        None,
        fs_name,
        len(fs_name),
    )
    if not ok:
        return False
    return fs_name.value.upper() == "NTFS"
