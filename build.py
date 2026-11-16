# -*- coding: utf-8 -*-
"""
build.py - 自动安装依赖并打包 sender.exe / receiver.exe
用法：python build.py
"""
import os
import sys

os.environ["PYTHONIOENCODING"] = "utf-8"
os.environ["PYTHONUTF8"] = "1"

import subprocess
import shutil
import traceback

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
LOG_FILE = os.path.join(SCRIPT_DIR, "build_log.txt")
PYPI_MIRROR = "https://pypi.tuna.tsinghua.edu.cn/simple"


def log(msg: str):
    print(msg)
    with open(LOG_FILE, "a", encoding="utf-8") as f:
        f.write(msg + "\n")


def run_cmd(cmd: list, desc: str) -> bool:
    log(f"\n{'='*55}")
    log(f"▶ {desc}")
    log(f"{'='*55}")
    log(f"命令: {' '.join(str(x) for x in cmd)}")
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
            log(f"\n[错误] {desc} 失败，退出码 {result.returncode}")
            return False
        log(f"[成功] {desc}")
        return True
    except Exception as e:
        log(f"[异常] {e}")
        traceback.print_exc()
        return False


def get_installed_version(pkg_name: str, python: str) -> str | None:
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


def ensure_pip(python: str) -> bool:
    """确保 pip 可用，缺失时用 ensurepip 自动修复"""
    try:
        result = subprocess.run(
            [python, "-m", "pip", "--version"],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
        )
        if result.returncode == 0:
            log(f"[跳过] pip 可用：{result.stdout.strip()}")
            return True
    except Exception:
        pass
    log("[修复] pip 不可用，尝试 ensurepip 自动安装...")
    ok = run_cmd([python, "-m", "ensurepip", "--upgrade"], "通过 ensurepip 安装 pip")
    if not ok:
        log("[致命] pip 安装失败，无法自动安装其他依赖，请检查 Python 安装")
    return ok


def check_tkinter(python: str):
    """tkinter 随 Python 安装，无法用 pip 补装，缺失只能提示"""
    try:
        result = subprocess.run(
            [python, "-c", "import tkinter; print(tkinter.TkVersion)"],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
        )
        if result.returncode == 0:
            log(f"[跳过] tkinter 可用（Tk {result.stdout.strip()}）")
            return
    except Exception:
        pass
    log("[警告] 本机 Python 缺少 tkinter（安装 Python 时未勾选 tcl/tk），")
    log("       无法用 pip 补装，sender/receiver 界面将无法打包运行。")
    log("       请重新运行 Python 安装程序并勾选 'tcl/tk and IDLE'。")


def check_sources() -> bool:
    """检查打包所需的源码文件是否齐全"""
    required = ["sender.py", "receiver.py", "common.py"]
    missing = [f for f in required
               if not os.path.exists(os.path.join(SCRIPT_DIR, f))]
    if missing:
        log("[致命] 缺少源码文件：" + ", ".join(missing))
        return False
    log("[跳过] 源码文件齐全（sender.py / receiver.py / common.py）")
    return True


def ensure_installed(python: str, pkg: str, ver: str | None, desc: str,
                     critical: bool = False) -> bool:
    """
    检查包版本，如已安装正确版本则跳过，否则安装。
    ver 为 None 时检查包是否存在，存在则跳过。
    安装失败时自动用国内镜像重试一次。
    critical=True 表示打包必需，失败时由调用方决定是否中止。
    """
    current = get_installed_version(pkg, python)
    if ver:
        if current == ver:
            log(f"[跳过] {pkg}=={ver} 已安装")
            return True
        log(f"[安装] {pkg} 当前={current or '未安装'} → 目标={ver}")
    else:
        if current:
            log(f"[跳过] {pkg} 已安装 ({current})")
            return True
        log(f"[安装] {pkg} 未安装，开始安装")

    cmd = [python, "-m", "pip", "install"]
    if ver:
        cmd.append(f"{pkg}=={ver}")
    else:
        cmd.append("--upgrade")
        cmd.append(pkg)
    if not run_cmd(cmd, desc):
        # 失败后用国内镜像重试（校园网/防火墙可能拦截 pypi.org）
        retry = cmd + ["-i", PYPI_MIRROR]
        if run_cmd(retry, f"{desc}（清华镜像重试）"):
            return True
        if critical:
            log(f"[致命] {pkg} 安装失败（含镜像重试），打包无法继续")
        return False
    return True


def clean_all_pycache():
    """
    删除 site-packages 下所有 __pycache__ 和 .pyc/.pyo 文件。
    原因：torch/pillow 等用 Python 3.12+ 编译的包，其 .pyc 文件与 Python 3.10 的
    dis.get_instructions() 不兼容，会导致 PyInstaller 字节码分析时 IndexError 崩溃。
    """
    import site
    site_packages = site.getsitepackages()[0]
    removed_pyc = 0
    removed_dirs = 0
    for root, dirs, files in os.walk(site_packages):
        for f in files:
            if f.endswith(".pyc") or f.endswith(".pyo"):
                try:
                    os.remove(os.path.join(root, f))
                    removed_pyc += 1
                except Exception:
                    pass
        for d in list(dirs):
            if d == "__pycache__":
                try:
                    shutil.rmtree(os.path.join(root, d), ignore_errors=True)
                    removed_dirs += 1
                except Exception:
                    pass
    log(f"已清理字节码缓存: {removed_pyc} 个 .pyc 文件, {removed_dirs} 个 __pycache__ 目录")


