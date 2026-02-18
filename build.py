#!/usr/bin/env python3
"""
PZMod Sync Tool - build script.

Packages the application for different platforms.

@author: Cyicek

Usage:
    python build.py [platform]

    platform:
        windows - Build Windows executable
        macos   - Build macOS app
        linux   - Build Linux AppImage
        all     - Build all platforms (default)
"""
import os
import sys
import shutil
import subprocess
from pathlib import Path


# Version info
VERSION = "1.0.0"
APP_NAME = "PZMod Sync"

# Paths
ROOT_DIR = Path(__file__).parent
DIST_DIR = ROOT_DIR / "dist"
BUILD_DIR = ROOT_DIR / "build"


def clean():
    """Clean build directories."""
    print("🧹 清理构建目录...")

    if DIST_DIR.exists():
        shutil.rmtree(DIST_DIR)

    if BUILD_DIR.exists():
        shutil.rmtree(BUILD_DIR)

    print("✅ 清理完成")


def build_windows():
    """Build Windows version."""
    print("🪟 开始打包 Windows 版本...")

    # Build with PyInstaller.
    cmd = [
        sys.executable, "-m", "PyInstaller",
        "--clean",
        "--noconfirm",
        "pzmod-sync.spec"
    ]

    result = subprocess.run(cmd, cwd=ROOT_DIR)

    if result.returncode == 0:
        print("✅ Windows 版本打包成功")
        print(f"   输出目录: {DIST_DIR / APP_NAME}")
    else:
        print("❌ Windows 版本打包失败")
        return False

    return True


def build_macos():
    """Build macOS version."""
    if sys.platform != "darwin":
        print("⚠️ 跳过 macOS 打包 (当前不在 macOS 系统)")
        return True

    print("🍎 开始打包 macOS 版本...")

    # Build with PyInstaller.
    cmd = [
        sys.executable, "-m", "PyInstaller",
        "--clean",
        "--noconfirm",
        "pzmod-sync.spec"
    ]

    result = subprocess.run(cmd, cwd=ROOT_DIR)

    if result.returncode == 0:
        print("✅ macOS 版本打包成功")
        print(f"   输出目录: {DIST_DIR / f'{APP_NAME}.app'}")
    else:
        print("❌ macOS 版本打包失败")
        return False

    return True


def build_linux():
    """Build Linux AppImage."""
    if sys.platform != "linux":
        print("⚠️ 跳过 Linux 打包 (当前不在 Linux 系统)")
        return True

    print("🐧 开始打包 Linux AppImage...")

    # First build with PyInstaller.
    cmd = [
        sys.executable, "-m", "PyInstaller",
        "--clean",
        "--noconfirm",
        "pzmod-sync.spec"
    ]

    result = subprocess.run(cmd, cwd=ROOT_DIR)

    if result.returncode != 0:
        print("❌ Linux 版本打包失败")
        return False

    # Create AppDir layout.
    appdir = DIST_DIR / f"{APP_NAME}.AppDir"
    appdir.mkdir(parents=True, exist_ok=True)

    # Copy files.
    src_dir = DIST_DIR / APP_NAME
    if src_dir.exists():
        shutil.copytree(src_dir, appdir / "usr" / "bin", dirs_exist_ok=True)

    # Create desktop file.
    desktop_content = f"""[Desktop Entry]
Name={APP_NAME}
Exec=pzmod-sync
Icon=pzmod-sync
Type=Application
Categories=Utility;Game;
"""
    (appdir / f"{APP_NAME}.desktop").write_text(desktop_content)

    # Create AppRun.
    apprun_content = """#!/bin/bash
HERE="$(dirname "$(readlink -f "${0}")")"
exec "${HERE}/usr/bin/PZMod Sync" "$@"
"""
    apprun_path = appdir / "AppRun"
    apprun_path.write_text(apprun_content)
    apprun_path.chmod(0o755)

    print("✅ Linux AppDir 创建成功")
    print(f"   输出目录: {appdir}")
    print("   提示: 请使用 appimagetool 生成最终的 AppImage 文件")

    return True


