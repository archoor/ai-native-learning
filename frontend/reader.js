/* ============================================================
   阅读器：网页 / txt / md / pdf 学习的内容区（方案 B）
   - 按段落渲染原文，保留标题/列表/引用/代码结构
   - 双语对照（与顶栏 双语/原文/中文 同步）
   - AI 高亮：按相关度给段落上色 + 句内关键词荧光标记
   - 信息地图点击 → 滚动定位段落
   ============================================================ */
(function () {
  const $ = (id) => document.getElementById(id);
  const rt = (k, p) => (window.I18n ? I18n.t(k, p) : k);

  const reader = {
    segs: [],
    highlightMap: {}, // index -> {relevance, highlights:[...]}
    translating: false, // 是否处于翻译进行中（决定无译文段是否显示"译文生成中"）
  };

  function escapeHtml(s) {
    return (s || "").replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
  }

  function tagFor(kind) {
    if (kind === "h1") return "h1";
    if (kind === "h2") return "h2";
    if (kind === "h3") return "h3";
    if (kind === "quote") return "blockquote";
    if (kind === "li") return "li";
    if (kind === "code") return "pre";
    return "p";
  }

  function relClass(rel) {
    if (rel >= 2) return "rel-2";
    if (rel >= 1) return "rel-1";
    return "";
  }

  function markKeywords(text, keywords) {
    let out = escapeHtml(text);
    if (!keywords || !keywords.length) return out;
    const sorted = [...keywords].filter(Boolean).sort((a, b) => b.length - a.length);
    for (const kw of sorted) {
      const safe = escapeHtml(kw);
      if (!safe) continue;
      out = out.replace(new RegExp(safe.replace(/[.*+?^${}()|[\]\\]/g, "\\$&"), "g"), `<mark>${safe}</mark>`);
    }
    return out;
  }

  function show() {
    const w = $("readerWrap");
    if (w) w.hidden = false;
  }
  function hide() {
    const w = $("readerWrap");
    if (w) w.hidden = true;
    const art = $("readerArticle");
    if (art) art.innerHTML = "";
    reader.segs = [];
    reader.highlightMap = {};
  }

  function setLoading(text) {
    show();
    const box = $("readerLoading");
    const t = $("readerLoadingText");
    if (t) t.textContent = text || "";
    if (box) box.hidden = false;
  }
  function clearLoading() {
    const box = $("readerLoading");
    if (box) box.hidden = true;
  }

  function setHeader(title, segs) {
    const tt = $("readerTitle");
    if (tt) tt.textContent = title || "";
    const meta = $("readerMeta");
    if (meta) {
      const n = segs.filter((s) => s && s.source).length;
      const unit = I18n && I18n.getLocale() === "en" ? "blocks" : "段";
      meta.textContent = `${n} ${unit}`;
    }
  }

  function setProgress(done, total) {
    const wrap = $("readerProg");
    const bar = $("readerProgBar");
    const txt = $("readerProgText");
    if (!wrap) return;
    if (!total || done >= total) {
      wrap.hidden = true;
      return;
    }
    wrap.hidden = false;
    const pct = Math.round((done / total) * 100);
    if (bar) bar.style.width = pct + "%";
    if (txt) txt.textContent = rt("readerTranslating", { done, total });
  }

  /** 渲染整篇文章。每段一个块，含原文（可高亮）与译文。 */
  function render(segs, title) {
    reader.segs = (segs || []).filter((s) => s && s.source);
    const art = $("readerArticle");
    if (!art) return;
    clearLoading();
    setHeader(title, reader.segs);

    art.innerHTML = reader.segs.map((s) => {
      const tag = tagFor(s.kind);
      const hi = reader.highlightMap[s.index] || {};
      const rel = hi.relevance != null ? hi.relevance : 0;
      const srcHtml = markKeywords(s.source, hi.highlights);
      let tgt = "";
      if (s.target) {
        tgt = `<span class="r-target">${escapeHtml(s.target)}</span>`;
      } else if (reader.translating && !s.no_translate) {
        tgt = `<span class="r-pending">${rt("readerPending")}</span>`;
      }
      return `<div class="r-block ${relClass(rel)}" data-index="${s.index}" data-rel="${rel}">`
        + `<${tag} class="r-src">${srcHtml}</${tag}>`
        + tgt
        + `</div>`;
    }).join("");

    art.querySelectorAll(".r-block").forEach((el) => {
      el.addEventListener("click", () => {
        el.classList.toggle("expand");
      });
    });
  }

  function applyHighlights(highlights) {
    const items = (highlights && highlights.segments) || [];
    reader.highlightMap = {};
    for (const it of items) reader.highlightMap[it.index] = it;
    if (reader.segs.length) render(reader.segs, $("readerTitle") && $("readerTitle").textContent);
  }

  function scrollToIndex(idx) {
    const art = $("readerArticle");
    if (!art) return;
    const el = art.querySelector(`.r-block[data-index="${Math.round(idx)}"]`);
    if (!el) return;
    el.scrollIntoView({ behavior: "smooth", block: "center" });
    el.classList.add("flash");
    setTimeout(() => el.classList.remove("flash"), 1400);
  }

  function setTranslating(b) { reader.translating = !!b; }

  window.Reader = {
    show, hide, render, applyHighlights, scrollToIndex,
    setLoading, clearLoading, setProgress, setTranslating,
  };
})();
