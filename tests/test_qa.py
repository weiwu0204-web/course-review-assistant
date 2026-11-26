"""P3b 问答单测：事件序列 / 降级路径 / SSE 编码。

这里最要紧的不是"能跑通"，而是**每条失败路径都不炸**：
改写失败、检索无结果、没配 Key、生成中途报错——四种情况都必须
以事件形式如实报告，而不是抛异常把连接挂死。
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.services import qa
from app.services.citation import CitationVerifier
from app.services.kb import KnowledgeBase
from app.services.retriever import BM25Index, Chunk

# ------------------------------------------------------------------ 桩


CHUNKS = [
    Chunk(
        "存储.pptx::1",
        "存储.pptx",
        1,
        "纠删码",
        "纠删码 erasure coding 把数据切成条带，用校验块恢复数据，冗余度可配置。",
    ),
    Chunk(
        "存储.pptx::2",
        "存储.pptx",
        2,
        "副本",
        "默认 3 副本，pg_num 决定归置组数量，CRUSH 负责数据分布。",
    ),
    Chunk(
        "总线.pdf::1",
        "总线.pdf",
        1,
        "总线",
        "总线控制采用链式查询或计数器定时查询仲裁，决定哪个设备获得使用权。",
    ),
]


def make_kb() -> KnowledgeBase:
    index = BM25Index(CHUNKS, extra_terms=set())
    return KnowledgeBase(
        kb_id="testkb",
        files=["存储.pptx", "总线.pdf"],
        chunks=CHUNKS,
        index=index,
        verifier=CitationVerifier(CHUNKS, index=index, course_terms={}),
        course_terms={"纠删码", "副本", "总线"},
        n_pages=len(CHUNKS),
        n_chars=sum(len(c.text) for c in CHUNKS),
    )


def _chunk(text: str):
    return SimpleNamespace(
        choices=[SimpleNamespace(delta=SimpleNamespace(content=text))], usage=None
    )


class FakeCompletions:
    def __init__(self, pieces: list[str], rewrite: str, boom: bool = False):
        self.pieces, self.rewrite, self.boom = pieces, rewrite, boom

    def create(self, **kwargs):
        if self.boom:
            raise RuntimeError("模拟服务商 500")
        if kwargs.get("stream"):
            return iter([_chunk(p) for p in self.pieces])
        return SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content=self.rewrite))],
            usage=SimpleNamespace(prompt_tokens=10, completion_tokens=5),
        )


class FakeClient:
    def __init__(self, pieces=("答案",), rewrite="纠删码 原理", boom=False):
        self.chat = SimpleNamespace(
            completions=FakeCompletions(list(pieces), rewrite, boom=boom)
        )


class FakeSettings:
    ai_api_key = "fake-key"
    ai_model = "fake-model"
    ai_thinking_off: dict = {}
    ai_base_url = "https://example.invalid"


def wire(monkeypatch, client=None, key="fake-key"):
    """把 qa 模块里的配置与客户端替换成桩。"""
    settings = FakeSettings()
    settings.ai_api_key = key
    monkeypatch.setattr(qa, "get_settings", lambda: settings)
    monkeypatch.setattr(qa, "build_client", lambda s: client or FakeClient())
    monkeypatch.setattr(qa, "current_provider", lambda: {"price_in": 1.0, "price_out": 2.0})


#: 真实实现先捕获下来——autouse 的拦桩会把它换掉，测落盘的用例需要它。
_REAL_APPEND = qa.append_qa_log


@pytest.fixture(autouse=True)
def _no_real_qa_log(monkeypatch):
    """拦掉落盘：单测不该往真实的 output/qa/ 里写东西。

    用 autouse + 打桩而不是给 answer_stream 加开关参数——
    **生产代码不该为了测试多出一个参数**（那个参数还会出现在接口文档里）。
    需要测落盘本身的用例，自己把它换回来（见 TestQaLog）。
    """
    monkeypatch.setattr(qa, "append_qa_log", lambda *a, **k: None)


# ------------------------------------------------------------------ SSE 编码


class TestEncodeSse:
    def test_中文不被转义(self):
        """ensure_ascii=False 必须开：转义后体积翻几倍，且前端拿到的仍是可读文本。"""
        frame = qa.encode_sse({"type": "delta", "text": "纠删码"})
        assert "纠删码" in frame
        assert "\\u" not in frame

    def test_以双换行结尾(self):
        assert qa.encode_sse({"a": 1}).endswith("\n\n")

    def test_以data前缀开头(self):
        assert qa.encode_sse({"a": 1}).startswith("data: ")


# ------------------------------------------------------------------ 查询改写


class TestRewrite:
    def test_关闭时不调模型(self, monkeypatch):
        wire(monkeypatch, client=FakeClient(boom=True))
        result = qa.rewrite_query("随便问", make_kb(), enabled=False)
        assert result.applied is False
        assert result.rewritten == "随便问"

    def test_未配置key时降级(self, monkeypatch):
        wire(monkeypatch, key="")
        result = qa.rewrite_query("机器不够用想加机器", make_kb())
        assert result.applied is False
        assert result.rewritten == "机器不够用想加机器"
        assert "API Key" in result.note

    def test_成功时采用改写结果(self, monkeypatch):
        wire(monkeypatch, client=FakeClient(rewrite="集群扩容 新增 OSD 节点"))
        result = qa.rewrite_query("机器不够用想加机器", make_kb())
        assert result.applied is True
        assert result.rewritten == "集群扩容 新增 OSD 节点"

    def test_模型返回引号时清理(self, monkeypatch):
        wire(monkeypatch, client=FakeClient(rewrite='"纠删码 校验块"'))
        assert qa.rewrite_query("冗余是什么", make_kb()).rewritten == "纠删码 校验块"

    def test_模型多行时只取第一行(self, monkeypatch):
        wire(monkeypatch, client=FakeClient(rewrite="纠删码 冗余\n（解释：因为……）"))
        assert qa.rewrite_query("冗余是什么", make_kb()).rewritten == "纠删码 冗余"

    def test_失败时绝不抛异常(self, monkeypatch):
        """★ 改写只是检索的增强环节，绝不能让整个问答跟着挂掉。"""
        wire(monkeypatch, client=FakeClient(boom=True))
        result = qa.rewrite_query("怎么恢复数据", make_kb())
        assert result.applied is False
        assert result.rewritten == "怎么恢复数据"
        assert "失败" in result.note

    def test_改写结果与原问题相同时不算生效(self, monkeypatch):
        wire(monkeypatch, client=FakeClient(rewrite="怎么恢复数据"))
        assert qa.rewrite_query("怎么恢复数据", make_kb()).applied is False


# ------------------------------------------------------------------ 问答事件流


def types_of(events: list[dict]) -> list[str]:
    return [e["type"] for e in events]


class TestAnswerStream:
    def test_正常事件序列(self, monkeypatch):
        wire(monkeypatch, client=FakeClient(pieces=["纠删码", "把数据切成条带"]))
        events = list(qa.answer_stream("纠删码是什么", make_kb()))
        assert types_of(events) == ["rewrite", "sources", "delta", "delta", "citations", "done"]

    def test_sources先于delta(self, monkeypatch):
        """★ 刻意如此：用户能立刻看到"找到了哪几页"，不必等生成完。"""
        wire(monkeypatch, client=FakeClient())
        events = list(qa.answer_stream("纠删码是什么", make_kb()))
        assert types_of(events).index("sources") < types_of(events).index("delta")

    def test_sources带页码与片段(self, monkeypatch):
        wire(monkeypatch, client=FakeClient())
        events = list(qa.answer_stream("纠删码是什么", make_kb()))
        hits = next(e for e in events if e["type"] == "sources")["hits"]
        assert hits and all({"file", "page", "excerpt"} <= set(h) for h in hits)

    def test_delta拼接等于完整答案(self, monkeypatch):
        wire(monkeypatch, client=FakeClient(pieces=["纠删码", " 是", "冗余编码"]))
        events = list(qa.answer_stream("纠删码是什么", make_kb()))
        text = "".join(e["text"] for e in events if e["type"] == "delta")
        assert text == "纠删码 是冗余编码"

    def test_检索无结果时不调生成模型(self, monkeypatch):
        """★ 省钱的路径：没材料时硬生成只会让模型编。"""
        wire(monkeypatch, client=FakeClient(boom=True))  # 一旦调模型就会抛
        events = list(qa.answer_stream("zzz不存在的关键词zzz", make_kb()))
        assert types_of(events) == ["rewrite", "sources", "done"]
        assert events[1]["hits"] == []
        assert events[1]["note"]
        assert events[2]["empty"] is True

    def test_未配置key时给error事件(self, monkeypatch):
        wire(monkeypatch, key="")
        events = list(qa.answer_stream("纠删码是什么", make_kb()))
        assert "error" in types_of(events)
        assert "API Key" in next(e for e in events if e["type"] == "error")["message"]

    def test_生成中途失败给error事件而不抛(self, monkeypatch):
        """★ 流式已经开始，状态码改不了了，只能以事件报告。"""
        wire(monkeypatch, client=FakeClient(boom=True))
        events = list(qa.answer_stream("纠删码是什么", make_kb()))
        assert "error" in types_of(events)

    def test_空问题被拒(self, monkeypatch):
        wire(monkeypatch)
        events = list(qa.answer_stream("   ", make_kb()))
        assert types_of(events) == ["error"]

    def test_scope限定到单份课件(self, monkeypatch):
        # 关掉改写：改写桩固定返回"纠删码 原理"，会把检索带偏到别的课件上。
        # 这本身是个提醒——**改写会改变检索结果**，测检索相关行为时要显式控制它。
        wire(monkeypatch, client=FakeClient())
        events = list(
            qa.answer_stream("总线仲裁", make_kb(), scope="总线.pdf", use_rewrite=False)
        )
        hits = next(e for e in events if e["type"] == "sources")["hits"]
        assert hits and all(h["file"] == "总线.pdf" for h in hits)

    def test_改写生效时用的是改写后的检索式(self, monkeypatch):
        """反向确认耦合：改写结果确实参与了检索（不是白花钱调一次模型）。"""
        wire(monkeypatch, client=FakeClient(rewrite="纠删码 校验块"))
        events = list(qa.answer_stream("冗余是怎么做的", make_kb(), use_rewrite=True))
        hits = next(e for e in events if e["type"] == "sources")["hits"]
        assert any(h["file"] == "存储.pptx" and h["page"] == 1 for h in hits)

    def test_引用核验结果随流返回(self, monkeypatch):
        wire(monkeypatch, client=FakeClient(pieces=["纠删码是冗余编码（源：存储.pptx 第 1 页）"]))
        events = list(qa.answer_stream("纠删码是什么", make_kb()))
        cite = next(e for e in events if e["type"] == "citations")
        assert cite["checks"] and "verdict_label" in cite["checks"][0]
        assert "note" in cite["summary"]

    def test_没有引用时汇总如实说明(self, monkeypatch):
        wire(monkeypatch, client=FakeClient(pieces=["我不确定。"]))
        events = list(qa.answer_stream("纠删码是什么", make_kb()))
        cite = next(e for e in events if e["type"] == "citations")
        assert cite["checks"] == []
        assert "未引用" in cite["summary"]["note"]

    def test_done带成本(self, monkeypatch):
        wire(monkeypatch, client=FakeClient())
        events = list(qa.answer_stream("纠删码是什么", make_kb()))
        done = next(e for e in events if e["type"] == "done")
        assert "cost_yuan" in done


# ------------------------------------------------------------------ 提示词约束


class TestPromptConstraints:
    """提示词里的诚实性约束——它们是防幻觉的第一道闸，值得钉住。"""

    def test_系统提示词要求先声明材料范围(self):
        assert "只使用给定片段里的信息" in qa.ANSWER_SYSTEM
        assert "绝不编造" in qa.ANSWER_SYSTEM

    def test_系统提示词规定引用格式(self):
        assert "（源：" in qa.ANSWER_SYSTEM
        assert "第 <页码> 页" in qa.ANSWER_SYSTEM

    def test_系统提示词禁止引用未提供的页码(self):
        assert "不得引用未提供的页码" in qa.ANSWER_SYSTEM

    def test_改写提示词要求用术语表(self):
        assert "本课术语表" in qa.REWRITE_SYSTEM

    def test_上下文片段带页码标签(self, monkeypatch):
        """标签必须显眼——模型据此标来源，也据此知道"能引的就这几页"。"""
        ctx = qa._context_of(make_kb().index.search("纠删码", topk=1))
        assert "【" in ctx and "第 1 页】" in ctx


# ------------------------------------------------------------------ 问答落盘


class TestQaLog:
    """问答历史：JSONL 追加 / 坏行跳过 / 倒序读回 / 失败不抛。

    这份历史是"问过什么"的唯一落点，丢了就找不回来（知识库缓存会过期，
    但历史刻意不跟着过期）——所以它的健壮性值得单独钉住。
    """

    @pytest.fixture(autouse=True)
    def _real_log(self, monkeypatch, tmp_path):
        # 换回真实实现，并把落盘目录指向临时目录
        monkeypatch.setattr(qa, "append_qa_log", _REAL_APPEND)
        monkeypatch.setattr(qa, "QA_DIR", tmp_path)

    def test_追加后能读回(self):
        qa.append_qa_log("k1", {"question": "Q1", "answer": "A1"})
        records = qa.load_qa_log("k1")
        assert len(records) == 1
        assert records[0]["question"] == "Q1"

    def test_落盘记录带课程名(self, monkeypatch):
        """★ 归组唯一可靠的依据。

        历史记录的 `course_name` 必须落盘——没有它，这条记录就永远归不到
        任何课程下（只能进「未归类」）。文件名不能替代：同一门课的课件常统一叫
        `讲义.pptx`（真实数据里 3 个批次各 4–5 份全叫这个）。
        """
        wire(monkeypatch, client=FakeClient())
        kb = make_kb()
        kb.course_name = "计算机组成原理"
        list(qa.answer_stream("纠删码", kb))
        assert qa.load_qa_log("testkb")[0]["course_name"] == "计算机组成原理"

    def test_没检索到内容时也记课程名(self, monkeypatch):
        """「问了但没找到」同样要能归组——它也是这门课的学习痕迹。

        用纯英文随机串作 query：中文串会被 2-gram 切出"问题"这类常见词而误命中，
        构造无结果的用例必须避开它。
        **且必须关掉改写**——FakeClient 的改写桩固定返回"纠删码 原理"，
        开着改写就永远检索得到东西。
        """
        wire(monkeypatch, client=FakeClient())
        kb = make_kb()
        kb.course_name = "计算机组成原理"
        list(qa.answer_stream("zzzqqqxxxwww", kb, use_rewrite=False))
        rec = qa.load_qa_log("testkb")[0]
        assert rec["no_hit"] is True
        assert rec["course_name"] == "计算机组成原理"

    def test_最新的排在前面(self):
        for i in range(3):
            qa.append_qa_log("k2", {"question": f"Q{i}"})
        assert [r["question"] for r in qa.load_qa_log("k2")] == ["Q2", "Q1", "Q0"]

    def test_limit只取最近若干条(self):
        for i in range(5):
            qa.append_qa_log("k3", {"question": f"Q{i}"})
        assert [r["question"] for r in qa.load_qa_log("k3", limit=2)] == ["Q4", "Q3"]

    def test_坏行被跳过而不是丢掉整份历史(self):
        qa.append_qa_log("k4", {"question": "好1"})
        with qa.qa_log_path("k4").open("a", encoding="utf-8") as f:
            f.write("{这不是合法 json\n")
        qa.append_qa_log("k4", {"question": "好2"})
        assert [r["question"] for r in qa.load_qa_log("k4")] == ["好2", "好1"]

    def test_没有历史时返回空列表(self):
        assert qa.load_qa_log("不存在") == []

    def test_每个知识库各占一个文件(self):
        qa.append_qa_log("ka", {"question": "A"})
        qa.append_qa_log("kb", {"question": "B"})
        assert [r["question"] for r in qa.load_qa_log("ka")] == ["A"]
        assert [r["question"] for r in qa.load_qa_log("kb")] == ["B"]

    def test_落盘失败不抛异常(self, monkeypatch, tmp_path):
        """写历史失败不该让用户已经拿到的答案变成一次失败。"""
        # 指向一个目录：open(..., "a") 会抛 IsADirectoryError（OSError 子类）
        monkeypatch.setattr(qa, "qa_log_path", lambda _: tmp_path)
        qa.append_qa_log("k5", {"question": "Q"})  # 不抛即通过

    def test_不写入答案时也能记录(self):
        """空结果的提问（no_hit）同样要记——它说明这个问题在课件里没有落点。"""
        qa.append_qa_log("k6", {"question": "偏题", "answer": "", "no_hit": True})
        assert qa.load_qa_log("k6")[0]["no_hit"] is True


class TestAnswerStreamWritesHistory:
    """确认落盘真的被接进了链路（而不只是函数存在）。"""

    def test_正常问答会落一条(self, monkeypatch, tmp_path):
        monkeypatch.setattr(qa, "QA_DIR", tmp_path)
        monkeypatch.setattr(qa, "append_qa_log", _REAL_APPEND)
        wire(monkeypatch, client=FakeClient(pieces=["纠删码是冗余编码"]))
        list(qa.answer_stream("纠删码是什么", make_kb()))
        records = qa.load_qa_log("testkb")
        assert len(records) == 1
        assert records[0]["question"] == "纠删码是什么"
        assert "files" in records[0] and "citations" in records[0]

    def test_检索无结果也要落一条(self, monkeypatch, tmp_path):
        monkeypatch.setattr(qa, "QA_DIR", tmp_path)
        monkeypatch.setattr(qa, "append_qa_log", _REAL_APPEND)
        wire(monkeypatch, client=FakeClient(boom=True))
        list(qa.answer_stream("zzz不存在的关键词zzz", make_kb()))
        records = qa.load_qa_log("testkb")
        assert len(records) == 1
        assert records[0]["no_hit"] is True
        assert records[0]["hits"] == []

    def test_生成失败不落无效记录(self, monkeypatch, tmp_path):
        """生成中途报错时没有答案可存，不该留下一条半截记录。"""
        monkeypatch.setattr(qa, "QA_DIR", tmp_path)
        monkeypatch.setattr(qa, "append_qa_log", _REAL_APPEND)
        wire(monkeypatch, client=FakeClient(boom=True))
        list(qa.answer_stream("纠删码是什么", make_kb()))
        assert qa.load_qa_log("testkb") == []


# ------------------------------------------------------------------ 验收 5 的对照开关


class _Capturing:
    """记录 create() 的入参，用来断言"两臂只差 system prompt"。"""

    def __init__(self, pieces, rewrite):
        self.inner = FakeCompletions(pieces, rewrite)
        self.calls: list[dict] = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        return self.inner.create(**kwargs)


def _run_arm(monkeypatch, require: bool):
    cap = _Capturing(["纠删码靠校验块恢复"], "纠删码 校验块")
    client = FakeClient()
    client.chat.completions = cap
    wire(monkeypatch, client=client)
    events = list(qa.answer_stream("纠删码怎么恢复", make_kb(), require_citation=require))
    return cap, events


def _stream_call(cap) -> dict:
    """取**生成**那一次请求。改写也走同一个 client，calls[0] 是改写，不是生成。"""
    return next(c for c in reversed(cap.calls) if c.get("stream"))


class TestCitationAbSwitch:
    """验收 5 的两条链路必须**只差引用约束**，这条性质要靠单测钉住。"""

    def test_默认走引用提示词(self, monkeypatch):
        cap, _ = _run_arm(monkeypatch, require=True)
        assert _stream_call(cap)["messages"][0]["content"] == qa.ANSWER_SYSTEM

    def test_关闭时走无引用提示词(self, monkeypatch):
        cap, _ = _run_arm(monkeypatch, require=False)
        assert _stream_call(cap)["messages"][0]["content"] == qa.ANSWER_SYSTEM_NO_CITATION

    def test_两臂的诚实性约束一致(self):
        """去掉引用要求时**不能顺手把"不许编造"也去掉**——
        否则测出来的差异就分不清是引用约束还是诚实约束带来的。"""
        for prompt in (qa.ANSWER_SYSTEM, qa.ANSWER_SYSTEM_NO_CITATION):
            assert "绝不编造" in prompt
            assert "课件这部分没提到" in prompt

    def test_只有无引用臂要求不标来源(self):
        assert "必须标注来源" in qa.ANSWER_SYSTEM
        assert "不要标注任何来源" not in qa.ANSWER_SYSTEM
        assert "不要标注任何来源" in qa.ANSWER_SYSTEM_NO_CITATION
        assert "必须标注来源" not in qa.ANSWER_SYSTEM_NO_CITATION

    def test_除提示词外其余参数一致(self, monkeypatch):
        cap_a, _ = _run_arm(monkeypatch, require=True)
        cap_b, _ = _run_arm(monkeypatch, require=False)
        a, b = _stream_call(cap_a), _stream_call(cap_b)
        for key in ("model", "max_tokens", "stream"):
            assert a[key] == b[key]
        # 上下文与问题必须完全一样，否则测的就不是引用约束了
        assert a["messages"][1] == b["messages"][1]

    def test_无引用臂照样检索(self, monkeypatch):
        _, events = _run_arm(monkeypatch, require=False)
        kinds = [e["type"] for e in events]
        assert "sources" in kinds and "delta" in kinds
        sources = next(e for e in events if e["type"] == "sources")
        assert sources["hits"], "无引用臂也必须走同一次检索"


class TestTimings:
    """分项计时是可观测性的一部分：首 token 里到底含了哪几段，必须能看见。"""

    def test_done事件带四段耗时(self, monkeypatch):
        _, events = _run_arm(monkeypatch, require=True)
        done = events[-1]
        assert done["type"] == "done"
        t = done["timings"]
        assert set(t) == {"rewrite_ms", "retrieve_ms", "first_token_ms", "total_ms"}
        assert all(v is not None and v >= 0 for v in t.values())

    def test_sources事件带改写与检索耗时(self, monkeypatch):
        _, events = _run_arm(monkeypatch, require=True)
        src = next(e for e in events if e["type"] == "sources")
        assert src["timings"]["retrieve_ms"] >= 0
        assert src["timings"]["rewrite_ms"] >= 0

    def test_检索无结果也带耗时(self, monkeypatch):
        wire(monkeypatch, client=FakeClient(boom=True))
        events = list(qa.answer_stream("zzz不存在zzz", make_kb()))
        done = events[-1]
        assert done["type"] == "done" and done["timings"]["first_token_ms"] is None
