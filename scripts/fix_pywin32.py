"""修复「uv 安装 pywin32 不完整」的问题 —— P2a 踩到的坑，可复用。

现象
----
    ``uv pip install mcp`` 之后 ``import mcp`` 直接炸：

        ModuleNotFoundError: No module named '_win32sysloader'
        （发生在 site-packages/win32/lib/pywintypes.py）

根因
----
    mcp 在 Windows 上依赖 pywin32（``pywin32>=311; sys_platform=='win32'``），
    而 pywin32 的 wheel **依赖安装后的 post-install 步骤**才能把部分文件落位。
    ``uv`` 出于"不执行任意安装脚本"的设计**不跑这一步**。实测对比 wheel 与
    实际安装目录：**611 个文件里有 264 个没被解包出来**，包括整个
    ``win32com/``、``win32comext/``，以及 ``win32/_win32sysloader.pyd``
    等 12 个关键 .pyd —— 而 mcp 恰恰需要它。

修法
----
    从镜像下载**与已装版本一致**的 wheel，把缺失文件补进当前 venv。
    · 幂等：已存在的文件一律跳过，重复执行无副作用
    · 只动当前虚拟环境，不写系统目录、不需要管理员
    · 只补文件，不跑 post-install（那会往 C:\\Windows 写东西）

用法
----
    .venv/Scripts/python.exe scripts/fix_pywin32.py          # 修复
    .venv/Scripts/python.exe scripts/fix_pywin32.py --check  # 只报告，不改动

其他用 uv 建环境、又装了需要 pywin32 的包（mcp / pywin32-ctypes / pypiwin32…）
的项目，把本文件拷过去即可复用。
"""

from __future__ import annotations

import argparse
import importlib.metadata as md
import re
import sys
import urllib.request
import zipfile
from pathlib import Path
from urllib.parse import urljoin

MIRRORS = [
    "https://pypi.tuna.tsinghua.edu.cn/simple/pywin32/",
    "https://pypi.org/simple/pywin32/",
]


def _site_and_scripts() -> tuple[Path, Path]:
    """定位当前解释器所在的 site-packages 与 Scripts。"""
    prefix = Path(sys.prefix)
    for cand in (prefix / "Lib" / "site-packages", prefix / "lib" / "site-packages"):
        if cand.exists():
            return cand, (prefix / "Scripts" if (prefix / "Scripts").exists() else prefix / "bin")
    raise SystemExit(f"找不到 site-packages（sys.prefix={prefix}）")


def _target_of(name: str, site: Path, scripts: Path) -> Path:
    """wheel 内路径 → 实际安装路径（.data/scripts 映射到 Scripts/）。"""
    if ".data/scripts/" in name:
        return scripts / name.split(".data/scripts/")[-1]
    return site / name


def _wheel_url(version: str) -> str:
    """在镜像上找与当前 Python 版本匹配的 pywin32 wheel 地址。"""
    tag = f"cp{sys.version_info.major}{sys.version_info.minor}"
    pat = re.compile(rf'href="([^"]*pywin32-{re.escape(version)}-[^"]*win_amd64\.whl[^"]*)"')
    for base in MIRRORS:
        try:
            html = urllib.request.urlopen(base, timeout=40).read().decode("utf-8", "ignore")
        except Exception as exc:  # noqa: BLE001
            print(f"  镜像不可用 {base}：{type(exc).__name__}")
            continue
        for href in pat.findall(html):
            if tag in href:
                return urljoin(base, href.split("#")[0])
    raise SystemExit(f"镜像上没找到 pywin32 {version} / {tag} 的 win_amd64 wheel")


def main() -> int:
    ap = argparse.ArgumentParser(description="补齐 uv 漏装的 pywin32 文件")
    ap.add_argument("--check", action="store_true", help="只报告缺失，不修改")
    args = ap.parse_args()

    try:
        version = md.version("pywin32")
    except md.PackageNotFoundError:
        print("未安装 pywin32 —— 本脚本不适用（先装 pywin32 或 mcp）")
        return 0

    site, scripts = _site_and_scripts()
    print(f"pywin32 {version}｜site-packages={site}")

    wheel = Path(".pywin32-wheel.whl")
    print(f"下载 wheel：{_wheel_url(version)}")
    urllib.request.urlretrieve(_wheel_url(version), wheel)
    try:
        # 必须用 with 关掉 ZipFile：Windows 上句柄占用的文件删不掉，
        # 会导致 finally 里的清理失败（实测踩到过）。
        with zipfile.ZipFile(wheel) as z:
            names = z.namelist()
            missing = [
                (n, _target_of(n, site, scripts))
                for n in names
                if not n.endswith("/") and not _target_of(n, site, scripts).exists()
            ]
            print(f"wheel 共 {len(names)} 项，缺失 {len(missing)} 项")
            if not missing:
                print("环境完整，无需修复")
                return 0
            if args.check:
                for n, _ in missing[:10]:
                    print(f"  缺：{n}")
                print("（--check 模式，未做修改）")
                return 1

            for n, tgt in missing:
                tgt.parent.mkdir(parents=True, exist_ok=True)
                tgt.write_bytes(z.read(n))
        print(f"已补齐 {len(missing)} 个文件")

        import importlib

        for mod in ("pywintypes", "_win32sysloader"):
            try:
                importlib.import_module(mod)
                print(f"  import {mod}: OK")
            except Exception as exc:  # noqa: BLE001
                print(f"  import {mod}: FAIL {type(exc).__name__}: {exc}")
                return 1
        return 0
    finally:
        # 删临时文件是 best-effort：某些环境（把删除重定向到回收站的 shim /
        # 沙箱）会拒绝删除 .whl，这不该让一次成功的修复变成失败退出。
        try:
            wheel.unlink(missing_ok=True)
        except OSError:
            print(f"（临时文件未能自动删除，可手动清理：{wheel}）")


if __name__ == "__main__":
    raise SystemExit(main())
