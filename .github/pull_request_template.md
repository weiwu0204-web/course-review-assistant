## 关联规格与变更级别
- **变更级别**：`S` / `M` / `L`（对照 `docs/workflow/development-process.md`）
- **关联文档**：`docs/specs/NNN-.../spec.md` 或 `change-note`（S 级填「无，可逆小改」）

## 验收标准核对
- [ ] `AC-1`：<测试名或可观察验证结果>
- [ ] 桌面版基线兼容，未破坏既有 API 与本地配置

## 完成门禁与验证层级
- [ ] `ruff check .` 通过
- [ ] `python -m scripts.check_docs` 通过
- [ ] `python -m pytest -q` 通过
- [ ] `node tests/test_coverage_ui.js` 通过
- **真实依赖 vs 替身说明**：<写明哪些在 SQLite/LocalObjects 替身上验证，哪些在隔离 MySQL/SeaweedFS/浏览器上实跑>

## 风险、依赖与回滚
- **新增第三方依赖**：无 / <包名、锁定版本与引入理由>
- **被删除代码核查**：已人工核对删除行，无误删边界与清理逻辑
- **数据迁移与回滚方式**：<如「纯代码变更，可直接 Revert PR」或具体镜像/数据回滚步骤>
