"""P3 探针：在真实课件上测量「分块规模 / 检索延迟 / 检索召回」。

## 为什么先写这个

P3 设计稿里最大的一个待定项是「检索层到底做成什么样」——
规划里写着"先 numpy 暴力余弦，再引入向量库做对比"，但那句的前提是**先有 embedding 向量**，
而本项目四家 provider 全是 chat 模型，**没有任何 embedding 配置**。

所以真正要先回答的不是"用不用向量库"，而是：
  **这个语料 + 这个规模，到底需不需要神经 embedding？**

本项目一贯的哲学是「能免费解析出来的，就不要花钱交给模型」。
如果零依赖的关键词检索（BM25 / 术语级）在这个语料上 Recall 已经够高，
那引入 embedding（无论是 API 还是本地模型）就是**过度设计**——
这和 P2 里"没用 LangGraph，因为循环只有三个状态"是同一种判断。

本脚本只测量、不下结论。数据出来后再定设计稿。

## 测什么

1. **规模**：14 份课件能切成多少 chunk、长度分布如何
2. **延迟**：暴力打分（纯 Python）在真实规模下的耗时 → 回答"需不需要向量库的 ANN 索引"
3. **召回 A（术语 query）**：从课件里抽术语当 query，ground truth = 含该术语的页
   —— 自动化、样本量大，但**对关键词检索天然有利**，读数会偏乐观
4. **召回 B（口语 query）**：人工构造"学生会怎么问"，ground truth = 对应课件文件
   —— 样本小，但更接近真实使用场景，能暴露关键词检索的短板

两类一起看才有意义：只报 A 会高估，只报 B 样本不够。

用法：
    .venv/Scripts/python.exe scripts/p3_probe.py
"""
from __future__ import annotations

import json
import math
import re
import sys
import time
from collections import Counter, defaultdict
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.services.coverage import extract_en_terms, mine_cn_terms  # noqa: E402
from app.services.extractor import SUPPORTED_EXTENSIONS, extract_any  # noqa: E402

DEFAULT_COURSE_DIR = ROOT / "课件"
_arg = next((a for a in sys.argv[1:] if not a.startswith("--")), "")
# 语料目录可由命令行指定——换个目录就能测另一门课：
#     .venv/Scripts/python.exe scripts/p3_probe.py 计组/
COURSE_DIR = Path(_arg) if _arg else DEFAULT_COURSE_DIR
if not COURSE_DIR.is_absolute():
    COURSE_DIR = ROOT / COURSE_DIR
OUT_JSON = ROOT / "output" / f"_p3_probe_{COURSE_DIR.name}.json"

# ------------------------------------------------------------------ 分块


def build_chunks() -> list[dict]:
    """按页分块：一份 PPT 的一页 = 一个 chunk。

    粒度选择的依据：**页码就是引用溯源的最小单位**（P3 的核心卖点要给出"源课件第 N 页"），
    所以 chunk 必须与页对齐，不能按固定字数硬切——硬切会让一页的内容跨两个 chunk，
    引用时就只能给出"第 N 页附近"这种模糊说法。

    代价是 chunk 长度不均（有的页只有标题，有的页塞满表格），这对 BM25 有影响（b 参数就是在补偿它）。
    本探针会输出长度分布，供判断是否需要二次切分。
    """
    chunks: list[dict] = []
    # 用 extractor 的 SUPPORTED_EXTENSIONS 而不是写死 "*.pptx"——
    # 第一版写死了 pptx，换到计组那批 PDF 时**一份都没读到**，
    # 表现是"chunk 数 0"，很容易被误当成语料有问题。
    paths = sorted(
        p for p in COURSE_DIR.iterdir() if p.suffix.lower() in SUPPORTED_EXTENSIONS
    )
    for path in paths:
        result = extract_any(path)
        for slide in result.slides:
            text = (slide.text + "\n" + slide.notes).strip()
            if not text:
                continue
            chunks.append(
                {
                    "id": len(chunks),
                    "file": path.name,
                    "page": slide.page,
                    "text": text,
                }
            )
    return chunks


