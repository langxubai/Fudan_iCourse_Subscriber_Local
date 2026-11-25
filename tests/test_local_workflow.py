"""Offline regression for the local HTTP workflow. Uses only the standard library."""

import http.client
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from contextlib import ExitStack
from http.server import ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import local_app
from src.data.database import Database
from src.runtime import local_sync
from src.runtime.local_store import LocalStore


COURSE = "42"
RECORDINGS = ("101", "102", "103")
KEYS = ("fixture-only-credential-first", "fixture-only-credential-second")


def trace(event, **detail):
    with (Path(os.environ["DATA_DIR"]) / "fixture-events.jsonl").open("a") as log:
        print(json.dumps({"event": event, **detail}), file=log)


class School:
    def __init__(self, session):
        pass

    def check_alive(self):
        return True

    def get_course_detail(self, course_id):
        from src.runtime.config import resolve_model_providers
        trace("discover", model_count=len(resolve_model_providers()))
        return {"title": "Fixture Course", "teacher": "Fixture Teacher", "lectures": [
            {"sub_id": n, "sub_title": "Lecture " + n, "has_playback": True, "date": "2026-01-01"}
            for n in RECORDINGS
        ]}

    def get_transcript_segments(self, sub_id):
        trace("transcript", sub_id=sub_id)
        return [{"start_ms": 0, "end_ms": 1000, "text": "fixture-content-" + sub_id}]


class Model:
    def summarize(self, course_title, text):
        from src.runtime.config import resolve_model_providers
        options = resolve_model_providers()
        assert len(options) == 1 and len(options[0]["models"]) == 1
        option = options[0]
        trace("model", model=option["models"][0], text=text,
              selected_key=option["api_key"] == KEYS[0], url=option["base_url"],
              has_unused_key=bool(os.environ.get("GEMINI_API_KEY")))
        if (Path(os.environ["DATA_DIR"]) / "fail-model").exists():
            raise RuntimeError("Synthetic provider failure")
        # Hold a busy worker so the HTTP test can inspect live progress and locking.
        deadline = time.monotonic() + 12
        while not (Path(os.environ["DATA_DIR"]) / "release-model").exists():
            if time.monotonic() >= deadline:
                raise TimeoutError("fixture model was never released")
            time.sleep(0.02)
        return "Fixture summary", option["models"][0]


def fixture_worker():
    # The real CLI, worker, reporter, database, selection, and lecture runner execute.
    # Replace school, model and media integrations before they are ever imported.
    services = {
        "src.api.icourse": SimpleNamespace(ICourseClient=School),
        "src.ai.summarizer": SimpleNamespace(Summarizer=Model),
        "src.ai.transcriber": SimpleNamespace(Transcriber=MagicMock, NoAudioStreamError=RuntimeError,
                                              IncompleteAudioError=RuntimeError),
        "src.pipeline.ppt_pipeline": SimpleNamespace(PPTPipeline=lambda *args: MagicMock()),
        "src.runtime.scheduler": SimpleNamespace(Scheduler=MagicMock),
    }
    with patch.dict(sys.modules, services), patch("main.login_with_retry", return_value=None), \
         patch.object(local_sync, "read_environment", lambda **kwargs: dict(os.environ)):
        sys.argv = ["local_app.py", "_worker"]
        return local_app.main()


