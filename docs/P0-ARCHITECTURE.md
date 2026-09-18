# P0 架构设计：批量生成「整门课复习总册」

> 状态：**M1 已实现（0.7.0 后端 + 前端；0.8.0 支持 PDF 课件），待用户实测**
> 背景：与通用 agent 竞争的差异化定位是「整门课=一份文档 / 覆盖率可见化 / 真题闭环」。
> 现状（0.6.x）只兑现了「单份 PPT → 单份复习文档」，P0 第一次站到通用 agent 做不了的战场上。

## 实施进度（更新于 2026-09-10，0.8.0）

| 模块 | 状态 | 位置 |
|---|---|---|
| 错误翻译/分类服务 | ✅ | `app/services/errors.py` |
| run_single + usage 透出 | ✅ | `app/services/ai_generator.py`（generate_review_document 保留兼容壳） |
| reducer 汇编合成 | ✅ | `app/services/reducer.py` + `batch_runner.rebuild_master()` |
| BatchRunner 编排/断点/熔断 | ✅ | `app/services/batch_runner.py` |
| 批 API（8 端点，含 peek） | ✅ | `app/api/batch_routes.py`（main.py 已注册） |
| 多格式输入（.pptx / .pdf） | ✅ | `app/services/extractor.py` 统一分发（0.8.0） |
| 进程重启后续跑 | ✅ | `_detail()` 把悬空 running 降级报告为 paused（0.8.0 修） |
| 测试 | ✅ 103 passed | test_batch_runner(11) / test_reducer(5) / test_batch_api(9) / test_pdf_extractor(17) |
| **前端批量视图** | ✅ | index.html：tab 化 / 文件列表排序 / peek 预估 / 2s 轮询进度 / 费用累计 / 总册+节切换 / 刷新恢复 |
| 用户实测（M1 验收） | ⬜ | 真实多份课件跑通——**步骤见 [BATCH-TEST-GUIDE.md](./BATCH-TEST-GUIDE.md)** |

实现与设计稿的偏差：① API 增加 `POST /api/batch/peek`（单份速览估价）；② `run_single`
直接并入 ai_generator（原设计的独立入口函数是空转包装）；③ 恢复续跑时 error job
**不**自动重跑（需显式 retry），防止重复计费。

## 1. 目标与价值验证

**P0 一句话定义**：用户一次拖入一门课的 3–20 份课件（.pptx / .pdf），系统逐份生成课节复习文档
（map），最终合成**一份**整门课复习总册（reduce）；全程可见逐份进度与费用，
进程中断可续、失败单份可单独重试。

**验收即价值验证**：拿一门真实课程（8–12 节）实测跑通，对照当初与通用 agent 的
对比口径记录证据：

| 当初的差异化论点 | P0 的兑现方式 | 度量口径 |
|---|---|---|
| 通用 agent 上下文装不下整门课 | 每份课件独立占用自己的请求窗口，N 份互不挤占 | 10 份 × 60 页跑通，无「超长被拒」 |
| 无法承诺逐页覆盖 | map 沿用现有逐页提取管线，单份覆盖与现状一致 | 单份输出与单份模式对比不劣化 |
| 不可复现 | 批任务状态落盘（`batch.json`），重启可续、失败可重试 | 断电后恢复续跑通过 |

## 2. 范围边界

### 做（P0 范围）

- 多文件上传与批任务编排（后台单线程串行执行 + 前端轮询进度）
- map：每份复用现有「提取 + AI 生成」管线，产出**课节片段文档 + 结构化元数据**
- reduce：**汇编式**合成总册（自动目录 + 节标题 + 出处锚点），不调用 LLM 重写正文
- 任务状态落盘、断点续跑、失败单份重试、批前费用预估 + 批中累计

### 不做（明确留给后面）

| 项 | 归属 | 原因 |
|---|---|---|
| 知识顺序语义重排 / 跨课件聚类 | P1/P2 | 老师讲义顺序通常≈知识顺序，先默认课件顺序 |
| 覆盖率回检（知识点 ↔ PPT 页覆盖比对） | P1 | 依赖总册结构稳定后才好做 |
| 多 job 并发、并行度调优、队列化 | P2 | 共享池/免费档 429 下并发无益；串行已够用 |
| 单份内多轮续写（解决超长截断） | P1 | M1 先「标注截断」，不阻塞主线 |

