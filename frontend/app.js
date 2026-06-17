/* ============================================================
   AI原生学习 前端逻辑
   - 提交 URL → 后台下载/转录/翻译，SSE 流式推进度与字幕
   - 本地 <video> 播放，currentTime 驱动视频下方双语字幕条
   ============================================================ */
const $ = (id) => document.getElementById(id);
const tr = (k, p) => I18n.t(k, p);

const state = {
  jobId: "",
  status: "",
  srcLang: "",
  duration: 0,
  segs: [],          // [{index,start,end,source,target}]
  llm: false,
  progress: {        // 各阶段进度
    download: 0,
    transcode: 0,
    transcribe: { done: 0, total: 0 },
    translate: { done: 0, total: 0 },
  },
};
let es = null;
let curIdx = -1;

const video = $("video");

function escapeHtml(s) {
  return (s || "").replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
}

function toast(msg, ms = 2400) {
  const el = $("toast");
  el.textContent = msg;
  el.classList.add("show");
  clearTimeout(toast._t);
  toast._t = setTimeout(() => el.classList.remove("show"), ms);
}

function fmt(sec) {
  sec = Math.max(0, Math.floor(sec || 0));
  return String(Math.floor(sec / 60)).padStart(2, "0") + ":" + String(sec % 60).padStart(2, "0");
}

// ---------- 状态条 ----------
function setStatusDot(kind) {
  const dot = $("statusDot");
  dot.className = "status-dot" + (kind ? " " + kind : "");
}

function setProgressPct(pct) {
  const wrap = $("bannerProg");
  if (pct == null) { wrap.style.display = "none"; return; }
  wrap.style.display = "";
  wrap.firstElementChild.style.width = Math.max(0, Math.min(100, pct)) + "%";
}

function showBanner(text, dotKind, pct) {
  $("banner").classList.add("show");
  $("bannerText").textContent = text;
  setStatusDot(dotKind);
  setProgressPct(pct == null ? null : pct);
}

function updateMeta() {
  $("metaInfo").textContent = state.segs.length
    ? `${state.segs.length} ${I18n.getLocale() === "zh" ? "段" : "segs"} · ${fmt(state.duration)}`
    : "";
}

// ---------- 提交 URL / 本地路径 ----------
$("urlForm").addEventListener("submit", async (e) => {
  e.preventDefault();
  const src = $("urlInput").value.trim();
  if (!src) return;
  await submitJob(src);
});

// ---------- 选择本地文件 ----------
// 桌面客户端（Electron）：用原生对话框拿本地路径直接提交，避免拷贝大文件；
// 浏览器：退回 <input type=file> 上传。
$("pickBtn").onclick = async () => {
  if (window.desktop && typeof window.desktop.pickVideo === "function") {
    const p = await window.desktop.pickVideo();
    if (p) {
      $("urlInput").value = p;
      await submitJob(p);
    }
    return;
  }
  $("fileInput").click();
};
$("fileInput").addEventListener("change", async (e) => {
  const file = e.target.files && e.target.files[0];
  e.target.value = "";
  if (!file) return;
  await uploadFile(file);
});

function setPlaceholder(visible) {
  const el = $("placeholder");
  if (el) el.style.display = visible ? "" : "none";
}

function startJobFromSnapshot(data) {
  state.jobId = data.id;
  setPlaceholder(false);
  applySnapshot(data);
  openEvents(data.id);
  if (window.LearningPanel) window.LearningPanel.onJobStart(data.id);
}

async function submitJob(src) {
  resetForNewJob();
  showBanner(tr("submitting"), "active");
  $("loadBtn").disabled = true;
  try {
    const res = await fetch("/api/job", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ source: src }),
    });
    const data = await res.json();
    if (!res.ok || data.error) {
      const msg = errMsg(data, res);
      showBanner(tr("submitFailed", { msg }), "error");
      toast(msg, 4000);
      return;
    }
    startJobFromSnapshot(data);
  } catch (err) {
    showBanner(tr("submitFailed", { msg: err.message }), "error");
  } finally {
    $("loadBtn").disabled = false;
  }
}

