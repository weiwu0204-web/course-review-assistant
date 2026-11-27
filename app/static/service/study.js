import { $, choices, item, when } from "./dom.js";
import { json, readEvents, request } from "./api.js";

let asking;
let currentBases = new Map();
let selection = 0;

export function resetStudy() {
  asking?.abort();
  asking = undefined;
  selection++;
  currentBases = new Map();
  $("kb").replaceChildren();
  $("kb-summary").textContent = "";
  $("answer").textContent = "选择知识库后即可提问。";
  $("answer").classList.remove("error");
  $("sources").replaceChildren();
  $("history").replaceChildren();
  $("ask").disabled = true;
}

export async function loadBases(courseId, guard, selected = "") {
  const rows = await json(`/courses/${courseId}/knowledge-bases`);
  if (!guard()) return;
  currentBases = new Map(rows.map(row => [row.id, row]));
  choices($("kb"), rows, "请选择知识库", row =>
    when(row.created_at) + " · " + `${row.counts.pages} 页`);
  if (selected && currentBases.has(selected)) $("kb").value = selected;
  if (!$("kb").value && rows.length) $("kb").value = rows[0].id;
  await selectBase(guard);
}

export async function selectBase(guard) {
  const id = $("kb").value;
  const n = ++selection;
  asking?.abort();
  asking = undefined;
  const base = currentBases.get(id);
  $("answer").textContent = base ? "可以开始提问。" : "选择知识库后即可提问。";
  $("answer").classList.remove("error");
  $("sources").replaceChildren();
  $("history").replaceChildren();
  $("ask").disabled = !base;
  $("kb-summary").textContent = base
    ? base.sources.map(ref => `${ref.name} v${ref.revision}`).join("；") +
      (base.counts.failures?.length ? `（${base.counts.failures.length} 份未能提取）` : "")
    : "";
  if (base) await history(id, () => guard() && selection === n);
}

async function history(id, guard) {
  const rows = await json(`/knowledge-bases/${id}/history`);
  if (!guard()) return;
  $("history").replaceChildren(...rows.map(row => {
    const entry = document.createElement("li");
    const detail = document.createElement("details");
    const title = document.createElement("summary");
    title.textContent = row.question;
    const body = document.createElement("p");
    body.className = "answer";
    body.textContent = row.answer || "未找到相关内容。";
    detail.append(title, body);
    entry.append(detail);
    return entry;
  }));
}

export async function buildBase(courseId, materials) {
  return json(`/courses/${courseId}/knowledge-bases`, { method: "POST", body: { materials } });
}

export async function ask(question, guard) {
  const id = $("kb").value;
  if (!id) return;
  const controller = new AbortController();
  asking?.abort();
  asking = controller;
  const active = () => guard() && asking === controller && $("kb").value === id;
  $("ask").disabled = true;
  $("answer").classList.remove("error");
  $("answer").textContent = "正在检索课件…";
  $("sources").replaceChildren();
  let text = "", completed = false, failed = false;
  try {
    const res = await request(`/knowledge-bases/${id}/ask`, {
      method: "POST", body: { question }, signal: controller.signal,
    });
    await readEvents(res, event => {
      if (!active()) return;
      if (event.type === "sources") {
        const hits = event.hits || [];
        $("sources").replaceChildren(...hits.map(hit => item(`${hit.file} · 第 ${hit.page} 页`)));
        if (!hits.length) $("answer").textContent = event.note || "未找到相关内容。";
      } else if (event.type === "delta") {
        text += event.text;
        $("answer").textContent = text;
      } else if (event.type === "citations" && event.summary) {
        const { supported, total } = event.summary;
        $("sources").append(item(`引用有原文支持：${supported} / ${total}`));
      } else if (event.type === "error") {
        failed = true;
        $("answer").classList.add("error");
        $("answer").textContent = text ? text + "\n\n" + event.message : event.message;
      } else if (event.type === "done") {
        completed = true;
      }
    });
    if (!active()) return;
    if (!failed && !completed) throw new Error("回答连接中断，请重试。");
    if (completed) await history(id, active);
  } catch (error) {
    if (error.name !== "AbortError" && active()) throw error;
  } finally {
    if (active()) {
      asking = undefined;
      $("ask").disabled = false;
    }
  }
}
