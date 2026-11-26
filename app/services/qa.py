"""课程知识库问答：查询改写 → 检索 → 流式生成 → 引用核验。

## 一次问答的完整链路

```
用户提问
  │ ① 查询改写（可选，调一次 LLM）
  │    把口语问句改写成术语式检索式。实测口语 query 原始召回 92.9% / 80.0%，
  │    改写后两个语料都是 100%（见 docs/P3-RAG.md §1）。
  │    ★ 提示词里**必须带上该课的术语表**——LLM 并不知道课件用了哪些词，
  │      不给约束它改写出的词照样落不进词汇空间。
  │    失败/超时一律降级用原 query：**不因为改写失败就让整个问答不可用**。
  ▼
  │ ② 检索（本地 BM25，0.2 ms，不花钱）
  │    命中若干页 → **先推给前端**，用户不必等生成完就看到"我找到了哪几页"
  ▼
  │ ③ 流式生成（LLM）
  │    只给检索到的原文，每段带【文件名 第 N 页】标签；
  │    强制要求逐条标注来源，允许且鼓励回答"材料里没有"。
  ▼
  │ ④ 引用核验（本地术语比对 + 检索排名，0 秒，不花钱）
  │    对生成完的完整答案逐条核验——见 services/citation.py
  ▼
  完成
```

## 诚实性约束（与 P1/P2 同级，写进提示词与措辞）

1. 只在给定材料范围内回答，**鼓励说"课件里没有提到"**——复习场景里，
   编一个答案比说"没有"危害大得多；
2. 引用的核验结论用「待核」不用「错误」，并把被引页原文一并给出；
3. 不宣称"回答准确率 100%"——检索召回是检索侧指标，不等于答案正确。
"""
from __future__ import annotations

import json
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Iterator

from app.config import OUTPUT_ROOT, current_provider, get_settings
from app.services.agent_repair import token_cost
from app.services.ai_generator import build_client, _create_completion
from app.services.kb import KnowledgeBase
from app.services.retriever import Chunk

#: 问答回答的输出上限。复习答疑是短文本，不需要生成文档那种 32k 余量；
#: 设小一点还能绕开"部分服务商拒绝过大 max_tokens"的降级路径。
QA_MAX_TOKENS = 4096

#: 改写请求的输出上限（改写结果是一条检索式，很短）。
REWRITE_MAX_TOKENS = 256

#: 给改写模型的术语表条数上限。计组实测 807 个术语，
#: 全给约 3200 字符（≈2000 token），成本仍可忽略，但没必要无上限地堆。
MAX_TERMS_IN_PROMPT = 600

#: 每个命中片段塞进生成上下文的字符数上限。
#: 单页实测 p90 = 532 字符，取 800 基本能装下整页而不截断。
CONTEXT_CHARS_PER_CHUNK = 800

REWRITE_SYSTEM = (
    "你是检索式改写器。把用户的口语提问改写成适合在课件里做关键词检索的检索式。\n"
    "要求：\n"
    "1. 只输出改写后的检索式本身，不要解释、不要引号、不要 Markdown\n"
    "2. 用课件里可能出现的**技术术语**替换口语说法\n"
    "3. 优先使用下面给出的「本课术语表」里的词，但不限于它\n"
    "4. 保留原问题的关键限定（如具体的组件名、数字）\n"
    "5. 控制在 20 字以内\n"
    "6. **即使原问题里已经有一个关键词，也必须在它基础上补充相关的概念词**"
    "（如机制名、属性名、组成部分）；只有在想不出任何补充词时才原样返回\n"
    "\n"
    "示例：\n"
    "  cache 是怎么工作的 → Cache 高速缓冲存储器 命中率 地址映射 替换算法\n"
    "  DMA 是什么 → DMA 直接存储器访问 周期挪用 接口 传送过程\n"
    "  机器不够用想加机器 → 集群扩容 新增 OSD 节点\n"
    "  冗余是怎么做的 → 纠删码 副本 校验块 冗余度\n"
    "\n"
    "注意：这些示例只是格式示范，你要处理的是**下面给出的这门课**的术语。"
)

