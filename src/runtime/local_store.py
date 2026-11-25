"""Configuration and read views shared by the local server and sync command."""

from __future__ import annotations

import json
import os
import re
import sqlite3
from contextlib import closing
from pathlib import Path
from tempfile import NamedTemporaryFile

ROOT = Path(__file__).resolve().parents[2]


def read_environment(*, strict: bool = True) -> dict[str, str]:
    """Read literal values. Shell environment overrides the project .env file."""
    values = {}
    path = ROOT / ".env"
    if path.is_file():
        for number, line in enumerate(path.read_text(encoding="utf-8-sig").splitlines(), 1):
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            key, sep, value = line.removeprefix("export ").partition("=")
            key, value = key.strip(), value.strip()
            if not sep or not re.fullmatch(r"[A-Za-z_][A-Za-z_0-9]*", key):
                if strict:
                    raise ValueError(f".env 第 {number} 行的格式应为 KEY=value。")
                continue
            if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
                value = value[1:-1]
            values[key] = value
    values.update(os.environ)
    return values


def course_ids(values: list) -> list[str]:
    if not isinstance(values, list) or any(
        not isinstance(v, str) or not re.fullmatch(r"[0-9]+", v) for v in values
    ):
        raise ValueError("课程 ID 应是由数字组成的列表。")
    return list(dict.fromkeys(values))


def save_json(path: Path, value: dict) -> None:
    # Readers always see a complete snapshot, including during concurrent requests.
    with NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent, delete=False) as f:
        temp = Path(f.name)
        try:
            json.dump(value, f, ensure_ascii=False)
            f.flush()
            os.replace(temp, path)
        finally:
            temp.unlink(missing_ok=True)


class LocalStore:
    def __init__(self, env: dict[str, str]):
        self.data = (ROOT / (env.get("DATA_DIR") or "data")).resolve()
        self.db = (ROOT / (env.get("DB_PATH") or self.data / "icourse.db")).resolve()
        self.data.mkdir(mode=0o700, parents=True, exist_ok=True)

    def subscriptions(self, env: dict[str, str]) -> list[str]:
        path = self.data / "subscriptions.json"
        if path.exists():
            return course_ids(json.loads(path.read_text(encoding="utf-8"))["course_ids"])
        return course_ids([s.strip() for s in env.get("COURSE_IDS", "").split(",") if s.strip()])

    def save_subscriptions(self, ids: list) -> dict:
        result = {"course_ids": course_ids(ids)}
        save_json(self.data / "subscriptions.json", result)
        return result

    def connect(self):
        conn = sqlite3.connect(self.db.as_uri() + "?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row
        conn.execute("BEGIN")  # Keep lecture metadata and pages in a single snapshot.
        return conn

    def library(self) -> dict:
        with closing(self.connect()) as conn:
            courses = {
                row["course_id"]: {**row, "lectures": []}
                for row in conn.execute("SELECT course_id,title,teacher FROM courses ORDER BY title,course_id")
            }
            for row in conn.execute("""
                SELECT course_id,sub_id,sub_title,date,processed_at,error_msg,
                       coalesce(length(summary),0)>0 AS has_summary
                FROM lectures ORDER BY date DESC,sub_title DESC,sub_id DESC
            """):
                if row["course_id"] in courses:
                    courses[row["course_id"]]["lectures"].append({**row, "has_summary": bool(row["has_summary"])})
        return {"courses": list(courses.values())}

    def lecture(self, sub_id: str) -> dict | None:
        with closing(self.connect()) as conn:
            row = conn.execute("""
                SELECT l.sub_id,l.sub_title,l.date,l.course_id,c.title AS course_title,c.teacher,
                       l.summary,l.transcript,l.summary_model,l.processed_at,l.error_msg
                FROM lectures l JOIN courses c USING(course_id) WHERE l.sub_id=?
            """, (sub_id,)).fetchone()
            if row is None:
                return None
            pages = conn.execute("""
                SELECT page_num,text,ocr_status FROM ppt_pages WHERE sub_id=? ORDER BY page_num
            """, (sub_id,))
            return {**row, "pages": [dict(p) for p in pages]}


def lecture_markdown(lecture: dict) -> str:
    title = lecture["course_title"] or lecture["course_id"]
    sub = lecture["sub_title"] or lecture["sub_id"]
    parts = [f"# {title} · {sub}"]
    for key, label in [("date", "日期"), ("teacher", "教师"), ("summary_model", "摘要模型")]:
        if lecture.get(key):
            parts.append(f"{label}：{lecture[key]}")
    parts += ["## 课程摘要", lecture.get("summary") or "暂无摘要。", "## 课堂转录",
              lecture.get("transcript") or "暂无转录。", "## PPT 文字"]
    for p in lecture["pages"]:
        if p["text"]:
            parts.extend([f"### 第 {p['page_num']} 页", p["text"]])
    if parts[-1] == "## PPT 文字":
        parts.append("暂无课件文字。")
    return "\n\n".join(parts) + "\n"
