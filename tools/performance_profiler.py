# -*- coding: utf-8 -*-
"""Documentation translated to English.

Documentation translated to English.
Documentation translated to English.
Documentation translated to English.
Documentation translated to English.

@author: Cyicek"""
from __future__ import annotations

import time
import json
import statistics
import sys
from typing import Dict, List, Optional, Callable, Any, Tuple
from dataclasses import dataclass, field, asdict
from pathlib import Path
from collections import defaultdict
import threading

# Comment translated to English.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

try:
    import psutil
    _HAS_PSUTIL = True
except ImportError:
    psutil = None
    _HAS_PSUTIL = False

from services.adaptive_resource_manager import (
    get_resource_manager, PerformanceProfile, PerformanceConfig
)


@dataclass
class BenchmarkResult:
    """Documentation translated to English."""tr
    duration_ms: float
    iterations: int
    avg_time_ms: float
    min_time_ms: float
    max_time_ms: float
    std_dev_ms: float
    memory_before_mb: float
    memory_after_mb: float
    memory_delta_mb: float
    timestamp: float = field(default_factory=time.time)


@dataclass
class ProfileComparison:
    """Documentation translated to English."""rformanceProfile
    config: PerformanceConfig
    results: List[BenchmarkResult] = field(default_factory=list)
    total_time_ms: float = 0.0
    avg_fps: float = 0.0
    cache_hit_rate: float = 0.0


@dataclass
class PerformanceReport:
    """Documentation translated to English."""le: str
    description: str
    created_at: str
    system_info: Dict[str, Any]
    comparisons: List[ProfileComparison]
    recommendations: List[str]

    def to_dict(self) -> Dict[str, Any]:
        """Documentation translated to English."""eturn {
            "title": self.title,
            "description": self.description,
            "created_at": self.created_at,
            "system_info": self.system_info,
            "comparisons": [
                {
                    "profile": c.profile.value,
                    "config": asdict(c.config),
                    "results": [asdict(r) for r in c.results],
                    "total_time_ms": c.total_time_ms,
                    "avg_fps": c.avg_fps,
                    "cache_hit_rate": c.cache_hit_rate,
                }
                for c in self.comparisons
            ],
            "recommendations": self.recommendations,
        }


