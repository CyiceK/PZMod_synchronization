"""Documentation translated to English.

Documentation translated to English.
1
python tools/memory_profiler.py snapshot

2. ()
python tools/memory_profiler.py monitor

3
python tools/memory_profiler.py objgraph

4. ( memray)
python -m memray run -o output.bin your_script.py
python -m memray flamegraph output.bin"""
from __future__ import annotations

import gc
import sys
import time
import tracemalloc
import linecache
from pathlib import Path
from typing import Dict, List, Optional, Tuple
from collections import Counter
from datetime import datetime

# path
PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))


def format_size(size: int) -> str:
    """Documentation translated to English."""in ['B', 'KB', 'MB', 'GB']:
        if abs(size) < 1024:
            return f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} TB"


def get_top_allocations(snapshot, limit: int = 20) -> List[str]:
    """Top N"""ts = snapshot.statistics('lineno')
    lines = []
    lines.append(f"\n{'='*80}")
    lines.append(f"Top {limit} 内存分配位置:")
    lines.append(f"{'='*80}")

    for index, stat in enumerate(top_stats[:limit], 1):
        frame = stat.traceback[0]
        lines.append(f"\n#{index}: {format_size(stat.size)} ({stat.count} 次分配)")
        lines.append(f"    文件: {frame.filename}:{frame.lineno}")

        # Comment translated to English.
        try:
            line = linecache.getline(frame.filename, frame.lineno).strip()
            if line:
                lines.append(f"    代码: {line[:80]}")
        except Exception:
            pass

    return lines


def get_top_traceback(snapshot, limit: int = 10) -> List[str]:
    """Top N"""t.statistics('traceback')
    lines = []
    lines.append(f"\n{'='*80}")
    lines.append(f"Top {limit} 内存分配调用栈:")
    lines.append(f"{'='*80}")

    for index, stat in enumerate(top_stats[:limit], 1):
        lines.append(f"\n#{index}: {format_size(stat.size)} ({stat.count} 次分配)")
        for frame in stat.traceback[:8]:  # 8
            lines.append(f"    {frame.filename}:{frame.lineno}")
            try:
                line = linecache.getline(frame.filename, frame.lineno).strip()
                if line:
                    lines.append(f"        -> {line[:60]}")
            except Exception:
                pass

    return lines


def compare_snapshots(snapshot1, snapshot2, limit: int = 20) -> List[str]:
    """Documentation translated to English.""".compare_to(snapshot1, 'lineno')
    lines = []
    lines.append(f"\n{'='*80}")
    lines.append(f"内存增长 Top {limit} (新增分配):")
    lines.append(f"{'='*80}")

    for index, stat in enumerate(top_stats[:limit], 1):
        if stat.size_diff <= 0:
            continue
        frame = stat.traceback[0]
        lines.append(f"\n#{index}: +{format_size(stat.size_diff)} (新增 {stat.count_diff} 次)")
        lines.append(f"    当前: {format_size(stat.size)} | 之前: {format_size(stat.size - stat.size_diff)}")
        lines.append(f"    文件: {frame.filename}:{frame.lineno}")
        try:
            line = linecache.getline(frame.filename, frame.lineno).strip()
            if line:
                lines.append(f"    代码: {line[:80]}")
        except Exception:
            pass

    return lines


def analyze_object_types() -> List[str]:
    """Documentation translated to English."""ype_counts: Counter = Counter()
    type_sizes: Dict[str, int] = {}

    for obj in gc.get_objects():
        try:
            obj_type = type(obj).__name__
            type_counts[obj_type] += 1
            size = sys.getsizeof(obj)
            type_sizes[obj_type] = type_sizes.get(obj_type, 0) + size
        except Exception:
            pass

    lines = []
    lines.append(f"\n{'='*80}")
    lines.append("对象类型统计 (按数量排序):")
    lines.append(f"{'='*80}")

    for obj_type, count in type_counts.most_common(30):
        size = type_sizes.get(obj_type, 0)
        lines.append(f"  {obj_type:40s}: {count:>10,} 个  ({format_size(size)})")

    lines.append(f"\n{'='*80}")
    lines.append("对象类型统计 (按大小排序):")
    lines.append(f"{'='*80}")

    sorted_by_size = sorted(type_sizes.items(), key=lambda x: x[1], reverse=True)
    for obj_type, size in sorted_by_size[:30]:
        count = type_counts[obj_type]
        lines.append(f"  {obj_type:40s}: {format_size(size):>12s}  ({count:,} 个)")

    return lines


