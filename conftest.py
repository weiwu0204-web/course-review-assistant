"""pytest 配置：把项目根目录加进 sys.path，让 `uv run pytest` 能导入 app 包。

直接 `python -m pytest` 时不需要它（`-m` 会自动加当前目录），
但 `uv run pytest` 不会，所以留着这个文件。
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