# ------------------------------------------------------------------ 分词


_TOKEN_EN = re.compile(r"[A-Za-z][A-Za-z0-9_.\-]*")
_TOKEN_CN = re.compile(r"[\u4e00-\u9fff]+")
# 数字 + 单位/阈值。**复制自 coverage.py 的 _NUM_UNIT**（那边是私有名，不适合跨模块 import；
# 若 P3 落地时两处都要用，应把它提升为公开常量共用一份定义，避免正则漂移）。
#
# 为什么必须有它：第一版探针漏掉了这一类，中文召回被拉到 52.5%。
# 调试后发现 59 条「非英文」query 里只有 17 条是纯中文，其余 42 条是 `3副本` / `0.85` / `64KB`
# 这类数字术语——英文正则要求字母开头、中文正则要求汉字，两边都不匹配 → **token 为空** → 必然检索失败。
# 而课件里数字阈值恰恰是最高频考点（副本数、容量阈值、超时秒数），漏掉它等于漏掉一整个类别。
_NUM_UNIT = re.compile(
    r"\d+\.\d+"
    r"|\d+(?:\.\d+)?\s*(?:%|KB|MB|GB|TB|PB|ms|us|ns|GiB|MiB|TiB|"
    r"字节|副本|小时|分钟|秒|倍|个|块|页|节点|核|GB/s|MB/s)"
)
_WS = re.compile(r"\s+")


def tokenize(text: str, terms: set[str] | None = None) -> list[str]:
    """零依赖分词：英文按词 + 数字阈值整串 + 中文 2-gram，可选再叠加术语整词。

    三层各补一类语料（顺序即优先级，后写的权重由 IDF 自行决定，不手工加权）：
      1. `_TOKEN_EN`   —— 技术术语（CRUSH / pg_num / rados）
      2. `_NUM_UNIT`   —— 数字与单位（3副本 / 64KB / 0.85）。**缺了它中文类召回直接腰斩**
      3. 中文 2-gram   —— 兜底，保证任何中文串都能被检索
      4. `terms` 整词  —— 可选的术语级 token（复用 coverage.mine_cn_terms）

    不引 jieba 的理由与 coverage.py 一致（词典 ~10MB、与"本地小工具"定位不符）。
    """
    tokens = [t.lower() for t in _TOKEN_EN.findall(text)]
    tokens += [_WS.sub("", m).lower() for m in _NUM_UNIT.findall(text)]
    for run in _TOKEN_CN.findall(text):
        if len(run) == 1:
            tokens.append(run)
        else:
            tokens.extend(run[i : i + 2] for i in range(len(run) - 1))
    if terms:
        flat = _WS.sub("", text).lower()
        tokens.extend(t for t in terms if t in flat)
    return tokens


def build_global_terms(chunks: list[dict]) -> set[str]:
    """全语料的术语表（供 hybrid 分词的查询侧使用）。

    与建索引侧用同一套逻辑：按份课件 mine_cn_terms（3–4 字、频次 ≥3），再取并集。
    查询是一条短句，无法自己挖频次，所以只能借全局表来识别 query 里的术语。
    """
    terms: set[str] = set()
    for fname in {c["file"] for c in chunks}:
        same_file = [c for c in chunks if c["file"] == fname]
        terms |= mine_cn_terms(" ".join(c["text"] for c in same_file))
    return terms


# ------------------------------------------------------------------ BM25


