"""verify → repair 闭环：让 Agent 自己把漏掉的知识点补回来（P2b）。

设计见 docs/P2-REPAIR.md。一句话：
    生成后回检（P1）发现覆盖率低于阈值时，起一个带 MCP 工具的 Agent——
    它能查缺失清单、读源课件页、提交结构化补丁、即时验证效果，自己决定何时收敛；
    程序侧只做 guardrail（轮次 / 成本 / 停滞）与回滚。

几条刻意的取舍：

1. **模型自主，程序不编排**（主模式）。
   程序编排的那条路径存在，但只作为 provider 不支持 Function Calling 时的降级，
   且如实记录 ``mode="orchestrated"``——不冒充自主 Agent。

2. **循环跑在同步世界里的方式**：现有生成链路（FastAPI 同步端点、批任务 worker 线程）
   都不是 async 的，而 MCP client 必须 async。这里统一用 ``_run_sync()`` 收口：
   没有事件循环就 ``asyncio.run``；已经在循环里（将来有人从 async 端点调）就另起线程跑，
   避免 ``asyncio.run() cannot be called from a running event loop``。

3. **失败绝不作废文档**：与 P1 的覆盖率回检同一原则——repair 是增强功能，
   任何异常都吞掉、返回**原文档**，只在记录里写明原因。

4. **没有变好就回滚**：补完一轮后重新回检，覆盖率没提升就交还原文档
   （``rolled_back=True``）。用户永远不会拿到"被改乱了却没变好"的文档。
"""

from __future__ import annotations

import asyncio
import json
import re
import threading
from pathlib import Path
from typing import Any

from app.config import current_provider, get_settings
from app.models import (
    ExtractionResult,
    RepairRecord,
    RepairRound,
)

from app.services.coverage import CoverageReport, measure

# ---------------------------------------------------------------- 常量
# 触发阈值 = coverage.verdict 里「覆盖良好」的分界，与 P1 标定的阈值体系一致。
# 低于它才补：对本来就良好的文档零触发零成本（验收标准 §5.2）。
REPAIR_TRIGGER_PERCENT = 94
# 轮次上限：真实模型的节奏是「1 轮侦察 → 若干轮读源页 → 若干轮提交」，
# 3 轮在缺失较多时会不够（实测：模型前两轮都在读页，第 3 轮才刚准备提交）。
# 提到 5 给这个节奏留空间；真正的成本闸门是下面的 COST_RATIO_LIMIT。
MAX_ROUNDS = 5
# 增量成本上限 = 生成阶段花费的比例（设计稿验收标准：≤40%）。
COST_RATIO_LIMIT = 0.40
# 生成成本未知时的绝对上限（按节），避免没有基线时失控。
FALLBACK_COST_CAP_YUAN = 0.5
# 一轮覆盖率提升低于这个值算「停滞」（百分点）。
STALL_DELTA = 1
# 补丁很短，不需要生成那套 32768 的输出上限。
REPAIR_MAX_TOKENS = 8192
# 降级模式下最多读几页、提交几段（防止 prompt 与轮次失控）。
ORCH_MAX_PAGES = 8
ORCH_MAX_PATCHES = 6

REPAIR_SYSTEM_PROMPT = """你是复习文档的「知识点补漏」助手。

这份文档已经写好，但覆盖率检查发现它可能漏掉了源课件里的某些知识点。
你**看不到文档全文**，只能通过工具观察和修改它：

  · check_coverage()     看当前覆盖率、缺失术语（含源课件页码）、文档章节大纲
  · list_missing()       只要缺失清单，更省 token
  · read_source_page(页码) 读源课件某页的原文（含讲者备注）
  · submit_patch(插入位置, 内容) 提交一段补充，返回最新覆盖率

任务：把「疑似未覆盖」的术语补进文档。规则：

1. **只补缺失，不改写已有内容**。工具只支持插入，你不需要也无法重写原文。
2. **补充内容必须来自 read_source_page 读到的真实内容**：先读页码，再动笔。
   为每个术语给出**实质说明**（它是什么、干什么用、有什么约束），不要只把词写进去。
3. **严禁为了凑覆盖率罗列术语**。凭空编造的内容比遗漏更糟。
   若某个术语其实已经讲清楚了、只是换了个说法，说明情况后跳过它即可。
4. 每条补充标注来源，如「（源：第 7 页）」。
5. **先把要用的源页一次读齐**（一轮里可以并行调用多次 read_source_page），然后立刻提交补丁。
   不要只读不写——你的轮次有限，把轮次留给提交。
6. 一次 submit_patch 可以覆盖多个缺失术语；提交后看返回的 percent_after 判断是否有效。
7. 覆盖率到 94% 以上、或连续两轮没有提升时，直接说明你做了什么并停止调用工具。
"""

