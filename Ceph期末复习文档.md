# Ceph 分布式存储实战 · 期末复习文档

> 依据工作区 14 份课件整理，覆盖每份课件页面上的知识点；只收录课件中出现的内容，未补充任何课件外数据。

---

## 〇、建议复习顺序

| 顺序 | 章节 | 定位 | 优先级 |
|---|---|---|---|
| 1 | 第1节 分布式存储概述与 Ceph 架构 | 全课程框架，必考基础 | ★★★★★ |
| 2 | 第2节 核心组件深度解析 | 五大组件 + 读写流程，与第1节连看 | ★★★★★ |
| 3 | 第3节 集群规划与硬件要求 | 容量计算、双网络、BlueStore 布局 | ★★★★☆ |
| 4 | 第4节 cephadm 部署 | 部署流程、端口、踩坑点 | ★★★★☆ |
| 5 | 第5/6/7节 RBD → RGW → CephFS | 三大接口按此顺序学，最后用"三大接口对比表"收束 | ★★★★★ |
| 6 | 第8节 监控与告警 | 阈值数字、诊断三剑客 | ★★★★☆ |
| 7 | 第9节 性能调优基础 | 参数名 + 默认值 | ★★★☆☆ |
| 8 | 第10节 OSD 故障排查 | 状态矩阵、PG 状态、空间满 SOP | ★★★★☆ |
| 9 | 第11节 扩容与缩容 | 安全移除流程、MON 先增后删 | ★★★☆☆ |
| 10 | 第12节 备份与恢复 | 3-2-1、副本 vs 备份、快照命令链 | ★★★★☆ |
| 11 | 第13节 K8s 集成 (CSI) | RWO/RWX 选型、动态供给链路 | ★★★★☆ |
| 12 | 第14节 安全配置 | cephx 四步、Caps 语法、msgr2 | ★★★☆☆ |

时间紧张时的最小复习集：第1–4节 + 三大接口对比表 + 监控阈值 + PG 状态表 + 3-2-1 备份原则 + RWO/RWX 选型。

---

## 一、分布式存储概述与 Ceph 架构（第1节）

### 1.1 存储发展史
- **DAS（直连存储）**：SCSI/SAS 直连服务器主板。优点：部署简单、技术成熟、初期成本低。局限：容量受限（单机插槽数）、数据孤岛（无法跨服务器共享）、单点故障。
- **集中式时代**：
  - **NAS**：通过 Ethernet 共享文件，NFS/SMB 协议，适合办公文档共享、轻量媒体库。
  - **SAN**：块级服务，通常光纤通道 (FC)，适合数据库、虚拟化。
  - 共同挑战：扩容成本极其昂贵，存储控制器（Head）成为性能吞吐最大瓶颈，无法满足 PB 级需求。
- **结论：单点控制架构无法通过增加硬盘线性提升吞吐（"性能天花板"）**。

### 1.2 云计算时代的驱动
5G、物联网、生成式 AI (LLM) 使需求从 TB 跃升至 PB/EB 级：高并发（成千上万客户端同时读写）、横向扩展（加机器不停机）、成本优化（通用 X86 服务器）。

### 1.3 分布式存储定义与对比
- 核心逻辑：数据分散存储在多台独立服务器上，软件层将其整合为逻辑统一的存储池。核心思想："**分而治之、统一调度、自动纠错、横向无限扩展**"。

| 维度 | 传统集中式 (NAS/SAN) | 分布式存储 |
|---|---|---|
| 扩展能力 | 纵向 Scale-up，受单机上限、成本极高 | 横向 Scale-out，按需加普通服务器 |
| 可靠性 | 单点故障风险高，依赖昂贵高可靠硬件 | 软件层备份/纠删码，硬件坏数据不丢 |
| 性能 | 容易遇到控制器瓶颈 | 多节点并发，吞吐随节点线性提升 |
| 成本 | 专用高端硬件 | 通用 X86，性价比高 |

### 1.4 四大核心特征
1. **高扩展性**：Scale-out，加节点线性提升容量与吞吐。
2. **高可用性**：多副本/纠删码，部分节点故障不影响业务。
3. **自动化管理**：自动故障检测、自动数据均衡、自动恢复。
4. **去中心化**：避免集中控制器单点瓶颈，所有节点共同承担负载。

### 1.5 主流系统对比

| 系统 | 研发机构 | 核心定位 | 适用场景 |
|---|---|---|---|
| HDFS | Apache | 大数据计算文件系统 | 离线计算、Hadoop 生态 |
| GlusterFS | RedHat | 通用文件存储 | NAS 替代、非结构化数据 |
| MinIO | 开源社区 | 高性能对象存储 | S3 协议对接、云原生 |
| Ceph | 开源社区 | 统一存储平台 | 虚拟化、K8s、全能存储 |

### 1.6 Ceph 定位与历史
- 定义：开源、高度自动化的分布式存储系统，提供优异的性能、可靠性和可扩展性。
- **"统一存储"：同一个底层集群 (RADOS) 上同时提供块 (RBD)、文件 (CephFS)、对象 (RGW)**。
- 设计哲学：无单点故障 (No Single Point of Failure)、无限水平扩展 (Self-Managing)。
- 时间线（课件原文）：**2004** 诞生（Sage Weil 在 UCSC 的博士课题）→ **2012** Inktank 公司成立 → **2014** RedHat 收购 Inktank → **2024+** 私有云、K8s、AI 存储的事实标准。
- 流行原因：降本增效（普通 X86）、极高可靠性（多副本跨节点/跨机架 + 数据自动洗刷 Scrub 校验）、深度集成（OpenStack、Kubernetes CSI 默认首选后端）。

### 1.7 逻辑分层架构
- **关键概念：在 Ceph 中，一切皆对象 (Objects)**。
- 三层：**接口层**（RBD/CephFS/RGW）→ **公共库层 LIBRADOS**（应用经 C/C++/Java/Python 直接访问底层）→ **基石层 RADOS**（可靠自主分布式对象存储，含 MON、OSD、MGR）。

### 1.8 五类组件速览（第1节版）
- **MON**：维护集群状态全局视图 Cluster Map（OSD Map / MON Map / PG Map / CRUSH Map）；法定人数 Quorum 部署奇数个 (3, 5, …)，**Paxos 协议**保证一致；负责成员资格检测、认证管理。
- **OSD**：Object Storage Daemon，一个 OSD 通常对应一块物理硬盘；处理读写、副本复制/恢复/回迁、互相心跳并汇报 MON。
- **MGR**：Luminous 版本引入，分担 MON 查询压力；收集运行数据，可扩展模块 Dashboard / Prometheus / Restful API。
- **MDS**：Metadata Server，**仅为 CephFS 提供元数据服务**（目录树、权限、时间戳）；**RBD 和 RGW 不需要 MDS**。
- **RGW**：RADOS Gateway，把 RESTful HTTP 请求转换为 RADOS 对象调用；兼容 **Amazon S3 API 与 OpenStack Swift API**；场景：APP 图片上传、网盘存储。

### 1.9 读写流程与 CRUSH（重点）
客户端**不通过中心节点路由数据**（消除中心网关单点瓶颈）：
1. 客户端从 MON 获取最新 Cluster Map；
2. **本地运行 CRUSH 算法**算出数据所属 OSD；
3. 直接向**主 OSD** 发起读写；
4. 主 OSD 负责在副本间同步数据。

**CRUSH (Controlled Replication Under Scalable Hashing)** 三大特性：
- **无需中心查表**：数据位置靠数学计算而非查数据库，极大减少元数据开销；
- **故障域感知**：可强制副本放在不同机柜/数据中心；
- **动态自愈**：扩容或故障时自动触发最少量的迁移来重新平衡。

### 1.10 高可用与冗余（课件数字）
- 3+ 经典副本数；99.9% 数据持久性（即使整柜断电数据在线）；Auto 故障自平衡（坏盘自动后台异步修复）。
- 典型场景：私有云基石（OpenStack/CloudStack）、K8s 动态供给块存储、大数据中台（Presto/Spark）、企业备份中心。

---

## 二、Ceph 核心组件深度解析（第2节）

### 2.1 五大组件总表

| 组件 | 全称 | 核心作用 | 重要性 |
|---|---|---|---|
| MON | Monitor | 维护集群状态图 (Cluster Map)，决策与管理 | ★ 核心必需 |
| OSD | Object Storage Daemon | 存储数据对象，执行复制、恢复与平衡 | ★ 核心必需 |
| MGR | Manager | 性能统计、监控及 Dashboard 模块化管理 | ★ 核心必需 |
| MDS | Metadata Server | 仅服务 CephFS，管理元数据与目录树 | 接口必需 |
| RGW | RADOS Gateway | 对象网关，标准 RESTful HTTP 接口 | 接口可选 |

### 2.2 MON 详解
- 类比 K8s 控制面；四大职责：**状态同步**（Cluster Map 权威副本）、**成员管理**（OSD 在线/下线）、**认证中心**（CephX 认证、客户端鉴权）、**配置分发**（Pool 属性、认证信息、拓扑）。
- **五大核心 Map**：MonMap（节点 ID/IP/选举 Epoch）、OSDMap（OSD Up/Down 状态、权重、Pool 配置）、PGMap（PG 分布与状态 Clean/Degraded）、CRUSH Map（层级结构、故障域、副本规则）、MDSMap/AuthMap（MDS 状态、密钥环与权限）。
- **Epoch**：Map 每次变更递增，保证各节点 Map 同步一致。
- **为什么多 MON**：1 个单点故障；偶数个网络分区时易"脑裂"。Paxos 选举 Leader，所有状态变更由 Leader 统一分发；**Quorum 要求存活节点 > N/2**。3 个允许坏 1 个，5 个允许坏 2 个。

### 2.3 OSD 详解
- 三大职责：持久化存储（默认 BlueStore 直接管理磁盘）、副本复制（**主 OSD 负责同步到所有副本，保证强一致性**）、状态汇报（心跳，邻居失联主动上报 MON 触发重构）。
- **1:1 映射原则**：一个磁盘对应一个 OSD 进程；**不建议 RAID 5/6**，冗余由 Ceph 软件层实现。
- 介质分工：HDD→大容量冷数据池；SSD→高性能卷或 BlueStore DB/WAL 加速；NVMe→极低延迟核心存储。

### 2.4 OSD 状态矩阵（高频考点）

| 组合 | 含义 |
|---|---|
| UP + IN | 完美状态，正在提供服务 |
| UP + OUT | 已启动但被管理员手动调出，常用于扩缩容 |
| DOWN + IN | 节点故障但未标出集群，触发数据读写阻塞（临时性），**最需关注** |
| DOWN + OUT | 确定性损坏，需更换磁盘重新部署（第10节补充） |

### 2.5 OSD 故障自愈逻辑（含默认参数）
1. **MON 标记 DOWN**：`mon_osd_min_down_reports = 3`（其他 OSD 汇报无法连接达 3 次）。
2. **等待周期**：`mon_osd_down_out_interval = 600s`（默认 10 分钟未恢复则标记 OUT，触发重平衡）。
3. **全集群重平衡**：CRUSH 重新寻址，规避坏 OSD。
4. **副本补齐**：`osd_max_backfills = 1`，后台 Backfill 迁移，直至回归 **active+clean**。
- 核心思想："Ceph 始终尝试维持 CRUSH 定义的目标副本数，即使面临硬盘损坏。"

### 2.6 MGR 详解
- **自 Nautilus 版本后成为必需组件**；性能数据集中库。
- 功能：周期收集所有 OSD 的 IOPS/带宽；内置 Dashboard Web UI；插件化（Prometheus 导出器、RESTful API、Zabbix）；**Balancer 模块**自动微调 PG 分布。