ANSWER_SYSTEM = (
    "你是课程复习答疑助手。只依据用户提供的课件原文片段回答问题。\n"
    "要求：\n"
    "1. **只使用给定片段里的信息**。片段里没有的内容，明确说「课件这部分没提到」，"
    "绝不编造——复习时被编造的答案误导，比没有答案严重得多\n"
    "2. **每一条论断后面必须标注来源**，格式严格为：（源：<文件名> 第 <页码> 页）\n"
    "3. 只能引用上面实际给出的片段，不得引用未提供的页码\n"
    "4. 如果多个片段拼起来才能回答，就分别标注各自的来源\n"
    "5. 用 Markdown 输出，简洁直接，不要客套话\n"
    "6. 不要生成思考题、开放讨论题"
)

#: **仅供验收 5 的对照实验使用**：与 `ANSWER_SYSTEM` 逐条对齐，唯一差别是去掉引用要求。
#:
#: 两条约束必须一起看，否则对照就不干净：
#: ① 诚实性约束（只使用给定片段、允许说"没提到"）**两臂完全一致**——
#:    如果把"不要编造"也去掉，测出来的差异就分不清是引用约束还是诚实约束带来的；
#: ② 显式要求"不要标注来源"——不写这一句，模型会习惯性地自己加引用，
#:    两臂就退化成同一条链路了。
#: 生产链路永远走 `ANSWER_SYSTEM`，这个常量只在脚本里被显式选中。
ANSWER_SYSTEM_NO_CITATION = (
    "你是课程复习答疑助手。只依据用户提供的课件原文片段回答问题。\n"
    "要求：\n"
    "1. **只使用给定片段里的信息**。片段里没有的内容，明确说「课件这部分没提到」，"
    "绝不编造——复习时被编造的答案误导，比没有答案严重得多\n"
    "2. 用 Markdown 输出，简洁直接，不要客套话\n"
    "3. **不要标注任何来源、文件名或页码**，直接给结论\n"
    "4. 不要生成思考题、开放讨论题"
)


@dataclass
class RewriteResult:
    original: str
    rewritten: str
    applied: bool
    note: str = ""
    cost_yuan: float = 0.0

    def to_dict(self) -> dict:
        return {
            "original": self.original,
            "rewritten": self.rewritten,
            "applied": self.applied,
            "note": self.note,
        }


# ------------------------------------------------------------------ ① 查询改写


def rewrite_query(
    question: str,
    kb: KnowledgeBase,
    enabled: bool = True,
) -> RewriteResult:
    """把口语提问改写成术语式检索式。任何失败都降级为原 query。

    **这个函数永远不抛异常**——它只是检索的一个增强环节，
    让它把整个问答拖垮是不划算的（原始口语 query 本身也有 80%~93% 的召回）。
    """
    if not enabled:
        return RewriteResult(question, question, applied=False, note="未开启改写")

    settings = get_settings()
    if not settings.ai_api_key:
        return RewriteResult(question, question, applied=False, note="未配置 API Key，已跳过改写")

    terms = sorted(kb.course_terms)[:MAX_TERMS_IN_PROMPT]
    term_hint = "、".join(terms) if terms else "（本课未提取到术语表）"

    try:
        provider = current_provider()
        client = build_client(settings)
        kwargs = {
            "model": settings.ai_model,
            "messages": [
                {"role": "system", "content": REWRITE_SYSTEM},
                {
                    "role": "user",
                    "content": f"本课术语表：{term_hint}\n\n用户提问：{question}",
                },
            ],
            "max_tokens": REWRITE_MAX_TOKENS,
            "temperature": 0,
        }
        if settings.ai_thinking_off:
            kwargs["extra_body"] = settings.ai_thinking_off

        response = _create_completion(client, kwargs)
        text = (response.choices[0].message.content or "").strip()
        # 模型偶尔会加引号/句号/换行，清掉再当检索式用
        text = text.strip("\"'“”‘’。. \n")
        text = text.splitlines()[0].strip() if text else ""

        if not text or text == question:
            return RewriteResult(question, question, applied=False, note="改写结果无变化，沿用原问题")

        usage = getattr(response, "usage", None)
        cost = 0.0
        if usage is not None:
            cost = token_cost(
                {
                    "prompt_tokens": int(getattr(usage, "prompt_tokens", 0) or 0),
                    "completion_tokens": int(getattr(usage, "completion_tokens", 0) or 0),
                },
                float(provider.get("price_in") or 0),
                float(provider.get("price_out") or 0),
            )
        return RewriteResult(question, text, applied=True, cost_yuan=cost)
    except Exception as exc:  # noqa: BLE001 改写失败绝不能拖垮问答
        return RewriteResult(
            question, question, applied=False, note=f"改写失败（{type(exc).__name__}），已用原问题检索"
        )