STALL_HINT = (
    "注意：上一轮覆盖率几乎没有提升。请判断是「已经补完」还是「缺的内容读不到」——"
    "如果是前者，请直接说明你补了什么并停止调用工具；不要为了凑指标重复提交。"
)


# ---------------------------------------------------------------- 工具函数


def needs_repair(coverage: CoverageReport | None) -> bool:
    """是否值得补漏。

    三个条件缺一不可（与覆盖率回检的适用口径完全一致）：
      · 有覆盖率数据；· 样本量够（reliable，术语太少时百分比是噪声）；
      · 低于触发阈值。
    档位（复习指南 / 详细）由调用方判断——那里已经算过 applies_to。
    """
    return bool(
        coverage and coverage.reliable and coverage.percent < REPAIR_TRIGGER_PERCENT
    )


def _prices(provider: dict) -> tuple[float, float]:
    """repair 是纯文本请求（不带图），一律用文本单价。"""
    return (
        float(provider.get("price_in") or 0),
        float(provider.get("price_out") or 0),
    )


def token_cost(usage: dict | None, pin: float, pout: float) -> float:
    if not usage:
        return 0.0
    return (
        usage.get("prompt_tokens", 0) * pin + usage.get("completion_tokens", 0) * pout
    ) / 1_000_000


def _budget(base_cost_yuan: float) -> float:
    """本轮 repair 允许花的钱。"""
    if base_cost_yuan > 0:
        # 下限 2 分钱：生成很便宜时（如用了免费档）也留出起码的补漏余量
        return max(base_cost_yuan * COST_RATIO_LIMIT, 0.02)
    return FALLBACK_COST_CAP_YUAN


def _run_sync(coro):
    """在同步世界里跑 async 协程（见模块 docstring 第 2 条）。"""
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro)

    box: dict[str, Any] = {}

    def _worker() -> None:
        try:
            box["value"] = asyncio.run(coro)
        except BaseException as exc:  # noqa: BLE001 原样抛回主线程
            box["error"] = exc

    t = threading.Thread(target=_worker, daemon=True)
    t.start()
    t.join()
    if "error" in box:
        raise box["error"]
    return box.get("value")


def _initial_user_prompt(coverage: CoverageReport, total_pages: int) -> str:
    return (
        f"这份复习文档当前的覆盖率是 {coverage.percent}%，"
        f"有 {coverage.missing_terms} 个「疑似未覆盖」的知识点术语（共抽取 "
        f"{coverage.total_terms} 个），源课件共 {total_pages} 页。\n"
        "请先查看缺失清单和文档大纲，再按需读取源课件页码，然后提交补充。"
    )


def _parse_patches(text: str) -> list[dict]:
    """从模型输出里抽取补丁数组（降级模式用）。

    容错优先：先找 ```json 围栏，再退化成找第一个 [...]；解析不了就返回空
    （宁可这一轮不补，也不要抛错中断整个流程）。
    """
    if not text:
        return []
    m = re.search(r"```(?:json)?\s*(.+?)```", text, re.S)
    raw = m.group(1) if m else text
    m2 = re.search(r"\[[\s\S]*\]", raw)
    if not m2:
        return []
    try:
        data = json.loads(m2.group(0))
    except json.JSONDecodeError:
        return []
    if not isinstance(data, list):
        return []
    return [d for d in data if isinstance(d, dict) and str(d.get("content", "")).strip()]


async def _tool_schemas(mc) -> list[dict]:
    """把 MCP 工具清单转成 OpenAI Function Calling 的 tools 参数。

    注意入参是宿主侧的 RepairClient 包装（不是底层 ClientSession）——
    ``mc.list_tools()`` 返回带 inputSchema 的 Tool 列表。
    """
    out: list[dict] = []
    for t in await mc.list_tools():
        out.append(
            {
                "type": "function",
                "function": {
                    "name": t.name,
                    "description": t.description or "",
                    "parameters": t.inputSchema
                    or {"type": "object", "properties": {}},
                },
            }
        )
    return out


async def _current_percent(mc) -> int:
    try:
        cov = await mc.call("check_coverage")
        return int(cov.get("percent", -1)) if isinstance(cov, dict) else -1
    except Exception:  # noqa: BLE001 取不到就不更新，别让统计拖垮流程
        return -1


