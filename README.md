# 📚 PPT 复习助手

多人服务版的范围、启动方法和回滚办法见 [服务版私有预览](docs/runbooks/service-preview.md)。以下是桌面版说明。

不同学校的同名专业课在授课范围和考法上有差异。复习助手用本校课件（PPT / PDF）确定复习范围，结合往年试题生成可追溯到课件页码的复习手册。

> 📖 设计与决策记录在 [docs/](./docs/)：[项目描述](docs/PROJECT.md) ·
> [架构思路](docs/ARCHITECTURE.md) · [版本记录](docs/CHANGELOG.md) ·
> [环境搭建](docs/SETUP.md) · [打包预案](docs/PACKAGING.md)

## ✨ 功能

- 📤 网页上传 `.pptx` / `.pdf`，逐页提取文字、表格、**备注**、**公式**、**图表数据**
- 📄 **PDF 课件支持**：直接读 PDF 文字层，零额外成本。注意「文字版 PDF」不等于
  「能提取出文字」——由「打印成 PDF」生成的文件，字体映射表可能损坏，字形看得见
  但程序读不出中文（实测 4 份课件的中文字符全部丢失）。这类文件会被自动拦下并
  提示走 OCR，而不是把乱码喂给模型
- 🖼️ 自动导出幻灯片里的嵌入图片，交给视觉模型一起理解（可开关）
- 🤖 OpenAI 兼容接口，默认 DeepSeek；有图时自动切视觉模型，无图时走纯文本模型
- 📋 三种文档类型：**复习指南**、**重点总结**、**模拟习题生成**
- 🎨 三种输出风格：**精简**、**详细**、**表格化**（覆盖率只对「复习指南 / 详细」这一组
  有意义——其它档位本就不追求全覆盖）
- 📝 **往年试题加持**（可选）：粘贴文本或上传 `.txt/.md/PDF`，
  可把真题**原样搬进文档**，或让 AI **参考其风格出预测题**（明示非原题）
- 💾 支持复制、下载 Markdown / HTML
- ⚙️ **网页设置面板**：首次启动强制配置——选服务商（DeepSeek / 智谱 GLM / Kimi / 通义，内置模型映射）→ 粘贴 Key → 微型请求验证 → 保存；智谱可切换付费/免费档；Key 只存本机 `%APPDATA%`
- 💰 **生成前费用预估**：按所选服务商单价粗估本次花费并显示在进度里，心里有数再点生成
- 🔄 **失败自愈**：429/5xx 自动退避重试；仍失败弹「重试」按钮一键续跑（余额不足充值后可直接续上）
- 🧠 **深度思考开关**：默认关——2026-09 核实四家服务商思考默认全开，关掉省 30-50% 费用且更快，需要更强推理时再勾
- 🔍 **覆盖率回检**：生成后自动比对「源课件的知识点术语 ↔ 成品文档」，逐节给出覆盖率，
  并列出**疑似未覆盖**的条目（**带源课件页码**，一眼能核对）。纯本地文本比对，
  **不额外花一分钱**；「复习指南 / 详细」档才有意义，其它档位会说明原因而不是硬打分
  （详见 [docs/P1-COVERAGE.md](docs/archive/desktop/P1-COVERAGE.md)）
- 🔧 **覆盖率不足时自动补漏**：低于 94% 时，一个带 **MCP 工具**的 Agent 会自己去查缺失清单、
  读源课件页、提交结构化补丁、即时验证效果，直到达标或触达成本上限。**只补不重写**
  （补丁由程序插入，模型碰不到原文）、**没提升就回滚**、全过程留痕（补了几轮、花了多少钱）。
  默认关闭；对已达标的文档**零触发、零成本**（详见 [docs/P2-ACCEPTANCE.md](docs/archive/desktop/P2-ACCEPTANCE.md)）
- 📚 **整课批量生成**：一次拖入一门课的多份 PPT（≤20），逐份生成后自动汇编成一份《整门课复习总册》——自动目录、出处锚点、正文原样不改写；逐份进度与费用可见、可停止/续跑/重试失败份，中途关页面任务照跑（详见 [docs/P0-ARCHITECTURE.md](docs/archive/desktop/P0-ARCHITECTURE.md)）

## 🧱 提取分三层，越靠前越便宜

