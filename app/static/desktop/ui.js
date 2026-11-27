  let currentFile = null;
  let currentMarkdown = ""; // 原始 Markdown 原文，供复制/下载用
  let examId = null;        // 已解析的往年试题 id
  let examChars = 0;        // 往年试题字符数（成本预估用）
  let examFile = null;      // 用户选的往年试题文件
  const $ = (id) => document.getElementById(id);
  const statusEl = $("status");

  function setStatus(msg, type = "") {
    statusEl.textContent = msg;
    statusEl.className = type;
  }

  // 把后端文案里的 URL 渲染成可点链接：先转义再链接化，最后 DOMPurify 兜底。
  // 只用于后端可控文案（错误提示等），用户输入/模型输出一律走 textContent。
  function linkifyHtml(text) {
    const esc = String(text)
      .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;");
    const linked = esc.replace(
      /(https?:\/\/[^\s<>"']+)/g,
      '<a href="$1" target="_blank" rel="noopener">$1</a>'
    );
    return DOMPurify.sanitize(linked);
  }

  // ---- 生成进度 / 结果弹窗 ----
  const modalMask = $("modalMask");
  const modalTextEl = $("modalText");
  const modalSubEl = $("modalSub");
  const modalTimerEl = $("modalTimer");
  let modalTimerId = null;
  let modalStartAt = 0;
  let modalExpectedSec = 90;   // 当前阶段的预计秒数，用于超时提示
  let slowTipShown = false;

  function fmtSize(bytes) {
    if (bytes < 1024) return bytes + " B";
    if (bytes < 1024 * 1024) return (bytes / 1024).toFixed(0) + " KB";
    return (bytes / 1024 / 1024).toFixed(1) + " MB";
  }

  // 打开进度弹窗并开始计时
  function openProgressModal(text, sub, expectedSec) {
    modalMask.classList.remove("hidden");
    $("mIcoSpin").classList.remove("hidden");
    $("mIcoOk").classList.add("hidden");
    $("mIcoFail").classList.add("hidden");
    $("modalFoot").classList.add("hidden");
    $("modalTitle").textContent = "正在生成复习文档";
    $("modalTimer").classList.remove("hidden");
    setPhase(text, sub, expectedSec);
    if (modalTimerId) clearInterval(modalTimerId);
    modalStartAt = Date.now();
    modalTimerId = setInterval(tickModalTimer, 500);
    tickModalTimer();
  }

  // 进程内切换阶段文案（extract → generate），计时不中断
  function setPhase(text, sub, expectedSec) {
    modalTextEl.textContent = text;
    modalSubEl.textContent = sub || "";
    if (expectedSec) modalExpectedSec = expectedSec;
    slowTipShown = false;
  }

  function tickModalTimer() {
    const secs = Math.floor((Date.now() - modalStartAt) / 1000);
    const mm = Math.floor(secs / 60);
    const ss = String(secs % 60).padStart(2, "0");
    modalTimerEl.textContent = `已用时 ${mm}:${ss}`;
    // 明显超过预计时间仍无结果时，给一句提示而不是让用户干等
    if (!slowTipShown && secs >= Math.max(150, modalExpectedSec + 60)) {
      slowTipShown = true;
      modalSubEl.textContent =
        (modalSubEl.textContent ? modalSubEl.textContent + " " : "") +
        "已超过通常耗时，若长时间无响应可关闭窗口后重试。";
    }
  }

  // 收尾：成功（可自动关闭并展示文档）或失败（可手动关闭）。
  // showClose=true 时成功也保留"关闭"按钮，用于下载结果这类需要用户
  // 看清路径再关的提示；生成流程不带该参数，成功后由调用方自动关闭。
  // html=true 时 text 按安全 HTML 渲染（支持余额不足等提示里的充值链接）。
  // retryable=true 且失败时额外显示「重试」按钮（瞬时网络/服务端错误专用）。
  function showResultModal(ok, title, text, showClose = false, html = false, retryable = false) {
    if (modalTimerId) { clearInterval(modalTimerId); modalTimerId = null; }
    modalMask.classList.remove("hidden"); // 不经进度弹窗直接调用的场景（下载提示）也要保证可见
    $("modalTitle").textContent = title;
    if (html) modalTextEl.innerHTML = linkifyHtml(text);
    else modalTextEl.textContent = text;
    modalSubEl.textContent = "";
    $("mIcoSpin").classList.add("hidden");
    $("mIcoOk").classList.toggle("hidden", !ok);
    $("mIcoFail").classList.toggle("hidden", ok);
    $("modalTimer").classList.add("hidden");
    // 仅失败的瞬时错误显示重试；成功态或参数类错误（4xx）不给
    $("modalRetryBtn").classList.toggle("hidden", ok || !retryable);
    $("modalFoot").classList.toggle("hidden", ok && !showClose);
  }

  function closeModal() {
    modalMask.classList.add("hidden");
    if (modalTimerId) { clearInterval(modalTimerId); modalTimerId = null; }
  }
  $("modalCloseBtn").addEventListener("click", closeModal);

  // ---- 文档类型 × 输出风格联动 ----
  // 习题生成不提供「表格化」（题目不是汇总表），风格文案也换成卷面语境；
  // 复习指南/重点总结保留三种通用风格（表格化对提炼重点、对比易错点仍有用）。
  // 单份页与批量页各有一套「文档类型/输出风格」下拉，必须都接上这套联动——
  // 批量页当初漏接，导致选了习题生成仍能看到「表格化」（2026-09-11 用户实测反馈）。
  const STYLE_OPTIONS = {
    "复习指南": [["详细", "详细"], ["精简", "精简"], ["表格化", "表格化"]],
    "重点总结": [["详细", "详细"], ["精简", "精简"], ["表格化", "表格化"]],
    "习题生成": [
      ["详细", "详解版：每题附答案与解析"],
      ["精简", "精简版：答案速查，不展开长篇解析"],
    ],
  };
  const STYLE_LABELS = { "习题生成": "卷面风格" };

  function wireStyleOptions(templateSel, styleSel, labelEl) {
    function rebuild() {
      const tpl = templateSel.value;
      const opts = STYLE_OPTIONS[tpl] || STYLE_OPTIONS["复习指南"];
      const prev = styleSel.value;
      styleSel.innerHTML = "";
      for (const [val, text] of opts) {
        const o = document.createElement("option");
        o.value = val;
        o.textContent = text;
        styleSel.appendChild(o);
      }
      // 当前值不在新选项里（例如从「表格化」切到习题生成）时落到第一项
      if (!opts.some(([val]) => val === prev)) styleSel.value = opts[0][0];
      labelEl.textContent = STYLE_LABELS[tpl] || "输出风格";
    }
    templateSel.addEventListener("change", rebuild);
    rebuild(); // 页面加载即对齐一次
  }

  wireStyleOptions($("template"), $("style"), $("styleLabel"));
  wireStyleOptions($("bTemplate"), $("bStyle"), $("bStyleLabel"));

  // 上传区交互
  const dropZone = $("dropZone");
  const fileInput = $("fileInput");
  dropZone.addEventListener("click", () => fileInput.click());
  dropZone.addEventListener("dragover", (e) => { e.preventDefault(); dropZone.classList.add("dragging"); });
  dropZone.addEventListener("dragleave", () => dropZone.classList.remove("dragging"));
  dropZone.addEventListener("drop", (e) => {
    e.preventDefault(); dropZone.classList.remove("dragging");
    if (e.dataTransfer.files.length) handleFile(e.dataTransfer.files[0]);
  });
  fileInput.addEventListener("change", () => { if (fileInput.files.length) handleFile(fileInput.files[0]); });

  // ---- 往年试题：选择文件 / 解析 ----
  const examFileBtn = $("examFileBtn");
  const examFileInput = $("examFileInput");
  examFileBtn.addEventListener("click", () => examFileInput.click());
  examFileInput.addEventListener("change", () => {
    examFile = examFileInput.files.length ? examFileInput.files[0] : null;
    $("examFileName").textContent = examFile ? `已选：${examFile.name}` : "";
  });

  $("parseExamBtn").addEventListener("click", async () => {
    const textValue = $("examText").value.trim();
    if (!examFile && !textValue) {
      setStatus("请先粘贴试题文本或选择试题文件", "error");
      return;
    }
    const btn = $("parseExamBtn");
    btn.disabled = true;
    setStatus("正在解析试题...");
    try {
      const form = new FormData();
      if (examFile) form.append("file", examFile);
      else form.append("text", textValue);
      const res = await fetch("/api/exam", { method: "POST", body: form });
      if (!res.ok) throw new Error((await res.json()).detail || "解析失败");
      const data = await res.json();
      examId = data.exam_id;
      examChars = data.chars || 0;
      const meta = $("examMeta");
      meta.textContent = `已载入《${data.filename}》 ${data.chars} 字符 · 预览：${data.preview}${data.chars > 200 ? "…" : ""}`;
      meta.classList.remove("hidden");
      $("examMode").disabled = false;
      setStatus("试题已就绪，可开始生成", "success");
    } catch (err) {
      examId = null;
      examChars = 0;
      setStatus(err.message, "error");
    } finally {
      btn.disabled = false;
    }
  });

  // 把 API 错误转成带 HTTP status 的 Error：fetch 网络层失败（无 status）与
  // 服务端 5xx 属于瞬时错误、可一键重试；4xx 是内容/配置问题，重试无意义。
  async function throwApiError(res, fallback) {
    let detail = fallback;
    try { detail = (await res.json()).detail || fallback; } catch { /* 非 JSON 响应 */ }
    const e = new Error(detail);
    e.status = res.status;
    throw e;
  }

  // 支持的课件格式：与后端 extractor.SUPPORTED_EXTENSIONS 保持一致
  const COURSEWARE_RE = /\.(pptx|pdf)$/i;

  async function handleFile(file) {
    if (!COURSEWARE_RE.test(file.name)) {
      setStatus("请选择 .pptx / .pdf 课件；旧版 .ppt 请先另存为 .pptx", "error");
      return;
    }
    currentFile = file;
    // 整个虚线框切换成「已就绪」实线卡片，反馈要明显到不可能看漏
    dropZone.classList.add("ready");
    $("dropIdle").classList.add("hidden");
    $("fileReady").classList.remove("hidden");
    $("fName").textContent = file.name;
    $("fSize").textContent = `${fmtSize(file.size)} · PPTX`;
    $("fileInput").value = ""; // 允许再次选择同一个文件时也触发 change
    $("fileMeta").classList.add("hidden");
    $("generateBtn").disabled = false;
    setStatus("", "");
  }

  // 完整生成流程：上传解析 → 成本预估 → AI 生成。抽成命名函数，
  // 「生成」按钮与失败弹窗的「重试」按钮共用（重试复用已选文件/试题，不丢状态）。
  async function startGenerate() {
    if (!currentFile) return;
    const btn = $("generateBtn");
    btn.disabled = true;
    const useVision = $("useVision").checked;
    const hasExam = examId != null;
    const isPredict = hasExam && $("examMode").value === "predict";

    // 第一步：上传并解析课件（快，几秒）
    openProgressModal("正在上传并解析课件内容…", "", 40);
    try {
      const form = new FormData();
      form.append("file", currentFile);
      const res = await fetch("/api/extract", { method: "POST", body: form });
      if (!res.ok) await throwApiError(res, "上传失败");
      const extracted = await res.json();
      const displayName = extracted.filename.split("/").pop();
      $("fileMeta").textContent =
        `《${displayName}》 共 ${extracted.total_pages} 页 · ` +
        `${extracted.image_count} 张图 · 备注 ${extracted.notes_chars} 字`;
      $("fileMeta").classList.remove("hidden");

      // 第二步：AI 生成（40~90s，耗时大头）。按实际情况给出动作与预计时长
      const imgCount = extracted.image_count;
      const hasVision = useVision && imgCount > 0;
      let genText, genSub, expectedSec;
      if (hasVision) {
        genText = `正在读取 ${imgCount} 张图片并撰写文档`;
        genSub = "图片识别较慢，预计 1~2 分钟，请勿关闭窗口";
        expectedSec = 110;
      } else {
        genText = "正在根据课件内容撰写文档";
        genSub = "预计 30 秒 ~ 1 分钟，请勿关闭窗口";
        expectedSec = 60;
      }
      if (hasExam) {
        if (isPredict) {
          genText += "，并仿照往年题风格出预测题";
          genSub = "预计 1~2 分钟，请勿关闭窗口";
          expectedSec = 120;
        } else {
          genText += "，并整理往年试题";
          genSub = "预计 1 分钟左右，请勿关闭窗口";
          expectedSec = 90;
        }
      }

      // 调用模型前先粗估费用——把「大概花多少」提前告诉用户，余额不足就不该是意外
      let costSuffix = "";
      try {
        const totalChars = (extracted.slides || []).reduce(
          (s, sl) => s + (sl.text || "").length + (sl.notes || "").length, 0
        );
        const estRes = await fetch("/api/settings/estimate", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            chars: totalChars,
            image_count: imgCount,
            use_vision: useVision,
            thinking: $("useThinking").checked,
            exam_chars: examChars,
            style: $("style").value,
            pages: extracted.total_pages || 0,
          }),
        });
        if (estRes.ok) {
          const est = await estRes.json();
          if (est.cost_text) costSuffix = ` · 本次预计花费${est.cost_text}`;
        }
      } catch { /* 预估失败不影响主流程 */ }
      genSub += costSuffix;

      setPhase(genText, genSub, expectedSec);

      const genRes = await fetch("/api/generate", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          filename: extracted.filename,
          template: $("template").value,
          style: $("style").value,
          use_vision: useVision,
          thinking: $("useThinking").checked,
          attach_coverage: $("useAttachCov").checked,
          enable_repair: $("useRepair").checked,
          ...(examId ? { exam_id: examId, exam_mode: $("examMode").value } : {}),
        }),
      });
      if (!genRes.ok) await throwApiError(genRes, "生成失败");
      const data = await genRes.json();

      const parts = [];
      if (data.exam_used) parts.push("已结合往年试题");
      if (data.used_vision) parts.push(`读取了 ${data.image_count} 张图片`);
      const tail = parts.length ? `（${parts.join(" · ")}）` : "";
      const truncNote = data.truncated
        ? "输出过长被截断，文档末尾（通常是题目章节）可能缺失，建议改用精简风格重试。"
        : "";
      // 弹成功结果 → 1 秒后自动关闭，滚动到文档
      showResultModal(true, data.truncated ? "已生成（内容可能不完整）" : "生成完成", `文档已生成${tail}`);
      setTimeout(() => {
        closeModal();
        $("batchSectionBar").classList.add("hidden");  // 清掉批量结果的节切换残留
        $("resultTitle").textContent = "复习文档";
        renderMarkdown(data.markdown);
        renderCoverageBox(
          $("covBox"), data.coverage, data.coverage_note, repairText(data.repair)
        );
        setStatus(truncNote ? `生成完成，但${truncNote.replace("。", "")}${tail}` : `生成完成${tail}`, data.truncated ? "error" : "success");
      }, 1000);
    } catch (err) {
      const raw = (err && err.message) || "生成失败";
      // 网络层失败（fetch 无 status）或服务端 5xx → 瞬时问题，给「重试」；
      // 4xx（未配 key / PPT 无内容 / 文件丢失等）是配置或内容问题，重试无意义
      const retryable = !err.status || err.status >= 500;
      const friendly = /failed to fetch|networkerror|load failed|aborted/i.test(raw)
        ? "网络连接中断，请点下方「重试」再试；若持续失败请检查网络连接。"
        : raw;
      // html=true：错误文案可能带充值链接/服务商指引，渲染为可点链接
      showResultModal(false, "生成失败", friendly, false, true, retryable);
      setStatus(friendly, "error");
    } finally {
      btn.disabled = false;
    }
  }

  $("generateBtn").addEventListener("click", startGenerate);
  // 失败弹窗「重试」：直接重跑整条流程（失败请求不计费，重复点击安全）
  $("modalRetryBtn").addEventListener("click", () => startGenerate());

  // ---- 通用确认弹窗（思考模式开启提醒等「操作前确认」场景）----
  let confirmOkCb = null;
  let confirmCancelCb = null;
  function openConfirm(title, text, onOk, onCancel) {
    $("confirmTitle").textContent = title;
    $("confirmText").textContent = text;
    $("confirmMask").classList.remove("hidden");
    confirmOkCb = onOk || null;
    confirmCancelCb = onCancel || null;
  }
  function closeConfirm() {
    $("confirmMask").classList.add("hidden");
    confirmOkCb = null;
    confirmCancelCb = null;
  }
  $("confirmOkBtn").addEventListener("click", () => {
    const cb = confirmOkCb;
    closeConfirm();
    if (cb) cb();
  });
  $("confirmCancelBtn").addEventListener("click", () => {
    const cb = confirmCancelCb;
    closeConfirm();
    if (cb) cb();
  });

  // 思考模式：默认关闭（省钱）。勾选时提醒费用与耗时增加；点「取消」则撤回勾选。
  const useThinkingEl = $("useThinking");
  useThinkingEl.addEventListener("change", () => {
    if (!useThinkingEl.checked) return;
    openConfirm(
      "开启思考模式？",
      "开启后模型会先深度推理再输出，生成质量更高（尤其习题生成、真题整理），\n但费用约增加 30%~50%，耗时明显变长。仍要开启吗？",
      () => setStatus("思考模式已开启：本次生成会更慢、更贵，追求省钱可取消勾选", ""),
      () => { useThinkingEl.checked = false; }  // 取消 → 撤回勾选，保持默认省钱态
    );
  });

  // ---- 覆盖率回检展示（P1）----
  // 判据是术语级的**词面**比对（见 app/services/coverage.py），阈值 94/86 按已知
  // 质量的参照文档标定。对外一律说「疑似未覆盖」——术语没出现也可能只是换了个说法，
  // 所以清单里必定附源课件页码，让用户自己一眼确认。
  const COV_MIN_TERMS = 8;   // 与后端 MIN_TERMS_FOR_PERCENT 一致：术语太少时百分比是噪声
  function covReliable(cov) {
    return cov.reliable !== false && (cov.total_terms || 0) >= COV_MIN_TERMS;
  }
  function covClass(percent, reliable) {
    if (!reliable) return "cov-none";
    if (percent >= 94) return "cov-ok";
    if (percent >= 86) return "cov-warn";
    return "cov-bad";
  }
  // P2：把补漏记录翻成一句人话。空串 = 没触发或没改善（不显示，别噪音）。
  // 「回滚」要如实说——用户得知道"试过但没变好"。
  function repairText(repair) {
    if (!repair || !repair.triggered) return "";
    const before = repair.trigger_percent >= 0 ? repair.trigger_percent : 0;
    const after = repair.final_percent >= 0 ? repair.final_percent : 0;
    if (after <= before) {
      return repair.rolled_back ? "已尝试自动补漏但未改善，保留原文档" : "";
    }
    return (
      "已自动补漏 " + (repair.rounds || []).length + " 轮：" +
      before + "% → " + after + "%，¥" + (repair.total_cost_yuan || 0).toFixed(3)
    );
  }
  function repairBadge(text) {
    if (!text) return null;
    const b = document.createElement("span");
    b.className = "covrepair";
    b.textContent = text;
    return b;
  }

  function covBadge(cov) {
    const rel = covReliable(cov);
    const b = document.createElement("span");
    b.className = "cov " + covClass(cov.percent, rel);
    b.textContent = rel ? `覆盖 ${cov.percent}%` : "覆盖样本不足";
    return b;
  }
  // 「疑似未覆盖」明细行（不含外层容器），单份直接有数据、批量按需取
  function covDetailLines(cov) {
    const frag = document.createDocumentFragment();
    const missing = cov.missing_detail || [];
    if (!missing.length) return frag;
    const hint = document.createElement("div");
    hint.textContent = "术语级比对，对照源课件页码核对即可：「疑似」不等于一定漏，可能只是换了个说法。";
    frag.appendChild(hint);
    missing.forEach((m) => {
      const line = document.createElement("div");
      const code = document.createElement("code");
      code.textContent = m.term;
      line.appendChild(code);
      line.appendChild(document.createTextNode(` 源课件第 ${m.page} 页`));
      frag.appendChild(line);
    });
    return frag;
  }
  function covToggleText(n, open) {
    return `疑似未覆盖 ${n} 项 ${open ? "▴" : "▾"}`;
  }
  // 单份结果：覆盖率就在生成响应里，直接渲染
  function renderCoverageBox(box, cov, note, repText) {
    box.innerHTML = "";
    if (!cov) {
      // 没有数字时不能只是沉默：解释一句，否则用户以为功能坏了
      if (note) {
        box.classList.remove("hidden");
        const n = document.createElement("div");
        n.className = "covdetail";
        n.textContent = note;
        box.appendChild(n);
      } else {
        box.classList.add("hidden");
      }
      return;
    }
    box.classList.remove("hidden");
    const line = document.createElement("div");
    line.className = "covline";
    line.appendChild(covBadge(cov));
    const rep = repairBadge(repText);   // P2：补漏提示跟在覆盖率徽标后面
    if (rep) line.appendChild(rep);
    const detail = document.createElement("div");
    detail.className = "covdetail hidden";
    detail.appendChild(covDetailLines(cov));
    if (!(cov.missing_terms || 0)) {
      const none = document.createElement("span");
      none.className = "covtoggle";
      none.style.color = "var(--muted)";
      none.textContent = "无疑似遗漏";
      line.appendChild(none);
    } else {
      const t = document.createElement("span");
      t.className = "covtoggle";
      t.textContent = covToggleText(cov.missing_terms, false);
      t.addEventListener("click", () => {
        const open = !detail.classList.toggle("hidden");
        t.textContent = covToggleText(cov.missing_terms, open);
      });
      line.appendChild(t);
    }
    box.appendChild(line);
    box.appendChild(detail);
  }
  // 批量某节：batch.json 只带摘要，明细要按需向 /coverage/{idx} 取（点开才请求）
  function attachJobCoverage(col, j) {
    const cov = {
      percent: j.coverage_percent, total_terms: j.coverage_total || 0,
      missing_terms: j.coverage_missing || 0,
      reliable: (j.coverage_total || 0) >= COV_MIN_TERMS,
    };
    const line = document.createElement("div");
    line.className = "covline";
    line.appendChild(covBadge(cov));
    const rep = repairBadge(j.repair_summary || "");   // P2：后端已压成一行
    if (rep) line.appendChild(rep);
    const holder = document.createElement("div");
    holder.className = "covdetail hidden";
    if (!cov.missing_terms) {
      const none = document.createElement("span");
      none.className = "covtoggle";
      none.style.color = "var(--muted)";
      none.textContent = "无疑似遗漏";
      line.appendChild(none);
    } else {
      const t = document.createElement("span");
      t.className = "covtoggle";
      t.textContent = covToggleText(cov.missing_terms, false);
      let loaded = false;
      t.addEventListener("click", async () => {
        if (!loaded) {
          loaded = true;
          t.textContent = "加载中…";
          try {
            const res = await fetch(`/api/batch/${batchId}/coverage/${j.idx}`);
            holder.innerHTML = "";
            if (res.ok) holder.appendChild(covDetailLines(await res.json()));
            else holder.textContent = "这一节没有覆盖率明细。";
          } catch { holder.textContent = "加载失败，请重试。"; }
        }
        const open = !holder.classList.toggle("hidden");
        t.textContent = covToggleText(cov.missing_terms, open);
      });
      line.appendChild(t);
    }
    col.appendChild(line);
    col.appendChild(holder);
  }

  function renderMarkdown(md, opts = {}) {
    currentMarkdown = md;
    // marked 渲染 Markdown，DOMPurify 清洗掉模型输出里的危险 HTML/链接
    const html = DOMPurify.sanitize(marked.parse(md));
    $("markdown").innerHTML = html;
    $("resultCard").classList.remove("hidden");
    if (opts.scroll !== false) $("resultCard").scrollIntoView({ behavior: "smooth" });
  }

  $("copyBtn").addEventListener("click", async () => {
    try {
      await navigator.clipboard.writeText(currentMarkdown);
      setStatus("已复制到剪贴板", "success");
    } catch { setStatus("复制失败，请手动复制", "error"); }
  });

  $("downloadBtn").addEventListener("click", async () => {
    await saveFile("复习文档.md", currentMarkdown, "text/markdown;charset=utf-8");
  });
  $("downloadHtmlBtn").addEventListener("click", async () => {
    const bodyHtml = DOMPurify.sanitize(marked.parse(currentMarkdown));
    const html = `<!DOCTYPE html><html lang="zh-CN"><head><meta charset="utf-8"><title>复习文档</title>
<style>body{font-family:-apple-system,"Segoe UI","PingFang SC","Microsoft YaHei",sans-serif;max-width:860px;margin:0 auto;padding:32px 20px;line-height:1.8;color:#1f2328;font-size:15px}h1{font-size:24px;border-bottom:1px solid #d8dee4;padding-bottom:8px;font-weight:600}h2{font-size:19px;margin-top:26px}h3{font-size:16px;margin-top:18px}table{border-collapse:collapse;margin:12px 0;width:100%}th,td{border:1px solid #d8dee4;padding:7px 10px;text-align:left;vertical-align:top}th{background:#f5f6f7}code{background:#eef0f1;padding:1px 5px;border-radius:4px;font-family:Consolas,monospace}pre{background:#1f2328;color:#e6e6e6;padding:12px 14px;border-radius:6px;overflow-x:auto}pre code{background:none;color:inherit;padding:0}blockquote{border-left:3px solid #c0c6cc;margin:10px 0;padding:2px 12px;color:#656d76}img{max-width:100%}ul,ol{padding-left:24px}li{margin:3px 0}</style></head><body>${bodyHtml}</body></html>`;
    await saveFile("复习文档.html", html, "text/html;charset=utf-8");
  });

  function download(content, filename, type) {
    const blob = new Blob([content], { type });
    const a = document.createElement("a");
    a.href = URL.createObjectURL(blob);
    a.download = filename;
    document.body.appendChild(a);
    a.click();
    document.body.removeChild(a);
    setTimeout(() => URL.revokeObjectURL(a.href), 1000);
  }

  async function saveFile(filename, content, mime) {
    // pywebview（桌面壳）默认禁用下载，<a download> 点击会被静默吞掉——
    // 改走 js_api 让 Python 端写入系统"下载"目录，并把完整路径显示出来。
    // 普通浏览器直连则用原生 Blob 下载（浏览器自带下载反馈）。
    if (window.pywebview && window.pywebview.api && window.pywebview.api.save_document) {
      let res;
      try {
        res = await window.pywebview.api.save_document(filename, content);
      } catch (err) {
        // 弹窗提示而不是状态栏小字——小字在按钮下方，用户可能看不到而重复点击
        showResultModal(false, "保存失败", `写入文件时出错：${err && err.message ? err.message : "未知错误"}`);
        return;
      }
      if (res && res.ok) {
        showResultModal(true, "保存成功", `文件已保存到：\n${res.path}`, true);
      } else {
        const msg = res && res.error ? res.error : "保存失败，请重试";
        showResultModal(false, "保存失败", msg);
      }
      return;
    }
    download(content, filename, mime);
  }

  // ---- API Key 设置（首次使用强制配置，之后顶栏「设置」可改）----
  const cfgMask = $("cfgMask");
  const cfgProviderSel = $("cfgProvider");
  // 各服务商的注册教程（纯文案，按 provider_id 取；未收录的显示通用兜底）
  const REGISTER_GUIDE = {
    deepseek: [
      "打开 platform.deepseek.com，用手机号注册并登录。",
      "左侧菜单点「API Keys」→「创建 API Key」。",
      "复制生成的一串 key（sk- 开头），粘贴到上面的输入框。",
      "新用户送免费额度，日常复习用量下能用很久。",
    ],
    zhipu: [
      "打开 open.bigmodel.cn，用手机号注册并完成实名认证。",
      "进入「API 密钥」页面 →「创建 API Key」。",
      "复制生成的 key 粘贴即可（智谱的 key 不是 sk- 开头，是普通字符，复制完整就行）。",
    ],
    moonshot: [
      "打开 platform.moonshot.cn，注册并登录。",
      "左侧「API Key 管理」→ 新建 API Key。",
      "复制 key（sk- 开头），粘贴到上面的输入框。",
    ],
    qwen: [
      "打开 bailian.console.aliyun.com，用支付宝 / 淘宝账号登录。",
      "首次进入按提示开通「百炼」服务（免费开通，可能需实名）。",
      "右上角头像 →「API-KEY」→ 创建新的 API-KEY → 复制（sk- 开头）。",
      "新用户有免费额度。",
    ],
  };
  let cfgForced = false;   // true = 首次强制（不提供取消按钮）
  let cfgProviders = [];
  let currentStatus = null;
  let cfgTier = "";        // 当前面板选中的档位 id（仅多档服务商如智谱；单档为空）

  function cfgMeta() {
    return cfgProviders.find((p) => p.id === cfgProviderSel.value);
  }

  // 取某档位的展示元数据；单档服务商（无 tiers）返回 null
  function tierMetaOf(meta, tierId) {
    if (!meta || !meta.tiers || !meta.tiers.length) return null;
    return meta.tiers.find((t) => t.id === tierId) || meta.tiers[0];
  }

  function cfgRenderNote() {
    const meta = cfgMeta();
    if (!meta) return;
    const t = tierMetaOf(meta, cfgTier);
    const model = (t && t.model) || meta.model;
    const visionModel = (t && t.vision_model) || meta.vision_model;
    const note = (t && t.note) || meta.note;
    const priceNote = (t && t.price_note) || meta.price_note;
    const readImg = visionModel
      ? `读图模型 ${visionModel}`
      : "不支持读图（有图的 PPT 将只按文字生成）";
    const box = $("cfgProviderNote");
    box.textContent = "";
    if (t) {
      const tierLine = document.createElement("div");
      tierLine.className = "cfg-tier-label";
      tierLine.textContent = `档位：${t.label}`;
      box.appendChild(tierLine);
    }
    const line1 = document.createElement("div");
    line1.textContent =
      `文本模型 ${model} · ${readImg}${note ? "。" + note : ""}`;
    box.appendChild(line1);
    if (priceNote) {
      const line2 = document.createElement("div");
      line2.className = "cfg-price";
      line2.textContent = "价格：" + priceNote;
      box.appendChild(line2);
    }
    if (meta.topup_url) {
      const line3 = document.createElement("div");
      const a = document.createElement("a");
      a.href = meta.topup_url;
      a.target = "_blank";
      a.rel = "noopener";
      a.textContent = `充值 / 余额管理（${meta.name} 官方控制台）`;
      line3.appendChild(a);
      box.appendChild(line3);
    }
  }

  // 渲染档位 radio 组（只有 providers 响应带 tiers 的服务商显示）
  function cfgRenderTier() {
    const meta = cfgMeta();
    const field = $("cfgTierField");
    const box = $("cfgTierOpts");
    if (!meta || !meta.tiers || !meta.tiers.length) {
      field.classList.add("hidden");
      box.innerHTML = "";
      cfgTier = "";
      return;
    }
    field.classList.remove("hidden");
    if (!meta.tiers.some((t) => t.id === cfgTier)) {
      cfgTier = meta.default_tier && meta.tiers.some((t) => t.id === meta.default_tier)
        ? meta.default_tier
        : meta.tiers[0].id;
    }
    box.innerHTML = "";
    for (const t of meta.tiers) {
      const label = document.createElement("label");
      label.className = "check";
      const radio = document.createElement("input");
      radio.type = "radio";
      radio.name = "cfgTier";
      radio.value = t.id;
      radio.checked = t.id === cfgTier;
      radio.addEventListener("change", () => onTierPick(t, radio));
      label.appendChild(radio);
      label.appendChild(document.createTextNode(t.label));
      box.appendChild(label);
    }
  }

  // 档位切换：切到免费档先弹确认（免费池高峰超载 / 需实名，0.6.4 实测教训）
  function onTierPick(t, radio) {
    if (t.free) {
      openConfirm(
        "切换为免费档？",
        "免费档标价 0 元，但免费模型是共享资源池：高峰期经常超载（可能验证/生成失败），"
        + "且需要该平台账户完成实名认证。赶时间或要稳定出文档建议留在付费档"
        + "（新用户通常有免费 tokens 可抵扣，不一定真花钱）。",
        () => { cfgTier = t.id; cfgRenderNote(); cfgSetStatus(""); },
        () => cfgRenderTier()  // 取消：按原 cfgTier 重渲染勾选态
      );
    } else {
      cfgTier = t.id;
      cfgRenderNote();
      cfgSetStatus("");
    }
  }

  function cfgSetStatus(msg, type = "") {
    const el = $("cfgStatus");
    el.textContent = msg;
    el.className = "cfg-status " + type;
  }

  function cfgRenderNote() {
    const meta = cfgProviders.find((p) => p.id === cfgProviderSel.value);
    if (!meta) return;
    const readImg = meta.vision_supported
      ? `读图模型 ${meta.vision_model}`
      : "不支持读图（有图的 PPT 将只按文字生成）";
    const box = $("cfgProviderNote");
    box.textContent = "";
    const line1 = document.createElement("div");
    line1.textContent =
      `文本模型 ${meta.model} · ${readImg}${meta.note ? "。" + meta.note : ""}`;
    box.appendChild(line1);
    if (meta.price_note) {
      const line2 = document.createElement("div");
      line2.className = "cfg-price";
      line2.textContent = "价格：" + meta.price_note;
      box.appendChild(line2);
    }
    if (meta.topup_url) {
      const line3 = document.createElement("div");
      const a = document.createElement("a");
      a.href = meta.topup_url;
      a.target = "_blank";
      a.rel = "noopener";
      a.textContent = `充值 / 余额管理（${meta.name} 官方控制台）`;
      line3.appendChild(a);
      box.appendChild(line3);
    }
  }

  function cfgRenderTutorial() {
    const steps = REGISTER_GUIDE[cfgProviderSel.value] || [];
    const box = $("cfgTutorial");
    box.innerHTML = "";
    if (!steps.length) return;
    const ol = document.createElement("ol");
    for (const s of steps) {
      const li = document.createElement("li");
      li.textContent = s;
      ol.appendChild(li);
    }
    box.appendChild(ol);
  }

  async function cfgLoadProviders(keepValue, keepTier) {
    try {
      const res = await fetch("/api/settings/providers");
      const data = await res.json();
      cfgProviders = data.providers || [];
      cfgProviderSel.innerHTML = "";
      for (const p of cfgProviders) {
        const o = document.createElement("option");
        o.value = p.id;
        o.textContent = p.name;
        cfgProviderSel.appendChild(o);
      }
      if (keepValue && cfgProviders.some((p) => p.id === keepValue)) cfgProviderSel.value = keepValue;
      // 初始档位：已配置且当前服务商多档 → 用已存档位，否则默认档
      const meta = cfgMeta();
      if (meta && meta.tiers && meta.tiers.length) {
        const has = (id) => meta.tiers.some((t) => t.id === id);
        cfgTier = keepTier && has(keepTier)
          ? keepTier
          : (meta.default_tier && has(meta.default_tier) ? meta.default_tier : meta.tiers[0].id);
      } else {
        cfgTier = "";
      }
      cfgRenderTier();
      cfgRenderNote();
      cfgRenderTutorial();
    } catch { cfgSetStatus("加载服务商列表失败，请刷新重试", "error"); }
  }

  function cfgProviderName(id) {
    const p = cfgProviders.find((x) => x.id === id);
    return p ? p.name : id;
  }

  function updateCfgCurrent(st) {
    if (!st) return;
    const parts = [];
    if (st.configured) {
      parts.push(`当前：${st.provider_name}`);
      if (st.tier_label) parts.push(st.tier_label);
      if (st.api_key_tail) parts.push(`Key ${st.api_key_tail}`);
      if (st.source === "env") parts.push("（来自 .env，保存后将改用你填的 Key）");
    } else {
      parts.push("尚未配置");
    }
    $("cfgCurrent").textContent = parts.join(" · ");
    if (st.configured) {
      $("cfgKey").placeholder = `已配置${st.api_key_tail ? " Key " + st.api_key_tail : ""}，粘贴新 Key 可替换`;
    }
  }

  function openCfg(force) {
    cfgForced = !!force;
    $("cfgTitle").textContent = force ? "先配置 AI 服务，才能开始使用" : "AI 服务设置";
    $("cfgSub").textContent = force
      ? "生成复习文档需要调用 AI 服务：选一家 → 粘贴你的 API Key → 验证并保存。"
      : "更换服务商、档位或 Key 后点「验证并保存」，立即生效。";
    $("cfgCloseBtn").classList.toggle("hidden", force);
    cfgSetStatus("", "");
    cfgMask.classList.remove("hidden");
    cfgLoadProviders(
      currentStatus && currentStatus.configured && currentStatus.provider_id
        ? currentStatus.provider_id
        : null,
      currentStatus && currentStatus.configured ? currentStatus.tier || null : null
    );
    updateCfgCurrent(currentStatus);
  }

  function closeCfg() { cfgMask.classList.add("hidden"); }

  $("settingsBtn").addEventListener("click", () => openCfg(false));
  $("cfgCloseBtn").addEventListener("click", closeCfg);
  cfgProviderSel.addEventListener("change", () => {
    // 换服务商后档位重置为该家默认档（智谱=付费）
    const meta = cfgMeta();
    cfgTier = meta && meta.tiers && meta.tiers.length
      ? (meta.default_tier || meta.tiers[0].id)
      : "";
    cfgRenderTier();
    cfgRenderNote();
    cfgRenderTutorial();
  });

  $("cfgTogglePwd").addEventListener("click", () => {
    const inp = $("cfgKey");
    const show = inp.type === "password";
    inp.type = show ? "text" : "password";
    $("cfgTogglePwd").textContent = show ? "隐藏" : "显示";
  });

  $("cfgVerifyBtn").addEventListener("click", async () => {
    const pid = cfgProviderSel.value;
    const key = $("cfgKey").value.trim();
    if (!key) { cfgSetStatus("请先在上方粘贴你的 API Key", "error"); return; }
    const btn = $("cfgVerifyBtn");
    btn.disabled = true;
    cfgSetStatus("正在验证 Key 并保存…", "busy");
    try {
      const vRes = await fetch("/api/settings/verify", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ provider_id: pid, api_key: key, tier: cfgTier }),
      });
      const v = await vRes.json();
      if (!vRes.ok || !v.ok) {
        cfgSetStatus(v.message || "验证失败，请重试", "error");
        return;
      }
      const sRes = await fetch("/api/settings/save", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ provider_id: pid, api_key: key, tier: cfgTier }),
      });
      const saved = await sRes.json();
      if (!sRes.ok || !saved.ok) {
        cfgSetStatus(saved.message || "保存失败", "error");
        return;
      }
      cfgSetStatus("验证通过，已保存。", "success");
      const stRes = await fetch("/api/settings/status");
      currentStatus = await stRes.json();
      updateCfgCurrent(currentStatus);
      $("cfgKey").value = "";
      const name = cfgProviderName(pid);
      setTimeout(() => {
        closeCfg();
        if (cfgForced) setStatus(`已配置 ${name}，上传课件即可开始`, "success");
      }, 700);
    } catch (err) {
      cfgSetStatus("网络错误：" + (err && err.message ? err.message : "未知错误"), "error");
    } finally {
      btn.disabled = false;
    }
  });

  // ---- 整课批量生成 ----
  const batchFiles = [];        // {file: File, peek: {total_pages,image_count,est_cost_yuan} | {error} | null}
  let batchId = null;           // 进行中/待查看的批任务 id
  let batchTimer = null;        // 2s 轮询
  let batchMasterText = "";     // 总册原文缓存
  let maxBatchFiles = 20;       // 单批上限，以 /api/batches 的 max_files 为准（启动时拉取）

  // 结果卡片（`#resultCard`）是「单份」和「批量」共用的一块，DOM 上位于三个视图之外。
  // 切到历史 tab 时必须把它藏起来，否则历史页下方会冒出上一份复习文档——
  // 那正是"点了文档再返回，下面的东西还在"这类困惑的来源。
  // 用一个标记记住"是被切换隐藏的"，切回来才恢复，避免误把本该隐藏的状态掀开。
  let resultCardHiddenByTab = false;

  function switchTab(tab) {
    const single = tab === "single";
    const batch = tab === "batch";
    const history = tab === "history";
    const stats = tab === "stats";
    $("tabSingle").classList.toggle("active", single);
    $("tabBatch").classList.toggle("active", batch);
    $("tabHistory").classList.toggle("active", history);
    $("tabStats").classList.toggle("active", stats);
    $("singleView").classList.toggle("hidden", !single);
    $("batchView").classList.toggle("hidden", !batch);
    $("historyView").classList.toggle("hidden", !history);
    $("statsView").classList.toggle("hidden", !stats);

    const rc = $("resultCard");
    // 统计页与历史页一样，不该在下面挂着上一份复习文档
    if (history || stats) {
      if (!rc.classList.contains("hidden")) {
        rc.classList.add("hidden");
        resultCardHiddenByTab = true;
      }
    } else if (resultCardHiddenByTab) {
      rc.classList.remove("hidden");
      resultCardHiddenByTab = false;
    }

    // 每次切到历史都刷新：在问答区问了新问题、或刚跑完一批，回来必须是最新的。
    // 只在这一个入口拉取，不在别处零散调用——避免"三个地方各自决定何时刷新"。
    if (history) loadHistory();
    // 统计同理：这是唯一拉取入口
    if (stats) loadStats();
  }
  $("tabSingle").addEventListener("click", () => switchTab("single"));
  $("tabBatch").addEventListener("click", () => switchTab("batch"));
  $("tabHistory").addEventListener("click", () => switchTab("history"));
  $("tabStats").addEventListener("click", () => switchTab("stats"));

  function bSetStatus(msg, type = "", html = false) {
    const el = $("batchStatus");
    if (html) el.innerHTML = linkifyHtml(msg);
    else el.textContent = msg;
    el.style.color = type === "error" ? "var(--err)" : type === "success" ? "var(--ok)" : "var(--muted)";
  }

  const bDropZone = $("batchDropZone"), bInput = $("batchFileInput");
  bDropZone.addEventListener("click", () => bInput.click());
  bDropZone.addEventListener("dragover", (e) => { e.preventDefault(); bDropZone.classList.add("dragging"); });
  bDropZone.addEventListener("dragleave", () => bDropZone.classList.remove("dragging"));
  bDropZone.addEventListener("drop", (e) => {
    e.preventDefault(); bDropZone.classList.remove("dragging");
    batchAddFiles(e.dataTransfer.files);
  });
  bInput.addEventListener("change", () => { if (bInput.files.length) batchAddFiles(bInput.files); bInput.value = ""; });

  function batchAddFiles(list) {
    for (const f of list) {
      if (!COURSEWARE_RE.test(f.name)) { bSetStatus(`《${f.name}》不是 .pptx / .pdf 课件，已跳过`, "error"); continue; }
      if (batchFiles.length >= maxBatchFiles) { bSetStatus(`一次最多 ${maxBatchFiles} 份课件，超出部分已忽略`, "error"); break; }
      if (batchFiles.some((x) => x.file.name === f.name && x.file.size === f.size)) continue; // 去重
      batchFiles.push({ file: f, peek: null });
    }
    renderBatchList();
    batchPeekAll();
  }

  function bOpBtn(label, fn, disabled) {
    const b = document.createElement("button");
    b.type = "button"; b.textContent = label; b.disabled = disabled;
    b.addEventListener("click", fn);
    return b;
  }

  function renderBatchList() {
    const box = $("batchList");
    box.innerHTML = "";
    batchFiles.forEach((item, i) => {
      const row = document.createElement("div");
      row.className = "bfile";
      const idx = document.createElement("span");
      idx.className = "bidx"; idx.textContent = i + 1;
      const col = document.createElement("div");
      col.style.cssText = "flex:1;min-width:0;";
      const name = document.createElement("div");
      name.className = "bname"; name.textContent = item.file.name;
      col.appendChild(name);
      if (item.peek && item.peek.error) {
        const e = document.createElement("div");
        e.className = "berr"; e.textContent = item.peek.error;
        col.appendChild(e);
      }
      const meta = document.createElement("span");
      meta.className = "bmeta";
      meta.textContent = item.peek
        ? (item.peek.error ? "—" : `${item.peek.total_pages} 页 · ${item.peek.image_count} 图 · 约 ¥${Number(item.peek.est_cost_yuan).toFixed(2)}`)
        : "分析中…";
      const ops = document.createElement("span");
      ops.className = "bops";
      ops.appendChild(bOpBtn("↑", () => { [batchFiles[i - 1], batchFiles[i]] = [batchFiles[i], batchFiles[i - 1]]; renderBatchList(); }, i === 0));
      ops.appendChild(bOpBtn("↓", () => { [batchFiles[i + 1], batchFiles[i]] = [batchFiles[i], batchFiles[i + 1]]; renderBatchList(); }, i === batchFiles.length - 1));
      ops.appendChild(bOpBtn("✕", () => { batchFiles.splice(i, 1); renderBatchList(); }, false));
      row.appendChild(idx); row.appendChild(col); row.appendChild(meta); row.appendChild(ops);
      box.appendChild(row);
    });
    $("batchStartBtn").disabled = batchFiles.length === 0;
    const total = $("batchTotalMeta");
    const okFiles = batchFiles.filter((x) => x.peek && !x.peek.error);
    if (batchFiles.length && okFiles.length === batchFiles.length) {
      const cost = okFiles.reduce((s, x) => s + (x.peek.est_cost_yuan || 0), 0);
      const pages = okFiles.reduce((s, x) => s + (x.peek.total_pages || 0), 0);
      total.textContent = `共 ${batchFiles.length} 份 · ${pages} 页 · 整批预计花费约 ¥${cost.toFixed(2)}（以实际账单为准）`;
      total.classList.remove("hidden");
    } else {
      total.classList.add("hidden");
    }
  }

  // 逐份速览（只提取不生成）：拿到页数/图片/单份预估，串行做，避免并发解析大文件
  async function batchPeekAll() {
    for (const item of batchFiles) {
      if (item.peek) continue;
      try {
        const form = new FormData();
        form.append("file", item.file);
        form.append("use_vision", $("bUseVision").checked);
        form.append("thinking", $("bThinking").checked);
        form.append("style", $("bStyle").value);
        const res = await fetch("/api/batch/peek", { method: "POST", body: form });
        const d = await res.json();
        item.peek = res.ok ? d : { error: d.detail || "分析失败" };
      } catch { item.peek = { error: "网络错误，分析失败" }; }
      renderBatchList();
    }
  }

  $("batchStartBtn").addEventListener("click", () => {
    if (!batchFiles.length) return;
    const est = batchFiles.filter((x) => x.peek && !x.peek.error)
      .reduce((s, x) => s + (x.peek.est_cost_yuan || 0), 0);
    const n = batchFiles.length;
    const minMin = n, maxMin = n * ($("bUseVision").checked ? 2.5 : 1.5);
    openConfirm(
      "开始批量生成？",
      `共 ${n} 份课件，整批预计花费约 ¥${est.toFixed(2)}（勾思考会更贵），\n` +
      `串行生成约需 ${minMin}~${Math.round(maxMin)} 分钟。可随时停止，\n生成到一半的总册也能下载。`,
      () => doStartBatch(),
      () => {}
    );
  });

  // ==================== 课程问答（P3 知识库）====================
  //
  // 与「生成复习文档」完全独立的一条链路：上传课件 → 建库 → 提问 → 流式答案 + 引用核验。
  // 知识库建在**课件原文**上，所以不依赖"生成过文档"。

  let qaKbId = "";
  let qaKbSig = "";   // 建库时的文件指纹，用来判断"选的文件换了没"
  let qaBusy = false;

  function qaStatus(html) { $("qaKbStatus").innerHTML = html || ""; }

  // 文件指纹：名字 + 大小。**不去监听选择列表的增删**——那样要改三四处现有代码，
  // 而且容易漏；改成每次提问前比一次指纹，换了文件就自动重建，逻辑集中在一处。
  function qaSignature() {
    return batchFiles.map((x) => `${x.file.name}:${x.file.size}`).join("|");
  }

  async function qaEnsureKb() {
    const sig = qaSignature();
    if (qaKbId && sig === qaKbSig) return true;
    if (!batchFiles.length) {
      qaStatus('<span style="color:var(--danger)">请先在上面选择这门课的课件</span>');
      return false;
    }
    const n = batchFiles.length;
    qaStatus(`正在建库（${n} 份课件，首次约需数秒）…`);
    const form = new FormData();
    batchFiles.forEach((x) => form.append("files", x.file));
    // 课程名与「生成复习文档」共用同一个输入框：**归组只能靠它**。
    // 课程名变了不重建索引（它只是标签），但会把历史记录归到新的课名下。
    const courseEl = $("batchCourseName");
    form.append("course_name", courseEl ? courseEl.value.trim() : "");
    try {
      const res = await fetch("/api/kb", { method: "POST", body: form });
      const d = await res.json().catch(() => ({}));
      if (!res.ok) {
        qaStatus(`<span style="color:var(--danger)">建库失败：${linkifyHtml(d.detail || res.status)}</span>`);
        return false;
      }
      qaKbId = d.kb_id;
      qaKbSig = sig;
      let msg = `已建库：<strong>${d.n_files}</strong> 份 / <strong>${d.n_pages}</strong> 页 / `
              + `${Number(d.n_chars).toLocaleString()} 字符`;
      if (d.failures && d.failures.length) {
        msg += `<br><span style="color:var(--danger)">⚠️ ${d.failures.length} 份未能提取：`
             + `${linkifyHtml(d.failures.slice(0, 3).join("；"))}</span>`;
      }
      qaStatus(msg);
      const sel = $("qaScope");
      sel.innerHTML = '<option value="">整门课</option>';
      batchFiles.forEach((x) => {
        const o = document.createElement("option");
        o.value = x.file.name;
        o.textContent = x.file.name;
        sel.appendChild(o);
      });
      qaLoadHistory();   // 建库成功即回显"这门课之前问过什么"
      return true;
    } catch (e) {
      qaStatus(`<span style="color:var(--danger)">建库失败：${linkifyHtml(String(e))}</span>`);
      return false;
    }
  }

  function qaRenderSources(ev) {
    const el = $("qaSources");
    if (!ev.hits || !ev.hits.length) {
      el.innerHTML = `<div class="meta">${linkifyHtml(ev.note || "没有检索到相关内容")}</div>`;
      return;
    }
    const rows = ev.hits.map((h) =>
      `<li><code>${linkifyHtml(h.file)}</code> 第 ${h.page} 页`
      + (h.title ? `：${linkifyHtml(h.title)}` : "") + "</li>"
    ).join("");
    el.innerHTML = `<div class="meta">找到了 ${ev.hits.length} 处相关原文：</div>`
      + `<ul style="padding-left:18px; margin:6px 0 0">${rows}</ul>`;
  }

  function qaRenderCitations(ev) {
    const el = $("qaCitations");
    const note = (ev.summary && ev.summary.note) || "";
    if (!ev.checks || !ev.checks.length) {
      el.innerHTML = note ? `<div class="meta">${linkifyHtml(note)}</div>` : "";
      return;
    }
    const items = ev.checks.map((c) => {
      const ok = c.verdict === "supported";
      const gone = c.verdict === "not_found";
      const color = ok ? "var(--ok, #2e7d32)" : (gone ? "var(--danger)" : "#b26a00");
      const mark = ok ? "✓" : (gone ? "✗" : "?");
      const claim = (c.claim || "").replace(/\s+/g, " ").slice(0, 90);
      // 附源页原文：核验结论一律是「待核」而非「错误」，最终判断权交回用户，
      // 所以必须把证据一并给出，否则用户无从核对。
      const detail = c.excerpt
        ? '<details style="margin-top:4px"><summary style="cursor:pointer">查看源页原文</summary>'
          + `<pre style="white-space:pre-wrap; font-size:12px; margin:6px 0 0">${linkifyHtml(c.excerpt)}</pre></details>`
        : "";
      return "<li style=\"margin-bottom:10px\">"
        + `<span style="color:${color}">${mark} ${linkifyHtml(c.verdict_label)}</span>`
        + ` — <code>${linkifyHtml(c.file)}</code> 第 ${c.page} 页`
        + `<div class="meta" style="margin-top:2px">论断：${linkifyHtml(claim)}</div>${detail}</li>`;
    }).join("");
    el.innerHTML = `<div class="meta">引用核验：${linkifyHtml(note)}</div>`
      + `<ul style="padding-left:18px; margin:6px 0 0">${items}</ul>`;
  }

  // ---- 问过的问题（读 output/qa/<kb_id>.jsonl）----
  //
  // 历史落在服务端的独立目录里，与知识库缓存的 TTL 无关——
  // 用户可能过了一周才回来翻"上次问了什么"，那时缓存早过期了，历史必须还在。
  // 所以这里**不做"知识库过期就不显示历史"的判断**。

  async function qaLoadHistory() {
    const el = $("qaHistory");
    if (!qaKbId) { el.innerHTML = ""; return; }
    try {
      const res = await fetch(`/api/kb/${encodeURIComponent(qaKbId)}/qa?limit=30`);
      if (!res.ok) { el.innerHTML = ""; return; }
      const d = await res.json();
      qaRenderHistory(d.records || []);
    } catch { el.innerHTML = ""; }   // 历史读不到不影响问答本身
  }

  function qaRenderHistory(records) {
    const el = $("qaHistory");
    if (!records.length) {
      el.innerHTML = '<div class="meta">这门课还没有提问记录</div>';
      return;
    }
    const rows = records.map((r) => {
      const when = String(r.ts || "").replace("T", " ").slice(5, 16);
      const nHit = (r.hits || []).length;
      const found = r.no_hit
        ? '<span style="color:#b26a00">未检索到内容</span>'
        : `${nHit} 处原文`;
      const cites = r.citations && r.citations.total
        ? ` · 引用 ${r.citations.total} 条` : "";
      // data-q 存原问题：点一条就能重问（重问会走完整链路，包括重建库）
      return '<li style="margin-bottom:7px">'
        + `<a href="#" class="qa-replay" data-q="${linkifyHtml(r.question)}" `
        + `style="color:inherit">${linkifyHtml(r.question)}</a>`
        + `<span class="meta"> — ${linkifyHtml(when)} · ${found}${cites}</span></li>`;
    }).join("");
    el.innerHTML = `<div class="meta">这门课问过的问题（点一条可重问）：</div>`
      + `<ul style="padding-left:18px; margin:6px 0 0">${rows}</ul>`;
    el.querySelectorAll(".qa-replay").forEach((a) => {
      a.addEventListener("click", (e) => {
        e.preventDefault();
        $("qaInput").value = a.dataset.q || "";
        qaAsk();
      });
    });
  }

  async function qaAsk() {
    if (qaBusy) return;
    const q = $("qaInput").value.trim();
    if (!q) { $("qaInput").focus(); qaStatus("请先输入问题"); return; }
    if (!(await qaEnsureKb())) return;

    qaBusy = true;
    $("qaAskBtn").disabled = true;
    const ansEl = $("qaAnswer");
    $("qaRewrite").textContent = "";
    $("qaSources").innerHTML = "";
    $("qaCitations").innerHTML = "";
    ansEl.innerHTML = '<div class="meta">正在检索…</div>';

    const st = { text: "", lastRender: 0 };
    try {
      const res = await fetch("/api/ask", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          kb_id: qaKbId,
          question: q,
          scope: $("qaScope").value,
          use_rewrite: true,
        }),
      });
      if (!res.ok || !res.body) {
        const d = await res.json().catch(() => ({}));
        ansEl.innerHTML = `<div class="meta" style="color:var(--danger)">`
          + `${linkifyHtml(d.detail || "请求失败")}</div>`;
        return;
      }

      // POST + ReadableStream 手工解析 SSE：浏览器的 EventSource 只支持 GET，
      // 而问答参数（kb_id / question / scope）塞进 query string 既难看又有长度限制。
      const reader = res.body.getReader();
      const decoder = new TextDecoder();
      let buf = "";
      for (;;) {
        const { done, value } = await reader.read();
        if (done) break;
        // stream:true 必须有——中文是多字节，可能正好被切在 chunk 边界上
        buf += decoder.decode(value, { stream: true });
        const frames = buf.split("\n\n");
        buf = frames.pop() || "";
        for (const frame of frames) {
          if (!frame.startsWith("data: ")) continue;
          let ev;
          try { ev = JSON.parse(frame.slice(6)); } catch { continue; }

          if (ev.type === "rewrite") {
            if (ev.applied) {
              $("qaRewrite").innerHTML =
                `已改写为检索式：<strong>${linkifyHtml(ev.rewritten)}</strong>`;
            } else if (ev.note) {
              $("qaRewrite").textContent = `未改写：${ev.note}`;
            }
          } else if (ev.type === "sources") {
            qaRenderSources(ev);
          } else if (ev.type === "delta") {
            st.text += ev.text;
            // 节流渲染：每个 token 都 marked.parse 一次会卡住长回答
            const now = performance.now();
            if (now - st.lastRender > 80) {
              st.lastRender = now;
              ansEl.innerHTML = DOMPurify.sanitize(marked.parse(st.text));
            }
          } else if (ev.type === "citations") {
            if (st.text) ansEl.innerHTML = DOMPurify.sanitize(marked.parse(st.text));
            qaRenderCitations(ev);
          } else if (ev.type === "error") {
            ansEl.innerHTML = `<div class="meta" style="color:var(--danger)">`
              + `${linkifyHtml(ev.message)}</div>`;
          }
        }
      }
      if (st.text) ansEl.innerHTML = DOMPurify.sanitize(marked.parse(st.text));
    } catch (e) {
      ansEl.innerHTML = `<div class="meta" style="color:var(--danger)">${linkifyHtml(String(e))}</div>`;
    } finally {
      qaBusy = false;
      $("qaAskBtn").disabled = false;
      qaLoadHistory();   // 这次问答已落盘，刷新列表让用户看到它进去了
    }
  }

  $("qaAskBtn").addEventListener("click", qaAsk);
  $("qaInput").addEventListener("keydown", (e) => {
    if (e.key === "Enter") { e.preventDefault(); qaAsk(); }
  });

  async function doStartBatch() {
    $("batchStartBtn").disabled = true;
    bSetStatus("正在创建批量任务…");
    try {
      const form = new FormData();
      batchFiles.forEach((x) => form.append("files", x.file));
      form.append("template", $("bTemplate").value);
      form.append("style", $("bStyle").value);
      form.append("use_vision", $("bUseVision").checked);
      form.append("thinking", $("bThinking").checked);
      form.append("attach_coverage", $("bAttachCov").checked);
      form.append("enable_repair", $("bRepair").checked);
      form.append("course_name", $("batchCourseName").value.trim());
      const res = await fetch("/api/batch/start", { method: "POST", body: form });
      const d = await res.json();
      if (!res.ok) { bSetStatus(d.detail || "创建失败", "error", true); $("batchStartBtn").disabled = false; return; }
      batchId = d.batch_id;
      enterBatchRun(d);
    } catch (err) {
      bSetStatus("网络错误：" + (err && err.message ? err.message : "未知错误"), "error");
      $("batchStartBtn").disabled = false;
    }
  }

  function enterBatchRun(detail) {
    $("batchSetupCard").classList.add("hidden");
    $("batchRunCard").classList.remove("hidden");
    $("batchRunTitle").textContent = `《${detail.course_name}》复习总册`;
    bSetStatus("");
    // 先启动轮询再渲染：即便首屏渲染因字段缺失抛错，轮询也会在 2 秒内把界面纠正过来。
    // （此前顺序相反，渲染一抛错就永远轮询不起来，界面停在 0/N。）
    startBatchPolling();
    renderBatchStatus(detail);
  }

  function startBatchPolling() {
    if (batchTimer) clearInterval(batchTimer);
    batchTimer = setInterval(pollBatchOnce, 2000);
    pollBatchOnce();
  }

  function stopBatchPolling() {
    if (batchTimer) { clearInterval(batchTimer); batchTimer = null; }
  }

  async function pollBatchOnce() {
    if (!batchId) return;
    try {
      const res = await fetch(`/api/batch/${batchId}`);
      if (!res.ok) { stopBatchPolling(); bSetStatus("查询任务状态失败，请刷新页面", "error"); return; }
      const d = await res.json();
      renderBatchStatus(d);
      if (d.status !== "running") {
        stopBatchPolling();
        onBatchFinished(d);
      }
    } catch { /* 瞬时网络错误：下一轮再试 */ }
  }

  const J_ICON = { waiting: "⋯", done: "✓", error: "✗" };
  const J_TEXT = { waiting: "等待中", running: "生成中", done: "完成", error: "失败" };
  const B_TEXT = { running: "正在生成", done: "已完成", partial: "部分完成", paused: "已暂停（网络不稳）", cancelled: "已停止" };

  function renderBatchStatus(d) {
    const total = d.total || 1;
    $("batchProgressBar").style.width = Math.round((d.done / total) * 100) + "%";
    const money = [];
    if (d.spent_yuan > 0) money.push(`已花费约 ¥${d.spent_yuan}`);
    if (d.est_total_yuan > 0) money.push(`整批预估约 ¥${d.est_total_yuan}`);
    $("batchRunMeta").textContent =
      `${B_TEXT[d.status] || d.status} ${d.done}/${d.total}` + (money.length ? " · " + money.join(" · ") : "");

    const box = $("batchJobList");
    box.innerHTML = "";
    if (d.coverage_note) {   // 档位不适用：说明一句，避免「一个徽标都没有」被当成故障
      const n = document.createElement("div");
      n.className = "covdetail";
      n.textContent = d.coverage_note;
      box.appendChild(n);
    }
    (d.jobs || []).forEach((j) => {
      const row = document.createElement("div");
      row.className = "jrow";
      const st = document.createElement("span");
      st.className = "jst";
      if (j.status === "running") st.innerHTML = '<span class="jspinner"></span>';
      else st.textContent = J_ICON[j.status] || "⋯";
      st.style.color = j.status === "done" ? "var(--ok)" : j.status === "error" ? "var(--err)" : "var(--muted)";
      const col = document.createElement("div");
      col.style.cssText = "flex:1;min-width:0;";
      const name = document.createElement("div");
      name.className = "jname";
      name.textContent = `第 ${j.idx} 节 · ${j.name}`;
      col.appendChild(name);
      if (j.status === "done") {
        const info = document.createElement("div");
        info.className = "jinfo";
        info.textContent = `${j.total_pages} 页 · 约 ¥${j.cost_yuan}${j.cost_is_estimate ? "（估）" : ""}`
          + (j.warning ? " · ⚠️ 内容被截断（已达单次输出上限），可改用「精简」档或单独生成这一份" : "");
        col.appendChild(info);
      }
      // 覆盖率徽标（coverage_percent < 0 表示没算出来/旧任务 → 不显示，不伪装成 0%）
      // 两重门控：
      // ① 档位必须适用（coverage_note 为空）——不是「复习指南/详细」时覆盖率没有意义，
      //    即使某份带着历史/回填数据也不能显示徽标，否则会用 59% 这种数字误导用户；
      // ② 必须显式判 null——JS 里 `null >= 0` 为 true，只写 >= 0 会显示「覆盖 null%」。
      if (!d.coverage_note && j.status === "done"
          && j.coverage_percent != null && j.coverage_percent >= 0) {
        attachJobCoverage(col, j);
      }
      if (j.error) {
        const e = document.createElement("div");
        e.className = "berr";
        e.innerHTML = linkifyHtml(j.error);  // 后端友好文案可能带充值链接
        col.appendChild(e);
      }
      const meta = document.createElement("span");
      meta.className = "jmeta"; meta.textContent = J_TEXT[j.status] || "";
      row.appendChild(st); row.appendChild(col); row.appendChild(meta);
      box.appendChild(row);
    });

    const running = d.status === "running";
    $("batchCancelBtn").classList.toggle("hidden", !running);
    const resumable = !running && (d.error > 0 || d.waiting > 0);
    $("batchRetryBtn").classList.toggle("hidden", !resumable);
    $("batchRetryBtn").textContent = d.waiting > 0 ? "继续生成" : "重试失败项";
    $("batchNewBtn").classList.toggle("hidden", running);
  }

  async function onBatchFinished(d) {
    if (d.done > 0) {
      await loadBatchResult(d);
      if (d.status === "partial" || d.status === "paused")
        $("batchRunStatus").textContent = "有未完成的课件：修复问题后点「重试失败项 / 继续生成」，总册会随之更新。";
    } else {
      $("batchRunStatus").textContent = "本批没有成功生成的课件；请检查网络与 Key 后重试。";
      $("resultCard").classList.add("hidden");
    }
  }

  async function loadBatchResult(d) {
    try {
      const res = await fetch(`/api/batch/${batchId}/master`);
      if (!res.ok) throw new Error();
      batchMasterText = await res.text();
    } catch { batchMasterText = ""; }
    renderBatchSectionBar(d);
    $("covBox").classList.add("hidden");  // 单份的覆盖率块不适用于批量视图
    $("resultTitle").textContent = `《${d.course_name}》复习总册`;
    await showBatchContent(null);
    $("resultCard").scrollIntoView({ behavior: "smooth" });
  }

  function renderBatchSectionBar(d) {
    const bar = $("batchSectionBar");
    bar.innerHTML = "";
    bar.classList.remove("hidden");
    const mk = (label, idx) => {
      const b = document.createElement("button");
      b.type = "button"; b.className = "sec-tab"; b.textContent = label;
      b.dataset.idx = idx == null ? "master" : String(idx);
      b.addEventListener("click", () => showBatchContent(idx));
      return b;
    };
    bar.appendChild(mk("总册", null));
    (d.jobs || []).filter((j) => j.status === "done")
      .forEach((j) => bar.appendChild(mk(`第${j.idx}节 ${j.name.replace(COURSEWARE_RE, "")}`, j.idx)));
  }

  async function showBatchContent(idx) {
    document.querySelectorAll("#batchSectionBar .sec-tab").forEach((b) =>
      b.classList.toggle("active", idx == null ? b.dataset.idx === "master" : b.dataset.idx === String(idx)));
    if (idx == null) {
      if (batchMasterText) renderMarkdown(batchMasterText, { scroll: false });
      return;
    }
    try {
      const res = await fetch(`/api/batch/${batchId}/doc/${idx}`);
      if (!res.ok) throw new Error();
      renderMarkdown(await res.text(), { scroll: false });
    } catch { setStatus("获取该节内容失败", "error"); }
  }

  $("batchRetryBtn").addEventListener("click", async () => {
    try {
      const res = await fetch(`/api/batch/${batchId}/retry`, {
        method: "POST", headers: { "Content-Type": "application/json" }, body: "{}",
      });
      const d = await res.json();
      if (!res.ok) { $("batchRunStatus").textContent = d.detail || "重试失败"; return; }
      $("batchRunStatus").textContent = "";
      renderBatchStatus(d);
      startBatchPolling();
    } catch { $("batchRunStatus").textContent = "网络错误，请稍后重试"; }
  });

  $("batchCancelBtn").addEventListener("click", async () => {
    try {
      await fetch(`/api/batch/${batchId}/cancel`, { method: "POST" });
      $("batchRunStatus").textContent = "已请求停止：当前这份做完后就停（已完成的部分会保留，可下载）。";
      pollBatchOnce();
    } catch { $("batchRunStatus").textContent = "网络错误"; }
  });

  $("batchNewBtn").addEventListener("click", () => {
    stopBatchPolling();
    batchId = null;
    batchMasterText = "";
    $("batchRunCard").classList.add("hidden");
    // ★ 必须一并隐藏结果卡片。`#resultCard` 在 DOM 上位于三个视图之外
    //   （单份与批量共用），只隐藏 `batchRunCard` 会让上一批的复习总册
    //   继续显示在页面下方——用户看到的就是"点了返回新建，底下还有刚才那份文档"。
    $("resultCard").classList.add("hidden");
    resultCardHiddenByTab = false;   // 手动隐藏，不该被 tab 切换的恢复逻辑掀开
    $("batchSetupCard").classList.remove("hidden");
    $("batchSectionBar").classList.add("hidden");
    $("batchRunStatus").textContent = "";
    $("batchStartBtn").disabled = batchFiles.length === 0;
    // 这里**不再刷新历史**：历史已挪到独立 tab，由 switchTab 负责拉取。
  });

  // ---- 历史记录：查看以往生成过的总册与各节 ----
  // 产物落在 output/<batch_id>/（总册 + 每节 doc.md），状态存 batch.json，
  // 服务重启也不丢。「返回新建」会清空结果卡片，所以另给一个入口重新打开。
  const H_STATUS = {
    running: "进行中", done: "已完成", partial: "部分完成",
    paused: "已暂停", cancelled: "已停止",
  };

  // ---- 历史记录：按课程归组（一门课 = 它的复习文档 + 问过的问题）----
  //
  // 为什么按课程而不是按时间混排：两类产物的性质不同——文档是**文件**（能下载重看），
  // 提问是**对话**。按时间混在一起，找文档会被一堆提问淹没，反过来也一样。
  // 而人回忆时先想起的是"那门课"，再想起"那次生成的"或"我问过的"。
  //
  // 归组的 key 只能是**课程名**：同一门课的课件常统一叫「讲义.pptx」
  // （实际数据里 3 个批次各 4–5 份全叫这个），靠文件名既分不出课程也归不了组。

  async function loadHistory() {
    const box = $("historyList");
    const empty = $("historyEmpty");
    const summary = $("historySummary");
    try {
      // 两件事一起拉：归组数据在 /api/library；
      // max_files 仍由 /api/batches 下发（前端不留第二份常量）。
      const [libRes, batchRes] = await Promise.all([
        fetch("/api/library"),
        fetch("/api/batches"),
      ]);
      if (batchRes.ok) {
        const { max_files } = await batchRes.json();
        if (max_files) {
          maxBatchFiles = max_files;
          const hint = $("maxFilesHint");
          if (hint) hint.textContent = String(max_files);
        }
      }
      if (!libRes.ok) { box.innerHTML = ""; empty.classList.remove("hidden"); summary.textContent = ""; return; }
      const d = await libRes.json();
      const courses = d.courses || [];

      const bits = [`${d.n_courses || 0} 门课程`];
      if (d.n_unfiled) bits.push("另有未归类记录（下面单独列出）");
      summary.textContent = courses.length ? bits.join(" · ") : "";
      empty.classList.toggle("hidden", courses.length > 0);
      renderHistory(courses);
    } catch {
      /* 拉不到就留空，不打扰用户 */
      box.innerHTML = "";
      summary.textContent = "";
      empty.classList.add("hidden");
    }
  }

  // ---------------------------------------------------------------- 统计页
  // 数字全部来自后端只读汇总（/api/stats）。前端不做二次计算——
  // 同一份数据在前后端各算一遍，迟早会算出两个不同的数。

  async function loadStats() {
    const box = $("statsBody");
    const empty = $("statsEmpty");
    try {
      const res = await fetch("/api/stats");
      if (!res.ok) { box.innerHTML = ""; empty.classList.remove("hidden"); return; }
      const d = await res.json();
      const g = d.generation || {};
      const q = d.qa || {};
      const hasAny = (g.n_jobs || 0) + (q.n_questions || 0) > 0;
      empty.classList.toggle("hidden", hasAny);
      box.innerHTML = hasAny ? renderStats(d) : "";
    } catch {
      box.innerHTML = "";
      empty.classList.remove("hidden");
    }
  }

  function fmtNum(v, digits = 0) {
    if (v === null || v === undefined) return "—";
    return Number(v).toLocaleString("zh-CN", { maximumFractionDigits: digits, minimumFractionDigits: digits });
  }
  function kpi(k, v, unit) {
    return `<div class="kpi"><div class="k">${k}</div><div class="v">${v}${
      unit ? `<span class="u">${unit}</span>` : ""
    }</div></div>`;
  }

  function renderStats(d) {
    const g = d.generation || {};
    const q = d.qa || {};
    const cov = g.coverage || {};
    const dur = g.duration_s || {};
    const tok = g.tokens || {};
    const ft = q.first_token_ms || {};
    const cites = q.citations || {};

    const kpis = [
      kpi("已生成", fmtNum(g.n_jobs || 0), "份"),
      kpi("生成花费", "¥" + fmtNum(g.cost_yuan || 0, 2), ""),
      kpi("平均覆盖率", cov.mean === null || cov.mean === undefined ? "—" : fmtNum(cov.mean, 1), "%"),
      kpi("单份耗时 P50", dur.p50 === null || dur.p50 === undefined ? "—" : fmtNum(dur.p50, 1), "s"),
      kpi("提问", fmtNum(q.n_questions || 0), "次"),
      kpi("首 token P50", ft.p50 === null || ft.p50 === undefined ? "—" : (ft.p50 / 1000).toFixed(1), "s"),
      kpi("引用可信率", cites.pass_rate === null || cites.pass_rate === undefined ? "—" : fmtNum(cites.pass_rate, 1), "%"),
    ].join("");

    const rows = (g.courses || []).map((c) => `<tr>
      <td>${escapeHtml2(c.course)}</td>
      <td>${c.n_done}/${c.n_jobs}</td>
      <td>¥${fmtNum(c.cost_yuan, 2)}</td>
      <td>${c.tokens ? fmtNum(c.tokens) : "—"}</td>
      <td>${c.coverage_mean === null ? "—" : fmtNum(c.coverage_mean, 1) + "%"}</td>
      <td>${c.duration_s_median === null ? "—" : fmtNum(c.duration_s_median, 1) + "s"}</td>
      <td>${escapeHtml2((c.models || []).join(", ") || "—")}</td>
    </tr>`).join("");

    const notes = [];
    if ((tok.covered_jobs || 0) === 0 && (g.n_jobs || 0) > 0) {
      notes.push("token 与耗时是 2026-10-19 之后才埋点的，之前的历史记录没有这两项，所以显示「—」而不是 0。");
    }
    if ((cov.n || 0) < (g.n_jobs || 0)) {
      notes.push(`覆盖率只在算得出来的 ${cov.n || 0} 份上统计（习题档 / 术语太少时不打分），不是把缺的当 0%。`);
    }
    if ((g.cost_estimated_jobs || 0) > 0 && (g.cost_measured_yuan || 0) > 0) {
      notes.push(`其中 ${g.cost_estimated_jobs} 份服务商没回 usage，成本是估算值。`);
    }

    return `<div class="kpis">${kpis}</div>
      <h4 class="stat-h">按课程</h4>
      <table class="stat-table">
        <thead><tr><th>课程</th><th>完成/总数</th><th>花费</th><th>tokens</th><th>平均覆盖率</th><th>单份耗时</th><th>模型</th></tr></thead>
        <tbody>${rows || `<tr><td colspan="7">还没有生成记录</td></tr>`}</tbody>
      </table>
      <h4 class="stat-h">问答</h4>
      <table class="stat-table">
        <thead><tr><th>指标</th><th>读数</th></tr></thead>
        <tbody>
          <tr><td>提问次数</td><td>${fmtNum(q.n_questions || 0)}</td></tr>
          <tr><td>问答花费</td><td>¥${fmtNum(q.cost_yuan || 0, 4)}</td></tr>
          <tr><td>首 token P50 / P90</td><td>${fmtNum(ft.p50, 1)} / ${fmtNum(ft.p90, 1)} ms</td></tr>
          <tr><td>改写耗时 P50</td><td>${fmtNum((q.rewrite_ms || {}).p50, 1)} ms</td></tr>
          <tr><td>引用核验</td><td>${cites.total ? `${cites.supported}/${cites.total} 可信（${fmtNum(cites.pass_rate, 1)}%）` : "—"}</td></tr>
        </tbody>
      </table>
      ${notes.length ? `<div class="stat-note">${notes.map((n) => "· " + n).join("<br>")}</div>` : ""}`;
  }

  function escapeHtml2(s) {
    return String(s === null || s === undefined ? "" : s).replace(/[&<>"]/g, (c) =>
      ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c])
    );
  }

  function renderHistory(courses) {
    const box = $("historyList");
    box.innerHTML = "";
    if (!courses.length) return;

    courses.forEach((c, idx) => {
      // 「未归类」的 key 用 \x00 开头（后端约定），不是真的课程名
      const unfiled = String(c.key || "").charCodeAt(0) === 0;

      const wrap = document.createElement("div");
      wrap.style.cssText =
        "margin-bottom:8px; border:1px solid var(--border); border-radius:8px; overflow:hidden";

      // ---- 课程头 ----
      const head = document.createElement("div");
      head.className = "jrow click";
      head.style.cssText = "margin:0; border-radius:0; align-items:flex-start";

      const hcol = document.createElement("div");
      hcol.style.cssText = "flex:1;min-width:0;";
      const hname = document.createElement("div");
      hname.className = "jname";
      hname.textContent = unfiled ? c.name : `《${c.name}》`;
      hcol.appendChild(hname);

      const bits = [];
      if (c.n_docs) bits.push(`${c.n_docs} 份文档`);
      if (c.n_qas) bits.push(`${c.n_qas} 个问题`);
      const when = String(c.last_active || "").replace("T", " ").slice(0, 16);
      if (when) bits.push(`最近 ${when}`);
      const hmeta = document.createElement("div");
      hmeta.className = "jinfo";
      hmeta.textContent = bits.join(" · ");
      hcol.appendChild(hmeta);

      if (unfiled) {
        const tip = document.createElement("div");
        tip.className = "jinfo";
        tip.style.cssText = "color:#b26a00; margin-top:2px";
        tip.textContent = "这些记录建档时没填课程名。下次就同一批课件提问/生成时填上，会自动归过来。";
        hcol.appendChild(tip);
      }
      head.appendChild(hcol);

      const arrow = document.createElement("span");
      arrow.className = "jopen";
      arrow.textContent = "展开 ▾";
      head.appendChild(arrow);
      wrap.appendChild(head);

      // ---- 课程体 ----
      const body = document.createElement("div");
      body.className = "hidden";
      body.style.cssText = "padding:0 12px 10px";
      wrap.appendChild(body);

      // 默认展开最新的一门：进页面第一眼就能看到最近在做什么，又不会一屏铺满
      let opened = idx === 0;
      const syncBody = () => {
        body.classList.toggle("hidden", !opened);
        arrow.textContent = opened ? "收起 ▴" : "展开 ▾";
      };
      head.addEventListener("click", () => { opened = !opened; syncBody(); });

      if (c.n_docs) {
        body.appendChild(sectionLabel("复习文档"));
        c.docs.forEach((d) => body.appendChild(docRow(d)));
      }
      if (c.n_qas) {
        body.appendChild(sectionLabel("问过的问题"));
        c.qas.forEach((q) => body.appendChild(qaRow(q)));
      }
      if (!c.n_docs && !c.n_qas) {
        body.appendChild(sectionLabel("（没有记录）"));
      }

      syncBody();
      box.appendChild(wrap);
    });
  }

  function sectionLabel(text) {
    const el = document.createElement("div");
    el.className = "meta";
    el.style.cssText = "margin:8px 0 4px; font-weight:600; color:var(--fg, inherit)";
    el.textContent = text;
    return el;
  }

  function docRow(d) {
    const openable = d.openable;
    const row = document.createElement("div");
    row.className = "jrow" + (openable ? " click" : "");
    row.style.cssText = "margin:0 0 4px; padding:6px 8px";

    const col = document.createElement("div");
    col.style.cssText = "flex:1;min-width:0;";
    const name = document.createElement("div");
    name.className = "jinfo";
    name.textContent = "复习总册";
    col.appendChild(name);

    const bits = [H_STATUS[d.status] || d.status, `完成 ${d.done}/${d.total}`];
    if (d.error) bits.push(`${d.error} 失败`);
    if (d.spent_yuan > 0) bits.push(`约 ¥${d.spent_yuan}`);
    if (d.updated_at) bits.push(String(d.updated_at).slice(0, 16));
    const meta = document.createElement("div");
    meta.className = "jinfo";
    meta.textContent = bits.join(" · ");
    col.appendChild(meta);
    row.appendChild(col);

    const tail = document.createElement("span");
    tail.className = "jopen";
    tail.textContent = openable ? "查看 →" : "无内容";
    row.appendChild(tail);
    if (openable) row.addEventListener("click", () => openHistoryBatch(d.batch_id));
    return row;
  }

  // 提问行：点开就地展开**那一次**的答案，而不是重问。
  // 重问需要知识库缓存还在（72 小时），而历史的意义恰是"过很久还能翻"——
  // 所以这里只读已存的答案，零依赖、零成本、永不失败。
  function qaRow(q) {
    const wrap = document.createElement("div");
    wrap.style.cssText = "border-bottom:1px dashed var(--border); padding:6px 0";

    const head = document.createElement("div");
    head.style.cssText = "cursor:pointer; display:flex; gap:8px; align-items:baseline";

    const when = String(q.ts || "").replace("T", " ").slice(5, 16);
    const qtext = document.createElement("div");
    qtext.style.cssText = "flex:1;min-width:0;";
    qtext.innerHTML =
      `<a href="#" style="color:inherit">${linkifyHtml(q.question)}</a>`;

    const info = [];
    if (q.no_hit) {
      info.push('<span style="color:#b26a00">未检索到内容</span>');
    } else {
      info.push(`${q.n_hits} 处原文`);
    }
    const c4 = q.citations || {};
    if (c4.total) {
      const bad = (c4.weak || 0) + (c4.not_found || 0);
      const tip = bad
        ? `<span style="color:#b26a00">${bad} 条待核</span>`
        : "全部可信";
      info.push(`引用 ${c4.total} 条（${tip}）`);
    }
    const meta = document.createElement("div");
    meta.className = "jinfo";
    meta.innerHTML = `${linkifyHtml(when)} · ${info.join(" · ")} · ${q.elapsed_s || "?"}s`;
    qtext.appendChild(meta);
    head.appendChild(qtext);
    wrap.appendChild(head);

    const body = document.createElement("div");
    body.className = "hidden";
    body.style.cssText = "margin-top:8px; font-size:14px";
    wrap.appendChild(body);
    let built = false;

    head.addEventListener("click", (e) => {
      e.preventDefault();
      if (!built) {
        built = true;
        body.innerHTML = q.answer
          ? DOMPurify.sanitize(marked.parse(q.answer))
          : '<div class="meta">（这次提问没有生成答案）</div>';
      }
      body.classList.toggle("hidden");
    });
    return wrap;
  }

  async function openHistoryBatch(id) {
    bSetStatus("正在打开历史记录…");
    // 先把共享的结果卡片藏起来：切到批量 tab 时它可能还留着上一份内容，
    // 等新数据到了再显示，避免"闪一下别人的文档"。
    $("resultCard").classList.add("hidden");
    resultCardHiddenByTab = false;
    try {
      const res = await fetch(`/api/batch/${id}`);
      if (!res.ok) { bSetStatus("打开失败：任务不存在或已被清理", "error"); return; }
      const d = await res.json();
      stopBatchPolling();
      batchId = d.batch_id;
      switchTab("batch");

      if (d.status === "running") {          // 还在跑：进运行视图接着跟进度
        enterBatchRun(d);
        return;
      }
      if (!d.done) { bSetStatus("该任务没有成功生成的课件，无法查看", "error"); return; }
      bSetStatus("");
      $("batchSetupCard").classList.add("hidden");
      $("batchRunCard").classList.remove("hidden");
      $("batchRunTitle").textContent = `《${d.course_name}》复习总册`;
      renderBatchStatus(d);                  // 按状态显隐「停止/重试/返回新建」
      await loadBatchResult(d);
    } catch { bSetStatus("网络错误，打开失败", "error"); }
  }

  // 刷新恢复：有进行中/暂停的批任务时，自动回到批量视图接着跟进度
  async function checkRunningBatch() {
    try {
      const res = await fetch("/api/batches");
      const { batches } = await res.json();
      const act = (batches || []).find((b) => b.status === "running" || b.status === "paused");
      if (!act) return;
      batchId = act.batch_id;
      switchTab("batch");
      const d = await (await fetch(`/api/batch/${batchId}`)).json();
      enterBatchRun(d);
    } catch { /* 忽略：不影响正常使用 */ }
  }

  // 启动时检查是否已配置 key；没有则强制弹出配置面板
  (async function checkSettingsOnBoot() {
    try {
      const res = await fetch("/api/settings/status");
      currentStatus = await res.json();
      updateCfgCurrent(currentStatus);
      if (!currentStatus.configured) openCfg(true);
    } catch { /* 后端未启动等场景跳过，不阻塞页面 */ }
    checkRunningBatch();
    loadHistory();
  })();
