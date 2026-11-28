---
name: ppt-tdd
description: 按红-绿-重构（Red-Green-Refactor）实现 PPT 复习助手的验收标准或修复可复现缺陷（Execute strict TDD with synthetic fixtures and model doubles）。
---

# ppt-tdd · 红-绿-重构测试驱动实现

> 测试先行纪律参考开源工程化工作流 `obra/superpowers`（test-driven-development，公开仓库地址可在 GitHub 核对）。

## 何时使用
实现 `tasks.md` 中的任务、`change-note` 中的 M 级改动，或修复任何涉及权限、版本、状态机、解析与计费逻辑的缺陷时使用。纯目录/文件移动改跑既有回归测试验证。

## 必读上下文
1. [AGENTS.md](../../../AGENTS.md)
2. 当前任务的 `spec.md` / `design.md` / `tasks.md`（或 `change-note`）
3. [编码规范](../../../docs/conventions/coding.md) 与 [模块边界](../../../docs/architecture/module-boundaries.md)

## 执行步骤
1. **Red（先让测试因预期原因失败）**：
   - 围绕单条 `AC` 或缺陷编写测试，使用合成文件与模型替身，不访问真实付费模型 API。
   - 运行定向 `python -m pytest <test_file> -k <test_name>`，确认失败原因是**目标业务行为尚未实现**（而非拼写、导入或夹具错误）。
   - 涉及权限、版本或状态变更时，先单独提交红灯测试：`test(NNN): AC-x red <简述>`，记下提交 SHA。
2. **Green（最小可行实现）**：
   - 编写刚好让该测试通过的最小代码，不提前塞入未被规格要求的参数或分支。
   - 严守核心约束：从认证会话派生 `owner`、发布前完整落盘对象、数据库 `begin()` 短事务内不做解析/上传/模型调用、worker 凭本次领取标记提交。
3. **Refactor（清理与全量回归）**：
   - 消除重复并保持命名与错误处理风格一致，不顺手格式化无关文件。
   - 依次运行：`ruff check .`、`python -m pytest -q`、`node tests/test_coverage_ui.js`。
   - 提交实现：`feat(NNN): ...` 或 `fix(...): ...`，并在 `tasks.md` 回填红灯与绿灯提交 SHA。

## 产出
通过全部门禁的测试与业务实现提交，以及更新了红绿证据的 `tasks.md`。

## 退出条件
所有目标 AC 均有测试覆盖且全量回归通过，桌面版契约零退化，无未跟踪的临时文件残留。
