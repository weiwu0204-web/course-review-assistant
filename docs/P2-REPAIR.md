# P2 设计稿 · verify → repair 闭环（MCP + Function Calling）

> 日期：2026-09-15 ｜ 前置：P1 覆盖率可见化（B1–B4，v0.8.9）已完成并全量回归
> 一句话：**把覆盖率回检从「给人看的报告」升级为「给 Agent 用的反馈信号」**——
> 生成 → 回检 → 缺失 → **Agent 调 MCP 工具定向补漏** → 再回检 → 收敛。

---

## 0. 为什么做这个（不是别的）

1. **简历最重的缺口只有它能填**：三份简历报告的一致结论——"面试官会质疑『**应聘 Agent 岗
   却无 Agent 项目**』"。改简历文字无解，必须真的做一个带工具调用的 Agent。
2. **独立评估器与我们原计划撞在了同一处**：报告建议"Function Calling 编排工具 + 状态图
   管理生成/回检/重试分支"，正是本设计（它多给的 MCP 也吸收了）。
3. **与实习经历同构**：实习的"AI 生成 → 校验器 → 真机实测 → 缺陷回流约束生成"就是
   verify → repair。做完这条，**实习 / 项目一 / 项目二串成同一条证据链**。

---

## 1. 架构与关键决策

```
run_single（现有生成逻辑，一行不改）
  │ ① 正常生成 markdown
  │ ② 覆盖率回检（P1 已有）
  ▼
③ 触发判定：applies_to(复习指南/详细) 且 reliable 且 percent < REPAIR_TRIGGER(94)
  │ 否 → 原样返回（零成本路径）
  ▼ 是
④ repair Agent 循环（新增，≤ 3 轮）
  ┌─────────────────────────────────────────────────────┐
  │  MCP Server（子进程，stdio，持本节上下文）             │
  │    check_coverage()   → 当前覆盖率/缺失清单            │
  │    list_missing()     → [{term, page}]               │
  │    read_source_page(p)→ 源页文本（含备注）             │
  │    submit_patch(...)  → 校验 + 插入 + 即时回检         │
  │                                                      │
  │  模型（带上述 tools，自主决定调用顺序与停止）            │
  │  程序侧 guardrail：轮次上限 / 成本上限 / 停滞检测      │
  └─────────────────────────────────────────────────────┘
  ▼
⑤ 产出：修好的 doc（原文备份可回滚）+ RepairRecord 全量留痕
```

### 关键决策（每条都定了"放弃的替代"，这就是面试答案）

| 决策 | 选择 | 理由 | 放弃的替代及原因 |
|---|---|---|---|
| 工具协议 | **MCP**（官方 Python SDK，stdio 子进程） | 行业标准协议；**工具独立成服务后可被任何 MCP 客户端复用**；跨进程权限边界清晰（模型只能碰这 4 个工具，读不到文件系统） | 进程内直接调函数——更简单，但那不叫 MCP 集成 |
| 循环驱动 | **模型自主**（Function Calling，程序不编排步骤） | 模型自己决定"先看缺什么 → 读哪页 → 提交补丁 → 验证"，这才是 Agent；程序只做 guardrail | 程序编排式 pipeline（固定顺序调工具）——那只是自动化脚本 |
| 补漏形态 | **结构化 patch（JSON）+ 程序插入** | **"只补缺失、不重写全文"由程序结构保证**——提示词约束模型可能违反，程序插入不会 | 让模型输出修订后的全文——会引入新缺失，且破坏"原文可回滚" |
| 编排框架 | **不引 LangGraph** | 循环只有「补漏 → 验证 → 继续或停」三个状态，最简 while 足够 | LangGraph——过度设计；"我评估过、结论是不需要"比"我用过"更难被驳倒 |
| 触发阈值 | **94**（= P1 verdict 的"良好"分界） | 与 P1 标定好的阈值体系一致；实测我们自己 14 节全 ≥95，**不会误触发** | 低于 100 都修——为凑分花钱，违背"宁可少报不冤枉"原则 |
| 档位限制 | 只对「复习指南 / 详细」 | 与覆盖率口径一致：其它档位本就不追求全覆盖 | 无 |

---

## 2. MCP 工具接口（4 个，签名定死）

MCP Server 为**每节一个会话**：宿主（FastAPI 进程）在启动 repair 时以子进程拉起 server，
通过一个**宿主专用**的初始化调用注入上下文（该节的 ExtractionResult + 当前 markdown），
模型可见的只有下列 4 个工具。

