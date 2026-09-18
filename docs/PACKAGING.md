# 打包预案（Step 1：pywebview 壳 → exe）

状态：**前置门槛已过，可进入打包轮**——pywebview 窗口已在本机验证通过
（v0.6.0 起全程在桌面壳内实测），API key 决策已拍板并随 v0.6.0 落地（见第二节第 1 点）。

---

## 一、打包形态建议

| 形态 | 优点 | 缺点 | 结论 |
|---|---|---|---|
| onefile（单 exe） | 微信传一个文件即可 | 启动慢 3~8s（解压到 %TEMP%）；杀软误报率更高 | **最终分发形态** |
| onedir（文件夹） | 启动快、好调试、日志可见 | 分发要打 zip | **开发/验收形态** |

流程：先 onedir 验收，稳定后出 onefile。两者 spec 几乎相同，只差最后一步
COLLECT vs EXE——建议直接写 spec 文件而不是反复拼命令行。

---

## 二、打包前必须处理的 4 个问题（按风险排序）

### 1. API key 不进 exe（产品决策，先于一切技术问题）

现状：`app/config.py` 的 `Settings` 从 **exe 旁 `.env`** 读取。若把 `.env` 一起放进
分发目录，同学解包即可拿到 DeepSeek key。

决策点（雾围拍板，两个选项）：

- **A. 内置自己的 key**：打包时把 key 写进代码常量，**不**把 `.env` 放进产物。
  代价要明示：编译进 exe 的 key 仍可被反编译/抓包提取——这不叫加密，只是提高
  提取门槛。可行前提 = 熟人小范围分发 + 客户端限流（原方案，**已否决**）。
- **B. exe 内留设置入口**，同学填自己的 key：零泄露风险，但要引导注册、体验降级。

> **已拍板：B**（2026-09，随 v0.6.0 落地）。实现 = 首次启动弹设置面板强制配置：
> 选服务商 → 粘 Key → `max_tokens=1` 微型请求验证（区分 6 类失败原因）→ 保存到
> `%APPDATA%\PPT复习助手\settings.json` 并热重载。附带服务商映射表（模型名/单价/
> 档位/充值入口，改 `app/config.py` 即可）、成本预估、失败重试——详情见 CHANGELOG 0.6.x。
> 若将来规模变大再谈服务端代理/中转。

### 2. pywebview 平台后端（PyInstaller 必踩的坑）

pywebview 在**运行时**按平台动态 `import` 后端（Windows 走
`webview.platforms.edgechromium`，底层 pythonnet）。PyInstaller 静态分析抓不到
动态导入 → 产物启动时报 ImportError 或窗口白屏。

解法：`--collect-all webview`（连带平台子包与资源一起收集），比手列 hidden-import
稳。若目标机缺 WebView2 运行时再考虑 `winforms` 后端，一般用不上（Win10/11 自带）。

### 3. 静态资源与 uploads 目录（打包后路径会漂移）

- **static**：打包命令加 `--add-data "app/static;app/static"`（注意 Windows 分隔符
  是 `;`）。`app/main.py` 用 `Path(__file__).parent / "static"` 定位——打包后
  `__file__` 指向 `_internal/app/`（onedir）或解压目录（onefile），只要 data 按
  `app/static` 的包结构打进去，**现有代码无需改动即可命中** ✅

- **uploads**：`routes.py` 的 `UPLOAD_DIR = Path(__file__)...parent.parent.parent / "uploads"`
  打包后会解析到 `_internal/uploads`（onedir，能写但藏得深、升级残留）或
  `%TEMP%` 临时目录（onefile，会话内稳定、退出即清）。**执行打包那一轮**把定位改为：

  ```python
  # app/_paths.py（草案，随打包轮一起加并验证）
  import sys
  from pathlib import Path

  def app_base_dir() -> Path:
      """源码运行 = 项目根；打包后 = exe 所在目录（同学放桌面/文件夹，可写可见）。"""
      if getattr(sys, "frozen", False):
          return Path(sys.executable).resolve().parent
      return Path(__file__).resolve().parent.parent  # app/ 的上一级 = 项目根
  ```

  `UPLOAD_DIR` 改为 `app_base_dir() / "uploads"`。改动放到打包轮做——当前环境
  测不到 `sys.frozen` 分支，提前写等于留无法验证的代码。

### 4. WebView2 运行时与 SmartScreen（预期行为，不是 bug）

- **WebView2**：Win10/11 通常自带，pywebview 自动探测。极老系统缺失时窗口起不来，
  `desktop_app.py` 已有兜底（打印真实原因 + 提示浏览器访问），首版可接受。
- **SmartScreen**：未签名 exe 首次运行必弹「Windows 已保护你的电脑」。
  预期行为，**不要**试图规避（无代码签名证书）。分发时给同学一句话提示：
  「提示未信任时点『更多信息 → 仍要运行』」。这属于换壳不解决的三件事之一（记忆里有）。

---

## 三、打包命令（onedir 验收版）

```bash
# 在项目根执行（用 .venv 里的 pyinstaller）
uv pip install pyinstaller
uv run pyinstaller --noconfirm --clean \
  --name "PPT复习助手" \
  --add-data "app/static;app/static" \
  --collect-all webview \
  --exclude-module pytest \
  desktop_app.py
```

要点：

- `--collect-all webview`：收集平台子包与内置资源，避免手列 hidden-import 漏项
- `--exclude-module pytest`：开发依赖不进产物，减体积
- 产物在 `dist/PPT复习助手/`，双击 `PPT复习助手.exe`
- `--windowed`（等价 `CONSOLE=False`）：不弹黑控制台，正式分发必加；
  开发期可先不加，方便看日志

体积预估：python-pptx + lxml + openai + Pillow + pythonnet 大头，onedir 约 80~120MB。

---

## 四、验收清单（打包产物 smoke test）

1. 双击 exe → 窗口 3~10s 内出现（onedir 快；onefile 慢属正常，不是卡死）
2. 上传 `课件.pptx` → 生成 → 文档渲染、复制/下载可用
3. 往年试题粘贴 → 解析 → 直搬 / 预测 各跑一次
4. 关闭窗口 → 任务管理器确认无残留 python 进程（uvicorn 是 daemon 线程，
   应随主进程退出；若有残留说明要加 `server.should_exit` 收尾——已有，验证即可）
5. **拷到一台无 Python 的 Windows 机器**跑 1–4（同学机器场景的最终验证）
6. 断网启动 → 界面能打开，仅在生成时报网络错误，不能白屏

---

## 五、待办衔接

- [x] 雾围本机跑 `uv run python desktop_app.py` 验证窗口（✅ 2026-09-04，0.6.x 全程壳内实测）
- [x] 拍板「API key 方案 A / B」→ **B**（✅ v0.6.0 落地：设置面板自填 + `%APPDATA%` 存储）
- [ ] 打包轮：加 `app/_paths.py`、改 `UPLOAD_DIR`、跑 onedir 验收 → onefile 发布
- [ ] 分发：微信传 exe + SmartScreen「仍要运行」提示文案