class PerformanceProfiler:
    """Documentation translated to English.

Documentation translated to English.
Documentation translated to English.
Documentation translated to English.
Documentation translated to English."""

    def __init__(self):
        self._results: Dict[str, List[float]] = defaultdict(list)
        self._benchmarks: List[BenchmarkResult] = []
        self._lock = threading.RLock()
        self._memory_before: Optional[float] = None

    def profile(self, name: Optional[str] = None):
        """/

Documentation translated to English.
#
@profiler.profile("my_function")
def my_function()
pass

#
with profiler.profile("code_block")
#
pass"""
        return _ProfileContext(self, name)

    def measure(self, name: str, iterations: int = 1) -> Callable:
        """Documentation translated to English.

Args
name
iterations

Returns
Documentation translated to English."""
        def decorator(func: Callable) -> Callable:
            def wrapper(*args, **kwargs):
                times: List[float] = []
                memory_before = self._get_memory_mb()

                for _ in range(iterations):
                    start = time.perf_counter()
                    result = func(*args, **kwargs)
                    end = time.perf_counter()
                    times.append((end - start) * 1000)  # Comment translated to English.
                    return result

                memory_after = self._get_memory_mb()

                result = BenchmarkResult(
                    name=name,
                    duration_ms=sum(times),
                    iterations=iterations,
                    avg_time_ms=statistics.mean(times),
                    min_time_ms=min(times),
                    max_time_ms=max(times),
                    std_dev_ms=statistics.stdev(times) if len(times) > 1 else 0.0,
                    memory_before_mb=memory_before,
                    memory_after_mb=memory_after,
                    memory_delta_mb=memory_after - memory_before,
                )

                with self._lock:
                    self._benchmarks.append(result)
                    self._results[name].extend(times)

                return result
            return wrapper
        return decorator

    def compare_profiles(
        self,
        test_func: Callable,
        profiles: Optional[List[PerformanceProfile]] = None,
        iterations: int = 5
    ) -> List[ProfileComparison]:
        """Documentation translated to English.

Args
test_func
profiles
iterations

Returns
Documentation translated to English."""
        if profiles is None:
            profiles = [
                PerformanceProfile.LOW,
                PerformanceProfile.MEDIUM,
                PerformanceProfile.HIGH,
                PerformanceProfile.ULTRA,
            ]

        comparisons: List[ProfileComparison] = []
        manager = get_resource_manager()

        for profile in profiles:
            print(f"\n测试性能档案: {profile.value.upper()}")
            print("-" * 50)

            # Comment translated to English.
            manager.set_profile(profile)
            config = manager.get_config()

            comparison = ProfileComparison(
                profile=profile,
                config=config,
            )

            # Comment translated to English.
            print("  预热中...")
            for _ in range(2):
                test_func()

            # Comment translated to English.
            print(f"  运行 {iterations} 次测试...")
            for i in range(iterations):
                memory_before = self._get_memory_mb()
                start = time.perf_counter()

                test_func()

                end = time.perf_counter()
                memory_after = self._get_memory_mb()

                duration_ms = (end - start) * 1000
                comparison.results.append(BenchmarkResult(
                    name=f"{profile.value}_run_{i+1}",
                    duration_ms=duration_ms,
                    iterations=1,
                    avg_time_ms=duration_ms,
                    min_time_ms=duration_ms,
                    max_time_ms=duration_ms,
                    std_dev_ms=0.0,
                    memory_before_mb=memory_before,
                    memory_after_mb=memory_after,
                    memory_delta_mb=memory_after - memory_before,
                ))

                print(f"    第 {i+1} 次: {duration_ms:.2f}ms")

            # Comment translated to English.
            times = [r.duration_ms for r in comparison.results]
            comparison.total_time_ms = sum(times)
            comparison.avg_fps = 1000 / (statistics.mean(times)) if times else 0.0

            comparisons.append(comparison)

        return comparisons

    def generate_report(
        self,
        title: str = "性能分析报告",
        description: str = "",
        output_path: Optional[Path] = None
    ) -> PerformanceReport:
        """Documentation translated to English.

Args
title
description
output_path

Returns
Documentation translated to English."""
        from datetime import datetime

        # Comment translated to English.
        system_info = self._get_system_info()

        # Comment translated to English.
        recommendations = self._generate_recommendations()

        report = PerformanceReport(
            title=title,
            description=description,
            created_at=datetime.now().isoformat(),
            system_info=system_info,
            comparisons=[],  # compare_profiles
            recommendations=recommendations,
        )

        # Comment translated to English.
        if output_path:
            output_path = Path(output_path)
            output_path.parent.mkdir(parents=True, exist_ok=True)
            with open(output_path, 'w', encoding='utf-8') as f:
                json.dump(report.to_dict(), f, indent=2, ensure_ascii=False)
            print(f"\n报告已保存: {output_path}")

        return report

    def print_summary(self):
        """Documentation translated to English."""nt("\n" + "=" * 60)
        print("性能测试摘要")
        print("=" * 60)

        with self._lock:
            if not self._benchmarks:
                print("暂无测试结果")
                return

            for result in self._benchmarks:
                print(f"\n测试: {result.name}")
                print(f"  迭代次数: {result.iterations}")
                print(f"  平均时间: {result.avg_time_ms:.2f}ms")
                print(f"  最短时间: {result.min_time_ms:.2f}ms")
                print(f"  最长时间: {result.max_time_ms:.2f}ms")
                print(f"  标准差: {result.std_dev_ms:.2f}ms")
                print(f"  内存变化: {result.memory_delta_mb:+.2f}MB")

    def export_json(self, path: Path) -> None:
        """JSON"""ath = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)

        with self._lock:
            data = {
                "benchmarks": [asdict(b) for b in self._benchmarks],
                "results": dict(self._results),
            }

        with open(path, 'w', encoding='utf-8') as f:
            json.dump(data, f, indent=2, ensure_ascii=False)

        print(f"结果已导出: {path}")

    def clear(self):
        """Documentation translated to English."""h self._lock:
            self._results.clear()
            self._benchmarks.clear()

    def _get_memory_mb(self) -> float:
        """MB"""TIL and psutil:
            try:
                proc = psutil.Process()
                return proc.memory_info().rss / 1024 / 1024
            except Exception:
                pass
        return 0.0

    def _get_system_info(self) -> Dict[str, Any]:
        """Documentation translated to English."""o = {
            "python_version": sys.version,
            "platform": sys.platform,
        }

        if _HAS_PSUTIL and psutil:
            try:
                mem = psutil.virtual_memory()
                info.update({
                    "cpu_count": psutil.cpu_count(),
                    "cpu_freq_mhz": psutil.cpu_freq().current if psutil.cpu_freq() else None,
                    "total_memory_gb": mem.total / 1024 / 1024 / 1024,
                    "available_memory_gb": mem.available / 1024 / 1024 / 1024,
                })
            except Exception:
                pass

        return info

    def _generate_recommendations(self) -> List[str]:
        """Documentation translated to English."""ndations: List[str] = []

        with self._lock:
            if not self._benchmarks:
                return recommendations

            # Comment translated to English.
            avg_times = [b.avg_time_ms for b in self._benchmarks]
            if avg_times:
                overall_avg = statistics.mean(avg_times)

                if overall_avg > 33:  # < 30fps
                    recommendations.append(
                        "渲染性能较低，建议降低性能档案到 MEDIUM 或 LOW"
                    )
                elif overall_avg > 16:  # < 60fps
                    recommendations.append(
                        "渲染性能一般，可以尝试优化或降低部分效果"
                    )
                else:
                    recommendations.append(
                        "渲染性能良好，可以尝试提高性能档案以获得更好体验"
                    )

            # Comment translated to English.
            memory_deltas = [b.memory_delta_mb for b in self._benchmarks]
            if memory_deltas:
                avg_memory = statistics.mean(memory_deltas)
                if avg_memory > 100:
                    recommendations.append(
                        f"内存占用较高（平均 {avg_memory:.1f}MB），建议检查内存泄漏"
                    )

        return recommendations