/** 从响应里提取可读错误：优先自定义 error，其次 Pydantic detail，最后状态文本。 */
function errMsg(data, res) {
  if (data && data.error) return data.error;
  if (data && data.detail) {
    if (typeof data.detail === "string") return data.detail;
    if (Array.isArray(data.detail) && data.detail[0]?.msg) return data.detail[0].msg;
  }
  return res.statusText;
}

async function uploadFile(file) {
  resetForNewJob();
  showBanner(tr("uploading"), "active");
  $("urlInput").value = file.name;
  const fd = new FormData();
  fd.append("file", file, file.name);
  try {
    const res = await fetch("/api/job/upload", { method: "POST", body: fd });
    const data = await res.json();
    if (!res.ok || data.error) {
      const msg = errMsg(data, res);
      showBanner(tr("submitFailed", { msg }), "error");
      toast(msg, 4000);
      return;
    }
    startJobFromSnapshot(data);
  } catch (err) {
    showBanner(tr("submitFailed", { msg: err.message }), "error");
  }
}

function resetForNewJob() {
  if (es) { es.close(); es = null; }
  state.jobId = "";
  state.status = "";
  state.srcLang = "";
  state.duration = 0;
  state.segs = [];
  state.progress = {
    download: 0, transcode: 0,
    transcribe: { done: 0, total: 0 },
    translate: { done: 0, total: 0 },
  };
  curIdx = -1;
  $("playerWrap").hidden = true;
  setPlaceholder(true);
  $("capSource").textContent = "";
  $("capTarget").textContent = "";
  video.removeAttribute("src");
  video.load();
  if (window.LearningPanel) window.LearningPanel.onJobReset();
}

function applySnapshot(data) {
  state.status = data.status;
  state.srcLang = data.src_lang || "";
  state.duration = data.duration || 0;
  if (Array.isArray(data.segments)) state.segs = data.segments.slice();
  if (data.video_url) setVideoSource(data.video_url);
  renderStatus();
  updateMeta();
  notifyLearningPanel();
}

function notifyLearningPanel() {
  if (window.LearningPanel && state.jobId) {
    window.LearningPanel.onSegmentsUpdate(state.segs);
  }
}

// ---------- SSE ----------
function openEvents(jobId) {
  es = new EventSource(`/api/job/${jobId}/events`);
  es.onmessage = (ev) => {
    let msg;
    try { msg = JSON.parse(ev.data); } catch { return; }
    handleEvent(msg);
  };
  es.onerror = () => {
    // 任务结束后服务端会关闭流，浏览器会自动重连；若已 done 则主动关闭
    if (state.status === "done" || state.status === "error") {
      if (es) { es.close(); es = null; }
    }
  };
}

function handleEvent(msg) {
  switch (msg.type) {
    case "status":
      state.status = msg.status;
      renderStatus();
      break;
    case "ready":
      state.duration = msg.duration || state.duration;
      if (msg.video_url) setVideoSource(msg.video_url);
      renderStatus();
      updateMeta();
      break;
    case "lang":
      state.srcLang = msg.src_lang || "";
      break;
    case "segment": {
      const i = msg.index;
      state.segs[i] = {
        index: i, start: msg.start, end: msg.end,
        source: msg.source || "", target: (state.segs[i] && state.segs[i].target) || "",
      };
      renderStatus();
      updateMeta();
      refreshCaption();
      notifyLearningPanel();
      break;
    }
    case "translated": {
      const i = msg.index;
      if (state.segs[i]) state.segs[i].target = msg.target || "";
      renderStatus();
      refreshCaption();
      break;
    }
    case "progress":
      if (msg.phase === "download") state.progress.download = msg.percent || 0;
      else if (msg.phase === "transcode") state.progress.transcode = msg.percent || 0;
      else if (msg.phase === "transcribe") state.progress.transcribe = { done: msg.done || 0, total: msg.total || 0 };
      else if (msg.phase === "translate") state.progress.translate = { done: msg.done || 0, total: msg.total || 0 };
      renderStatus();
      break;
    case "error":
      state.status = "error";
      showBanner(tr("statusError", { msg: msg.message }), "error");
      toast(msg.message, 5000);
      break;
    case "done":
      state.status = "done";
      renderStatus();
      if (es) { es.close(); es = null; }
      break;
  }
}