## 3. 总体架构

**决策：加两层，不动现有单份层的调用地基。** Map 是现有管线的批化包装，
Reduce 是新增的轻量汇编器。

```
前端（批量视图 / 新增 tab）
  多选 .pptx + 参数(模板/风格/视觉/思考/课程名) + 顺序调整
        │ POST /api/batch/start（multipart, files[]）
        ▼
后端 BatchRunner（进程内后台线程, 串行执行 job）
  for job_i in jobs:                      ← 每轮检查 cancel flag
    ├─ 提取一次: extract_pptx()               （同一份不重复解析）
    ├─ 生成:     generate_review_document()   （现有管线原样复用）
    ├─ 落盘:     output/<batch_id>/NN_<slug>/doc.md + meta.json
    └─ 记账:     更新 batch.json（进度/费用/错误, 写 tmp 再 rename 原子替换）
        │
        ▼ 全部 done / error
Reducer（纯汇编, 不调 LLM 重写正文）
  读各 job doc.md（保留正文）+ meta.headings（取 H1/H2 做目录）
        ▼
  output/<batch_id>/总复习册-<课程名>.md   ← 每个 job 完成后增量重建, 随时可下载最新版
```

关键点：每份课件独立占用自己的请求窗口（token / 图片上限只作用于单份），
所以**批的规模不受单请求硬上限约束**——这正是通用 agent 做不到的原因。

## 4. 数据模型与落盘结构

新增根目录 `output/`（与 `uploads/` 同级；打包轮与 UPLOAD_DIR 一起迁到可写用户目录，
见 PACKAGING.md 待办）。

```
output/<batch_id>/
├── batch.json                      # 批状态（每 job 完成后原子更新）
├── 01_第一章-计算机网络概述/doc.md + meta.json
├── 02_第二章-物理层/doc.md + meta.json
└── 总复习册-计算机网络.md           # reducer 增量产物
```

### BatchState（batch.json）

| 字段 | 类型 | 说明 |
|---|---|---|
| batch_id | str | uuid4().hex[:8] |
| course_name | str | 用户填的课程名，缺省用第一份文件名 |
| status | str | running / done / partial(有失败) / cancelled |
| provider / tier / 生成参数 | - | 快照当次配置，与单份一致 |
| jobs | list[JobState] | 见下 |
| cancel_requested | bool | worker 在 job 边界检查 |
| total_est_cost | float | 批前 sum(estimate)，批中累计实际 |
| created_at / updated_at | str | |

### JobState

| 字段 | 类型 | 说明 |
|---|---|---|
| idx | int | 章节序号（= 用户排序） |
| name | str | 原始文件名（slug 用） |
| status | str | waiting / running / done / error |
| total_pages / image_count / chars | int | extract 结果（先展示给用户） |
| cost_yuan | float | 实际 usage 计费；无 usage 则记估算 |
| warning | str \| "" | 如 truncated（done 但内容可能不完整） |
| error | str \| "" | 友好文案（复用 _friendly_ai_error，需从 routes.py 提升为公共函数） |
| doc_md | str | 相对路径 output/<batch_id>/NN_<slug>/doc.md |
| started_at / finished_at | str | 用于前端 ETA |

### JobMeta（meta.json）

| 字段 | 说明 |
|---|---|
| headings | 从 doc.md 解析出的 H1/H2 列表，reducer 目录的唯一输入 |
| usage | prompt_tokens / completion_tokens（response.usage 有则记） |
| exam_used / used_vision / truncated | 对齐 GenerateResponse 现有字段 |
| 其余 | filename / pages / chars / 时间 |

**决策：费用优先记实际 usage。** `generate_review_document` 目前丢掉了
`response.usage`，P0 改造为透出 `(prompt_tokens, completion_tokens)`，
按当前服务商单价算实际费用；无 usage 时回退 estimate 值并在 UI 标注「估算」。

## 5. Map 层：单份管线的最小改造

### 5.1 抽 `run_single()` 公共入口

现状 `routes.py::generate` 是唯一入口（内部自己 `extract_pptx`）。BatchRunner
与它共用同一实现会互相漂移，故：