def main():
    with open(LOG_FILE, "w", encoding="utf-8") as f:
        f.write("")

    python = sys.executable
    log(f"Python 路径：{python}")
    log(f"Python 版本：{sys.version}")
    log(f"工作目录  ：{SCRIPT_DIR}")

    # ── 0. 基础环境检查 ──────────────────────────────────────────
    if not check_sources():
        return
    if not ensure_pip(python):
        return
    check_tkinter(python)

    # ── 1. 依赖检查与自动安装 ────────────────────────────────────
    # 关键：setuptools 82+ 删除了 pkg_resources，但 PyInstaller 5.13.2 需要它
    ok = True
    ok &= ensure_installed(python, "pyinstaller", "5.13.2",
                           "安装 pyinstaller 5.13.2", critical=True)
    ok &= ensure_installed(python, "pyinstaller-hooks-contrib", "2023.10",
                           "安装 pyinstaller-hooks-contrib 2023.10", critical=True)
    ok &= ensure_installed(python, "setuptools", "69.0.0",
                           "降级 setuptools 到 69.0.0（修复 pkg_resources 兼容性）",
                           critical=True)
    if not ok:
        log("\n[中止] 关键依赖安装失败，请检查网络后重试（详见日志）")
        return

    # 确认版本
    run_cmd([python, "-m", "PyInstaller", "--version"], "确认 PyInstaller 版本")

    # 安装其他依赖
    # 关键：Pillow 12.x 是 Python 3.12+ 编译的，其 .pyc 与 Python 3.10 的 dis 模块不兼容
    # 降级到 10.3.0（Python 3.10 官方 wheel）
    if not ensure_installed(python, "pillow", "10.3.0", "安装 pillow 10.3.0"):
        log("[警告] Pillow 安装失败，receiver.exe 将没有系统托盘图标，")
        log("       其余功能不受影响，继续打包")
    if not ensure_installed(python, "pystray", None, "安装 pystray（最新）"):
        log("[警告] pystray 安装失败，receiver.exe 将没有系统托盘，")
        log("       其余功能不受影响，继续打包")

    # ── 4. 使用预置 ICO（icons/ 已不含 PNG 源图）─────────────────
    icons_dir   = os.path.join(SCRIPT_DIR, "icons")
    sender_ico   = os.path.join(icons_dir, "sender.ico")
    receiver_ico = os.path.join(icons_dir, "receiver.ico")

    log(f"sender.ico 存在: {os.path.exists(sender_ico)}")
    log(f"receiver.ico 存在: {os.path.exists(receiver_ico)}")

    # ── 5. 清理字节码缓存（避免 PyInstaller 分析崩溃）─────────────────
    clean_all_pycache()

    # ── 6. 清理旧的 build/dist ────────────────────────────────────
    for d in ["build", "dist"]:
        full_d = os.path.join(SCRIPT_DIR, d)
        if os.path.isdir(full_d):
            log(f"清理旧目录：{full_d}/")
            shutil.rmtree(full_d, ignore_errors=True)
    for spec in ["sender.spec", "receiver.spec"]:
        full_spec = os.path.join(SCRIPT_DIR, spec)
        if os.path.exists(full_spec):
            os.remove(full_spec)

    # ── 6. 打包 sender.exe ───────────────────────────────────────────
    # sender.py 不使用 PIL，只需 tkinter + 标准库
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
        sender_cmd.append(f"--icon={sender_ico}")
    if os.path.isdir(os.path.join(SCRIPT_DIR, "icons")):
        sender_cmd.append("--add-data=icons;icons")
    sender_cmd.append("sender.py")
    run_cmd(sender_cmd, "打包 sender.exe")

    # ── 7. 打包 receiver.exe ─────────────────────────────────────────
    # receiver.py 使用 PIL（try/except 包裹）和 pystray
    # Pillow 降级到 10.3.0 后，其 .pyc 与 Python 3.10 兼容，不会再崩溃
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
        # pystray Windows 后端
        "--hidden-import=pystray._win32",
        # PIL（Pillow 10.3.0 .pyc 与 Python 3.10 兼容）
        "--hidden-import=PIL._tkinter_finder",
        "--hidden-import=PIL.Image",
        "--hidden-import=PIL.ImageDraw",
        # Windows 注册表
        "--hidden-import=winreg",
        # sqlite3
        "--hidden-import=sqlite3",
    ]
    if os.path.exists(receiver_ico):
        receiver_cmd.append(f"--icon={receiver_ico}")
    if os.path.isdir(os.path.join(SCRIPT_DIR, "hooks")):
        receiver_cmd.append("--additional-hooks-dir=hooks")
    if os.path.isdir(os.path.join(SCRIPT_DIR, "icons")):
        receiver_cmd.append("--add-data=icons;icons")
    receiver_cmd.append("receiver.py")
    run_cmd(receiver_cmd, "打包 receiver.exe")

    # ── 8. 结果汇报 ───────────────────────────────────────────────
    dist_dir = os.path.join(SCRIPT_DIR, "dist")
    log(f"\n{'='*55}")
    log("打包流程结束，检查 dist/ 目录：")
    for name in ("sender.exe", "receiver.exe"):
        path = os.path.join(dist_dir, name)
        if os.path.exists(path):
            size_mb = os.path.getsize(path) / 1024 / 1024
            log(f"  OK  {name:<20} {size_mb:.1f} MB")
        else:
            log(f"  !! {name:<20} 未生成")
    log("="*55)
    log(f"\n完整日志已保存到：{LOG_FILE}")


if __name__ == "__main__":
    main()