def _brief_args(args) -> str:
    """日志用：工具参数摘要（长内容截断，别把正文刷满屏）。"""
    if not isinstance(args, dict):
        return str(args)[:60]
    parts = []
    for k, v in args.items():
        s = str(v)
        parts.append(f"{k}={s[:40] + '…' if len(s) > 40 else s}")
    return ", ".join(parts)


def _brief_out(out) -> str:
    """日志用：工具返回摘要——一眼能看出「模型为什么没补上」。"""
    if isinstance(out, dict):
        if "applied" in out:
            if out.get("applied"):
                return f"已插入（{out.get('located_by')}）→ {out.get('percent_after')}%"
            return f"被拒：{out.get('reason')}"
        if "percent" in out:
            return f"覆盖率 {out['percent']}%（缺 {out.get('missing_terms')}）"
        if "count" in out:
            return f"缺 {out['count']} 项"
        if "text" in out:
            return f"第 {out.get('page')} 页 / {len(out['text'])} 字"
        if "error" in out:
            return f"错误：{out['error']}"
    return str(out)[:80]


# ---------------------------------------------------------------- tools 支持探测

_TOOLS_SUPPORT: dict[str, tuple[bool, str]] = {}


def supports_tools(settings) -> tuple[bool, str]:
    """该服务商是否支持 Function Calling（按 base_url + model 缓存探测结果）。

    探测方式：发一个带 tools 的最小请求。**失败不等于"不支持"**（也可能是网络、
    额度问题），所以降级路径会如实把原因写进 note，不假装是能力缺陷。
    """
    key = f"{settings.ai_base_url}|{settings.ai_model}"
    if key in _TOOLS_SUPPORT:
        return _TOOLS_SUPPORT[key]

    from app.services.ai_generator import build_client

    probe = [
        {
            "type": "function",
            "function": {
                "name": "ping",
                "description": "连通性检查",
                "parameters": {"type": "object", "properties": {}},
            },
        }
    ]
    ok, why = True, ""
    try:
        client = build_client(settings)
        client.chat.completions.create(
            model=settings.ai_model,
            messages=[{"role": "user", "content": "ping"}],
            tools=probe,
            max_tokens=8,
        )
    except Exception as exc:  # noqa: BLE001 各家报错形态不一，一律按不支持处理
        ok, why = False, f"{type(exc).__name__}: {str(exc)[:160]}"
    _TOOLS_SUPPORT[key] = (ok, why)
    return ok, why


# ---------------------------------------------------------------- 主入口


def repair_document(
    result: ExtractionResult,
    markdown: str,
    coverage: CoverageReport,
    *,
    workdir: Path,
    base_cost_yuan: float = 0.0,
    provider: dict | None = None,
) -> tuple[str, RepairRecord]:
    """跑一次补漏，返回 ``(最终文档, 记录)``。

    ``workdir`` 存放会话上下文、原文备份与补漏后的文档（留痕，用户可事后查看）。
    """
    rec = RepairRecord(
        triggered=True,
        trigger_percent=coverage.percent,
        final_percent=coverage.percent,
    )
    try:
        workdir = Path(workdir)
        workdir.mkdir(parents=True, exist_ok=True)
        # 原文备份：既是回滚依据，也是给用户对照"补了什么"的底本
        (workdir / "doc.md.pre-repair").write_text(markdown, encoding="utf-8")

        settings = get_settings()
        prov = provider or (current_provider() or {})
        pin, pout = _prices(prov)
        budget = _budget(base_cost_yuan)

        ok, why = supports_tools(settings)
        if ok:
            rec.mode = "autonomous"
        else:
            rec.mode = "orchestrated"
            rec.note = f"该服务商不支持 Function Calling，改用程序编排补漏（{why}）"

        if rec.mode == "autonomous":
            doc = _run_sync(
                _autonomous_loop(
                    result, markdown, coverage, workdir, rec, settings, pin, pout, budget
                )
            )
        else:
            doc = _run_sync(
                _orchestrated_loop(
                    result, markdown, coverage, workdir, rec, settings, pin, pout
                )
            )

        # 重新回检：只有真的变好才采用（回滚原则）
        try:
            rec.final_percent = measure(result, doc).percent
        except Exception as exc:  # noqa: BLE001
            rec.note = (rec.note + "；" if rec.note else "") + f"回检失败：{exc}"
            rec.final_percent = coverage.percent

        if rec.final_percent > coverage.percent:
            (workdir / "doc.md.post-repair").write_text(doc, encoding="utf-8")
            return doc, rec

        rec.rolled_back = True
        rec.final_percent = coverage.percent
        rec.note = (rec.note + "；" if rec.note else "") + "补漏后覆盖率未提升，已回退到原文档"
        return markdown, rec

    except Exception as exc:  # noqa: BLE001 —— 绝不让 repair 作废已生成好的文档
        rec.stop_reason = "error"
        rec.note = f"{type(exc).__name__}: {exc}"
        print(f"[repair] 补漏失败（不影响文档）：{rec.note}")
        return markdown, rec


