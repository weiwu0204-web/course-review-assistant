---
status: current
verified: 274dde4
---

# 编码与测试规范

本规范提炼自仓库现有代码风格，所有新增或修改代码保持一致，不在功能提交中混入大范围无关格式化。

## 1. 语言版本与静态检查
- 目标版本：**Python 3.13**，行宽上限 **120** 字符，由根目录 `pyproject.toml` 中的 `ruff`（规则集 `E9, F`）强制检查。
- 模块依赖方向由 `tests/test_architecture.py` 自动断言，分层规则见 [模块边界](../architecture/module-boundaries.md)。
- 新增第三方依赖必须在 PR 中说明不可替代理由，并在 `requirements-service.txt` 或 `requirements-dev.txt` 中明确版本约束。

## 2. 命名与类型约束
- 包名、模块名、函数与变量使用 `snake_case`；类名使用 `PascalCase`；模块级常量使用 `UPPER_SNAKE_CASE`。
- HTTP 请求体统一使用 Pydantic `BaseModel` + `Annotated[..., Field(...)]` 显式声明长度、数值区间或正则边界（参见 `app/api/service_routes.py` 与 `app/api/types.py`）。

## 3. 错误处理与安全响应
- **服务版（`app/courses`、`app/study`、`app/api/service_routes.py`）**：
  - 业务层统一抛出 `ServiceError(status, detail)`，由入口异常处理器转换为 `{"detail": "..."}` 且附带 `Cache-Control: no-store`。
  - 未认证或会话失效返回 `401`（附 `WWW-Authenticate: Bearer`）；**资源不存在与跨账号无权访问统一调用 `missing()` 返回 `404`**，防止通过状态码探测他人资源 ID；版本或幂等参数冲突返回 `409`；上传超限返回 `413`。
  - 数据库（`SQLAlchemyError`）与对象存储（`ObjectStoreError`、`BotoCoreError`）异常统一在边界转为 `503`，日志只记请求方法、路由模板与异常类名，**严禁在响应或日志中打印密钥、Token、连接串或课件原文**。
- **桌面版（`app/api/routes.py` 等）**：保持既有 `app/services/errors.py` 用户向提示契约兼容。

## 4. 数据库与外部 IO 边界
- 服务版持久化统一使用 **SQLAlchemy Core**（显式 `select / insert / update` + `.mappings()`），表定义集中在 `migrations/v0001.py`、`migrations/v0002.py`，不使用隐式懒加载 ORM 实体。
- 只读查询使用 `with self.db.engine.connect() as conn:`；状态写入使用 `with self.db.engine.begin() as conn:` **短事务**。
- **硬性禁令**：严禁在 `engine.begin()` 事务块内执行课件解析（`extract_any`）、对象存储流传输（`objects.put/open`）或模型 API 调用。时间比较统一使用数据库时钟 `self.db.now(conn)` / `self.db.clock()`，不混用应用进程本地时间判断租约或会话过期。

## 5. 测试与注释约定
- **测试组织**：桌面与共享核心测试位于 `tests/test_*.py`，服务版多租户与状态机测试位于 `tests/service/test_*.py`，前端纯函数逻辑测试位于 `tests/test_coverage_ui.js`。
- **测试命名与替身**：测试函数名直接表达业务场景与不变量（如 `test_expired_worker_cannot_publish_and_cancel_stays_cancelled`）；默认在 `tmp_path` SQLite 与 `LocalObjects` 上使用合成字节流和假生成器运行，真实 MySQL 用例通过环境变量 `PPT_TEST_MYSQL_URL` 条件开启。
- **注释原则**：代码注释与文档默认使用**简体中文**，重点解释“为什么这样取舍 / 防什么坑”，不重复代码字面意思。