# ------------------------------------------------------------------ ② 检索 + ③ 生成


def _hit_payload(hits, excerpt_chars: int = 200) -> list[dict]:
    out = []
    for h in hits:
        out.append(
            {
                "file": h.chunk.file,
                "page": h.chunk.page,
                "title": h.chunk.title,
                "score": h.score,
                "rank": h.rank,
                "key": h.chunk.key,
                "excerpt": h.chunk.text[:excerpt_chars],
            }
        )
    return out


def _context_of(hits) -> str:
    """给模型的材料：每段都带【文件名 第 N 页】标签。

    标签**必须显眼**——它同时服务两件事：模型据此标来源，
    以及让模型清楚"你能引用的就这几页"（降低它编页码的动机）。
    """
    blocks = []
    for h in hits:
        text = h.chunk.text[:CONTEXT_CHARS_PER_CHUNK]
        blocks.append(f"【{h.chunk.file} 第 {h.chunk.page} 页】\n{text}")
    return "\n\n---\n\n".join(blocks)


def answer_stream(
    question: str,
    kb: KnowledgeBase,
    topk: int = 5,
    scope: str | None = None,
    use_rewrite: bool = True,
    require_citation: bool = True,
) -> Iterator[dict]:
    """产出问答的事件流（每个事件是一个 dict，由路由层编码成 SSE）。

    事件类型见模块 docstring 与 docs/P3-RAG.md §6。
    **先推 sources 再推答案**是刻意的：用户能立刻看到"找到了哪几页"，
    不必等生成完——这是流式体验里最容易做砸的一点。

    `require_citation=False` 只服务验收 5 的对照实验（见 `ANSWER_SYSTEM_NO_CITATION`），
    生产链路不要传它。
    """
    question = (question or "").strip()
    if not question:
        yield {"type": "error", "message": "问题不能为空"}
        return

    t_start = time.perf_counter()

    # ① 改写
    rw = rewrite_query(question, kb, enabled=use_rewrite)
    t_rewrite = time.perf_counter() - t_start
    yield {"type": "rewrite", **rw.to_dict()}

    yield from answer_from_rewrite(
        question,
        kb,
        rw,
        topk=topk,
        scope=scope,
        require_citation=require_citation,
        t_start=t_start,
        t_rewrite=t_rewrite,
    )


