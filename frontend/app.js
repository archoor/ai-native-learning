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
  title: "",
  duration: 0,
  contentKind: "video", // 'video' | 'text'：决定显示播放器还是阅读器
  segs: [],          // [{index,start,end,source,target,kind?}]
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
let lastSource = "";

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

function hideBanner() { $("banner").classList.remove("show"); }

// ---------- 处理进度环（下载/转码） ----------
const RING_CIRC = 2 * Math.PI * 52; // r=52 → 周长 ≈ 326.7
const STEP_ORDER = ["dl", "tc", "tr", "mt"];

function setRing(pct) {
  const bar = $("ringBar");
  const txt = $("ringPct");
  const p = Math.max(0, Math.min(100, pct || 0));
  if (bar) bar.style.strokeDashoffset = String(RING_CIRC * (1 - p / 100));
  if (txt) txt.textContent = Math.round(p) + "%";
}

function setSteps(activeKey, doneKeys) {
  const done = doneKeys || [];
  document.querySelectorAll("#progSteps .step").forEach((s) => {
    const k = s.dataset.step;
    s.classList.toggle("active", k === activeKey);
    s.classList.toggle("done", done.includes(k));
  });
  document.querySelectorAll("#progSteps .step-line").forEach((l, i) => {
    l.classList.toggle("done", done.includes(STEP_ORDER[i]));
  });
}

function showProcessing(stageText, subText, pct, activeKey, doneKeys) {
  hideBanner();
  setPlaceholder(false);
  // 已有视频源时（如命中缓存后回放早期状态），避免把播放器误隐藏成白屏。
  if (!video.getAttribute("src")) $("playerWrap").hidden = true;
  const v = $("vprog");
  v.hidden = false;
  v.classList.add("show");
  $("progStage").textContent = stageText;
  $("progSub").textContent = subText || "";
  setRing(pct);
  setSteps(activeKey, doneKeys || []);
}

function hideProcessing() {
  const v = $("vprog");
  v.hidden = true;
  v.classList.remove("show");
}

function ensurePlayerVisible() {
  if (state.contentKind !== "video") return;
  if (!video.getAttribute("src")) return;
  setPlaceholder(false);
  $("playerWrap").hidden = false;
}

// ---------- 字幕生成进度条（转录/翻译并行） ----------
function showSubtitleBar(label, pct, statHtml, done) {
  const bar = $("subBar");
  if (!bar) return;
  bar.classList.add("show");
  bar.classList.toggle("is-done", !!done);
  $("subLabel").textContent = label || "";
  $("subFill").style.width = Math.max(0, Math.min(100, pct || 0)) + "%";
  $("subStat").innerHTML = statHtml || "";
}

function hideSubtitleBar() {
  const bar = $("subBar");
  if (bar) { bar.classList.remove("show"); bar.classList.remove("is-done"); }
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
  if (data.url) lastSource = data.url;
  setPlaceholder(false);
  applySnapshot(data);
  openEvents(data.id);
  if (window.LearningPanel) window.LearningPanel.onJobStart(data.id);
}

async function submitJob(src) {
  resetForNewJob();
  lastSource = src;
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
    const canonical = data.url || src;
    lastSource = canonical;
    await fetchHistory();
    await pushHistory(canonical, data.title || displayNameFromSource(canonical));
    startJobFromSnapshot(data);
  } catch (err) {
    showBanner(tr("submitFailed", { msg: err.message }), "error");
  } finally {
    $("loadBtn").disabled = false;
  }
}

// ---------- 历史视频（最近 10 个，按打开时间倒序，持久化到后端）----------
const HISTORY_KEY = "anl_history"; // 旧版 localStorage，仅用于一次性迁移
const HISTORY_MAX = 10;
let historyCache = [];

function displayNameFromSource(src) {
  if (/^https?:\/\//i.test(src)) return src;
  return src.split(/[\\/]/).pop() || src;
}

async function fetchHistory() {
  try {
    const res = await fetch("/api/history");
    const data = await res.json();
    if (res.ok && Array.isArray(data.items)) {
      historyCache = data.items.slice(0, HISTORY_MAX);
      return historyCache;
    }
  } catch {}
  return historyCache;
}

/** 将旧版 localStorage 历史迁移到后端（仅在后端为空时执行一次）。 */
async function migrateHistoryFromLocalStorage() {
  let legacy = [];
  try {
    legacy = JSON.parse(localStorage.getItem(HISTORY_KEY) || "[]");
    if (!Array.isArray(legacy)) legacy = [];
  } catch { legacy = []; }
  if (!legacy.length) return;

  const current = await fetchHistory();
  if (current.length) {
    localStorage.removeItem(HISTORY_KEY);
    return;
  }

  const sorted = legacy
    .filter((x) => x && x.source)
    .sort((a, b) => (a.ts || 0) - (b.ts || 0));
  for (const it of sorted) {
    await pushHistory(it.source, it.name, { silent: true });
  }
  localStorage.removeItem(HISTORY_KEY);
}

/** 按 source 去重前插，名称以最新为准；上限 HISTORY_MAX。 */
async function pushHistory(source, name, opts = {}) {
  if (!source) return;
  try {
    const res = await fetch("/api/history", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        source,
        name: name || displayNameFromSource(source),
      }),
    });
    const data = await res.json();
    if (res.ok && Array.isArray(data.items)) {
      historyCache = data.items.slice(0, HISTORY_MAX);
    }
  } catch {}
  if (!opts.silent && !$("historyPanel").hidden) renderHistoryPanel();
}