- 新增 service：`run_single(extracted: ExtractionResult, request, exam_text) -> GenerateOutcome`
- `GenerateOutcome = (doc_md, image_count, truncated, usage)`，`generate_review_document`
  改造为返回它（不再裸返回 tuple）
- `/api/generate` 端点改为调用 `run_single`（先 extract 再调），行为不变
- BatchRunner 提取一次，把 `ExtractionResult` 传入 `run_single`——**避免同一份
  PPT 在 extract + generate 两处重复解析**（现状端点各自提取，批场景里重复就是浪费）

### 5.2 两个小改造

1. **usage 透出**：见 §4，取 `response.usage`。
2. **headings 解析**：`extract_headings(md)` 正则取 `^#{1,2} ` 行，供 meta 与目录。

### 5.3 必须承认的边界：单份截断

单请求 `max_tokens=16384`，一份 60 页详细输出的复习文档可能触顶截断
（现状单份模式同样存在，UI 已提示「尾部可能缺失」）。批场景不做特殊处理：

- job 标 `done` + `warning="truncated"`，前端该行标黄
- 总册对应节标题下加一行注：「本节输出可能不完整（生成被截断），可对单份重试」
- 根治（单份内多轮续写 / 分块 map）留给 P1，不塞进 P0

## 6. Reduce 层：汇编式合成（不重写正文）

**核心决策：reduce 不调用 LLM 二次改写正文。** 理由：

1. 改写 N 份正文有信息丢失风险（模型压缩/润色必然删细节）；
2. 额外花一份 token 钱，且不可复现；
3. 复习场景冗余无害——第二章 PPT 复习第一章内容，重复出现反而利于复习。

### 输出结构（随时增量重建）

```markdown
# 《课程名》期末复习总册
> 由 N 份课件合成 · 生成时间 · 按课件顺序编排
## 目录
1. 第一章 计算机网络概述 ……（第 1 节）
2. 第二章 物理层 ……（第 2 节）
...
---

## 第 1 节：第一章 计算机网络概述
> 来源：01_计算机网络概述.pptx（共 62 页）

[该份 doc.md 正文原样]

## 第 2 节：第二章 物理层
> 来源：02_物理层.pptx（共 58 页）

[该份 doc.md 正文原样]
```

- 每节之间分隔线 + 出处锚点（文件名 + 页数），保证任何一段都能回溯到哪份课件
- 历年真题/预测题章节若在单份里生成过，随该节正文保留（现状语义：真题按节筛选）
- **目录排序 = 用户拖入顺序**（前端支持上移/下移），不自动重排

### 唯一可选的 LLM 参与点（M2，不做进 M1）

总册完成时用**一次小调用**（输入仅为各份 meta.headings 词条，几十~几百 token）
生成「总册序言 + 跨节重复概念提示清单」（例：「负载均衡在第 3 节与第 8 节均出现，
可对照复习」）。只喂标题、不喂正文，成本可忽略且无改写风险。

## 7. 任务编排与断点（BatchRunner）

### 线程模型

- 进程内 `threading.Thread` 单 worker，**串行**执行 job
- 理由：① 免费档/共享池 429 下并发只会放大限流；② 单机桌面 3–20 份 × 30–120s ≈
  5–20 分钟，串行可接受；③ 实现与测试复杂度最低
- 每份内部的 OpenAI client 已带 `max_retries=3`（429/5xx 指数退避），job 级不再加重试

### 失败处置（不阻塞整批）

| 场景 | 表现 | 处置 |
|---|---|---|
| 单份 429 重试仍失败 | job=error，文案透服务商原文（1305 等 code 保留） | 其余 job 继续；批末用户「重试失败项」 |
| 余额不足 / 402 | 连续余额类错误 | **剩余 waiting job 全部标 error**（避免白打），弹窗引导充值/换家（复用现有文案分叉） |
| 网络断开 | 当前 job=error | 连续 2 个网络类失败 → 自动暂停批并提示检查网络 |
| 单份提取失败（损坏文件） | job=error，文案具体到文件 | 其余继续 |
| 输出截断 | done + warning | 前端标黄，见 §5.3 |

### 断点与恢复