def find_large_objects(min_size_mb: float = 1.0) -> List[str]:
    """Documentation translated to English."""llect()
    min_size = int(min_size_mb * 1024 * 1024)

    large_objects = []
    for obj in gc.get_objects():
        try:
            size = sys.getsizeof(obj)
            if size >= min_size:
                large_objects.append((type(obj).__name__, size, repr(obj)[:100]))
        except Exception:
            pass

    large_objects.sort(key=lambda x: x[1], reverse=True)

    lines = []
    lines.append(f"\n{'='*80}")
    lines.append(f"大对象 (>= {min_size_mb} MB):")
    lines.append(f"{'='*80}")

    for obj_type, size, preview in large_objects[:20]:
        lines.append(f"  {obj_type}: {format_size(size)}")
        lines.append(f"    预览: {preview}")

    if not large_objects:
        lines.append("  (未发现大对象)")

    return lines


def find_growing_dicts_and_lists() -> List[str]:
    """Documentation translated to English."""ge_containers = []

    for obj in gc.get_objects():
        try:
            if isinstance(obj, dict) and len(obj) > 1000:
                # Comment translated to English.
                keys = list(obj.keys())[:5]
                large_containers.append(('dict', len(obj), sys.getsizeof(obj), str(keys)[:80]))
            elif isinstance(obj, (list, tuple)) and len(obj) > 1000:
                # Comment translated to English.
                elem_types = set(type(x).__name__ for x in list(obj)[:10])
                large_containers.append((type(obj).__name__, len(obj), sys.getsizeof(obj), str(elem_types)))
            elif isinstance(obj, set) and len(obj) > 1000:
                large_containers.append(('set', len(obj), sys.getsizeof(obj), ''))
        except Exception:
            pass

    large_containers.sort(key=lambda x: x[1], reverse=True)

    lines = []
    lines.append(f"\n{'='*80}")
    lines.append("大型容器对象 (> 1000 元素):")
    lines.append(f"{'='*80}")

    for container_type, length, size, preview in large_containers[:20]:
        lines.append(f"  {container_type}: {length:,} 元素, {format_size(size)}")
        if preview:
            lines.append(f"    预览: {preview}")

    if not large_containers:
        lines.append("  (未发现大型容器)")

    return lines