// ---------- 播放进度（按 source 持久化到后端历史 + localStorage 兜底）----------
const POSITION_MIN_SEC = 5;
const POSITION_END_RATIO = 0.95;
const POSITION_SAVE_MS = 5000;
const POS_LOCAL_KEY = "anl_playback_pos";
let lastSavedPosition = 0;
let lastPositionSaveAt = 0;
let pendingRestorePosition = null;
let restoreAttempted = false;

function readLocalPositions() {
  try {
    const raw = localStorage.getItem(POS_LOCAL_KEY);
    const map = raw ? JSON.parse(raw) : {};
    return map && typeof map === "object" ? map : {};
  } catch {
    return {};
  }
}

function writeLocalPosition(source, pos) {
  if (!source) return;
  try {
    const map = readLocalPositions();
    if (pos > 0) map[source] = pos;
    else delete map[source];
    localStorage.setItem(POS_LOCAL_KEY, JSON.stringify(map));
  } catch {}
}

function positionForSource(source) {
  if (!source) return 0;
  const it = historyCache.find((x) => x.source === source);
  const fromHistory = it && it.position_sec > 0 ? Number(it.position_sec) : 0;
  const fromLocal = Number(readLocalPositions()[source] || 0);
  return Math.max(fromHistory, fromLocal);
}

function planRestorePosition() {
  restoreAttempted = false;
  if (!lastSource || state.contentKind !== "video") {
    pendingRestorePosition = null;
    return;
  }
  const pos = positionForSource(lastSource);
  pendingRestorePosition = pos >= POSITION_MIN_SEC ? pos : null;
}

function postPositionPayload(source, pos) {
  const payload = JSON.stringify({ source, position_sec: pos });
  if (navigator.sendBeacon) {
    const blob = new Blob([payload], { type: "application/json" });
    return navigator.sendBeacon("/api/history/position", blob);
  }
  fetch("/api/history/position", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: payload,
    keepalive: true,
  }).catch(() => {});
  return true;
}

async function savePlaybackPosition(force = false) {
  if (!lastSource || state.contentKind !== "video") return;
  if (!video.getAttribute("src")) return;
  const dur = video.duration;
  if (!dur || !Number.isFinite(dur)) return;

  const t = video.currentTime || 0;
  const now = Date.now();
  if (!force && now - lastPositionSaveAt < POSITION_SAVE_MS) return;
  if (!force && Math.abs(t - lastSavedPosition) < 1) return;

  let pos = t;
  if (t >= dur * POSITION_END_RATIO) pos = 0;

  lastPositionSaveAt = now;
  lastSavedPosition = pos;
  writeLocalPosition(lastSource, pos);

  const it = historyCache.find((x) => x.source === lastSource);
  if (it) {
    if (pos > 0) it.position_sec = pos;
    else delete it.position_sec;
  }

  if (force) {
    postPositionPayload(lastSource, pos);
    return;
  }
  try {
    const res = await fetch("/api/history/position", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ source: lastSource, position_sec: pos }),
    });
    const data = await res.json();
    if (res.ok && Array.isArray(data.items)) {
      historyCache = data.items.slice(0, HISTORY_MAX);
    }
  } catch {}
}

function restorePlaybackPosition() {
  if (restoreAttempted || pendingRestorePosition == null) {
    if (pendingRestorePosition == null) forceFirstFrame();
    return;
  }
  const dur = video.duration;
  if (!dur || !Number.isFinite(dur)) return;

  const pos = pendingRestorePosition;
  if (pos >= dur * POSITION_END_RATIO || pos < POSITION_MIN_SEC) {
    pendingRestorePosition = null;
    restoreAttempted = true;
    forceFirstFrame();
    return;
  }

  const doSeek = () => {
    if (restoreAttempted || pendingRestorePosition == null) return;
    restoreAttempted = true;
    pendingFirstFrame = false;
    pendingRestorePosition = null;
    const target = pos;
    const onSeeked = () => {
      video.removeEventListener("seeked", onSeeked);
      if (Math.abs(video.currentTime - target) > 2) return;
      toast(tr("resumeFrom", { time: fmt(target) }));
      refreshCaption(true);
    };
    video.addEventListener("seeked", onSeeked);
    try {
      video.currentTime = target;
    } catch {
      video.removeEventListener("seeked", onSeeked);
      forceFirstFrame();
    }
  };

  if (video.readyState >= 2) doSeek();
  else video.addEventListener("canplay", doSeek, { once: true });
}

