"""P2b 单测：Agent 循环 / 三个 guardrail / 降级模式 / 回滚。

为什么这样搭：
    **LLM 是假的，MCP 是真的。** 协议层已在 test_mcp_server 验过，这里要验的是
    「循环有没有正确驱动工具、guardrail 有没有生效、失败会不会作废文档」——
    所以用假 LLM 按剧本返回 tool_calls（不花钱、可复现），MCP 那一侧仍起真子进程，
    保证「模型说要调什么」→「工具真的被调用并影响文档」这条链路是通的。
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from app.config import reload_settings
from app.models import ExtractionResult, GenerateRequest, SlideContent
from app.services import agent_repair, ai_generator
from app.services.user_config import save_user_settings

# 覆盖率的触发阈值取自 coverage.verdict 的「良好」分界，测试里也跟着用常量
TRIGGER = agent_repair.REPAIR_TRIGGER_PERCENT


# ---------------------------------------------------------------- 环境隔离

def _activate(monkeypatch, tmp_path, provider_id: str = "deepseek"):
    """隔离环境 + 保存假 key + 热重载，让 get_settings / current_provider 有值。"""
    for k in ("AI_API_KEY", "AI_BASE_URL", "AI_MODEL", "AI_VISION_MODEL"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("PPT_REVIEWER_CONFIG_DIR", str(tmp_path))
    reload_settings()
    save_user_settings(provider_id, "sk-test-key", "")
    reload_settings()


# ---------------------------------------------------------------- 素材

def _extraction() -> ExtractionResult:
    return ExtractionResult(
        filename="第1节.pptx",
        total_pages=2,
        slides=[
            SlideContent(
                page=1,
                text="CRUSH 算法负责数据分布；rados bench 用于性能测试；osd 负责数据存储",
            ),
            SlideContent(
                page=2,
                text="pg_num 决定归置组数量；ceph osd tree 查看拓扑；mon 监控集群状态",
                notes="重点：pg_num 与 pgp_num 必须保持一致",
            ),
        ],
    )


LOW_DOC = "# 第1节 复习指南\n\n## 知识点\n\nCRUSH 算法是 Ceph 的核心机制。\n"

GOOD_PATCH = (
    "补充：rados bench 是课件里介绍的性能测试工具（源：第 1 页）；"
    "pg_num 决定归置组数量、pgp_num 须与之一致（源：第 2 页）；"
    "osd 负责实际数据存储，可用 ceph osd tree 查看拓扑（源：第 2 页）。"
)


# ---------------------------------------------------------------- 假 LLM

def _usage(p: int = 800, c: int = 200):
    return SimpleNamespace(prompt_tokens=p, completion_tokens=c, total_tokens=p + c)


def _reply(tool_calls: list | None = None, content: str = ""):
    msg = SimpleNamespace(content=content, tool_calls=tool_calls)
    return SimpleNamespace(
        choices=[SimpleNamespace(message=msg, finish_reason="stop")], usage=_usage()
    )


def _call(cid: str, name: str, args: dict):
    return SimpleNamespace(
        id=cid,
        type="function",
        function=SimpleNamespace(name=name, arguments=json.dumps(args, ensure_ascii=False)),
    )


def _fake_llm(monkeypatch, script: list):
    """把 build_client 换成按剧本返回的假客户端；返回收到的 kwargs 列表供断言。"""
    seen: list[dict] = []

    def create(**kwargs):
        seen.append(kwargs)
        return script.pop(0) if script else _reply()

    fake = SimpleNamespace(
        chat=SimpleNamespace(completions=SimpleNamespace(create=create))
    )
    monkeypatch.setattr(ai_generator, "build_client", lambda settings: fake)
    return seen


@pytest.fixture(autouse=True)
def _no_network_probe(monkeypatch):
    """探测函数会真发请求——测试里一律替掉（各自用例按需覆盖）。"""
    monkeypatch.setattr(agent_repair, "supports_tools", lambda settings: (True, ""))


def _low_coverage():
    """取一份低覆盖文档的真实覆盖率对象。"""
    from app.services.coverage import measure

    return measure(_extraction(), LOW_DOC)


# ---------------------------------------------------------------- 纯函数


def test_needs_repair_requires_all_three_conditions():
    """三个条件缺一不可：有数据 / 样本可靠 / 低于阈值。"""
    from app.services.coverage import CoverageReport

    ok = CoverageReport(filename="a", total_terms=50, missing_terms=20)  # 60%
    assert agent_repair.needs_repair(ok) is True
    # 样本太小 → 百分比是噪声，不补
    assert agent_repair.needs_repair(
        CoverageReport(filename="a", total_terms=2, missing_terms=1)
    ) is False
    # 已经够好 → 不补（这是"零触发零成本"的那条路径）
    assert agent_repair.needs_repair(
        CoverageReport(filename="a", total_terms=100, missing_terms=2)
    ) is False
    assert agent_repair.needs_repair(None) is False


def test_token_cost_and_budget():
    assert agent_repair.token_cost({"prompt_tokens": 1000, "completion_tokens": 500}, 3.0, 9.0) == pytest.approx(0.0075)
    assert agent_repair.token_cost(None, 3.0, 9.0) == 0.0
    # 有基线：按 40% 给
    assert agent_repair._budget(0.10) == pytest.approx(0.04)
    # 基线极小：至少给 2 分钱余量，否则一轮都跑不完
    assert agent_repair._budget(0.001) == pytest.approx(0.02)
    # 没基线：用绝对上限
    assert agent_repair._budget(0.0) == agent_repair.FALLBACK_COST_CAP_YUAN


def test_parse_patches_is_forgiving():
    assert agent_repair._parse_patches('[{"content": "甲"}]') == [{"content": "甲"}]
    fenced = '看这里：\n```json\n[{"insert_after": "知识点", "content": "乙"}]\n```\n完'
    assert agent_repair._parse_patches(fenced) == [
        {"insert_after": "知识点", "content": "乙"}
    ]
    # 混在文字里的裸数组也能捞出来
    assert agent_repair._parse_patches('先这样 [{"content": "丙"}] 再说') == [
        {"content": "丙"}
    ]
    # 各种坏输入一律返回空，绝不抛错（宁可这轮不补）
    assert agent_repair._parse_patches("") == []
    assert agent_repair._parse_patches("没有数组") == []
    assert agent_repair._parse_patches("[这不是 json]") == []
    assert agent_repair._parse_patches('[{"no_content": 1}]') == []


# ---------------------------------------------------------------- 主循环


def test_autonomous_loop_drives_tools_and_raises_coverage(monkeypatch, tmp_path):
    """一轮调工具补漏、二轮模型自己收工——循环要能把工具效果落到文档上。"""
    _activate(monkeypatch, tmp_path)
    script = [
        _reply([
            _call("c1", "list_missing", {}),
            _call("c2", "read_source_page", {"page": 1}),
            _call("c3", "submit_patch", {"insert_after": "知识点", "content": GOOD_PATCH}),
        ]),
        _reply(content="已补充完毕"),  # 不再调工具 → 循环结束
    ]
    seen = _fake_llm(monkeypatch, script)

    cov = _low_coverage()
    assert cov.percent < TRIGGER and cov.reliable
    doc, rec = agent_repair.repair_document(
        _extraction(),
        LOW_DOC,
        cov,
        workdir=tmp_path / "repair",
        base_cost_yuan=1.0,
        provider={"price_in": 3.0, "price_out": 9.0},
    )

    assert rec.mode == "autonomous"
    assert rec.trigger_percent == cov.percent
    assert rec.final_percent > cov.percent          # 真的补上去了
    assert rec.rolled_back is False
    assert rec.stop_reason == "model_stopped"
    assert doc != LOW_DOC and "rados" in doc        # 返回的是补后的文档
    # 每轮都记了账，且第一轮确实落地了补丁
    assert len(rec.rounds) == 2
    assert rec.rounds[0].patches_applied == 1
    assert rec.total_cost_yuan > 0
    # 工具 schema 真的发给了模型（否则这就不算 Function Calling）
    assert any("tools" in kw for kw in seen)
    tool_names = {t["function"]["name"] for t in seen[0]["tools"]}
    assert tool_names == {
        "check_coverage",
        "list_missing",
        "read_source_page",
        "submit_patch",
    }
    # 原文备份留着（回滚依据 + 用户对照"补了什么"）
    assert (tmp_path / "repair" / "doc.md.pre-repair").read_text(encoding="utf-8") == LOW_DOC


def test_rolls_back_when_no_improvement(monkeypatch, tmp_path):
    """模型只说不动手 → 覆盖率没变 → 必须回滚，用户拿到的还是原文档。"""
    _activate(monkeypatch, tmp_path)
    _fake_llm(monkeypatch, [_reply(content="我看了一下，不需要补")])

    cov = _low_coverage()
    doc, rec = agent_repair.repair_document(
        _extraction(), LOW_DOC, cov, workdir=tmp_path / "r",
        base_cost_yuan=1.0, provider={"price_in": 3.0, "price_out": 9.0},
    )
    assert doc == LOW_DOC                 # 一个字都不能变
    assert rec.rolled_back is True
    assert rec.final_percent == cov.percent
    assert "回退" in rec.note


def test_cost_guardrail_stops_the_loop(monkeypatch, tmp_path):
    """增量成本超上限就停——防的是模型无止境地调工具刷 token。"""
    _activate(monkeypatch, tmp_path)
    # 基线极小 → 预算 2 分钱；每轮消耗按 usage 算会迅速超掉
    # 单轮 (100000×3 + 50000×9)/1e6 = 0.75 元，远超预算，一轮就该停
    big = SimpleNamespace(
        prompt_tokens=100000, completion_tokens=50000, total_tokens=150000
    )

    def create(**kwargs):
        return SimpleNamespace(
            choices=[SimpleNamespace(
                message=SimpleNamespace(
                    content="", tool_calls=[_call("x", "list_missing", {})]
                ),
                finish_reason="stop",
            )],
            usage=big,
        )

    fake = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
    monkeypatch.setattr(ai_generator, "build_client", lambda settings: fake)

    cov = _low_coverage()
    _, rec = agent_repair.repair_document(
        _extraction(), LOW_DOC, cov, workdir=tmp_path / "r",
        base_cost_yuan=1.0,  # 预算 = 1.0 × 40% = 0.4 元
        provider={"price_in": 3.0, "price_out": 9.0},
    )
    assert rec.stop_reason == "cost_limit"
    # 第 1 轮之后就超预算了，不该再烧第二轮
    assert len(rec.rounds) == 1
    assert rec.total_cost_yuan > 0.4


def test_stall_guardrail_after_one_warning(monkeypatch, tmp_path):
    """提交了但没效果：先提醒一次（给模型自纠的机会），再犯才强制停。

    ⚠️ 判定停滞的前提是**这一轮调用过 submit_patch**——只读不写的侦察轮不计入，
    否则真实模型（先看清单、再读源页、最后才提交）刚起步就会被掐死。
    见 test_scouting_rounds_do_not_trigger_stall 的反向用例。
    """
    _activate(monkeypatch, tmp_path)
    # 每轮都提交一段"无助于覆盖率"的内容（CRUSH 本来就在文档里）→ 覆盖率恒不升
    _fake_llm(monkeypatch, [
        _reply([
            _call(
                f"c{i}",
                "submit_patch",
                {"insert_after": "知识点", "content": f"重申{i}：CRUSH 算法（源：第 1 页）"},
            )
        ])
        for i in range(5)
    ])

    cov = _low_coverage()
    _, rec = agent_repair.repair_document(
        _extraction(), LOW_DOC, cov, workdir=tmp_path / "r",
        base_cost_yuan=100.0, provider={"price_in": 3.0, "price_out": 9.0},
    )
    assert rec.stop_reason == "stalled"
    assert len(rec.rounds) == 2          # 提醒一次，再犯即停（早止损）
    assert rec.rolled_back is True       # 没提升 → 回滚，用户拿到的还是原文档


def test_scouting_rounds_do_not_trigger_stall(monkeypatch, tmp_path):
    """回归：只读不写的侦察轮**不能**被判停滞。

    2026-09-15 真实冒烟踩到的 bug —— 模型第 1 轮 check_coverage + list_missing、
    第 2 轮连读 4 页源课件，全程还没到提交阶段，就被"覆盖率没提升"判成停滞、
    一行都没补上就退出。真实模型就是这个节奏，所以判定必须等到它真的提交过。
    """
    _activate(monkeypatch, tmp_path)
    _fake_llm(monkeypatch, [
        _reply([_call("s1", "check_coverage", {}), _call("s2", "list_missing", {})]),
        _reply([_call("s3", "read_source_page", {"page": 1}),
                _call("s4", "read_source_page", {"page": 2})]),
        _reply([_call("s5", "submit_patch",
                      {"insert_after": "知识点", "content": GOOD_PATCH})]),
        _reply(content="补完了"),
    ])

    cov = _low_coverage()
    doc, rec = agent_repair.repair_document(
        _extraction(), LOW_DOC, cov, workdir=tmp_path / "r",
        base_cost_yuan=100.0, provider={"price_in": 3.0, "price_out": 9.0},
    )
    assert rec.stop_reason == "model_stopped"     # 走完了，而不是被停滞掐死
    assert len(rec.rounds) == 4                   # 两轮侦察都保留下来了
    assert rec.final_percent > cov.percent        # 第 3 轮提交真的生效
    assert "rados" in doc


def test_repair_failure_never_breaks_the_document(monkeypatch, tmp_path):
    """repair 是增强功能：起不来也必须原样返回文档（P1 覆盖率回检同一原则）。"""
    _activate(monkeypatch, tmp_path)

    from app.services import mcp_client

    class _Boom:
        async def __aenter__(self):
            raise RuntimeError("模拟 MCP 子进程起不来")

        async def __aexit__(self, *a):
            return False

    monkeypatch.setattr(mcp_client, "open_repair_session", lambda *a, **k: _Boom())

    cov = _low_coverage()
    doc, rec = agent_repair.repair_document(
        _extraction(), LOW_DOC, cov, workdir=tmp_path / "r",
        base_cost_yuan=1.0, provider={"price_in": 3.0, "price_out": 9.0},
    )
    assert doc == LOW_DOC
    assert rec.stop_reason == "error"
    assert "RuntimeError" in rec.note


# ---------------------------------------------------------------- 降级模式


def test_orchestrated_fallback_when_provider_lacks_tools(monkeypatch, tmp_path):
    """provider 不支持 Function Calling 时：工具照用，但顺序由程序定，并如实记录 mode。"""
    _activate(monkeypatch, tmp_path)
    monkeypatch.setattr(
        agent_repair, "supports_tools", lambda settings: (False, "BadRequestError: no tools")
    )
    patches = json.dumps(
        [{"insert_after": "知识点", "content": GOOD_PATCH}], ensure_ascii=False
    )
    _fake_llm(monkeypatch, [_reply(content=patches)])

    cov = _low_coverage()
    doc, rec = agent_repair.repair_document(
        _extraction(), LOW_DOC, cov, workdir=tmp_path / "r",
        base_cost_yuan=1.0, provider={"price_in": 3.0, "price_out": 9.0},
    )
    assert rec.mode == "orchestrated"
    assert "不支持 Function Calling" in rec.note
    assert rec.final_percent > cov.percent
    assert "rados" in doc
    assert rec.rounds[0].patches_applied == 1


# ---------------------------------------------------------------- 未开启时不动作


def test_disabled_request_skips_repair_entirely(monkeypatch, tmp_path):
    """默认关闭：连模型都不该被调用（这是"零成本"的保证）。"""
    _activate(monkeypatch, tmp_path)
    called: list[dict] = []

    def create(**kwargs):
        called.append(kwargs)
        return _reply(content="# 文档")

    fake = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
    monkeypatch.setattr(ai_generator, "build_client", lambda settings: fake)

    out = ai_generator.run_single(
        _extraction(), GenerateRequest(filename="第1节.pptx", enable_repair=False)
    )
    assert out.repair is None
    assert len(called) == 1  # 只有生成那一次调用
