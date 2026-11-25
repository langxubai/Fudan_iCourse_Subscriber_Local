"""Discover recordings and process an explicitly confirmed selection on this Mac."""

from __future__ import annotations

import fcntl
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import traceback
from contextlib import closing
from datetime import datetime
from pathlib import Path
from uuid import uuid4

from src.data.schema import PENDING_LECTURE_SQL
from src.runtime.local_providers import LocalProviders
from src.runtime.local_store import ROOT, LocalStore, read_environment, save_json


def now() -> str:
    return datetime.now().astimezone().isoformat(timespec="microseconds")


def empty_plan() -> dict:
    return {"plan_id": None, "lectures": [], "failed_courses": [], "created_at": None}


def current_plan(store: LocalStore, env: dict | None = None) -> dict:
    """Only offer still-subscribed, unfinished rows in the most recent discovery."""
    path = store.data / "sync-plan.json"
    if not path.is_file():
        return empty_plan()
    plan = json.loads(path.read_text(encoding="utf-8"))
    if not plan["lectures"]:
        return plan
    ids = set(store.subscriptions(env if env is not None else read_environment(strict=False)))
    with closing(store.connect()) as conn:
        pending = {
            (r["course_id"], r["sub_id"]): r["error_stage"]
            for r in conn.execute("SELECT course_id,sub_id,error_stage FROM lectures WHERE " + PENDING_LECTURE_SQL)
        }
    rows = []
    for row in plan["lectures"]:
        pair = row["course_id"], row["sub_id"]
        if pair[0] in ids and pair in pending:
            stage = pending[pair]
            rows.append({**row, "error_stage": stage, "error_msg": "上次处理未完成" if stage else ""})
    return {**plan, "lectures": rows}


def idle() -> dict:
    return dict(running=False, phase="idle", operation=None, message="尚未检查新课次", started_at=None,
                finished_at=None, progress=None, plan_id=None, pending_count=0, provider_label="")


class SyncBusy(Exception):
    pass


def _lock(store: LocalStore):
    store.db.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    handle = open(str(store.db) + ".sync.lock", "a+b")
    try:
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        handle.close()
        raise SyncBusy("已有任务正在运行，请等待本次任务完成。") from None
    return handle


def _legacy_status(store: LocalStore, saved: dict) -> dict:
    """A worker started before the upgrade only exposes progress in its text log."""
    state = {**idle(), **saved}
    if not state["running"]:
        state["phase"] = "finished"
        return state
    state.update(phase="processing", operation="process", message="升级前启动的任务正在解析")
    log = store.data / "sync.log"
    if not log.is_file():
        return state
    total = started = 0
    course = lecture = ""
    stage, detail = "prepare", "准备课程资料"
    with log.open(encoding="utf-8", errors="replace") as lines:
        for line in lines:
            if match := re.match(r"  New/retry lectures: (\d+)", line):
                total += int(match[1])
            elif match := re.match(r"  -- \[(.+?)\] (.+) \([^()]*\)$", line):
                started += 1
                course, lecture = match.groups()
                stage, detail = "prepare", "准备录播和课件"
            elif started:
                if "[Time] Generating summary" in line:
                    stage, detail = "summarize", "正在生成课程摘要"
                elif "[Transcriber]" in line and stage != "summarize":
                    stage, detail = "transcribe", "正在转写课堂音频"
                elif "[Time] Done at" in line:
                    stage, detail = "done", "当前课次已完成"
    state["progress"] = dict(course_id="", sub_id="", course_title=course, sub_title=lecture,
                             completed=max(0, started - (stage != "done")), total=total,
                             stage=stage, detail=detail)
    return state


def status(store: LocalStore) -> dict:
    path = store.data / "sync-status.json"
    if not path.exists():
        return idle()
    saved = json.loads(path.read_text(encoding="utf-8"))
    state = saved if "phase" in saved else _legacy_status(store, saved)
    if not state["running"]:
        return state
    try:
        lock = _lock(store)
    except SyncBusy:
        return state
    with lock:
        saved = json.loads(path.read_text(encoding="utf-8"))
        state = {**idle(), **saved}
        if state["running"]:
            plan = current_plan(store)
            state.update(running=False, phase="error", finished_at=now(),
                         plan_id=plan["plan_id"], pending_count=len(plan["lectures"]),
                         message="上次任务中断，已保存的笔记仍可阅读。")
            save_json(path, state)
        return state


def _selection(body: dict, plan: dict) -> list[tuple[str, str]]:
    if not body.get("plan_id") or body["plan_id"] != plan["plan_id"]:
        raise ValueError("课次清单已变更，请重新打开待选课次。")
    selected = body.get("lectures")
    if not isinstance(selected, list) or not selected:
        raise ValueError("请至少勾选一个待解析课次。")
    available = {(r["course_id"], r["sub_id"]) for r in plan["lectures"]}
    pairs = []
    for row in selected:
        if not isinstance(row, dict) or not all(isinstance(row.get(k), str) for k in ("course_id", "sub_id")):
            raise ValueError("所选课次的格式无效。")
        pair = row["course_id"], row["sub_id"]
        if pair not in available:
            raise ValueError("所选课次已完成、已退订或不在本次清单中，请重新选择。")
        pairs.append(pair)
    return list(dict.fromkeys(pairs))