### 2.7 MDS 详解
- 为什么需要：分布式对象存储本身不支持"目录嵌套"和"POSIX 权限"，MDS 专门处理。
- 核心：分离**元数据**（文件名、权限、修改时间、目录结构）与**数据**（文件实际内容）。

### 2.8 RGW 详解
- 运行在 Web 服务器之上的 **FastCGI 服务**。
- 功能：协议转换（HTTP S3/Swift → RADOS 请求）、多租户（用户账户、Quota 限额、密钥管理）、兼容标准 S3 客户端（AWS CLI、Boto3）、多活部署（跨地域 Multi-site 复制）。

### 2.9 客户端读写完整数据流（重点）
"**写入确认 (ACK) 只有在所有副本完成后才会返回客户端。**"
1. 初始化：客户端向 MON 获取 Cluster Map，存入本地缓存；
2. 计算：CRUSH(Object ID) → 算出目标 [OSD.1, OSD.5, OSD.8]；
3. 写主本：直接访问 OSD.1，OSD.1 同时通知 5 和 8 写入。

### 2.10 部署数量建议

| 场景 | MON | MGR | OSD | MDS/RGW |
|---|---|---|---|---|
| 实验/开发 | 1 | 1 | 3+ | 不建议部署 |
| 小型生产 | 3 | 2 | 10–30 | 2 (Active/Standby) |
| 中大型生产 | 3 或 5 | 2 | 50–500+ | 2–4 (多活) |

原则：**生产环境 MON 必须单数，所有组件 N+1 冗余**。
企业级 3 节点拓扑示例：Node1 (MON1/MGR1/OSD0-3/RGW1)、Node2 (MON2/MGR2/OSD4-7/MDS1)、Node3 (MON3/OSD8-11/MDS2-Standby)，可承受任意单台服务器宕机不丢数据不中断业务。

---

## 三、集群规划与硬件要求（第3节）

### 3.1 规划失误的后果
性能瓶颈（恢复时业务卡死）、成本激增（内存配比低致 OSD 频繁崩坏）、扩容困难（机柜/端口预留不足）、可用性危机（副本同机架，机柜掉电丢数据）。

### 3.2 六大支柱
1. 节点角色规划 → 2. 硬件资源选型 → 3. 网络架构设计（内外网隔离、带宽冗余、MTU）→ 4. 存储引擎布局（BlueStore DB/WAL 分区）→ 5. 容量与可靠性（副本数、可用空间、预留）→ 6. 故障域与安全（主机/机架/数据中心隔离）。

### 3.3 最小部署 vs 生产标准

| 维度 | 实验/开发 | 生产推荐 |
|---|---|---|
| 节点数 | 3（勉强维持 Quorum） | ≥5 |
| MON | 3（单节点故障时集群只读） | 3 或 5（独立部署） |
| MGR | 1（无热备） | 2（Active-Passive 主备） |
| 网络 | 公网/集群网共用单网卡 | 双网卡绑定，Public 与 Cluster 物理隔离 |
| 单节点 OSD | 1 个（无冗余保护） | 多 OSD（建议 4–12 个，混搭 SSD/HDD） |

**组件解耦**：大型集群将 RGW（计算密集）、MON/MGR（管理密集）与 OSD（存储密集）物理隔离。案例（100 节点集群）：3× 管理节点 (MON/MGR)、90× 存储节点 (OSD)、4× RGW、3× MDS。

### 3.4 节点角色配置参考

| 角色 | CPU | 内存 | 职责 |
|---|---|---|---|
| Admin | 低 (4-8 Core) | 8GB | ansible/cephadm 部署中心 |
| MON | 中 (8 Core+) | 16GB+ | 状态维护、Paxos 仲裁 |
| OSD | 高（依磁盘数） | 高（依磁盘数） | 读写、复制、心跳、校验 |
| MDS | 中 (8-16 Core) | 16-32GB+ | 文件元数据缓存（内存密集） |
| RGW | 高 (16-32 Core) | 16GB+ | HTTP 协议解析转换（计算密集） |

**CPU/OSD 配比**：HDD 1–2 Core/OSD；SSD 2–4 Core/OSD；NVMe 4–8+ Core/OSD（CPU 往往成为性能释放瓶颈）。建议禁用超线程或按物理核心计算。

### 3.5 磁盘选型
- **严禁**：家用硬盘、SMR 叠瓦盘、低速 USB、无掉电保护的廉价 SSD。
- **推荐**：企业级 SATA/SAS HDD (7200 RPM)；具备 PLP（掉电保护）的企业级 SSD；数据中心级 U.2 NVMe。

| 特性 | SATA HDD | Enterprise SSD | NVMe SSD |
|---|---|---|---|
| 典型 IOPS | ~100 | ~50,000+ | ~500,000+ |
| 典型延迟 | 10–20 ms | <1 ms | <100 μs |
| 成本/TB | $ | $$$ | $$$$$ |
| Ceph 角色 | 冷数据存储 | 热数据/混合云 | DB/WAL 缓存/全闪池 |

### 3.6 BlueStore 引擎（重点）
- 现代版本默认引擎，**直接绕过文件系统操作裸盘**。OSD 分三部分：**Data**（对象数据）、**DB**（RocksDB 元数据）、**WAL**（预写日志）。
- DB/WAL 放高速介质可大幅提升 HDD OSD 随机写性能。
- 布局方案：
  - **混合模式**：Data: HDD，DB/WAL: NVMe 分区，性价比最高；**DB 分区建议为 Data 的 4% 或至少 30GB 以上**。
  - **全闪模式**：Data/DB/WAL 都在 SSD，通常不需单独分 WAL/DB。
  - **纯机械模式**：仅用于极低成本备份，IOPS 极低，RocksDB 合并时剧烈延迟。
- ⚠️ **DB 分区存满会溢出到 HDD Data 盘，导致性能暴跌**。

### 3.7 双网络架构（重点）
- **Public Network**：客户端请求流量，流向 Client ↔ OSD / MON / RGW。
- **Cluster Network**：后端同步、副本复制、恢复流量，流向 OSD ↔ OSD。
- 分离理由：性能保障（防止恢复流量冲击前端）、安全（集群网不暴露给客户端）、故障排除（易识别拥塞来源）。
- Ceph 依赖网络的三类流量：数据重平衡 (Rebalancing)、心跳检测 (Heartbeats)、副本同步 (Replication)。

| 规模 | Public | Cluster | 建议 |
|---|---|---|---|
| 实验/测试 | 1 GbE | 1 GbE | 禁止生产使用 |
| 小型生产 | 10 GbE | 10 GbE | 双链路聚合 Bonding |
| 高性能 NVMe 池 | 25 GbE | 25/100 GbE | 必须 25G+ |

### 3.8 容量计算（必考公式）
- **原始容量 = 所有磁盘容量之和**。
- 3 副本：100TB 原始 → 33.3TB 可用。
- **0.8 系数**：利用率超过 80% 性能剧烈下降，85% 触发 Full 报警停止写入（规划时按 80% 打折）。
- 纠删码收益：4+2 EC 利用率 4/6 ≈ 66%，远高于 3 副本。
- 副本数选择：**Size=2**（可用率 50%，仅允许 1 块盘坏，修复期间再坏即丢数据，不推荐）；**Size=3**（可用率 33%，允许同时坏 2 块盘，性能/安全/成本最优平衡，推荐）；**Size=4**（可用率 25%，金融核心零容忍场景）。
- 故障域：Host 级（默认）、Rack 级（推荐）、Room 级。
- **扩容金律："不要等到集群 90% 满才扩容，理想时机是利用率达 65–70% 时。"** 扩容无感、线性增长，需控制 Backfill 迁移速率。

### 3.9 案例实战：100TB 可用容量（3 副本）
1. 原始容量：100TB / 0.8 × 3 = **375 TB**；
2. 硬盘：12TB 企业级 HDD；
3. 数量：375 / 12 ≈ **32 块**；
4. 分布：4 台服务器 × 8 块 12TB；
5. 缓存：每节点额外 2 块 960GB NVMe 作 DB/WAL。

### 3.10 官方最佳实践 & 部署前检查
- 最佳实践：同一 Pool OSD 规格统一（防木桶效应）；独立存储网卡（双万兆与业务分离）；DB 按 Data 4% 配 SSD；**禁止 RAID 5/6（OSD 裸盘）**；双电源接不同 PDU；避免单节点盘过多（如 36 盘，节点故障导致大规模重构）。
- 检查清单：时间同步（Chrony/NTP）精准；主机名/IP 规划 FQDN；防火墙端口（MON 6789/3300，OSD 6800+）；内核 4.x/5.x+；SSH 互信；数据网卡 Bond；Python 3.x；硬盘清空分区表。
- **避坑 1（时间同步）**：节点时间偏差超过 `mon_clock_drift_allowed`（默认 0.05s / 50ms）→ CLOCK_SKEW 警告，甚至 OSD 异常下线、集群拒绝写入。
- **避坑 2（裸盘残留）**：磁盘有旧分区表（LVM 残留/文件系统签名）时 ceph-volume 会保护性报错中断，部署前用 `dd` 或 `wipefs` 深度清理。

---

## 四、cephadm 部署全攻略（第4节）

### 4.1 部署方式演进
- 手工部署：繁琐易错，仅学术研究；**ceph-deploy**：经典 Python 工具，**已停止维护**；Ceph-Ansible：适合大规模定制化但学习曲线陡；**cephadm（当前推荐）**：全容器化 (Podman/Docker)、自托管自管理、Dashboard/CLI 声明式编排。
- cephadm 三大优势：生命周期管理（部署+升级+监控+启停）、环境隔离（容器运行避免依赖冲突）、智能编排（类似 K8s Operator 的自愈伸缩）。
- 架构：cephadm 是 **MGR 模块**，通过 SSH 连接各主机调用容器运行时；经 **Orchestrator API** 统一调度；用 **systemd** 管理容器生命周期。

### 4.2 实验环境
3 节点 ceph-node1/2/3（192.168.10.101–103，Rocky Linux 9 / Ubuntu 22.04）；node1: MON+MGR+OSD，node2/3: MON+OSD；每台至少 2 块物理硬盘（sdb 供 OSD）。

### 4.3 环境准备（第一阶段）
- Checklist：主机名唯一且 FQDN 可解析；Chrony 正常；SSH 及 Ceph 端口全开；临时关闭 Firewalld 与 SELinux；安装 Podman 或 Docker；至少 1 块空闲裸盘。
- **踩坑 1**：主机名严禁大写字母、下划线、特殊字符（仅小写字母、数字、中划线），否则容器化部署报错。
- **踩坑 2**：/etc/hosts 中必须删除"本机主机名 → 127.0.0.1"的配置，否则守护进程把监听地址绑定到回环网卡，其他节点无法通信。
- **时间同步**：Paxos 对时间偏移极度敏感，**误差超 50ms OSD 可能被标记 Down**。
- 安全硬化对照：实验环境直接关防火墙/SELinux disabled/root 免密；生产环境精准开放 3300, 6789, 6800-7300 / SELinux permissive 或配 Policy / 专用部署用户限权 sudo / 主备内部时间服务器。
- **Podman vs Docker**（Ceph Reef 推荐Podman）：Daemonless 无守护进程（避免 dockerd 挂掉全瘫痪）vs Daemon-based；支持 Rootless vs 默认必须 Root；与 systemd 深度集成 vs 需第三方工具。命令：`dnf install podman -y` → `podman --version` → `systemctl enable --now podman`。

### 4.4 Bootstrap 引导（第二阶段）
- cephadm 本身是独立 Python 脚本，用于引导集群并安装仓库。
- `cephadm bootstrap` 一条命令完成：创建本地 MON/MGR、配置集群网络、生成管理密钥、部署 Dashboard、自动配置 SSH 密钥环。
- 自动化生命周期：01 拉取镜像 → 02 启动 MON → 03 启动 MGR → 04 部署 Dashboard。
- 验证：进入 cephadm shell 查看集群状态，**成功标志 `health: HEALTH_OK`**；初始无 OSD 时为 WARN 属正常。
- Dashboard：`https://192.168.10.101:8443`，默认用户 admin（Bootstrap 输出打印初始密码）。

