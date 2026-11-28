---
name: ppt-spec
description: 为 PPT 复习助手涉及公共接口、课件/知识库版本、账号隔离、存储或异步任务的变更起草规格、技术方案与任务拆解（Draft spec.md, design.md, and tasks.md for SDD changes）。
---

# ppt-spec · 规格、方案与任务拆解

> 阶段划分参考 `github/spec-kit`（specify → plan → tasks，公开仓库地址可在 GitHub 核对）。

## 何时使用
- **L 级变更**：在 `ppt-clarify` 获得人工确认后使用，产出完整规格三件套。
- **M 级变更**：不建完整规格目录，改填 [变更说明模板](../../../docs/templates/change-note.md)。
- **S 级变更**：跳过本 Skill，直接在 PR 描述写明可逆改动说明。

## 必读上下文
1. [文档索引](../../../docs/README.md)、[产品需求文档](../../../docs/prd.md)、[服务版架构](../../../docs/service-architecture.md)
2. [模块边界与依赖方向](../../../docs/architecture/module-boundaries.md)
3. 既有规格参照：[001 服务基础规格](../../../docs/specs/001-courseware-revision/spec.md)、[002 学习工作台规格](../../../docs/specs/002-service-study/spec.md)

## 执行步骤
1. **编写 `spec.md`**（基于 [spec 模板](../../../docs/templates/spec.md)）：
   - 将验收条件写成带编号的可观察断言（`AC-1`, `AC-2`…），明确前置状态、动作与外部响应。
   - **横向核对相邻流程**：如下载、单节重试、取消、历史列表、第二 API 实例读取是否遵守完全相同的归属与版本固定规则。
2. **编写 `design.md`**（基于 [design 模板](../../../docs/templates/design.md)）：
   - 明确涉及模块边界、API 状态码、owner 派生校验、短事务与事务外 IO 切分、失败清理及**不删用户数据的回滚路径**。
   - 若引入新的架构级取舍，按 [ADR 模板](../../../docs/templates/adr.md) 在 `docs/decisions/` 新增记录。
3. **编写 `tasks.md`**（基于 [tasks 模板](../../../docs/templates/tasks.md)）：
   - 按模块边界拆成可独立验证的小任务，确保单个 PR 净改动 ≤ 400 行，并预留红/绿提交记录列。
4. 运行 `python -m scripts.check_docs` 确认文档头部状态标签与相对链接全部有效。

## 产出
`docs/specs/NNN-name/spec.md`、`design.md`、`tasks.md`（及可选的新 ADR）。

## 退出条件
规格三件套经人工确认后**单独提交**（`docs(NNN): add spec, design, and tasks`），提交中严禁包含任何 `app/` 业务代码改动。
