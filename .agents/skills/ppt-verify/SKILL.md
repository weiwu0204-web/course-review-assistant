---
name: ppt-verify
description: 执行 PPT 复习助手里程碑的集成验收、真实存储与跨进程演练、回滚验证并记录证据（Run milestone verification, rollback drills, and tag releases）。
---

# ppt-verify · 集成验收与回滚演练

## 何时使用
L 级规格合入前，或变更涉及数据库 schema、对象存储交互、worker 队列调度、Docker Compose 部署配置时使用。

## 必读上下文
1. [AGENTS.md](../../../AGENTS.md)
2. [服务版启动与回滚手册](../../../docs/runbooks/service-preview.md)
3. [服务版验收记录](../../../docs/runbooks/service-verification.md)

## 执行步骤
1. **本地确定性门禁**：运行 `ruff check .`、`python -m scripts.check_docs`、`python -m pytest -q`、`node tests/test_coverage_ui.js`。
2. **真实存储与跨进程演练**（按变更范围启用）：
   - 准备**专为测试创建的空 MySQL 库与私有对象桶**，严禁连接共享或存量用户环境。
   - 运行带 `PPT_TEST_MYSQL_URL` 的并发/租约测试，以及 `scripts/service_smoke.py`、`scripts/service_preview_check.py` 或 `scripts/service_browser_check.py`。
   - 默认保持 `PPT_SERVICE_GENERATOR=demo`，将付费模型探针与功能机制验收严格分开。
3. **升级与回滚演练**（涉及迁移或镜像变更时必做）：
   - 在保留同一测试数据卷的前提下，演练「升级候选版本 → 切回上一可用标签镜像 → 核对旧产物下载与跨账号 404 → 恢复新版本核对新增数据」，并记录本机恢复耗时。
4. **诚实记录证据**：更新 [service-verification.md](../../../docs/runbooks/service-verification.md)，明确写清**哪些在真实 MySQL/SeaweedFS/浏览器上跑过、哪些使用了替身、哪些边界尚未验证**。

## 产出
更新后的 [service-verification.md](../../../docs/runbooks/service-verification.md)（合入 `main` 后可按里程碑打附注标签 `service/preview-x.y.z`）。

## 退出条件
验收数据真实可复现，升级与回滚步骤已验证无损，CI `Reviewer checks` 在 PR 上全绿。
