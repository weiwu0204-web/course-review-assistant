"""把 `raw.json` 变成一张人工评分表（HTML，单文件，可离线打开）。

## 为什么是 HTML 而不是 Markdown 表格

40 条答案（20 题 × 2 臂）用问卷星式的界面打分会快很多，而且能**自动存草稿**
（localStorage），关掉浏览器也不丢；评完一键导出 JSON 交给 `p3_ab_score.py` 解盲。

## 关于「盲评」：这里只能做到一半，说清楚

- **能做的**：每题 甲/乙 的顺序按固定种子随机（不是固定的"先引用后无引用"），
  这样评分不会因为"总是先看好话"而系统性偏向某一臂。
- **做不到的**：**没法隐藏哪条带引用**——引用就明明白白写在答案里。
  所以真正的防偏袒只能靠评分规则：**只看事实是否与原文一致，忽略格式、长度、有无引用标注**。
  这条写在表格最上面，也是写进 docs 的原话。

用法：
    .venv/Scripts/python.exe scripts/p3_ab_sheet.py
    → output/eval_citation/评分表.html（双击打开）
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.config import OUTPUT_ROOT

OUT_DIR = OUTPUT_ROOT / "eval_citation"
ANCHOR_CHARS = 1500


def _anchor_text(file: str, page: int, kb_id: str | None = None) -> str:
    """取锚点页**完整原文**（给评分人做对照用）。

    优先从知识库缓存（`output/kb/<id>/kb.json`）读——那里是整页文本；
    读不到再退回 `chunks.json`（只有前 400 字）。
    评分要能核对"这句话到底是不是原文说的"，短片段不够用。
    """
    if kb_id:
        cache = OUTPUT_ROOT / "kb" / kb_id / "kb.json"
        try:
            data = json.loads(cache.read_text(encoding="utf-8"))
            for c in data.get("chunks") or []:
                if c.get("file") == file and int(c.get("page")) == page:
                    return (c.get("text") or "")[:ANCHOR_CHARS]
        except (OSError, json.JSONDecodeError, ValueError):
            pass

    path = OUT_DIR / "chunks.json"
    if not path.exists():
        return ""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return ""
    for c in data:
        if c["file"] == file and c["page"] == page:
            return c["head"][:ANCHOR_CHARS]
    return ""


TEMPLATE = """<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8">
<title>验收 5 · 有溯源 vs 无溯源 评分表</title>
<style>
  :root { --bd:#d9dee7; --mut:#6b7280; --bg:#f6f7f9; --acc:#2563eb; }
  * { box-sizing: border-box; }
  body { margin:0; padding:24px 18px 80px; font:14px/1.65 -apple-system,"Segoe UI","Microsoft YaHei",sans-serif;
         color:#111827; background:var(--bg); }
  .wrap { max-width:1080px; margin:0 auto; }
  h1 { font-size:20px; margin:0 0 6px; }
  .lead { color:var(--mut); margin:0 0 16px; }
  .rules { background:#fff; border:1px solid var(--bd); border-radius:10px; padding:14px 16px; margin-bottom:18px; }
  .rules b { color:#b91c1c; }
  .bar { position:sticky; top:0; z-index:9; background:rgba(246,247,249,.96); padding:8px 0 10px;
         display:flex; gap:12px; align-items:center; border-bottom:1px solid var(--bd); }
  .bar .n { font-weight:600; }
  .btn { border:1px solid var(--bd); background:#fff; border-radius:8px; padding:6px 12px; cursor:pointer; font-size:13px; }
  .btn.primary { background:var(--acc); border-color:var(--acc); color:#fff; }
  .card { background:#fff; border:1px solid var(--bd); border-radius:10px; padding:14px 16px; margin:16px 0; }
  .q { font-weight:600; font-size:15px; margin:0 0 6px; }
  .tag { display:inline-block; font-size:12px; color:#374151; background:#eef2f7; border-radius:999px;
         padding:1px 8px; margin-left:6px; vertical-align:1px; }
  .anchor { font-size:13px; color:#374151; background:#fbfbfd; border:1px dashed var(--bd);
            border-radius:8px; padding:10px 12px; margin:10px 0 14px; white-space:pre-wrap; max-height:260px; overflow:auto; }
  .anchor .h { color:var(--mut); font-size:12px; margin-bottom:4px; }
  .kp { margin:4px 0 0; padding-left:18px; }
  .kp li { margin:2px 0; }
  .col2 { display:grid; grid-template-columns:1fr 1fr; gap:14px; }
  @media (max-width:900px){ .col2 { grid-template-columns:1fr; } }
  .ans { border:1px solid var(--bd); border-radius:8px; padding:12px; }
  .ans h4 { margin:0 0 8px; font-size:13px; letter-spacing:.5px; }
  .ans .body { white-space:pre-wrap; font-size:13.5px; max-height:340px; overflow:auto; }
  .score { margin-top:10px; padding-top:10px; border-top:1px dashed var(--bd); display:flex; flex-wrap:wrap; gap:12px; align-items:center; }
  .score label { cursor:pointer; font-size:13px; }
  .done { outline:2px solid #86efac; }
  .note { font-size:12px; color:var(--mut); }
</style></head><body><div class="wrap">
<h1>验收 5 · 有溯源 vs 无溯源 评分表</h1>
<p class="lead">__N__ 题 × 2 个答案。每题两段答案里，有一段来自「强制引用」链路，一段来自「不要求引用」链路；
顺序已随机化，表里不标。</p>

<div class="rules">
  <b>评分只看事实，不看格式。</b>请对照「原文锚点」判断答案里的<b>事实性陈述</b>是否正确：
  <ul style="margin:8px 0 6px; padding-left:20px;">
    <li><b>准确（2）</b>：关键事实都对，且没有与原文冲突的陈述。答得比锚点更全也照样算准确。</li>
    <li><b>部分准确（1）</b>：说对了一部分，但漏掉关键点、或有一处小错。</li>
    <li><b>错误（0）</b>：关键事实错、答非所问，或整段都是原文没有的内容。</li>
    <li><b>有编造</b>：出现了锚点页（及相邻页）里没有的具体数字/命令/机制，且被当作事实陈述。
      回答"课件没提到"<b>不算编造</b>。</li>
  </ul>
  <span class="note">注意：长度、是否有（源：…）、写得漂亮与否，<b>都不参与判断</b>。
  这是防止"偏向我们的方案"的唯一办法——引用本来就看得见，藏不住，只能靠这条规则。</span>
</div>

<div class="bar">
  <span class="n" id="prog">已评 0/__TOTAL__</span>
  <button class="btn primary" id="export">导出结果 JSON</button>
  <button class="btn" id="dump">显示 JSON（下载被拦时用这个）</button>
  <button class="btn" id="reset">清空</button>
  <span class="note">草稿自动存在本机浏览器，关掉再开不会丢</span>
</div>
<textarea id="jsonBox" style="display:none;width:100%;height:200px;margin:10px 0;padding:8px;
  font:12px/1.5 ui-monospace,Consolas,monospace;border:1px solid var(--bd);border-radius:8px"></textarea>

<div id="list"></div>
</div>

<script>
const DATA = __DATA__;
const KEY = "p3_ab_score_v1";
let state = {};
try { state = JSON.parse(localStorage.getItem(KEY) || "{}"); } catch(e) { state = {}; }

function save(){ localStorage.setItem(KEY, JSON.stringify(state)); updateProg(); }
function updateProg(){
  const done = DATA.items.filter(it => state[it.id] && state[it.id].A && state[it.id].B &&
      state[it.id].A.grade !== undefined && state[it.id].B.grade !== undefined).length;
  document.getElementById("prog").textContent = `已评 ${done}/${DATA.items.length}`;
}
function esc(s){ return s.replace(/[&<>]/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;"}[c])); }

function armBlock(it, label, armKey){
  const st = (state[it.id] && state[it.id][label]) || {};
  const g = st.grade;
  const radios = [[2,"准确"],[1,"部分准确"],[0,"错误"]].map(([v,t]) =>
    `<label><input type="radio" name="${it.id}-${label}" value="${v}" ${g===v?"checked":""}
      data-q="${it.id}" data-arm="${label}" data-k="grade"> ${t}</label>`).join("");
  return `<div class="ans" id="box-${it.id}-${label}">
    <h4>答案 ${label}</h4>
    <div class="body">${esc(it.arms[armKey].answer || "（空）")}</div>
    <div class="score">
      ${radios}
      <label><input type="checkbox" ${st.fabricated?"checked":""}
        data-q="${it.id}" data-arm="${label}" data-k="fabricated"> 有编造</label>
      <span class="note">${(it.arms[armKey].answer||"").length} 字</span>
    </div>
  </div>`;
}

function render(){
  const host = document.getElementById("list");
  host.innerHTML = DATA.items.map((it,i) => {
    const kp = (it.key_points||[]).map(k => `<li>${esc(k)}</li>`).join("");
    return `<div class="card" id="card-${it.id}">
      <p class="q">${i+1}. ${esc(it.question)}<span class="tag">${it.type}</span></p>
      <div class="anchor"><div class="h">原文锚点：${esc(it.anchor.file)} 第 ${it.anchor.page} 页</div>
        ${esc(it.anchor_text || "（未取到原文，按下面要点判断）")}
        <div class="h" style="margin-top:8px">评分要点</div><ul class="kp">${kp}</ul></div>
      <div class="col2">
        ${armBlock(it, "甲", it.blind.A)}
        ${armBlock(it, "乙", it.blind.B)}
      </div>
    </div>`;
  }).join("");
  host.querySelectorAll("input").forEach(el => {
    el.addEventListener("change", () => {
      const q = el.dataset.q, arm = el.dataset.arm, k = el.dataset.k;
      state[q] = state[q] || {};
      state[q][arm] = state[q][arm] || {};
      state[q][arm][k] = (k === "grade") ? Number(el.value) : el.checked;
      if (k === "grade") document.getElementById(`box-${q}-${arm}`).classList.add("done");
      save();
    });
  });
  Object.keys(state).forEach(q => ["A","B"].forEach(a => {
    if (state[q][a] && state[q][a].grade !== undefined) {
      const b = document.getElementById(`box-${q}-${a}`); if (b) b.classList.add("done");
    }
  }));
  updateProg();
}

document.getElementById("export").addEventListener("click", () => {
  const payload = { scored_at: new Date().toISOString(), course: DATA.course, scores: state };
  const blob = new Blob([JSON.stringify(payload, null, 1)], {type:"application/json"});
  const a = document.createElement("a");
  a.href = URL.createObjectURL(blob);
  a.download = "scores.json";
  a.click();
});
document.getElementById("dump").addEventListener("click", () => {
  const box = document.getElementById("jsonBox");
  const payload = { scored_at: new Date().toISOString(), course: DATA.course, scores: state };
  box.value = JSON.stringify(payload, null, 1);
  box.style.display = "block";
  box.focus(); box.select();
  box.scrollIntoView({behavior:"smooth", block:"center"});
});
document.getElementById("reset").addEventListener("click", () => {
  if (!confirm("清空所有评分？")) return;
  state = {}; localStorage.removeItem(KEY); render();
});
render();
</script>
</body></html>
"""


def main() -> None:
    raw_path = OUT_DIR / "raw.json"
    if not raw_path.exists():
        raise SystemExit("没有 raw.json，先跑 scripts/p3_citation_ab.py")
    raw = json.loads(raw_path.read_text(encoding="utf-8"))

    questions = json.loads(
        (Path(__file__).resolve().parents[1] / "eval" / "citation_ab" / "questions.json")
        .read_text(encoding="utf-8")
    )
    meta = {it["id"]: it for it in questions["items"]}

    items = []
    for it in raw["items"]:
        anchor_text = _anchor_text(
            it["anchor"]["file"], it["anchor"]["page"], raw["meta"].get("kb_id")
        )
        items.append(
            {
                "id": it["id"],
                "type": it["type"],
                "question": it["question"],
                "anchor": it["anchor"],
                "anchor_text": anchor_text,
                # 评分要点用 questions.json 里的（raw.json 也有，以题集为准）
                "key_points": meta.get(it["id"], {}).get("key_points", it.get("key_points", [])),
                "blind": it["blind"],
                "arms": {
                    "cite": it["arms"]["cite"],
                    "noref": it["arms"]["noref"],
                },
            }
        )

    # 评分表只需要答案与题干，把 citations/timings 剥掉（体积小一半，也不引导评分人看引用）
    for it in items:
        for arm in it["arms"].values():
            arm.pop("checks", None)
            arm.pop("timings", None)
            arm.pop("hits", None)

    data = {"course": raw["meta"].get("course", "Ceph"), "items": items}
    out = TEMPLATE.replace("__DATA__", json.dumps(data, ensure_ascii=False)) \
                  .replace("__N__", str(len(items))) \
                  .replace("__TOTAL__", str(len(items) * 2))
    dest = OUT_DIR / "评分表.html"
    dest.write_text(out, encoding="utf-8")
    print(f"→ {dest}（{len(items)} 题 / {len(items) * 2} 个答案）")
    print("用浏览器打开，评完点「导出结果 JSON」，存到 output/eval_citation/scores.json")


if __name__ == "__main__":
    main()