这是整个项目的核心设计——**能免费解析出来的，就不要花钱交给模型**。

| 层级 | 内容 | 成本 |
|------|------|------|
| 1. 纯解析 | 文本框、表格、**备注页**、**OMML 公式**、**图表数据** | 零 |
| 2. 嵌入图片 | 截图、示意图、以图片形式存在的公式 | 视觉模型，单图 ≤ 384 tokens |
| 3. 整页渲染 | 扫描版幻灯片（尚未实现，需 PowerPoint / LibreOffice） | 最贵 |

之所以要分：图表不是图片，数据就存在 XML 里，直接读比让模型去猜准得多；
公式同理，PowerPoint 2007 以后用的是 OMML，也是可解析的 XML。
老师写在备注里的「这题必考」更是白捡的重点。

## 🚀 快速开始

推荐用 **uv**（比 pip 快一个量级，本机已装在 `~/.local/bin/uv`）。
本机 conda 是 2018 年的 4.5.11 + Python 3.7，太旧，不建议用。

```bash
cd ppt-reviewer

uv venv                       # 创建 .venv，默认拉一个较新的 Python
uv pip install -r requirements.txt
```

国内网络建议先设镜像，否则容易超时：

```bash
export UV_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple   # macOS / Linux
$env:UV_INDEX_URL="https://pypi.tuna.tsinghua.edu.cn/simple"   # Windows PowerShell
```

<details>
<summary>没装 uv 的话</summary>

```bash
# Windows
powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"

# macOS / Linux
curl -LsSf https://astral.sh/uv/install.sh | sh
```

装完重开终端即可。之后日常只需要记三条：
`uv venv` 建环境、`uv pip install X` 装包、`uv run python x.py` 跑脚本。
它会自动管理 `.venv`，不用手动 `activate`。

</details>

### 配置 API

**两种方式，网页设置优先：**

1. **网页设置页（推荐，打包版唯一方式）**：启动后第一次打开会自动弹出配置面板——
   选一家服务商（DeepSeek / 智谱 GLM / Kimi / 通义千问，已内置各家模型映射）→
   粘贴你的 API Key → 点「验证并保存」。面板里有 30 秒注册教程，验证失败会提示
   具体原因（key 无效 / 余额不足 / 限流 / 网络…）。
   Key 只保存在本机 `%APPDATA%\PPT复习助手\settings.json`，软件不联网上传。
2. **开发态 `.env`**（仅本仓库开发调试用，打包版没有）：

   ```bash
   cp .env.example .env
   ```

   ```ini
   AI_API_KEY=sk-xxx
   AI_BASE_URL=https://api.deepseek.com
   AI_MODEL=deepseek-v4-flash              # 纯文本
   AI_VISION_MODEL=deepseek-v4-flash-vision-exp   # 视觉
   AI_MAX_TOKENS=32768
   AI_MAX_RETRIES=3                        # 可选：429/5xx 自动重试次数（默认 3）
   AI_TIMEOUT_SECONDS=600                  # 可选：单次请求总超时秒数（默认 600）
   ```

> ⚠️ **`deepseek-chat` 和 `deepseek-reasoner` 已于 2026-07-24 永久停用**，
> 现在调用会直接报错。文本用 `deepseek-v4-flash`，看图用 `deepseek-v4-flash-vision-exp`；
> 网页设置面板已内置各家模型映射，平时无需手改这些字段。

### 启动

浏览器模式（开发调试用，带热重载）：

```bash
uv run python run.py
```

浏览器打开 <http://127.0.0.1:8000>。

桌面窗口（正式使用，与打包版同壳）：

```bash
uv run python desktop_app.py               # 弹原生窗口，内嵌仅本机可访问的服务
uv run python desktop_app.py --serve-only  # 无头起服务，用于验证/调试
```

## 🔍 先诊断，再生成

生成前先扫一遍这份 PPT 能被提取到什么程度，以及值不值得开视觉模型。
**文件名随意**（老师的「第一章 xxx」「第三节 xxx」都行，不用改名）：

```bash
uv run python scripts/diagnose.py "第一章 数据结构.pptx"
```

也可以不传文件名——脚本会自动找当前目录下的 .pptx（只有一个时直接用，
有多个时会列出让你选）：