async function removeHistory(source) {
  try {
    const res = await fetch("/api/history/remove", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ source }),
    });
    const data = await res.json();
    if (res.ok && Array.isArray(data.items)) {
      historyCache = data.items.slice(0, HISTORY_MAX);
    }
  } catch {}
  renderHistoryPanel();
}

function renderHistoryPanel() {
  const panel = $("historyPanel");
  if (!panel) return;
  const list = historyCache.slice().sort((a, b) => (b.opened_at || 0) - (a.opened_at || 0));
  if (!list.length) {
    panel.innerHTML = `<div class="history-empty">${tr("historyEmpty")}</div>`;
    return;
  }
  panel.innerHTML = list.map((it) => {
    const isUrl = /^https?:\/\//i.test(it.source);
    const name = escapeHtml(it.name || it.source);
    return `<div class="history-item" data-src="${escapeHtml(it.source)}" title="${escapeHtml(it.source)}">`
      + `<span class="hi-icon">${isUrl ? "🔗" : "🎬"}</span>`
      + `<span class="hi-name">${name}</span>`
      + `<button class="hi-del" type="button" data-del="${escapeHtml(it.source)}" title="${escapeHtml(tr("historyRemove"))}">✕</button>`
      + `</div>`;
  }).join("");

  panel.querySelectorAll(".history-item").forEach((el) => {
    el.addEventListener("click", (e) => {
      if (e.target.closest(".hi-del")) return;
      const src = el.dataset.src;
      closeHistoryPanel();
      $("urlInput").value = src;
      submitJob(src);
    });
  });
  panel.querySelectorAll(".hi-del").forEach((btn) => {
    btn.addEventListener("click", (e) => {
      e.stopPropagation();
      removeHistory(btn.dataset.del);
    });
  });
}

async function openHistoryPanel() {
  await fetchHistory();
  renderHistoryPanel();
  $("historyPanel").hidden = false;
  $("historyBtn").setAttribute("aria-expanded", "true");
}
function closeHistoryPanel() {
  const p = $("historyPanel");
  if (p) p.hidden = true;
  $("historyBtn").setAttribute("aria-expanded", "false");
}
function toggleHistoryPanel() {
  if ($("historyPanel").hidden) openHistoryPanel(); else closeHistoryPanel();
}

$("historyBtn").addEventListener("click", (e) => { e.stopPropagation(); toggleHistoryPanel(); });
document.addEventListener("click", (e) => {
  if (!$("historyPanel").hidden && !e.target.closest(".history-wrap")) closeHistoryPanel();
});

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
    if (data.url) lastSource = data.url;
    await fetchHistory();
    await pushHistory(lastSource, data.title || displayNameFromSource(lastSource));
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
  state.title = "";
  pendingRestorePosition = null;
  restoreAttempted = false;
  lastSavedPosition = 0;
  setContentKind("video");
  setNowPlaying("");
  hideProcessing();
  hideSubtitleBar();
  if (window.Reader) Reader.hide();
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
  if (data.content_kind) setContentKind(data.content_kind);
  if (data.title) {
    state.title = data.title;
    setNowPlaying(data.title);
    if (lastSource) pushHistory(lastSource, data.title);
  }
  if (Array.isArray(data.segments)) {
    state.segs = data.segments.slice();
    syncProgressFromSegments();
  }
  if (data.video_url) setVideoSource(data.video_url);
  renderStatus();
  updateMeta();
  notifyLearningPanel();
}

function setContentKind(kind) {
  state.contentKind = kind === "text" ? "text" : "video";
  document.body.dataset.content = state.contentKind;
}

// 文本事件成批到达（分段、批量译文）；用 rAF 合并，避免每条事件都整篇重渲染。
let textRenderScheduled = false;
function scheduleTextRender() {
  if (state.contentKind !== "text" || !window.Reader) return;
  if (textRenderScheduled) return;
  textRenderScheduled = true;
  requestAnimationFrame(() => {
    textRenderScheduled = false;
    Reader.render(state.segs, state.title);
    const t = state.progress.translate;
    Reader.setProgress(t.done || translatedCount(), t.total || transcribedCount());
  });
}