class BM25:
    """标准 BM25（k1/b 用文献默认值），纯 Python，零依赖。"""

    def __init__(
        self,
        docs_tokens: list[list[str]],
        terms: set[str] | None = None,
        k1: float = 1.5,
        b: float = 0.75,
    ):
        self.k1, self.b = k1, b
        self.terms = terms
        self.n_docs = len(docs_tokens)
        self.doc_len = [len(t) for t in docs_tokens]
        self.avgdl = sum(self.doc_len) / self.n_docs if self.n_docs else 0.0
        self.tf = [Counter(t) for t in docs_tokens]
        df: Counter[str] = Counter()
        for toks in docs_tokens:
            for w in set(toks):
                df[w] += 1
        self.idf = {
            w: math.log(1 + (self.n_docs - d + 0.5) / (d + 0.5)) for w, d in df.items()
        }

    def search(self, query: str, topk: int = 5) -> list[tuple[int, float]]:
        scores: dict[int, float] = defaultdict(float)
        for w in tokenize(query, self.terms):
            idf = self.idf.get(w)
            if idf is None:
                continue
            for i, tf in enumerate(self.tf):
                f = tf.get(w)
                if not f:
                    continue
                denom = f + self.k1 * (1 - self.b + self.b * self.doc_len[i] / self.avgdl)
                scores[i] += idf * f * (self.k1 + 1) / denom
        return sorted(scores.items(), key=lambda kv: -kv[1])[:topk]


# ------------------------------------------------------------------ 评估


def _qtype(q: str) -> str:
    if re.search(r"[A-Za-z]", q):
        return "en"
    if re.search(r"\d", q):
        return "num"
    return "cn"


_Q_LABEL = {"en": "英文术语", "num": "数字/单位", "cn": "纯中文"}