class MemoryTracker:
    """Documentation translated to English."""  self.snapshots: List[Tuple[str, tracemalloc.Snapshot]] = []
        tracemalloc.start(25)  # 25
        print("[MemoryTracker] 启动内存追踪")

    def checkpoint(self, name: str) -> None:
        """Documentation translated to English."""c.collect()
        snapshot = tracemalloc.take_snapshot()
        self.snapshots.append((name, snapshot))

        current, peak = tracemalloc.get_traced_memory()
        print(f"[MemoryTracker] 检查点 '{name}': 当前={format_size(current)}, 峰值={format_size(peak)}")

    def report(self) -> str:
        """Documentation translated to English.""" lines = []
        lines.append(f"\n{'#'*80}")
        lines.append(f"# 内存追踪报告")
        lines.append(f"# 时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
        lines.append(f"{'#'*80}")

        if len(self.snapshots) >= 2:
            # Comment translated to English.
            first_name, first_snap = self.snapshots[0]
            last_name, last_snap = self.snapshots[-1]

            lines.append(f"\n对比: '{first_name}' -> '{last_name}'")
            lines.extend(compare_snapshots(first_snap, last_snap, limit=30))

        if self.snapshots:
            # Comment translated to English.
            last_name, last_snap = self.snapshots[-1]
            lines.append(f"\n最终状态 '{last_name}' 的内存分配:")
            lines.extend(get_top_allocations(last_snap, limit=30))
            lines.extend(get_top_traceback(last_snap, limit=15))

        # Comment translated to English.
        lines.extend(analyze_object_types())
        lines.extend(find_growing_dicts_and_lists())
        lines.extend(find_large_objects(min_size_mb=1.0))

        return '\n'.join(lines)

    def save_report(self, path: Optional[Path] = None) -> Path:
        """Documentation translated to English."""th is None:
            timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
            path = PROJECT_ROOT / 'logs' / f'memory_report_{timestamp}.txt'

        path.parent.mkdir(parents=True, exist_ok=True)
        report = self.report()
        path.write_text(report, encoding='utf-8')
        print(f"[MemoryTracker] 报告已保存: {path}")
        return path

    def stop(self) -> None:
        """Documentation translated to English.""" tracemalloc.stop()
        print("[MemoryTracker] 停止内存追踪")


def run_with_tracking(func, *args, **kwargs):
    """Documentation translated to English."""oryTracker()
    tracker.checkpoint("开始")

    try:
        result = func(*args, **kwargs)
        tracker.checkpoint("完成")
        return result
    finally:
        tracker.save_report()
        tracker.stop()


# ============================================================================
# Comment translated to English.
# ============================================================================

def profile_map_index_rebuild():
    """Documentation translated to English."""rint("地图索引重建内存分析")
    print("=" * 80)

    tracker = MemoryTracker()
    tracker.checkpoint("初始化前")

    # Comment translated to English.
    from utils.save_map_window_utils import MapBinScanThread
    tracker.checkpoint("导入模块后")

    # Comment translated to English.
    # Comment translated to English.
    if len(sys.argv) > 2:
        save_path = Path(sys.argv[2])
        if save_path.exists():
            print(f"分析存档: {save_path}")

            tracker.checkpoint("扫描前")

            # Comment translated to English.
            result = MapBinScanThread._scan_bins(save_path)

            tracker.checkpoint("扫描后")

            # Comment translated to English.
            del result
            gc.collect()

            tracker.checkpoint("清理后")
    else:
        print("用法: python tools/memory_profiler.py profile <存档路径>")
        print("示例: python tools/memory_profiler.py profile \"C:/Users/xxx/Zomboid/Saves/Sandbox/存档名\"")

    tracker.save_report()
    tracker.stop()


def realtime_monitor(interval: float = 1.0):
    """Documentation translated to English."""il

    process = psutil.Process()
    print("实时内存监控 (Ctrl+C 停止)")
    print("-" * 60)
    print(f"{'时间':^12} | {'RSS':^12} | {'VMS':^12} | {'变化':^12}")
    print("-" * 60)

    last_rss = 0
    try:
        while True:
            mem = process.memory_info()
            rss = mem.rss
            vms = mem.vms
            diff = rss - last_rss if last_rss else 0

            timestamp = datetime.now().strftime('%H:%M:%S')
            diff_str = f"+{format_size(diff)}" if diff > 0 else format_size(diff)

            print(f"{timestamp:^12} | {format_size(rss):^12} | {format_size(vms):^12} | {diff_str:^12}")

            last_rss = rss
            time.sleep(interval)
    except KeyboardInterrupt:
        print("\n监控停止")


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        return

    command = sys.argv[1]

    if command == "snapshot":
        # Comment translated to English.
        tracemalloc.start(25)
        gc.collect()
        snapshot = tracemalloc.take_snapshot()

        for line in get_top_allocations(snapshot, limit=30):
            print(line)
        for line in get_top_traceback(snapshot, limit=15):
            print(line)
        for line in analyze_object_types():
            print(line)

        tracemalloc.stop()

    elif command == "monitor":
        realtime_monitor()

    elif command == "objgraph":
        for line in analyze_object_types():
            print(line)
        for line in find_growing_dicts_and_lists():
            print(line)
        for line in find_large_objects():
            print(line)

    elif command == "profile":
        profile_map_index_rebuild()

    else:
        print(f"未知命令: {command}")
        print(__doc__)


if __name__ == "__main__":
    main()
