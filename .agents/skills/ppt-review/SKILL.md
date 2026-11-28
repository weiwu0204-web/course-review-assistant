---
name: ppt-review
description: 对照规格与架构不变量审查 PPT 复习助手变更，输出审查记录并沉淀防再犯规则（Review diffs against ACs, ownership, tx boundaries, dependencies, and deleted code）。
---

# ppt-review · 异构代码审查与规则沉淀

> 审查清单机制参考 `obra/superpowers`（requesting-code-review，公开仓库地址可在 GitHub 核对）。不做“同源 AI 空泛互查”，只对照规格与工程不变量逐项核验。

## 何时使用
编码完成准备提 PR 前，或在审查已有分支变更时使用。

## 必读上下文
1. [AGENTS.md](../../../AGENTS.md) 与 [模块边界](../../../docs/architecture/module-boundaries.md)
2. 对应规格 `spec.md` / `design.md` / `tasks.md`
3. [代码审查记录模板](../../../docs/templates/review.md)

## 执行步骤
1. **控制审查规模**：运行 `git diff --stat main...HEAD`，确认净改动原则上 ≤ 400 行且未混入无关文件、本地凭据或运行数据。
2. **逐条核对验收标准**：检查每条 `AC` 是否都有对应测试，且测试覆盖了正常路径、跨账号越权（404）、未认证（401）、并发冲突（409）与异常恢复，而非只测快乐路径。
3. **核查架构与安全不变量**：
   - `owner` 是否全部由认证身份派生并在存储层条件中校验？
   - 数据库事务是否只含元数据读写，外部对象与模型调用是否均在事务外？
   - Worker 续租与结果提交是否校验当前领取标记，防止过期 worker 覆盖成功状态？
   - 是否违反模块单向依赖（`tests/test_architecture.py`）？
4. **核查新依赖与被删除代码**：
   - `requirements*.txt` 是否有新增包？若有，是否锁死版本并说明不可替代理由？
   - **逐行阅读 diff 中的红色删除块**：确认未误删异常清理、降级兜底或桌面版兼容逻辑。
5. **沉淀防再犯规则**：若某类缺陷在项目中出现 ≥ 2 次，必须将其转化为 `AGENTS.md` 禁区、架构断言测试或静态检查规则。

## 产出
- **L 级**：填写并提交 `docs/specs/NNN-name/review.md`。
- **M 级**：将核查结果写入 PR 描述的审查核对栏。

## 退出条件
审查清单无未解决阻塞项，`ruff check .`、`python -m scripts.check_docs`、`python -m pytest -q`、`node tests/test_coverage_ui.js` 全部通过。
