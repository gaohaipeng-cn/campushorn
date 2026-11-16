# -*- coding: utf-8 -*-
"""
build32.py - 针对 32 位 Windows 7 + Python 3.8.10 的打包脚本
用法：双击运行，或 python build32.py

与 build.py 的区别：
  1. 全程兼容 Python 3.8 语法（不用 X | None 注解，加了 from __future__ import annotations）
  2. 无论成功失败都会暂停显示结果，绝不"一闪而过"
  3. 异常会写入 build32_log.txt 并打印完整 traceback
  4. Pillow 安装失败时自动降级尝试 9.5.0（32 位 wheel 更全）
"""
from __future__ import annotations

import os
import sys

os.environ["PYTHONIOENCODING"] = "utf-8"
os.environ["PYTHONUTF8"] = "1"

import subprocess
import shutil
import traceback

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
LOG_FILE = os.path.join(SCRIPT_DIR, "build32_log.txt")
PYPI_MIRROR = "https://pypi.tuna.tsinghua.edu.cn/simple"

# GBK 控制台遇到无法编码的字符时用 ? 替代，避免 print 崩溃
try:
    sys.stdout.reconfigure(errors="replace")
    sys.stderr.reconfigure(errors="replace")
except Exception:
    pass


def log(msg):
    print(msg)
    try:
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(str(msg) + "\n")
    except Exception:
        pass


def run_cmd(cmd, desc):
    log("")
    log("=" * 55)
    log(">> " + desc)
    log("=" * 55)
    log("命令: " + " ".join(str(x) for x in cmd))
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            cwd=SCRIPT_DIR,
        )
        if result.stdout:
            log(result.stdout)
        if result.stderr:
            log("[STDERR]" + result.stderr)
        if result.returncode != 0:
            log("")
            log("[错误] " + desc + " 失败，退出码 " + str(result.returncode))
            return False
        log("[成功] " + desc)
        return True
    except Exception as e:
        log("[异常] " + str(e))
        traceback.print_exc()
        return False


def get_installed_version(pkg_name, python):
    """获取已安装包的版本，找不到返回 None"""
    try:
        result = subprocess.run(
            [python, "-m", "pip", "show", pkg_name],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
        )
        for line in result.stdout.splitlines():
            if line.startswith("Version:"):
                return line.split(":", 1)[1].strip()
    except Exception:
        pass
    return None


def ensure_pip(python):
    """确保 pip 可用，缺失时用 ensurepip 自动修复"""
    try:
        result = subprocess.run(
            [python, "-m", "pip", "--version"],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
        )
        if result.returncode == 0:
            log("[跳过] pip 可用：" + result.stdout.strip())
            return True
    except Exception:
        pass
    log("[修复] pip 不可用，尝试 ensurepip 自动安装...")
    ok = run_cmd([python, "-m", "ensurepip", "--upgrade"],
                 "通过 ensurepip 安装 pip")
    if not ok:
        log("[致命] pip 安装失败，无法自动安装其他依赖，请检查 Python 安装")
    return ok


def check_tkinter(python):
    """tkinter 随 Python 安装，无法用 pip 补装，缺失只能提示"""
    try:
        result = subprocess.run(
            [python, "-c", "import tkinter; print(tkinter.TkVersion)"],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
        )
        if result.returncode == 0:
            log("[跳过] tkinter 可用（Tk " + result.stdout.strip() + "）")
            return
    except Exception:
        pass
    log("[警告] 本机 Python 缺少 tkinter（安装 Python 时未勾选 tcl/tk），")
    log("       无法用 pip 补装，sender/receiver 界面将无法打包运行。")
    log("       请重新运行 Python 安装程序并勾选 'tcl/tk and IDLE'。")


def check_sources():
    """检查打包所需的源码文件是否齐全"""
    required = ["sender.py", "receiver.py", "common.py"]
    missing = [f for f in required
               if not os.path.exists(os.path.join(SCRIPT_DIR, f))]
    if missing:
        log("[致命] 缺少源码文件：" + ", ".join(missing))
        return False
    log("[跳过] 源码文件齐全（sender.py / receiver.py / common.py）")
    return True


