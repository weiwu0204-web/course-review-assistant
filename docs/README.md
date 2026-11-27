# 文档索引

- [产品范围](product.md) · [服务版架构](service-architecture.md)
- [001 服务基础规格](specs/001-courseware-revision/spec.md) · [002 学习工作台规格](specs/002-service-study/spec.md) · [002 实施任务](specs/002-service-study/tasks.md)
- [服务版启动与回滚](runbooks/service-preview.md)
- [验收与回滚记录](runbooks/service-verification.md) · [ADR 0001：桌面与服务版分开运行](decisions/0001-service-preview.md)
- 桌面版保全点：`baseline/desktop-0.11.0`（`3ac947a`）。下面保留现有桌面说明、实验和历史决策。
- [桌面历史设计与验收](archive/desktop/README.md)

| 文档 | 内容 |
|------|------|
| [PROJECT.md](./PROJECT.md) | 项目是什么、解决谁的什么问题、当前能力边界 |
| [ARCHITECTURE.md](./ARCHITECTURE.md) | 三层提取 + 三层过滤的设计思路，以及为什么这么设计 |
| [CHANGELOG.md](./CHANGELOG.md) | 版本记录 |
| [SETUP.md](./SETUP.md) | 环境搭建步骤与已经踩过的坑 |
| [PACKAGING.md](./PACKAGING.md) | 打包预案（pywebview → exe，含 API key 决策） |
| [P0-ARCHITECTURE.md](./archive/desktop/P0-ARCHITECTURE.md) | 整课批量生成（P0）架构设计与实施进度 |
| [BATCH-TEST-GUIDE.md](./archive/desktop/BATCH-TEST-GUIDE.md) | 批量生成实测指南（M1 验收：步骤、预期、判读对照、产物位置） |
| [M3-VALIDATION.md](./archive/desktop/M3-VALIDATION.md) | **M3 价值验证方案**（整门课实跑 + 与通用 agent 对照 + 记录表 + 判定标准） |
| [P2-ACCEPTANCE.md](./archive/desktop/P2-ACCEPTANCE.md) | **P2 验收报告**（8 项判据的逐项实测证据 + 限制说明） |
| [P2-REPAIR.md](./archive/desktop/P2-REPAIR.md) | **P2 verify→repair 闭环设计**（MCP 工具接口、Agent 循环、收敛 guardrail、验收标准） |
| [P1-COVERAGE.md](./archive/desktop/P1-COVERAGE.md) | P1 覆盖率可见化的设计（判据标定、诚实性约束、验收结果） |
| [PROVIDER-MODELS.md](./PROVIDER-MODELS.md) | **AI 服务商型号与价格核对表**（带核实日期与来源；改 `AI_PROVIDERS` 前先看） |
| [PDF-SUPPORT.md](./PDF-SUPPORT.md) | PDF 课件支持：文字层健康检测、实测数据与已知边界 |
| [CV-INTEGRATION.md](./CV-INTEGRATION.md) | 图像处理的 CV 介入点（P0 去重 / P1 OCR / P2 分级） |

根目录的 [README.md](../README.md) 是快速上手用的；这里放的是设计和决策记录。

## 阅读顺序建议

新接手这个项目的话：先看 `PROJECT.md` 搞清楚要解决的问题，
再看 `ARCHITECTURE.md` 理解「为什么要分三层提取」——这是整个项目最核心的设计判断，
然后按 `SETUP.md` 把环境跑起来，翻 `CHANGELOG.md` 看它一路是怎么长过来的；
要发版本给同学时，最后看 `PACKAGING.md` 的打包轮；
想理解「整课批量生成」怎么做起来的，看 `P0-ARCHITECTURE.md`；
遇到 PDF 上传被拦下、想知道为什么，看 `PDF-SUPPORT.md`。
