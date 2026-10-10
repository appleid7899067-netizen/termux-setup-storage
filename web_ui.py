#!/usr/bin/env python3
"""Serve a local web client backed by the installed Codex CLI."""
import json
import os
import pty
import re
import select
import signal
import shutil
import subprocess
import tempfile
import threading
import time
import tomllib
import uuid
import sys
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote, urlsplit
from urllib.request import Request, urlopen

PROJECT = Path(__file__).resolve().parent
WEB_FILE = PROJECT / "web" / "index.html"
CODEX_HOME = Path(os.environ.get("CODEX_HOME", Path.home() / ".codex")).expanduser()
SESSION_FILE = CODEX_HOME / "codex-web-sessions.json"
TERMINAL_STORE = PROJECT / "data" / "terminal-history"
LOCK = threading.RLock()
TERMINALS = {}
TERMINAL_LOCK = threading.RLock()
INTEGRATION_TASKS = {}
INTEGRATION_TASK_LOCK = threading.RLock()
PLUGIN_CATALOG = None
PLUGIN_CATALOG_TIME = 0
FEATURE_CACHE = []
PROVIDER_STORE = CODEX_HOME / "web-provider-keys.json"
OPENROUTER_MODELS = []
OPENROUTER_MODELS_TIME = 0
PUTER_LOCK = threading.RLock()
PUTER_BRIDGE = None
PUTER_PROXY = None
PUTER_PROXY_URL = None


class BackgroundTerminal:
    """A PTY-backed shell that stays alive independently of the browser tab."""
    MAX_OUTPUT = 250_000

    def __init__(self, terminal_id):
        self.id = terminal_id
        self.master, slave = pty.openpty()
        shell = os.environ.get("SHELL") or shutil.which("bash") or shutil.which("sh") or "/bin/sh"
        env = os.environ.copy()
        env.setdefault("TERM", "xterm-256color")
        try:
            self.process = subprocess.Popen(
                [shell, "-i"], cwd=PROJECT, stdin=slave, stdout=slave,
                stderr=slave, env=env, close_fds=True, preexec_fn=os.setsid,
            )
        finally:
            os.close(slave)
        os.set_blocking(self.master, False)
        self.output = ""
        self.base = 0
        self.lock = threading.RLock()
        self.created_at = datetime.now(timezone.utc).isoformat()
        TERMINAL_STORE.mkdir(parents=True, exist_ok=True)
        (TERMINAL_STORE / f"{self.id}.json").write_text(
            json.dumps({"id": self.id, "cwd": str(PROJECT), "created_at": self.created_at}, ensure_ascii=False),
            encoding="utf-8",
        )
        (TERMINAL_STORE / f"{self.id}.log").write_text("", encoding="utf-8")
        self.reader = threading.Thread(target=self._read, daemon=True)
        self.reader.start()

    def _read(self):
        while True:
            try:
                ready, _, _ = select.select([self.master], [], [], 0.25)
                if not ready:
                    if self.process.poll() is not None:
                        break
                    continue
                chunk = os.read(self.master, 8192).decode("utf-8", errors="replace")
                if not chunk:
                    break
                with self.lock:
                    self.output += chunk
                    if len(self.output) > self.MAX_OUTPUT:
                        trim = len(self.output) - self.MAX_OUTPUT
                        self.output = self.output[trim:]
                        self.base += trim
                        metadata_path = TERMINAL_STORE / f"{self.id}.json"
                        metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
                        metadata["base"] = self.base
                        metadata_path.write_text(json.dumps(metadata, ensure_ascii=False), encoding="utf-8")
                        (TERMINAL_STORE / f"{self.id}.log").write_text(self.output, encoding="utf-8")
                    else:
                        with (TERMINAL_STORE / f"{self.id}.log").open("a", encoding="utf-8") as log:
                            log.write(chunk)
                            log.flush()
                            os.fsync(log.fileno())
            except (OSError, ValueError):
                break

    def write(self, value):
        os.write(self.master, value.encode("utf-8"))

    def snapshot(self, since=0):
        with self.lock:
            start = max(0, min(len(self.output), since - self.base))
            return {"output": self.output[start:], "cursor": self.base + len(self.output),
                    "running": self.process.poll() is None, "cwd": str(PROJECT)}

    def close(self):
        if self.process.poll() is None:
            try:
                os.killpg(self.process.pid, signal.SIGTERM)
                self.process.wait(timeout=2)
            except (OSError, subprocess.TimeoutExpired):
                self.process.kill()
        reader = getattr(self, "reader", None)
        if reader and reader is not threading.current_thread():
            reader.join(timeout=1)
        try:
            os.close(self.master)
        except OSError:
            pass


class ArchivedTerminal:
    """Persisted terminal output restored after the shell process has ended."""
    def __init__(self, terminal_id):
        self.id = terminal_id
        metadata = json.loads((TERMINAL_STORE / f"{terminal_id}.json").read_text(encoding="utf-8"))
        self.created_at = metadata.get("created_at", "")
        self.output = (TERMINAL_STORE / f"{terminal_id}.log").read_text(encoding="utf-8")
        self.base = int(metadata.get("base", 0))
        self.lock = threading.RLock()

    def snapshot(self, since=0):
        with self.lock:
            start = max(0, min(len(self.output), since - self.base))
            return {"output": self.output[start:], "cursor": self.base + len(self.output),
                    "running": False, "archived": True, "cwd": str(PROJECT)}

    def write(self, _value):
        raise OSError("เทอร์มินัลเดิมสิ้นสุดแล้ว เปิดเทอร์มินัลใหม่เพื่อรันคำสั่ง")

    def close(self):
        for suffix in (".json", ".log"):
            try:
                (TERMINAL_STORE / f"{self.id}{suffix}").unlink()
            except FileNotFoundError:
                pass


def restore_archived_terminals():
    TERMINAL_STORE.mkdir(parents=True, exist_ok=True)
    with TERMINAL_LOCK:
        for metadata_path in TERMINAL_STORE.glob("*.json"):
            terminal_id = metadata_path.stem
            if not re.fullmatch(r"[a-f0-9]{32}", terminal_id):
                continue
            try:
                TERMINALS[terminal_id] = ArchivedTerminal(terminal_id)
            except (OSError, ValueError, json.JSONDecodeError):
                continue


def codex_defaults():
    config_path = CODEX_HOME / "config.toml"
    try:
        config = tomllib.loads(config_path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError):
        config = {}
    return {
        "model": os.environ.get("CODEX_MODEL", config.get("model", "gpt-6-luna")),
        "effort": config.get("model_reasoning_effort", "low"),
    }