function setVideoSource(url) {
  setPlaceholder(false);
  if (video.getAttribute("src") === url) {
    $("playerWrap").hidden = false;
    return;
  }
  video.src = url;
  video.load();
  $("playerWrap").hidden = false;
}

function transcribedCount() { return state.segs.filter((s) => s && s.source).length; }
function translatedCount() { return state.segs.filter((s) => s && s.target).length; }

function pctOf(done, total) { return total ? Math.round((done / total) * 100) : 0; }

// 从头连续「转录+翻译均完成」的前缀时长（秒）：标识可正常观看的时长
function readySeconds() {
  let end = 0;
  for (let i = 0; i < state.segs.length; i++) {
    const s = state.segs[i];
    if (!s || !s.source || !s.target) break; // 遇到首个未就绪段即停
    end = s.end;
  }
  return end;
}

function renderStatus() {
  const p = state.progress;
  switch (state.status) {
    case "queued":
      showBanner(tr("statusQueued"), "active");
      break;
    case "downloading":
      showBanner(tr("statusDownloading", { pct: Math.round(p.download) }), "active", p.download);
      break;
    case "transcoding":
      showBanner(tr("statusTranscoding", { pct: Math.round(p.transcode) }), "active", p.transcode);
      break;
    case "ready":
      showBanner(tr("statusReady"), "active");
      break;
    // 转录与翻译并行：统一展示「正在处理字幕」组合进度
    case "transcribing":
    case "translating": {
      const t = p.transcribe;
      if (!t.total) {
        showBanner(tr("statusTranscribingInit"), "active");
        break;
      }
      const nTotal = transcribedCount();
      const nDone = translatedCount();
      const readySec = readySeconds();
      const readyMin = (readySec / 60).toFixed(1);
      // 进度条以「已就绪时长 / 总时长」为准，最贴合“能看多少”
      const pct = state.duration
        ? Math.round((readySec / state.duration) * 100)
        : pctOf(nDone, nTotal);
      showBanner(
        tr("statusSubtitle", { tDone: t.done, tTotal: t.total, nDone, nTotal, readyMin }),
        "active",
        pct,
      );
      break;
    }
    case "done":
      showBanner(tr("statusDone", { n: state.segs.length }), "done");
      break;
    case "error":
      setStatusDot("error");
      break;
    default:
      // 其他阶段兜底：正在进行 {phase}，进度 {pct}%
      showBanner(tr("statusGeneric", { phase: state.status || "", pct: 0 }), "active");
      break;
  }
}

// ---------- 字幕条（由 video.currentTime 驱动）----------
function currentSegIndex(t) {
  // 命中 [start,end) 的段；找不到则取最近的已过段
  for (let i = 0; i < state.segs.length; i++) {
    const s = state.segs[i];
    if (!s) continue;
    if (t >= s.start && t < s.end) return i;
  }
  // 退而求其次：最后一个 start <= t 的段
  let best = -1;
  for (let i = 0; i < state.segs.length; i++) {
    const s = state.segs[i];
    if (s && t >= s.start) best = i;
  }
  return best;
}

function refreshCaption(force) {
  const idx = currentSegIndex(video.currentTime || 0);
  if (idx === curIdx && !force) return;
  curIdx = idx;
  const seg = state.segs[idx];
  if (!seg) {
    $("capSource").textContent = "";
    $("capTarget").textContent = "";
    return;
  }
  $("capSource").textContent = seg.source || "";
  const tgt = $("capTarget");
  if (seg.target) {
    tgt.textContent = seg.target;
    tgt.classList.remove("pending");
  } else if (state.status === "translating" || state.status === "transcribing") {
    tgt.textContent = tr("pendingTranslate");
    tgt.classList.add("pending");
  } else {
    tgt.textContent = "";
    tgt.classList.remove("pending");
  }
}