def answer_from_rewrite(
    question: str,
    kb: KnowledgeBase,
    rw: RewriteResult,
    *,
    topk: int = 5,
    scope: str | None = None,
    require_citation: bool = True,
    t_start: float | None = None,
    t_rewrite: float = 0.0,
) -> Iterator[dict]:
    """② 检索 → ③ 生成 → ④ 核验 → ⑤ 落盘，从「已完成的改写」接着跑。

    **为什么把改写拆到外面**：验收 5 的 A/B 对照必须让两臂用同一个改写结果。
    如果两臂各自改一次，差异里就混进了"改写的随机性 + 检索结果的差异"，
    测出来的就不是引用约束的作用了。
    """
    t_start = time.perf_counter() if t_start is None else t_start

    # ② 检索
    t_r0 = time.perf_counter()
    hits = kb.index.search(rw.rewritten, topk=topk, scope=scope)
    t_retrieve = time.perf_counter() - t_r0

    if not hits:
        # 空结果不是错误，但也不能硬生成——没有材料时模型只会编
        yield {
            "type": "sources",
            "hits": [],
            "note": "这份课件里没有检索到相关内容，换个说法或增加课件试试",
        }
        # 「问了但没找到」同样值得记：它说明这个问题在这门课的课件里没有落点，
        # 要么是问偏了，要么是课件本身缺这块内容。
        append_qa_log(
            kb.kb_id,
            {
                "ts": datetime.now().isoformat(timespec="seconds"),
                "question": question,
                "rewritten": rw.rewritten,
                "rewrite_applied": rw.applied,
                # 课程名随记录落盘：**归组只能靠它**。靠文件名不行——同一门课的
                # 课件常统一叫 `讲义.pptx`（实际数据里 3 批各 4–5 份全叫这个）。
                "course_name": kb.course_name,
                "files": kb.files,
                "hits": [],
                "answer": "",
                "citations": {"total": 0, "supported": 0, "weak": 0, "not_found": 0},
                "cost_yuan": round(rw.cost_yuan, 6),
                "elapsed_s": round(time.perf_counter() - t_start, 1),
                "timings": _timings(t_rewrite, t_retrieve, None, t_start),
                "no_hit": True,
            },
        )
        yield {
            "type": "done",
            "usage": None,
            "cost_yuan": rw.cost_yuan,
            "empty": True,
            "timings": _timings(t_rewrite, t_retrieve, None, t_start),
        }
        return
    yield {
        "type": "sources",
        "hits": _hit_payload(hits),
        "note": "",
        "timings": {
            "rewrite_ms": round(t_rewrite * 1000, 1),
            "retrieve_ms": round(t_retrieve * 1000, 1),
        },
    }

    # ③ 流式生成
    settings = get_settings()
    if not settings.ai_api_key:
        yield {"type": "error", "message": "未配置 API Key：请在设置页填写后再试"}
        return

    provider = current_provider()
    system_prompt = ANSWER_SYSTEM if require_citation else ANSWER_SYSTEM_NO_CITATION
    kwargs = {
        "model": settings.ai_model,
        "messages": [
            {"role": "system", "content": system_prompt},
            {
                "role": "user",
                "content": f"课件原文片段：\n\n{_context_of(hits)}\n\n问题：{question}",
            },
        ],
        "max_tokens": QA_MAX_TOKENS,
        "stream": True,
    }
    if settings.ai_thinking_off:
        kwargs["extra_body"] = settings.ai_thinking_off

    answer_parts: list[str] = []
    usage: dict | None = None
    t_first_token: float | None = None
    try:
        client = build_client(settings)
        stream = client.chat.completions.create(**kwargs)
        for chunk in stream:
            if not chunk.choices:
                continue
            delta = chunk.choices[0].delta
            piece = getattr(delta, "content", None)
            if piece:
                if t_first_token is None:
                    t_first_token = time.perf_counter() - t_start
                answer_parts.append(piece)
                yield {"type": "delta", "text": piece}
            # 部分服务商在最后一个 chunk 里回传 usage（需 stream_options）
            chunk_usage = getattr(chunk, "usage", None)
            if chunk_usage is not None:
                usage = {
                    "prompt_tokens": int(getattr(chunk_usage, "prompt_tokens", 0) or 0),
                    "completion_tokens": int(getattr(chunk_usage, "completion_tokens", 0) or 0),
                }
    except Exception as exc:  # noqa: BLE001 流式已开始，只能以事件形式报告
        yield {"type": "error", "message": f"生成失败：{type(exc).__name__}: {exc}"}
        return

    answer = "".join(answer_parts)

    # ④ 引用核验（本地、0 秒、不花钱）
    checks = kb.verifier.verify(answer)
    summary = _summary(checks)
    yield {
        "type": "citations",
        "checks": [c.to_dict() for c in checks],
        "summary": summary,
    }

    cost = rw.cost_yuan
    if usage:
        cost += token_cost(
            usage,
            float(provider.get("price_in") or 0),
            float(provider.get("price_out") or 0),
        )

    # ⑤ 落盘。用户要能回看"上次问过什么、怎么答的"；
    #    复习场景下这份历史还等于**薄弱点清单**（问得多的地方就是不会的地方）。
    append_qa_log(
        kb.kb_id,
        {
            "ts": datetime.now().isoformat(timespec="seconds"),
            "question": question,
            "rewritten": rw.rewritten,
            "rewrite_applied": rw.applied,
            "course_name": kb.course_name,
            "files": kb.files,
            "hits": [
                {"file": h.chunk.file, "page": h.chunk.page, "rank": h.rank} for h in hits
            ],
            "answer": answer,
            "citations": {
                k: int(summary.get(k, 0)) for k in ("total", "supported", "weak", "not_found")
            },
            "cost_yuan": round(cost, 6),
            "elapsed_s": round(time.perf_counter() - t_start, 1),
            # 埋点（P4）：分项耗时与 token。只记"总耗时"的话，
            # 事后无法回答"慢在改写还是慢在生成"——这正是 §22 那个问题的来源。
            "timings": _timings(t_rewrite, t_retrieve, t_first_token, t_start),
            "tokens": usage or {},
        },
    )

    yield {
        "type": "done",
        "usage": usage,
        "cost_yuan": round(cost, 6),
        "empty": False,
        "timings": _timings(t_rewrite, t_retrieve, t_first_token, t_start),
    }