### 4.5 扩展与验证
- 添加节点：分发公钥到 node2/3 的 `~/.ssh/authorized_keys` → 注册主机 → `ceph orch host ls` 验证。
- 部署 OSD：cephadm 自动发现裸盘并初始化；`ceph osd tree` 验证拓扑（root default → host → osd.N, up, REWEIGHT 1.00）。
- 常见问题：SSH 认证失败（检查密钥登录与 /etc/hosts 解析）；磁盘未发现（cephadm 仅识别裸盘，有分区/LVM 需 `wipefs -a`）；健康报警（`ceph health detail`，查时间偏差或容器日志）。

---

## 五、块存储 RBD 配置与使用（第5节）

### 5.1 三大存储分类（基础对比表，必背）

| 维度 | 块存储 | 文件存储 | 对象存储 |
|---|---|---|---|
| 访问接口 | 原始磁盘扇区 / SCSI / NVMe | POSIX API / 共享目录 | RESTful API (HTTP/HTTPS) |
| 层级结构 | 无（裸磁盘） | 树状目录层级 | 扁平化 (Bucket + Object) |
| 典型协议/代表 | SAN、硬盘、EBS | NFS、Samba、FTP | AWS S3、OpenStack Swift |
| 核心优势 | 极低延迟、高性能 | 易共享、符合传统习惯 | 海量扩展、成本低、自带元数据 |
| 适用场景 | 数据库、虚拟机镜像 | 协同办公、日志共享 | 图片/视频网站、数据湖、备份归档 |

### 5.2 RBD 定义与原理
- RBD (RADOS Block Device)：基于 Ceph 集群的软件定义块存储；分布式（数据在 RADOS 节点，无单点，自动副本保护与故障自愈）；企业特性：**精简配置 (Thin Provisioning)、快照、差异化克隆、动态在线扩容**。
- **数据条带化 (Striping)**：镜像不整体存储，而是：对象化（默认切分 **4MB** 对象）→ 条带化（跨对象并发读写）→ 映射（**镜像 → 对象集合 → PG → OSD**）。10TB 虚拟磁盘可利用成百上千块硬盘吞吐。
- 逻辑组件：**librbd**（用户态库，供 QEMU/KVM）、**krbd**（内核模块，映射为 /dev/rbdX）、MON（提供 OSD Map）、OSD（实际存储）。

### 5.3 管理流程（四步闭环：Pool → Image → Map → 挂载）
1. 创建并初始化存储池；**`rbd pool init` 是必须操作**（创建必要元数据对象）。
2. `rbd create` 创建镜像；`rbd info` 查看。
3. `rbd map` 映射为本地块设备。
4. 格式化（mkfs）并 mount 挂载使用。

### 5.4 镜像元数据

| 字段 | 说明 |
|---|---|
| size | 逻辑容量，精简配置，初始不占物理空间 |
| objects | 切分后的对象总数（10G/4M ≈ 2560 个） |
| order | 对象大小幂指数，默认 22（2^22 = 4MB） |
| features | 高级功能：layering（快照克隆）、exclusive-lock（排他锁）等 |

### 5.5 常用命令
- 在线扩容（可在挂载状态）：`rbd resize rbdpool/vm-disk01 --size 20G`
- 重命名：`rbd rename rbdpool/vm-disk01 rbdpool/disk-new`
- 删除：必须先卸载快照与客户端挂载：`rbd rm rbdpool/disk-new`

### 5.6 快照与克隆（重点）
- **快照**：镜像某时刻的**只读**状态；秒级创建（基于元数据记录，无物理拷贝）；用于系统升级、误删恢复。
- **回滚 Rollback**：`rbd snap rollback`——⚠️ **会永久丢失自快照创建以来写入的所有新数据**。
- **克隆 Clone**：从快照创建新的**可写**镜像，用于虚拟机镜像分发；**COW (Copy On Write)**：克隆初始不占空间，修改父镜像数据时才拷贝。
- 高级特性标志：

| 特性 | 作用 |
|---|---|
| layering | 支持分层快照与克隆 |
| exclusive-lock | 排他锁，防止多主机并发写入冲突 |
| object-map | 加速快照导出与 diff 计算 |
| fast-diff | 快速比较镜像间差异 |
| deep-flatten | 彻底解除克隆体与父镜像依赖 |

### 5.7 排错
- 映射报 `Operation not supported`：旧内核不支持某些特性（如 object-map）→ 禁用不支持的特性映射。
- 权限不足：检查 `/etc/ceph/ceph.client.admin.keyring` 读权限。
- 映射挂死：检查 MTU 设置或 OSD 集群 Blocked 状态。
- 镜像删除失败：有关联快照或 Watchers 占用，`rbd status lab_pool/web-data` 查看。
- 空间已满：Pool 配额限制，精简配置下实际物理占用超限也会不可写。

### 5.8 应用场景
- **OpenStack**（事实标准后端）：Glance 镜像存 RBD、Cinder 虚拟卷、Nova 瞬间克隆虚拟机磁盘。
- **Kubernetes**：Ceph-CSI 动态供给——Pod 申请 PVC → CSI 自动创建 RBD；Pod 漂移自动解挂重映射；支持扩容与备份。
- 实验逻辑流：创建专用池 lab_pool → 生成密钥分发客户端 → 远程映射挂载 → 测试快照与恢复。

---

## 六、对象存储 RGW 配置与使用（第6节）

### 6.1 对象存储模型与对比

| 维度 | RBD (块) | CephFS (文件) | RGW (对象) |
|---|---|---|---|
| 访问方式 | iSCSI / KRBD | POSIX 接口 | RESTful API (HTTP) |
| 元数据管理 | 极简（偏移量） | 目录树结构 | 自定义元数据 |
| 典型应用 | 数据库、虚拟机硬盘 | NAS、代码库、共享目录 | 海量图片、视频、备份数据 |

对象存储三大特点：无限水平扩展（扁平结构不受目录树深度限制）、REST API（原生 HTTP 适合云原生）、丰富元数据（每对象可贴标便于搜索）。

### 6.2 RGW 架构
- 构建在 **librados** 之上，提供与 **Amazon S3 / OpenStack Swift 兼容**的网关。
- 主要功能：协议翻译（RESTful → RADOS 指令）、权限控制（用户认证与 Bucket 策略）、租户隔离（多租户与多区域）。
- 架构特点：**无状态 (Stateless)**——实例不存持久化数据，所有状态在 RADOS；高可用（前端 LB 如 HAProxy 多实例）；自动创建专用 Pool（索引、元数据、日志）。
- S3 兼容能力：多版本控制、生命周期管理、静态网站托管、分片上传 (Multipart)、ACL/IAM 策略均支持；主流 S3 SDK（Java/Go/Python）可直接对接。

### 6.3 部署与验证
- 用 cephadm Orchestrator 部署；前提：集群已创建且 HEALTH_OK。
- 验证三件套：
  - `ceph -s`：services 下应包含 rgw；
  - `ceph orch ps --daemon-type rgw`：容器 running；
  - `curl -I http://<RGW_IP>:7480`：返回 HTTP 状态码（200 OK 或带 XML 的 403）即端口已通。

### 6.4 用户与对象管理
- **创建 S3 用户**：`radosgw-admin` 工具；关键输出 `user_id` / `access_key` / `secret_key`（AK/SK 是访问存储桶的唯一凭证，妥善保存）。
- 用户管理：设置配额 Quota（容量/对象数）、查看用户信息、列出所有用户、删除用户。
- **AWS CLI 要点**：Ceph RGW 不是真正的 AWS 服务，**必须显式指定 `--endpoint-url http://<RGW_IP>:7480`**。操作：`aws s3 mb`（建桶）、`aws s3 cp`（上传）、`aws s3 ls`（列对象）。
- **Bucket 名称在整个集群中必须唯一**。
- 大文件：**分片上传**——AWS CLI 默认对 **>5MB** 文件自动启用；切分多个 Part 并行上传，服务端 Complete 合并，支持断点续传。

### 6.5 RGW 数据在 OSD 上的映射（重点）

| 维度 | 对象数据 (.rgw.buckets.data) | 桶索引 (.rgw.buckets.index) |
|---|---|---|
| 存储位置 | OSD 的 Data 块存储区 | OSD 的 OMAP（RocksDB 键值库） |
| 切片行为 | 大文件自动切分 1~N 个对象 | 单一 KV 条目，无切片 |
| I/O 特点 | 大块顺序读写，高吞吐 | 小块随机读写，极低时延 |
| 优化建议 | HDD 纯存储或纠删码 (EC) 降成本 | **必须三副本 + SSD**，防索引损坏 |

### 6.6 生产高可用
多活实例（≥3 个 RGW 在不同节点）+ 负载均衡（HAProxy/Nginx）+ **Keepalived VIP** 防单点 + **Multi-site 跨数据中心异地容灾同步**。

### 6.7 排错
- 无法连接：防火墙 7480 端口、RGW 进程状态。
- 403 Forbidden：系统时间同步、AK/SK 匹配。
- 性能缓慢：Index Pool 读写延迟，需 SSD 加速索引。

---

## 七、文件系统 CephFS 配置与使用（第7节）

### 7.1 定位与特性
- **CephFS 是 Ceph 集群中唯一提供 POSIX 兼容接口的服务**；支持 K8s RWX 卷。
- 定义：符合 POSIX 标准的分布式文件系统，可扩展处理 PB 级数据和数亿文件。
- 三大关键特性：**强一致性**（多客户端看到的文件内容完全同步）、**多挂载支持**（多客户端同时对同一文件读写）、**管理灵活**（原生配额 Quota 与秒级快照）。

### 7.2 与传统 NFS 对比

| 特性 | CephFS | 传统 NFS |
|---|---|---|
| 扩展能力 | 水平扩展（计算存储解耦） | 纵向扩展为主 |
| 可用性 | 内置高可用（MDS 主备） | 通常单点故障 |
| 并发性能 | 极高（客户端与 OSD 直连） | 一般（流量集中在网关） |
| 元数据处理 | 专用 MDS 集群 | 单一服务器处理 |
| 一致性 | POSIX 严格一致性 | 弱一致性 (Close-to-open) |

### 7.3 架构（重点）
- 组件：**MDS**（元数据管理：目录树、权限、Inode）+ **MON**（集群状态）+ **OSD**（数据持久化）。
- **关键设计：数据流与元数据流分离**——客户端先问 MDS 拿元数据，随后**直接与 OSD 通信**做大块数据读写，避免 MDS 成为流量瓶颈。
- MDS 四大职责：管理文件树（目录层次与 Inode）、处理路径查找（POSIX 路径名 → 对象标识）、元数据缓存（内存）、记录日志（修改操作以 Journal 同步到 RADOS Pool）。
- **MDS 状态机**：**Active**（正常服务主节点）/ **Standby**（热备，接管失效主节点）/ **Replay**（正在从日志恢复元数据状态）。

### 7.4 数据组织
- **Metadata Pool**：存目录结构、权限、Inode（文件系统的"大脑"），建议 **64–128 PGs**，基于 SSD/NVMe。
- **Data Pool**：存实际文件分片，可按策略分级（HDD/SSD），PG 按容量计算。
- 所有数据最终切分为 **4MB 左右对象**分布式存于 OSD。

