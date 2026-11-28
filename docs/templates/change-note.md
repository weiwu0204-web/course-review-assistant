# 变更说明（M 级）：<简短标题>

- **日期 / 分支**：`YYYY-MM-DD` / `<branch>`
- **改动原因**：<要修复的行为偏差或小范围接口调整>
- **修改范围**：<涉及的 1–3 个模块与公共行为变化>
- **不变承诺**：<明确桌面版入口、既有数据结构或权限边界不受影响>
- **TDD 验证**：
  - 失败复现测试：`<test_file>::<test_name>`（红灯原因：`<简述>`）
  - 全量回归：`ruff check .` / `pytest` / `node tests/test_coverage_ui.js` / `check_docs`
- **回滚方式**：`git revert <commit>`（无需数据迁移）
