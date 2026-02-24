# 内存泄漏分析指南

## 快速诊断

### 方法 1: 查看控制台输出 (已启用)

运行应用后，控制台会自动输出内存追踪信息：

```
[MEM] scan_bins_开始: 50.0MB (峰值:50.0MB)
[MEM] 扫描目录后_共2000个区块: 55.0MB (峰值:55.0MB) [+5.0MB]
[MEM] 区块扫描循环结束: 2500.0MB (峰值:2500.0MB) [+2445.0MB]
...
```

**看哪个阶段增长最多，那就是问题所在！**

### 方法 2: 使用 memray 生成火焰图 (推荐)

```bash
# 安装 memray
pip install memray

# 运行并记录内存分配
python -m memray run -o memory.bin main.py

# 生成火焰图 (在浏览器中打开)
python -m memray flamegraph memory.bin -o memory_flamegraph.html

# 或生成表格报告
python -m memray table memory.bin
```

### 方法 3: 使用内置分析工具

```bash
# 基础对象分析
python tools/memory_profiler.py objgraph

# 实时监控
python tools/memory_profiler.py monitor

# 针对特定存档的分析
python tools/inject_memory_trace.py "C:\Users\xxx\Zomboid\Saves\Sandbox\存档名"
```

## 禁用内存追踪

如果不需要追踪，在 `utils/save_map_window_utils.py` 中找到：

```python
_mem_debug = True  # 设为 False 可禁用内存追踪
```

改为 `False` 即可。

## 常见内存泄漏模式

1. **大字典/列表累积** - 数据不断添加但从不清理
2. **全局缓存无界增长** - 缓存没有大小限制
3. **循环引用** - 对象之间相互引用导致无法GC
4. **闭包捕获** - 闭包意外捕获了大对象

## 火焰图解读

- **宽的条** = 占用内存多
- **高的堆栈** = 调用层级深
- **红色/橙色** = 热点区域

找到最宽的条，查看它的调用栈，就能定位问题！
