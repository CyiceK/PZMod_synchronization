"""
Debug log analysis service.

Analyzes Project Zomboid debug logs and extracts mod error info.

@author: Cyicek
"""
import logging
import re
import os
import traceback
from collections import deque
from concurrent.futures import as_completed
from pathlib import Path
from datetime import datetime
from dataclasses import dataclass, field
from typing import List, Dict, Optional, Set, Tuple
from enum import Enum

from PyQt6.QtCore import QObject, pyqtSignal

from config import cfg
from services.thread_pool import get_log_executor

logger = logging.getLogger(__name__)


class ErrorSeverity(Enum):
    """Error severity."""
    ERROR = "error"
    WARNING = "warning"
    INFO = "info"


@dataclass
class StackTraceFrame:
    """Stack frame info."""
    function_name: str
    file_name: str
    line_number: int
    mod_name: str  # "Vanilla" means base game.

    @property
    def is_vanilla(self) -> bool:
        return self.mod_name.lower() == "vanilla"


@dataclass
class ModError:
    """Mod error info."""
    error_message: str
    timestamp: Optional[datetime]
    stack_frames: List[StackTraceFrame] = field(default_factory=list)
    raw_stack_trace: str = ""
    log_file: str = ""
    line_number: int = 0

    @property
    def primary_mod(self) -> Optional[str]:
        """Get primary mod causing the error (top non-vanilla frame)."""
        for frame in self.stack_frames:
            if not frame.is_vanilla:
                return frame.mod_name
        return None

    @property
    def involved_mods(self) -> Set[str]:
        """Get all involved mods."""
        return {frame.mod_name for frame in self.stack_frames if not frame.is_vanilla}

    @property
    def severity(self) -> ErrorSeverity:
        """Determine severity based on error message."""
        if "ERROR" in self.error_message.upper():
            return ErrorSeverity.ERROR
        elif "WARN" in self.error_message.upper():
            return ErrorSeverity.WARNING
        return ErrorSeverity.INFO


@dataclass
class LogFileInfo:
    """Log file info."""
    file_path: Path
    file_name: str
    timestamp: Optional[datetime]
    file_size: int
    error_count: int = 0

    @property
    def display_name(self) -> str:
        """Display name."""
        if self.timestamp:
            return self.timestamp.strftime("%Y-%m-%d %H:%M:%S")
        return self.file_name


@dataclass(frozen=True)
class LogFileSignature:
    """Log file signature for quick cache checks."""
    file_size: int
    mtime_ns: int


@dataclass
class ModErrorSummary:
    """Mod error summary."""
    mod_name: str
    error_count: int
    errors: List[ModError] = field(default_factory=list)
    first_seen: Optional[datetime] = None
    last_seen: Optional[datetime] = None


