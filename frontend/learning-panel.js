/* 视频知识学习面板（Phase 1 + Phase 2） */
(function () {
  const $ = (id) => document.getElementById(id);
  const lt = (k, p) => (window.I18n ? I18n.t(k, p) : k);

  const panel = {
    jobId: "",
    segs: [],
    goal: "",
    viewMode: "all",
    outline: null,
    highlights: null,
    questions: [],
    quizGrades: [],
    quizChats: {},
    quizConfirmed: {},
    feynmanGrade: null,
    feynmanChat: [],
    report: "",
    outlineRequested: false,
    voiceAvailable: false,
    subtitleView: "both",
    mediaRecorder: null,
    recordChunks: [],
    recordTarget: null,
  };

  function fmt(sec) {
    sec = Math.max(0, Math.floor(sec || 0));
    return String(Math.floor(sec / 60)).padStart(2, "0") + ":" + String(sec % 60).padStart(2, "0");
  }

  function escapeHtml(s) {
    return (s || "").replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
  }

  function toast(msg, ms) {
    if (window.LearnBridge && window.LearnBridge.toast) window.LearnBridge.toast(msg, ms);
  }

  function verdictClass(v) {
    if (v === "correct") return "verdict-correct";
    if (v === "wrong" || v === "empty") return "verdict-wrong";
    return "verdict-partial";
  }

  function relClass(rel) {
    if (rel >= 2) return "rel2";
    if (rel >= 1) return "rel1";
    return "";
  }

  function relBadge(rel) {
    if (rel >= 2) return `<span class="rel-badge rel-h">${lt("learnRelHigh")}</span>`;
    if (rel >= 1) return `<span class="rel-badge rel-m">${lt("learnRelMid")}</span>`;
    return `<span class="rel-badge rel-l">${lt("learnRelLow")}</span>`;
  }

  function getSubtitleView() {
    return document.body.dataset.view || panel.subtitleView || "both";
  }

  /** 与顶栏 双语/原文/中文 同步 */
  function segmentDisplayHtml(seg) {
    const view = getSubtitleView();
    const src = seg.source || "";
    const tgt = seg.target || "";
    if (view === "source") {
      return applyHighlights(src, highlightMap()[seg.index]?.highlights);
    }
    if (view === "target") {
      if (tgt) return escapeHtml(tgt);
      if (seg.no_translate) return escapeHtml(src);
      return `<span class="seg-pending">${lt("learnTranslating")}</span>`;
    }
    // both
    let html = applyHighlights(src, highlightMap()[seg.index]?.highlights);
    if (tgt) {
      html += `<span class="seg-target">${escapeHtml(tgt)}</span>`;
    } else if (src && !seg.no_translate) {
      html += `<span class="seg-pending">${lt("learnTranslating")}</span>`;
    }
    return html;
  }

  function setHighlightProgress(done, total, msg) {
    const wrap = $("learnHighlightProg");
    const bar = $("learnHighlightBar");
    const txt = $("learnHighlightProgText");
    if (!wrap) return;
    if (total <= 0) {
      wrap.hidden = true;
      return;
    }
    wrap.hidden = false;
    const pct = Math.round((done / total) * 100);
    if (bar) bar.style.width = pct + "%";
    if (txt) txt.textContent = msg || lt("learnHlProgress", { done, total });
  }

  function applyHighlights(text, highlights) {
    if (!highlights || !highlights.length) return escapeHtml(text);
    let out = escapeHtml(text);
    const sorted = [...highlights].filter(Boolean).sort((a, b) => b.length - a.length);
    for (const kw of sorted) {
      const safe = escapeHtml(kw);
      if (!safe) continue;
      out = out.replace(new RegExp(safe.replace(/[.*+?^${}()|[\]\\]/g, "\\$&"), "g"), `<mark>${safe}</mark>`);
    }
    return out;
  }

  function highlightMap() {
    const m = {};
    const items = (panel.highlights && panel.highlights.segments) || [];
    for (const it of items) m[it.index] = it;
    return m;
  }

  // 三态视图：
  //  - all : 显示完整转录稿，隐藏骨架与地图
  //  - map : 隐藏转录稿，显示骨架+信息地图，并显示「生成骨架」按钮
  //  - rel : 仅显示关联转录稿，隐藏骨架与地图
  function applyViewMode() {
    const mode = panel.viewMode;
    const isMap = mode === "map";
    const relOnly = mode === "rel";
    // 转录稿区块（含小标题）：信息地图态整体隐藏
    document.querySelectorAll(".transcript-only").forEach((el) => {
      el.classList.toggle("hidden-seg", isMap);
    });
    // 仅关联：隐藏无关段
    document.querySelectorAll("#learnTranscript .learn-seg").forEach((el) => {
      const rel = parseInt(el.dataset.rel || "0", 10);
      el.classList.toggle("hidden-seg", relOnly && rel === 0);
    });
    // 骨架 + 信息地图：仅信息地图态显示
    document.querySelectorAll(".outline-only").forEach((el) => {
      el.classList.toggle("hidden-outline", !isMap);
    });
    // 「生成骨架」按钮仅信息地图态显示
    const btn = $("learnOutlineBtn");
    if (btn) btn.style.display = isMap ? "" : "none";
  }

  function renderTranscript() {
    const box = $("learnTranscript");
    if (!box || !panel.segs.length) { if (box) box.innerHTML = ""; return; }
    const hm = highlightMap();
    box.innerHTML = panel.segs
      .filter((s) => s && s.source)
      .map((s) => {
        const hi = hm[s.index] || {};
        const rel = hi.relevance != null ? hi.relevance : 0;
        const text = segmentDisplayHtml(s);
        return `<div class="learn-seg ${relClass(rel)}" data-rel="${rel}" data-start="${s.start}">`
          + `<span class="t">${fmt(s.start)}</span><span>${text}</span></div>`;
      })
      .join("");
    box.querySelectorAll(".learn-seg").forEach((el) => {
      el.addEventListener("click", () => {
        if (window.LearnBridge) window.LearnBridge.seek(parseFloat(el.dataset.start));
      });
    });
    applyViewMode();
  }

  function renderOutline() {
    const sk = $("learnSkeleton");
    const mp = $("learnMap");
    if (!panel.outline) {
      if (sk) sk.innerHTML = "";
      if (mp) mp.innerHTML = "";
      return;
    }
    const skeleton = panel.outline.skeleton || [];
    if (sk) sk.innerHTML = skeleton.map((x) => `<li>${escapeHtml(x)}</li>`).join("");
    const map = panel.outline.map || [];
    if (mp) {
      mp.innerHTML = map.map((row) => {
        const start = row.start_sec != null ? row.start_sec : row.start || 0;
        const rel = row.relevance != null ? row.relevance : 0;
        return `<div class="learn-map-row" data-start="${start}">`
          + `<span class="time">${fmt(start)}</span>`
          + `<span>${escapeHtml(row.summary || "")}</span>`
          + relBadge(rel) + `</div>`;
      }).join("");
      mp.querySelectorAll(".learn-map-row").forEach((el) => {
        el.addEventListener("click", () => {
          if (window.LearnBridge) window.LearnBridge.seek(parseFloat(el.dataset.start));
        });
      });
    }
  }

  function gradeHtml(g) {
    if (!g) return "";
    return `<div class="grade-box">`
      + `<div class="${verdictClass(g.verdict)}"><strong>${escapeHtml(g.summary || "")}</strong> (${g.verdict || ""})</div>`
      + (g.gaps && g.gaps.length ? `<div>${lt("learnGradeGaps")}${g.gaps.map(escapeHtml).join("；")}</div>` : "")
      + (g.suggestion ? `<div>${lt("learnGradeSuggest")}${escapeHtml(g.suggestion)}</div>` : "")
      + `</div>`;
  }

  function renderQuiz() {
    const cards = $("quizCards");
    const empty = $("quizEmpty");
    if (!cards) return;
    if (!panel.questions.length) {
      cards.innerHTML = "";
      if (empty) empty.style.display = "";
      return;
    }
    if (empty) empty.style.display = "none";
    const gmap = {};
    panel.quizGrades.forEach((g) => { gmap[g.id] = g; });

    cards.innerHTML = panel.questions.map((q, i) => {
      const id = q.id != null ? q.id : i + 1;
      const confirmed = panel.quizConfirmed[id] || "";
      const g = gmap[id];
      return `<div class="q-card" data-qid="${id}">`
        + `<div class="q-title">${id}. ${escapeHtml(q.text)}</div>`
        + `<textarea data-qid="${id}" placeholder="${escapeHtml(lt("learnQuizAnswerPh"))}">${escapeHtml(confirmed)}</textarea>`
        + `<div class="q-actions">`
        + `<button type="button" class="mic-btn" data-qid="${id}">${lt("learnMicAnswer")}</button>`
        + `<button type="button" class="btn btn-sm" data-confirm="${id}">${lt("learnConfirmAnswer")}</button>`
        + `</div>`
        + `<div class="q-grade" data-qgrade="${id}">${gradeHtml(g)}</div>`
        + `<div class="q-chat-row">`
        + `<input type="text" data-qchat="${id}" placeholder="${escapeHtml(lt("learnQuizChatPh"))}" />`
        + `</div>`
        + `<div class="q-chat-log" data-qlog="${id}"></div>`
        + `</div>`;
    }).join("");

    cards.querySelectorAll(".mic-btn").forEach((btn) => {
      btn.addEventListener("click", () => toggleRecord(btn));
    });
    cards.querySelectorAll("[data-confirm]").forEach((btn) => {
      btn.addEventListener("click", () => confirmQuizAnswer(parseInt(btn.dataset.confirm, 10)));
    });
    cards.querySelectorAll("[data-qchat]").forEach((inp) => {
      inp.addEventListener("keydown", (e) => {
        if (e.key === "Enter") {
          e.preventDefault();
          sendQuizChat(parseInt(inp.dataset.qchat, 10), inp.value.trim(), inp);
        }
      });
    });
    renderQuizChatLogs();
  }

  function renderQuizChatLogs() {
    for (const [qid, hist] of Object.entries(panel.quizChats)) {
      const log = document.querySelector(`[data-qlog="${qid}"]`);
      if (!log) continue;
      log.innerHTML = (hist || []).map((h) =>
        `<div class="msg-${h.role === "user" ? "user" : "ai"}">${h.role === "user" ? lt("learnChatMe") : lt("learnChatAi")}：${escapeHtml(h.content)}</div>`
      ).join("");
    }
  }

  function renderFeynman() {
    const ol = $("feynmanOutline");
    if (ol) ol.textContent = lt("learnFeynmanOutline");
    const box = $("feynmanGradeBox");
    const chatBox = $("feynmanChatBox");
    if (panel.feynmanGrade && box) {
      box.hidden = false;
      const g = panel.feynmanGrade;
      box.innerHTML = `<div><strong>${escapeHtml(g.overall || "")}</strong></div>`
        + (g.socratic_question ? `<div>${lt("learnFeynmanSocratic")}${escapeHtml(g.socratic_question)}</div>` : "")
        + ((g.missing || []).length ? `<div>${lt("learnFeynmanMissing")}${g.missing.map(escapeHtml).join("；")}</div>` : "");
      if (chatBox) chatBox.hidden = false;
    }
    const log = $("feynmanChatLog");
    if (log) {
      log.innerHTML = panel.feynmanChat.map((h) =>
        `<div class="msg-${h.role === "user" ? "user" : "ai"}">${h.role === "user" ? lt("learnChatMe") : lt("learnChatAi")}：${escapeHtml(h.content)}</div>`
      ).join("");
    }
  }

  function applyLearnI18n() {
    if (window.I18n) I18n.applyPageI18n();
    const rp = $("reportPreview");
    if (rp && !panel.report) rp.textContent = lt("learnReportPlaceholder");
    renderTranscript();
    renderOutline();
    renderQuiz();
    renderFeynman();
  }

  function setPanelVisible(show) {
    const el = $("learnPanel");
    if (el) el.hidden = !show;
  }

  async function loadSession() {
    if (!panel.jobId) return;
    try {
      const res = await fetch(`/api/learn/${panel.jobId}/session`);
      const data = await res.json();
      if (data.goal) { panel.goal = data.goal; $("learnGoal").value = data.goal; }
      if (data.outline) { panel.outline = data.outline; panel.outlineRequested = true; }
      if (data.highlights) {
        panel.highlights = data.highlights;
        if (window.Reader) window.Reader.applyHighlights(panel.highlights);
      }
      if (data.questions) panel.questions = data.questions;
      if (data.quiz_grades) panel.quizGrades = data.quiz_grades;
      if (data.quiz_chats) panel.quizChats = data.quiz_chats;
      if (data.quiz_confirmed) {
        panel.quizConfirmed = {};
        data.quiz_confirmed.forEach((c) => { panel.quizConfirmed[c.id] = c.answer; });
      }
      if (data.feynman_draft) $("feynmanDraft").value = data.feynman_draft;
      if (data.feynman_confirmed) $("feynmanDraft").value = data.feynman_confirmed;
      if (data.feynman_grade) panel.feynmanGrade = data.feynman_grade;
      if (data.feynman_chat) panel.feynmanChat = data.feynman_chat;
      if (data.report) { panel.report = data.report; $("reportPreview").textContent = data.report; }
      if (data.persisted && Object.keys(data.persisted).length) showPersistResult(data.persisted);
      renderOutline();
      renderTranscript();
      renderQuiz();
      renderFeynman();
    } catch (_) { /* ignore */ }
  }

  async function genOutline() {
    if (!panel.jobId) return;
    $("learnOutlineBtn").disabled = true;
    toast(lt("learnToastOutlineGen"));
    try {
      const res = await fetch(`/api/learn/${panel.jobId}/outline`, { method: "POST" });
      const data = await res.json();
      if (!res.ok) throw new Error(data.error || res.statusText);
      panel.outline = data.outline;
      panel.outlineRequested = true;
      renderOutline();
      toast(lt("learnToastOutlineDone"));
    } catch (e) { toast(e.message, 4000); }
    finally { $("learnOutlineBtn").disabled = false; }
  }

  async function refreshVoiceStatus() {
    try {
      const res = await fetch("/api/learn/health");
      const h = await res.json();
      panel.voiceAvailable = !!(h.voice && h.voice.available);
      if (!h.llm) toast(lt("learnToastNoLlm"), 5000);
    } catch (_) {
      panel.voiceAvailable = false;
    }
  }

  function ensureVoiceOrToast() {
    if (panel.voiceAvailable) return true;
    toast(lt("learnVoiceUnavailable"), 3500);
    return false;
  }

  async function applyHighlight() {
    const goal = ($("learnGoal").value || "").trim();
    if (!goal) { toast(lt("learnToastNeedGoal")); return; }
    if (!panel.jobId) return;
    panel.goal = goal;
    $("learnHighlightBtn").disabled = true;
    setHighlightProgress(0, panel.segs.length || 1, lt("learnHlPreparing"));

    try {
      const res = await fetch(`/api/learn/${panel.jobId}/highlight/stream`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ goal }),
      });
      if (!res.ok) {
        const err = await res.json().catch(() => ({}));
        throw new Error(err.error || res.statusText);
      }
      const reader = res.body.getReader();
      const decoder = new TextDecoder();
      let buf = "";
      while (true) {
        const { done, value } = await reader.read();
        if (done) break;
        buf += decoder.decode(value, { stream: true });
        const parts = buf.split("\n\n");
        buf = parts.pop() || "";
        for (const block of parts) {
          const line = block.trim();
          if (!line.startsWith("data:")) continue;
          const ev = JSON.parse(line.slice(5).trim());
          if (ev.type === "progress") {
            setHighlightProgress(ev.done || 0, ev.total || 1, ev.message);
          } else if (ev.type === "batch" && ev.highlights) {
            panel.highlights = ev.highlights;
            renderTranscript();
            if (window.Reader) window.Reader.applyHighlights(panel.highlights);
            setHighlightProgress(ev.done || 0, ev.total || 1, lt("learnHlProgress", { done: ev.done, total: ev.total }));
          } else if (ev.type === "done") {
            panel.highlights = ev.highlights;
            renderTranscript();
            if (window.Reader) window.Reader.applyHighlights(panel.highlights);
            setHighlightProgress(ev.total, ev.total, lt("learnHlDone"));
            toast(lt("learnToastHlDone"));
          } else if (ev.type === "error") {
            throw new Error(ev.message || lt("learnToastHlFail"));
          }
        }
      }
    } catch (e) {
      toast(e.message, 4000);
    } finally {
      $("learnHighlightBtn").disabled = false;
      setTimeout(() => setHighlightProgress(0, 0), 1200);
    }
  }

  async function genQuestions() {
    const goal = ($("learnGoal").value || "").trim();
    if (!goal) { toast(lt("learnToastNeedGoalQuiz")); return; }
    const count = parseInt($("quizCount").value, 10) || 5;
    $("quizGenBtn").disabled = true;
    try {
      const res = await fetch(`/api/learn/${panel.jobId}/questions`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ goal, count }),
      });
      const data = await res.json();
      if (!res.ok) throw new Error(data.error || res.statusText);
      panel.questions = data.questions || [];
      panel.quizGrades = [];
      panel.quizChats = {};
      renderQuiz();
      toast(lt("learnToastQuestionsDone"));
    } catch (e) { toast(e.message, 4000); }
    finally { $("quizGenBtn").disabled = false; }
  }

  function collectQuizAnswers() {
    return panel.questions.map((q, i) => {
      const id = q.id != null ? q.id : i + 1;
      const ta = document.querySelector(`textarea[data-qid="${id}"]`);
      return { id, answer: ta ? ta.value.trim() : "" };
    });
  }

  async function submitGrade() {
    if (!panel.jobId || !panel.questions.length) return;
    $("quizGradeBtn").disabled = true;
    toast(lt("learnToastGrading"));
    try {
      const res = await fetch(`/api/learn/${panel.jobId}/grade`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ answers: collectQuizAnswers() }),
      });
      const data = await res.json();
      if (!res.ok) throw new Error(data.error || res.statusText);
      panel.quizGrades = data.grades || [];
      renderQuiz();
      toast(data.overall || lt("learnToastGradeDone"));
    } catch (e) { toast(e.message, 4000); }
    finally { $("quizGradeBtn").disabled = false; }
  }

  async function sendQuizChat(qid, message, inputEl) {
    if (!message || !panel.jobId) return;
    try {
      const res = await fetch(`/api/learn/${panel.jobId}/grade/chat`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ question_id: qid, message }),
      });
      const data = await res.json();
      if (!res.ok) throw new Error(data.error || res.statusText);
      panel.quizChats[String(qid)] = data.history || [];
      if (inputEl) inputEl.value = "";
      renderQuizChatLogs();
    } catch (e) { toast(e.message, 4000); }
  }

  async function confirmQuizAnswer(qid) {
    const ta = document.querySelector(`textarea[data-qid="${qid}"]`);
    const answer = ta ? ta.value.trim() : "";
    if (!answer) { toast(lt("learnToastNeedAnswer")); return; }
    try {
      const res = await fetch(`/api/learn/${panel.jobId}/grade/confirm`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ confirmations: [{ id: qid, answer }] }),
      });
      const data = await res.json();
      if (!res.ok) throw new Error(data.error || res.statusText);
      panel.quizConfirmed[qid] = answer;
      toast(lt("learnToastAnswerConfirmed", { qid }));
    } catch (e) { toast(e.message, 4000); }
  }

  async function submitFeynmanGrade() {
    const draft = ($("feynmanDraft").value || "").trim();
    if (draft.length < 10) { toast(lt("learnToastFeynmanShort")); return; }
    $("feynmanGradeBtn").disabled = true;
    try {
      const res = await fetch(`/api/learn/${panel.jobId}/feynman/grade`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ draft }),
      });
      const data = await res.json();
      if (!res.ok) throw new Error(data.error || res.statusText);
      panel.feynmanGrade = data;
      renderFeynman();
      toast(lt("learnToastFeynmanGraded"));
    } catch (e) { toast(e.message, 4000); }
    finally { $("feynmanGradeBtn").disabled = false; }
  }

  async function sendFeynmanChat() {
    const msg = ($("feynmanChatInput").value || "").trim();
    if (!msg) return;
    try {
      const res = await fetch(`/api/learn/${panel.jobId}/feynman/chat`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ message: msg }),
      });
      const data = await res.json();
      if (!res.ok) throw new Error(data.error || res.statusText);
      panel.feynmanChat = data.history || [];
      $("feynmanChatInput").value = "";
      renderFeynman();
    } catch (e) { toast(e.message, 4000); }
  }

  async function confirmFeynman() {
    const draft = ($("feynmanDraft").value || "").trim();
    if (!draft) return;
    try {
      const res = await fetch(`/api/learn/${panel.jobId}/feynman/confirm`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ draft }),
      });
      const data = await res.json();
      if (!res.ok) throw new Error(data.error || res.statusText);
      toast(lt("learnToastFeynmanConfirmed"));
    } catch (e) { toast(e.message, 4000); }
  }

  async function genReport() {
    $("reportGenBtn").disabled = true;
    toast(lt("learnToastReportGen"));
    try {
      const res = await fetch(`/api/learn/${panel.jobId}/report`, { method: "POST" });
      const data = await res.json();
      if (!res.ok) throw new Error(data.error || res.statusText);
      panel.report = data.report;
      $("reportPreview").textContent = data.report;
      toast(lt("learnToastReportDone"));
    } catch (e) { toast(e.message, 4000); }
    finally { $("reportGenBtn").disabled = false; }
  }

  function showPersistResult(paths) {
    const el = $("persistResult");
    if (!el) return;
    el.hidden = false;
    el.innerHTML = lt("learnPersistDone") + "<br>" + Object.entries(paths)
      .map(([k, v]) => `${k}: <code>${escapeHtml(v)}</code>`).join("<br>");
  }

  async function persistToKb() {
    $("reportPersistBtn").disabled = true;
    toast(lt("learnToastPersisting"));
    try {
      const res = await fetch(`/api/learn/${panel.jobId}/persist`, { method: "POST" });
      const data = await res.json();
      if (!res.ok) throw new Error(data.error || res.statusText);
      showPersistResult(data.paths);
      toast(lt("learnToastPersistDone"));
    } catch (e) { toast(e.message, 4000); }
    finally { $("reportPersistBtn").disabled = false; }
  }

  async function uploadAudio(blob, textarea) {
    const fd = new FormData();
    fd.append("file", blob, "answer.webm");
    const res = await fetch("/api/learn/transcribe-answer", { method: "POST", body: fd });
    const data = await res.json();
    if (!res.ok) throw new Error(data.error || lt("learnVoiceUnavailable"));
    if (textarea) {
      textarea.value = (textarea.value ? textarea.value + "\n" : "") + (data.text || "");
    }
    toast(data.backend === "whisper" ? lt("learnVoiceLocal") : lt("learnVoiceDone"));
  }

  async function toggleRecord(btn) {
    if (!ensureVoiceOrToast()) return;
    if (panel.mediaRecorder && panel.mediaRecorder.state === "recording") {
      panel.mediaRecorder.stop();
      btn.classList.remove("recording");
      btn.textContent = btn.dataset.origText || lt("learnMicAnswer");
      return;
    }
    if (!navigator.mediaDevices) { toast(lt("learnMicUnavailable")); return; }
    try {
      const stream = await navigator.mediaDevices.getUserMedia({ audio: true });
      panel.recordChunks = [];
      panel.recordTarget = btn.closest(".q-card")?.querySelector("textarea")
        || $("feynmanDraft");
      btn.dataset.origText = btn.textContent;
      const rec = new MediaRecorder(stream);
      panel.mediaRecorder = rec;
      rec.ondataavailable = (e) => { if (e.data.size) panel.recordChunks.push(e.data); };
      rec.onstop = async () => {
        stream.getTracks().forEach((t) => t.stop());
        try {
          await uploadAudio(new Blob(panel.recordChunks, { type: "audio/webm" }), panel.recordTarget);
        } catch (e) { toast(e.message, 4000); }
      };
      rec.start();
      btn.classList.add("recording");
      btn.textContent = lt("learnRecording");
    } catch (e) { toast(lt("learnMicDenied", { msg: e.message }), 4000); }
  }

  function bindUi() {
    $("learnHighlightBtn").addEventListener("click", applyHighlight);
    $("learnGoal").addEventListener("keydown", (e) => {
      if (e.key === "Enter") { e.preventDefault(); applyHighlight(); }
    });
    $("learnOutlineBtn").addEventListener("click", genOutline);
    $("quizGenBtn").addEventListener("click", genQuestions);
    $("quizGradeBtn").addEventListener("click", submitGrade);
    $("feynmanGradeBtn").addEventListener("click", submitFeynmanGrade);
    $("feynmanMic").addEventListener("click", function () { toggleRecord(this); });
    $("feynmanChatSend").addEventListener("click", sendFeynmanChat);
    $("feynmanConfirmBtn").addEventListener("click", confirmFeynman);
    $("reportGenBtn").addEventListener("click", genReport);
    $("reportPersistBtn").addEventListener("click", persistToKb);

    $("learnViewToggle").addEventListener("click", (e) => {
      const btn = e.target.closest("button[data-mode]");
      if (!btn) return;
      panel.viewMode = btn.dataset.mode;
      [...$("learnViewToggle").children].forEach((b) => b.classList.toggle("active", b === btn));
      applyViewMode();
    });

    $("learnTabs").addEventListener("click", (e) => {
      const btn = e.target.closest("button[data-stage]");
      if (!btn || btn.disabled) return;
      [...$("learnTabs").children].forEach((b) => b.classList.toggle("active", b === btn));
      document.querySelectorAll(".learn-stage").forEach((s) => s.classList.remove("active"));
      $("stage-" + btn.dataset.stage).classList.add("active");
    });
  }

  window.LearningPanel = {
    applyI18n: applyLearnI18n,

    onViewChange(view) {
      panel.subtitleView = view || "both";
      renderTranscript();
    },

    onJobStart(jobId) {
      panel.jobId = jobId;
      panel.segs = [];
      panel.goal = "";
      panel.outline = null;
      panel.highlights = null;
      panel.questions = [];
      panel.quizGrades = [];
      panel.quizChats = {};
      panel.quizConfirmed = {};
      panel.feynmanGrade = null;
      panel.feynmanChat = [];
      panel.report = "";
      panel.outlineRequested = false;
      $("learnGoal").value = "";
      if ($("feynmanDraft")) $("feynmanDraft").value = "";
      renderTranscript();
      renderOutline();
      renderQuiz();
      renderFeynman();
      setPanelVisible(true);
      refreshVoiceStatus();
      loadSession();
    },
    onJobReset() {
      panel.jobId = "";
      panel.segs = [];
      setPanelVisible(false);
    },
    onSegmentsUpdate(segs) {
      panel.segs = segs.filter((s) => s && s.source);
      renderTranscript();
      if (panel.segs.length && !panel.outlineRequested && panel.jobId) {
        panel.outlineRequested = true;
        genOutline();
      }
    },
    setModelName(name) {
      const el = $("learnModel");
      if (el) el.textContent = name || "—";
    },
  };

  bindUi();
  applyLearnI18n();
  refreshVoiceStatus();
  fetch("/api/models-config").then((r) => r.json()).then((cfg) => {
    const m = cfg.text && cfg.text.tasks && cfg.text.tasks.outline && cfg.text.tasks.outline.model;
    if (m) window.LearningPanel.setModelName(m);
  }).catch(() => {});
})();
