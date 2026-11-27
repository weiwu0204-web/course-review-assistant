# 服务版私有预览

服务版与桌面版分别启动。服务版首页提供课件版本、批量手册、按页问答和个人历史，也可以通过 Swagger `/docs` 调用。默认演示使用实际的解析与检索，生成章节标注「部署演示」，问答返回召回原文，不调用模型。

## Compose

1. 从仓库根目录复制 `.service.env.example` 为 `.service.env`，填四项独立凭据；数据库密码使用 URL 安全字符。为不同工作树使用独立 Compose 项目名、HTTP 端口和数据卷。
2. 执行 `docker compose --env-file .service.env -p ppt-review-preview -f deploy/compose.yaml up -d --build`。
3. 执行 `docker compose --env-file .service.env -p ppt-review-preview -f deploy/compose.yaml exec api python -m scripts.service_admin create-user alice`，在提示中输入密码。按需另建 bob 做权限验收。
4. 打开本机 `http://127.0.0.1:8010/` 登录。创建课程 → 上传并勾选课件 → 生成并下载手册，或建库问答；发布新版后可在工作台选择先前建好的库。
5. 只在获准用真实材料时，把 `PPT_SERVICE_GENERATOR` 改为 `model` 并配置服务端凭据。生成和问答会把相应课件内容发给所配置的模型服务商。填入真实文本模型的每百万 token 输入、输出单价后，工作台才显示自动补漏选项。

对象存储和数据库均未发布主机端口。公网接入、真实账户运营、较大规模下的检索和文档保留策略还需单独验收。

若本机 Docker 镜像加速器无法拉取基础镜像，可在 `.service.env` 中设定 `PPT_SERVICE_PYTHON_BASE_IMAGE`，使用组织认可的同一 Python 镜像分发地址，无须修改全局 Docker 配置。

## 从 001 升级到 002

为候选版本单独设置 `PPT_SERVICE_IMAGE_TAG`。先在独立测试数据卷验证候选镜像，然后在目标项目停止新提交，等正在执行的任务结束。使用同一份环境文件和 Compose 项目名执行增量迁移，再启动候选 API 和 worker。迁移新增学习相关表，保留 001 的表和兼容标记。

```powershell
docker compose --env-file .service.env -p ppt-review-preview -f deploy/compose.yaml run --rm --no-deps migrate
docker compose --env-file .service.env -p ppt-review-preview -f deploy/compose.yaml up -d --no-build --no-deps api worker
```

## 快速回滚

- 桌面版：回原目录运行，基线是 `baseline/desktop-0.11.0`（`3ac947a`）。需要另开干净目录时，从原仓库执行 `git worktree add --detach ../ppt-reviewer-desktop-restore baseline/desktop-0.11.0`，保留现有文件和私人资料。
- 服务版保留镜像 `ppt-reviewer-service:preview-0.1.0` 与 `ppt-reviewer-service:preview-0.1.1`。若旧镜像缺失，可在独立工作树从对应 Git 标签重建。
- 先停止新提交，确认没有尚需 002 处理的自动补漏任务。候选服务故障且仍有此类任务时，保持 worker 停止，仅恢复旧 API（下面命令的末尾只留 `api`）。恢复 002 后再启动 worker。
- 使用原 Compose 项目和环境文件，保留数据库和对象存储数据卷。用 `PPT_SERVICE_IMAGE_TAG` 选择旧镜像并启动：

  ```powershell
  $env:PPT_SERVICE_IMAGE_TAG = 'preview-0.1.0'
  docker compose --env-file .service.env -p ppt-review-preview -f deploy/compose.yaml up -d --no-build --no-deps api worker
  ```

  检查 `/health/ready` 和已有手册下载，再用测试账号提交普通生成任务验证已启动的 worker。重新上线 002 后，核对已有知识库版本和问答记录。[演练记录](service-verification.md)
