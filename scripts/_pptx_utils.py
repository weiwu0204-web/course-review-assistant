"""脚本公共工具：命令行参数解析与 PPT 文件自动发现。

让命令行工具做到「不传文件名也能跑」：
- 传了参数 → 用传入的路径（任意文件名，无需改名）
- 没传参数 → 自动找当前目录下唯一的 .pptx；有多个则列出让用户指定
"""

import sys
from pathlib import Path


def resolve_pptx(usage: str) -> Path:
    """解析命令行参数，返回要处理的 PPT 路径。

    参数:
        usage: 帮助文案，无法确定文件时打印。

    返回:
        PPT 文件路径（相对或绝对均可）。
    """
    if len(sys.argv) >= 2:
        return Path(sys.argv[1])

    candidates = sorted(Path.cwd().glob("*.pptx"))
    if len(candidates) == 1:
        print(f"✓ 自动发现当前目录下的 {candidates[0].name}")
        return candidates[0]

    if len(candidates) > 1:
        print("当前目录有多个 .pptx，请指定要处理哪一个：")
        for c in candidates:
            print(f'  例如: python {Path(sys.argv[0]).name} "{c.name}"')
        sys.exit(1)

    print("未指定文件，且当前目录下没有 .pptx。")
    print(f"用法: {usage}")
    sys.exit(1)