会逐页报告文字 / 备注 / 图片的情况，并估算图片部分的 token 成本。
**如果输出「没有可用的嵌入图片」，就用纯文本模型，别开视觉。**

没有现成 PPT 时，可以先造一份样例练手：

```bash
uv run python scripts/make_sample.py
```

## 📊 生成质量评测（消融实验）

`scripts/evaluate_generation.py` 对样例课件做三配置消融，量化「分层提取」每一层的边际贡献。
评测集是 `tests/samples/sample_course.pptx`（6 页，覆盖标题 / 要点 / 表格 / 图表 / 纯图片 / 带备注六种典型页面），
ground truth 从 `scripts/make_sample.py` 源码推导，无需人工标注。

```bash
uv run python scripts/evaluate_generation.py          # 跑全部三种配置
uv run python scripts/evaluate_generation.py --dry-run # 不调 API，只验证提取链路
```

| 指标 | 只提取文本框 | 分层提取（无图） | 分层 + 视觉模型 |
|------|------------:|----------------:|----------------:|
| 知识点召回率（9 项） | 100% | 100% | 100% |
| 硬数据保留率（11 项表格/图表数值） | **18%** | **73%** | 73% |
| 教师备注考点（4 项） | 50% | **100%** | 100% |
| 图片独有内容（3 项） | 33% | 33% | **100%** |
| 幻觉率（参考，越低越好） | 6% | 15% | 8% |

**四条实测结论：**

1. **核心知识点的区分度不在分层，在数据完整性**——概念都在文字层，三种配置都能 100% 召回
2. **分层提取的最大贡献是硬数据：18% → 73%**。图表数值（0.01 / 0.8 / 80.0 这类）只靠文本框
   几乎全丢，从 XML 直接读是「免费那层」的实证
3. **备注是白捡的重点：50% → 100%**（只提取文本框时，命中的 2 项还是模型自带知识补的）
4. **视觉模型的边际价值只在「图片独有内容」：33% → 100%**，对数据完整性无增量——
   图片里若是重复信息，开视觉不提升质量。这正是诊断脚本要先跑的原因

**已知限制**：图表分类轴（n=100 / n=1000 / n=10000）三种配置都未能完整转写——数据已提取到，
但模型生成时省略了。后续在提示词中显式强调图表数据即可改善。

## 🎯 覆盖率回检：让「漏了什么」看得见

AI 生成的复习文档最大的隐患不是「写得差」，而是**漏了东西你看不出来**。

我用同一门课（Ceph 分布式存储，14 节 / 325 页）做过一次对照实测——让一个通用大模型
一次性处理全部 14 节课件，与本项目逐节生成做逐术语比对：

| | 平均 | 前 7 节 | 后 7 节 |
|---|---:|---:|---:|
| **本项目**（每节独立请求） | **97.7%** | 98.6% | **96.9%** |
| 通用大模型（单次处理整门课） | 89.5% | 95.1% | **83.9%** |

**关键不在平均值，而在后半程**：通用模型**不报错、文档看着完整**，但覆盖率从 95% 掉到
84%——**用户不可能察觉后 1/3 漏了约 1/5**。本项目几乎不衰减，因为每节各自占用一个请求窗口。

所以生成完成后会做一次覆盖率回检（纯本地文本比对，**不花钱**）：

- 逐节给出覆盖率，并列出**疑似未覆盖**的术语 + **源课件页码**，点开即可核对
- 术语有三条来源：英文/技术术语、数字阈值、**中文术语（按课件内频次挖掘，不依赖分词库）**
- 措辞很克制：判据是**词面**的，术语没出现也可能只是换了说法（`cipher suites`→「加密套件」），
  所以一律说「**疑似**」，判断权交回用户
- 判据与阈值都是**用真实数据标定**的，不是拍脑袋：详见 [docs/P1-COVERAGE.md](docs/archive/desktop/P1-COVERAGE.md)

### 复现与回归

```bash
# 体检某个批任务（只读，零成本）
uv run python scripts/verify_coverage.py --batch <batch_id>

# 回归：平均覆盖率不得低于 94.7%（= 基准 97.7 − 容差 3），退化则退出码 1
uv run python scripts/verify_coverage.py --batch <batch_id> --expect 97.7 --tol 3

# 复现上表的对照：拿一份外部生成的文档与同一批课件逐节比对
uv run python scripts/verify_coverage.py --batch <batch_id> --compare 某文档.md
```