### 7.5 部署与挂载
- 部署：cephadm 编排 MDS → 创建底层 Pools → 初始化文件系统 → 验证（`ceph fs ls`、`ceph fs status my-cephfs`、MDS 进入 `up:active`）。
- **方式一：内核客户端（高性能）**：内核态运行、减少上下文切换、吞吐高，适合高性能计算/重度 IO。挂载要点：MON IP:6789、挂载子目录可写 `/sub_dir`、`-o secretfile=...` 指定密钥文件；生产追加参数：`_netdev`（网络就绪后再挂，防开机卡死）、`rbytes`（df 显示目录真实递归大小）。
- **方式二：FUSE 客户端（易安装）**：用户空间文件系统，不依赖特定内核版本；适用：内核版本低、需灵活切换 Ceph 版本、容器环境快速映射。
- 对比：

| 特性 | 内核客户端 | FUSE |
|---|---|---|
| 实现位置 | Linux 内核空间 | 用户空间 |
| 性能 | 更高（更少数据拷贝） | 稍低（上下文切换开销） |
| 依赖 | 内核支持 ceph.ko | libfuse，兼容性更广 |
| 升级灵活性 | 受限于内核版本 | 随 Ceph 版本快速更新 |
| 调试 | 需分析内核日志 dmesg | 标准用户进程调试 |

- **持久化挂载**：
  - /etc/fstab：改完**切勿直接重启**，必须先 `mount -a` 测试无报错才安全（否则配置错误可能无法开机）；`_netdev` 必加。
  - Systemd Mount Unit：`systemctl enable mnt-cephfs.mount`；每次修改 .mount 后必须 `systemctl daemon-reload`；`systemctl status mnt-cephfs.mount` 查状态。

### 7.6 权限、配额与快照
- 权限：完整支持 POSIX Owner/Group/Others；可创建专属路径授权（如 user1 仅能读写 /project1，实现目录级逻辑隔离）。
- **配额 Quota**：目录级设置容量与文件数配额，无需格式化。⚠️ **配额并非实时硬限制**：客户端本地缓存配额并定期与 MDS 同步（异步近实时），极快写入时可能少量超额；MDS 检测到超额会立即停止该目录后续写入——属于"用微小延迟换高吞吐"的工程权衡。查询 `getfattr -d /path`，清除 `setfattr -x ceph.quota...`。
- **快照**：瞬间创建（毫秒级，无论文件多大）、只读安全、**COW 写时复制**（初始不占空间，仅在原文件修改时记录差异；消耗量 = 被修改的数据量）。
- **Subvolume（子卷）**：为云原生环境（K8s CSI）设计的抽象层，简化子目录作为卷的管理，是 K8s StorageClass 动态卷供应的基础。

### 7.7 性能优化
- 多 MDS 负载均衡：元数据负载极高时增加 `max_mds`（目录树级分担）。
- 缓存：增大 `mds_cache_memory_limit` 减少磁盘访问。
- 挂载参数：`readdir_max_bytes` 优化大目录读取。

### 7.8 排错
- MDS standby 无法上线/无 active：检查 Metadata Pool PG 是否 active+clean；`journalctl -u ceph-mds@*`；确认是否处于 noout 或元数据只读模式。
- 挂载失败：密钥文件权限路径、MON 6789 端口连通、`dmesg | tail` 看错误码。
- 读写慢：网络带宽（建议万兆）、元数据池是否有慢盘。

### 7.9 三大接口横向对比（收束表，必背）

| 特性 | RBD (块) | CephFS (文件) | RGW (对象) |
|---|---|---|---|
| 访问形式 | 裸设备（磁盘） | 目录/文件 | HTTP API (S3) |
| POSIX 支持 | 完全支持（格式化后） | 原生支持 | 不支持 |
| 共享读写 | 一般不支持 | 完美支持 | 完全支持 |
| 元数据处理 | 客户端处理 | MDS 服务处理 | RGW 处理 |
| 典型应用 | VM、数据库 | NAS、容器共享 | 备份、网盘 |

---

## 八、集群监控与告警（第8节）

### 8.1 监控体系目标与动因
链路：**数据采集 → 指标展示 → 异常检测 → 告警通知**。三大动因：
- 容量透支风险：写满（Nearfull）会致 OSD 拒绝写入甚至集群停机；
- 性能黑盒定位：用 Latency/IOPS 区分网络瓶颈、磁盘老化、OSD 分布不均；
- 数据风险预警：PG degraded/undersized 时冗余度降低，需第一时间介入。

### 8.2 官方监控体系
- **Ceph Dashboard**：MGR 内置模块，图形化管理。
- **Prometheus**：时间序列数据库，大规模指标采集与持久化。
- **Node Exporter**：采集节点 OS 层指标（CPU/MEM/IO/Net）。
- **Grafana**：可视化引擎，Ceph 专用大屏。
- **CLI 工具**：`ceph -s` 等，监控最后的防线。

### 8.3 Dashboard 模块与告警配置
- 模块：Cluster（健康度、PG 状态、Raw 存储量、IOPS 走势）、Hosts（节点清单/服务分布）、OSDs（单盘性能、读写延迟、Re-weight）、Pools（副本数、PG 数、使用率）、Performance（聚合性能曲线）。
- 告警链路：**MGR alerts 模块**（告警源头，`ceph mgr module enable alerts`）→ Dashboard（中转展示，set-alertmanager-host）→ **Alertmanager**（去重、分组、静默，9093 端口收 API）→ 通知渠道（SMTP 邮件、Webhook 钉钉/企业微信）。
- **Prometheus 模块**：将集群数千指标暴露为 metrics 文本；覆盖 OSD 性能、Pool 使用率、PG 状态分布、MON 投票状态。
- **Node Exporter**：CPU（OSD 异常占用）、内存（OOM/Swap 风险）、物理 IO（读 /proc/diskstats 拿磁盘物理延迟）、网络（前/后端网口吞吐）。
- prometheus.yml 配置 targets 周期拉取；**Status → Targets 全部 UP；若 Down 检查防火墙放行 9283/9100**。
- Grafana 推荐 Dashboard ID：**2842 (Cluster) | 5336 (OSD) | 5342 (Pools)**。

### 8.4 核心指标（重点数字）
- **容量三级水位**：

| 参数 | 默认阈值 | 集群行为 | 应对 |
|---|---|---|---|
| mon_osd_nearfull_ratio | 85% | Warning 告警，正常读写 | 准备扩容/清理 |
| mon_osd_backfillfull_ratio | 90% | 停止数据迁移（禁止 Backfill） | 紧急介入防压垮 |
| mon_osd_full_ratio | 95% | **拒绝写入，集群变 Read-Only，客户端写入挂起** | 立即删数据或紧急加盘 |

- Health Status：0 = OK，1 = 警告；%RAW USED 超过 85% 进入 Nearfull 必须立即扩容。
- **OSD 延迟**：**Commit Latency**（数据写入 Journal/WAL 的时间）建议 **< 15ms**；**Apply Latency**（数据落入后端存储介质的时间）建议 **< 25ms**。
- **PG 状态**：理想 **active+clean**；**Degraded**（副本数不足，数据可用但有风险）、**Stale**（主 PG 对应 MGR 模块长时间未收到心跳汇报）、**Peering**（副本同步状态中，长期如此代表拓扑异常）。

### 8.5 CLI 诊断三剑客

| 命令 | 场景 | 关注点 |
|---|---|---|
| `ceph -s` | 日常巡检/快速通览 | HEALTH_OK/WARN/ERR、OSD 在线数、PG 分布 |
| `ceph -w` | 故障排查/扩容监控 | 实时事件流：OSD 挂掉、PG backfilling 进度 |
| `ceph health detail` | 深度诊断/根因分析 | 具体错误代码、受影响 PG 编号、slow requests 的 OSD |

- 最佳实践：故障替换、扩容、变更配置后保持一个终端跑 `ceph -w`。

### 8.6 Alertmanager 工作流与运维清单
- 流程：接收告警 → 去重过滤 → **抑制机制**（底层服务挂了自动静默上层次生告警）→ 智能分组 → 路由分发（邮件/企业微信/电话；CRITICAL 级电话/PagerDuty）。
- 通知矩阵：去重（HA 多条相同告警只留一条）、分组（同机房故障合并为一封）、路由（普通→钉钉，Critical→电话）。
- 预防性检查清单：**每日** `ceph -s` / `ceph df`（健康与容量水位）+ `ceph health detail`（异常 OSD/PG）；**每周** Grafana 面板回顾 + `ceph osd df`（负载平衡）；**每月** `ceph report`（容量规划与扩容评审）。

---

## 九、性能调优基础（第9节）

### 9.1 调优方法论（三原则）
1. **变量控制**：一次只改一个参数；2. **可量化**：改前后必须有测试数据对比；3. **分层定位**：硬件（磁盘/网络/CPU）→ 配置（PG/缓存）→ 架构（CRUSH）。

### 9.2 基准测试工具
- **rados bench**：Ceph 原生自带，Pool 级压力测试；输出 Bandwidth / Average IOPS / Average Latency。
- **fio**：更细粒度控制，模拟真实业务场景（数据库、大文件处理）测块存储性能。

### 9.3 瓶颈定位

| 维度 | 命令 | 临界点 | 手段 |
|---|---|---|---|
| 磁盘 | `iostat -x 1` | await > 10ms 或 %util 持续 100% | 换 SSD/NVMe；HDD 可调 bluestore_rocksdb_options |
| 网络 | `iftop`、`ping` | 带宽接近上限、丢包抖动 | 双网物理隔离；MTU 9000 |
| OSD | `ceph osd perf` | commit_latency 过高 | 剔除慢盘；调 osd_max_backfills |

### 9.4 PG 数量优化
- PG 是数据分布的基本单位：**太少**→分布不均、单盘负载高、并发差；**太多**→MON 与 OSD 内存 CPU 开销飙升。
- 官方推荐公式见课件图示；**自 Nautilus 版本起推荐使用 PG Autoscaler 自动管理**；大规模调整触发数据迁移，业务低峰期操作。

### 9.5 CRUSH Map 调优
- 常见操作：调整 OSD 权重（Weight）、修改故障域等级（Host/Rack）、为 SSD/HDD 设置不同 device class。
- 编辑流程：`ceph osd getcrushmap -o bin` 导出 → `crushtool -d bin -o txt` 反编译 → 编辑后重新编译导入。

| 故障域级别 | 物理实体 | 容灾能力 | 实践 |
|---|---|---|---|
| osd | 单块硬盘 | 仅抗单盘损坏 | 开发测试 |
| host（默认） | 单台服务器 | 抗整机宕机 | 生产常规；同一 PG 副本必须落不同 Host |
| rack | 机架 | 抗机架交换机/电源掉电 | 大规模集群推荐 |
| room / row | 机房/机柜列 | 抗单一机房物理灾难 | 跨机房双活/多活 |

### 9.6 网络调优
- **巨型帧 Jumbo Frame**：标准帧 1500 字节 → MTU 9000；显著减少 CPU 中断频率；适用 10/25/100 GbE。
- **网卡绑定 Bonding**：

| 模式 | 名称 | 特点 | 场景 |
|---|---|---|---|
| Mode 0 | balance-rr | 轮询负载均衡 | 需交换机特殊支持 |
| Mode 1 | active-backup | 主备，仅冗余 | 简单高可用 |
| **Mode 4** | 802.3ad | LACP 动态聚合，带宽倍增 | **Ceph 集群首选** |
| Mode 6 | balance-alb | 自适应负载均衡 | 无需交换机配置 |

- **Public/Cluster 分离硬规范**：生产环境 OSD 服务器准备两组独立双网卡 Bonding 接不同交换机，防脑裂；不分离则恢复流量瞬间吃满带宽致前端 IO 挂起；两条网络均建议 MTU 9000。

