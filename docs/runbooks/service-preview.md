# 服务版私有预览

服务版与桌面版分别启动。此版本只提供受保护的 API 和批量生成主链路，可以在 Swagger `/docs` 演示。桌面 UI、知识库问答、历史统计仍在桌面版；后续逐个接到新服务。默认 worker 解析真实格式的测试课件、输出有明确标记的演示文档，零模型调用。

## Compose

1. 从仓库根目录复制 `.service.env.example` 为 `.service.env`，填四项独立凭据；数据库密码使用 URL 安全字符。为不同工作树使用独立 Compose 项目名、HTTP 端口和数据卷。
2. 执行 `docker compose --env-file .service.env -p ppt-review-preview -f deploy/compose.yaml up -d --build`。
3. 执行 `docker compose --env-file .service.env -p ppt-review-preview -f deploy/compose.yaml exec api python -m scripts.service_admin create-user alice`，在提示中输入密码。按需另建 bob 做权限验收。
4. 打开本机 `http://127.0.0.1:8010/docs`。用 `POST /api/v1/sessions` 得到 token，在 Swagger 的 Authorize 填入 token。创建课程 → 上传课件 → 提交材料 ID 列表（设置 `Idempotency-Key`）→ 查询批次 → 下载。
5. 只在获准用真实材料时，把 worker 的生成模式改成 model 并配置服务端凭据。课件和题目会发送给所配置的模型服务商。

对象存储和数据库均未发布主机端口。公网接入、真实账户运营、检索和文档保留策略还需单独验收。

若本机 Docker 镜像加速器无法拉取基础镜像，可在 `.service.env` 中设定 `PPT_SERVICE_PYTHON_BASE_IMAGE`，使用组织认可的同一 Python 镜像分发地址，无须修改全局 Docker 配置。

## 快速回滚

- 桌面版：回原目录运行，基线是 `baseline/desktop-0.11.0`（`7ee595b`）。需要另开干净目录时，从原仓库执行 `git worktree add --detach ../ppt-reviewer-desktop-restore baseline/desktop-0.11.0`，保留现有文件和私人资料。
- 服务版：先停候选 API 和 worker，再从最近验证过的服务提交重新构建，或切回该版本的镜像并使用原 Compose 项目名。保留数据库和对象数据卷；回滚不执行清空数据的命令。在首个服务版本之前，可停掉服务版，继续用桌面版。
- 在保留旧镜像的情况下，用 `PPT_SERVICE_IMAGE_TAG` 指向已验证标签，保持原项目名和同一份环境文件，然后执行：

  ```powershell
  docker compose --env-file .service.env -p ppt-review-preview -f deploy/compose.yaml up -d --no-build --no-deps api worker
  ```

  检查 `/health/ready` 后查询一个已有任务，再用测试账号提交一项任务，确认 worker 重新开始领取。[演练记录](service-verification.md)
- 若数据结构已变化，先验证旧版兼容。回退代码不能恢复已删除的数据。
