import { $, choices, option } from "./dom.js";
import { ApiError, json, setSession } from "./api.js";
import { changeBatch, download, loadBatches, refreshBatch, resetBatch, submitBatch } from "./handbook.js";
import { ask, buildBase, loadBases, resetStudy, selectBase } from "./study.js";

let account = "", course = "", view = 0, session = 0;
let materials = [];
const selected = new Set();

function notice(text, error = false) {
  $("notice").textContent = text;
  $("notice").dataset.error = error;
  $("notice").hidden = !text;
}

function resetAccount() {
  account = "";
  setSession("");
  session++;
  view++;
  course = "";
  materials = [];
  selected.clear();
  resetBatch();
  resetStudy();
  $("course").replaceChildren();
  $("materials").replaceChildren();
  $("account-name").textContent = "";
  $("stats").textContent = "";
  $("repair-option").hidden = true;
  $("upload-form").reset();
  $("batch-form").reset();
  $("ask-form").reset();
  $("course-form").reset();
  $("login-form").reset();
  for (const control of document.querySelectorAll("form button[type=submit], #build-kb")) {
    control.disabled = control.id === "ask";
  }
  $("workspace").hidden = true;
  $("logout").hidden = true;
  $("login-panel").hidden = false;
}

function report(error) {
  if (error instanceof ApiError && error.status === 401 && account) {
    resetAccount();
    $("login-form").elements.username.focus();
    notice("登录已过期，请重新登录。", true);
  } else notice(error.message || "请求未完成，请稍后重试。", true);
}

function bind(id, eventName, work) {
  $(id).addEventListener(eventName, async event => {
    event.preventDefault();
    const activeSession = session;
    const control = event.submitter || (event.currentTarget.tagName === "BUTTON" ? event.currentTarget : null);
    if (control) control.disabled = true;
    notice("");
    try {
      await work(event.currentTarget);
    } catch (error) {
      if (session === activeSession) report(error);
    } finally {
      if (control && session === activeSession &&
          !["ask", "refresh-batch", "download", "retry-batch", "cancel-batch"].includes(control.id)) {
        control.disabled = false;
      }
    }
  });
}

const viewGuard = () => {
  const target = view;
  return () => target === view && !!account;
};

async function stats() {
  const expected = session;
  const rollup = await json("/stats");
  if (expected === session) {
    $("stats").textContent = `手册任务 ${rollup.batch_count} · 知识库 ${rollup.kb_count} · 问答 ${rollup.qa_count}`;
  }
}

function paintMaterials() {
  for (const id of selected) if (!materials.some(row => row.id === id)) selected.delete(id);
  $("materials").replaceChildren(...materials.map(row => {
    const label = document.createElement("label");
    label.className = "item-check";
    const name = document.createElement("span");
    name.textContent = `${row.name} · v${row.revision}${row.kind === "exam" ? "（试题）" : ""}`;
    if (row.kind === "courseware") {
      const box = document.createElement("input");
      box.type = "checkbox";
      box.value = row.id;
      box.checked = selected.has(row.id);
      box.addEventListener("change", () => box.checked ? selected.add(row.id) : selected.delete(row.id));
      label.append(box);
    }
    label.append(name);
    return label;
  }));
  const exams = materials.filter(row => row.kind === "exam");
  choices($("batch-form").elements.exam, exams, "不附试题", row => `${row.name} v${row.revision}`);
  paintUpload();
}

function paintUpload() {
  const form = $("upload-form");
  const kind = form.elements.kind.value;
  choices(form.elements.material, materials.filter(row => row.kind === kind), "添加新资料",
          row => `发布新版：${row.name} v${row.revision}`);
  form.elements.file.accept = kind === "exam" ? ".txt,.md,.pdf" : ".pptx,.pdf";
}

async function loadMaterials(cid, guard) {
  const rows = await json("/courses/" + cid + "/materials");
  if (!guard()) return;
  materials = rows;
  paintMaterials();
}

async function openCourse(id) {
  view++;
  course = id;
  materials = [];
  selected.clear();
  resetBatch();
  resetStudy();
  paintMaterials();
  $("course-tools").hidden = !id;
  $("course-empty").hidden = !!id;
  if (!id) return;
  const guard = viewGuard();
  try {
    await Promise.all([loadMaterials(id, guard), loadBatches(id, guard), loadBases(id, guard)]);
  } catch (error) {
    if (guard()) throw error;
  }
}

