"""学习会话：outline / highlight / questions / 批改 / 费曼 / 报告。"""

from __future__ import annotations

import json
import threading
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from ..paths import user_data_dir


@dataclass
class LearningSession:
    job_id: str
    title: str = ""
    goal: str = ""
    outline: dict[str, Any] | None = None
    highlights: dict[str, Any] | None = None
    questions: list[dict[str, Any]] = field(default_factory=list)
    # 自测：用户答案、批改、多轮对话、确认稿
    quiz_answers: list[dict[str, Any]] = field(default_factory=list)
    quiz_grades: list[dict[str, Any]] = field(default_factory=list)
    quiz_chats: dict[str, list[dict[str, str]]] = field(default_factory=dict)
    quiz_confirmed: list[dict[str, Any]] = field(default_factory=list)
    # 费曼
    feynman_draft: str = ""
    feynman_grade: dict[str, Any] | None = None
    feynman_chat: list[dict[str, str]] = field(default_factory=list)
    feynman_confirmed: str = ""
    # 报告与入库
    report: str = ""
    persisted: dict[str, str] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class SessionStore:
    def __init__(self) -> None:
        self._sessions: dict[str, LearningSession] = {}
        self._lock = threading.Lock()
        self._dir = user_data_dir() / "learning"
        self._dir.mkdir(parents=True, exist_ok=True)

    def _path(self, job_id: str) -> Path:
        return self._dir / f"{job_id}.json"

    def get(self, job_id: str) -> LearningSession:
        with self._lock:
            if job_id in self._sessions:
                return self._sessions[job_id]
            p = self._path(job_id)
            if p.exists():
                try:
                    data = json.loads(p.read_text(encoding="utf-8"))
                    fields = LearningSession.__dataclass_fields__
                    # quiz_chats 的 key 在 JSON 里是 str
                    kwargs = {k: data[k] for k in fields if k in data}
                    if "quiz_chats" in kwargs and isinstance(kwargs["quiz_chats"], dict):
                        kwargs["quiz_chats"] = {
                            str(k): v for k, v in kwargs["quiz_chats"].items()
                        }
                    sess = LearningSession(**kwargs)
                    self._sessions[job_id] = sess
                    return sess
                except Exception:
                    pass
            sess = LearningSession(job_id=job_id)
            self._sessions[job_id] = sess
            return sess

    def save(self, sess: LearningSession) -> None:
        with self._lock:
            self._sessions[sess.job_id] = sess
            self._path(sess.job_id).write_text(
                json.dumps(sess.to_dict(), ensure_ascii=False, indent=2),
                encoding="utf-8",
            )


store = SessionStore()
