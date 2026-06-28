/* ============================================================
   图片 OCR 视图：左侧展示原图 + 归一化 bbox 叠加层。
   - setSegments(segs)：按 segment.bbox 在原图上绘制可点击的定位框
   - locate(index)：高亮指定段落的框并滚动到可见（右侧「全文」点击触发）
   - 点击框 → 反向高亮右侧「全文」对应条目（LearningPanel.focusSegment）
   bbox 用百分比定位，随图片渲染尺寸自适应缩放，无需监听 resize。
   ============================================================ */
(function () {
  const $ = (id) => document.getElementById(id);
  const iv = { segs: [], url: "" };

  function show() {
    const w = $("imageWrap");
    if (w) w.hidden = false;
  }
  function hide() {
    const w = $("imageWrap");
    if (w) w.hidden = true;
    const img = $("ocrImage");
    if (img) img.removeAttribute("src");
    iv.segs = [];
    iv.url = "";
    clearBoxes();
    clearLoading();
    const lb = $("imgLightbox");
    if (lb) lb.hidden = true;
  }

  function setLoading(text, badge) {
    show();
    const box = $("imageLoading");
    const t = $("imageLoadingText");
    if (t) t.textContent = text || "";
    if (box) {
      // badge 模式：半透明顶部小条，原图在后面仍可见（OCR 进行中）
      box.classList.toggle("as-badge", !!badge);
      box.hidden = false;
    }
  }
  function clearLoading() {
    const box = $("imageLoading");
    if (box) box.hidden = true;
  }

  function setTitle(title) {
    const el = $("imageTitle");
    if (el) el.textContent = title || "";
  }

  function setMeta(n) {
    const el = $("imageMeta");
    if (!el) return;
    const isEn = window.I18n && I18n.getLocale() === "en";
    el.textContent = n ? `${n} ${isEn ? "blocks" : "段"}` : "";
  }

  function setImage(url) {
    if (!url) return;
    iv.url = url;
    const img = $("ocrImage");
    if (img && img.getAttribute("src") !== url) img.src = url;
    show();
  }

  function clearBoxes() {
    const st = $("imgStage");
    if (!st) return;
    st.querySelectorAll(".ocr-box").forEach((b) => b.remove());
  }

  function setSegments(segs) {
    iv.segs = (segs || []).filter((s) => s && Array.isArray(s.bbox));
    setMeta((segs || []).filter((s) => s && s.source).length);
    const st = $("imgStage");
    if (!st) return;
    clearBoxes();
    iv.segs.forEach((s) => {
      const b = s.bbox;
      if (!b || b.length < 4) return;
      const d = document.createElement("div");
      d.className = "ocr-box";
      d.dataset.index = s.index;
      d.style.left = (b[0] * 100) + "%";
      d.style.top = (b[1] * 100) + "%";
      d.style.width = (b[2] * 100) + "%";
      d.style.height = (b[3] * 100) + "%";
      d.title = (s.source || "").slice(0, 80);
      d.addEventListener("click", () => {
        locate(s.index);
        if (window.LearningPanel && window.LearningPanel.focusSegment) {
          window.LearningPanel.focusSegment(s.index);
        }
      });
      st.appendChild(d);
    });
  }

  function locate(index) {
    const st = $("imgStage");
    if (!st) return;
    const idx = Math.round(index);
    st.querySelectorAll(".ocr-box.active").forEach((b) => b.classList.remove("active"));
    const el = st.querySelector(`.ocr-box[data-index="${idx}"]`);
    if (!el) return;
    el.classList.add("active");
    el.scrollIntoView({ behavior: "smooth", block: "center", inline: "center" });
  }

  function bindLightbox() {
    const zoom = $("imgZoomBtn");
    const lb = $("imgLightbox");
    const lbImg = $("imgLbImg");
    const close = $("imgLbClose");
    if (zoom) zoom.addEventListener("click", () => {
      if (!iv.url) return;
      if (lbImg) lbImg.src = iv.url;
      if (lb) lb.hidden = false;
    });
    if (close) close.addEventListener("click", () => { if (lb) lb.hidden = true; });
    if (lb) lb.addEventListener("click", (e) => { if (e.target === lb) lb.hidden = true; });
    document.addEventListener("keydown", (e) => {
      if (e.key === "Escape" && lb && !lb.hidden) lb.hidden = true;
    });
  }

  bindLightbox();

  window.ImageViewer = {
    show, hide, setLoading, clearLoading,
    setImage, setTitle, setSegments, locate,
  };
})();
