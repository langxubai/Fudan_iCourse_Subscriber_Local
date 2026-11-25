"""Private, per-library model settings and one-model snapshots for sync."""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
from contextlib import contextmanager
from urllib.parse import urlsplit
from uuid import uuid4

from src.runtime.config import resolve_model_providers
from src.runtime.local_store import LocalStore, save_json


def _text(value, label):
    if not isinstance(value, str) or not value.strip() or not value.strip().isprintable():
        raise ValueError(f"请填写有效的{label}。")
    return value.strip()


def _endpoint(value):
    value = _text(value, "API 基础地址").rstrip("/")
    error = "请填写 HTTP 或 HTTPS API 基础地址，将密钥单独填在密钥栏。"
    try:
        url = urlsplit(value)
        host = url.hostname
        port = url.port
    except ValueError:
        raise ValueError(error) from None
    if (not host or port == 0 or url.scheme not in {"http", "https"} or url.username is not None
            or any(c in value for c in ("?", "#", "\\")) or any(c.isspace() for c in value)):
        raise ValueError(error)
    return value


def _environment_options(env):
    # Resolve the legacy LLM_* and built-in settings separately: both are
    # selectable even when the cloud runner would prefer the custom endpoint.
    custom = ("LLM_BASE_URL", "LLM_MODEL", "LLM_API_KEY")
    entries = []
    if all(env.get(key, "").strip() for key in custom):
        entries.append(dict(name="自选接口", base_url=env[custom[0]],
                            models=[env[custom[1]]], api_key=env[custom[2]]))
    builtins = resolve_model_providers({**env, **dict.fromkeys(custom, "")})
    options = []
    for entry in [*entries, *builtins]:
        # An unusable .env entry should not block a different, valid choice.
        try:
            url = _endpoint(entry["base_url"])
            secret = _text(entry["api_key"], "API 密钥")
        except ValueError:
            continue
        for model_id in entry["models"]:
            try:
                model = _text(model_id, "模型 ID")
            except ValueError:
                continue
            name = entry["name"]
            identity = json.dumps([name, url, model], ensure_ascii=False).encode("utf-8")
            option_id = "env-" + hashlib.sha256(identity).hexdigest()
            options.append(dict(id=option_id, name=name, model=model, base_url=url,
                                api_key=secret, source="env"))
    return options


class LocalProviders:
    def __init__(self, store: LocalStore):
        self.path = store.data / "providers.json"

    def _read(self):
        try:
            state = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return {"profiles": []}
        except (ValueError, UnicodeError):
            raise ValueError("本机模型配置文件格式无效，请检查 providers.json。") from None
        try:
            if not isinstance(state["profiles"], list):
                raise ValueError
            for row in state["profiles"]:
                for key in ("id", "name", "model", "api_key"):
                    _text(row[key], key)
                _endpoint(row["base_url"])
            return state
        except (TypeError, KeyError, ValueError):
            raise ValueError("本机模型配置文件格式无效，请检查 providers.json。") from None

    @contextmanager
    def _update(self):
        lock_path = self.path.with_suffix(".lock")
        with os.fdopen(os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600), "rb+") as lock:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
            state = self._read()
            yield state
            # save_json replaces the file atomically with a 0600 temporary file.
            save_json(self.path, state)

    def _options(self, state, env):
        return _environment_options(env) + [{**row, "source": "local"} for row in state["profiles"]]

    @staticmethod
    def _selected_id(state, options):
        if "selected_id" not in state:
            return options[0]["id"] if options else None
        selected = state["selected_id"]
        return selected if any(row["id"] == selected for row in options) else None

    def _public(self, state, env):
        options = self._options(state, env)
        visible = ("id", "name", "model", "base_url", "source")
        return {"providers": [{k: row[k] for k in visible} for row in options],
                "selected_id": self._selected_id(state, options)}

    def list(self, env):
        return self._public(self._read(), env)

    def save(self, body, env):
        name = _text(body.get("name"), "显示名称")
        url = _endpoint(body.get("base_url"))
        model = _text(body.get("model"), "模型 ID")
        with self._update() as state:
            option_id = body.get("id")
            if option_id is None:
                row = {"id": "local-" + uuid4().hex}
                state["profiles"].append(row)
                state["selected_id"] = row["id"]
            else:
                row = next((r for r in state["profiles"] if r["id"] == option_id), None)
                if row is None:
                    raise ValueError("找不到可编辑的本机模型设置。")
            secret = body.get("api_key")
            if secret in (None, ""):
                if row.get("base_url") != url and row.get("api_key"):
                    raise ValueError("更改 API 地址后，请重新填写该服务的密钥。")
                secret = row.get("api_key")
            row.update(name=name, base_url=url, model=model, api_key=_text(secret, "API 密钥"))
            return self._public(state, env)

    def select(self, option_id, env):
        with self._update() as state:
            if not any(row["id"] == option_id for row in self._options(state, env)):
                raise ValueError("所选模型已不可用，请重新选择。")
            state["selected_id"] = option_id
            return self._public(state, env)

    def delete(self, option_id, env):
        with self._update() as state:
            for row in state["profiles"]:
                if row["id"] == option_id:
                    if self._selected_id(state, self._options(state, env)) == option_id:
                        state["selected_id"] = None
                    state["profiles"].remove(row)
                    return self._public(state, env)
            raise ValueError("找不到可删除的本机模型设置。")

    def resolve(self, env, provider_id):
        """Freeze the confirmed profile and its display label from the same snapshot."""
        for row in self._options(self._read(), env):
            if row["id"] == provider_id:
                settings = {"LLM_BASE_URL": row["base_url"], "LLM_MODEL": row["model"],
                            "LLM_API_KEY": row["api_key"]}
                return settings, f"{row['name']} · {row['model']}"
        raise ValueError("请在模型设置中选择可用的摘要模型。")