改了判据、换了模型或调了提示词之后跑一次上面第二条，就知道有没有退化。

> **存量批次**：本功能上线前跑的批次没有覆盖率数据（`coverage_percent = -1`，界面不显示徽标）。
> **不用重跑**（重跑要花钱）——课件和生成文档都还在产物目录里，本地重算即可：
>
> ```bash
> uv run python scripts/backfill_coverage.py --dry-run   # 先看会改什么（推荐）
> uv run python scripts/backfill_coverage.py             # 实际写入（会先备份 batch.json）
> ```
>
> 脚本只对「复习指南 / 详细」的批次计算，其它档位整体跳过（它们本来就不追求全覆盖）。

## 🔧 发现漏了之后：自动补漏（verify → repair）

覆盖率回检告诉你「漏了什么」，但**发现问题只是第一步**。开启自动补漏后，
一个带上工具的 Agent 会接手把它补回来：

```
生成 → 回检 → 覆盖率 <94% → Agent 调 MCP 工具定向补漏 → 再回检 → 达标或触达成本上限
                                ↑
                 查缺失清单 / 读源课件页 / 提交补丁 / 即时验证
```

**实测（2026-09-15，DeepSeek `deepseek-flash`）**：

| 场景 | 起点 → 终点 | 轮次 | 成本 | 占该节生成成本 |
|---|---|---|---|---|
| 真实低覆盖文档 | 89% → **100%** | 3 | ¥0.024 | 26% |
| 人为删掉一半小节 | 82% → **100%** | 3 | ¥0.034 | 37% |
| Ceph 课件删掉一半 | 58% → **100%** | 3 | ¥0.059 | — |

**平均 ≈ ¥0.002 / 百分点**（补 10 个百分点约 2 分钱）。
三次的轮次结构完全一致：**2 轮侦察（看清单、读源页）+ 第 3 轮提交补丁**。

### 三个刻意的设计取舍

| 取舍 | 为什么 |
|---|---|
| **补丁由程序插入，模型不输出修订全文** | 「只补缺失、不改写原文」由**程序结构**保证——提示词约束模型可能违反，程序插入不会 |
| **没提升就回滚** | 用户永远不会拿到「被改乱了却没变好」的文档；原文备份在 `doc.md.pre-repair` |
| **不引 LangGraph** | 循环只有「补漏 → 验证 → 继续或停」三个状态，最简 while 足够。**"我评估过、结论是不需要"比"我用过"更难被驳倒** |

### 它不会做什么

- **对已达标的文档一次都不触发**（本地判定验证过 25 节，零调用零成本）
- **档位不适用时不触发**：习题生成 / 重点总结本就不追求全覆盖，即使覆盖率只有 58% 也不补 ——
  否则就是「拿一个不成立的承诺去指责文档」
- **不为了凑覆盖率罗列术语**：抽查 20 条补入内容，100% 都是「术语 + 实质说明」

八项验收标准与逐项证据见 **[docs/P2-ACCEPTANCE.md](docs/archive/desktop/P2-ACCEPTANCE.md)**（含六条限制的如实记录）。

## 🔧 项目结构