/** 文本来源（阅读器）的状态渲染：替代视频的进度环/字幕条。 */
function renderTextStatus() {
  if (window.Reader) Reader.show();
  $("playerWrap").hidden = true;
  hideProcessing();
  hideSubtitleBar();
  setPlaceholder(false);
  switch (state.status) {
    case "queued":
    case "fetching":
      hideBanner();
      if (window.Reader) Reader.setLoading(tr("readerFetching"));
      break;
    case "extracting":
      hideBanner();
      if (window.Reader) Reader.setLoading(tr("readerExtracting"));
      break;
    case "ready":
    case "translating":
      hideBanner();
      if (window.Reader) Reader.setTranslating(true);
      scheduleTextRender();
      break;
    case "done":
      hideBanner();
      if (window.Reader) {
        Reader.setTranslating(false);
        Reader.render(state.segs, state.title);
        Reader.setProgress(1, 1);
      }
      break;
    case "error":
      setStatusDot("error");
      break;
    default:
      break;
  }
}

function setNowPlaying(name) {
  const el = $("nowPlaying");
  if (!el) return;
  el.textContent = name || "";
  el.title = name || "";
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
      // ready 事件可能先于/晚于 status 事件到达，显式更新避免沿用 queued 状态。
      if (state.status !== "done" && state.status !== "error") state.status = "ready";
      state.duration = msg.duration || state.duration;
      if (msg.content_kind) setContentKind(msg.content_kind);
      if (msg.title) {
        state.title = msg.title;
        setNowPlaying(msg.title);
        if (lastSource) pushHistory(lastSource, msg.title);
      }
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
        kind: msg.kind || (state.segs[i] && state.segs[i].kind) || "p",
        no_translate: !!msg.no_translate,
      };
      syncProgressFromSegments();
      renderStatus();
      updateMeta();
      if (state.contentKind !== "text") refreshCaption();
      notifyLearningPanel();
      break;
    }
    case "translated": {
      const i = msg.index;
      if (state.segs[i]) state.segs[i].target = msg.target || "";
      syncProgressFromSegments();
      renderStatus();
      if (state.contentKind !== "text") refreshCaption();
      notifyLearningPanel();
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
  planRestorePosition();
  if (video.getAttribute("src") === url) {
    $("playerWrap").hidden = false;
    if (video.readyState >= 1) restorePlaybackPosition();
    else pendingFirstFrame = pendingRestorePosition == null;
    return;
  }
  video.src = url;
  pendingFirstFrame = pendingRestorePosition == null;
  video.load();
  $("playerWrap").hidden = false;
}

function transcribedCount() { return state.segs.filter((s) => s && s.source).length; }
function translatedCount() {
  return state.segs.filter((s) => s && (s.target || s.no_translate)).length;
}

/** 续接/快照回放时，用已有字幕段回填 progress，避免缺 total 误显示「分析音频」。 */
function syncProgressFromSegments() {
  const nTotal = transcribedCount();
  if (!nTotal) return;
  const nDone = translatedCount();
  if (!state.progress.transcribe.total) {
    state.progress.transcribe = { done: nTotal, total: nTotal };
  }
  if (!state.progress.translate.total) {
    state.progress.translate = { done: nDone, total: nTotal };
  }
}

function pctOf(done, total) { return total ? Math.round((done / total) * 100) : 0; }

// 从头连续「转录+翻译均完成」的前缀时长（秒）：标识可正常观看的时长
function readySeconds() {
  let end = 0;
  for (let i = 0; i < state.segs.length; i++) {
    const s = state.segs[i];
    if (!s || !s.source) break;
    if (!s.target && !s.no_translate) break; // 中文段无需译文，视为已就绪
    end = s.end;
  }
  return end;
}

let doneHideTimer = null;
function renderStatus() {
  if (doneHideTimer) { clearTimeout(doneHideTimer); doneHideTimer = null; }
  if (state.contentKind === "text") { renderTextStatus(); return; }
  const p = state.progress;
  switch (state.status) {
    case "queued":
      showProcessing(tr("progQueued"), tr("progQueuedSub"), 0, null, []);
      break;
    case "downloading":
      showProcessing(tr("progDownload"), tr("progDownloadSub"), p.download, "dl", []);
      break;
    case "transcoding":
      showProcessing(tr("progTranscode"), tr("progTranscodeSub"), p.transcode, "tc", ["dl"]);
      break;
    case "ready":
      hideProcessing();
      hideBanner();
      ensurePlayerVisible();
      showSubtitleBar(tr("progReadyPrep"), 0, "", false);
      break;
    // 转录与翻译并行：视频已可播放，进度移到视频卡顶部细条
    case "transcribing":
    case "translating": {
      hideProcessing();
      hideBanner();
      ensurePlayerVisible();
      syncProgressFromSegments();
      const t = state.progress.transcribe;
      const nTotal = transcribedCount();
      const nDone = translatedCount();
      const tTotal = t.total || nTotal;
      const tDone = t.total ? t.done : nTotal;
      // 仅「转录刚开始、尚无分段」时显示分析音频
      if (state.status === "transcribing" && !tTotal && nTotal === 0) {
        showSubtitleBar(tr("statusTranscribingInit"), 0, "", false);
        break;
      }
      const readySec = readySeconds();
      const pct = state.duration
        ? Math.round((readySec / state.duration) * 100)
        : pctOf(nDone, nTotal || tTotal);
      showSubtitleBar(
        tr("subGenerating"),
        pct,
        tr("subStat", {
          tDone: tDone,
          tTotal: tTotal || nTotal,
          nDone,
          nTotal: nTotal || tTotal,
        }),
        false,
      );
      break;
    }
    case "done":
      hideProcessing();
      hideBanner();
      ensurePlayerVisible();
      showSubtitleBar(
        tr("subReady"),
        100,
        `<span class="check">${tr("subDoneStat", { n: state.segs.length })}</span>`,
        true,
      );
      doneHideTimer = setTimeout(hideSubtitleBar, 3500);
      break;
    case "error":
      hideProcessing();
      setStatusDot("error");
      break;
    default:
      // 其他阶段兜底：用顶栏 banner 显示
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
  } else if ((state.status === "translating" || state.status === "transcribing") && !seg.no_translate) {
    tgt.textContent = tr("pendingTranslate");
    tgt.classList.add("pending");
  } else {
    tgt.textContent = "";
    tgt.classList.remove("pending");
  }
}