def load_providers():
    try:
        data = json.loads(PROVIDER_STORE.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def save_providers(data):
    CODEX_HOME.mkdir(parents=True, exist_ok=True)
    temp = PROVIDER_STORE.with_suffix(".tmp")
    temp.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    try:
        temp.chmod(0o600)
    except OSError:
        pass
    temp.replace(PROVIDER_STORE)
    try:
        PROVIDER_STORE.chmod(0o600)
    except OSError:
        pass


def stop_process(process):
    if process and process.poll() is None:
        process.terminate()
        try:
            process.wait(timeout=3)
        except subprocess.TimeoutExpired:
            process.kill()


def close_puter_services():
    global PUTER_BRIDGE, PUTER_PROXY, PUTER_PROXY_URL
    with PUTER_LOCK:
        stop_process(PUTER_PROXY)
        stop_process(PUTER_BRIDGE)
        PUTER_PROXY = PUTER_BRIDGE = PUTER_PROXY_URL = None


def startup_line(process, timeout=30):
    if not process.stdout:
        raise RuntimeError("ไม่พบช่องทางรับสถานะบริการ Puter")
    ready, _, _ = select.select([process.stdout], [], [], timeout)
    if not ready:
        raise RuntimeError("บริการ Puter เริ่มทำงานไม่ทันเวลา")
    line = process.stdout.readline().strip()
    if not line or not line.startswith("http://127.0.0.1:"):
        raise RuntimeError("เริ่มบริการ Puter ไม่สำเร็จ")
    return line


def ensure_puter_services():
    global PUTER_BRIDGE, PUTER_PROXY, PUTER_PROXY_URL
    token_file = Path.home() / ".codex" / "puter-auth-token"
    if not token_file.is_file() or not token_file.read_text(encoding="utf-8").strip():
        raise ValueError("ยังไม่ได้เชื่อมบัญชี Puter · รัน python agent.py login ใน Terminal ก่อน")
    with PUTER_LOCK:
        if PUTER_BRIDGE and PUTER_PROXY and PUTER_BRIDGE.poll() is None and PUTER_PROXY.poll() is None and PUTER_PROXY_URL:
            proxy_url = PUTER_PROXY_URL
        else:
            close_puter_services()
            node = shutil.which("node")
            if not node:
                raise ValueError("ไม่พบ Node.js สำหรับเปิด Puter SDK")
            try:
                PUTER_BRIDGE = subprocess.Popen([node, str(PROJECT / "puter-sdk-bridge.cjs")], cwd=PROJECT,
                                                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
                bridge_url = startup_line(PUTER_BRIDGE)
                env = os.environ.copy()
                env["PUTER_SDK_BRIDGE_URL"] = bridge_url
                PUTER_PROXY = subprocess.Popen([sys.executable, str(PROJECT / "puter_responses_proxy.py")], cwd=PROJECT,
                                               env=env, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
                PUTER_PROXY_URL = startup_line(PUTER_PROXY).removesuffix("/v1")
                proxy_url = PUTER_PROXY_URL
            except (OSError, RuntimeError) as error:
                close_puter_services()
                raise ValueError(str(error)) from error
        try:
            with urlopen(proxy_url + "/v1/models", timeout=45) as response:
                catalog = json.loads(response.read())
            rows = catalog.get("models", catalog.get("data", []))
            models = [(item.get("slug") or item.get("id")) for item in rows if isinstance(item, dict) and (item.get("slug") or item.get("id"))]
        except (OSError, ValueError, AttributeError) as error:
            close_puter_services()
            raise ValueError("โหลดรายการโมเดล Puter ไม่สำเร็จ: " + str(error)[:240]) from error
        if not models:
            raise ValueError("Puter ไม่ส่งรายการโมเดลกลับมา")
        return proxy_url + "/v1", models


def persist_provider_config(provider_id, item):
    table = "model_providers." + provider_id
    for key, value in (("name", item["name"]), ("base_url", item["base_url"]), ("env_key", item["env_key"]),
                       ("wire_api", "responses"), ("supports_websockets", False)):
        set_config_value(key, value, table=table)


def safe_provider(provider_id, item):
    return {"id": provider_id, "name": item.get("name", provider_id), "base_url": item.get("base_url", ""),
            "models": item.get("models", []), "has_key": bool(item.get("api_key"))}


def provider_env(provider_id=None):
    env = os.environ.copy()
    provider = load_providers().get(provider_id) if provider_id else None
    if isinstance(provider, dict) and provider.get("kind") == "puter":
        env[provider["env_key"]] = "local-proxy"
    elif isinstance(provider, dict) and provider.get("env_key") and provider.get("api_key"):
        env[provider["env_key"]] = provider["api_key"]
    return env


def add_provider(data):
    provider_id = data.get("id", "")
    name = data.get("name", "")
    base_url = data.get("base_url", "")
    model_text = data.get("models", "")
    api_key = data.get("api_key", "")
    if not all(isinstance(value, str) for value in (provider_id, name, base_url, model_text, api_key)):
        raise ValueError("ข้อมูล provider ต้องเป็นข้อความ")
    name = name.strip()
    base_url = base_url.strip().rstrip("/")
    if not re.fullmatch(r"[a-z][a-z0-9_-]{0,40}", provider_id):
        raise ValueError("รหัส provider ใช้ a-z, 0-9, _ และ - โดยขึ้นต้นด้วยตัวอักษร")
    if provider_id in {"openai", "ollama", "lmstudio", "amazon-bedrock"}:
        raise ValueError("รหัสนี้สงวนไว้สำหรับ provider ที่ติดตั้งมากับ Codex")
    if provider_id == "puter":
        base_url, models = ensure_puter_services()
        env_key = "PUTER_PROXY_API_KEY"
        entry = {"name": "Puter", "base_url": base_url, "models": models, "api_key": "local-proxy", "env_key": env_key, "kind": "puter"}
        existing = load_providers()
        existing[provider_id] = entry
        save_providers(existing)
        persist_provider_config(provider_id, entry)
        return safe_provider(provider_id, entry)
    parsed = urlsplit(base_url)
    if parsed.scheme != "https" or not parsed.netloc or parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ValueError("Base URL ต้องเป็น HTTPS และไม่มีข้อมูลล็อกอินฝังใน URL")
    if not isinstance(name, str) or not name or len(name) > 80:
        raise ValueError("กรุณาระบุชื่อ provider")
    if not isinstance(api_key, str) or len(api_key) > 4096:
        raise ValueError("API key ยาวเกินกำหนด")
    old = load_providers()
    if not api_key.strip() and provider_id in old:
        api_key = old[provider_id].get("api_key", "")
    if not isinstance(api_key, str) or not api_key.strip():
        raise ValueError("กรุณาระบุ API key")
    if not isinstance(model_text, str):
        raise ValueError("รายชื่อโมเดลไม่ถูกต้อง")
    models = list(dict.fromkeys(x.strip() for x in model_text.split(",") if x.strip()))
    if not models or any(len(x) > 120 or "::" in x for x in models):
        raise ValueError("ใส่ชื่อโมเดลอย่างน้อยหนึ่งรายการ คั่นด้วยจุลภาค")
    env_key = "CODEX_WEB_" + re.sub(r"[^A-Z0-9]", "_", provider_id.upper()) + "_API_KEY"
    entry = {"name": name, "base_url": base_url, "models": models, "api_key": api_key.strip(), "env_key": env_key}
    old[provider_id] = entry
    save_providers(old)
    persist_provider_config(provider_id, entry)
    return safe_provider(provider_id, entry)


def openrouter_models():
    global OPENROUTER_MODELS, OPENROUTER_MODELS_TIME
    if OPENROUTER_MODELS and time.time() - OPENROUTER_MODELS_TIME < 600:
        return OPENROUTER_MODELS
    headers = {"Accept": "application/json", "User-Agent": "Codex-Agent-WebUI"}
    saved = load_providers().get("openrouter", {})
    if isinstance(saved, dict) and saved.get("api_key"):
        headers["Authorization"] = "Bearer " + saved["api_key"]
    request = Request("https://openrouter.ai/api/v1/models?output_modalities=text", headers=headers)
    with urlopen(request, timeout=20) as response:
        payload = json.loads(response.read())
    rows = payload.get("data", []) if isinstance(payload, dict) else []
    models = [{"id": item["id"], "name": item.get("name") or item["id"]}
              for item in rows if isinstance(item, dict) and isinstance(item.get("id"), str)]
    if not models:
        raise ValueError("OpenRouter ไม่ส่งรายการโมเดลกลับมา")
    OPENROUTER_MODELS = models
    OPENROUTER_MODELS_TIME = time.time()
    return models


def run_codex(args, timeout=30):
    codex = shutil.which("codex")
    if not codex:
        return None, "Codex CLI ไม่อยู่ใน PATH"
    try:
        result = subprocess.run([codex, *args], cwd=PROJECT, text=True, capture_output=True, timeout=timeout, check=False)
    except subprocess.TimeoutExpired:
        return None, "คำสั่ง Codex ใช้เวลานานเกินกำหนด"
    except OSError as error:
        return None, str(error)
    if result.returncode:
        detail = (result.stderr or result.stdout).strip()
        return None, (detail[-800:] or f"Codex จบการทำงานด้วยรหัส {result.returncode}")
    return result.stdout, None


def codex_json(args):
    output, error = run_codex(args)
    if error:
        return None, error
    try:
        return json.loads(output), None
    except (ValueError, TypeError):
        return None, "Codex ส่งข้อมูลกลับมาในรูปแบบที่อ่านไม่ได้"


def integration_snapshot():
    mcp_data, mcp_error = codex_json(["mcp", "list", "--json"])
    mcp_raw = mcp_data if isinstance(mcp_data, list) else (mcp_data.get("servers", []) if isinstance(mcp_data, dict) else [])
    mcp_servers = []
    for item in mcp_raw:
        if not isinstance(item, dict):
            continue
        name = item.get("name")
        if not isinstance(name, str):
            continue
        endpoint = item.get("url")
        host = urlsplit(endpoint).hostname if isinstance(endpoint, str) else None
        command = item.get("command")
        mcp_servers.append({
            "name": name,
            "transport": "remote" if endpoint else "local",
            "endpoint": host or (Path(command).name if isinstance(command, str) else "MCP server"),
            "enabled": item.get("enabled", True) is not False and mcp_enabled(name),
            "auth": "OAuth/token" if item.get("auth") or item.get("bearer_token_env_var") or item.get("oauth") else "ตามที่เซิร์ฟเวอร์กำหนด",
        })
    plugin_data, plugin_error = codex_json(["plugin", "list", "--json"])
    plugin_raw = plugin_data.get("installed", []) if isinstance(plugin_data, dict) else []
    plugins = []
    for item in plugin_raw:
        if isinstance(item, dict) and isinstance(item.get("pluginId"), str):
            plugin = {key: item.get(key) for key in ("pluginId", "name", "marketplaceName", "version", "enabled")}
            plugin["enabled"] = bool(plugin.get("enabled")) and plugin_enabled(plugin["pluginId"])
            plugins.append(plugin)
    marketplace_data, marketplace_error = codex_json(["plugin", "marketplace", "list", "--json"])
    marketplace_raw = marketplace_data.get("marketplaces", []) if isinstance(marketplace_data, dict) else []
    marketplaces = []
    for item in marketplace_raw:
        if isinstance(item, dict):
            name = item.get("name") or item.get("marketplaceName")
            if isinstance(name, str):
                marketplaces.append({"name": name, "source": item.get("source") or item.get("url") or ""})
    return {"mcp_servers": mcp_servers, "plugins": plugins, "marketplaces": marketplaces,
            "errors": [e for e in (mcp_error, plugin_error, marketplace_error) if e]}


def mcp_enabled(name):
    servers = load_config().get("mcp_servers", {})
    value = servers.get(name, {}) if isinstance(servers, dict) else {}
    return value.get("enabled", True) is not False if isinstance(value, dict) else True


def valid_integration_name(value):
    import re
    return isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}", value) is not None


def valid_mcp_url(value):
    if not isinstance(value, str) or len(value) > 2048:
        return False
    parsed = urlsplit(value)
    if parsed.scheme == "https":
        return bool(parsed.hostname) and not parsed.username and not parsed.password and not parsed.fragment
    return parsed.scheme == "http" and parsed.hostname in ("127.0.0.1", "localhost", "::1") and not parsed.username and not parsed.password and not parsed.fragment


def valid_marketplace_source(value):
    import re
    if not isinstance(value, str) or len(value) > 500:
        return False
    if re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+(?:@[A-Za-z0-9_.-]+)?", value):
        return True
    parsed = urlsplit(value)
    return parsed.scheme == "https" and parsed.hostname in ("github.com", "gitlab.com") and not parsed.username and not parsed.password and not parsed.fragment


def start_mcp_login(name):
    task_id = uuid.uuid4().hex
    with INTEGRATION_TASK_LOCK:
        INTEGRATION_TASKS[task_id] = {"status": "running", "message": "กำลังเปิดขั้นตอน OAuth"}

    def worker():
        codex = shutil.which("codex")
        if not codex:
            state = {"status": "error", "message": "Codex CLI ไม่อยู่ใน PATH"}
        else:
            try:
                process = subprocess.Popen([codex, "mcp", "login", name], cwd=PROJECT, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
                code = process.wait(timeout=300)
                state = {"status": "connected" if code == 0 else "error", "message": "เชื่อมต่อสำเร็จ" if code == 0 else "การยืนยัน OAuth ไม่สำเร็จ"}
            except subprocess.TimeoutExpired:
                process.kill()
                state = {"status": "error", "message": "หมดเวลารอ OAuth"}
            except OSError as error:
                state = {"status": "error", "message": str(error)[:300]}
        with INTEGRATION_TASK_LOCK:
            INTEGRATION_TASKS[task_id] = state

    threading.Thread(target=worker, daemon=True).start()
    return task_id


def load_config():
    try:
        return tomllib.loads((CODEX_HOME / "config.toml").read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError):
        return {}


def toml_value(value):
    if isinstance(value, bool):
        return "true" if value else "false"
    return json.dumps(value, ensure_ascii=False)


def set_config_value(key, value, table=None):
    """Set one known config value while preserving unrelated TOML/comments."""
    path = CODEX_HOME / "config.toml"
    CODEX_HOME.mkdir(parents=True, exist_ok=True)
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        lines = []
    encoded = toml_value(value)
    if table is None:
        pattern = re.compile(r"^\s*" + re.escape(key) + r"\s*=")
        end = next((i for i, line in enumerate(lines) if re.match(r"^\s*\[", line)), len(lines))
        found = next((i for i, line in enumerate(lines[:end]) if pattern.match(line)), None)
        if found is not None:
            lines[found] = f"{key} = {encoded}"
        else:
            lines.insert(end, f"{key} = {encoded}")
    else:
        header = f"[{table}]"
        start = next((i for i, line in enumerate(lines) if line.strip() == header), None)
        if start is None:
            if lines and lines[-1].strip():
                lines.append("")
            lines.extend((header, f"{key} = {encoded}"))
        else:
            end = next((i for i in range(start + 1, len(lines)) if re.match(r"^\s*\[", lines[i])), len(lines))
            pattern = re.compile(r"^\s*" + re.escape(key) + r"\s*=")
            found = next((i for i in range(start + 1, end) if pattern.match(lines[i])), None)
            if found is not None:
                lines[found] = f"{key} = {encoded}"
            else:
                lines.insert(end, f"{key} = {encoded}")
    temp = path.with_suffix(".toml.tmp")
    temp.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")
    temp.replace(path)


def settings_snapshot():
    config = load_config()
    features, error = run_codex(["features", "list"])
    rows = []
    if features:
        for line in features.splitlines():
            match = re.match(r"^\s*([a-zA-Z0-9_.-]+)\s+(stable|experimental|under development|removed|deprecated)\s+(true|false)\s*$", line)
            if match:
                rows.append({"name": match.group(1), "stage": match.group(2), "enabled": match.group(3) == "true"})
    profiles = sorted(p.name.removesuffix(".config.toml") for p in CODEX_HOME.glob("*.config.toml") if p.is_file())
    approval = config.get("approval_policy", "on-request")
    if not isinstance(approval, str):
        approval = "custom"
    sandbox_config = config.get("sandbox_workspace_write", {})
    if not isinstance(sandbox_config, dict):
        sandbox_config = {}
    configured_model = config.get("model", codex_defaults()["model"])
    configured_provider = config.get("model_provider", "openai")
    if configured_provider in load_providers():
        configured_model = configured_provider + "::" + configured_model
    return {"model": configured_model,
            "effort": config.get("model_reasoning_effort", codex_defaults()["effort"]),
            "approval_policy": approval,
            "sandbox_mode": config.get("sandbox_mode", "workspace-write"),
            "web_search": config.get("web_search", "disabled"),
            "personality": config.get("personality", "none"),
            "network_access": sandbox_config.get("network_access", False),
            "analytics_enabled": config.get("analytics", {}).get("enabled", True) if isinstance(config.get("analytics", {}), dict) else True,
            "profiles": profiles, "features": rows, "feature_error": error}


def available_plugins(query=""):
    global PLUGIN_CATALOG, PLUGIN_CATALOG_TIME
    now = time.time()
    if PLUGIN_CATALOG is None or now - PLUGIN_CATALOG_TIME > 300:
        data, error = codex_json(["plugin", "list", "--available", "--json"])
        if error:
            return [], error
        PLUGIN_CATALOG = data.get("available", []) if isinstance(data, dict) else []
        PLUGIN_CATALOG_TIME = now
    query = query.strip().casefold()
    entries = []
    for item in PLUGIN_CATALOG or []:
        if not isinstance(item, dict):
            continue
        name = item.get("name") or ""
        marketplace = item.get("marketplaceName") or ""
        title = f"{name}@{marketplace}"
        description = item.get("description") or ""
        if query and query not in title.casefold() and query not in description.casefold():
            continue
        entries.append({"pluginId": item.get("pluginId", title), "name": name, "marketplaceName": marketplace,
                        "version": item.get("version", ""), "description": description,
                        "installPolicy": item.get("installPolicy", "AVAILABLE"), "enabled": bool(item.get("enabled"))})
        if len(entries) >= 60:
            break
    return entries, None


def plugin_enabled(plugin_id):
    raw = load_config().get("plugins", {})
    value = raw.get(plugin_id, {}) if isinstance(raw, dict) else {}
    return value.get("enabled", True) is not False if isinstance(value, dict) else True

def timestamp():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def load_sessions():
    try:
        raw = json.loads(SESSION_FILE.read_text(encoding="utf-8"))
        return raw if isinstance(raw, list) else []
    except (OSError, json.JSONDecodeError):
        return []


def save_sessions(sessions):
    SESSION_FILE.parent.mkdir(parents=True, exist_ok=True)
    temp = SESSION_FILE.with_suffix(".tmp")
    temp.write_text(json.dumps(sessions, ensure_ascii=False, indent=2), encoding="utf-8")
    temp.replace(SESSION_FILE)


def clean_session(value):
    return {"id": value["id"], "title": value.get("title", "แชตใหม่"), "created_at": value.get("created_at"), "updated_at": value.get("updated_at"), "messages": value.get("messages", []), "codex_thread_id": value.get("codex_thread_id"), "mode": value.get("mode", "codex-default")}


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    def do_GET(self):
        path = self.path.split("?", 1)[0]
        if path in ("/", "/index.html"):
            try:
                data = WEB_FILE.read_bytes()
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Cache-Control", "no-store")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)
            except OSError as error:
                self.send_json({"error": str(error)}, 500)
        elif path == "/api/settings":
            self.send_json(settings_snapshot())
        elif path == "/api/providers":
            self.send_json({"providers": [safe_provider(k, v) for k, v in load_providers().items() if isinstance(v, dict)]})
        elif path == "/api/openrouter/models":
            try:
                self.send_json({"models": openrouter_models()})
            except (OSError, ValueError, json.JSONDecodeError) as error:
                self.send_json({"error": "โหลดโมเดลจาก OpenRouter ไม่สำเร็จ: " + str(error)[:240]}, 502)
        elif path == "/api/integrations/catalog":
            query = self.parse_query().get("q", [""])[0]
            entries, error = available_plugins(query)
            self.send_json({"plugins": entries, "error": error})
        elif path == "/api/integrations":
            self.send_json(integration_snapshot())
        elif path.startswith("/api/integrations/tasks/"):
            task_id = path.removeprefix("/api/integrations/tasks/").strip("/")
            with INTEGRATION_TASK_LOCK:
                task = INTEGRATION_TASKS.get(task_id)
            self.send_json(task or {"status": "error", "message": "ไม่พบงานเชื่อมต่อ"}, 200 if task else 404)
        elif path == "/api/sessions":
            with LOCK:
                sessions = sorted(load_sessions(), key=lambda item: item.get("updated_at", ""), reverse=True)
            self.send_json({"sessions": [clean_session(item) for item in sessions]})
        elif path == "/api/models":
            self.send_json(self.models_snapshot())
        elif path == "/api/terminal":
            with TERMINAL_LOCK:
                ids = list(TERMINALS)
            self.send_json({"terminals": [{"id": terminal_id} for terminal_id in ids]})
        elif path.startswith("/api/terminal/"):
            terminal_id = path.removeprefix("/api/terminal/").strip("/")
            with TERMINAL_LOCK:
                terminal = TERMINALS.get(terminal_id)
            if not terminal:
                self.send_json({"error": "ไม่พบเทอร์มินัล"}, 404)
            else:
                try:
                    since = int(self.parse_query().get("since", ["0"])[0])
                except ValueError:
                    since = 0
                self.send_json(terminal.snapshot(since))
        elif path.startswith("/api/sessions/"):
            session_id = path.removeprefix("/api/sessions/").strip("/")
            with LOCK:
                session = next((item for item in load_sessions() if item.get("id") == session_id), None)
            if session:
                self.send_json({"session": clean_session(session)})
            else:
                self.send_json({"error": "ไม่พบเซสชัน"}, 404)
        else:
            self.send_error(404)

    def do_POST(self):
        path = self.path.split("?", 1)[0]
        try:
            body = self.read_json()
            if path == "/api/sessions":
                session = {"id": uuid.uuid4().hex, "title": "แชตใหม่", "created_at": timestamp(), "updated_at": timestamp(), "messages": [], "codex_thread_id": None, "mode": "codex-default"}
                with LOCK:
                    sessions = load_sessions()
                    sessions.insert(0, session)
                    save_sessions(sessions)
                self.send_json({"session": clean_session(session)}, 201)
            elif path == "/api/providers":
                self.send_json({"provider": add_provider(body)}, 201)
            elif path == "/api/integrations/mcp":
                name, url = body.get("name", ""), body.get("url", "")
                if not valid_integration_name(name) or not valid_mcp_url(url):
                    self.send_json({"error": "ชื่อหรือ URL ของ MCP server ไม่ถูกต้อง (ใช้ HTTPS; HTTP ได้เฉพาะ localhost)"}, 400)
                    return
                _, error = run_codex(["mcp", "add", name, "--url", url], timeout=90)
                if error:
                    self.send_json({"error": error}, 400)
                else:
                    self.send_json({"ok": True, "integrations": integration_snapshot()}, 201)
            elif path.startswith("/api/integrations/mcp/") and path.endswith("/login"):
                name = unquote(path.removeprefix("/api/integrations/mcp/").removesuffix("/login").strip("/"))
                if not valid_integration_name(name) or not any(x["name"] == name for x in integration_snapshot()["mcp_servers"]):
                    self.send_json({"error": "ไม่พบ MCP server นี้"}, 404)
                    return
                task_id = start_mcp_login(name)
                self.send_json({"task_id": task_id, "status": "running"}, 202)
            elif path == "/api/integrations/plugins":
                selector = body.get("plugin", "")
                import re
                if not isinstance(selector, str) or not re.fullmatch(r"[A-Za-z0-9_.-]+@[A-Za-z0-9_.-]+", selector):
                    self.send_json({"error": "รูปแบบปลั๊กอินต้องเป็นชื่อ@แหล่งที่มา"}, 400)
                    return
                _, error = run_codex(["plugin", "add", selector, "--json"], timeout=300)
                if error:
                    self.send_json({"error": error}, 502)
                else:
                    self.send_json({"ok": True, "integrations": integration_snapshot()})
            elif path == "/api/integrations/marketplaces/refresh":
                _, error = run_codex(["plugin", "marketplace", "upgrade"], timeout=300)
                if error:
                    self.send_json({"error": error}, 502)
                else:
                    global PLUGIN_CATALOG, PLUGIN_CATALOG_TIME
                    PLUGIN_CATALOG = None
                    PLUGIN_CATALOG_TIME = 0
                    self.send_json({"ok": True, "integrations": integration_snapshot()})
            elif path == "/api/integrations/marketplaces":
                source = body.get("source", "")
                if not valid_marketplace_source(source):
                    self.send_json({"error": "ใส่ owner/repo หรือ GitHub/GitLab HTTPS URL"}, 400)
                    return
                _, error = run_codex(["plugin", "marketplace", "add", source], timeout=300)
                if error:
                    self.send_json({"error": error}, 502)
                else:
                    self.send_json({"ok": True, "integrations": integration_snapshot()})
            elif path in ("/api/chat", "/api/chat/stream"):
                self.chat(body, stream=path.endswith("/stream"))
            elif path == "/api/terminal":
                terminal_id = uuid.uuid4().hex
                try:
                    terminal = BackgroundTerminal(terminal_id)
                except OSError as error:
                    self.send_json({"error": f"เปิดเทอร์มินัลไม่ได้: {error}"}, 500)
                    return
                with TERMINAL_LOCK:
                    TERMINALS[terminal_id] = terminal
                self.send_json({"id": terminal_id, **terminal.snapshot()}, 201)
            elif path.startswith("/api/terminal/") and path.endswith("/input"):
                terminal_id = path.removeprefix("/api/terminal/").removesuffix("/input").strip("/")
                with TERMINAL_LOCK:
                    terminal = TERMINALS.get(terminal_id)
                value = body.get("input")
                if not terminal:
                    self.send_json({"error": "ไม่พบเทอร์มินัล"}, 404)
                elif not isinstance(value, str) or len(value) > 20_000:
                    self.send_json({"error": "ข้อมูลคำสั่งไม่ถูกต้อง"}, 400)
                else:
                    try:
                        terminal.write(value)
                        self.send_json({"ok": True})
                    except OSError as error:
                        self.send_json({"error": str(error)}, 500)
            elif path.startswith("/api/terminal/") and path.endswith("/close"):
                terminal_id = path.removeprefix("/api/terminal/").removesuffix("/close").strip("/")
                with TERMINAL_LOCK:
                    terminal = TERMINALS.pop(terminal_id, None)
                if terminal:
                    terminal.close()
                self.send_json({"ok": True})
            elif path.startswith("/api/sessions/") and path.endswith("/clear"):
                session_id = path.removeprefix("/api/sessions/").removesuffix("/clear").strip("/")
                with LOCK:
                    sessions = load_sessions()
                    session = next((item for item in sessions if item.get("id") == session_id), None)
                    if not session:
                        self.send_json({"error": "ไม่พบเซสชัน"}, 404)
                        return
                    session["messages"] = []
                    session["codex_thread_id"] = None
                    session["title"] = "แชตใหม่"
                    session["updated_at"] = timestamp()
                    save_sessions(sessions)
                self.send_json({"session": clean_session(session)})
            else:
                self.send_error(404)
        except (ValueError, json.JSONDecodeError) as error:
            self.send_json({"error": str(error)}, 400)

    def do_PATCH(self):
        path = self.path.split("?", 1)[0]
        if path == "/api/settings":
            try:
                body = self.read_json()
            except (ValueError, json.JSONDecodeError) as error:
                self.send_json({"error": str(error)}, 400)
                return
            choices = {
                "model": ("model", set(x["id"] for x in self.models_snapshot()["models"])),
                "effort": ("model_reasoning_effort", {"minimal", "low", "medium", "high", "xhigh", "max", "ultra"}),
                "approval_policy": ("approval_policy", {"on-request", "never"}),
                "sandbox_mode": ("sandbox_mode", {"read-only", "workspace-write", "danger-full-access"}),
                "web_search": ("web_search", {"disabled", "cached", "indexed", "live"}),
                "personality": ("personality", {"none", "friendly", "pragmatic"}),
            }
            try:
                for field, (key, allowed) in choices.items():
                    if field in body:
                        selected = body[field]
                        if selected not in allowed:
                            self.send_json({"error": f"ค่า {field} ไม่รองรับ"}, 400)
                            return
                        if field == "model" and "::" in selected:
                            provider_id, selected = selected.split("::", 1)
                            if provider_id not in load_providers():
                                self.send_json({"error": "ไม่พบ provider ที่เลือก"}, 400)
                                return
                            set_config_value("model_provider", provider_id)
                        elif field == "model":
                            set_config_value("model_provider", "openai")
                        set_config_value(key, selected)
                if "network_access" in body and isinstance(body["network_access"], bool):
                    set_config_value("network_access", body["network_access"], table="sandbox_workspace_write")
                if "analytics_enabled" in body and isinstance(body["analytics_enabled"], bool):
                    set_config_value("enabled", body["analytics_enabled"], table="analytics")
            except (OSError, ValueError) as error:
                self.send_json({"error": str(error)}, 500)
                return
            self.send_json({"settings": settings_snapshot()})
            return
        if path.startswith("/api/integrations/plugins/") and path.endswith("/enabled"):
            plugin_id = unquote(path.removeprefix("/api/integrations/plugins/").removesuffix("/enabled").strip("/"))
            try:
                body = self.read_json()
            except (ValueError, json.JSONDecodeError) as error:
                self.send_json({"error": str(error)}, 400)
                return
            if not re.fullmatch(r"[A-Za-z0-9_.-]+@[A-Za-z0-9_.-]+", plugin_id) or not isinstance(body.get("enabled"), bool):
                self.send_json({"error": "ข้อมูลปลั๊กอินไม่ถูกต้อง"}, 400)
                return
            installed, error = codex_json(["plugin", "list", "--json"])
            if error or not any(p.get("pluginId") == plugin_id for p in installed.get("installed", [])):
                self.send_json({"error": error or "ปลั๊กอินนี้ยังไม่ได้ติดตั้ง"}, 404)
                return
            name = plugin_id.replace('"', '')
            set_config_value("enabled", body["enabled"], table=f'plugins."{name}"')
            self.send_json({"ok": True, "integrations": integration_snapshot()})
            return
        if path.startswith("/api/integrations/mcp/") and path.endswith("/enabled"):
            name = unquote(path.removeprefix("/api/integrations/mcp/").removesuffix("/enabled").strip("/"))
            try:
                body = self.read_json()
            except (ValueError, json.JSONDecodeError) as error:
                self.send_json({"error": str(error)}, 400)
                return
            if not valid_integration_name(name) or not isinstance(body.get("enabled"), bool) or not any(x["name"] == name for x in integration_snapshot()["mcp_servers"]):
                self.send_json({"error": "ข้อมูล MCP server ไม่ถูกต้อง"}, 400)
                return
            set_config_value("enabled", body["enabled"], table=f'mcp_servers."{name}"')
            self.send_json({"ok": True, "integrations": integration_snapshot()})
            return
        if path.startswith("/api/features/"):
            feature = unquote(path.removeprefix("/api/features/").strip("/"))
            try:
                body = self.read_json()
            except (ValueError, json.JSONDecodeError) as error:
                self.send_json({"error": str(error)}, 400)
                return
            known = {item["name"]: item for item in settings_snapshot()["features"]}
            if feature not in known or known[feature]["stage"] in ("removed", "deprecated") or not isinstance(body.get("enabled"), bool):
                self.send_json({"error": "feature นี้ไม่สามารถเปลี่ยนได้"}, 400)
                return
            _, error = run_codex(["features", "enable" if body["enabled"] else "disable", feature])
            if error:
                self.send_json({"error": error}, 400)
                return
            self.send_json({"settings": settings_snapshot()})
            return
        if not path.startswith("/api/sessions/"):
            self.send_error(404)
            return
        session_id = path.removeprefix("/api/sessions/").strip("/")
        try:
            body = self.read_json()
        except (ValueError, json.JSONDecodeError) as error:
            self.send_json({"error": str(error)}, 400)
            return
        with LOCK:
            sessions = load_sessions()
            session = next((item for item in sessions if item.get("id") == session_id), None)
            if not session:
                self.send_json({"error": "ไม่พบเซสชัน"}, 404)
                return
            if isinstance(body.get("title"), str):
                session["title"] = body["title"].strip()[:80] or "แชตใหม่"
            if body.get("mode") in ("codex-default", "read-only", "workspace-write", "approve", "danger-full-access"):
                session["mode"] = body["mode"]
            session["updated_at"] = timestamp()
            save_sessions(sessions)
        self.send_json({"session": clean_session(session)})

    def do_DELETE(self):
        path = self.path.split("?", 1)[0]
        if path.startswith("/api/providers/"):
            provider_id = unquote(path.removeprefix("/api/providers/").strip("/"))
            if not re.fullmatch(r"[a-z][a-z0-9_-]{0,40}", provider_id):
                self.send_json({"error": "รหัส provider ไม่ถูกต้อง"}, 400)
                return
            providers = load_providers()
            if provider_id not in providers:
                self.send_json({"error": "ไม่พบ provider นี้"}, 404)
                return
            providers.pop(provider_id)
            save_providers(providers)
            if load_config().get("model_provider") == provider_id:
                set_config_value("model_provider", "openai")
            if provider_id == "puter":
                close_puter_services()
            self.send_json({"ok": True, "providers": [safe_provider(k, v) for k, v in providers.items()]})
            return
        if path.startswith("/api/integrations/mcp/"):
            name = unquote(path.removeprefix("/api/integrations/mcp/").strip("/"))
            if not valid_integration_name(name):
                self.send_json({"error": "ชื่อ MCP server ไม่ถูกต้อง"}, 400)
                return
            _, error = run_codex(["mcp", "remove", name])
            self.send_json({"error": error} if error else {"ok": True, "integrations": integration_snapshot()}, 400 if error else 200)
            return
        if path.startswith("/api/integrations/plugins/"):
            selector = unquote(path.removeprefix("/api/integrations/plugins/").strip("/"))
            import re
            if not re.fullmatch(r"[A-Za-z0-9_.-]+@[A-Za-z0-9_.-]+", selector):
                self.send_json({"error": "ชื่อปลั๊กอินไม่ถูกต้อง"}, 400)
                return
            _, error = run_codex(["plugin", "remove", selector, "--json"], timeout=180)
            self.send_json({"error": error} if error else {"ok": True, "integrations": integration_snapshot()}, 400 if error else 200)
            return
        if path.startswith("/api/integrations/marketplaces/"):
            name = unquote(path.removeprefix("/api/integrations/marketplaces/").strip("/"))
            if not valid_integration_name(name):
                self.send_json({"error": "ชื่อ marketplace ไม่ถูกต้อง"}, 400)
                return
            _, error = run_codex(["plugin", "marketplace", "remove", name])
            self.send_json({"error": error} if error else {"ok": True, "integrations": integration_snapshot()}, 400 if error else 200)
            return
        if not path.startswith("/api/sessions/"):
            self.send_error(404)
            return
        session_id = path.removeprefix("/api/sessions/").strip("/")
        with LOCK:
            sessions = load_sessions()
            updated = [item for item in sessions if item.get("id") != session_id]
            if len(updated) == len(sessions):
                self.send_json({"error": "ไม่พบเซสชัน"}, 404)
                return
            save_sessions(updated)
        self.send_json({"ok": True})

    def models_snapshot(self):
        defaults = codex_defaults()
        cache_path = CODEX_HOME / "models_cache.json"
        try:
            cache = json.loads(cache_path.read_text(encoding="utf-8"))
            models = [{"id": item["slug"], "name": item.get("display_name", item["slug"])} for item in cache.get("models", []) if isinstance(item, dict) and item.get("slug") and item.get("slug") != "codex-auto-review"]
        except (OSError, json.JSONDecodeError, AttributeError):
            models = []
        if not any(item["id"] == defaults["model"] for item in models):
            models.insert(0, {"id": defaults["model"], "name": "Codex · " + defaults["model"]})
        providers = load_providers()
        for provider_id, provider in providers.items():
            if isinstance(provider, dict):
                for model in provider.get("models", []):
                    models.append({"id": provider_id + "::" + model, "name": provider.get("name", provider_id) + " · " + model})
        default_provider = load_config().get("model_provider", "openai")
        default = defaults["model"]
        if default_provider in providers:
            default = default_provider + "::" + default
        return {"models": models, "default": default, "effort": defaults["effort"]}

    def parse_query(self):
        from urllib.parse import parse_qs, urlsplit
        return parse_qs(urlsplit(self.path).query)

    def read_json(self):
        length = int(self.headers.get("Content-Length", "0"))
        if length > 1_500_000:
            raise ValueError("คำขอใหญ่เกินไป")
        data = json.loads(self.rfile.read(length) or b"{}")
        if not isinstance(data, dict):
            raise ValueError("ข้อมูลต้องเป็น JSON object")
        return data

    def chat(self, body, stream=False):
        session_id = body.get("session_id")
        message = body.get("message", "")
        if not isinstance(session_id, str) or not isinstance(message, str) or not message.strip():
            self.send_json({"error": "ข้อมูลเซสชันหรือข้อความไม่ถูกต้อง"}, 400)
            return
        with LOCK:
            sessions = load_sessions()
            session = next((item for item in sessions if item.get("id") == session_id), None)
            if not session:
                self.send_json({"error": "ไม่พบเซสชัน กรุณาเริ่มเซสชันใหม่"}, 404)
                return
            thread_id = session.get("codex_thread_id")
        codex = shutil.which("codex")
        if not codex:
            self.send_json({"error": "ไม่พบ Codex CLI ใน PATH"}, 500)
            return
        defaults = codex_defaults()
        model = body.get("model") if isinstance(body.get("model"), str) else defaults["model"]
        provider_id = None
        if "::" in model:
            provider_id, model = model.split("::", 1)
            if provider_id not in load_providers():
                self.send_json({"error": "ไม่พบ provider ที่เลือก"}, 400)
                return
        mode = body.get("mode", "workspace-write")
        config = load_config()
        default_sandbox = config.get("sandbox_mode", "workspace-write")
        if default_sandbox not in ("read-only", "workspace-write", "danger-full-access"):
            default_sandbox = "workspace-write"
        default_approval = config.get("approval_policy", "on-request")
        if default_approval not in ("on-request", "never"):
            default_approval = "on-request"
        mode_options = {
            "codex-default": (default_sandbox, ["--ask-for-approval", default_approval]),
            "read-only": ("read-only", ["--ask-for-approval", default_approval]),
            "workspace-write": ("workspace-write", ["--ask-for-approval", default_approval]),
            "approve": ("workspace-write", ["--approve-for-me"]),
            "danger-full-access": ("danger-full-access", ["--ask-for-approval", "never"]),
        }
        if mode not in mode_options:
            mode = "codex-default"
        sandbox, approval_args = mode_options[mode]
        allowed_effort = {"minimal", "low", "medium", "high", "xhigh"}
        effort = body.get("effort", defaults["effort"])
        if effort not in allowed_effort:
            effort = defaults["effort"]
        with tempfile.TemporaryDirectory(prefix="web-chat-", dir=CODEX_HOME) as temp_dir:
            answer_path = Path(temp_dir) / "last-message.txt"
            common = ["--sandbox", sandbox, *approval_args]
            if provider_id:
                common.extend(("--config", f'model_provider="{provider_id}"'))
            profile = body.get("profile")
            available_profiles = {p.name.removesuffix(".config.toml") for p in CODEX_HOME.glob("*.config.toml")}
            if isinstance(profile, str) and profile in available_profiles:
                common.extend(("--profile", profile))
            if config.get("web_search") == "live":
                common.append("--search")
            common.append("exec")
            if thread_id:
                command = [codex, *common, "resume", thread_id, message, "--json", "--skip-git-repo-check", "--model", model, "--config", f'model_reasoning_effort="{effort}"', "--output-last-message", str(answer_path)]
            else:
                command = [codex, *common, "--json", "--cd", str(PROJECT), "--skip-git-repo-check", "--model", model, "--config", f'model_reasoning_effort="{effort}"', "--output-last-message", str(answer_path), message]
            try:
                process = subprocess.Popen(command, cwd=PROJECT, env=provider_env(provider_id), text=True, encoding="utf-8", errors="replace", stdout=subprocess.PIPE, stderr=subprocess.PIPE, bufsize=1)
            except OSError as error:
                self.send_json({"error": f"เปิด Codex ไม่สำเร็จ: {error}"}, 502)
                return
            connected = True
            streamed_text = ""
            event_error = None
            new_thread_id = thread_id

            def emit(name, value):
                nonlocal connected
                if not stream or not connected:
                    return
                payload = (f"event: {name}\ndata: {json.dumps(value, ensure_ascii=False)}\n\n").encode("utf-8")
                try:
                    self.wfile.write(f"{len(payload):X}\r\n".encode("ascii") + payload + b"\r\n")
                    self.wfile.flush()
                except (BrokenPipeError, ConnectionResetError, OSError):
                    connected = False

            if stream:
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream; charset=utf-8")
                self.send_header("Cache-Control", "no-cache, no-transform")
                self.send_header("Connection", "close")
                self.send_header("Transfer-Encoding", "chunked")
                self.end_headers()
                emit("status", {"message": "เชื่อมต่อ Codex แล้ว · กำลังคิดคำตอบ…"})

            try:
                assert process.stdout is not None
                for line in process.stdout:
                    try:
                        event = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    kind = str(event.get("type", ""))
                    if kind in ("thread.started", "thread_start"):
                        new_thread_id = event.get("thread_id") or event.get("thread", {}).get("id") or new_thread_id
                        with LOCK:
                            stored = load_sessions()
                            target = next((item for item in stored if item.get("id") == session_id), None)
                            if target and target.get("codex_thread_id") == thread_id:
                                target["codex_thread_id"] = new_thread_id
                                save_sessions(stored)
                        emit("thread", {"id": new_thread_id})
                    elif kind == "error":
                        event_error = event.get("message") or event.get("error") or "Codex ทำงานไม่สำเร็จ"
                    elif "delta" in kind.lower():
                        delta = event.get("delta")
                        if not isinstance(delta, str):
                            item = event.get("item", {})
                            delta = item.get("delta") if isinstance(item, dict) else None
                        if isinstance(delta, str) and delta:
                            streamed_text += delta
                            emit("delta", {"text": delta})
                    elif kind in ("item.started", "item.updated"):
                        item = event.get("item", {})
                        item_type = item.get("type", "") if isinstance(item, dict) else ""
                        if item_type in ("command_execution", "shell", "tool_call"):
                            emit("status", {"message": "Codex กำลังใช้เครื่องมือในโปรเจกต์…"})
                    elif kind == "item.completed":
                        item = event.get("item", {})
                        if isinstance(item, dict) and item.get("type") in ("agent_message", "assistant_message"):
                            text = item.get("text", "")
                            if isinstance(text, str) and text:
                                if text.startswith(streamed_text):
                                    delta = text[len(streamed_text):]
                                    if delta:
                                        streamed_text += delta
                                        emit("delta", {"text": delta})
                                elif not streamed_text:
                                    streamed_text = text
                                    emit("delta", {"text": text})
                stderr = process.stderr.read() if process.stderr else ""
                returncode = process.wait(timeout=1800)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
                if stream:
                    emit("error", {"message": "Codex ใช้เวลานานเกิน 30 นาที คำขอถูกหยุด"})
                    if connected:
                        self.wfile.write(b"0\r\n\r\n")
                        self.wfile.flush()
                else:
                    self.send_json({"error": "Codex ใช้เวลานานเกิน 30 นาที คำขอถูกหยุด"}, 504)
                return
            reply = answer_path.read_text(encoding="utf-8").strip() if answer_path.exists() else ""
            if returncode or not reply:
                detail = event_error or stderr.strip() or "Codex ไม่ได้ส่งคำตอบกลับ"
                if stream:
                    emit("error", {"message": detail[-4000:]})
                    if connected:
                        self.wfile.write(b"0\r\n\r\n")
                        self.wfile.flush()
                else:
                    self.send_json({"error": detail[-4000:]}, 502)
                return
        display = body.get("display_message") if isinstance(body.get("display_message"), str) else message
        user_record = {"role": "user", "content": display, "files": body.get("files", []), "time": datetime.now().astimezone().strftime("%H:%M")}
        assistant_record = {"role": "assistant", "content": reply, "time": datetime.now().astimezone().strftime("%H:%M"), "model": model, "mode": mode}
        with LOCK:
            sessions = load_sessions()
            session = next((item for item in sessions if item.get("id") == session_id), None)
            if not session:
                if stream:
                    emit("error", {"message": "เซสชันถูกลบระหว่างส่งข้อความ"})
                else:
                    self.send_json({"error": "เซสชันถูกลบระหว่างส่งข้อความ"}, 404)
                return
            if session.get("codex_thread_id") != thread_id and session.get("codex_thread_id") != new_thread_id:
                if stream:
                    emit("error", {"message": "เซสชันถูกล้างระหว่างส่งข้อความ; ทิ้งคำตอบเก่าแล้ว"})
                else:
                    self.send_json({"error": "เซสชันถูกล้างระหว่างส่งข้อความ; ทิ้งคำตอบเก่าแล้ว"}, 409)
                return
            session["codex_thread_id"] = new_thread_id
            session["mode"] = mode
            session.setdefault("messages", []).extend((user_record, assistant_record))
            if session.get("title") == "แชตใหม่":
                session["title"] = display.strip().splitlines()[0][:60] or "แชตใหม่"
            session["updated_at"] = timestamp()
            save_sessions(sessions)
        cleaned = clean_session(session)
        if stream:
            emit("done", {"reply": reply, "session": cleaned})
            if connected:
                try:
                    self.wfile.write(b"0\r\n\r\n")
                    self.wfile.flush()
                except OSError:
                    pass
        else:
            self.send_json({"reply": reply, "session": cleaned})

    def send_json(self, value, status=200):
        data = json.dumps(value, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *_args):
        pass


def main():
    if not shutil.which("codex"):
        raise SystemExit("ไม่พบ Codex CLI ใน PATH")
    CODEX_HOME.mkdir(parents=True, exist_ok=True)
    restore_archived_terminals()
    server = ThreadingHTTPServer(("127.0.0.1", 8765), Handler)
    server.daemon_threads = True
    print("เปิด Codex web UI ที่ http://127.0.0.1:8765", flush=True)
    def stop_server(_signum, _frame):
        raise KeyboardInterrupt
    signal.signal(signal.SIGTERM, stop_server)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        close_puter_services()
        with TERMINAL_LOCK:
            terminals = list(TERMINALS.values())
        for terminal in terminals:
            if isinstance(terminal, BackgroundTerminal):
                terminal.close()


if __name__ == "__main__":
    main()