```
ppt-reviewer/
├── run.py                  # Web 模式启动入口（uvicorn + reload）
├── desktop_app.py          # 桌面壳（pywebview + 内嵌 FastAPI），打包版入口
├── requirements.txt
├── .env.example            # 环境变量模板
├── app/
│   ├── main.py             # FastAPI 应用
│   ├── config.py           # 配置加载 + 服务商映射表（模型/价格/档位/充值入口）
│   ├── models.py           # 数据模型
│   ├── mcp_server.py       # P2：MCP Server（覆盖率工具集，stdio 子进程）
│   ├── api/routes.py       # 主流程 API（上传/解析/生成）
│   │   ├── settings_routes.py # 设置 API（status/providers/estimate/verify/save/clear）
│   │   └── batch_routes.py   # 批量 API（peek/start/status/retry/cancel/下载）
│   ├── services/
│   │   ├── extractor.py      # 统一提取入口（按后缀分发 .pptx / .pdf）
│   │   ├── ppt_extractor.py  # PPT 提取（文本/备注/公式/图表/图片）
│   │   ├── pdf_extractor.py  # PDF 提取（文字层 + 健康检测 + 水印过滤）
│   │   ├── ai_generator.py   # AI 生成，自动切视觉模型，退避重试
│   │   ├── batch_runner.py   # 批任务编排（进度/断点/取消/熔断）
│   │   ├── reducer.py        # 总册汇编（目录 + 出处锚点，不重写正文）
│   │   ├── errors.py         # AI 错误分类与友好文案
│   │   ├── coverage.py       # 覆盖率回检（术语级比对，纯本地零成本）
│   │   ├── agent_repair.py   # P2：verify→repair 循环 + 三个收敛 guardrail
│   │   ├── mcp_client.py     # P2：宿主侧 MCP 会话封装
│   │   ├── doc_patch.py      # P2：补丁插入（「只补不改」由程序结构保证）
│   │   ├── cost_estimate.py  # 生成前成本预估（按服务商单价粗估 token 与费用）
│   │   └── user_config.py    # 用户 API Key 存储（%APPDATA% settings.json）
│   └── static/             # 桌面页面入口 index.html，样式和脚本位于 desktop/
├── scripts/
│   ├── _pptx_utils.py      # 自动发现 .pptx（命令行可不传文件名）
│   ├── diagnose.py         # 提取覆盖率诊断 + 视觉成本估算
│   ├── mcp_probe.py        # P2：MCP stdio 协议探针（Windows 兼容性验证）
│   ├── fix_pywin32.py      # P2：修「uv 装 pywin32 不完整」（可复用）
│   ├── _coverage_io.py     # 覆盖率脚本共用读取（优先用当时落盘的提取结果）
│   ├── backfill_coverage.py # 给存量批次零成本回填覆盖率（不调模型）
│   ├── verify_coverage.py  # 覆盖率体检 / 回归验收（支持外部文档对照）
│   ├── verify_repair_trigger.py # P2：零误触发验收（本地判定，零成本）
│   ├── smoke_repair.py     # P2：真实冒烟（人为削弱 → 真模型补漏）
│   ├── p2c_acceptance.py   # P2：边际成本曲线实验
│   ├── evaluate_generation.py # 生成质量消融评测（三配置 × 五指标）
│   ├── e2e_exam_flow.py    # 往年试题端到端回归（直搬 / 预测两种模式）
│   ├── step0_generate.py   # 完整链路验证（提取 → 视觉 → 生成）
│   ├── preview_images.py   # 图片联系表，肉眼判断哪些值得发
│   └── make_sample.py      # 生成样例 PPT（评测的 ground truth 来源）
├── docs/                   # 项目描述 / 架构 / 版本记录 / 环境 / 打包
├── eval/                   # 评测结果与通用题库样例（generation_eval_results.json 等）
├── conftest.py             # 让 uv run pytest 能导入 app 包
├── tests/samples/          # 样例文件（make_sample.py 生成）
└── uploads/                # 上传的课件及导出的图片（24h 自动清理）
```

## 📝 API

| 方法 | 路径 | 说明 |
|------|------|------|
| POST | `/api/extract` | 上传课件（`.pptx` / `.pdf`），提取内容并导出图片（`?with_images=true`） |
| POST | `/api/exam` | 解析往年试题（文本或文件），返回 exam_id |
| POST | `/api/generate` | 基于已上传的课件生成复习文档 |
| POST | `/api/batch/peek` · `/api/batch/start` | 批量：单份速览估价 / 建批并后台串行生成（详见 [P0-ARCHITECTURE.md](./docs/archive/desktop/P0-ARCHITECTURE.md)） |
| GET  | `/api/batch/{id}/coverage/{idx}` | 取某节覆盖率明细（疑似未覆盖条目 + 出处页码），供前端下钻 |
| GET  | `/api/health`  | 健康检查 |
| GET  | `/api/settings/status` | 是否已配置 key（configured / provider / 档位 tier / 来源 / 掩码尾部） |
| GET  | `/api/settings/providers` | 设置页服务商下拉（含模型映射、档位 tiers、价格说明与充值入口） |
| POST | `/api/settings/estimate` | 按当前服务商与字数/图片数粗估一次生成的 token 与费用 |
| POST | `/api/settings/verify` | 发 max_tokens=1 微型请求验证 key，区分 6 类失败原因（按档位给指引） |
| POST | `/api/settings/save` · `/clear` | 保存 / 清除用户 key 与档位（保存后热重载立即生效） |