# ---------------------------------------------------------------- 主模式：模型自主


async def _autonomous_loop(
    result: ExtractionResult,
    markdown: str,
    coverage: CoverageReport,
    workdir: Path,
    rec: RepairRecord,
    settings,
    pin: float,
    pout: float,
    budget: float,
) -> str:
    from app.services.ai_generator import _create_completion, build_client, _extract_usage
    from app.services.mcp_client import open_repair_session

    llm = build_client(settings)
    stall_notified = False

    async with open_repair_session(result, markdown, workdir) as mc:
        tools = await _tool_schemas(mc)
        messages: list[dict] = [
            {"role": "system", "content": REPAIR_SYSTEM_PROMPT},
            {
                "role": "user",
                "content": _initial_user_prompt(coverage, result.total_pages),
            },
        ]
        percents = [coverage.percent]

        for rnd in range(1, MAX_ROUNDS + 1):
            if rec.total_cost_yuan >= budget:
                rec.stop_reason = "cost_limit"
                break

            kwargs: dict = {
                "model": settings.ai_model,
                "messages": messages,
                "temperature": 0.2,
                "max_tokens": REPAIR_MAX_TOKENS,
                "tools": tools,
            }
            # 补漏是纯文本整理活，不需要思考模式（省 30-50% 费用）
            if settings.ai_thinking_off:
                kwargs["extra_body"] = settings.ai_thinking_off

            resp = _create_completion(llm, kwargs)
            usage = _extract_usage(resp)
            cost = token_cost(usage, pin, pout)
            rec.total_cost_yuan = round(rec.total_cost_yuan + cost, 4)

            msg = resp.choices[0].message
            calls = list(getattr(msg, "tool_calls", None) or [])
            row = RepairRound(
                round=rnd,
                tool_calls=len(calls),
                prompt_tokens=(usage or {}).get("prompt_tokens", 0),
                completion_tokens=(usage or {}).get("completion_tokens", 0),
                cost_yuan=round(cost, 4),
                percent_before=percents[-1],
            )

            if not calls:
                # 模型自己认为补完了
                row.percent_after = percents[-1]
                rec.rounds.append(row)
                rec.stop_reason = "model_stopped"
                break

            messages.append(
                {
                    "role": "assistant",
                    "content": msg.content or "",
                    "tool_calls": [
                        {
                            "id": c.id,
                            "type": "function",
                            "function": {
                                "name": c.function.name,
                                "arguments": c.function.arguments,
                            },
                        }
                        for c in calls
                    ],
                }
            )

            applied = 0
            for call in calls:
                try:
                    args = json.loads(call.function.arguments or "{}")
                except json.JSONDecodeError:
                    args = {}
                try:
                    out = await mc.call(call.function.name, args)
                except Exception as exc:  # noqa: BLE001 工具报错要回给模型，别中断循环
                    out = {"error": f"{type(exc).__name__}: {exc}"}
                if isinstance(out, dict) and out.get("applied") is True:
                    applied += 1
                # 工具调用轨迹：既是排查依据，也是「Agent 到底做了什么」的用户可见证据
                print(
                    f"[repair]   第{rnd}轮 {call.function.name}"
                    f"({_brief_args(args)}) → {_brief_out(out)}"
                )
                messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": call.id,
                        "content": json.dumps(out, ensure_ascii=False),
                    }
                )

            row.patches_applied = applied
            cur = await _current_percent(mc)
            row.percent_after = cur
            rec.rounds.append(row)
            percents.append(cur)

            if cur >= REPAIR_TRIGGER_PERCENT:
                rec.stop_reason = "reached_target"
                break

            # 停滞检测：先提醒一次（仍由模型决定），再犯才强制中止。
            #
            # ⚠️ 只有**尝试过提交补丁**的轮次才参与判定。真实模型的节奏是
            # 「先侦察（看清单）→ 再读源页 → 最后提交」，前两轮覆盖率必然不动——
            # 拿它们判停滞会把模型刚起步就掐死。实测踩到：模型第 1 轮
            # check_coverage + list_missing 就被判停滞，一行都没补上。
            tried_patch = any(c.function.name == "submit_patch" for c in calls)
            stalled = (
                tried_patch
                and len(percents) >= 2
                and (percents[-1] - percents[-2]) < STALL_DELTA
            )
            if stalled:
                if stall_notified:
                    rec.stop_reason = "stalled"
                    break
                stall_notified = True
                messages.append({"role": "user", "content": STALL_HINT})
        else:
            rec.stop_reason = "max_rounds"

        return await mc.final_document()


