---
status: current
verified: 274dde4
---

# 环境搭建

## 用 uv，不要用 conda

本机 conda 是 **4.5.11（2018 年）+ Python 3.7**，太旧，别在上面浪费时间。

uv 已经装好了（`uv 0.12.5`）。三条命令够用：

```bash
uv venv                    # 建环境
uv pip install X           # 装包
uv run python run.py       # 跑脚本，不用手动 activate
```

没装的话：

```bash
# Windows
powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"

# macOS / Linux
curl -LsSf https://astral.sh/uv/install.sh | sh
```

## 完整步骤

```bash
cd ppt-reviewer

# 1. 建环境并安装依赖
uv venv
uv pip install -r requirements.txt

# 2. 配置 API（Windows 没有 cp，手动复制文件也行）
copy .env.example .env      # 或者直接在资源管理器里复制粘贴

# 3. 启动
uv run python run.py
```

浏览器打开 <http://127.0.0.1:8000>。

`.env` 里必填的只有 `AI_API_KEY`，`AI_BASE_URL` 和模型名已经默认配好 DeepSeek 了。

## 已经踩过的坑

### pip.exe 被沙箱安全删除机制搞坏

在项目里用 `python -m venv .venv` 建环境，然后 `pip install --upgrade pip`，
升级过程会尝试替换 `pip.exe`，被沙箱拦下，结果 venv 直接残废
（报 `No module named pip`）。

**用 `uv venv` 就没这个问题**——uv 建的 venv 不装 pip，
`uv pip install` 走的是自己的安装器。

### 直连 pypi.org 超时

装包前先设镜像：

```bash
# Git Bash / macOS / Linux
export UV_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple

# Windows PowerShell
$env:UV_INDEX_URL="https://pypi.tuna.tsinghua.edu.cn/simple"
```

首次 `uv pip install -r requirements.txt` 大约 4~5 分钟。

### `uv run pytest` 报 `No module named 'app'`

`python -m pytest` 会自动把当前目录加进 `sys.path`，`uv run pytest` 不会。
项目根目录放了 `conftest.py` 解决这个问题，别删它。

### Pillow 12.3 没有 `Image.stat()`

`im.stat()` 这个快捷方法在 Pillow 12.3 上不存在，必须用：

```python
from PIL import ImageStat
std = ImageStat.Stat(im.convert("L")).stddev[0]
```

（这个 bug 当时的表现是所有图片被误判为纯色，提取数直接归零。）

### Windows 下没有 cp 命令

README 里写的 `cp .env.example .env` 在 cmd / PowerShell 里跑不了，
用 `copy` 或者手动复制。

### 本机沙箱里 httpx 连 127.0.0.1 必然 ReadTimeout

在**这个执行环境**（沙箱 bash）里，`httpx` 通过 TCP 访问本机服务
（如同进程内 uvicorn）会一直 `ReadTimeout`，而 `socket` / `urllib` / `curl`
都正常——不是服务的问题，是 httpx 在此环境的坑（疑似网络栈/代理层问题）。

踩坑记录：2026-09-02 用 httpx 写端到端脚本跑 3 次全挂在等待服务就绪，
换 urllib 即通。

**解法：本地接口测试别走真实 TCP**——用 FastAPI `TestClient`
（ASGI 直连，不经网络栈），见 `scripts/e2e_exam_flow.py`。

### `.env` 残留旧值会悄悄覆盖代码默认值

pydantic-settings 里**环境变量 / `.env` 的优先级高于 `app/config.py` 的默认值**。
踩坑记录：`ai_max_tokens` 在代码里提到 16384，但 `.env` 还留着 8-30 写的
`AI_MAX_TOKENS=8192`，导致实测输出在 8192 处被截断（predict 首跑甚至产出 0 字符 + truncated）。

**改代码默认值时，记得 grep 一下 `.env` 和 `.env.example` 有没有残留旧值，双处维护：**

```bash
grep -n "AI_MAX_TOKENS" .env .env.example
```

### 桌面壳（desktop_app.py）依赖 pywebview

桌面壳入口 `desktop_app.py` 顶部 `import webview`，**依赖声明在
`requirements.txt`**（`pywebview>=6.0.0; sys_platform == "win32"`，装 pywebview 会
自动拉 Windows 侧的 `pythonnet`）。曾出现「代码写了但依赖没进 requirements、
venv 里靠历史裸装才有 pywebview」的脱管情况——改 `requirements.txt` 后记得
`uv pip install -r requirements.txt` 同步。

pywebview 6.x 在 Windows 用 WebView2（EdgeChromium）渲染，Win10/11 自带运行时，
无需额外安装。GUI 弹窗只能在本机有桌面会话时看到；无头/远程环境用
`--serve-only` 只验后端：

```bash
uv run python desktop_app.py                # 本机弹窗（1120×800 原生窗口）
uv run python desktop_app.py --serve-only   # 只起服务不弹窗（验证用）
```

服务端口**不再写死 8000**：启动时从 8000 起自动探测空闲端口（被占则顺延 8001…），
`desktop_app.py` 会打印实际使用的端口。注意探测 socket 不能设 `SO_REUSEADDR`
（Windows 上会让 bind 对占用端口静默成功，误判空闲）。

## 验证

```bash
uv run pytest                                  # 61 passed
uv run python scripts/diagnose.py "第一章 数据结构.pptx"   # 诊断提取覆盖率（文件名随意）
uv run python scripts/preview_images.py "第一章 数据结构.pptx" --out uploads/preview.png
uv run python scripts/e2e_exam_flow.py 课件.pptx eval/sample_exam.txt           # 直搬真题回归（约 1 分钟）
uv run python scripts/e2e_exam_flow.py 课件.pptx eval/题库_网络分层模型_通用.md predict  # 预测题回归
uv run python desktop_app.py --serve-only    # 桌面壳后端无头验证：/api/health 应返回 {"status":"ok"}
```

诊断工具会告诉你这份 PPT 有多少页只能靠图片、图片部分要花多少 token。
**先跑它，再决定要不要开视觉。**