def start_sync(store: LocalStore, *, background: bool, selection: dict | None = None):
    """No selection means discovery only, including for unattended scheduled runs."""
    from src.runtime.config import MODEL_PROVIDERS

    with _lock(store) as lock:
        env = read_environment()
        ids = store.subscriptions(env)
        path = store.data / "sync-status.json"
        state = {**idle(), "started_at": now()}
        if not ids and selection is None:
            save_json(store.data / "sync-plan.json", empty_plan())
            state.update(phase="finished", finished_at=now(), message="尚未订阅课程，本次无需检查。")
            save_json(path, state)
            return None, state

        missing = [key for key in ("StuId", "UISPsw") if not env.get(key)]
        if missing:
            raise ValueError("请在项目的 .env 中填写" + "、".join(missing) + "，保存后重试。")
        selected_model = {}
        pairs = []
        if selection is None:
            state.update(phase="discovering", operation="discover", message="正在检查订阅课程的录播…")
        else:
            plan = current_plan(store, env)
            pairs = _selection(selection, plan)
            provider_id = selection.get("provider_id")
            if not isinstance(provider_id, str) or not provider_id:
                raise ValueError("请为本次解析选择 API 和模型。")
            selected_model, label = LocalProviders(store).resolve(env, provider_id)
            state.update(phase="processing", operation="process", message="正在准备所选课次…", plan_id=plan["plan_id"],
                         pending_count=len(plan["lectures"]), provider_label=label)

        scratch = store.db.with_name("." + store.db.name + ".audio")
        for abandoned in scratch.glob("run-*"):
            if abandoned.is_dir() and not abandoned.is_symlink():
                shutil.rmtree(abandoned)
        # Discovery never needs a model key. Processing gets exactly one frozen endpoint/model/key.
        for key in ("LLM_BASE_URL", "LLM_MODEL", "LLM_API_KEY", *(p["api_key_env"] for p in MODEL_PROVIDERS)):
            env.pop(key, None)
        env.update(selected_model)
        env.update(COURSE_IDS=",".join(ids), DATA_DIR=str(store.data), DB_PATH=str(store.db),
                   ICOURSE_SCRATCH_DIR=str(scratch))
        interpreter = ROOT / ".venv/bin/python"
        if not interpreter.is_file():
            interpreter = Path(sys.executable)
        state.update(running=True)
        if selection is None:
            previous = current_plan(store, env)
            if previous["plan_id"]:
                previous["plan_id"] = uuid4().hex
                save_json(store.data / "sync-plan.json", previous)
                state.update(plan_id=previous["plan_id"], pending_count=len(previous["lectures"]))
        else:
            # Consume this confirmation even if a provider fails quickly. Retrying
            # a POST must never charge twice; a human retry gets the latest plan.
            plan["plan_id"] = uuid4().hex
            state["plan_id"] = plan["plan_id"]
            save_json(store.data / "sync-plan.json", plan)
        save_json(path, state)
        try:
            with tempfile.TemporaryFile(mode="w+", encoding="utf-8") as job, (store.data / "sync.log").open("w") as log:
                json.dump({"state": state, "pairs": pairs}, job, ensure_ascii=False)
                job.seek(0)
                process = subprocess.Popen(
                    [str(interpreter), "-u", str(ROOT / "local_app.py"), "_worker"],
                    env=env, cwd=ROOT, stdin=job, stdout=log, stderr=subprocess.STDOUT,
                    pass_fds=(lock.fileno(),), start_new_session=background,
                )
        except OSError:
            state.update(running=False, phase="error", finished_at=now(), message="任务启动失败，请检查本地 Python 环境。")
            save_json(path, state)
            raise
        return process, state


def worker() -> int:
    from main import discover_local, run
    from src.runtime import config

    job = json.load(sys.stdin)
    state = job["state"]
    store = LocalStore(os.environ)
    path = store.data / "sync-status.json"

    def progress(event):
        state["progress"] = event
        save_json(path, state)

    code = 0
    try:
        if state["phase"] == "discovering":
            results = discover_local(on_progress=progress)
            plan = {**results, "plan_id": uuid4().hex, "created_at": now()}
            if state["pending_count"] and len(results["failed_courses"]) == len(config.COURSE_IDS):
                raise RuntimeError("本次未能读取任何订阅课程的录播清单。")
            save_json(store.data / "sync-plan.json", plan)
            plan = current_plan(store, os.environ)
            count, errors = len(plan["lectures"]), len(plan["failed_courses"])
            state.update(plan_id=plan["plan_id"], pending_count=count, phase="ready" if count else "finished")
            state["message"] = f"发现 {count} 个待解析课次，请选择要解析的录播。" if count else "没有待解析的录播。"
            if errors:
                code = 1
                state["message"] = f"找到 {count} 个待解析课次，另有 {errors} 门课程检查失败。"
                if not count:
                    state["phase"] = "error"
        else:
            scratch = Path(os.environ["ICOURSE_SCRATCH_DIR"])
            scratch.mkdir(mode=0o700, exist_ok=True)
            with tempfile.TemporaryDirectory(prefix="run-", dir=scratch) as audio_dir:
                config.AUDIO_DIR = audio_dir
                code = run(local=True, selection=[tuple(p) for p in job["pairs"]], on_progress=progress)
            state.update(phase="error" if code else "finished",
                         message="部分课次未能解析，可重新选择重试。" if code else "所选课次解析完毕。")
    except KeyboardInterrupt:
        code = 130
        state.update(phase="error", message="任务已中断，已完成的课次已保存。")
    except Exception:
        traceback.print_exc()
        code = 1
        message = "任务未能完成，详情见 " + str(store.data / "sync.log")
        if state["operation"] == "discover" and state["pending_count"]:
            message = "检查失败，可以重试或查看上次检查留下的待选课次。"
        state.update(phase="error", message=message)
    finally:
        if state.get("plan_id"):
            state["pending_count"] = len(current_plan(store, os.environ)["lectures"])
        state.update(running=False, finished_at=now())
        save_json(path, state)
    return code