# ---------------------------------------------------------------- 降级：程序编排


async def _orchestrated_loop(
    result: ExtractionResult,
    markdown: str,
    coverage: CoverageReport,
    workdir: Path,
    rec: RepairRecord,
    settings,
    pin: float,
    pout: float,
) -> str:
    """provider 不支持 Function Calling 时的降级：工具照用，只是调用顺序由程序决定。

    **不是"没有 MCP"**——4 个工具全部照常通过 MCP 协议调用，只是失去模型的自主性，
    所以记录里 mode 如实写 orchestrated，不冒充自主 Agent。
    """
    from app.services.ai_generator import _create_completion, build_client, _extract_usage
    from app.services.mcp_client import open_repair_session

    llm = build_client(settings)

    async with open_repair_session(result, markdown, workdir) as mc:
        miss = await mc.call("list_missing") or {}
        items = miss.get("missing", []) if isinstance(miss, dict) else []
        if not items:
            rec.stop_reason = "model_stopped"
            return await mc.final_document()

        pages = sorted({int(m["page"]) for m in items if m.get("page")})[:ORCH_MAX_PAGES]
        sources: list[str] = []
        for p in pages:
            got = await mc.call("read_source_page", {"page": p})
            if isinstance(got, dict) and got.get("text"):
                sources.append(f"【第 {p} 页】\n{got['text']}")

        cov = await mc.call("check_coverage") or {}
        outline = cov.get("outline", []) if isinstance(cov, dict) else []
        terms = "、".join(str(m.get("term")) for m in items[:40])

        prompt = (
            f"下面是源课件的部分原文，以及这份复习文档「疑似未覆盖」的术语清单。\n"
            f"请依据原文为这些术语各写一段**实质说明**，并给出应该插入到哪个小节。\n\n"
            f"术语清单：{terms}\n\n"
            f"文档已有的小节标题（insert_after 只能从这个列表里选，选不出就给 null）："
            f"{'、'.join(outline) or '（无）'}\n\n"
            f"源课件原文：\n{chr(10).join(sources)}\n\n"
            "输出**严格的 JSON 数组**，不要任何解释文字，格式：\n"
            '[{"insert_after": "小节标题或 null", "content": "补充内容（含来源页码）"}]\n'
            "内容必须来自上面读到的原文；读不到依据的术语请跳过，不要编造。"
        )

        kwargs: dict = {
            "model": settings.ai_model,
            "messages": [{"role": "user", "content": prompt}],
            "temperature": 0.2,
            "max_tokens": REPAIR_MAX_TOKENS,
        }
        if settings.ai_thinking_off:
            kwargs["extra_body"] = settings.ai_thinking_off

        resp = _create_completion(llm, kwargs)
        usage = _extract_usage(resp)
        cost = token_cost(usage, pin, pout)
        rec.total_cost_yuan = round(rec.total_cost_yuan + cost, 4)

        text = resp.choices[0].message.content or ""
        patches = _parse_patches(text)[:ORCH_MAX_PATCHES]

        applied = 0
        for pt in patches:
            out = await mc.call(
                "submit_patch",
                {
                    "insert_after": pt.get("insert_after"),
                    "content": pt.get("content", ""),
                },
            )
            if isinstance(out, dict) and out.get("applied") is True:
                applied += 1

        rec.rounds.append(
            RepairRound(
                round=1,
                tool_calls=len(pages) + 2,  # 读页 + list_missing + check_coverage
                patches_applied=applied,
                prompt_tokens=(usage or {}).get("prompt_tokens", 0),
                completion_tokens=(usage or {}).get("completion_tokens", 0),
                cost_yuan=round(cost, 4),
                percent_before=coverage.percent,
                percent_after=await _current_percent(mc),
            )
        )
        rec.stop_reason = "model_stopped"
        if not patches:
            rec.note = (rec.note + "；" if rec.note else "") + "模型未产出可解析的补丁"
        return await mc.final_document()