### 9.7 BlueStore 缓存与内存
- 缓存大小：HDD 1–2G（bluestore_cache_size_hdd）、SSD 3–6G（bluestore_cache_size_ssd）。
- 缓存切片默认比例：**KV 缓存 (RocksDB) 0.4**（读写放大严重/Omap 大时调大）、**元数据缓存 0.4**（海量小文件场景调大可暴增 IOPS）、**数据缓存 0.2**（自动计算剩余；分布式存储更依赖客户端缓存，故占比最少）。
- **OSD 内存防 OOM**：
  - `osd_memory_target`：单 OSD 内存目标，SSD 4–6G、HDD 4G（Ceph 据此动态伸缩内部缓存）；
  - 单节点整机：**预留 20% + 16GB** 给内核/MON/MGR；
  - `MALLOC_RELEASE_RATE = 10`：加速归还空闲内存，减小碎片虚高；
  - 单个 OSD 建议预留 4–8GB 内存（含 BlueStore 元数据缓存、OSD 自身 ~3GB、网络缓冲与 PG 元数据）。

### 9.8 内核调度器与 DB/WAL 分离

| 设备 | 推荐调度器 |
|---|---|
| NVMe SSD | none |
| SATA SSD | mq-deadline |
| HDD | mq-deadline（减少磁头随机寻道开销） |

- **DB/WAL 分离是 HDD OSD 性能提升最显著的手段**：Data 在 HDD，RocksDB 元数据 + WAL 在 NVMe/SSD。

### 9.9 后端核心参数推荐（默认值）
- 恢复并发：`osd_max_backfills = 1`、`osd_recovery_max_active = 3`。
- IO 优先级：`osd_client_op_priority = 63`（客户端优先）、`osd_recovery_op_priority = 3`（限制恢复速度）。
- PG 限制：`osd_max_pg_per_osd = 250`（防单节点承载过多 PG）。

### 9.10 检查清单与排错
- Checklist：公网/集群网物理分离；全链路 MTU 9000；LACP 绑定；DB/WAL 用企业级 NVMe；PG 合理且开 Autoscaler；OSD 内存 Target 已设定；调度器 none/mq；副本数推荐 3。
- 排错三例：写入延迟极高（`ceph osd slow-requests`、iostat await、丢包 → 剔慢盘/双网分离）；IOPS 随时间下降（`ceph tell osd.* perf dump`、RocksDB 碎片或后台 Backfill → 压低 osd_max_backfills=1 / recovery_max_active=1）；OSD 进程崩溃（`dmesg -T | grep -i oom`，多发生于 Scrub 或高并发写入 → osd_memory_target=4G、MALLOC_RELEASE_RATE=10）。

---

## 十、常见故障排查：OSD 故障专题（第10节）

### 10.1 故障分类

| 类型 | 典型表现 | 常见根因 |
|---|---|---|
| 磁盘故障 | OSD Down / I/O error | 坏道、SMART 报错、控制器断连 |
| 网络抖动 | OSD Flapping（频繁 Up/Down） | 网线松动、网卡驱动 Bug、交换机拥塞 |
| 资源不足 | 被 OOM Killer 强杀 | 内存配置超物理上限、缓存溢出 |
| 空间爆满 | HEALTH_ERR: nearfull/full | Pool 占比不均、写入突增、扩容滞后 |

### 10.2 状态矩阵与处置
- Up+In 正常服务；**Down+In 紧急（数据仍属该 OSD 但无法访问）**：查磁盘、重启进程；Up+Out 维护中（数据迁移）；**Down+Out 确定性损坏**：物理换盘重新部署。

### 10.3 分级诊断命令
- 第一级全景：`ceph osd tree`（Down 节点在拓扑中的位置）、`ceph osd stat`（Up/In 统计）。
- 第二级指标：`ceph osd perf`（延迟飙升）、`ceph osd df`（空间倾斜）。
- 第三级日志：`dmesg | grep -i osd`（硬件/内核 OOM 报错）。

### 10.4 OSD Down 标准流程
1. 发现：`ceph osd tree | grep -i down`、`ceph health detail` → 锁定主机与 osd.X；
2. 进程检查：`systemctl status ceph-osd@X`、`journalctl -u ceph-osd@X -n 50`（是否 inactive/dead、是否 OOM）；
3. 硬件验证：`smartctl -H /dev/sdX`（PASSED?）、`smartctl -a`（重点 Reallocated_Sector_Ct 坏道数）、`dmesg | grep -i -E "error|ata|scsi"`；
4. 恢复决策：尝试 `systemctl restart ceph-osd@X`；确认硬件损坏则 `ceph osd out X` 触发迁移。
- 网络补充诊断：`ping -s 8972 <IP>`（验 MTU 9000 全链路，大包不通会致心跳超时）、`iperf3 -c`（带宽）、`mtr --report`（隐蔽丢包）。
- **重启原则：逐个操作，绝不允许同时重启同一故障域下多个 OSD；重启后 `ceph -w` 观察 PG 恢复**。

### 10.5 空间满紧急处理（SOP）
- 水位：85% NEARFULL（告警）→ 90% BACKFILLFULL（停止迁移）→ 95% FULL（**PAUSE_WRITE 拒绝所有 IO，集群强行只读**）。
- 四步处置：
  1. 紧急救火：`ceph osd set full_ratio 0.97` 临时放宽（仅限救急，须在 1 小时内删掉无用数据，否则物理满载会致 OSD 崩溃）；
  2. 空间释放：`rbd snap rm`、`rbd trash purge`（清过期镜像/垃圾卷）；
  3. 负载再平衡：`ceph osd reweight-by-utilization`（防部分 OSD 偏满的"假空间满"）；
  4. 横向扩容：新增 OSD 硬盘根治。
- 运维红线：**70% 告警、80% 必须处理**。

### 10.6 安全移除 OSD 五步
1. `ceph osd out osd.X`（触发重平衡，集群保持 3 副本）；
2. Wait：`ceph -w` / `ceph osd safe-to-destroy osd.X`（等 HEALTH_OK）；
3. `systemctl stop ceph-osd@X`；
4. `ceph osd crush rm osd.X` + `ceph auth del osd.X`；
5. `ceph osd purge osd.X --yes-i-really-mean-it`（一键完成 4&5，从 Monitor 彻底删除）。

### 10.7 磁盘更换与 OOM
- 换盘"先出后进"：逻辑下线 out → `ceph osd stop` → 点亮定位灯 `ceph device light on`（按 Serial No. 确认槽位防拔错盘）→ 物理插拔 → `ceph orch device zap <node> <device> --force`（清 LVM/GPT 残留）→ `ceph orch daemon add osd <node>:<device>`。
- **OOM 处理**：症状为 Out of Memory、进程频繁退出（多发生在重平衡期间，元数据开销激增）；对策：设定内存目标（限制缓存）、**禁用透明大页**（减少碎片浪费）、硬件升级（**每 TB 存储配 4–8G 内存**）。

### 10.8 Scrub 数据校验

| 特性 | Light Scrub | Deep Scrub |
|---|---|---|
| 频率 | 通常每日一次 | 通常每周一次 |
| 内容 | 对象元数据、Checksum | 逐字节比对所有副本 |
| IO 消耗 | 低（仅扫描索引） | 高（读全部原始数据） |
| 目标 | 验证索引一致性 | 发现寂静损坏 (Bit rot) |

- 痛点：业务高峰期 Scrub 增加客户端延迟 → 配置时间窗口（凌晨低峰执行）、系统负载高于阈值时暂停。

### 10.9 PG 状态全景与修复

| PG 状态 | 含义 | 处理 |
|---|---|---|
| Degraded | 副本数不满足设定值 | 正常恢复中，观察回填进度 |
| Inconsistent | 副本间数据校验不一致 | `ceph pg repair` |
| Stale | OSD 与 MON 通信超时，状态过期 | 检查 MON Quorum 稳定性 |
| Peering | 正在协商主副本，暂不可读写 | 通常自动结束，卡住查 OSD |
| Incomplete | 丢失关键历史数据片段 | 极度危险，需找回旧 OSD |

- Stuck 三态：Stuck Inactive（主副本所需 OSD 永久掉线，无法 Peering）、Stuck Unclean（无法补齐副本：目标 OSD 空间满或带宽耗尽）、Stuck Stale（MON 联系不到任何 OSD：排查网络分区 Split Brain）。诊断：`ceph pg query`。
- **Inconsistent 修复流程**：① `ceph health detail` / `ceph pg dump | grep -i inconsistent` 定位 PG ID；② `ceph pg deep-scrub <pg_id>` 复验排除假阳性；③ `ceph pg repair <pg_id>`（**默认以主副本数据强制覆盖少数派副本——若主副本恰好是坏数据，会"劣币驱逐良币"**）；④ 高级：`rados list-inconsistent-obj <pg_id>` 比对 Digest/Size 找正确副本，必要时从健康 OSD 手动导出覆盖。

### 10.10 MON 排查与 MGR
- MON 排查点：时钟偏差 < 0.05s；Quorum 丢失（存活数 < 50% 节点数）；MON 磁盘爆满（数据库无法写 WAL）。
- **强制重建 Monmap**（仅一个存活 MON 且无法启动时）：`ceph-mon -i mon.a --extract-monmap /tmp/monmap` → `monmaptool --rm <failed_mon_id>` → `--inject-monmap` → 重启 ceph-mon 重建 Quorum。
- MGR 故障影响：Dashboard 短暂不可用（备 MGR 升主后自动重定向）、Prometheus 采集断流（Target 配所有 MGR 或 VIP）；手动切主：`ceph mgr fail <active_mgr_id>`。

---

## 十一、集群扩容与缩容（第11节）

### 11.1 扩展方式
- **纵向 Scale Up**：加单节点资源，受插槽/总线限制；**横向 Scale Out**：加服务器，Ceph 核心优势；**平滑扩容**：在线加 OSD/节点，CRUSH 自动算新位置，业务不停机。
- 扩容动因：业务增长（容量/IOPS 超限）、硬件更新、维护需求（移除故障盘/迁机房）。
- 前置检查：硬件就绪（磁盘挂载、Bonding/MTU）、`ssh-copy-id` 互信、NTP/Chrony 正常、`ceph -s` 为 HEALTH_OK。
- 新节点网络验收：`ping -s 8972 -M do <IP>`（巨型帧；"Message too long"即异常）、`iperf3 -c <IP> -t 10`（带宽基线）、`mtr -c 100 --report <IP>`（Loss% 应为 0，否则心跳超时）。

### 11.2 添加节点与 OSD
- `ceph orch host add`；**主机标签管理**：`_admin`（分发 ceph.conf 与 admin 密钥，管理节点必备）、`mon`（3 或 5 个奇数）、`osd`（placement: label:osd）、`rgw`/`mds`。
- 加 OSD：自动检测应用（自动发现所有空余磁盘）或指定特定设备（混合盘场景）；执行后自动触发数据重平衡。
- **声明式 YAML 编排**：`ceph orch apply -i osd_spec.yaml`；对比 CLI 手工：可重复（Git 版本控制）、批量按 Label 生效、声明式收敛防配置漂移。

### 11.3 数据重平衡与限速
- 流程：拓扑感知（改 CRUSH Map）→ 标记错位（**Misplaced = 位置不正确的对象，并非数据丢失，仍可正常读写**）→ 后台迁移（主 PG 拷贝至新 OSD 后清理旧数据）。
- **业务影响控制**：

| 场景 | osd_max_backfills | osd_recovery_max_active | 策略 |
|---|---|---|---|
| 业务高峰期 | 1 | 1 | 保障业务 IO 优先 |
| 低峰/夜间 | 3–5 | 5–8 | 加快重平衡 |
| 极速扩容 | 8+ | 10+ | 牺牲部分客户端性能 |