```python
check_coverage() -> dict
    # {percent, total_terms, missing_terms, reliable,
    #  missing_detail: [{term, page}, ...],
    #  outline: ["知识点", "部署步骤", ...]}   ← 模型看不到全文，靠它选插入位置
    # 对「当前待修文档」计算；每次 submit_patch 成功后自动基于新文档更新

list_missing() -> dict
    # {count, missing: [{term, page}, ...]}
    # 单列出来是因为模型的第一步通常是它（语义更直白、更省 token）
    # ⚠️ 返回 dict 而不是裸 list：实测 FastMCP 会把 list **拆成多个 content 块**，
    #    客户端只取第一块就会静默丢数据（见 §9.1）

read_source_page(page: int) -> dict
    # {page, text, truncated}
    # text = 该页幻灯片文本 + 备注；超过 3000 字符截断并置 truncated=true
    # page 越界 → 返回错误信息（模型可改用 list_missing 拿到的合法页码）

submit_patch(insert_after: str | None, content: str) -> dict
    # insert_after：目标小节标题（须与文档中 ## 标题完全一致）
    #   - 省略 / 找不到 → fallback：追加到文末「## 知识点补充（自动补漏）」小节
    #   - fallback 恒成功，模型永远能提交
    # content：补充内容，须包含来源标注（如「（源：第 7 页）」）
    # 校验（拒绝则返回原因，模型可修正后重交）：
    #   - 非空且 ≤ 2000 字符
    #   - 与上一轮提交的内容不得相同（防重复塞同一段）
    # 返回 {applied, new_percent, located_by: "heading"|"fallback", reason?}
```

**权限边界（MCP 的卖点之一）**：模型拿不到文件系统、拿不到原 pptx、只能通过这 4 个工具
观察和修改一个内存中的文档副本——**最小权限**，写进文档供面试讲。

---

## 3. Agent 循环（P2b）

### 3.1 循环体

```python
round = 0
while round < MAX_ROUNDS(3) and cost < BUDGET and not converged:
    resp = client.chat.completions.create(model=…, messages=…, tools=MCP_TOOLS)
    if resp 无 tool_calls:  break        # 模型自主判断已收敛
    执行 tool_calls（经 MCP client 转发到 server 子进程）
    把工具结果回填 messages；round += 1
```

- 现有代码用的是 openai 风格 `client.chat.completions.create`（ai_generator.py:307,319），
  **`tools` 参数即 Function Calling 通路，deepseek / glm 均支持**——无需换 SDK。
- **停滞检测**：连续 2 轮覆盖率提升 < 1pp → 在下一轮 system 消息里明示模型"提升停滞，
  建议停止"，仍由模型决定（guardrail 只在超限后强制中止）。

### 3.2 降级模式（provider 不支持 tools 时）

启动 repair 前先探测（发一个带 tools 的 1-token 请求）：
- 支持 → 主模式（模型自主）
- 不支持 → **降级为程序编排**：程序按 `list_missing → read_source_page(每个缺失页)
  → 生成 patch → submit_patch` 固定顺序走一遍。**4 个 MCP 工具仍然全用**（协议层价值
  不变），只是失去自主性。meta 里如实记录 `mode: "orchestrated" | "autonomous"`。

### 3.3 system prompt 要点（防凑词是最重要的一条）

1. 任务：把「疑似未覆盖」的术语补进文档，**只补缺失，不改写已有内容**
2. **防凑词约束**：补充内容必须基于 `read_source_page` 读到的真实课件内容，
   为每个术语给出**实质说明**；**禁止只罗列术语凑覆盖率**——宁可少补，不可凑词
3. 每条补充标注来源页码
4. 提示工具的典型用法与"何时该停"（覆盖率 ≥94 或两轮无提升）

---

## 4. 数据结构与落盘

```python
@dataclass
class RepairRound:
    round: int
    tool_calls: int
    prompt_tokens: int
    completion_tokens: int
    cost_yuan: float
    percent_before: int
    percent_after: int

@dataclass
class RepairRecord:
    triggered: bool = False
    mode: str = ""            # autonomous | orchestrated
    trigger_percent: int = -1
    rounds: list[RepairRound]
    total_cost_yuan: float = 0.0
    final_percent: int = -1
    rolled_back: bool = False
```

