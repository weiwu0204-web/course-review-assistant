import { $, choices, item, when } from "./dom.js";
import { json, request } from "./api.js";

const statusLabel = {
  queued: "排队中", running: "生成中", done: "已完成", failed: "失败",
  partial: "部分完成", cancelled: "已取消",
};
let timer;
let sequence = 0;
let previousRequest;

function paint(batch) {
  const jobs = batch?.jobs ?? [];
  const inProgress = batch && ["queued", "running"].includes(batch.status);
  $("batch-state").textContent = batch ? statusLabel[batch.status] ?? batch.status : "";
  $("jobs").replaceChildren(...jobs.map(job => {
    const info = job.diagnostics;
    let coverage = "";
    if (info?.coverage) {
      coverage = info.coverage.reliable
        ? ` · 术语覆盖 ${info.coverage.percent}%（${info.coverage.missing_terms} 项待核对）`
        : " · 术语样本不足";
    }
    const repair = info?.repair;
    const repairLabel = repair ? (repair.rolled_back ? " · 保留补漏前版本" : " · 已完成补漏") : "";
    const detail = document.createElement("li");
    detail.textContent = `第 ${job.position} 节：${job.name}（v${job.revision}） · ${statusLabel[job.status]}${coverage}${repairLabel}`;
    const missing = info?.coverage?.missing_detail ?? [];
    if (missing.length) {
      const list = document.createElement("details");
      const label = document.createElement("summary");
      label.textContent = "定位疑似遗漏";
      const terms = document.createElement("ul");
      terms.replaceChildren(...missing.map(entry => item(`${entry.term} · 原课件第 ${entry.page} 页`)));
      list.append(label, terms);
      detail.append(list);
    }
    return detail;
  }));
  $("refresh-batch").disabled = !batch;
  $("retry-batch").disabled = !jobs.some(job => job.status === "failed");
  $("cancel-batch").disabled = !inProgress;
  $("download").disabled = !!inProgress || !jobs.some(job => job.status === "done");
}

export function resetBatch() {
  clearTimeout(timer);
  sequence++;
  previousRequest = undefined;
  $("batch").replaceChildren();
  paint();
}

export async function loadBatches(courseId, guard) {
  const rows = (await json("/batches?limit=100")).filter(row => row.course_id === courseId);
  if (!guard()) return;
  choices($("batch"), rows, "请选择任务", row => when(row.created_at) + " · " + row.id.slice(0, 7));
}

export async function refreshBatch(batchId, guard, report) {
  clearTimeout(timer);
  const call = ++sequence;
  if (!batchId) { paint(); return; }
  const active = () => guard() && $("batch").value === batchId && sequence === call;
  try {
    const result = await json("/batches/" + batchId);
    if (!active()) return;
    paint(result);
    if (["queued", "running"].includes(result.status)) {
      timer = setTimeout(() => void refreshBatch(batchId, guard, report), 1250);
    }
  } catch (error) {
    if (active()) report(error);
  }
}

export async function submitBatch(courseId, payload) {
  const fingerprint = JSON.stringify([courseId, payload]);
  if (previousRequest?.fingerprint !== fingerprint) {
    previousRequest = { fingerprint, key: crypto.randomUUID() };
  }
  const pending = previousRequest;
  const result = await json("/courses/" + courseId + "/batches", {
    method: "POST", body: payload, idempotencyKey: pending.key,
  });
  if (previousRequest === pending) previousRequest = undefined;
  return result;
}

export async function changeBatch(batchId, action) {
  await json(`/batches/${batchId}/${action}`, { method: "POST" });
}

export async function download(batchId, guard) {
  const response = await request(`/batches/${batchId}/download`);
  const blob = await response.blob();
  if (!guard()) return;
  const url = URL.createObjectURL(blob);
  const link = document.createElement("a");
  link.href = url;
  link.download = "复习手册.md";
  link.click();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}