video.addEventListener("timeupdate", () => {
  refreshCaption();
  savePlaybackPosition();
});
video.addEventListener("seeked", () => refreshCaption(true));
video.addEventListener("pause", () => { savePlaybackPosition(true); });
function flushPlaybackPosition() { savePlaybackPosition(true); }
window.addEventListener("pagehide", flushPlaybackPosition);
window.addEventListener("beforeunload", flushPlaybackPosition);

// 首帧绘制：轻推 currentTime 强制 Chromium 解码并绘制第一帧，避免黑屏。
// 用一次性标志 + 多事件兜底（冷启动转码完成后 loadeddata 可能早于 seekable，单一事件不可靠）。
let pendingFirstFrame = false;
function forceFirstFrame() {
  if (!pendingFirstFrame) return;
  if (video.readyState < 1) return;
  if (!video.paused) { pendingFirstFrame = false; return; }
  if (video.currentTime >= 1) { pendingFirstFrame = false; return; }
  if (video.currentTime < 0.05) {
    try { video.currentTime = 0.08; } catch (_) { return; } // 失败保留标志，等下个事件重试
  }
  pendingFirstFrame = false;
}
video.addEventListener("loadeddata", forceFirstFrame);
video.addEventListener("canplay", forceFirstFrame);

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
video.addEventListener("loadedmetadata", () => {
  applyPlaybackRate();
  restorePlaybackPosition();
});
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

// ---------- 模型配置 ----------
let modelsConfig = null;

const CFG_TASKS = [
  { id: "translate", hint: "字幕翻译" },
  { id: "outline", hint: "骨架 / 信息地图" },
  { id: "highlight", hint: "主题高亮" },
  { id: "quiz", hint: "自测出题" },
  { id: "grade", hint: "答案批改" },
  { id: "feynman", hint: "费曼学习" },
  { id: "report", hint: "学习报告" },
];
const STT_BACKENDS = ["whisper", "cloud", "funasr"];
const STT_LABELS = { whisper: "本地 Whisper", cloud: "云端 STT", funasr: "Fun-ASR" };
const SAVED_SECRET_MASK = "••••••••••••••••";
const SECRET_INPUT_IDS = ["cfgApiKey", "sttCloudKey", "sttFunKey", "sttOssSk"];

function fillSecretField(input, configured, hint) {
  if (!input) return;
  input.dataset.saved = configured ? "1" : "0";
  if (configured) {
    input.value = SAVED_SECRET_MASK;
    input.placeholder = hint ? `${tr("cfgKeySaved")} ${hint}` : tr("cfgKeySaved");
  } else {
    input.value = "";
    input.placeholder = tr("cfgApiKeyPlaceholder");
  }
}

function readSecretField(input) {
  if (!input) return "";
  const v = input.value.trim();
  if (!v) return "";
  if (input.dataset.saved === "1" && v === SAVED_SECRET_MASK) return "";
  return v;
}

function bindSecretFields() {
  SECRET_INPUT_IDS.forEach((id) => {
    const input = $(id);
    if (!input || input.dataset.secretBound) return;
    input.dataset.secretBound = "1";
    input.addEventListener("focus", () => {
      if (input.dataset.saved === "1" && input.value === SAVED_SECRET_MASK) {
        input.value = "";
        input.dataset.saved = "0";
        input.placeholder = tr("cfgApiKeyPlaceholder");
      }
    });
  });
}

function showTestResult(el, ok, msg) {
  if (!el) return;
  el.hidden = false;
  el.className = "test-result " + (ok ? "ok" : "err");
  el.textContent = msg;
}