def create_installer_windows():
    """Create Windows installer (using NSIS or Inno Setup)."""
    print("📦 创建 Windows 安装包...")

    # Check for Inno Setup.
    inno_path = Path(r"C:\Program Files (x86)\Inno Setup 6\ISCC.exe")

    if not inno_path.exists():
        print("⚠️ 未找到 Inno Setup，跳过安装包创建")
        print("   请安装 Inno Setup 6 以创建 Windows 安装包")
        return True

    # Create Inno Setup script.
    iss_content = f"""
#define MyAppName "{APP_NAME}"
#define MyAppVersion "{VERSION}"
#define MyAppPublisher "PZMod Team"
#define MyAppExeName "PZMod Sync.exe"

[Setup]
AppId={{{{A1B2C3D4-E5F6-7890-ABCD-EF1234567890}}}}
AppName={{#MyAppName}}
AppVersion={{#MyAppVersion}}
AppPublisher={{#MyAppPublisher}}
DefaultDirName={{autopf}}\\{{#MyAppName}}
DefaultGroupName={{#MyAppName}}
OutputDir={DIST_DIR}
OutputBaseFilename=PZMod-Sync-Setup-{VERSION}
Compression=lzma2
SolidCompression=yes
WizardStyle=modern

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"
Name: "chinesesimplified"; MessagesFile: "compiler:Languages\\ChineseSimplified.isl"

[Tasks]
Name: "desktopicon"; Description: "{{cm:CreateDesktopIcon}}"; GroupDescription: "{{cm:AdditionalIcons}}"; Flags: unchecked

[Files]
Source: "{DIST_DIR / APP_NAME}\\*"; DestDir: "{{app}}"; Flags: ignoreversion recursesubdirs

[Icons]
Name: "{{group}}\\{{#MyAppName}}"; Filename: "{{app}}\\{{#MyAppExeName}}"
Name: "{{autodesktop}}\\{{#MyAppName}}"; Filename: "{{app}}\\{{#MyAppExeName}}"; Tasks: desktopicon

[Run]
Filename: "{{app}}\\{{#MyAppExeName}}"; Description: "{{cm:LaunchProgram,{{#StringChange(MyAppName, '&', '&&')}}}}"; Flags: nowait postinstall skipifsilent
"""

    iss_path = ROOT_DIR / "installer.iss"
    iss_path.write_text(iss_content, encoding='utf-8')

    # Run Inno Setup.
    cmd = [str(inno_path), str(iss_path)]
    result = subprocess.run(cmd)

    if result.returncode == 0:
        print("✅ Windows 安装包创建成功")
        print(f"   输出文件: {DIST_DIR / f'PZMod-Sync-Setup-{VERSION}.exe'}")
    else:
        print("❌ Windows 安装包创建失败")
        return False

    return True


def main():
    """Main entry."""
    platform = sys.argv[1] if len(sys.argv) > 1 else "all"

    print(f"🚀 PZMod Sync Tool 打包脚本 v{VERSION}")
    print(f"   目标平台: {platform}")
    print()

    # Clean.
    clean()
    print()

    # Build.
    success = True

    if platform in ("windows", "all"):
        if not build_windows():
            success = False
        print()

    if platform in ("macos", "all"):
        if not build_macos():
            success = False
        print()

    if platform in ("linux", "all"):
        if not build_linux():
            success = False
        print()

    # Create installer (Windows only).
    if platform in ("windows", "all") and sys.platform == "win32":
        create_installer_windows()
        print()

    # Result.
    if success:
        print("🎉 打包完成!")
    else:
        print("⚠️ 打包过程中出现错误，请检查日志")
        sys.exit(1)


if __name__ == "__main__":
    main()
