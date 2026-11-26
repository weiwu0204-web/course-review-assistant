"""把 `.ppt` 旧格式批量转成 `.pptx`（PowerPoint COM 自动化）。

## 为什么需要这一步

`extractor` 明确不支持 `.ppt`：python-pptx 只能读 `.pptx`，`.ppt` 的复合文档二进制格式
需要另外的解析库。遇到 `.ppt` 只有两条路——手动另存为，或者 COM 自动化。

本机装了 PowerPoint（Office16，实测 COM 版本 16.0），所以走自动化。
**9 个文件让用户手动另存为太劝退**，而这恰恰是"拿到别人课件"的常见形态。

## 为什么单独成脚本、不塞进 extractor

`.ppt` 是**导入期的一次性动作**，不是每次提取都要做的事：转换产物与源文件并列存放，
之后所有流程（提取 / 建库 / 问答）都只认 `.pptx`。
把它塞进 `extractor` 会让**每次提取都去探测 COM**，既慢又脆弱
（PowerPoint 没装、被安全软件拦、进程残留……都会变成提取失败）。

## 用法

    .venv/Scripts/python.exe scripts/convert_ppt.py 计算机组成原理/
    .venv/Scripts/python.exe scripts/convert_ppt.py 计算机组成原理/ --force

幂等：已存在同名 `.pptx` 就跳过（`--force` 可强制重转）。
单个文件失败不中断整批，最后汇总报告。
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

ROOT = Path(__file__).resolve().parent.parent

#: PowerPoint 的另存为格式常量：ppSaveAsOpenXMLPresentation。
#: 不引 `win32com` 的常量枚举（那个枚举在部分版本里缺失），写数字更稳。
PP_SAVE_AS_PPTX = 24


def convert_folder(folder: Path, force: bool = False) -> int:
    ppts = sorted(p for p in folder.rglob("*.ppt") if not p.name.startswith("~$"))
    if not ppts:
        print(f"目录下没有 .ppt 文件：{folder}")
        return 2

    print("=" * 72)
    print(f"批量转换 .ppt → .pptx · {folder}")
    print("=" * 72)
    print(f"共 {len(ppts)} 个文件\n")

    import win32com.client

    try:
        app = win32com.client.Dispatch("PowerPoint.Application")
    except Exception as exc:  # noqa: BLE001
        print(f"✗ 无法启动 PowerPoint：{exc}")
        print("  请确认本机装了 PowerPoint，或手动另存为 .pptx。")
        return 1

    done, skipped, failed = 0, 0, []
    try:
        for i, src in enumerate(ppts, 1):
            dest = src.with_suffix(".pptx")
            if dest.exists() and not force:
                print(f"  [{i}/{len(ppts)}] 跳过（已存在）{dest.name}")
                skipped += 1
                continue

            print(f"  [{i}/{len(ppts)}] 转换 {src.name} …", end="", flush=True)
            t0 = time.perf_counter()
            pres = None
            try:
                # WithWindow=False 让它在后台打开，不弹窗口抢焦点
                pres = app.Presentations.Open(str(src.resolve()), ReadOnly=True, WithWindow=False)
                pres.SaveAs(str(dest.resolve()), PP_SAVE_AS_PPTX)
                print(f" 完成（{time.perf_counter() - t0:.1f}s）")
                done += 1
            except Exception as exc:  # noqa: BLE001 单份失败不中断整批
                print(f" 失败：{type(exc).__name__}: {exc}")
                failed.append(src.name)
            finally:
                # 不 close 会残留进程、锁住文件（Windows 上表现是"删不掉"）
                if pres is not None:
                    try:
                        pres.Close()
                    except Exception:  # noqa: BLE001
                        pass
    finally:
        try:
            app.Quit()
        except Exception:  # noqa: BLE001
            pass

    print("\n" + "=" * 72)
    print(f"完成 {done} · 跳过 {skipped} · 失败 {len(failed)}")
    for name in failed:
        print(f"  ✗ {name}")
    print("=" * 72)
    return 0 if not failed else 1


def main() -> None:
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    folder = Path(args[0]) if args else Path.cwd()
    if not folder.is_absolute():
        folder = ROOT / folder
    sys.exit(convert_folder(folder, force="--force" in sys.argv))


if __name__ == "__main__":
    main()