### 11.4 扩展 RGW / MDS / MGR
- RGW：横向加实例分担 HTTP 并发，配 HAProxy/Keepalived；`ceph orch apply rgw`。
- MDS：增加 Active MDS 开多活，提升元数据吞吐；需评估目录分片 (Subtree Partitioning)。
- MGR：跨节点 Active/Standby 冗余，保障 Dashboard 与 Prometheus 插件切换。

### 11.5 缩容与安全下线
- **安全移除 OSD 四步**：Mark OUT（停止分配数据并触发迁移）→ Wait（等重平衡至健康）→ Safe Check（确认 safe-to-destroy）→ Purge（停进程、删 CRUSH 与 Auth 记录）。
- 前置校验：`ceph df` & `ceph osd df`，**剩余使用率须低于 70%**；确认故障域避免单点；大容量盘迁移可能耗时数小时至数天；**严禁同时移除过多磁盘**。
- Host 级下线：逐个下线 OSD → 迁移 MON/MGR 等服务 → `ceph orch host drain nodeX` → 最终从 Orchestrator 注销。
- **MON 迁移：先增后删保 Quorum**——① `ceph quorum_status -f json-pretty` 前置检查（奇数且健康）；② `ceph orch daemon add mon <new_node>`（Quorum 暂时扩大，如 3→4）；③ `ceph mon stat` 确认加入；④ `ceph orch daemon rm mon.<old>`（恢复奇数，如 4→3）。

### 11.6 CRUSH 权重与动态均衡
- Ceph 按 OSD 权重分配数据，不匹配会致磁盘空间倾斜：1TB HDD → 1.0（基准）、4TB → 4.0、8TB → 8.0（按容量比例；定期校对实际分布）。
- `ceph osd reweight-by-utilization 105`：自动扫描使用率、降过载/升空闲权重，将偏差控制在 5% 以内。

### 11.7 最佳实践与排查
- 容量预留：始终保留 20–30% 缓冲，**利用率低于 70% 触发扩容**（防 nearfull/full 只读）；渐进式：每次 1–3 个节点；硬件同质性（同 Pool 内 OSD 介质/容量/网卡一致，防"木桶短板"慢 Request）；测试先行（Staging 模拟迁移速率，锁定维护窗口）。
- 常见问题：新节点加不上（SSH/防火墙 2443, 6800-7300/NTP → 免密、放行、chronyc sources）；移除 OSD 卡住（磁盘满或 PG 非 clean：`ceph pg dump_stuck`、调 osd_recovery_op_priority）；扩容后性能下降（Rebalance 并发过高：`ceph config set global osd_max_backfills 1`）；新 OSD 拉不起 Zap 失败（LVM/GPT 残留：`lsblk`、`ceph orch device zap --force`）。

---

## 十二、数据备份与恢复（第12节）

### 12.1 副本 vs 备份（核心概念辨析）
- **副本 (Replication)**：目的为硬件故障时高可用；实时同步、同一集群内、**无法防止误删（同步删除）**。
- **备份 (Backup)**：应对逻辑错误与灾难恢复；**特定时间点隔离、跨集群/异地存储、支持历史版本回溯**。
- Ceph 原生保护的局限：副本/纠删码仅防物理磁盘故障；**无法防逻辑误删、勒索病毒、整站点物理灾难（火灾断电）、应用级错误（数据库需时间点恢复）**。

### 12.2 备份黄金准则：3-2-1 原则
- **3 份数据副本**（1 份生产 + 2 份备份）、**2 种不同介质**（Disk + 磁带/对象存储 S3，防单一硬件缺陷）、**1 份异地保存**（异地 DR 机房/公有云归档，防火灾与区域性自然灾害）。

### 12.3 RBD 备份
- **快照时间点恢复流程**：创建快照 `rbd snap create pool/img@snap1` → Cron 定期备份 → 故障发生 → 回滚 `rbd snap rollback pool/img@snap1`。⚠️ **回滚前必须先 `rbd unmap` 卸载块设备阻断写 IO**；若只需恢复单个文件，建议用 rbd-nbd 只读挂载快照提取，无需整体回滚。
- **自动化快照**：Cron（如每小时 `0 * * * *` 跑脚本），保留最近 24 小时，轮转清理过期快照释放元数据开销。
- **快照保护链（顺序必背）**：`rbd snap create` → `rbd snap protect`（锁定防误删，**克隆的必要前置条件**）→ `rbd clone pool/img@snap1 pool/clone_img`（基于受保护快照建可写克隆，COW 共享数据块）→（可选）`rbd flatten`（解除父子依赖，拷贝全部数据块，之后父快照才可删）→ `rbd snap unprotect`（**有任何 Clone 存在时取消保护会报错**）。
- **导出/导入（离线备份与跨集群迁移）**：全量 `rbd export pool/vm01 /backup/vm01.img`；增量 `rbd export-diff pool/vm01@snap1 --from-snap snap0 out.img`（只导两次快照间变化量，快且小）；恢复全量 `rbd import`；追增量 `rbd import-diff`（**必须依附已有基础镜像或前序快照链**）。
- **优化三招**：管道压缩 `rbd export pool/vm01 - | pigz -c > vm01.img.gz`；多线程并行 `cat images.txt | xargs -P 4 -I {} rbd export ...`（控并发防 OSD 过载）；`--export-format 2` 跳过全零块。

### 12.4 CephFS 备份
- 快照基于**目录级 `.snap` 虚拟目录**：先 `ceph fs set <fs> allow_new_snaps true` 开启；创建 `mkdir /mnt/cephfs/dir/.snap/snap_v1`（秒级，MDS 记录元数据快照）；恢复单文件 `cp .snap/snap_v1/file.txt ./`（**用户自助恢复，无需管理员**）；删除 `rmdir data/.snap/backup-01`（后台异步回收；**禁止 rm -rf**）。
- 管理建议：容量监控 `getfattr -n ceph.dir.rbytes`（COW 初建不占空间，但频繁修改会暴涨）；保留策略 **7 天日快照 + 4 周周快照**，自动清理防 MDS 元数据膨胀；低峰期执行 rmdir；配 `ceph.quota.max_bytes` 防快照写满池。

### 12.5 RGW 容灾
- **Versioning 版本控制**：开启后每次修改/删除保留历史版本；**逻辑删除只是堆叠 Delete Marker，历史数据仍完好**；用 Version-ID GET 可秒级回滚单对象；指定 version-id DELETE 才物理删除（通常配 Lifecycle 自动清理）。
- **Lifecycle 生命周期**：版本过期（自动清理 30 天前非当前版本）、自动转换（90 天未访问日志迁 GLACIER 冷存储）、基于 Prefix 定制保护深度。
- **Multisite 多站点容灾**：跨地域**异步**复制；层级：**Realm（全局域，全局唯一元数据命名空间）→ ZoneGroup（域组，一个地理区域）→ Zone（数据区，对应真实数据中心/独立集群）→ Sync Status（Active-Active 双活或 Active-Standby 主备）**；命令如 `radosgw-admin realm create` / `zonegroup create` / `zone create` / `sync status`。

### 12.6 方案选型与底层兜底

| 场景 | 推荐方案 | 一致性 |
|---|---|---|
| VM/数据库磁盘 | RBD 增量快照 + Export-Diff | 高（配合 Flush） |
| 办公文档/用户主目录 | CephFS .snap 自动清理策略 | 中 |
| 海量图片/静态文件 | RGW 版本控制 + Multisite | 高 |
| 极端站点容灾 | 跨集群 RBD 镜像同步 | 最高 |

- **RADOS 级兜底 (Last Resort)**：`rados -p <pool> ls` 遍历对象；`rados -p <pool> get <obj> <file>` 直接提取二进制对象；`rados export / import` 池级全量打包迁移；元数据备份：定期 `radosgw-admin metadata`。
- 自动化三要素：脚本中心化（Python/Shell 遍历多镜像）、告警集成（备份失败推 Prometheus/企业微信）、**可恢复性测试（定期随机恢复演练，确保备份真实可用）**。

---

## 十三、Ceph 与 Kubernetes 集成 CSI（第13节）

### 13.1 K8s 存储三件套

| 组件 | 职能 |
|---|---|
| PersistentVolume (PV) | 集群实际存储资源，管理员配置或动态生成 |
| PersistentVolumeClaim (PVC) | 用户对存储的请求（类比 Pod 请求 CPU/内存） |
| StorageClass (SC) | 存储类，定义类型、参数及动态供应方式 |

### 13.2 Ceph CSI 架构

| 维度 | Controller Plugin（集群侧） | Node Plugin（节点侧） |
|---|---|---|
| 部署形态 | Deployment（多副本高可用，Master 节点） | DaemonSet（每个 Worker 节点） |
| 功能 | 卷/快照生命周期管理（创建、删除、扩容） | 本地挂载、解挂、格式化与目录映射 |
| 核心 RPC | CreateVolume, DeleteVolume, CreateSnapshot | NodeStageVolume, NodePublishVolume |
| 范围 | 控制面 Control Plane | 数据面 Data Plane |

- 部署：推荐 **Helm Chart**；部署后确认 csi-rbdplugin 与 csi-cephfsplugin 所有 Pod Running。
- Sidecar：Provisioner、Snapshotter 等。

### 13.3 凭据管理（最小权限原则）
- 为每个 K8s 集群/租户创建**专属 client.kubernetes 账号**（❌ 不用 client.admin）；权限限定特定 pool（如 `pool=kubernetes` 的 rwx）；Secret 统一放 kube-system 或专用命名空间并配 RBAC；定期轮换 Keyring 并同步更新 K8s Secret。

### 13.4 RBD StorageClass 关键参数
- `clusterID`：Ceph 的 FSID（唯一标识集群）；`imageFeatures`：**必须设为 layering 以兼容内核**；`reclaimPolicy`：Delete 则 PVC 删除时自动清理底层 RBD 镜像；`allowVolumeExpansion: true` 允许在线扩容。
- **动态供给四步链路**：① 用户提交 PVC（指定 storageClassName）→ ② CSI Provisioner 监听并匹配 StorageClass（读 clusterID 与 pool）→ ③ CSI 调 Ceph API 自动创建 RBD 镜像（csi-vol-xxx）或 CephFS Subvolume → ④ 自动创建 PV 并 Bound 绑定，无需人工。

### 13.5 RWO vs RWX 选型（必考）

| 维度 | RBD (块) | CephFS (文件) |
|---|---|---|
| 访问模式 | ReadWriteOnce (RWO)，单节点读写 | ReadWriteMany (RWX)，数百 Pod 跨节点共享 |
| 底层原理 | 直接暴露块设备（无分布式锁） | MDS 维护统一文件系统树 |
| 性能/开销 | I/O 路径短，极高，无元数据开销 | 有元数据交互开销 |
| 典型场景 | MySQL/Redis/Postgres、Kafka/RabbitMQ、VM 磁盘 | Web 共享静态资源、上传目录、CI/CD 产物、日志采集 |

- CephFS StorageClass 参数：fsName（文件系统名）、pool（数据池）、mounter（推荐 kernel 模式）。
- 场景速查：有状态单节点 MySQL → RBD `/var/lib/mysql` RWO；无状态多副本 Nginx → CephFS `/usr/share/nginx/html` RWX；日志集中采集 Filebeat → CephFS `/var/log/nginx/` RWX。
- **经验法则："能用 RBD 就不用 CephFS"**（绝大多数有状态应用优先 RBD）。

### 13.6 StatefulSet 有状态存储
- `volumeClaimTemplates` 为每个副本自动创建独立卷：Pod-N ↔ PVC data-mysql-N ↔ PV（Ceph RBD Vol-N）**一一绑定**；Pod 崩溃漂移重建后仍精准挂回原卷，数据不丢状态不变。
- 与 Deployment 对比：Deployment Pod 随机后缀、多副本共享同一 PVC、重建可能挂载冲突；StatefulSet 固定序号、按副本独立 PVC、重建精准重连原 PV。