function renderTaskList(tasks) {
  const el = $("cfgTaskList");
  if (!el) return;
  el.innerHTML = CFG_TASKS.map((t) => {
    const model = (tasks[t.id] && tasks[t.id].model) || "";
    return `<div class="task-row" data-task="${t.id}">`
      + `<div class="task-name">${escapeHtml(t.hint)}</div>`
      + `<input class="task-model" data-task="${t.id}" value="${escapeHtml(model)}" />`
      + `<button class="btn sm task-test-btn" type="button" data-task="${t.id}">${escapeHtml(tr("cfgTestTask"))}</button>`
      + `</div>`;
  }).join("");
}

function renderSttUseList(stt) {
  const el = $("sttUseList");
  if (!el) return;
  const active = stt.active || "";
  const backends = stt.backends || {};
  el.innerHTML = STT_BACKENDS.map((b) => {
    const ready = !!backends[b];
    const checked = active === b ? "checked" : "";
    const disabled = ready ? "" : "disabled";
    const cls = ready ? "use-row" : "use-row disabled";
    const pill = ready ? `<span class="cfg-pill ok">${tr("cfgReady")}</span>` : `<span class="cfg-pill">${tr("cfgNotReady")}</span>`;
    return `<label class="${cls}" data-backend="${b}">`
      + `<input type="radio" name="sttActive" value="${b}" ${checked} ${disabled ? "disabled" : ""} />`
      + `<div class="use-name">${STT_LABELS[b]}</div>${pill}</label>`;
  }).join("");
}

function updateStatusBars(r) {
  const textBar = $("textStatusBar");
  const textTxt = $("textStatusText");
  if (textBar && textTxt) {
    textBar.className = "cfg-status " + (r.text_ready ? "ok" : "warn");
    textBar.querySelector(".cfg-dot").className = "cfg-dot " + (r.text_ready ? "ok" : "warn");
    textTxt.textContent = r.text_ready ? tr("cfgTextReady") : tr("cfgTextNotReady");
  }
  const sttBar = $("sttStatusBar");
  const sttTxt = $("sttStatusText");
  if (sttBar && sttTxt) {
    sttBar.className = "cfg-status " + (r.stt_ready ? "ok" : "warn");
    sttBar.querySelector(".cfg-dot").className = "cfg-dot " + (r.stt_ready ? "ok" : "warn");
    const label = STT_LABELS[r.stt_active] || "";
    sttTxt.textContent = r.stt_ready ? tr("cfgSttReady", { name: label }) : tr("cfgSttNotReady");
  }
}

function switchSttEdit(backend) {
  $("sttEditTabs").querySelectorAll("button").forEach((b) => {
    b.classList.toggle("active", b.dataset.backend === backend);
  });
  document.querySelectorAll(".stt-form").forEach((f) => {
    f.hidden = f.dataset.backend !== backend;
  });
}

function switchCfgTab(tab) {
  $("cfgMainTabs").querySelectorAll("button").forEach((b) => {
    b.classList.toggle("active", b.dataset.tab === tab);
  });
  $("cfgPanelText").hidden = tab !== "text";
  $("cfgPanelStt").hidden = tab !== "stt";
  $("cfgPanelProxy").hidden = tab !== "proxy";
}

function fillSettings(cfg) {
  modelsConfig = cfg;
  const text = cfg.text || {};
  const stt = cfg.stt || {};
  $("cfgBaseUrl").value = text.base_url || "";
  fillSecretField($("cfgApiKey"), text.api_key_configured, text.api_key_hint);
  $("cfgTemperature").value = text.temperature ?? 0.3;
  $("cfgTemperatureVal").textContent = Number(text.temperature ?? 0.3).toFixed(2);
  $("cfgMaxTokens").value = text.max_tokens ?? 4096;
  renderTaskList(text.tasks || {});

  const w = stt.whisper || {};
  $("sttWhisperUrl").value = w.base_url || "";
  $("sttWhisperModel").value = w.model || "";
  $("sttWhisperTimeout").value = w.timeout_sec ?? 120;
  const c = stt.cloud || {};
  $("sttCloudUrl").value = c.base_url || "";
  fillSecretField($("sttCloudKey"), c.api_key_configured, c.api_key_hint);
  $("sttCloudModel").value = c.model || "";
  $("sttCloudTimeout").value = c.timeout_sec ?? 120;
  $("sttCloudLang").value = c.language_hint || "";
  const f = stt.funasr || {};
  fillSecretField($("sttFunKey"), f.api_key_configured, f.api_key_hint);
  $("sttFunModel").value = f.model || "";
  $("sttOssEndpoint").value = f.oss_endpoint || "";
  $("sttOssBucket").value = f.oss_bucket || "";
  $("sttOssAk").value = f.oss_access_key_id || "";
  fillSecretField($("sttOssSk"), f.oss_secret_configured, "");
  const funKeyNote = $("sttFunKeyNote");
  if (funKeyNote) funKeyNote.textContent = f.api_key_configured ? tr("cfgKeySaved") : tr("cfgApiKeyNote");
  const ossSkNote = $("sttOssSkNote");
  if (ossSkNote) ossSkNote.textContent = f.oss_secret_configured ? tr("cfgSecretSaved") : tr("cfgSecretNote");

  const dp = cfg.download_proxy || {};
  $("cfgProxyEnabled").checked = !!dp.enabled;
  $("cfgProxyUrl").value = dp.url || "";

  renderSttUseList(stt);
  const editBackend = stt.active && STT_BACKENDS.includes(stt.active) ? stt.active : "cloud";
  switchSttEdit(editBackend);
  updateStatusBars(cfg.readiness || {});
}