class DebugLogService(QObject):
    """
    Debug log analysis service.

    Features:
    - Scan Zomboid Logs directory
    - Parse DebugLog files
    - Extract STACK TRACE errors
    - Identify erroring mods
    - Aggregate error stats
    """

    # Signals
    scan_started = pyqtSignal()
    scan_progress = pyqtSignal(int, int)  # current, total
    scan_completed = pyqtSignal(int)  # error_count
    error_found = pyqtSignal(object)  # ModError

    # Log filename pattern: DD-MM-YY_HH-MM-SS_DebugLog.txt
    LOG_FILE_PATTERN = re.compile(
        r"(\d{2})-(\d{2})-(\d{2})_(\d{2})-(\d{2})-(\d{2})_DebugLog(?:-server)?\.txt$",
        re.IGNORECASE
    )

    # STACK TRACE parsing markers
    STACK_TRACE_START = "STACK TRACE"
    STACK_TRACE_SEPARATOR = "-" * 40

    # Stack frame pattern: function: xxx -- file: xxx line # xxx | MOD: xxx
    FRAME_PATTERN = re.compile(
        r"function:\s*(\S+)\s*--\s*file:\s*(\S+)\s*line\s*#\s*(\d+)\s*\|\s*(?:MOD:\s*)?(.+?)\.?$",
        re.IGNORECASE
    )

    # Simplified frame pattern (for "Callframe at: xxx")
    CALLFRAME_PATTERN = re.compile(r"Callframe at:\s*(.+)")

    def __init__(self):
        super().__init__()

        self._errors: List[ModError] = []
        self._log_files: List[LogFileInfo] = []
        self._mod_summaries: Dict[str, ModErrorSummary] = {}
        self._logs_path: Optional[Path] = None
        self._log_cache: Dict[Path, Tuple[LogFileSignature, List[ModError]]] = {}

    @property
    def errors(self) -> List[ModError]:
        """All errors."""
        return self._errors

    @property
    def log_files(self) -> List[LogFileInfo]:
        """All log files."""
        return self._log_files

    @property
    def mod_summaries(self) -> Dict[str, ModErrorSummary]:
        """Errors grouped by mod."""
        return self._mod_summaries

    @property
    def error_count(self) -> int:
        """Total error count."""
        return len(self._errors)

    @property
    def affected_mod_count(self) -> int:
        """Count of affected mods."""
        return len(self._mod_summaries)

    def get_logs_path(self) -> Optional[Path]:
        """Get logs directory path."""
        # Prefer configured documents path.
        document_path = cfg.get(cfg.document_path)
        if document_path:
            logs_path = Path(document_path) / "Logs"
            if logs_path.exists():
                return logs_path

        # Try default path C:\Users\<username>\Zomboid\Logs
        default_path = Path.home() / "Zomboid" / "Logs"
        if default_path.exists():
            return default_path

        return None

    def scan_log_files(self, path: Optional[Path] = None) -> List[LogFileInfo]:
        """
        Scan log files.

        Args:
            path: Log directory path; defaults to standard path

        Returns:
            List of log files
        """
        self._logs_path = path or self.get_logs_path()
        self._log_files.clear()

        if not self._logs_path or not self._logs_path.exists():
            return []

        # Scan directory.
        for item in self._logs_path.iterdir():
            if item.is_file():
                # Check for DebugLog files.
                match = self.LOG_FILE_PATTERN.match(item.name)
                if match:
                    timestamp = self._parse_log_filename(item.name)
                    file_info = LogFileInfo(
                        file_path=item,
                        file_name=item.name,
                        timestamp=timestamp,
                        file_size=item.stat().st_size
                    )
                    self._log_files.append(file_info)
            elif item.is_dir():
                # Check log files in subdirectories.
                for sub_item in item.iterdir():
                    if sub_item.is_file():
                        match = self.LOG_FILE_PATTERN.match(sub_item.name)
                        if match:
                            timestamp = self._parse_log_filename(sub_item.name)
                            file_info = LogFileInfo(
                                file_path=sub_item,
                                file_name=sub_item.name,
                                timestamp=timestamp,
                                file_size=sub_item.stat().st_size
                            )
                            self._log_files.append(file_info)

        # Sort by time (newest first).
        self._log_files.sort(key=lambda x: x.timestamp or datetime.min, reverse=True)

        return self._log_files

    def _parse_log_filename(self, filename: str) -> Optional[datetime]:
        """Parse log filename to get timestamp."""
        match = self.LOG_FILE_PATTERN.match(filename)
        if match:
            day, month, year, hour, minute, second = match.groups()
            try:
                # Year is two digits; assume 2000s.
                full_year = 2000 + int(year)
                return datetime(full_year, int(month), int(day),
                                int(hour), int(minute), int(second))
            except ValueError:
                pass
        return None

    @staticmethod
    def _get_log_signature(file_path: Path) -> Optional[LogFileSignature]:
        try:
            stat = file_path.stat()
        except OSError:
            return None
        return LogFileSignature(file_size=stat.st_size, mtime_ns=stat.st_mtime_ns)

    @staticmethod
    def _open_log_file(file_path: Path):
        encodings = ["utf-8", "gbk", "gb2312", "latin-1"]
        for encoding in encodings:
            handle = None
            try:
                handle = open(file_path, "r", encoding=encoding, errors="strict")
                handle.read(4096)
                handle.seek(0)
                return handle
            except (UnicodeDecodeError, UnicodeError):
                if handle is not None:
                    try:
                        handle.close()
                    except Exception:
                        pass
        return open(file_path, "r", encoding="utf-8", errors="replace")

    def _find_error_context(self, buffer: deque) -> Tuple[str, Optional[datetime]]:
        error_msg = ""
        error_timestamp = None
        for prev in reversed(buffer):
            prev_line = prev.strip()
            if not prev_line or prev_line.startswith("-"):
                continue
            ts_match = re.match(
                r"\[(\d{2}-\d{2}-\d{2}\s+\d{2}:\d{2}:\d{2}\.\d+)\]",
                prev_line,
            )
            if ts_match:
                try:
                    error_timestamp = datetime.strptime(
                        ts_match.group(1), "%d-%m-%y %H:%M:%S.%f"
                    )
                except ValueError:
                    pass
            clean_msg = re.sub(
                r"\[.*?\]\s*(?:LOG|ERROR|WARN)\s*:\s*\w+\s*,\s*\d+>\s*",
                "",
                prev_line,
            )
            if clean_msg and not clean_msg.startswith("-"):
                error_msg = clean_msg
                break
        return error_msg, error_timestamp

    def _read_stack_frames(self, handle, line_number: int):
        stack_frames = []
        raw_lines = []
        pending_line = None
        pending_line_no = 0

        while True:
            line = handle.readline()
            if not line:
                return stack_frames, raw_lines, None, 0, line_number
            line_number += 1
            text = line.rstrip("\n")
            if text.strip().startswith("-"):
                raw_lines.append(text)
                continue
            current = text
            current_no = line_number
            break

        while True:
            frame_line = current.strip()
            if not frame_line or frame_line.startswith("["):
                pending_line = current
                pending_line_no = current_no
                break

            raw_lines.append(current)
            frame_match = self.FRAME_PATTERN.match(frame_line)
            if frame_match:
                func_name, file_name, line_num, mod_name = frame_match.groups()
                stack_frames.append(
                    StackTraceFrame(
                        function_name=func_name,
                        file_name=file_name,
                        line_number=int(line_num),
                        mod_name=mod_name.strip(),
                    )
                )
            else:
                callframe_match = self.CALLFRAME_PATTERN.match(frame_line)
                if callframe_match:
                    stack_frames.append(
                        StackTraceFrame(
                            function_name=callframe_match.group(1),
                            file_name="",
                            line_number=0,
                            mod_name="Vanilla",
                        )
                    )

            line = handle.readline()
            if not line:
                break
            line_number += 1
            current = line.rstrip("\n")
            current_no = line_number

        return stack_frames, raw_lines, pending_line, pending_line_no, line_number

    def _update_mod_summary(self, errors: List[ModError]) -> None:
        for error in errors:
            for mod_name in error.involved_mods:
                if mod_name not in self._mod_summaries:
                    self._mod_summaries[mod_name] = ModErrorSummary(
                        mod_name=mod_name,
                        error_count=0,
                        errors=[],
                    )

                summary = self._mod_summaries[mod_name]
                summary.error_count += 1
                summary.errors.append(error)

                if error.timestamp:
                    if summary.first_seen is None or error.timestamp < summary.first_seen:
                        summary.first_seen = error.timestamp
                    if summary.last_seen is None or error.timestamp > summary.last_seen:
                        summary.last_seen = error.timestamp

    def analyze_log_file(self, file_path: Path) -> List[ModError]:
        """
        Analyze a single log file.

        Args:
            file_path: Log file path

        Returns:
            Error list
        """
        errors = []

        try:
            with self._open_log_file(file_path) as handle:
                buffer = deque(maxlen=6)
                pending_line = None
                pending_line_no = 0
                line_number = 0

                while True:
                    if pending_line is not None:
                        line = pending_line
                        line_number = pending_line_no
                        pending_line = None
                    else:
                        line = handle.readline()
                        if not line:
                            break
                        line_number += 1

                    text = line.rstrip("\n")
                    if self.STACK_TRACE_START in text:
                        stack_line_no = line_number
                        error_msg, error_timestamp = self._find_error_context(buffer)
                        (
                            stack_frames,
                            raw_lines,
                            pending_line,
                            pending_line_no,
                            line_number,
                        ) = self._read_stack_frames(handle, line_number)

                        if stack_frames or error_msg:
                            error = ModError(
                                error_message=error_msg or "Unknown error",
                                timestamp=error_timestamp,
                                stack_frames=stack_frames,
                                raw_stack_trace="\n".join(raw_lines),
                                log_file=str(file_path),
                                line_number=stack_line_no,
                            )
                            errors.append(error)
                            self.error_found.emit(error)

                        buffer.clear()
                        continue

                    buffer.append(text)

        except Exception:
            logger.error(
                "Error analyzing log file %s:\n%s",
                file_path,
                traceback.format_exc(),
            )

        return errors

    def analyze_all_logs(self, path: Optional[Path] = None) -> List[ModError]:
        """
        Analyze all log files.

        Args:
            path: Log directory path

        Returns:
            List of all errors
        """
        self._errors.clear()
        self._mod_summaries.clear()

        self.scan_started.emit()

        # Scan log files.
        log_files = self.scan_log_files(path)

        if not log_files:
            self.scan_completed.emit(0)
            return []

        total = len(log_files)
        executor = get_log_executor()
        futures = {}
        completed = 0

        current_paths = {log.file_path for log in log_files}
        if self._log_cache:
            self._log_cache = {
                path: cache for path, cache in self._log_cache.items()
                if path in current_paths
            }

        for log_file in log_files:
            signature = self._get_log_signature(log_file.file_path)
            cached = self._log_cache.get(log_file.file_path)
            if signature and cached and cached[0] == signature:
                errors = cached[1]
                log_file.error_count = len(errors)
                self._errors.extend(errors)
                self._update_mod_summary(errors)
                completed += 1
                self.scan_progress.emit(completed, total)
            else:
                futures[executor.submit(self.analyze_log_file, log_file.file_path)] = (
                    log_file,
                    signature,
                )

        for future in as_completed(futures):
            log_file, signature = futures[future]
            try:
                errors = future.result()
            except Exception:
                logger.error(
                    "Error analyzing log file %s:\n%s",
                    log_file.file_path,
                    traceback.format_exc(),
                )
                errors = []

            log_file.error_count = len(errors)
            self._errors.extend(errors)
            self._update_mod_summary(errors)

            if signature is None:
                signature = self._get_log_signature(log_file.file_path)
            if signature is not None:
                self._log_cache[log_file.file_path] = (signature, errors)

            completed += 1
            self.scan_progress.emit(completed, total)

        self.scan_completed.emit(len(self._errors))

        return self._errors

    def get_errors_by_mod(self, mod_name: str) -> List[ModError]:
        """Get errors for a specific mod."""
        return [e for e in self._errors if mod_name in e.involved_mods]

    def get_most_problematic_mods(self, limit: int = 10) -> List[ModErrorSummary]:
        """Get mods with the most errors."""
        summaries = list(self._mod_summaries.values())
        summaries.sort(key=lambda x: x.error_count, reverse=True)
        return summaries[:limit]

    def clear(self):
        """Clear analysis results."""
        self._errors.clear()
        self._log_files.clear()
        self._mod_summaries.clear()


# Global service instance
debug_log_service = DebugLogService()
