---
status: current
verified: 274dde4
---

# Git、Worktree 与 PR 操作规范

## 1. 主干保护与分支约定

- `main` 为受保护分支：禁止直接 `push`、禁止 `force push`、必须通过 Pull Request 且云端 `Reviewer checks`（`regression`）通过后方可合入。
- 分支一律从已验证的最新 `main` 创建，命名格式：
  - `feat/NNN-short-slug`：对应编号规格的新功能（如 `feat/003-batch-audit`）
  - `fix/short-slug`：缺陷修复
  - `test/short-slug`：补充测试或评测脚本
  - `chore/short-slug`：工程工具链、依赖或规则调整
  - `docs/short-slug`：纯文档与知识库更新

---

## 2. Worktree 并行隔离规范

禁止在同一个目录内频繁 `switch/stash` 或让多个 AI 任务共用同一工作目录。按职责固定工作树分工：

| 目录路径 | 职责 | 运行约束 |
|---|---|---|
| `F:\agent_pro\ppt-reviewer` | 常驻 `main` 基线目录 | 只做 `git pull --ff-only` 同步与对照验证，**禁止在此直接改代码** |
| `F:\agent_pro\ppt-reviewer-service` | 常驻服务版开发工作树 | 持有独立 `.venv`、`.service.env` 与 `.service-data/` |
| `F:\agent_pro\ppt-wt\<task>` | 并行任务临时工作树（修 Bug / 补测试 / 并行特性） | 独立端口（`8011`、`8012`…）、独立 `.service-data/`、独立 Compose 项目名 `-p ppt-wt-<task>` |

常用生命周期命令（参见 [ppt-worktree](../../.agents/skills/ppt-worktree/SKILL.md)）：

```powershell
# 1. 从最新 main 创建隔离工作树
git -C F:\agent_pro\ppt-reviewer fetch origin
git -C F:\agent_pro\ppt-reviewer worktree add ..\ppt-wt\<task> -b <branch> origin/main

# 2. 任务合并入 main 后清理工作树与已合并分支
git -C F:\agent_pro\ppt-reviewer worktree remove ..\ppt-wt\<task>
git -C F:\agent_pro\ppt-reviewer pull --ff-only origin main
git -C F:\agent_pro\ppt-reviewer branch -d <branch>
```

---

## 3. 提交粒度与提交信息（Conventional Commits）

格式：`<type>(<scope>): <imperative summary>`，常用 `type`：`docs`、`test`、`feat`、`fix`、`refactor`、`ci`、`chore`。

**必须分步留痕的提交边界**：
1. **Spec 独立提交**：L 级需求的 `spec.md / design.md / tasks.md` 必须先单独提交（`docs(003): add spec and design for ...`），不夹带任何 `app/` 代码。
2. **TDD 红绿留痕**：涉及权限、版本、状态机的验收项，先提交因预期业务原因失败的测试（`test(003): AC-1 red reject cross-owner retry`），再提交最小实现转绿（`feat(003): check batch owner before retry`）。
3. **结构移动与逻辑修改分离**：纯文件移动/重命名单独一笔提交，不与业务逻辑改动混在同一笔提交中。

---

## 4. PR 规模、审查与合并方式

- **规模上限**：单个 PR 的净增删（不含纯移动、归档文件）原则上 **≤ 400 行**。超过 400 行应提前按子任务拆分为多个递进 PR；确需超限须在 PR 模板「风险与拆解说明」中写明原因。
- **合并方式**：统一选择 **`Create a merge commit`**（不使用 `Squash and merge`），在主干历史中完整保留分支内「Spec → 红测试 → 实现 → 审查修正」的时间线证据。
- **里程碑标签**：桌面基线使用 `baseline/desktop-x.y.z`，服务版可运行里程碑使用附注标签 `git tag -a service/preview-x.y.z -m "..."` 并显式推送标签。

---

## 5. 回退与冲突处理

- **合并冲突**：任务拆分阶段即按模块/文件边界切分以规避冲突；若并行分支确有冲突，在开发分支执行 `git fetch origin && git merge origin/main`，人工核对冲突块并重跑全量检查后再推送，复盘切分边界。
- **主干回退**：`main` 合入后若发现缺陷，**严禁改写主干历史**；使用 GitHub PR 页面的 `Revert` 或本地在临时分支执行 `git revert -m 1 <merge_sha>` 发起回退 PR。数据库与容器镜像回滚遵循 [服务版启动与回滚](../runbooks/service-preview.md)。
