# PZMod Sync Tool

[English](./readme.md)

## 项目概览

PZMod Sync Tool 是一个用于 Project Zomboid 的 MOD 同步与存档/地图分析桌面工具。
它用于保持客户端 MOD 列表、服务器配置与存档分析流程的一致性。

GitHub: https://github.com/CyiceK/PZMod_synchronization

## 功能特性

- MOD 发现、筛选、排序与批量启用/禁用
- 服务器 MOD 配置同步
- 存档与地图分析工具
- 运行时日志查看与调试诊断控制
- 多语言界面（English / 简体中文 / 繁體中文）

## 页面与导航概览

主导航包含：

- 首页
- MOD 管理
- 服务器同步
- 存档管理
- 地图管理
- 软链接
- 日志
- 日志分析
- 关于
- 设置

其中“关于”页面包含：

- 项目简介
- 贡献者模板区
- 鸣谢模板区
- 参考项目模板区
- `Open GitHub` 按钮

## 快速开始

### 环境要求

- Python 3.10+
- Windows / macOS / Linux

### 安装与运行

```bash
git clone https://github.com/CyiceK/PZMod_synchronization.git
cd PZMod_synchronization
pip install -r requirements.txt
python main.py
```

## 配置说明

首次运行请在“设置”中配置以下路径：

- Workshop 路径（Steam Workshop 内容目录）
- 游戏路径（Project Zomboid 安装目录）
- 文档/存档路径（`Zomboid` 用户目录）

## 开发与测试

### 开发

```bash
pip install -r requirements.txt
python main.py
```

### 测试

```bash
python -m compileall .
pytest -q
```

建议先跑目标模块的快速用例，再执行完整回归。

## 贡献指南

欢迎提交 Issue 和 Pull Request。

建议包含：

- 可复现步骤（缺陷）
- 变更范围与理由（行为修改）
- 非平凡逻辑对应的测试

## Contributors

模板：

- Name / Handle: `<placeholder>`
- Contribution Area: `<placeholder>`

## Acknowledgements

模板：

- Thanks to `<person or project>` for `<support details>`

## References

模板：

- Referenced project: `<xxx project>` (`<url>`)

## License

项目采用 [GPL-3.0](LICENSE) 协议。