def eval_term_queries(
    chunks: list[dict], engine: BM25, topk: int = 5, per_type: int = 200
) -> dict:
    """评估 A：术语 query → 页级命中。

    ground truth = 所有**含该术语**的 chunk（一个术语可能跨多页，命中任意一个即算 hit）。
    只取「首次出现页」当唯一答案是错的——那会把检索正确但没排第一的情况误判为失败。

    **必须按 query 类型分层采样。**第一版探针直接 `unique_terms[:600]` 并按长度排序，
    结果 600 条里 541 条是短英文术语、中文只占 59 条——中文的低分被平均掉了，
    差点把一个真实缺陷（数字类查询无 token）掩盖过去。分层后每类样本量相当，
    哪一类弱就一目了然。
    """
    term_to_chunks: dict[str, set[int]] = defaultdict(set)
    for c in chunks:
        for t in extract_en_terms(c["text"]):
            term_to_chunks[t].add(c["id"])
    for fname in {c["file"] for c in chunks}:
        same_file = [c for c in chunks if c["file"] == fname]
        cn_terms = mine_cn_terms(" ".join(c["text"] for c in same_file))
        for t in cn_terms:
            for c in same_file:
                if t in _WS.sub("", c["text"]):
                    term_to_chunks[t].add(c["id"])

    # 只保留"有区分度"的术语：恰好出现在 1 个 chunk 的，最能测准定位能力
    unique_terms = sorted(
        (t for t, ids in term_to_chunks.items() if len(ids) == 1), key=lambda t: (len(t), t)
    )
    buckets: dict[str, list[str]] = {"en": [], "num": [], "cn": []}
    for t in unique_terms:
        buckets[_qtype(t)].append(t)
    queries = [q for k in ("en", "num", "cn") for q in buckets[k][:per_type]]

    stats = {k: [0, 0] for k in ("en", "num", "cn")}  # [hits, total]
    latencies: list[float] = []
    for q in queries:
        truth = term_to_chunks[q]
        t0 = time.perf_counter()
        got = engine.search(q, topk=topk)
        latencies.append((time.perf_counter() - t0) * 1000)
        hit = 1 if (set(i for i, _ in got) & truth) else 0
        s = stats[_qtype(q)]
        s[0] += hit
        s[1] += 1

    total_hits = sum(v[0] for v in stats.values())
    total_n = sum(v[1] for v in stats.values())
    latencies.sort()
    return {
        "n_queries": total_n,
        "recall_at_k": total_hits / total_n if total_n else 0.0,
        "by_type": {
            _Q_LABEL[k]: {
                "n": v[1],
                "recall": v[0] / v[1] if v[1] else 0.0,
                "pool": len(buckets[k]),
            }
            for k, v in stats.items()
        },
        "latency_ms_p50": round(latencies[len(latencies) // 2], 2) if latencies else 0.0,
        "latency_ms_p95": (
            round(latencies[int(len(latencies) * 0.95)], 2) if latencies else 0.0
        ),
    }


# 评估 B：口语 query（人工构造）—— ground truth 是「应该命中哪份课件」。
#
# 口语问句**没法自动生成**（它要模拟"学生不懂术语时会怎么问"），只能人工写。
# 所以按语料目录分集：换一门课就要补一套，**补不出就跳过口语评估、只报术语评估**——
# 这不是缺陷，而是这条评估本身的边界：它测的是"当人说得不像课本时检索还灵不灵"，
# 而这取决于该领域的人会怎么说，没有通用答案。
COLLOQUIAL_SETS: dict[str, list[tuple[str, str]]] = {
    "课件": [
        ("集群满了怎么加机器", "Ceph 集群扩容与缩容.pptx"),
        ("OSD 挂了怎么排查", "Ceph 常见故障排查：OSD 故障.pptx"),
        ("纠删码是什么东西", "Ceph 分布式存储实战：分布式存储概述与 Ceph 架构.pptx"),
        ("怎么装 cephadm", "Ceph 分布式存储实战：cephadm 部署全攻略.pptx"),
        ("块存储怎么配置", "Ceph 块存储（RBD）配置与使用.pptx"),
        ("对象存储网关怎么搭", "Ceph 对象存储（RGW）配置与使用.pptx"),
        ("文件系统怎么挂载", "Ceph 文件系统（CephFS）配置与使用.pptx"),
        ("数据丢了怎么恢复", "Ceph 数据备份与恢复.pptx"),
        ("集群怎么监控", "Ceph 集群监控与告警.pptx"),
        ("性能怎么调优", "Ceph 性能调优基础.pptx"),
        ("和 k8s 怎么对接", "Ceph 与 Kubernetes 集成 (CSI).pptx"),
        ("安全怎么加固", "第14节：Ceph 集群安全配置.pptx"),
        ("硬件要买什么样的", "Ceph 分布式存储实战：集群规划与硬件要求.pptx"),
        ("crush 算法是干嘛的", "Ceph 分布式存储实战：Ceph 核心组件深度解析.pptx"),
    ],
    "计组": [
        ("cache 是怎么工作的", "4.3 高速缓冲存储器.pdf"),
        ("中断是怎么处理的", "5.5 程序中断方式.pdf"),
        ("DMA 是什么", "5.6 DMA方式.pdf"),
        ("浮点数怎么表示", "6.2 数的定点表示和浮点表示.pdf"),
        ("有哪些寻址方式", "7.3 寻址方式.pdf"),
        ("流水线有什么用", "8.3 指令流水.pdf"),
        ("总线怎么控制", "3.5总线控制.pdf"),
        ("微程序控制是什么", "10.2 微程序设计.pdf"),
        ("补码怎么表示负数", "6.1 无符号数和有符号数.pdf"),
        ("指令周期分几步", "8.2 指令周期.pdf"),
        ("RISC 和 CISC 有什么区别", "7.5 RISC技术.pdf"),
        ("磁盘怎么存数据", "4.4 辅助存储器.pdf"),
        ("IO 接口是干嘛的", "5.3 IO接口.pdf"),
        ("组合逻辑控制器怎么实现", "10.1 组合逻辑控制.pdf"),
        ("CPU 由哪些部分组成", "8.1 CPU的结构.pdf"),
    ],
}

# 口语 query 的「改写版」：把日常说法换成课件里可能用的技术词。
# 这是评估「查询改写」这一环的**上限**——真实 LLM 改写不会知道课件用了哪些词，
# 所以落地时必须把「该课的术语表」作为约束一起喂给改写模型，否则改写结果同样落不进词汇空间。
REWRITTEN_SETS: dict[str, dict[str, str]] = {
    "课件": {
        "集群满了怎么加机器": "集群扩容 新增 OSD 节点 规模",
        "OSD 挂了怎么排查": "OSD 故障 排查 恢复 下线",
        "纠删码是什么东西": "纠删码 erasure coding 原理 编码",
        "怎么装 cephadm": "cephadm 部署 安装 引导 bootstrap",
        "块存储怎么配置": "RBD 块存储 创建 映像 image",
        "对象存储网关怎么搭": "RGW 对象存储 网关 配置",
        "文件系统怎么挂载": "CephFS 文件系统 挂载 元数据",
        "数据丢了怎么恢复": "数据恢复 备份 快照 回滚",
        "集群怎么监控": "监控 告警 仪表盘 指标",
        "性能怎么调优": "性能调优 参数 优化 吞吐",
        "和 k8s 怎么对接": "Kubernetes CSI 集成 插件",
        "安全怎么加固": "安全 加密 认证 加固 密钥",
        "硬件要买什么样的": "硬件 集群规划 要求 配置",
        "crush 算法是干嘛的": "CRUSH 算法 数据分布 映射",
    },
    "计组": {
        "cache 是怎么工作的": "高速缓冲存储器 cache 命中率 映射方式",
        "中断是怎么处理的": "程序中断 中断服务 中断处理 流程",
        "DMA 是什么": "DMA 直接存储器访问 周期挪用 控制器",
        "浮点数怎么表示": "浮点表示 阶码 尾数 规格化",
        "有哪些寻址方式": "寻址方式 立即寻址 直接寻址 间接寻址",
        "流水线有什么用": "指令流水 流水线 加速比 相关",
        "总线怎么控制": "总线控制 仲裁 链式查询 计数器定时查询",
        "微程序控制是什么": "微程序设计 微指令 微命令 控制存储器",
        "补码怎么表示负数": "补码 原码 反码 符号位 表示范围",
        "指令周期分几步": "指令周期 取指 间址 执行 中断",
        "RISC 和 CISC 有什么区别": "RISC 精简指令 CISC 复杂指令 对比",
        "磁盘怎么存数据": "辅助存储器 磁盘 磁道 扇区 寻址",
        "IO 接口是干嘛的": "IO 接口 端口 地址 数据传送",
        "组合逻辑控制器怎么实现": "组合逻辑控制 控制单元 译码 实现",
        "CPU 由哪些部分组成": "CPU 结构 运算器 控制器 寄存器",
    },
}

COLLOQUIAL: list[tuple[str, str]] = COLLOQUIAL_SETS.get(COURSE_DIR.name, [])
COLLOQUIAL_REWRITTEN: dict[str, str] = REWRITTEN_SETS.get(COURSE_DIR.name, {})


def eval_colloquial(
    chunks: list[dict], engine: BM25, topk: int = 5, rewritten: bool = False
) -> dict:
    """评估 B：口语 query → 文件级命中（top-K 里有该文件的页即算命中）。

    rewritten=True 时走「查询改写」版本——模拟 P3 规划里那个用 LLM 把口语问句
    转成术语式检索式的环节。**注意这个改写是我手工写的、且知道答案**，
    所以是**乐观上界**：真实 LLM 改写受限于它并不知道课件里用了哪些词
    （这正是"改写要拿课件术语表做引导"这条设计决策的来由）。
    """
    id2file = {c["id"]: c["file"] for c in chunks}
    if not COLLOQUIAL:
        # 这门课还没人工写口语问句集——如实跳过，不去编一个数字出来
        return {"n_queries": 0, "recall_at_k": 0.0, "misses": [], "skipped": True}
    hits = 0
    misses: list[dict] = []
    for q, expect in COLLOQUIAL:
        query = COLLOQUIAL_REWRITTEN.get(q, q) if rewritten else q
        got = engine.search(query, topk=topk)
        files = [id2file[i] for i, _ in got]
        ok = expect in files
        hits += ok
        if not ok:
            misses.append({"query": q, "expect": expect, "got": files})
    return {
        "n_queries": len(COLLOQUIAL),
        "recall_at_k": hits / len(COLLOQUIAL),
        "misses": misses,
    }


def main() -> None:
    print("=" * 72)
    print("P3 探针 · 真实课件上的检索规模 / 延迟 / 召回")
    print("=" * 72)

    t0 = time.perf_counter()
    chunks = build_chunks()
    extract_s = time.perf_counter() - t0

    n_files = len({c["file"] for c in chunks})
    lens = sorted(len(c["text"]) for c in chunks)
    print("\n[1] 规模")
    print(f"    课件 {n_files} 份 → chunk {len(chunks)} 个（按页切）")
    print(f"    提取总耗时 {extract_s:.1f}s")
    print(
        f"    chunk 字符数：min {lens[0]} / p50 {lens[len(lens)//2]} / "
        f"p90 {lens[int(len(lens)*0.9)]} / max {lens[-1]}"
    )
    print(f"    总字符 {sum(lens):,}")

    global_terms = build_global_terms(chunks)
    print(f"    全局术语表（复用 coverage.mine_cn_terms）：{len(global_terms)} 个")

    variants: list[tuple[str, set[str] | None]] = [
        ("A · 纯 2-gram", None),
        ("B · 2-gram + 术语整词", global_terms),
    ]

    report: dict = {
        "n_files": n_files,
        "n_chunks": len(chunks),
        "chars_total": sum(lens),
        "extract_seconds": round(extract_s, 2),
        "n_global_terms": len(global_terms),
        "variants": {},
    }

    for name, terms in variants:
        print("\n" + "-" * 72)
        print(f"档位 {name}")
        print("-" * 72)
        docs_tokens = [tokenize(c["text"], terms) for c in chunks]
        avg_tok = sum(len(t) for t in docs_tokens) / max(len(docs_tokens), 1)

        t0 = time.perf_counter()
        engine = BM25(docs_tokens, terms=terms)
        build_s = time.perf_counter() - t0
        print(
            f"[2] 索引 {build_s*1000:.0f} ms   词表 {len(engine.idf):,}   "
            f"平均 {avg_tok:.0f} token/chunk"
        )

        for _ in range(3):
            engine.search("Ceph 集群扩容", topk=5)
        lat: list[float] = []
        for _ in range(30):
            t = time.perf_counter()
            engine.search("纠删码 数据恢复", topk=5)
            lat.append((time.perf_counter() - t) * 1000)
        lat.sort()
        p50, p95 = lat[len(lat) // 2], lat[int(len(lat) * 0.95)]
        print(f"[3] 查询延迟（暴力全量打分，无 ANN 索引）p50 {p50:.2f} ms  p95 {p95:.2f} ms")

        res_a = eval_term_queries(chunks, engine)
        print(
            f"[4] 召回 A · 术语 query → 页级（n={res_a['n_queries']}）"
            f"  Recall@5 = {res_a['recall_at_k']*100:.1f}%"
        )
        for label, d in res_a["by_type"].items():
            print(f"      {label}  取 {d['n']}/{d['pool']} 条 → {d['recall']*100:.1f}%")

        res_b = eval_colloquial(chunks, engine)
        if res_b.get("skipped"):
            print("[5] 召回 B · 口语 query —— 本语料未提供口语问句集，跳过")
        else:
            res_b_rw = eval_colloquial(chunks, engine, rewritten=True)
            print(
                f"[5] 召回 B · 口语 query → 文件级（n={res_b['n_queries']}）"
                f"  原始 = {res_b['recall_at_k']*100:.1f}%"
                f"  →  改写后 = {res_b_rw['recall_at_k']*100:.1f}%"
            )
            for m in res_b["misses"]:
                top1 = m["got"][0][:28] if m["got"] else "—"
                print(f"      ✗ 「{m['query']}」 实得 top1 = {top1}…")
        res_b_rw = res_b_rw if not res_b.get("skipped") else {"recall_at_k": None}

        report["variants"][name] = {
            "bm25_build_ms": round(build_s * 1000),
            "vocab_size": len(engine.idf),
            "avg_tokens": round(avg_tok),
            "latency_ms": {"p50": round(p50, 2), "p95": round(p95, 2)},
            "recall_terms": res_a,
            "recall_colloquial": res_b,
            "recall_colloquial_rewritten": res_b_rw,
        }

    OUT_JSON.parent.mkdir(parents=True, exist_ok=True)
    OUT_JSON.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n原始数据已写入 {OUT_JSON.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
