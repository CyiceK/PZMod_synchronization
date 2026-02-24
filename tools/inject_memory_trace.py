"""
内存追踪注入器 - 在关键代码路径中自动插入检查点

使用方法:
    python tools/inject_memory_trace.py <存档路径>

这将:
1. 启动内存追踪
2. 模拟地图索引重建流程
3. 在每个关键阶段创建快照
4. 生成详细的内存分析报告
"""
from __future__ import annotations

import gc
import sys
import time
import tracemalloc
from pathlib import Path
from datetime import datetime
from typing import Dict, List, Any

# 添加项目根目录
PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))


def format_size(size: int) -> str:
    for unit in ['B', 'KB', 'MB', 'GB']:
        if abs(size) < 1024:
            return f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} TB"


class MemoryCheckpoint:
    """内存检查点"""
    def __init__(self, name: str):
        self.name = name
        self.timestamp = datetime.now()
        gc.collect()
        self.snapshot = tracemalloc.take_snapshot()
        self.current, self.peak = tracemalloc.get_traced_memory()
        self.gc_stats = gc.get_stats()

    def __repr__(self):
        return f"Checkpoint({self.name}, current={format_size(self.current)}, peak={format_size(self.peak)})"


class DetailedMemoryTracer:
    """详细内存追踪器"""

    def __init__(self):
        self.checkpoints: List[MemoryCheckpoint] = []
        self.start_time = datetime.now()

    def start(self):
        """启动追踪"""
        tracemalloc.start(25)
        print(f"[Tracer] 内存追踪已启动 @ {self.start_time}")

    def checkpoint(self, name: str) -> MemoryCheckpoint:
        """创建检查点"""
        cp = MemoryCheckpoint(name)
        self.checkpoints.append(cp)

        # 实时输出
        if len(self.checkpoints) > 1:
            prev = self.checkpoints[-2]
            diff = cp.current - prev.current
            diff_str = f"+{format_size(diff)}" if diff > 0 else format_size(diff)
            print(f"[Tracer] {name}: {format_size(cp.current)} ({diff_str} since '{prev.name}')")
        else:
            print(f"[Tracer] {name}: {format_size(cp.current)}")

        return cp

    def generate_report(self) -> str:
        """生成详细报告"""
        lines = []
        lines.append("=" * 100)
        lines.append("内存追踪详细报告")
        lines.append(f"开始时间: {self.start_time}")
        lines.append(f"检查点数: {len(self.checkpoints)}")
        lines.append("=" * 100)

        # 检查点时间线
        lines.append("\n## 检查点时间线")
        lines.append("-" * 100)
        lines.append(f"{'检查点':<30} | {'当前内存':>15} | {'峰值内存':>15} | {'增量':>15}")
        lines.append("-" * 100)

        for i, cp in enumerate(self.checkpoints):
            if i > 0:
                diff = cp.current - self.checkpoints[i-1].current
                diff_str = f"+{format_size(diff)}" if diff > 0 else format_size(diff)
            else:
                diff_str = "-"
            lines.append(f"{cp.name:<30} | {format_size(cp.current):>15} | {format_size(cp.peak):>15} | {diff_str:>15}")

        # 找出内存增长最大的阶段
        if len(self.checkpoints) >= 2:
            lines.append("\n## 内存增长最大的阶段")
            lines.append("-" * 100)

            growth_stages = []
            for i in range(1, len(self.checkpoints)):
                prev = self.checkpoints[i-1]
                curr = self.checkpoints[i]
                growth = curr.current - prev.current
                growth_stages.append((prev.name, curr.name, growth))

            growth_stages.sort(key=lambda x: x[2], reverse=True)

            for prev_name, curr_name, growth in growth_stages[:10]:
                if growth > 0:
                    lines.append(f"  {prev_name} -> {curr_name}: +{format_size(growth)}")

        # 最后一个检查点的详细分析
        if self.checkpoints:
            last = self.checkpoints[-1]
            lines.append(f"\n## 最终状态 '{last.name}' 的 Top 30 内存分配")
            lines.append("-" * 100)

            top_stats = last.snapshot.statistics('lineno')
            for i, stat in enumerate(top_stats[:30], 1):
                frame = stat.traceback[0]
                lines.append(f"#{i:2d}: {format_size(stat.size):>12} ({stat.count:>6} 次) @ {frame.filename}:{frame.lineno}")

        # 对比第一个和最后一个检查点
        if len(self.checkpoints) >= 2:
            first = self.checkpoints[0]
            last = self.checkpoints[-1]

            lines.append(f"\n## 内存增长对比: '{first.name}' -> '{last.name}'")
            lines.append("-" * 100)

            top_diff = last.snapshot.compare_to(first.snapshot, 'lineno')
            for i, stat in enumerate(top_diff[:30], 1):
                if stat.size_diff <= 0:
                    continue
                frame = stat.traceback[0]
                lines.append(f"#{i:2d}: +{format_size(stat.size_diff):>12} ({stat.count_diff:>+6} 次) @ {frame.filename}:{frame.lineno}")

        return '\n'.join(lines)

    def save_report(self, filename: str = None) -> Path:
        """保存报告"""
        if filename is None:
            timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
            filename = f'memory_trace_{timestamp}.txt'

        path = PROJECT_ROOT / 'logs' / filename
        path.parent.mkdir(parents=True, exist_ok=True)

        report = self.generate_report()
        path.write_text(report, encoding='utf-8')
        print(f"\n[Tracer] 报告已保存: {path}")
        return path

    def stop(self):
        """停止追踪"""
        tracemalloc.stop()