def ensure_installed(python, pkg, ver, desc, critical=False):
    """
    检查包版本，如已安装正确版本则跳过，否则安装。
    ver 为 None 时检查包是否存在，存在则跳过。
    安装失败时自动用国内镜像重试一次。
    critical=True 表示打包必需，失败时由调用方决定是否中止。
    """
    current = get_installed_version(pkg, python)
    if ver:
        if current == ver:
            log("[跳过] " + pkg + "==" + ver + " 已安装")
            return True
        log("[安装] " + pkg + " 当前=" + (current or "未安装") + " -> 目标=" + ver)
    else:
        if current:
            log("[跳过] " + pkg + " 已安装 (" + current + ")")
            return True
        log("[安装] " + pkg + " 未安装，开始安装")

    cmd = [python, "-m", "pip", "install"]
    if ver:
        cmd.append(pkg + "==" + ver)
    else:
        cmd.append("--upgrade")
        cmd.append(pkg)
    if not run_cmd(cmd, desc):
        # 失败后用国内镜像重试（校园网/防火墙可能拦截 pypi.org）
        retry = cmd + ["-i", PYPI_MIRROR]
        if run_cmd(retry, desc + "（清华镜像重试）"):
            return True
        if critical:
            log("[致命] " + pkg + " 安装失败（含镜像重试），打包无法继续")
        return False
    return True


def clean_all_pycache():
    """清理 site-packages 下所有 .pyc 缓存（防止旧字节码干扰 PyInstaller 分析）"""
    try:
        import site
        site_packages = site.getsitepackages()[0]
    except Exception:
        log("[跳过] 无法定位 site-packages，跳过字节码清理")
        return
    removed = 0
    for root, dirs, files in os.walk(site_packages):
        for f in files:
            if f.endswith(".pyc") or f.endswith(".pyo"):
                try:
                    os.remove(os.path.join(root, f))
                    removed += 1
                except Exception:
                    pass
        for d in list(dirs):
            if d == "__pycache__":
                try:
                    shutil.rmtree(os.path.join(root, d), ignore_errors=True)
                except Exception:
                    pass
    log("已清理字节码缓存: " + str(removed) + " 个 .pyc 文件")


def check_environment():
    """环境预检，返回 False 表示无法继续"""
    log("Python 路径：" + sys.executable)
    log("Python 版本：" + sys.version)
    log("工作目录  ：" + SCRIPT_DIR)

    if sys.version_info[:2] != (3, 8):
        log("[警告] 本脚本针对 Python 3.8 调试，当前版本 "
            + ".".join(str(x) for x in sys.version_info[:3]))

    if sys.maxsize > 2 ** 32:
        log("[警告] 当前是 64 位 Python！打包出的 exe 仍是 64 位，")
        log("       无法在 32 位 Windows 7 上运行。请改用 32 位 Python 3.8.10 重新运行。")
        return False

    if os.name != "nt":
        log("[错误] 请在 Windows 上运行本脚本")
        return False
    return True