- **落盘点**：每完成一个 job，立即原子更新 `batch.json`（写 `.tmp` 再 `os.replace`）
- **进程被杀 / 断电 / 关窗口**：重启后前端调 `GET /api/batches` 发现 running 批 →
  提示「恢复上次任务」→ 继续轮询；`POST /api/batch/<id>/retry` 只跑 error 与未完成 job
- **取消**：`POST /api/batch/<id>/cancel` 置 flag，worker 在 job 边界检查；
  运行中的当前 job 不打断（避免半截状态），可随后「继续」

## 8. API 契约（新增 `/api/batch/*`）

| 方法/路径 | 请求 | 响应 | 说明 |
|---|---|---|---|
| POST `/api/batch/start` | multipart：`files[]` + 表单（template/style/use_vision/thinking/course_name/exam_id?/exam_mode?） | `{batch_id}` | 先整体校验（全部 .pptx、各文件 ≤50MB、数量 ≤10）再建批 |
| GET `/api/batch/{id}` | - | BatchStatus | jobs 明细 + 总进度 + 累计费用；**轮询 2s** |
| GET `/api/batch/{id}/doc/{idx}` | - | markdown | 单节下载/预览 |
| GET `/api/batch/{id}/master` | - | markdown（FileResponse） | 总册（最新增量版，未全完也返回当前版） |
| POST `/api/batch/{id}/retry` | body: `{indices?: [...]}` | 新 BatchStatus | 缺省 = 全部 error/未完成 |
| POST `/api/batch/{id}/cancel` | - | `{ok: true}` | 停止调度新 job |
| GET `/api/batches` | - | 批历史列表 | 页面刷新找回 / 续跑 |

边界规则与单份对齐：文件后缀非 `.pptx` → 400（`.ppt` 给另存为提示）；未配 key →
400 带设置指引；job 的错误文案全部复用现有 `_friendly_ai_error` 分叉
（**把它从 routes.py 提升到 `services/`**，两条路由共用）。

exam 语义（P0 简化）：exam_id 可选，若提供则应用于**每一份** job（与单份模式
「筛选与本份知识点相关的题」一致）；「整门课视角的总册真题章节」留 M2。

## 9. 前端改造点

现状 `index.html` 是单文件流（`handleFile → startGenerate → 结果卡片`），改动：

1. **顶部 tab**：「单份生成 | 整课批量」。单份页保留（价值仍在），两页共享
   参数区渲染函数（模板/风格/视觉/思考/真题控件抽成函数避免双份 HTML 漂移）
2. **批量页**：
   - 多选 `.pptx`（`input multiple`）+ 文件列表（可删除、上移/下移 = 章节顺序）
   - 选完即显示每份页数/图片/单份预估（可先逐个只调 extract 不给 AI 费，
     extract 很快）→ 顶部「整批预计花费约 ¥X」
   - 「开始生成」前弹确认（复用 confirmMask，文案带预估费用与预计时长）
3. **进度视图**：总进度条 + 每行 = 文件状态（waiting/running spinner/done/error 标黄标红）
   + 单份费用；失败行内联「重试」；底部「重试失败项」「停止」
   - 轮询 2s；**关弹窗不杀任务**（任务在服务端跑，刷新可恢复——这是与单份
     模式最大的体验差异，弹窗文案要写明「可关闭本页，稍后回来查看」）
4. **结果视图**：默认展示总册（renderMarkdown 复用），顶部页签切各节；
   下载总册 / 下载单节；页面 url 带 batch_id 便于刷新恢复
5. 新增批状态、费用累计、断点恢复提示的状态渲染（`renderBatch()` 独立函数）

## 10. 费用模型

### 批前预估

```
batch_est = Σ estimate_generation_cost(provider, chars_i, images_i, ...)   # 逐份, 复用现有函数
reduce 成本 ≈ 0（汇编式不调模型；M2 的序言调用 < ¥0.01, 可忽略）
```

### 量级参考（关思考，2026-09 单价，按现有估算常数）

样例：60 页讲义 ≈ 2 万汉字、10 张有效图（单份约 ¥0.1 量级）。

| 服务商 | 文本模型(价 in/out) | 单份 ≈ | 10 份 ≈ | 开思考(×1.5) 10 份 ≈ |
|---|---|---|---|---|
| DeepSeek | deepseek-v4-flash (3/9) | ¥0.11 | ¥1.1 | ¥1.7 |
| 智谱付费 | glm-4.7 (2/8) | ¥0.09 | ¥0.9 | ¥1.3 |
| Kimi | kimi-k2.6 (6.5/27) | ¥0.30 | ¥3.0 | ¥4.5 |

