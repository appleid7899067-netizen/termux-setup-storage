#!/usr/bin/env python3
"""Launch Codex TUI through Puter or the local Codex tools web UI."""
import argparse
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
from urllib.request import urlopen

DEFAULT_MODEL = "puter/qwen-flash"
DEFAULT_THEME = "termux-night"
PROJECT = Path(__file__).resolve().parent


def puter_login() -> int:
    node = shutil.which("node")
    if not node:
        print("ไม่พบ Node.js ใน PATH", file=sys.stderr)
        return 127
    return subprocess.run([node, str(PROJECT / "puter-login.cjs")], cwd=PROJECT, check=False).returncode


def main() -> int:
    parser = argparse.ArgumentParser(description="เปิด Codex TUI หรือเว็บ UI (ใช้คำสั่ง web)")
    parser.add_argument("-m", "--model", default=os.environ.get("PUTER_MODEL", DEFAULT_MODEL), help="ชื่อโมเดลที่ Puter รองรับ")
    parser.add_argument("--theme", default=DEFAULT_THEME, help=f"ชื่อธีม Codex (ค่าเริ่มต้น: {DEFAULT_THEME})")
    if len(sys.argv) > 1 and sys.argv[1] == "login":
        return puter_login()
    if len(sys.argv) > 1 and sys.argv[1] == "web":
        return subprocess.run([sys.executable, str(PROJECT / "web_ui.py"), *sys.argv[2:]], cwd=PROJECT, check=False).returncode
    args, codex_args = parser.parse_known_args()
    token_file = Path.home() / ".codex" / "puter-auth-token"

    codex = shutil.which("codex")
    if not codex:
        print("ไม่พบคำสั่ง codex ใน PATH", file=sys.stderr)
        return 127
    if not token_file.is_file() or not token_file.read_text(encoding="utf-8").strip():
        rc = puter_login()
        if rc:
            return rc

    env = os.environ.copy()
    # This key only satisfies Codex's provider auth check; the local proxy reads Puter's token privately.
    env["PUTER_PROXY_API_KEY"] = "local-proxy"
    codex_home = Path(env.get("CODEX_HOME", Path.home() / ".codex")).expanduser()
    codex_home.mkdir(parents=True, exist_ok=True)
    theme_file = PROJECT / "theme" / f"{args.theme}.tmTheme"
    if theme_file.is_file():
        theme_dir = codex_home / "themes"
        theme_dir.mkdir(parents=True, exist_ok=True)
        shutil.copy2(theme_file, theme_dir / theme_file.name)

    node = shutil.which("node")
    if not node:
        print("ไม่พบ Node.js ใน PATH", file=sys.stderr)
        return 127
    bridge_env = env.copy()
    bridge = subprocess.Popen(
        [node, str(PROJECT / "puter-sdk-bridge.cjs")],
        cwd=PROJECT, env=bridge_env, stdout=subprocess.PIPE, stderr=None, text=True,
    )
    bridge_url = bridge.stdout.readline().strip() if bridge.stdout else ""
    if not bridge_url.startswith("http://127.0.0.1:"):
        bridge.terminate()
        print("เปิด Puter SDK ไม่สำเร็จ", file=sys.stderr)
        return 1
    env["PUTER_SDK_BRIDGE_URL"] = bridge_url
    proxy = subprocess.Popen(
        [sys.executable, str(PROJECT / "puter_responses_proxy.py")],
        cwd=PROJECT, env=env, stdout=subprocess.PIPE, stderr=None, text=True,
    )
    try:
        base_url = proxy.stdout.readline().strip() if proxy.stdout else ""
        if not base_url.startswith("http://127.0.0.1:"):
            print("เปิด local Puter proxy ไม่สำเร็จ", file=sys.stderr)
            return 1
        catalog_path = codex_home / "puter-models.json"
        try:
            with urlopen(f"{base_url}/models", timeout=35) as response:
                catalog = json.loads(response.read())
            if not isinstance(catalog, dict) or not catalog.get("models"):
                raise ValueError("Puter ไม่ส่งรายการโมเดลกลับมา")
            catalog_path.write_text(json.dumps(catalog, ensure_ascii=False), encoding="utf-8")
            print(f"โหลดโมเดล Puter แล้ว {len(catalog['models'])} รายการ; เปิด /model ใน Codex เพื่อเลือก")
        except (OSError, ValueError, json.JSONDecodeError) as error:
            print(f"โหลดรายการโมเดล Puter ไม่สำเร็จ: {error}", file=sys.stderr)
            return 1

        command = [
            codex, "--model", args.model,
            "--config", 'model_provider="puter"',
            "--config", 'model_providers.puter.name="Puter"',
            "--config", f'model_providers.puter.base_url="{base_url}"',
            "--config", 'model_providers.puter.wire_api="responses"',
            "--config", 'model_providers.puter.env_key="PUTER_PROXY_API_KEY"',
            "--config", f"model_catalog_json={json.dumps(str(catalog_path))}",
            "--config", f'tui.theme="{args.theme}"',
            *codex_args,
        ]
        return subprocess.run(command, cwd=PROJECT, env=env, check=False).returncode
    except KeyboardInterrupt:
        return 130
    finally:
        proxy.terminate()
        try:
            proxy.wait(timeout=3)
        except subprocess.TimeoutExpired:
            proxy.kill()
        bridge.terminate()
        try:
            bridge.wait(timeout=3)
        except subprocess.TimeoutExpired:
            bridge.kill()


if __name__ == "__main__":
    raise SystemExit(main())
