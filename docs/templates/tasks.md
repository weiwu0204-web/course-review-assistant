# NNN 实施任务与 TDD 追踪

拆分原则：每个任务只解决一个清晰子问题，可独立测试与提交，单个 PR 净改动控制在 400 行以内。

| 任务 ID | 关联 AC | 目标模块 / 文件 | 验收测试函数名 | 红灯证据（提交 SHA / 失败原因） | 绿灯提交 SHA | 状态 |
|---|---|---|---|---|---|---|
| T1 | AC-1 | `app/...` | `test_...` | `<sha>: AssertionError ...` | `<sha>` | `[ ]` |
| T2 | AC-2 | `app/...` | `test_...` | `<sha>: expected 404 got 200` | `<sha>` | `[ ]` |
| T3 | AC-3 | `app/...` | `test_...` | `<sha>: ...` | `<sha>` | `[ ]` |

## 验证收尾清单
- [ ] 全部 AC 已回填红绿提交证据
- [ ] `ruff check .`、`python -m scripts.check_docs`、`python -m pytest -q`、`node tests/test_coverage_ui.js` 全部通过
- [ ] 完成 `review.md` 与验收记录更新