> 单份估算明细：in ≈ chars/1.5 + 1500 + 图×384 token；out ≈ chars/1.5×0.5(详细)×思考系数。
> 视觉走 vision 单价（上表为纯文本价）。以上是数量级参考，以批中累计为准。

### 批中累计

每 job 完成记实际费用（usage × 单价），顶部显示「已用约 ¥X / 预估共 ¥Y」。
费用用尽（余额类错误）触发 §7 的整批熔断，不白烧。

## 11. 里程碑与验收

### M1 = 0.7.0（核心链路）

- [x] 多选上传（上限 20，0.8.3 由 10 上调），逐份串行生成，前端逐份进度轮询
- [ ] 每份产出 doc.md + meta.json；全部完成（或部分失败）后总册可下载
- [ ] 单份失败不阻塞整批，失败汇总 + 单项重试 + 重试失败项
- [ ] 批前费用预估确认弹窗、批中费用累计
- [ ] 取消 / 断点续跑（杀进程后恢复）
- [ ] 单份模式全部回归通过 + 新增 tests/test_batch_runner.py（monkeypatch
      `generate_review_document` 返回固定文本，不真调模型）、tests/test_reducer.py、
      tests/test_batch_api.py（start/status/retry/cancel 状态机）

### M2 = 0.7.x（体验与健壮）

- [ ] 总册序言 + 跨节重复概念提示（轻量 LLM，仅喂 headings）
- [ ] 整门课视角真题章节（exam 汇总到总册末尾，一次调用）
- [ ] 页面刷新恢复运行中任务、ETA 显示、截断节提示下钻
- [ ] HTML/复制按钮对齐批量结果

### M3 = 价值验证门（P0 的最终评估）

- [ ] 一门真实课程（8–12 节）完整跑通，记录表：份数 / 总页数 / 总时长 / 实际费用 /
      失败与重试次数
- [ ] 与通用 agent 对照自测：整门课内容是否装得下 / 逐份覆盖是否可承诺 / 输出是否可复现
- [ ] 数据达标 → 进 P1（覆盖率回检）；不达标 → 回炉本节

## 12. 风险与对策

| 风险 | 对策 |
|---|---|
| 单份输出截断 | M1 标黄提示；0.8.1 上限 16384→32768；P1 做单份内多轮续写 |
| 全批 15–20 分钟偏长 | 串行 + 逐份进度 + 「可关闭页面稍后回」+ 断点恢复 |
| 成本不可控 | 批前预估 + 累计显示 + 停止按钮 + 余额类错误整批熔断 |
| 免费档共享池 429 | 串行执行天然降频 + job 级退避 + 失败文案引导换付费档 |
| 章节间内容重叠 | 汇编不合并（冗余利于复习）；M2 序言标注重复概念位置 |
| 批与单份逻辑漂移 | `run_single()` 单一实现，两个入口共用；单份模式回归测试兜底 |

## 13. 改动文件清单

**新增**

| 文件 | 内容 |
|---|---|
| `app/services/batch_runner.py` | BatchRunner（线程 + 状态机 + 落盘/断点） |
| `app/services/reducer.py` | 汇编式合成 + extract_headings |
| `app/services/errors.py` | `_friendly_ai_error` 从 routes.py 提升至此 |
| `app/api/batch_routes.py` | §8 七个端点 |
| `tests/test_batch_runner.py` / `test_reducer.py` / `test_batch_api.py` | 见 M1 |

**修改**

| 文件 | 改动 |
|---|---|
| `app/services/ai_generator.py` | `generate_review_document` 返回含 usage；新增 `run_single()` |
| `app/api/routes.py` | generate 端点改调 `run_single`；错误翻译改引用 services |
| `app/static/index.html` | tab 化 + 批量视图（文件列表/进度/费用/结果页签） |
| `docs/CHANGELOG.md` | 0.7.0 记录 |

**不动**：`config.py`（AI_PROVIDERS/tier 照旧）、`ppt_extractor.py`、
`cost_estimate.py`（函数直接复用）、`settings_routes.py`。
