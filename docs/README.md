---
status: current
verified: 274dde4
---

# 文档索引（Single Source of Truth）

仓库内每份非归档文档均在文件头维护机器可读的 `status`（`current` 现行有效 / `draft` 设计草案未入主线 / `archived` 历史归档只读）与 `verified` 校验基线，并由 `python -m scripts.check_docs` 在 CI 中自动检查状态标签与相对链接有效性。

---

## 1. AI 工程化工作流与规范（先读）

| 文档 | 状态 | 说明 |
|---|---|---|
| [AGENTS.md](../AGENTS.md) | `current` | AI 开发总入口：项目双入口、核心禁区与完成门禁命令 |
| [开发流程（SDD + TDD）](workflow/development-process.md) | `current` | S/M/L 变更分级、五阶段闭环与 Skills 对应关系 |
| [Git 与 Worktree 规范](workflow/git-workflow.md) | `current` | 受保护主干、分支命名、并行工作树资源隔离、PR 规模与合并策略 |
| [编码与测试规范](conventions/coding.md) | `current` | Python 3.13 / Ruff 约束、错误码映射、短事务边界与测试约定 |
| [工程模板目录](templates/) | — | `clarify` / `spec` / `design` / `tasks` / `review` / `change-note` / `adr` 模板 |

---

## 2. 产品、架构与历史决策

| 文档 | 状态 | 说明 |
|---|---|---|
| [产品需求文档（PRD）](prd.md) | `current` | 目标用户痛点、服务版/桌面版定位、核心场景、非目标与量化指标 |
| [服务版架构](service-architecture.md) | `current` | API / Worker / MySQL / 私有对象存储协作拓扑与升级兼容机制 |
| [模块边界与单向依赖](architecture/module-boundaries.md) | `current` | 包职责划分与 `tests/test_architecture.py` 强制执行的依赖方向 |
| [项目路线图](roadmap.md) | `current` | 已交付里程碑与 `003` 候选演进方向 |
| [ADR 0001：保留桌面版，独立运行服务版](decisions/0001-service-preview.md) | `current` | 双入口隔离动机与基本存储边界 |
| [ADR 0002：MySQL 短事务与租约充当作业队列](decisions/0002-mysql-as-queue.md) | `current` | 不提前引入消息中间件的取舍与崩溃恢复机制 |
| [ADR 0003：不可变对象键与先对象后元数据提交](decisions/0003-object-store-without-cross-store-tx.md) | `current` | 无跨库分布式事务下的数据一致性保证 |
| [ADR 0004：按页 BM25 与读取时从分块重建索引](decisions/0004-bm25-rebuilt-per-library.md) | `current` | 零向量库依赖的实测依据与多实例无状态设计 |
| [ADR 0005：知识库问答使用 SSE 单向流](decisions/0005-sse-for-qa.md) | `current` | 来源、流式答案与引用核验的事件流协议取舍 |
| [复盘：001/002 服务版重构过程与改进](retros/2026-11-service-refactor.md) | `current` | 早期提交粒度、规格留痕与语料脱敏问题复盘及对应新机制 |

---

## 3. 服务版规格与运行手册

| 文档 | 状态 | 说明 |
|---|---|---|
| [001 服务基础规格](specs/001-courseware-revision/spec.md) · [方案](specs/001-courseware-revision/design.md) · [任务](specs/001-courseware-revision/tasks.md) | `current` | 登录归属隔离、课件版本固定、幂等批次、租约队列与单节重试 |
| [002 学习工作台规格](specs/002-service-study/spec.md) · [方案](specs/002-service-study/design.md) · [任务](specs/002-service-study/tasks.md) | `current` | 固定版本知识库、SSE 引用问答、生成诊断、兼容迁移与静态工作台 |
| [服务版启动与回滚手册](runbooks/service-preview.md) | `current` | Docker Compose 启动、增量迁移与镜像快速回滚步骤 |
| [服务版验收记录](runbooks/service-verification.md) | `current` | 隔离 MySQL / SeaweedFS / 跨进程故障恢复 / 浏览器与回滚实测记录 |
| [评测语料与复现说明](../eval/README.md) | `current` | 私有语料脱敏边界、评测脚本用法与历史实测指标汇总 |

---

## 4. 共享核心与桌面基线专题（保全点 `baseline/desktop-0.11.0` / `3ac947a`）

| 文档 | 状态 | 说明 |
|---|---|---|
| [三层提取与图片过滤架构](ARCHITECTURE.md) | `current` | 零成本 XML/公式/备注优先提取与图片标准差去重原理（双形态共用） |
| [PDF 课件支持与文字层体检](PDF-SUPPORT.md) | `current` | 损坏字体映射表检测与乱码拦截机制 |
| [AI 服务商型号与价格核对表](PROVIDER-MODELS.md) | `current` | 修改 `app/config.py::AI_PROVIDERS` 前必查的定价与思考模式基准 |
| [桌面版项目初衷与边界](PROJECT.md) | `current` | 单机版能力清单与产品取舍背景 |
| [本地环境搭建（uv）](SETUP.md) · [桌面打包预案](PACKAGING.md) | `current` | Windows/uv 开发环境配置与 pywebview 打包说明 |
| [CV 视觉融合设计草案](CV-INTEGRATION.md) | `draft` | 整页渲染与版面分类探索方案（尚未合入主链路） |
| [版本更新日志](CHANGELOG.md) | `current` | 桌面版各迭代版本详细变更记录 |
| [桌面版历史设计与实验归档](archive/desktop/README.md) | `archived` | P0–P4 桌面阶段原始设计稿、标定过程与 A/B 实验长文（只读归档） |