### 13.7 在线扩容 / 快照 / 克隆
- **在线扩容**：StorageClass 开 `allowVolumeExpansion: true` → `kubectl edit pvc` 改 storage 值 → CSI 通知 Ceph 扩 RBD 镜像并触发节点文件系统扩展。⚠️ **扩容通常不支持缩容**；若 PV 扩容成功但容器内未变，需重启 Pod 触发文件系统 resize。
- **卷快照 VolumeSnapshot**：创建 VolumeSnapshotClass → 声明 VolumeSnapshot 指向源 PVC → 恢复时新 PVC 指定 dataSource。优势：秒级创建（COW 只记元数据指针）、按需分配（仅源数据被修改时才写新数据）、瞬间克隆。
- **卷克隆 Volume Clone**：底层为 RBD COW 克隆；机制：克隆前自动创建并保护快照（Protect Snapshot）→ 新卷初始仅建引用指针（秒级、不占额外空间）→ 读未修改块直接从父快照读，写则写入克隆卷自己的差异空间。场景：Dev/Test 克隆、破坏性操作前备份、多分析 Pod 并行处理同源数据。

### 13.8 拓扑感知调度
- 目标：Pod 调度到与 OSD 网络距离最近的节点（跨机房/AZ 场景降低 I/O 延迟与跨区流量）。
- **volumeBindingMode**：
  - `Immediate`（默认）：PVC 一创建就在随机节点/AZ 建 PV → Pod 可能调度到没有该存储的节点 → **VolumeZoneConflict** 挂载失败；
  - **`WaitForFirstConsumer`（拓扑感知必备）**：PVC 创建后先不生成 PV，等使用它的 Pod 被创建时，调度器综合节点约束与存储拓扑选定最佳节点后再建 PV，实现存算同区。

### 13.9 排错速查

| 现象 | 根因 | 处置 |
|---|---|---|
| PVC Pending | StorageClass 错 / Secret 鉴权失败 / Pool 空间不足 | `kubectl describe pvc`；查 SC、Secret 权限、`ceph df` |
| Mount Failed | 网络不通 / 缺内核模块 / 节点响应超时 | Ping 通 MON；`lsmod | grep rbd` 确认模块 |
| Resize 失败 | 未开 allowVolumeExpansion / 文件系统未扩展 | 确认 SC 参数；重启 Pod 触发 resize |
| Snapshot Failed | VolumeSnapshotClass 未定义 / external-snapshotter CRD 缺失 | 安装 CRD、启用驱动快照功能 |
| VolumeToolAttachFailed | CSI Driver 通信失败或密钥失效 | 查 Secret 与 MON 连通性 |
| ProvisioningFailed | 池空间不足或配额超限 | `ceph df` 查剩余容量 |

- 资源关联：`kubectl get pv` 看卷 ID → Ceph 端 `rbd ls` 找物理镜像；日志看 csi-rbdplugin-provisioner（挂载失败第一现场）；RBD PVC 映射到 kubernetes Pool，CephFS PVC 映射到 subvolumes。

### 13.10 生产最佳实践
- 池化管理：K8s 用独立 Pool，禁与 OpenStack 等混用；回收策略：初期用 Delete，**重要数据用 Retain 手动清理**（核心业务卷 reclaimPolicy 务必 Retain 防误删）；反亲和：数据库副本 Pod 分散到不同物理节点。
- 容量水位（本节课件表述）：Nearfull 75% 介入清理，Full 85% 触发紧急扩容；QoS：StorageClass 参数 rbd:qosMaxIOPS 限速非核心应用；监控：Prometheus + Grafana，关注 MDS 延迟、PG Clean、OSD 抖动、PVC 使用率。

---

## 十四、Ceph 集群安全配置（第14节）

### 14.1 四维风险与三层防御
- 四维：访问控制（谁能连）、操作范围（连上能做什么）、数据隐私（传输是否被窃听）、合规审计（行为可追溯）。
- 三层：**认证层**（cephx 双向认证 + Keyring）→ **授权层**（Caps 精细控制 Pool/命令/路径）→ **加密层**（msgr2.1 链路加密 + HTTPS 终端加密；静态加密支持 LUKS 与 KMS）。
- 生产实践：强制开启全集群身份鉴权（禁 `auth_cluster_required = none`）；K8s CSI 账户只赋予对应 StorageClass 存储池权限。

### 14.2 cephx 认证（重点）
- 特点：**双向认证**（客户端验证集群，集群验证客户端）、**Ticket 门票机制**（一次性 Ticket 避免频繁传密钥）、**无单点故障**（每个 MON 都能独立处理认证）。
- 四步流程：① 初始请求：Client 用共享密钥 (Keyring) 向 MON 请求，获取全局通行令牌 Ticket → ② 获取服务授权：持 Ticket 向 MON 请求具体服务（OSD/MDS）的 Session Key → ③ 直接通信：携 Session Key 与 OSD 直接通信并签名校验，**数据传输不再经过 MON**。
- 设计优势：**密钥零明文传输**（共享密钥 + 对称加密协商动态 Session Key）；MON 极简参与（仅建连时 1–3 步认证，无性能瓶颈）；Ticket 自带过期时间并配合随机数 Nonce 防重放攻击。

### 14.3 Keyring 与用户管理
- 默认路径：`/etc/ceph/ceph.client.admin.keyring`（client.admin，最高 * 权限）；`/var/lib/ceph/osd/ceph-{id}/keyring`（osd 注册通信）；`/var/lib/ceph/mon/ceph-{id}/keyring`（MON 同步选举）；`/etc/ceph/ceph.client.{user}.keyring`（客户端专用）。
- 命名规范：**`client.` 前缀 = 外部使用者**；**`daemon.` 类型 = 集群内部组件**（osd.0、mon.node1）；每个应用独立用户，禁止混用。
- 创建示例：`ceph auth get-or-create client.k8s-prod mon 'allow r' osd 'allow rwx pool=k8s-pool'`。

### 14.4 Capabilities (Caps) 权限

| 权限位 | 描述 |
|---|---|
| r | 读取（获取数据、列对象）——MON/OSD/MDS/MGR |
| w | 写入（创建、修改）——OSD/MDS |
| x | 执行（调用类方法 Class Methods）——OSD |
| * | 所有权限（等同管理员） |
| class-read/write | 细粒度类方法访问控制——OSD |

- 作用域：MON 权限限管理命令（只读状态 + 黑名单操作）；OSD 权限限存储池（业务池 rwx、备份池只读）；MDS 权限限目录路径；MGR 权限限 Dashboard/插件指标只读。
- **Profile 预定义模板**：`rbd`（块客户端全部所需权限）、`rbd-read-only`（备份/监控只读）、`rgw`（网关元数据池权限）、`bootstrap-{type}`（部署工具初始化）。
- **RBD vs CephFS 用户创建的核心差异：CephFS 必须额外配置 MDS 权限**。
  - RBD：`mon 'profile rbd' osd 'profile rbd pool=k8s'`；
  - CephFS：`mds 'allow rw path=/app1'`（目录隔离）+ `osd 'allow rw pool=fs_data'`（数据池）。
- 调试：`ceph auth list` 审计；`ceph -n client.app --keyring app.keyring osd pool ls` 以指定身份模拟；Monitor 日志搜 auth 关键字查拒绝来源。
- Permission denied 速查：挂 RBD 报 AccessDenied → 补 OSD pool rwx（`ceph auth caps client.app mon 'allow r' osd 'allow rwx pool=rbd-pool'`）；CephFS 报 Operation not permitted → 补 MDS 路径 Caps；Keyring not found → 检查 ceph.conf 的 keyring 路径配置。

### 14.5 权限变更风险与 Keyring 物理安全
- `ceph auth caps` **实时生效（<1s）且是覆盖式**——遗漏的 Caps 立刻失效，建议先 `ceph auth get` 备份；`ceph auth del` 即刻生效且高危（依赖此 Keyring 的 Pod 挂载失败，需先从 K8s Secret 卸载）；`ceph auth change-key` 轮换密钥后**必须同步更新 K8s CSI Secret**。
- Keyring 文件：`chmod 600` 且属主 root；严禁邮件/聊天工具分发（用 scp/Ansible）；严禁提交 Git（加 .gitignore）；泄露即刻 `ceph auth change-key` 重置。

### 14.6 RGW SSL/TLS（Beast 前端）
- RGW 常暴露公网，HTTPS 必选：生产用 Let's Encrypt 或商业 CA，测试可自签名；防 AK/SK 被拦截；满足 PCI-DSS 等合规。
- 配置要点：`rgw_frontends` 设 `ssl_port=443s`——**末尾 s 标志代表强制 SSL/TLS 握手，遗漏则端口无法正常加密**；证书文件属主 ceph:ceph、权限 600；Beast 不支持分开时可把 .crt/.key 合并为 .pem。
- 验证：`openssl s_client -connect rgw.example.com:443 -tls1_2`（证书链 + Verify return code: 0）；`aws --endpoint-url https://... s3 ls` 正常列出桶。
- **HTTPS 最佳实践四条**：① 80 → 443 强制 301 重定向（前置 HAProxy/Nginx）；② HSTS 响应头（推荐 `max-age=31536000; includeSubDomains; preload`，防 SSL Stripping 降级攻击）；③ 协议收紧：**禁用 TLS 1.0/1.1，仅留 TLS 1.2/1.3**，用具备前向安全 (PFS) 的套件（如 ECDHE-ECDSA-AES128-GCM-SHA256）；④ 证书全生命周期监控：**过期前 30 天告警**，Cert-Manager / Let's Encrypt + Crontab 自动续签。

### 14.7 Messenger v2 (msgr2) 传输加密
- 对比：msgr1 旧版——端口 6789、明文传输、仅认证不加密；**msgr2.1 新版——端口 3300、secure 模式全量加密、支持 AES-GCM 硬件加速（Intel AES-NI）**。
- 三种模式：`crc`（仅校验不加密，内网极致吞吐）、`secure`（全量 AES-GCM，跨机房/公网/高合规）、`prefer-crc / prefer-secure`（协商降级兼容，迁移过渡期）。
- 配置：`ceph mon enable-msgr2` 开启 3300 绑定；`ms_cluster_mode`（Daemon 间）、`ms_service_mode`（服务端对外）、`ms_client_mode`（客户端倾向）。

### 14.8 网络物理隔离与管理台安全
- 三面隔离：Public Network（客户端，开 3300/6789）、Cluster Network（OSD 同步，**完全闭锁，严禁外部路由进入**）、Management（SSH/Dashboard，限堡垒机 IP）。价值：业务网被攻破也嗅探不到存储同步流量；流量互不干扰；攻击面最小化。落地：独立 NIC + VLAN/交换机；ACL + 堡垒机白名单。
- Dashboard 安全：RBAC 角色（administrator / read-only / block-manager）；强密码策略（8 位以上 + 复杂度，推荐 MFA）；**强制 SSL**（`ceph dashboard set-ssl-certificate[-key] -i ...`，否则凭据明文）；会话超时（如 15 分钟）；操作审计（日志同步 syslog/SIEM）。
- **SSO 单点登录**：SAML 2.0（`ceph dashboard sso enable saml2`，对接 Keycloak/ADFS）与 OIDC（OAuth2.0 / OpenID Connect）。

### 14.9 安全审计自查清单
身份与访问（每个业务独立 Keyring，严禁共享 client.admin——高危）；权限控制（Caps 收敛到特定 Pool——高危）；传输安全（公网 HTTPS + 内部 msgr2 加密——中危）；网络隔离（Cluster 网禁止外网路由，VLAN/物理隔离——高危）；系统加固（SSH 白名单、禁不必要服务——中危）。

**第14节总结四句**：cephx 认证身份；Caps 控制行为；SSL/msgr2 保护传输数据；RBAC 规范管理者操作。

