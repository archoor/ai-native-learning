"""学习产物落盘 workspace/，遵守 CLAUDE.md 红线。"""

from __future__ import annotations

import re
from datetime import date
from pathlib import Path

from ..paths import project_root
from .session import LearningSession


def _workspace() -> Path:
    return project_root() / "workspace"


def safe_dir_name(title: str) -> str:
    t = (title or "未命名视频").strip()
    t = re.sub(r'[<>:"/\\|?*]', "", t)
    t = re.sub(r"\s+", " ", t).strip()
    return t[:80] or "未命名视频"


def _fmt_time(sec: float) -> str:
    sec = max(0, int(sec))
    return f"{sec // 60:02d}:{sec % 60:02d}"


def _rel_label(r: int) -> str:
    return {2: "🔴 高", 1: "🟡 中", 0: "⚪ 低"}.get(r, "⚪ 低")


def persist_session(sess: LearningSession, segments: list[dict]) -> dict[str, str]:
    """写入 workspace，返回相对路径映射。"""
    ws = _workspace()
    title = safe_dir_name(sess.title)
    today = date.today().isoformat()
    raw_name = f"{title}.md"
    learning_dir = ws / "0.5-learning" / title
    learning_dir.mkdir(parents=True, exist_ok=True)
    (ws / "1-raw" / "transcripts").mkdir(parents=True, exist_ok=True)
    (ws / "5-output").mkdir(parents=True, exist_ok=True)

    paths: dict[str, str] = {}
    transcript_link = f"[[{Path(raw_name).stem}]]"
    learning_link = f"[[{title}/01-目标与信息地图]]"

    # ── 1-raw 转录稿 ──
    raw_path = ws / "1-raw" / "transcripts" / raw_name
    raw_lines = [
        "---",
        "type: transcript",
        f"source_file: ai_native_learning job {sess.job_id}",
        f"processed: {today}",
        "---",
        "",
        f"# {title}",
        "",
    ]
    for seg in segments:
        if seg and seg.get("source"):
            raw_lines.append(f"**{_fmt_time(seg.get('start', 0))}** {seg['source']}")
            raw_lines.append("")
    raw_path.write_text("\n".join(raw_lines), encoding="utf-8")
    paths["transcript"] = str(raw_path.relative_to(project_root()))

    # ── 0.5-learning 01 目标与信息地图 ──
    p01 = learning_dir / "01-目标与信息地图.md"
    sk = (sess.outline or {}).get("skeleton") or []
    mp = (sess.outline or {}).get("map") or []
    lines01 = [
        "---",
        "type: learning",
        "stage: 提取",
        f'source: "[[{Path(raw_name).stem}]]"',
        f"created: {today}",
        "---",
        "",
        "# 01 · 目标与信息地图",
        "",
        "## 学习目标",
        "",
        f"> {sess.goal or '（未填写）'}",
        "",
        "## 骨架（去水版）",
        "",
        "```",
    ]
    for i, s in enumerate(sk, 1):
        lines01.append(f"{i}. {s}")
    lines01 += ["```", "", "## 信息地图", "", "| 时间戳 | 讲什么 | 相关度 |", "|---|---|---|"]
    for row in mp:
        start = row.get("start_sec", row.get("start", 0))
        end = row.get("end_sec", row.get("end", start + 60))
        rel = row.get("relevance", 0)
        lines01.append(
            f"| {_fmt_time(start)}–{_fmt_time(end)} | {row.get('summary', '')} | {_rel_label(rel)} |"
        )
    lines01 += ["", "## 相关", "", f"- {transcript_link}"]
    p01.write_text("\n".join(lines01), encoding="utf-8")
    paths["outline"] = str(p01.relative_to(project_root()))

    # ── 02 自测题（含确认稿）──
    p02 = learning_dir / "02-自测题.md"
    lines02 = [
        "---",
        "type: learning",
        "stage: 自测",
        f'source: "[[{Path(raw_name).stem}]]"',
        f"created: {today}",
        "---",
        "",
        "# 02 · 自测题",
        "",
        f"> 关联：{learning_link}",
        "",
        "## 题目与确认答案",
        "",
    ]
    qmap = {q.get("id", i + 1): q for i, q in enumerate(sess.questions)}
    confirmed = {c.get("id"): c for c in sess.quiz_confirmed}
    for qid, q in sorted(qmap.items(), key=lambda x: x[0] if isinstance(x[0], int) else 0):
        lines02.append(f"### Q{qid}. {q.get('text', '')}")
        lines02.append("")
        ans = confirmed.get(qid, {})
        lines02.append(f"**确认答案**：{ans.get('answer', '（未确认）')}")
        grade = next((g for g in sess.quiz_grades if g.get("id") == qid), None)
        if grade:
            lines02.append(f"**批改**：{grade.get('summary', '')} ({grade.get('verdict', '')})")
        lines02.append("")
    lines02 += ["## 相关", "", f"- {learning_link}", f"- {transcript_link}"]
    p02.write_text("\n".join(lines02), encoding="utf-8")
    paths["quiz"] = str(p02.relative_to(project_root()))

    # ── 03 费曼草稿 ──
    p03 = learning_dir / "03-费曼草稿.md"
    feyn = sess.feynman_confirmed or sess.feynman_draft or "（未完成）"
    lines03 = [
        "---",
        "type: learning",
        "stage: 费曼",
        f'source: "[[{Path(raw_name).stem}]]"',
        f"created: {today}",
        "---",
        "",
        "# 03 · 费曼草稿",
        "",
        f"> 关联：[[{title}/02-自测题]]",
        "",
        feyn,
        "",
        "## 相关",
        "",
        f"- [[{title}/02-自测题]]",
        f"- {transcript_link}",
    ]
    p03.write_text("\n".join(lines03), encoding="utf-8")
    paths["feynman"] = str(p03.relative_to(project_root()))

    # ── 5-output 学习报告 ──
    report_name = f"学习报告-{title}-{today.replace('-', '')}.md"
    p_report = ws / "5-output" / report_name
    report_body = sess.report or f"# 学习报告 · {title}\n\n（未生成）"
    if "## 相关" not in report_body:
        report_body += (
            f"\n\n## 相关\n\n- {learning_link}\n- [[{title}/03-费曼草稿]]\n"
        )
    p_report.write_text(report_body, encoding="utf-8")
    paths["report"] = str(p_report.relative_to(project_root()))

    # ── 5-output 费曼沉淀候选（不自动写 2-wiki）──
    if sess.feynman_confirmed:
        wiki_cand = ws / "5-output" / f"费曼沉淀候选-{title}-{today.replace('-', '')}.md"
        wiki_body = [
            "---",
            "type: output",
            "status: draft",
            "tags: [learning/feynman-candidate]",
            f"created: {today}",
            f'sources: ["[[{title}/03-费曼草稿]]"]',
            "---",
            "",
            f"# 沉淀候选 · {title}",
            "",
            "> 请主人审阅后手动合并进 [[2-wiki/]]。AI 不自动写入 wiki。",
            "",
            sess.feynman_confirmed,
            "",
            "## 相关",
            "",
            f"- [[{title}/03-费曼草稿]]",
            f"- {learning_link}",
        ]
        wiki_cand.write_text("\n".join(wiki_body), encoding="utf-8")
        paths["wiki_candidate"] = str(wiki_cand.relative_to(project_root()))

    # ── 盲区库 ──
    blind_path = ws / "0.5-learning" / "_盲区库.md"
    spots: list[str] = []
    for g in sess.quiz_grades:
        spots.extend(g.get("gaps") or [])
    if sess.feynman_grade:
        spots.extend(sess.feynman_grade.get("blind_spots") or [])
        spots.extend(sess.feynman_grade.get("missing") or [])
    spots = [s for s in spots if s and s.strip()]
    if spots:
        if not blind_path.exists():
            blind_path.write_text(
                "# 盲区库\n\n> 常驻累积，不参与清理。\n\n",
                encoding="utf-8",
            )
        block = [
            f"\n## {today} · {title}\n",
            f"> 来源：{learning_link}\n",
        ]
        for s in spots[:10]:
            block.append(f"- {s}")
        with blind_path.open("a", encoding="utf-8") as f:
            f.write("\n".join(block) + "\n")
        paths["blind_spots"] = str(blind_path.relative_to(project_root()))

    return paths
