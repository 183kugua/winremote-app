#!/usr/bin/env python3
"""WinRemote App —— C 端一键打包脚本（跨平台，Python 实现）。

职责：
  1. 自动检测/创建虚拟环境（Windows 的 .venv 或 Linux 的 .venv）
  2. 安装 requirements.txt + pyinstaller
  3. 调用 PyInstaller 打单文件应用（Windows=exe，Linux=可执行文件）
  4. 在 dist 直接生成 config.yaml（免去手动改名）

用法：
    python packaging/build_exe.py            # 正常打包
    python packaging/build_exe.py --dry      # 仅打印将执行的命令，不跑（调试）
    python packaging/build_exe.py --noclean  # 跳过 PyInstaller 的 --clean

  默认构建：无控制台窗口（windowless），双击 exe 即静默后台运行；
  用 --gui 参数打开图形控制台管理启停。
"""
from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
from pathlib import Path

def _read_app_version() -> str:
    """从 src/winremote_app/__init__.py 读取版本号（单一来源）。"""
    init = Path(__file__).resolve().parent.parent / "src" / "winremote_app" / "__init__.py"
    for line in init.read_text(encoding="utf-8").splitlines():
        if line.startswith("__version__"):
            return line.split("=", 1)[1].strip().strip('"\'')
    return "0.0.0"


APP_VERSION = _read_app_version()
APP_NAME = "winremote-app"


def repo_root() -> Path:
    """仓库根 = 本文件所在目录的上一级。"""
    return Path(__file__).resolve().parent.parent


def ensure_venv(root: Path) -> Path:
    """返回解释器路径；不存在则创建。Windows: <root>/.venv/Scripts/python(.exe)。"""
    if os.name == "nt":
        py = root / ".venv" / "Scripts" / "python.exe"
    else:
        py = root / ".venv" / "bin" / "python"
    if not py.exists():
        print(f"[1/4] 创建虚拟环境 .venv ...")
        subprocess.check_call([sys.executable, "-m", "venv", str(root / ".venv")])
    else:
        print(f"[1/4] 复用虚拟环境 .venv")
    return py


def run(py: Path, args: list[str], dry: bool) -> None:
    cmd = [str(py)] + args
    print(f"    $ {' '.join(cmd)}")
    if not dry:
        subprocess.check_call(cmd)




def check_webui_js() -> None:
    """打包前对内联 <script> 做 JS 语法检查（SyntaxError 会让整个界面变静态外壳）。"""
    import re as _re

    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    idx = os.path.join(root, "src", "winremote_app", "webui", "index.html")
    try:
        import esprima  # type: ignore
    except Exception:
        run(sys.executable, ["-m", "pip", "install", "esprima", "-q",
                             "--disable-pip-version-check"], False)
        import esprima  # type: ignore
    html = open(idx, encoding="utf-8").read()
    m = _re.search(r"<script>(.*)</script>", html, _re.S)
    if not m:
        print("!! 未找到 <script>，跳过 JS 检查")
        return
    try:
        esprima.parseScript(m.group(1))
        print("[JS check] webui/index.html syntax OK")
    except Exception as e:
        raise SystemExit("!! webui/index.html JS 语法错误，已阻止打包：%s" % e)


def main() -> int:
    check_webui_js()
    ap = argparse.ArgumentParser(description="WinRemote App 打包脚本")
    ap.add_argument("--dry", action="store_true", help="只打印命令，不实际执行")
    ap.add_argument("--noclean", action="store_true", help="跳过 PyInstaller --clean")
    args = ap.parse_args()
    dry = args.dry

    root = repo_root()
    os.chdir(root)
    print(f"仓库根: {root}")

    py = ensure_venv(root)

    print("[2/4] 更新 pip 并安装依赖 ...")
    run(py, ["-m", "pip", "install", "--upgrade", "pip"], dry)
    run(py, ["-m", "pip", "install", "-r", str(root / "requirements.txt")], dry)

    print("[3/4] 确保 PyInstaller ...")
    if not _have_pkg(py, "PyInstaller"):
        run(py, ["-m", "pip", "install", "pyinstaller"], dry)

    print("[4/4] 打包 ...")
    dist = root / "dist"
    dist.mkdir(exist_ok=True)

    pyi = ["-m", "PyInstaller", "--noconfirm"]
    if not args.noclean:
        pyi.append("--clean")
    # 默认无控制台窗口：双击 exe 静默后台运行，--gui 打开图形控制台管理
    if os.name == "nt":
        pyi.append("--noconsole")

    pyi += [
        "--onefile",
        "--name", APP_NAME,
        "--paths", "src",
        "--hidden-import", "winremote_app",
        "--collect-all", "pystray",
        # 强制全量收集 Pillow（含 _imaging* C 扩展，避免 hook 漏收 _imagingft 等）
        "--collect-all", "PIL",
    ]
    # 打包时把配置样例嵌入（作为兜底模板）
    sep = ";" if os.name == "nt" else ":"

    # ---- Windows 专用修复 ----
    if os.name == "nt":
        # 1) Pillow 12 的 hook 会漏收 _imagingft（FreeType 扩展）：显式补二进制
        pil_dir = Path(sys.prefix) / "Lib" / "site-packages" / "PIL"
        if pil_dir.is_dir():
            for pyd in sorted(pil_dir.glob("_imagingft*.pyd")):
                pyi += ["--add-binary", f"{pyd}{sep}PIL"]
                print(f"    补收 PIL 扩展: {pyd.name}")
        # 2) venv 打包时 tkinter 的 tcl/tk 运行时在基础解释器目录，hook 会误判 broken：
        #    显式带上 tcl/tk 数据目录、解释器 DLLs 里的 tcl/tk DLL，并声明 tkinter。
        base_tcl = Path(sys.base_prefix) / "tcl"
        if base_tcl.is_dir():
            pyi += ["--add-data", f"{base_tcl}{sep}tcl"]
            print(f"    补收 tcl/tk: {base_tcl}")
        dll_dir = Path(sys.base_prefix) / "DLLs"
        for dll in ("tcl86t.dll", "tk86t.dll"):
            dll_path = dll_dir / dll
            if dll_path.is_file():
                pyi += ["--add-binary", f"{dll_path}{sep}."]
                print(f"    补收 DLL: {dll}")
        pyi += ["--hidden-import", "tkinter"]

    pyi += ["--add-data", f"config.example.yaml{sep}."]
    pyi += ["--add-data", f"src/winremote_app/webui{sep}webui"]
    pyi += ["packaging/entry.py"]
    run(py, pyi, dry)
    if dry:
        return 0

    exe = dist / (APP_NAME + (".exe" if os.name == "nt" else ""))
    if not exe.exists():
        print("[错误] 打包完成但未找到产物：", exe, file=sys.stderr)
        return 1

    # 配置一并拷到 dist：直接生成 config.yaml，免去手动改名
    cfg = root / "config.example.yaml"
    if cfg.exists():
        shutil.copy(cfg, dist / "config.yaml")
    size_kb = exe.stat().st_size // 1024
    print("=" * 52)
    print(f"  打包完成！")
    print(f"    产物：{exe}  （{size_kb} KB, windowless 无黑框）")
    print(f"    配置：{dist.name}/config.yaml 已生成，编辑后即可用")
    print("=" * 52)
    return 0


def _have_pkg(py: Path, pkg: str) -> bool:
    try:
        r = subprocess.run(
            [str(py), "-m", "pip", "show", pkg],
            capture_output=True, text=True, timeout=30,
        )
        return r.returncode == 0
    except Exception:
        return False


if __name__ == "__main__":
    sys.exit(main())