- `GenerateOutcome` 增加 `repair: RepairRecord | None`（None = 未触发，与 coverage 的
  None 语义一致——**不伪装成 0**）
- 原文备份：`doc.md.pre-repair`（repair 前无条件写）；**回滚条件**：最终覆盖率比触发时
  更低，或 markdown 结构损坏（标题层级数异常）
- meta.json / API 响应带 repair 摘要；前端徽标旁加一行「已自动补漏（N 轮，¥x.xx）」
  ——**用户必须知道文档被改过**（诚实性约束）

---

## 5. 验收标准（事先定死，沿用 P1 的纪律）

> ✅ **八项已全部跑完，实测数据见 [P2-ACCEPTANCE.md](./P2-ACCEPTANCE.md)**（2026-09-15）。

> ✅ **八项已全部跑完，实测数据见 [P2-ACCEPTANCE.md](./P2-ACCEPTANCE.md)**（2026-09-15）。

| # | 验收项 | 判据 | 手段 |
|---|---|---|---|
| 1 | **可控实验：能把低覆盖修回来** | 取 Ceph 课 2 节，用脚本删掉覆盖特定术语的段落到 ~80% → repair 后 **≥93%**（回到"良好"档），轮次 ≤3 | 程序化构造（可复现），记录全程 |
| 2 | **零误触发** | M3 全量 14 节（97.7%，全 ≥95）跑一遍 → **repair 一次都不触发**，零额外成本 | 批量回归 |
| 3 | 成本 | 实验 1 中 repair 增量成本 ≤ 原生成成本的 **40%** | 记账 |
| 4 | 边际成本曲线 | 每轮覆盖率提升 vs 花费 → **「每提升 1 个百分点花多少钱」** | 实验数据绘图 |
| 5 | **防凑词抽查（人工）** | 随机抽 20 条补充段落，「含实质说明而非术语罗列」比例 **≥80%** | 我抽查 + 你复核 |
| 6 | 回滚 | 构造一次"越修越差"（如 patch 全是凑词）→ 自动回滚且记录 | 单测 + 构造 |
| 7 | 单测 | 工具接口 / patch 插入与 fallback / 三个 guardrail / 降级模式 | pytest |
| 8 | 速度 | 单节 repair 全程（≤3 轮）≤ 5 分钟 | 实测 |

> 验收 2 是"零成本路径"的证明：**这个功能对已经良好的文档分文不花**——
> 这句话本身就是简历/面试素材。

---

## 6. 诚实性约束（与 P1 同级，写死在实现里）

1. **repair 全程留痕且用户可见**：补了几轮、花了多少钱、从多少到多少——不悄悄改文档
2. 补充内容**必须带来源页码**；凑词是验收 5 的否决项
3. **词面覆盖 ≠ 内容质量**：repair 提升的是词面指标，沿用 P1 的措辞——
   「疑似未覆盖 → 已尝试补全」而不是「已 100% 覆盖」
4. 回滚备份保留原文，**用户可以不要 repair 结果**（`doc.md.pre-repair` 就是原版）
5. 降级模式如实标注 `mode`，不冒充自主 Agent

---

## 7. 分批与工作量

| 批 | 内容 | 预估 | 花钱？ |
|---|---|---|---|
| **P2a** | `app/mcp_server.py`（4 工具 + 会话上下文）+ MCP client 封装 + 单测 | 2–3 天 | **0 元**（不接 LLM） |
| **P2b** | `app/services/agent_repair.py`（循环 + guardrail + 降级）+ run_single 接入 + 落盘 + 前端标记 | 3–5 天 | 联调少量真调用（约 ¥1–2） |
| **P2c** | 实验 1/2 + 边际成本曲线 + 人工抽查 + 文档 + CHANGELOG | 2–3 天 | 实验 1 约 ¥1–2 |

**新依赖 1 个**：`mcp`（官方 Python SDK，加进 requirements.txt）。
理由：见 §1 决策表——行业标准 + 工具可复用 + 最小权限边界。

**已知风险与探针**（P2a 第一天先做，不通过就调整方案）：
1. `mcp` SDK 的 stdio transport 在 **Windows** 上的兼容性 → 先写 hello-world 探针验证
2. 各 provider 对 `tools` 参数的支持差异 → 探测逻辑 + 降级模式双保险

---

## 8. 面试怎么讲（这段就是简历素材的雏形）

