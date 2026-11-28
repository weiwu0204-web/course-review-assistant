---
name: ppt-clarify
description: 在编写规格或代码前澄清 PPT 复习助手的需求、歧义与边界并判定 S/M/L 变更级别（Clarify requirements, edge cases, non-goals, and S/M/L scope before spec or coding）。
---

# ppt-clarify · 需求澄清与变更定级

> 结构参考开源工程化工作流 `obra/superpowers`（brainstorming）与 `github/spec-kit` 的前置澄清阶段（公开仓库地址可在 GitHub 核对）。

## 何时使用
收到任何新功能、流程改造或边界不明确的修改请求时**第一个触发**。在完成复述与定级前，不直接进入规格编写或代码修改。

## 必读上下文
1. [AGENTS.md](../../../AGENTS.md)
2. [文档索引](../../../docs/README.md) 与 [产品需求文档](../../../docs/prd.md)
3. [开发流程与分级标准](../../../docs/workflow/development-process.md)

## 执行步骤
1. **复述真实意图**：用 2–3 句话复述要解决的用户/工程问题及涉及入口（桌面版 `app.main:app` / 服务版 API / Worker / 浏览器工作台），主动暴露潜在理解偏差。
2. **排查高风险歧义**：针对以下维度列出待确认问题并提供推荐选项：
   - 权限与跨账号隔离行为（未登录 401 vs 非归属 404）；
   - 课件或知识库更新时，已排队任务与历史记录看哪个版本；
   - 失败、并发冲突、重试或降级时的外部表现；
   - 是否影响桌面版既有契约。
3. **划定不做边界（Non-goals）**：明确列出本次不顺手扩圈的内容。
4. **判定 S / M / L 级别**：对照 [开发流程](../../../docs/workflow/development-process.md) 给出定级与后续步骤。

## 产出
- **L 级**：按 [澄清模板](../../../docs/templates/clarify.md) 产出 `docs/specs/NNN-name/clarify.md` 草稿并向用户提问。
- **S / M 级**：直接在对话或 [变更说明](../../../docs/templates/change-note.md) 中列出复述、边界与定级供确认。

## 退出条件
所有歧义项已由人工明确拍板确认；未经确认不得自行假设产品决策并开始写 Spec 或业务代码。