def main():
    with open(LOG_FILE, "w", encoding="utf-8") as f:
        f.write("")

    if not check_environment():
        return

    python = sys.executable

    # ── 0. 基础环境检查 ──────────────────────────────────────────
    if not check_sources():
        return
    if not ensure_pip(python):
        return
    check_tkinter(python)

    # ── 1. 依赖检查与自动安装 ────────────────────────────────────
    ok = True
    ok &= ensure_installed(python, "pyinstaller", "5.13.2",
                           "安装 pyinstaller 5.13.2（最后支持 Win7 + 32 位的大版本）",
                           critical=True)
    ok &= ensure_installed(python, "pyinstaller-hooks-contrib", "2023.10",
                           "安装 pyinstaller-hooks-contrib 2023.10",
                           critical=True)
    # setuptools 69.0.0 是支持 Python 3.8 且带 pkg_resources 的版本
    if not ensure_installed(python, "setuptools", "69.0.0",
                            "安装 setuptools 69.0.0（PyInstaller 5.13.2 需要 pkg_resources）",
                            critical=True):
        log("[警告] setuptools 69.0.0 安装失败，尝试 59.8.0（Python 3.8 时代默认版本）")
        if not ensure_installed(python, "setuptools", "59.8.0",
                                "安装 setuptools 59.8.0", critical=True):
            ok = False
    if not ok:
        log("")
        log("[中止] 关键依赖安装失败，请检查网络后重试（详见日志）")
        return

    run_cmd([python, "-m", "PyInstaller", "--version"], "确认 PyInstaller 版本")

    # Pillow 10.3.0 支持 Python 3.8；32 位 wheel 缺失时降级 9.5.0
    if not ensure_installed(python, "pillow", "10.3.0", "安装 pillow 10.3.0"):
        log("[警告] pillow 10.3.0 安装失败，尝试 9.5.0")
        if not ensure_installed(python, "pillow", "9.5.0", "安装 pillow 9.5.0"):
            log("[警告] Pillow 全部安装失败！receiver.exe 将没有系统托盘图标，")
            log("       其余功能不受影响，继续打包。")

    if not ensure_installed(python, "pystray", None, "安装 pystray"):
        log("[警告] pystray 安装失败，receiver.exe 将没有系统托盘，")
        log("       其余功能不受影响，继续打包。")

    # ── 2. 使用预置 ICO（icons/ 已不含 PNG 源图）──
    icons_dir = os.path.join(SCRIPT_DIR, "icons")
    sender_ico = os.path.join(icons_dir, "sender.ico")
    receiver_ico = os.path.join(icons_dir, "receiver.ico")

    if not os.path.exists(sender_ico) or not os.path.exists(receiver_ico):
        log("[警告] icons/ 目录缺少 ico 文件，打包出的 exe 将使用系统默认图标")
    log("sender.ico 存在: " + str(os.path.exists(sender_ico)))
    log("receiver.ico 存在: " + str(os.path.exists(receiver_ico)))

    # ── 3. 清理 ─────────────────────────────────────────────────
    clean_all_pycache()

    for d in ["build", "dist"]:
        full_d = os.path.join(SCRIPT_DIR, d)
        if os.path.isdir(full_d):
            log("清理旧目录：" + full_d + "/")
            shutil.rmtree(full_d, ignore_errors=True)
    for spec in ["sender.spec", "receiver.spec"]:
        full_spec = os.path.join(SCRIPT_DIR, spec)
        if os.path.exists(full_spec):
            os.remove(full_spec)

    # ── 4. 打包 sender.exe ──────────────────────────────────────
    sender_cmd = [
        python, "-m", "PyInstaller",
        "--clean",
        "--onefile",
        "--windowed",
        "--name=sender",
        "--add-data=common.py;.",
        "--exclude-module=pygame",
        "--exclude-module=torch",
        "--exclude-module=torchvision",
        "--exclude-module=torchaudio",
        "--exclude-module=transformers",
    ]
    if os.path.exists(sender_ico):
        sender_cmd.append("--icon=" + sender_ico)
    if os.path.isdir(os.path.join(SCRIPT_DIR, "icons")):
        sender_cmd.append("--add-data=icons;icons")
    sender_cmd.append("sender.py")
    run_cmd(sender_cmd, "打包 sender.exe")

    # ── 5. 打包 receiver.exe ────────────────────────────────────
    receiver_cmd = [
        python, "-m", "PyInstaller",
        "--clean",
        "--onefile",
        "--windowed",
        "--name=receiver",
        "--add-data=common.py;.",
        "--exclude-module=pygame",
        "--exclude-module=torch",
        "--exclude-module=torchvision",
        "--exclude-module=torchaudio",
        "--exclude-module=transformers",
        "--hidden-import=pystray._win32",
        "--hidden-import=PIL._tkinter_finder",
        "--hidden-import=PIL.Image",
        "--hidden-import=PIL.ImageDraw",
        "--hidden-import=winreg",
        "--hidden-import=sqlite3",
    ]
    if os.path.exists(receiver_ico):
        receiver_cmd.append("--icon=" + receiver_ico)
    if os.path.isdir(os.path.join(SCRIPT_DIR, "hooks")):
        receiver_cmd.append("--additional-hooks-dir=hooks")
    if os.path.isdir(os.path.join(SCRIPT_DIR, "icons")):
        receiver_cmd.append("--add-data=icons;icons")
    receiver_cmd.append("receiver.py")
    run_cmd(receiver_cmd, "打包 receiver.exe")

    # ── 6. 结果汇报 ─────────────────────────────────────────────
    dist_dir = os.path.join(SCRIPT_DIR, "dist")
    log("")
    log("=" * 55)
    log("打包流程结束，检查 dist/ 目录：")
    ok_all = True
    for name in ("sender.exe", "receiver.exe"):
        path = os.path.join(dist_dir, name)
        if os.path.exists(path):
            size_mb = os.path.getsize(path) / 1024 / 1024
            log("  OK  " + name.ljust(20) + ("%.1f MB" % size_mb))
        else:
            ok_all = False
            log("  !!  " + name.ljust(20) + " 未生成")
    log("=" * 55)
    log("完整日志已保存到：" + LOG_FILE)
    if ok_all:
        log("两个 exe 均已生成（32 位版本，可在 32 位 Win7 上运行）")


if __name__ == "__main__":
    try:
        main()
    except Exception:
        err = traceback.format_exc()
        print(err)
        try:
            with open(LOG_FILE, "a", encoding="utf-8") as f:
                f.write(err + "\n")
        except Exception:
            pass
    finally:
        # 无论成功失败都暂停，杜绝"一闪而过"
        try:
            input("\n按回车键退出...")
        except Exception:
            pass