---

## 附录 A：关键数字速记表

| 数字 | 含义 |
|---|---|
| 4MB | RBD/CephFS 默认对象切分大小（order 22 = 2²² = 4MB） |
| 10GB/4M ≈ 2560 | 10G 镜像的对象数 |
| 3 副本 = 33% 可用率 | Size=3 允许同时坏 2 块盘；Size=2 = 50%（不推荐）；Size=4 = 25% |
| 4+2 EC ≈ 66% | 纠删码利用率 |
| 0.8 / 80% | 容量规划安全系数（超 80% 性能剧烈下降） |
| 65–70% | 理想扩容时机；缩容前置校验要求剩余使用率 < 70%；运维红线 70% 告警 / 80% 必须处理 |
| 20–30% | 常年预留缓冲空间 |
| 85% / 90% / 95% | nearfull / backfillfull / full 官方默认水位（95% 拒绝写入只读） |
| 50ms (0.05s) | MON 时钟偏差上限（mon_clock_drift_allowed），超限 CLOCK_SKEW |
| 600s | mon_osd_down_out_interval（DOWN 后 10 分钟未恢复即 OUT） |
| 3 次 | mon_osd_min_down_reports（汇报失联次数阈值） |
| MON Quorum > N/2 | 3 个坏 1 个、5 个坏 2 个；必须奇数 |
| 6789 / 3300 | msgr1 / msgr2 MON 端口；OSD 6800–7300 |
| 7480 | RGW 默认 HTTP 端口；8443 Dashboard；9283 Prometheus exporter；9100 Node Exporter；9093 Alertmanager |
| Commit < 15ms / Apply < 25ms | OSD 延迟建议值 |
| DB = Data 的 4% 或 ≥30GB | BlueStore 混合模式 DB 分区规划 |
| 1–2 / 2–4 / 4–8+ Core | HDD / SSD / NVMe 每 OSD 的 CPU 配比 |
| osd_memory_target 4G（HDD）/ 4–6G（SSD） | 单 OSD 内存目标；节点另预留 20% + 16GB；每 TB 存储 4–8G 内存 |
| 0.4 / 0.4 / 0.2 | BlueStore 缓存 KV / 元数据 / 数据比例 |
| 64–128 PGs | CephFS Metadata Pool 建议 PG 数 |
| >5MB | AWS CLI 自动分片上传阈值 |
| 每日 / 每周 | Light Scrub / Deep Scrub 频率 |
| 7 天日 + 4 周周 | CephFS 快照保留策略 |
| 3-2-1 | 备份黄金准则（3 副本、2 介质、1 异地） |
| 2842 / 5336 / 5342 | Grafana Cluster / OSD / Pools 推荐面板 ID |
| 100TB → 375TB | 案例题：100/0.8×3；≈32 块 12TB 盘，4 节点 × 8 盘 |
| 9283 / 9100 端口 Down | 先查防火墙放行 |

## 附录 B：易错点 / 易混淆概念对照

1. **UP/DOWN vs IN/OUT**：UP/DOWN 看**进程与网络**；IN/OUT 看**是否承载数据**。UP+OUT 是扩缩容维护态；DOWN+IN 最紧急（数据还挂在它身上但访问不到）；DOWN+OUT 才是确定性损坏。
2. **副本 vs 备份**：副本是实时同步、同集群，**误删会被同步删掉**；备份是时间点隔离、可异地、可回溯历史版本。
3. **快照 vs 克隆**：快照是**只读**时间点；克隆是从快照创建的**可写**新镜像；都基于 COW，创建秒级、初始零空间。克隆前必须先 `snap protect`；有克隆存在时 `unprotect` 会报错，需先 `flatten`。
4. **RBD 回滚风险**：`rbd snap rollback` 会**永久丢失快照之后的所有新数据**，且回滚前必须 `rbd unmap`。CephFS 快照恢复是 `cp` 单文件出来，**删除用 `rmdir` 而不是 rm -rf**。
5. **MDS 只服务 CephFS**：RBD 和 RGW 都不需要 MDS；反过来说，**CephFS 用户授权必须额外配 MDS Caps**，RBD 不用。
6. **MON vs MGR 版本记忆点**：MGR 自 **Luminous** 引入（分担 MON）、**Nautilus** 后必需；MON 用 **Paxos** 维持 Quorum（>N/2），必须**奇数**个。
7. **Public vs Cluster 网络**：Public 走客户端流量（Client↔OSD/MON/RGW）；Cluster 走 OSD↔OSD 的同步/恢复流量。混用后果：恢复流量吃满带宽导致前端业务 IO 挂起。
8. **Light vs Deep Scrub**：Light 每日、只扫元数据/Checksum；Deep 每周、逐字节比对，才能发现 Bit rot（寂静损坏）。
9. **Degraded vs Inconsistent vs Stale**：Degraded = 副本**数量**不够；Inconsistent = 副本**内容**不一致（用 `pg repair`，注意以主副本覆盖、主副本坏会"劣币驱逐良币"）；Stale = 心跳超时状态过期（查 MON Quorum）。
10. **Misplaced ≠ 数据丢失**：重平衡中被标记 Misplaced 的对象仍可正常读写，只是位置不符合新 CRUSH Map。
11. **容量水位两组数字别混**：**85%/90%/95%** 是 nearfull/backfillfull/full 的官方默认阈值（95% 拒写只读）；**0.8 系数（80%）、70%/80% 红线、65–70% 扩容时机**是规划与运维红线建议；第13节 K8s 页出现的 "Nearfull 75% / Full 85%" 属该节的运维建议表述——答题时按题目问的是"默认阈值"还是"规划建议"区分。
12. **第1节与第3节副本容错的表述**：第3节明确 **Size=3 允许同时坏 2 块盘、可用率 33%**（即 1/3 而不是 2/3）；第1节"3+ 副本"页的表述以第3节机制为准。
13. **RGW 两个池的存储策略不同**：`.rgw.buckets.data`（对象数据）可用 HDD/纠删码降成本；`.rgw.buckets.index`（桶索引，存 OMAP/RocksDB）**必须三副本 + SSD**。
14. **RBD vs CephFS 在 K8s 中**：RBD = RWO 单节点（无分布式锁）；CephFS = RWX 多节点共享（MDS 统一文件树）。数据库选 RBD，共享静态资源/日志选 CephFS；**经验法则"能用 RBD 就不用 CephFS"**。
15. **内核客户端 vs FUSE**：内核性能高但受内核版本限制；FUSE 兼容性广、随 Ceph 版本更新、性能稍低（用户态上下文切换）。
16. **immediate vs WaitForFirstConsumer**：默认 Immediate 提前在随机 AZ 建 PV，易 VolumeZoneConflict；拓扑感知必须用 WaitForFirstConsumer（等 Pod 调度后再建 PV）。
17. **BlueStore DB 溢出**：DB 分区（RocksDB）存满会**溢出到 HDD Data 盘导致性能暴跌**，不是报错停机；规划按 Data 的 4% 或 ≥30GB。
18. **磁盘禁用 RAID 5/6**：OSD 必须裸盘（1 盘 = 1 OSD 进程），冗余由 Ceph 软件层（副本/EC）负责，底层 RAID 严重影响恢复效率。
19. **ceph auth caps 是覆盖不是增量**：改权限实时生效（<1s），漏写的 Caps 立即失效；轮换密钥后必须同步更新 K8s Secret。
20. **reclaimPolicy Delete vs Retain**：Delete 在 PVC 删除时连底层 RBD 镜像一起删；核心业务生产环境务必 Retain。
21. **RGW AWS CLI**：必须显式 `--endpoint-url`；Bucket 名**集群内全局唯一**；403 优先查时间同步和 AK/SK。
22. **MON 移除顺序**：先增后删——新 MON 加入 Quorum 后才能移除旧 MON，否则 Quorum 可能丢失；MON 磁盘满会导致数据库无法写 WAL。
23. **时间同步**：>50ms 即可致 OSD 被标记 Down / 集群无法共识——部署前必须确认 chronyd 正常。
24. **主机名与 /etc/hosts 两个坑**：主机名只能小写字母/数字/中划线；本机主机名不能解析到 127.0.0.1（否则守护进程绑定回环，节点间无法通信）。
25. **RGW ssl_port=443s 的 "s"**：表示强制 SSL 握手，漏写则端口不加密；证书文件要 ceph:ceph 属主 + 600 权限。
26. **msgr1 vs msgr2 端口**：6789 = msgr1（明文）；3300 = msgr2（可 secure 全量 AES-GCM 加密）。
27. **配额非实时**：CephFS Quota 是客户端本地缓存 + 定期与 MDS 同步的异步机制，极快写入可能少量超额，属正常工程权衡。
28. **扩容并发参数方向别记反**：业务高峰要**调小** osd_max_backfills / osd_recovery_max_active（保业务），低峰**调大**（加速恢复）；客户端优先级 63 > 恢复优先级 3。
29. **CRUSH 权重按 TB**：1TB = 1.0，4TB = 4.0；`reweight-by-utilization 105` 表示把使用率偏差控制在 5% 内。
30. **rbd pool init 不能省**：创建 RBD 池后必须初始化，否则缺元数据对象。

## 附录 C：命令速查（按场景）

- **状态诊断**：`ceph -s`（巡检）/ `ceph -w`（实时流）/ `ceph health detail`（根因）/ `ceph df`、`ceph osd df`（容量与倾斜）/ `ceph osd tree`（拓扑）/ `ceph osd perf`（延迟）/ `ceph pg dump_stuck`（卡住的 PG）
- **OSD 操作**：`ceph osd out X` → 等 `safe-to-destroy` → `ceph osd purge X --yes-i-really-mean-it`；重启 `systemctl restart ceph-osd@X`；磁盘清理 `ceph orch device zap <node> <device> --force`；加盘 `ceph orch daemon add osd <node>:<device>`；均衡 `ceph osd reweight-by-utilization 105`
- **PG 修复**：`ceph pg deep-scrub <id>`（复验）→ `ceph pg repair <id>`（覆盖修复）；高级 `rados list-inconsistent-obj <id>`
- **RBD**：`rbd pool init` → `rbd create` → `rbd info` → `rbd map` → mkfs/mount；`rbd resize --size 20G`（在线扩容）；快照 `rbd snap create / rollback / protect / unprotect`；克隆 `rbd clone`、`rbd flatten`；状态 `rbd status`
- **CephFS**：`ceph fs set <fs> allow_new_snaps true`；快照 `mkdir .snap/snap_v1` / `cp` 恢复 / `rmdir` 删除；配额 `setfattr -n ceph.quota.max_bytes`、`getfattr -n ceph.dir.rbytes`；挂载验证 `mount -a`；systemd 挂载 `systemctl daemon-reload`
- **RGW**：`radosgw-admin user create`（拿 AK/SK）；`radosgw-admin sync status`（多站点）；S3 操作 `aws s3 mb/cp/ls --endpoint-url http://<IP>:7480`；版本控制 `aws s3api put-bucket-versioning`
- **MON 重建**：`ceph-mon --extract-monmap` → `monmaptool --rm` → `--inject-monmap` → 重启 ceph-mon
- **网络验收**：`ping -s 8972 -M do <IP>`（MTU 9000）、`iperf3 -c <IP> -t 10`（带宽）、`mtr --report <IP>`（丢包）
- **K8s CSI**：`kubectl describe pvc`（Pending 排查）、`kubectl edit pvc`（在线扩容）、`kubectl get pv` → `rbd ls`（资源关联）、`lsmod | grep rbd`（内核模块）
- **安全**：`ceph auth list`（审计）、`ceph auth get-or-create`、`ceph auth caps`（覆盖式修改）、`ceph auth change-key`（轮换）、`ceph -n client.X --keyring ... <cmd>`（模拟身份）