> 我给 LLM 文档生成系统做了个 **verify → repair 的 Agent 闭环**：生成后用本地术语比对
> 做回检（不花一分钱），覆盖率低于阈值时，一个带 **MCP 工具**的 Agent 自主补漏——
> 它能查缺失清单、读源课件页、提交结构化补丁、即时验证效果，自己决定什么时候收敛。
> 程序侧只做 guardrail（轮次/成本上限、防凑词、可回滚）。可控实验里我把一份 80% 覆盖
> 的文档修回 93%，增量成本控制在生成的 40% 以内；对本来就良好的文档**零触发零成本**。
> 没用 LangGraph——循环只有三个状态，我的判断是不需要。

**对应简历技术栈栏（P2 做完后才写）**：MCP · Function Calling · Agent 循环与收敛控制
——全部有实物支撑，每个都能当场演示。

---

## 9. 实现记录（P2a · 2026-09-15）

### 9.1 设计稿的两处调整（都是实现时才暴露的）

| 原设计 | 实际实现 | 为什么必须改 |
|---|---|---|
| `check_coverage` 只返回覆盖率 + 缺失清单 | **增加 `outline`**（文档二级标题列表） | **原设计有个缺口**：模型看不到文档全文（最小权限），那它凭什么指定插入位置？不给大纲它只能瞎猜标题，内容会全部落进兜底小节——等于白设计 |
| `list_missing` 返回裸 `list` | 返回 **`{count, missing: [...]}`** | **FastMCP 把 list 返回值拆成多个 content 块**（每块一个 JSON 文本），客户端只取第一块的写法会**静默丢数据**。单测当场抓到（拿到的是列表首项而非列表）。包一层 dict 让契约稳定 |

> 这两条都是"单测/探针发现的，不是想出来的"——**先跑起来再改设计**比先写完再看强。

### 9.2 踩到的坑：uv 装 pywin32 不完整（阻塞性，已解决）

**现象**：`uv pip install mcp` 之后 `import mcp` 直接炸：

```
ModuleNotFoundError: No module named '_win32sysloader'
  （发生在 site-packages/win32/lib/pywintypes.py）
```

**排查路径**（值得记）：

1. 先怀疑版本：mcp 2.2.0 的 METADATA 里有 `pywin32>=311; sys_platform=='win32'` → **降级到 1.30.0**
   → ❌ 仍然报错（1.x 同样 import pywintypes）。**"降级能绕开"是错的直觉**。
2. 查 RECORD 与实例目录的差异 → **wheel 里 611 个文件，实际缺 264 个**：整个 `win32com/`、
   `win32comext/`，以及 `win32/_win32sysloader.pyd` 等 12 个 `.pyd`。
3. **根因**：pywin32 的 wheel 依赖安装后的 post-install 步骤落位部分文件，
   而 **`uv` 出于"不执行任意安装脚本"的设计不跑这一步**。

**修法**：从镜像下载**同版本** wheel，按 RECORD 补齐缺失文件（不写系统目录、不需管理员）。
已固化为可复用脚本 `scripts/fix_pywin32.py`（幂等，`--check` 只报告）。

> **可迁移的教训**：`uv` 建的环境遇到"装上了却 import 不了"的包，
> 先拿 wheel 的 RECORD 和实际目录**做一次 diff**——比猜版本、猜依赖快得多。
> 另外：`ZipFile` 不 close 会导致 Windows 上文件删不掉，`finally` 清理必须容错。

### 9.3 P2a 交付与验收

| 项 | 实际 |
|---|---|
| 代码 | `app/mcp_server.py`（Server + 会话）、`app/services/mcp_client.py`（宿主侧）、`app/services/doc_patch.py`（插入纯函数） |
| 探针 | `scripts/mcp_probe.py` —— Windows stdio 三项检查**全绿**（列工具 / 调用取结果 / 中文参数往返） |
| 修复工具 | `scripts/fix_pywin32.py`（幂等，可拷到其他 uv 项目复用） |
| 单测 | `tests/test_mcp_server.py` 12 个用例：纯函数层 → 会话层 → **真起子进程走 MCP 协议的端到端** |
| 全量回归 | **153 passed + 1 skipped**（P2a 前 141） |
| 成本 | **0 元**（完全不调模型） |
| 新依赖 | `mcp>=1.30,<2`（锁 <2：2.x 额外拉 httpx2 / opentelemetry，1.30 已够用） |