class _ProfileContext:
    """Documentation translated to English."""elf, profiler: PerformanceProfiler, name: Optional[str] = None):
        self.profiler = profiler
        self.name = name
        self.start_time: Optional[float] = None

    def __enter__(self):
        self.start_time = time.perf_counter()
        self.profiler._memory_before = self.profiler._get_memory_mb()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        if self.start_time is not None:
            duration = (time.perf_counter() - self.start_time) * 1000
            name = self.name or f"block_{len(self.profiler._results)}"

            with self.profiler._lock:
                self.profiler._results[name].append(duration)

            print(f"[{name}] 执行时间: {duration:.2f}ms")

        return False

    def __call__(self, func: Callable) -> Callable:
        """Documentation translated to English."""rapper(*args, **kwargs):
            with self:
                return func(*args, **kwargs)
        return wrapper


# Comment translated to English.
_profiler: Optional[PerformanceProfiler] = None


def get_profiler() -> PerformanceProfiler:
    """Documentation translated to English."""    if _profiler is None:
        _profiler = PerformanceProfiler()
    return _profiler


def profile(name: Optional[str] = None):
    """/"""(name)


def measure(name: str, iterations: int = 1) -> Callable:
    """Documentation translated to English."""er().measure(name, iterations)


def run_benchmark(
    test_func: Callable,
    name: str = "benchmark",
    iterations: int = 10,
    warmup: int = 3
) -> Dict[str, Any]:
    """Documentation translated to English.

Args
test_func
name
iterations
warmup

Returns
Documentation translated to English."""
    print(f"\n运行基准测试: {name}")
    print(f"  预热: {warmup} 次")
    print(f"  测试: {iterations} 次")
    print("-" * 50)

    profiler = get_profiler()

    # Comment translated to English.
    for i in range(warmup):
        test_func()
        print(f"    预热 {i+1}/{warmup}")

    # Comment translated to English.
    times: List[float] = []
    memory_before = profiler._get_memory_mb()

    for i in range(iterations):
        start = time.perf_counter()
        test_func()
        end = time.perf_counter()
        duration_ms = (end - start) * 1000
        times.append(duration_ms)
        print(f"    迭代 {i+1}/{iterations}: {duration_ms:.2f}ms")

    memory_after = profiler._get_memory_mb()

    # Comment translated to English.
    result = {
        "name": name,
        "iterations": iterations,
        "avg_ms": statistics.mean(times),
        "min_ms": min(times),
        "max_ms": max(times),
        "std_dev_ms": statistics.stdev(times) if len(times) > 1 else 0.0,
        "median_ms": statistics.median(times),
        "memory_delta_mb": memory_after - memory_before,
    }

    print(f"\n结果:")
    print(f"  平均: {result['avg_ms']:.2f}ms")
    print(f"  最小: {result['min_ms']:.2f}ms")
    print(f"  最大: {result['max_ms']:.2f}ms")
    print(f"  标准差: {result['std_dev_ms']:.2f}ms")
    print(f"  内存变化: {result['memory_delta_mb']:+.2f}MB")

    return result


