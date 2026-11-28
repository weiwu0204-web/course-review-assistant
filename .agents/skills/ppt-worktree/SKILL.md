---
name: ppt-worktree
description: 使用 Git worktree 为 PPT 复习助手创建端口与运行数据完全隔离的并行开发环境及收尾清理（Create, isolate, verify, and clean up parallel git worktrees）。
---

# ppt-worktree · 并行工作树隔离与收尾

> 工作树隔离实践参考 `obra/superpowers`（using-git-worktrees，公开仓库地址可在 GitHub 核对）。

## 何时使用
开始新分支开发，或需要同时推进多个独立任务（如一个做新功能、一个修缺陷、一个补测试）而互不污染目录与本地数据时使用。

## 必读上下文
1. [Git、Worktree 与 PR 操作规范](../../../docs/workflow/git-workflow.md)
2. [AGENTS.md](../../../AGENTS.md)

## 执行步骤
1. **切分边界**：创建前先确认并行任务修改的模块/文件集合互不重叠；`F:\agent_pro\ppt-reviewer` 固定停在干净 `main`，不在其中直接改代码。
2. **从最新主干创建工作树**：
   ```powershell
   git -C F:\agent_pro\ppt-reviewer fetch origin
   git -C F:\agent_pro\ppt-reviewer worktree add ..\ppt-wt\<task> -b <branch> origin/main
   ```
3. **隔离运行环境与数据**：
   - 每个工作树使用各自独立的 `.service.env`、`.service-data/`、HTTP 端口（如 `8010`、`8011`、`8012`）及 Compose 项目名 `-p ppt-wt-<task>`。
   - 若启用真实存储测试，为该工作树分配独立的测试库名与对象桶/前缀。
4. **交付前检查**：在工作树内核对 `git status -sb` 与 `git diff --stat main...HEAD`，运行全量完成检查后 `git push -u origin <branch>` 并发起 PR。
5. **合并后清理**：PR 通过 Merge commit 合入 `main` 后，停止并清理该工作树的临时容器卷，移除工作树并同步主干：
   ```powershell
   git -C F:\agent_pro\ppt-reviewer worktree remove ..\ppt-wt\<task>
   git -C F:\agent_pro\ppt-reviewer pull --ff-only origin main
   git -C F:\agent_pro\ppt-reviewer branch -d <branch>
   ```

## 产出
资源隔离的独立开发目录，以及 PR 合并后的干净工作树列表（`git worktree list` 无僵尸残留）。

## 退出条件
工作树内无未跟踪凭据或课件残留；合并后主工作树已快进同步至最新 `main`。