def _timings(
    t_rewrite: float, t_retrieve: float, t_first_token: float | None, t_start: float
) -> dict:
    """分项耗时（毫秒）。**首 token 从请求开始算**——用户感受到的等待就是这一段，
    单独报"生成耗时"会把改写那一跳藏起来（P3 的 4.9s 主要就藏在那一跳里）。"""
    total = time.perf_counter() - t_start
    return {
        "rewrite_ms": round(t_rewrite * 1000, 1),
        "retrieve_ms": round(t_retrieve * 1000, 1),
        "first_token_ms": None if t_first_token is None else round(t_first_token * 1000, 1),
        "total_ms": round(total * 1000, 1),
    }


def _summary(checks) -> dict:
    """给前端的一句话汇总。刻意**不做"总体可信度百分比"**——
    把若干条独立判断平均成一个数会掩盖"其中有一条是编的"，而那恰恰最要紧。"""
    from app.services.citation import summarize

    return summarize(checks)


# ------------------------------------------------------------------ 问答历史（落盘）


#: 问答历史目录。**刻意与 `output/kb/` 分开**：知识库缓存有 72 小时 TTL
#: （见 `kb.KB_TTL_HOURS`），而"问过什么"是用户想长期留着的东西——
#: 复习场景下它等于一份**薄弱点清单**（你问过的地方就是你不会的地方），
#: 跟着缓存一起被清掉就没意义了。
QA_DIR: Path = OUTPUT_ROOT / "qa"


def qa_log_path(kb_id: str) -> Path:
    """一门课一个文件（按 kb_id 分）。

    不合并成全局一个文件，是为了避免每次读写整份历史；
    也不按"课程名"分，是因为课程名是用户可选的、可能为空或重复。
    """
    return QA_DIR / f"{kb_id}.jsonl"


def append_qa_log(kb_id: str, record: dict) -> None:
    """追加一条问答记录。

    **绝不抛异常**：写历史失败不该让用户已经拿到的答案变成一次失败。
    jsonl 追加而不是读写整个 JSON —— 历史会越来越长，全量重写没必要。
    """
    try:
        QA_DIR.mkdir(parents=True, exist_ok=True)
        with qa_log_path(kb_id).open("a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
    except OSError:
        pass


def load_qa_log(kb_id: str, limit: int = 50) -> list[dict]:
    """读回最近的问答记录（**倒序，最新在前**）。

    坏行跳过而不是整份丢掉——手工编辑或写一半断电都可能产生坏行，
    为一行丢掉全部历史不划算。
    """
    path = qa_log_path(kb_id)
    if not path.exists():
        return []
    records: list[dict] = []
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                records.append(json.loads(line))
            except json.JSONDecodeError:
                continue  # 单行损坏不影响其余
    except OSError:
        return []
    return list(reversed(records[-limit:]))


def encode_sse(event: dict) -> str:
    """把事件编码成 SSE 帧。

    `ensure_ascii=False` 必须开：中文被转成 \\uXXXX 后体积翻数倍，
    而且前端 `EventSource` 拿到的仍是可读文本——没有理由转义。
    """
    return f"data: {json.dumps(event, ensure_ascii=False)}\n\n"
