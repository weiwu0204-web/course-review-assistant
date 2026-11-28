# PPT 复习助手 · AI 开发约定

## 0. 先读上下文（动手前必读）
1. 先看 [文档索引](docs/README.md) 了解现行文档（忽略已归档的桌面历史设计，除非排查旧行为）。
2. 了解模块分层与单向依赖：[模块边界](docs/architecture/module-boundaries.md)。
3. 改认证、课件版本或批任务队列前先读 [001 服务基础规格](docs/specs/001-courseware-revision/spec.md) 及 `design.md`；改浏览器工作台、知识库问答或生成诊断前先读 [002 学习工作台规格](docs/specs/002-service-study/spec.md) 及 `design.md`。

## 1. 项目双入口与兼容契约
- **桌面版入口**：`app.main:app`（本地单用户文件与 `%APPDATA%` 配置），保持既有 API、配置和本地运行数据兼容。
- **服务版入口**：`app.service_main:create_app`（仅挂载 `/api/v1` 服务路由与健康检查，不挂载桌面路由，不共享本地凭据）及独立进程 `python -m app.worker`。

## 2. 开发流程与 Skills 分工
按 [开发流程](docs/workflow/development-process.md) 先做 **S / M / L 定级**：
- **L 大改**：依次执行 `ppt-clarify` → `ppt-spec`（单独提交） → `ppt-worktree` + `ppt-tdd` → `ppt-review` → `ppt-verify`。
- **M 中改**：填写 [change-note](docs/templates/change-note.md)，走 `ppt-tdd` 与 `ppt-review`。
- **S 小改**：可逆且不碰权限/状态/数据/公共 API，直接修改并在 PR 说明原因。

## 3. 核心不变量与禁区（硬约束）
- **归属只从认证身份派生**：课程、版本、任务、知识库、问答、下载和重试一律在可信存储中校验 `owner`；对象键和资源 ID 仅用于定位数据，跨账号统一返回 `404`。
- **版本固定与短事务边界**：提交批任务或建库时固定输入版本快照；发布新版本前先完整写入对象存储，数据库事务只更新元数据；worker 在短事务领取任务，事务外解析、生成和上传，携带本次领取标记提交结果。
- **数据与成本红线**：默认测试只用合成课件与模型替身，**严禁访问真实模型 API**；凭据、第三方课件、本地评测语料、生成产物和服务运行数据留在 Git 之外；真实存储测试必须使用专用隔离库和存储桶。
- **架构与历史保护**：禁止修改只读历史目录 `docs/archive/**`；禁止在共享层 `app/services/**` 反向依赖服务版包（由 `tests/test_architecture.py` 强制）；未经 PR 理由说明与版本锁定，禁止新增第三方依赖。

## 4. 编码规范与工作树隔离
- 命名、事务、错误处理与测试风格遵循 [编码规范](docs/conventions/coding.md)。
- 分支从已验证的 `main` 创建；并行工作树各用独立端口、配置和数据目录，详见 [Git 与 Worktree 规范](docs/workflow/git-workflow.md)。

## 5. 完成定义（每次提交 / PR 前必跑）
```powershell
ruff check .
python -m scripts.check_docs
python -m pytest -q
node tests/test_coverage_ui.js
```
单个 PR 净改动原则上 ≤ 400 行，统一使用 Merge commit 合入；涉及共享数据迁移、版本切换或外部部署前必须先明确回滚方式。