function collectSettingsBody() {
  const tasks = {};
  document.querySelectorAll(".task-model").forEach((inp) => {
    const id = inp.dataset.task;
    const model = inp.value.trim();
    if (model) tasks[id] = { model };
  });
  const activeEl = document.querySelector('input[name="sttActive"]:checked');
  const stt = {
    active: activeEl ? activeEl.value : "",
    whisper: {
      base_url: $("sttWhisperUrl").value.trim(),
      model: $("sttWhisperModel").value.trim(),
      timeout_sec: parseInt($("sttWhisperTimeout").value, 10) || 120,
    },
    cloud: {
      base_url: $("sttCloudUrl").value.trim(),
      api_key: readSecretField($("sttCloudKey")),
      model: $("sttCloudModel").value.trim(),
      timeout_sec: parseInt($("sttCloudTimeout").value, 10) || 120,
      language_hint: $("sttCloudLang").value.trim(),
    },
    funasr: {
      api_key: readSecretField($("sttFunKey")),
      model: $("sttFunModel").value.trim(),
      oss_endpoint: $("sttOssEndpoint").value.trim(),
      oss_bucket: $("sttOssBucket").value.trim(),
      oss_access_key_id: $("sttOssAk").value.trim(),
      oss_access_key_secret: readSecretField($("sttOssSk")),
    },
  };
  return {
    text: {
      base_url: $("cfgBaseUrl").value.trim(),
      api_key: readSecretField($("cfgApiKey")),
      temperature: parseFloat($("cfgTemperature").value),
      max_tokens: parseInt($("cfgMaxTokens").value, 10),
      tasks,
    },
    stt,
    download_proxy: {
      enabled: $("cfgProxyEnabled").checked,
      url: $("cfgProxyUrl").value.trim(),
    },
  };
}

async function openSettings() {
  try {
    const res = await fetch("/api/models-config");
    const cfg = await res.json();
    if (!res.ok) throw new Error(cfg.error || res.statusText);
    fillSettings(cfg);
    switchCfgTab("text");
    $("settingsModal").hidden = false;
  } catch (e) {
    toast(e.message, 3500);
  }
}

async function saveSettings() {
  const body = collectSettingsBody();
  if (!body.text.base_url) { toast(tr("baseUrlRequired")); return; }
  if (!body.text.api_key && !modelsConfig?.text?.api_key_configured) { toast(tr("apiKeyRequired")); return; }
  try {
    const res = await fetch("/api/models-config", {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    const data = await res.json();
    if (!res.ok || data.error) { toast(tr("saveFailed", { msg: data.error || res.statusText }), 3500); return; }
    modelsConfig = data;
    state.llm = !!(data.readiness && data.readiness.translate_ready);
    fillSettings(data);
    $("settingsModal").hidden = true;
    toast(tr("saveOk"));
  } catch (e) {
    toast(tr("saveFailed", { msg: e.message }), 3500);
  }
}

async function resetSettings() {
  try {
    const res = await fetch("/api/models-config/reset", { method: "POST" });
    const data = await res.json();
    if (!res.ok || data.error) throw new Error(data.error || res.statusText);
    modelsConfig = data;
    state.llm = false;
    fillSettings(data);
    toast(tr("resetOk"));
  } catch (e) {
    toast(e.message, 3500);
  }
}

async function testTextConn() {
  try {
    const saveRes = await fetch("/api/models-config", {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(collectSettingsBody()),
    });
    const saved = await saveRes.json();
    if (!saveRes.ok) throw new Error(saved.error || saveRes.statusText);
    modelsConfig = saved;
    const res = await fetch("/api/models-config/test/text", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ task: "translate" }),
    });
    const data = await res.json();
    if (!res.ok) throw new Error(data.error || res.statusText);
    showTestResult($("textConnResult"), true, tr("cfgTestOk"));
    fillSettings(saved);
  } catch (e) {
    showTestResult($("textConnResult"), false, tr("cfgTestFail", { msg: e.message }));
  }
}

async function testTextTask(task) {
  try {
    const saveRes = await fetch("/api/models-config", {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(collectSettingsBody()),
    });
    const saved = await saveRes.json();
    if (!saveRes.ok) throw new Error(saved.error || saveRes.statusText);
    modelsConfig = saved;
    const res = await fetch("/api/models-config/test/text", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ task }),
    });
    const data = await res.json();
    if (!res.ok) throw new Error(data.error || res.statusText);
    toast(tr("cfgTestOk"));
    fillSettings(saved);
  } catch (e) {
    toast(tr("cfgTestFail", { msg: e.message }), 4000);
  }
}