def trace_map_scan(save_path: Path):
    """追踪地图扫描的内存使用"""

    tracer = DetailedMemoryTracer()
    tracer.start()
    tracer.checkpoint("00_初始状态")

    # 阶段 1: 导入模块
    print("\n[Phase 1] 导入模块...")
    from utils.save_map_window_utils import MapBinScanThread, ChunkDataProvider
    from services.chunk_object_parser import scan_chunk_object_summary, scan_chunk_player_build_counts
    from utils.save_version_utils import get_chunk_params
    tracer.checkpoint("01_导入模块后")

    # 阶段 2: 扫描目录
    print("\n[Phase 2] 扫描目录结构...")
    import os
    import re
    from collections import Counter

    map_entries: Dict[tuple, Path] = {}
    chunkdata_dir = save_path / "chunkdata"

    if chunkdata_dir.exists():
        for x_dir in os.scandir(chunkdata_dir):
            if not x_dir.is_dir():
                continue
            try:
                x = int(x_dir.name)
            except ValueError:
                continue
            for file_entry in os.scandir(x_dir.path):
                if not file_entry.is_file():
                    continue
                name = file_entry.name
                if not (name.endswith(".bin") or name.endswith(".map")):
                    continue
                stem = name.rsplit(".", 1)[0]
                try:
                    y = int(stem)
                except ValueError:
                    continue
                map_entries[(x, y)] = Path(file_entry.path)

    # 也扫描根目录的 map_*.bin 文件
    for entry in os.scandir(save_path):
        if not entry.is_file():
            continue
        name = entry.name
        if not name.startswith("map_") or not (name.endswith(".bin") or name.endswith(".map")):
            continue
        match = re.match(r"^map_(-?\d+)_(-?\d+)", name)
        if match:
            x = int(match.group(1))
            y = int(match.group(2))
            map_entries.setdefault((x, y), Path(entry.path))

    print(f"  发现 {len(map_entries)} 个区块文件")
    tracer.checkpoint("02_扫描目录后")

    # 阶段 3: 测试 ChunkDataProvider
    print("\n[Phase 3] 测试区块数据加载...")
    provider = ChunkDataProvider(max_in_flight=32)

    # 只加载前 100 个区块测试
    test_entries = list(map_entries.items())[:100]
    for (x, y), path in test_entries:
        data = provider.get(path)

    print(f"  加载了 {len(provider)} 个区块到缓存")
    tracer.checkpoint("03_加载区块后")

    # 阶段 4: 清理 provider
    provider.clear()
    gc.collect()
    tracer.checkpoint("04_清理provider后")

    # 阶段 5: 测试完整扫描 (小规模)
    print("\n[Phase 5] 测试区块扫描 (前50个)...")

    activity_counts: Dict[tuple, int] = {}
    build_counts: Dict[tuple, int] = {}
    player_build_counts: Dict[tuple, int] = {}
    fire_counts: Dict[tuple, int] = {}

    marker_a = 0x4745
    marker_b = 0x4E57

    test_entries = list(map_entries.items())[:50]
    for (x, y), path in test_entries:
        try:
            data = path.read_bytes()
        except Exception:
            continue

        # 扫描标记
        mv = memoryview(data)
        count = 0
        build = 0
        for idx in range(0, len(mv) - 1, 2):
            value = (mv[idx] << 8) | mv[idx + 1]
            if value == marker_a:
                count += 1
            elif value == marker_b:
                count += 1
                build += 1

        if count > 0:
            activity_counts[(x, y)] = count
        if build > 0:
            build_counts[(x, y)] = build

        # 扫描玩家建筑
        build_hits, fire_hits, _ = scan_chunk_player_build_counts(data, save_path)
        if build_hits > 0:
            player_build_counts[(x, y)] = build_hits
        if fire_hits > 0:
            fire_counts[(x, y)] = fire_hits

        # 释放数据
        del data
        del mv

    gc.collect()
    tracer.checkpoint("05_区块扫描后")

    # 阶段 6: 测试对象摘要解析
    print("\n[Phase 6] 测试对象摘要解析...")
    chunk_paths = [path for _, path in list(map_entries.items())[:16]]
    if chunk_paths:
        object_summary = scan_chunk_object_summary(
            save_path,
            chunk_paths,
            max_chunks=16,
            max_objects=5000,
        )
        print(f"  解析了 {object_summary.get('chunks', 0)} 个区块, {object_summary.get('objects', 0)} 个对象")

    gc.collect()
    tracer.checkpoint("06_对象解析后")

    # 阶段 7: 清理所有临时数据
    print("\n[Phase 7] 清理临时数据...")
    del map_entries
    del activity_counts
    del build_counts
    del player_build_counts
    del fire_counts
    del test_entries
    if 'object_summary' in dir():
        del object_summary

    gc.collect()
    tracer.checkpoint("07_清理后")

    # 生成报告
    tracer.save_report()
    tracer.stop()

    print("\n" + "=" * 80)
    print("分析完成！请查看 logs/ 目录下的报告文件")
    print("=" * 80)


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        print("\n示例存档路径:")
        print("  Windows: C:\\Users\\xxx\\Zomboid\\Saves\\Sandbox\\存档名")
        print("  Linux:   ~/Zomboid/Saves/Sandbox/存档名")
        return

    save_path = Path(sys.argv[1])
    if not save_path.exists():
        print(f"错误: 存档路径不存在: {save_path}")
        return

    print(f"分析存档: {save_path}")
    trace_map_scan(save_path)


if __name__ == "__main__":
    main()
