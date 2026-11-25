"""Start a single local sync, shared by the browser and scheduled CLI runs."""

from __future__ import annotations

import fcntl
import json
import os
import shutil
import subprocess
import sys
import tempfile
import traceback
from datetime import datetime
from pathlib import Path

from src.runtime.local_store import ROOT, LocalStore, read_environment, save_json


def now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def idle() -> dict:
    return {"running": False, "message": "尚未同步", "started_at": None, "finished_at": None}


class SyncBusy(Exception):
    pass


def _lock(store: LocalStore):
    store.db.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    handle = open(str(store.db) + ".sync.lock", "a+b")
    try:
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        handle.close()
        raise SyncBusy("已有同步任务正在运行。") from None
    return handle


def status(store: LocalStore) -> dict:
    path = store.data / "sync-status.json"
    state = json.loads(path.read_text(encoding="utf-8")) if path.exists() else idle()
    if not state["running"]:
        return state
    try:
        lock = _lock(store)
    except SyncBusy:
        return state
    with lock:
        state = json.loads(path.read_text(encoding="utf-8"))
        if state["running"]:
            state.update(running=False, message="上次同步中断，可以再次检查更新。")
            save_json(path, state)
        return state


def start_sync(store: LocalStore, *, background: bool) -> tuple[subprocess.Popen | None, dict]:
    from src.runtime.config import resolve_model_providers

    with _lock(store) as lock:
        # Only this DB's lock owner can have a live scratch directory here.
        scratch = store.db.with_name("." + store.db.name + ".audio")
        for abandoned in scratch.glob("run-*"):
            if abandoned.is_dir() and not abandoned.is_symlink():
                shutil.rmtree(abandoned)
        env = read_environment()
        ids = store.subscriptions(env)
        path = store.data / "sync-status.json"
        start = now()
        if not ids:
            state = dict(running=False, started_at=start, finished_at=start, message="尚未订阅课程，本次无需同步。")
            save_json(path, state)
            return None, state

        missing = [key for key in ("StuId", "UISPsw") if not env.get(key)]
        if not resolve_model_providers(env):
            missing.append("模型供应商的 API Key")
        if missing:
            raise ValueError("请在项目的 .env 中填写" + "、".join(missing) + "，保存后重新检查更新。")

        state = dict(running=True, started_at=start, finished_at=None, message="正在检查订阅并处理新课次…")
        env.update(COURSE_IDS=",".join(ids), DATA_DIR=str(store.data), DB_PATH=str(store.db),
                   ICOURSE_SCRATCH_DIR=str(scratch))
        interpreter = ROOT / ".venv/bin/python"
        if not interpreter.is_file():
            interpreter = Path(sys.executable)
        save_json(path, state)
        try:
            with (store.data / "sync.log").open("w", encoding="utf-8") as log:
                # The worker inherits the open lock. A server restart cannot start a duplicate.
                process = subprocess.Popen(
                    [str(interpreter), "-u", str(ROOT / "local_app.py"), "_worker", start],
                    env=env, cwd=ROOT, stdin=subprocess.DEVNULL, stdout=log,
                    stderr=subprocess.STDOUT, pass_fds=(lock.fileno(),), start_new_session=background,
                )
        except OSError:
            state.update(running=False, finished_at=now(), message="同步启动失败，请检查本地 Python 环境。")
            save_json(path, state)
            raise
        return process, state


def worker(start: str) -> int:
    data = Path(os.environ["DATA_DIR"])
    message = "同步失败，详情见 " + str(data / "sync.log")
    try:
        from main import run
        from src.runtime import config

        scratch = Path(os.environ["ICOURSE_SCRATCH_DIR"])
        scratch.mkdir(mode=0o700, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix="run-", dir=scratch) as audio_dir:
            config.AUDIO_DIR = audio_dir
            code = run(local=True)
        if code == 0:
            message = "同步完成，课程库已是最新。"
        else:
            message = "同步未全部完成，详情见 " + str(data / "sync.log")
    except KeyboardInterrupt:
        code = 130
        message = "同步已中断，可以重新检查更新。"
    except Exception:
        traceback.print_exc()
        code = 1
    save_json(data / "sync-status.json", dict(running=False, message=message,
              started_at=start, finished_at=now()))
    return code