**下一步（P2c）**：跑验收标准 §5 的两组实验（可控实验 + M3 全量零误触发回归）
+ 边际成本曲线 + 防凑词人工抽查 + CHANGELOG/README。

---

## 10. 实现记录（P2b · 2026-09-15）

### 10.1 交付

| 项 | 内容 |
|---|---|
| `app/services/agent_repair.py` | Agent 循环（自主 + 降级两条路径）、三个 guardrail、回滚、成本记账 |
| 接线 | `run_single` 加 `enable_repair`（默认关）+ `repair_workdir`；`batch_runner` 逐 job 记录与记账；前端勾选框与补漏徽标 |
| 测试 | `tests/test_agent_repair.py` **11 个用例**（假 LLM + 真 MCP 子进程） |
| 冒烟 | `scripts/smoke_repair.py` —— 用真实课件人为削弱的可控实验 |
| 全量 | **164 passed + 1 skipped** |

### 10.2 ★ 真实冒烟结果（这是 P2b 最值钱的产出）

用 M3 实跑留下的真实课件（19 页）+ 人为删掉 30% 小节制造低覆盖：

| | |
|---|---|
| 覆盖率 | **58% → 100%**（该文档原版本身只有 67%，即 repair 补回了原生成漏掉的内容） |
| 模式 / 停止原因 | `autonomous` / `reached_target` |
| 轮次轨迹 | 第 1 轮侦察（2 次工具）→ 第 2 轮读源页（8 次）→ 第 3 轮提交 4 个补丁：58→64→86→91→**100%** |
| 总成本 | **¥0.059** |
| 补入内容 | 带来源页码（「源：第 7、8 页」），是有结构的实质知识（对比表、由来、生活类比），**不是术语罗列** |

### 10.3 ★★ 三个只有"真跑"才能发现的问题

**① 停滞检测把纯侦察轮也算成停滞 → 模型一行都没补上就被掐死（最严重）**

第一版冒烟的结果是 `58% → 58%`、补丁落地 **0 个**、`stop_reason=stalled`。加日志看清轨迹：

```
第1轮 check_coverage + list_missing     ← 侦察
第2轮 read_source_page × 4              ← 读源页
→ 被判"覆盖率没提升"（停滞），退出
```

**真实模型的节奏是「先侦察 → 再读源页 → 最后提交」**，前两轮覆盖率必然不动。
而我的判定从"触发时的覆盖率"起算，等于把起步动作当成了空转。

**修法**：只有**这一轮调用过 `submit_patch`** 的轮次才参与停滞判定（纯侦察轮不计）。

> ⚠️ **这个问题单测发现不了**——假 LLM 的剧本是我自己写的，我会让它直接提交补丁；
> 只有真模型才会"先看两轮再动手"。这是"**真实冒烟不可替代**"的最好例证；
> 也说明**把工具调用轨迹打成日志**这件事本身值——没有那几行日志，我只会看到
> "调了 6 次工具但 0 个补丁"，完全不知道为什么。

**② 轮次上限 3 不够**：真实节奏需要"1 轮侦察 + N 轮读页 + M 轮提交"，
3 轮在缺失较多时刚够到提交就结束了。→ 提到 **5**（真正的成本闸门是 `COST_RATIO_LIMIT`）。

**③ `final_document()` 在"模型没提交任何补丁"时抛 FileNotFoundError**：
server 只在**成功应用补丁后**才写盘，所以"模型判断不需要补"这一**正常结果**会变成 error。
→ 改成：文件不存在/读失败时返回会话开始时的文档。

（另修一个接线期的问题：`_tool_schemas` 拿到的是宿主侧 `RepairClient` 包装，
却按底层 `ClientSession` 的接口调 `list_tools()` → AttributeError。已给包装补上该方法。）

### 10.4 批量侧的一个坑（值得记）

worker 要不要给生成函数传 `repair_workdir`，一开始是按"`generate_fn is None`"判断的。
但**测试是 monkeypatch 掉 `br.run_single`** 的（`generate_fn` 仍是 None），
于是把一个 3 参数的假函数当成支持新参数的真实现 → 多传一个关键字参数 TypeError
→ **整个批次变 `partial`**。

**修法**：按 `inspect.signature(...)` 判断目标函数是否真的接受该参数 ——
**"谁会被调用"和"调用者以为在调谁"是两回事**。