video.addEventListener("timeupdate", () => refreshCaption());
video.addEventListener("seeked", () => refreshCaption(true));

// ---------- 全屏（对 video-box 整体全屏，使叠加字幕同步显示）----------
const videoBox = $("videoBox");
function toggleFullscreen() {
  if (document.fullscreenElement) {
    document.exitFullscreen();
  } else if (videoBox && videoBox.requestFullscreen) {
    videoBox.requestFullscreen().catch(() => {});
  }
}
$("fsBtn").addEventListener("click", toggleFullscreen);
video.addEventListener("dblclick", toggleFullscreen);
document.addEventListener("fullscreenchange", () => {
  $("fsBtn").textContent = document.fullscreenElement ? "🗗" : "⛶";
});

// ---------- 控件 ----------
$("viewToggle").addEventListener("click", (e) => {
  const btn = e.target.closest("button");
  if (!btn) return;
  document.body.dataset.view = btn.dataset.view;
  [...$("viewToggle").children].forEach((b) => b.classList.toggle("active", b === btn));
  if (window.LearningPanel && window.LearningPanel.onViewChange) {
    window.LearningPanel.onViewChange(btn.dataset.view);
  }
});

$("themeBtn").onclick = () => {
  document.body.dataset.theme = document.body.dataset.theme === "dark" ? "light" : "dark";
  localStorage.setItem("vt_theme", document.body.dataset.theme);
};

$("font").oninput = (e) => {
  document.body.style.setProperty("--fs", e.target.value + "px");
  $("fontVal").textContent = e.target.value;
};

// 播放倍速：仅改 video.playbackRate；字幕由 currentTime 驱动，自动同步
function applyPlaybackRate() {
  const rate = parseFloat($("speed").value) || 1;
  if (video.playbackRate !== rate) video.playbackRate = rate;
}
$("speed").onchange = applyPlaybackRate;
// 切换视频源时浏览器会把 playbackRate 重置为 1，需重新应用所选倍速
video.addEventListener("loadedmetadata", applyPlaybackRate);
video.addEventListener("ratechange", () => {
  const cur = String(video.playbackRate);
  if ([...$("speed").options].some((o) => o.value === cur)) $("speed").value = cur;
});

$("langBtn").onclick = () => {
  const next = I18n.getLocale() === "zh" ? "en" : "zh";
  I18n.setLocale(next);
  I18n.applyPageI18n();
  $("langBtn").textContent = next === "zh" ? "EN" : "中";
  if (window.LearningPanel && window.LearningPanel.applyI18n) {
    window.LearningPanel.applyI18n();
  }
  renderStatus();
  updateMeta();
  refreshCaption(true);
};

// ---------- 翻译设置 ----------
let translateConfig = null;

async function openSettings() {
  try {
    const res = await fetch("/api/translate-config");
    const cfg = await res.json();
    if (!res.ok) throw new Error(cfg.error || res.statusText);
    translateConfig = cfg;
    fillSettings(cfg);
    $("settingsModal").hidden = false;
  } catch (e) {
    toast(e.message, 3500);
  }
}

function fillSettings(cfg) {
  $("cfgBaseUrl").value = cfg.base_url || "";
  $("cfgApiKey").value = "";
  $("cfgModel").value = cfg.model || "";
  $("cfgTemperature").value = cfg.temperature ?? 0.3;
  $("cfgTemperatureVal").textContent = Number(cfg.temperature ?? 0.3).toFixed(2);
  $("cfgMaxTokens").value = cfg.max_tokens ?? 4096;
  $("cfgBatchSize").value = cfg.batch_size ?? 16;
  const keyOk = cfg.api_key_configured;
  $("settingsMeta").innerHTML = [
    `Endpoint：<strong>${escapeHtml(cfg.base_url_host || "-")}</strong> (${escapeHtml(cfg.base_url_source || "-")})`,
    `API Key：${keyOk ? `<span class="ok">已配置 ${escapeHtml(cfg.api_key_hint || "")}</span>` : `<span class="warn">未配置</span>`}`,
    `翻译可用：${cfg.llm_available ? `<span class="ok">是</span>` : `<span class="warn">否</span>`}`,
  ].join("<br>");
}