def compare_before_after(
    before_func: Callable,
    after_func: Callable,
    name: str = "comparison",
    iterations: int = 10
) -> Dict[str, Any]:
    """Documentation translated to English.

Args
before_func
after_func
name
iterations

Returns
Documentation translated to English."""
    print(f"\n性能对比测试: {name}")
    print("=" * 60)

    # Comment translated to English.
    print("\n[优化前]")
    before_result = run_benchmark(before_func, f"{name}_before", iterations)

    # Comment translated to English.
    print("\n[优化后]")
    after_result = run_benchmark(after_func, f"{name}_after", iterations)

    # Comment translated to English.
    improvement = (
        (before_result['avg_ms'] - after_result['avg_ms']) /
        before_result['avg_ms'] * 100
    ) if before_result['avg_ms'] > 0 else 0

    speedup = (
        before_result['avg_ms'] / after_result['avg_ms']
    ) if after_result['avg_ms'] > 0 else 1.0

    print("\n" + "=" * 60)
    print("对比总结")
    print("=" * 60)
    print(f"性能提升: {improvement:.1f}%")
    print(f"加速比: {speedup:.2f}x")

    return {
        "name": name,
        "before": before_result,
        "after": after_result,
        "improvement_percent": improvement,
        "speedup": speedup,
    }


# CLI
if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="性能分析工具")
    parser.add_argument(
        "--profile",
        choices=["low", "medium", "high", "ultra", "all"],
        default="all",
        help="性能档案测试"
    )
    parser.add_argument(
        "--iterations",
        type=int,
        default=5,
        help="每个档案的测试次数"
    )
    parser.add_argument(
        "--output",
        type=str,
        help="输出报告路径"
    )

    args = parser.parse_args()

    print("性能分析工具")
    print("=" * 60)

    # Comment translated to English.
    def sample_test():
        """Documentation translated to English."""  for i in range(100000):
            result += i * i
        return result

    # Comment translated to English.
    profiler = get_profiler()

    if args.profile == "all":
        profiles = [
            PerformanceProfile.LOW,
            PerformanceProfile.MEDIUM,
            PerformanceProfile.HIGH,
            PerformanceProfile.ULTRA,
        ]
    else:
        profiles = [PerformanceProfile(args.profile)]

    comparisons = profiler.compare_profiles(
        sample_test,
        profiles=profiles,
        iterations=args.iterations
    )

    # Comment translated to English.
    report = profiler.generate_report(
        title="性能档案对比报告",
        description=f"测试迭代次数: {args.iterations}",
        output_path=Path(args.output) if args.output else None
    )
    report.comparisons = comparisons

    # Comment translated to English.
    print("\n" + "=" * 60)
    print("性能档案对比结果")
    print("=" * 60)

    for comp in comparisons:
        avg_time = statistics.mean([r.duration_ms for r in comp.results])
        print(f"\n{comp.profile.value.upper()}:")
        print(f"  平均时间: {avg_time:.2f}ms")
        print(f"  预估 FPS: {1000/avg_time:.1f}")
        print(f"  内存缓存: {comp.config.tile_cache_size} tiles")
        print(f"  渲染线程: {comp.config.render_threads}")

    profiler.print_summary()