async function testProxy() {
  const body = collectSettingsBody();
  try {
    const saveRes = await fetch("/api/models-config", {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    const saved = await saveRes.json();
    if (saveRes.ok) {
      modelsConfig = saved;
    }
    const res = await fetch("/api/models-config/test/proxy", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ download_proxy: body.download_proxy }),
    });
    const data = await res.json();
    if (!res.ok) throw new Error(data.error || res.statusText);
    if (saveRes.ok) fillSettings(saved);
    showTestResult(
      $("cfgProxyResult"),
      true,
      tr("cfgProxyTestOk", { code: data.status_code, ms: data.elapsed_ms }),
    );
  } catch (e) {
    showTestResult($("cfgProxyResult"), false, tr("cfgTestFail", { msg: e.message }));
  }
}

async function testSttBackend(backend) {
  const body = collectSettingsBody();
  const form = document.querySelector(`.stt-form[data-backend="${backend}"]`);
  const resultEl = form && form.querySelector(".stt-test-result");
  try {
    const saveRes = await fetch("/api/models-config", {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    const saved = await saveRes.json();
    if (saveRes.ok) {
      modelsConfig = saved;
    }
    const res = await fetch("/api/models-config/test/stt", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ backend, stt: body.stt }),
    });
    const data = await res.json();
    if (!res.ok) throw new Error(data.error || res.statusText);
    if (saveRes.ok) {
      fillSettings(saved);
    } else {
      const cfgRes = await fetch("/api/models-config");
      const cfg = await cfgRes.json();
      if (cfgRes.ok) fillSettings(cfg);
    }
    showTestResult(resultEl, true, tr("cfgTestOk"));
  } catch (e) {
    showTestResult(resultEl, false, tr("cfgTestFail", { msg: e.message }));
  }
}

$("settingsBtn").onclick = openSettings;
$("settingsClose").onclick = () => ($("settingsModal").hidden = true);
$("settingsCancel").onclick = () => ($("settingsModal").hidden = true);
$("settingsSave").onclick = saveSettings;
$("settingsReset").onclick = resetSettings;
$("testTextConn").onclick = testTextConn;
$("testProxyBtn").onclick = testProxy;
$("cfgTemperature").oninput = (e) => ($("cfgTemperatureVal").textContent = parseFloat(e.target.value).toFixed(2));
$("cfgMainTabs").addEventListener("click", (e) => {
  const btn = e.target.closest("button[data-tab]");
  if (btn) switchCfgTab(btn.dataset.tab);
});
$("sttEditTabs").addEventListener("click", (e) => {
  const btn = e.target.closest("button[data-backend]");
  if (btn) switchSttEdit(btn.dataset.backend);
});
$("cfgTaskList").addEventListener("click", (e) => {
  const btn = e.target.closest(".task-test-btn");
  if (btn) testTextTask(btn.dataset.task);
});
document.querySelectorAll(".stt-test-btn").forEach((btn) => {
  btn.addEventListener("click", () => testSttBackend(btn.dataset.backend));
});
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
    if (isNaN(t)) return;
    // 文本来源：把"时间"当作段落序号，滚动定位阅读器对应段落
    if (state.contentKind === "text") {
      if (window.Reader) Reader.scrollToIndex(t);
      return;
    }
    if (video) {
      video.currentTime = t;
      video.play().catch(() => {});
    }
  },
  get contentKind() { return state.contentKind; },
  get jobId() { return state.jobId; },
  get segments() { return state.segs; },
  toast,
};

async function syncLocalPositionsToBackend() {
  const map = readLocalPositions();
  const entries = Object.entries(map).filter(([, pos]) => Number(pos) >= POSITION_MIN_SEC);
  for (const [source, pos] of entries) {
    try {
      await fetch("/api/history/position", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ source, position_sec: Number(pos) }),
      });
    } catch {}
  }
  if (entries.length) await fetchHistory();
}

(function init() {
  bindSecretFields();
  const savedTheme = localStorage.getItem("vt_theme");
  if (savedTheme) document.body.dataset.theme = savedTheme;
  $("langBtn").textContent = I18n.getLocale() === "zh" ? "EN" : "中";
  migrateHistoryFromLocalStorage()
    .then(() => syncLocalPositionsToBackend())
    .then(() => fetchHistory())
    .catch(() => {});
  fetch("/api/health").then((r) => r.json()).then((h) => {
    state.llm = !!h.llm;
    if (!h.stt) toast(tr("noStt"), 5000);
    else if (!h.llm) toast(tr("noLlm"), 5000);
  }).catch(() => {});
})();