async function saveSettings() {
  const body = {
    base_url: $("cfgBaseUrl").value.trim(),
    api_key: $("cfgApiKey").value.trim(),
    model: $("cfgModel").value.trim(),
    temperature: parseFloat($("cfgTemperature").value),
    max_tokens: parseInt($("cfgMaxTokens").value, 10),
    batch_size: parseInt($("cfgBatchSize").value, 10),
  };
  if (!body.model) { toast(tr("modelRequired")); return; }
  if (!body.base_url) { toast(tr("baseUrlRequired")); return; }
  if (!body.api_key && !translateConfig?.api_key_configured) { toast(tr("apiKeyRequired")); return; }
  try {
    const res = await fetch("/api/translate-config", {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    const data = await res.json();
    if (!res.ok || data.error) { toast(tr("saveFailed", { msg: data.error || res.statusText }), 3500); return; }
    translateConfig = data;
    state.llm = data.llm_available;
    $("settingsModal").hidden = true;
    toast(tr("saveOk"));
  } catch (e) {
    toast(tr("saveFailed", { msg: e.message }), 3500);
  }
}

async function resetSettings() {
  try {
    const res = await fetch("/api/translate-config/reset", { method: "POST" });
    const data = await res.json();
    if (!res.ok || data.error) throw new Error(data.error || res.statusText);
    translateConfig = data;
    fillSettings(data);
    toast(tr("resetOk"));
  } catch (e) {
    toast(e.message, 3500);
  }
}

$("settingsBtn").onclick = openSettings;
$("settingsClose").onclick = () => ($("settingsModal").hidden = true);
$("settingsCancel").onclick = () => ($("settingsModal").hidden = true);
$("settingsSave").onclick = saveSettings;
$("settingsReset").onclick = resetSettings;
$("cfgTemperature").oninput = (e) => ($("cfgTemperatureVal").textContent = parseFloat(e.target.value).toFixed(2));
$("settingsModal").addEventListener("click", (e) => {
  if (e.target === e.currentTarget) $("settingsModal").hidden = true;
});
document.addEventListener("keydown", (e) => {
  if (e.code === "Escape" && !$("settingsModal").hidden) $("settingsModal").hidden = true;

  // 输入框/弹窗中不拦截
  const tag = (e.target.tagName || "").toLowerCase();
  if (tag === "input" || tag === "textarea" || e.target.isContentEditable) return;
  if (!$("settingsModal").hidden) return;
  if ($("playerWrap").hidden || !video.getAttribute("src")) return;

  if (e.code === "Space" || e.code === "KeyK") {
    e.preventDefault();
    if (video.paused) video.play(); else video.pause();
  } else if (e.code === "ArrowLeft") {
    e.preventDefault();
    video.currentTime = Math.max(0, video.currentTime - 5);
  } else if (e.code === "ArrowRight") {
    e.preventDefault();
    video.currentTime = Math.min(video.duration || Infinity, video.currentTime + 5);
  } else if (e.code === "KeyF") {
    e.preventDefault();
    toggleFullscreen();
  }
});

// ---------- 初始化 ----------
window.LearnBridge = {
  seek(t) {
    if (video && !isNaN(t)) {
      video.currentTime = t;
      video.play().catch(() => {});
    }
  },
  get jobId() { return state.jobId; },
  get segments() { return state.segs; },
  toast,
};

(function init() {
  const savedTheme = localStorage.getItem("vt_theme");
  if (savedTheme) document.body.dataset.theme = savedTheme;
  $("langBtn").textContent = I18n.getLocale() === "zh" ? "EN" : "中";
  fetch("/api/health").then((r) => r.json()).then((h) => {
    state.llm = !!h.llm;
    if (!h.stt) toast(tr("noStt"), 5000);
  }).catch(() => {});
})();