服务商价格（元/百万 tokens，2026-09-04 官方定价页）：DeepSeek flash 高峰 3/9（空闲半价）、
**智谱默认付费档**（glm-4.7 2/8 + glm-4.6v 2/6，设置面板可切换免费档：glm-4.7-flash /
glm-4.6v-flash 均 0 元——免费档是共享资源池，高峰易超载 HTTP 429，且需实名认证）、
Kimi k2.6 6.5/27、通义 qwen3.7-plus 2/8。
生成前会先粗估本次花费展示给用户；余额不足/限流报错会带充值入口与「换一家」指引
（免费档不提示充值，改指引实名认证/稍后重试）。

`/api/generate` 请求体新增 `use_vision`（默认 `true`，是否送图）与 `thinking`（默认 `false`，
是否开启思考模式——2026-09 核实四家服务商思考默认全开，关思考省 30-50% 费用且更快，
故产品默认关闭；`/api/settings/estimate` 同样接受 `thinking` 以估算一致）；响应新增
`used_vision`、`image_count`，方便确认图片到底有没有被送进去。

## ⚠️ 已知限制

- `.ppt` 旧格式无法解析，会在接口层明确拒绝并提示转存
- 扫描版幻灯片（整页都是图片）需要整页渲染，目前未实现
- 公式编辑器产生的 WMF/EMF 依赖 Pillow 在 Windows 上借 GDI 解码，转不出来会跳过
- 单请求上限 600 张图 / 32MiB，超出部分直接丢弃而不是报错
- 图片按 384 tokens/张封顶，模型侧会缩到约 800×800，**密集小字可能看不清**
- `uploads/` 上传副本保留 24h 后自动清理（.env 可调 `UPLOAD_TTL_HOURS`）
- **覆盖率是「词面」判据，不是语义判据**：术语没出现也可能只是被换了说法，
  因此一律显示为「疑似未覆盖」；要彻底消除这类误报需要上 embedding 或再花一次模型调用，
  目前认为不值得（见 [docs/P1-COVERAGE.md](docs/archive/desktop/P1-COVERAGE.md) 的取舍说明）
- 覆盖率**只对「复习指南 / 详细」有意义**——习题生成、重点总结、精简本就不追求全覆盖，
  这些档位不计算覆盖率，界面会说明原因而不是给一个误导性的低分
- 术语数少于 8 个时不给百分比（显示「覆盖样本不足」）：样本太小时一个词的出入就是几十个百分点
- 停用词表**刻意保留**了 `monitoring` / `architecture` / `mechanism` 这类「可能是真概念」的词，
  在某些课件里它们其实只是幻灯片版式标签，会出现在疑似清单里。取舍是宁可少报不冤枉

## 🔜 后续规划

- [ ] 整页渲染兜底（PowerPoint COM / LibreOffice），覆盖扫描版 PPT
- [ ] 超长 PPT 分块 map-reduce，避免一次性塞爆上下文
- [ ] 覆盖率：对「疑似未覆盖」做一次轻量 LLM 复核（区分真漏 vs 换了说法），
      把词面判据的误报压下去——只在用户点开清单时按需触发，控制成本
- [ ] 覆盖率：把「疑似遗漏」沉淀成「该节建议重点复习」的提示，直接指导复习顺序

## 🧪 运行测试

```bash
uv run pytest                      # Python：141 个用例
node tests/test_coverage_ui.js     # 前端覆盖率渲染逻辑（用最小 DOM 桩跑真实代码）
```

前端没有引入测试框架，但覆盖率徽标的 DOM 拼装用了一个轻量办法验证：
把 `index.html` 里**真实的渲染函数**抽出来，在最小 DOM 桩上执行并断言结构与文案
（不是复制粘贴代码，避免两处漂移）。理由：`node --check` 只能查语法，抓不出运行时与
结构错误；而给一个自用工具上一个完整前端测试框架不划算。