class LocalWorkflowTest(unittest.TestCase):
    def setUp(self):
        cleanup = self.enterContext(ExitStack())
        self.directory = Path(cleanup.enter_context(tempfile.TemporaryDirectory()))
        self.env = {"StuId": "fixture-user", "UISPsw": "fixture-password", "COURSE_IDS": COURSE,
                    "DATA_DIR": str(self.directory), "USE_OFFICIAL_TRANSCRIPT": "true"}
        self.store = LocalStore(self.env)
        Database(str(self.store.db)).conn.close()
        for module in (local_app, local_sync):
            cleanup.enter_context(patch.object(module, "read_environment", lambda **kwargs: self.env.copy()))

        real_popen = subprocess.Popen
        self.workers = []

        def launch(args, **kwargs):
            self.assertEqual(args[-1], "_worker")
            proc = real_popen([sys.executable, str(Path(__file__).resolve()), "fixture-worker"], **kwargs)
            self.workers.append(proc)
            return proc

        cleanup.enter_context(patch.object(local_sync.subprocess, "Popen", launch))
        server = ThreadingHTTPServer(("127.0.0.1", 0), local_app.LocalHandler)
        server.store = self.store
        self.port = server.server_port
        thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True)
        thread.start()
        cleanup.callback(server.server_close)
        cleanup.callback(thread.join)
        cleanup.callback(server.shutdown)
        cleanup.callback(self.finish_workers)

    def finish_workers(self):
        (self.directory / "release-model").touch()
        for proc in self.workers:
            proc.wait(timeout=15)

    def api(self, route, body=None, expected=200):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        try:
            conn.request("GET" if body is None else "POST", "/api/" + route,
                         body=None if body is None else json.dumps(body),
                         headers={"Content-Type": "application/json"})
            response = conn.getresponse()
            raw = response.read().decode()
            for secret in KEYS:
                self.assertNotIn(secret, raw)
            self.assertEqual(response.status, expected, raw)
            return json.loads(raw)
        finally:
            conn.close()

    def wait_for(self, condition):
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            state = self.api("status")
            if condition(state):
                return state
            time.sleep(0.025)
        log = self.directory / "sync.log"
        self.fail("Timed out waiting for worker: " + (log.read_text() if log.exists() else str(state)))

    def discover(self):
        self.api("discover", {}, 202)
        self.wait_for(lambda state: not state["running"] and state["phase"] == "ready")
        self.assertEqual(self.workers[-1].wait(timeout=5), 0)
        return self.api("plan")

    def events(self):
        return [json.loads(line) for line in (self.directory / "fixture-events.jsonl").read_text().splitlines()]

    def test_discover_and_process_one_confirmed_recording(self):
        self.assertEqual(self.api("providers")["providers"], [])
        plan = self.discover()
        self.assertEqual({r["sub_id"] for r in plan["lectures"]}, set(RECORDINGS))
        self.assertEqual(self.events(), [{"event": "discover", "model_count": 0}])
        chosen = {"course_id": COURSE, "sub_id": RECORDINGS[1]}
        body = {"plan_id": plan["plan_id"], "lectures": [chosen]}
        self.api("process", body, 400)

        ids = []
        for label, key in zip(("first", "second"), KEYS):
            result = self.api("providers", {"name": label, "model": label + "-model",
                              "base_url": "https://" + label + ".invalid/v1", "api_key": key})
            ids.append(result["selected_id"])
        self.assertNotEqual(ids[0], ids[1])
        self.assertEqual(len(self.api("providers")["providers"]), 2)
        self.env["GEMINI_API_KEY"] = "unselected-fixture-key"
        body["provider_id"] = ids[0]  # Intentionally different from the current default.
        self.api("process", {**body, "lectures": []}, 400)
        self.api("process", {**body, "lectures": [{**chosen, "sub_id": "unknown"}]}, 400)
        self.api("process", {**body, "provider_id": "unknown"}, 400)
        newer = self.discover()
        self.assertNotEqual(plan["plan_id"], newer["plan_id"])
        self.api("process", body, 400)
        body["plan_id"] = newer["plan_id"]
        self.api("process", {**body, "lectures": [chosen, chosen]}, 202)
        running = self.wait_for(lambda state: state["running"] and
                                (state.get("progress") or {}).get("stage") == "summarize")
        self.assertEqual((running["phase"], running["progress"]["total"], running["progress"]["sub_id"]),
                         ("processing", 1, RECORDINGS[1]))
        self.api("process", body, 409)
        (self.directory / "release-model").touch()
        finished = self.wait_for(lambda state: not state["running"])
        self.assertEqual(self.workers[-1].wait(timeout=5), 0)
        self.assertEqual((finished["phase"], finished["pending_count"], finished["progress"]["stage"]),
                         ("finished", 2, "done"))
        self.api("process", body, 400)
        self.assertEqual({r["sub_id"] for r in self.api("plan")["lectures"]}, {RECORDINGS[0], RECORDINGS[2]})
        chosen_note = self.api("lectures/" + RECORDINGS[1])
        self.assertEqual(chosen_note["summary"], "Fixture summary")
        for other in (RECORDINGS[0], RECORDINGS[2]):
            untouched = self.api("lectures/" + other)
            self.assertFalse(untouched["summary"] or untouched["transcript"])
        effects = self.events()
        model_calls = [e for e in effects if e["event"] == "model"]
        self.assertEqual(len(model_calls), 1)
        self.assertEqual(model_calls[0]["model"], "first-model")
        self.assertTrue(model_calls[0]["selected_key"])
        self.assertFalse(model_calls[0]["has_unused_key"])
        self.assertEqual(model_calls[0]["url"], "https://first.invalid/v1")
        self.assertIn("fixture-content-" + RECORDINGS[1], model_calls[0]["text"])
        self.assertEqual({e["sub_id"] for e in effects if e["event"] == "transcript"}, {RECORDINGS[1]})

    def test_fast_failure_requires_new_confirmation_before_retry(self):
        plan = self.discover()
        self.env.update(LLM_BASE_URL="https://fixture.invalid/v1", LLM_MODEL="fixture", LLM_API_KEY=KEYS[0])
        provider = self.api("providers")["selected_id"]
        chosen = {"course_id": COURSE, "sub_id": RECORDINGS[0]}
        body = {"plan_id": plan["plan_id"], "lectures": [chosen], "provider_id": provider}
        (self.directory / "fail-model").touch()
        self.api("process", body, 202)
        self.assertEqual(self.workers[-1].wait(timeout=8), 1)
        self.assertEqual(self.api("status")["phase"], "error")
        remaining = self.api("plan")
        self.assertNotEqual(remaining["plan_id"], plan["plan_id"])
        self.assertEqual({r["sub_id"] for r in remaining["lectures"]}, set(RECORDINGS))
        self.api("process", body, 400)
        self.assertEqual(sum(e["event"] == "model" for e in self.events()), 1)


if __name__ == "__main__":
    if sys.argv[1:] == ["fixture-worker"]:
        raise SystemExit(fixture_worker())
    unittest.main()
