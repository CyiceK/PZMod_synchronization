# PZMod Sync Tool

<p align="center">
  <strong>Project Zomboid MOD 管理与同步解决方案</strong>
</p>

<p align="center">
  <a href="#功能特性">功能特性</a> •
  <a href="#快速开始">快速开始</a> •
  <a href="#开发指南">开发指南</a> •
  <a href="#打包发布">打包发布</a>
</p>

---

## 📖 简介

PZMod Sync Tool 是一个用于管理 Project Zomboid MOD 并在客户端与服务器之间同步 MOD 配置的工具。采用现代化的 PyQt6 + Fluent Design 界面设计，提供流畅的用户体验。

## ✨ 功能特性

### 🎮 MOD 管理
- 📋 查看和管理所有已安装的 MOD
- 🔍 搜索、筛选和排序功能
- ✅ 批量启用/禁用 MOD
- 🔗 依赖检查和缺失依赖提示
- 🖼️ MOD 图标懒加载，优化性能

### 🔄 服务器同步
- 📤 将客户端 MOD 配置同步到服务器
- 📥 从服务器下载 MOD 配置
- 🔐 支持 SFTP/FTP 连接

### 💾 存档管理
- 📁 查看和管理游戏存档
- 📦 存档备份和恢复
- 🗺️ 地图 MOD 管理

### 🌍 国际化
- 🇨🇳 简体中文
- 🇺🇸 English

### ⚡ 性能优化
- 🚀 虚拟滚动，支持大量 MOD 无卡顿
- 🖼️ 图片懒加载
- ⏳ 后台任务管理

## 🚀 快速开始

### 环境要求

- Python 3.10+
- Windows / macOS / Linux

### 安装

```bash
# 克隆项目
git clone https://github.com/your-repo/PZMod_synchronization.git
cd PZMod_synchronization

# 安装依赖
pip install -r requirements.txt

# 运行
python main.py
```

### 配置

首次运行时，请在 **设置** 页面配置以下路径：

- **Workshop 路径**: Steam Workshop 的 MOD 下载目录
  - Windows: `C:\Program Files (x86)\Steam\steamapps\workshop\content\108600`
  - macOS: `~/Library/Application Support/Steam/steamapps/workshop/content/108600`
  - Linux: `~/.steam/steam/steamapps/workshop/content/108600`

- **游戏路径**: Project Zomboid 安装目录

- **存档路径**: 游戏存档目录
  - Windows: `C:\Users\<用户名>\Zomboid`
  - macOS: `~/Zomboid`
  - Linux: `~/Zomboid`

## 📁 项目结构

```
PZMod_synchronization/
├── main.py              # 应用入口
├── main_window.py       # 主窗口
├── config.py            # 配置管理
├── components/          # UI 组件
│   ├── mod_card.py      # MOD 卡片组件
│   ├── virtual_list.py  # 虚拟滚动组件
│   └── ...
├── interfaces/          # 页面界面
│   ├── home_interface.py
│   ├── mod_interface.py
│   ├── server_interface.py
│   └── ...
├── models/              # 数据模型
│   └── mod.py
├── services/            # 业务服务
│   ├── mod_service.py
│   ├── server_service.py
│   ├── i18n.py          # 国际化
│   ├── image_loader.py  # 图片懒加载
│   └── task_manager.py  # 后台任务管理
├── resources/           # 资源文件
│   ├── icons/
│   └── i18n/
├── docs/                # 文档
├── build.py             # 打包脚本
├── pzmod-sync.spec      # PyInstaller 配置
└── requirements.txt     # 依赖列表
```

## 🛠️ 开发指南

### 技术栈

- **UI 框架**: PyQt6
- **UI 库**: QFluentWidgets (Fluent Design)
- **Python**: 3.10+

### 架构设计

项目采用分层架构：

1. **视图层 (interfaces/)**: 页面 UI 和用户交互
2. **组件层 (components/)**: 可复用的 UI 组件
3. **服务层 (services/)**: 业务逻辑处理
4. **模型层 (models/)**: 数据模型定义

### 编码规范

- 遵循 PEP 8 代码风格
- 使用类型注解
- 编写清晰的文档字符串
- 保持 KISS、DRY、SOLID 原则

## 📦 打包发布

### 使用打包脚本

```bash
# 打包所有平台
python build.py all

# 仅打包 Windows
python build.py windows

# 仅打包 macOS
python build.py macos

# 仅打包 Linux
python build.py linux
```

### 手动打包

```bash
# Windows
pyinstaller pzmod-sync.spec

# 或使用 Nuitka (更好的性能)
nuitka --standalone --enable-plugin=pyqt6 --windows-icon-from-ico=resources/icons/logo.ico main.py
```

## 📄 更新日志

### v1.0.0 (2024-xx-xx)

- 🎉 首个正式版本发布
- ✨ MOD 管理功能
- ✨ 服务器同步功能
- ✨ 存档管理功能
- ✨ 国际化支持 (中/英)
- ✨ 性能优化 (虚拟滚动、图片懒加载)

## 📜 开源协议

本项目采用 [GPL-3.0](LICENSE) 协议开源。

## 🤝 贡献

欢迎提交 Issue 和 Pull Request！

## 📬 联系方式

如有问题，请通过以下方式联系：

- GitHub Issues
- B站
- 贴吧

---

<p align="center">Made with ❤️ by PZMod Team</p>