async function courses(preferred) {
  const current = session;
  const rows = await json("/courses");
  if (session !== current) return;
  $("course").replaceChildren(...rows.map(row => option(row.id, row.title)));
  $("course").value = rows.some(row => row.id === preferred) ? preferred : rows[0]?.id ?? "";
  await openCourse($("course").value);
}

function chosen() {
  if (!selected.size) throw new Error("请至少勾选一份课件。");
  if (selected.size > 20) throw new Error("一次最多选择 20 份课件。");
  return [...selected];
}

bind("login-form", "submit", async form => {
  const data = Object.fromEntries(new FormData(form));
  const login = await json("/sessions", { method: "POST", body: data });
  setSession(login.token);
  account = data.username;
  form.reset();
  $("login-panel").hidden = true;
  $("workspace").hidden = false;
  $("logout").hidden = false;
  $("account-name").textContent = account;
  const expected = session;
  await Promise.all([courses(), stats(), json("/capabilities").then(data => {
    if (session === expected) $("repair-option").hidden = !data.auto_repair;
  })]);
});

bind("logout", "click", async () => {
  try { await json("/sessions/current", { method: "DELETE" }); }
  finally {
    resetAccount();
    $("logout").disabled = false;
    $("login-form").elements.username.focus();
  }
});

bind("course-form", "submit", async form => {
  const expected = session;
  const value = await json("/courses", { method: "POST", body: { title: form.elements.title.value } });
  if (session !== expected) return;
  form.reset();
  await courses(value.id);
});

bind("course", "change", select => openCourse(select.value));
$("upload-form").elements.kind.addEventListener("change", paintUpload);

bind("upload-form", "submit", async form => {
  if (!course) return;
  const cid = course, guard = viewGuard();
  const target = materials.find(row => row.id === form.elements.material.value);
  const data = new FormData();
  data.set("file", form.elements.file.files[0]);
  data.set("base_revision", target?.revision ?? 0);
  data.set("kind", form.elements.kind.value);
  const root = `/courses/${cid}/materials`;
  try {
    const row = await json(target ? `${root}/${target.id}/revisions` : root, { method: "POST", body: data });
    if (!guard()) return;
    form.elements.file.value = "";
    if (row.kind === "courseware") selected.add(row.id);
    await loadMaterials(cid, guard);
    if (guard()) notice(`${row.name} 已发布 v${row.revision}。`);
  } catch (error) {
    if (guard()) {
      if (error.status === 409) await loadMaterials(cid, guard);
      throw error;
    }
  }
});

bind("batch-form", "submit", async form => {
  const cid = course, guard = viewGuard();
  const payload = {
    materials: chosen(), exam: form.elements.exam.value || null, exam_mode: form.elements.exam_mode.value,
    template: form.elements.template.value, style: form.elements.style.value,
    use_vision: form.elements.use_vision.checked, enable_repair: form.elements.enable_repair.checked,
  };
  const result = await submitBatch(cid, payload);
  if (!guard()) return;
  await loadBatches(cid, guard);
  if (!guard()) return;
  $("batch").value = result.id;
  await Promise.all([refreshBatch(result.id, guard, report), stats()]);
});

bind("batch", "change", select => refreshBatch(select.value, viewGuard(), report));
bind("refresh-batch", "click", () => refreshBatch($("batch").value, viewGuard(), report));
for (const [id, action] of [["retry-batch", "retry"], ["cancel-batch", "cancel"]]) {
  bind(id, "click", async () => {
    const batch = $("batch").value, guard = viewGuard();
    try { await changeBatch(batch, action); }
    finally { if (guard() && $("batch").value === batch) await refreshBatch(batch, guard, report); }
  });
}
bind("download", "click", async button => {
  const batch = $("batch").value, guard = viewGuard();
  const active = () => guard() && $("batch").value === batch;
  try { await download(batch, active); }
  finally { if (active()) button.disabled = false; }
});

bind("build-kb", "click", async () => {
  const cid = course, guard = viewGuard();
  const materials = chosen();
  notice("正在读取并整理所选课件…");
  const built = await buildBase(cid, materials);
  if (!guard()) return;
  await Promise.all([loadBases(cid, guard, built.id), stats()]);
  if (guard()) notice("知识库已就绪。");
});

bind("kb", "change", () => selectBase(viewGuard()));
bind("ask-form", "submit", async form => {
  const question = form.elements.question.value.trim();
  if (!question) { $("ask").disabled = false; throw new Error("请输入问题。"); }
  if (!$("kb").value) return;
  const guard = viewGuard();
  await ask(question, guard);
  if (guard()) await stats();
});
