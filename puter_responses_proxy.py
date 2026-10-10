#!/usr/bin/env python3
"""Local Responses API adapter for Puter's OpenAI-compatible Chat Completions API."""
import json
import os
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

TOKEN_FILE = Path.home() / ".codex" / "puter-auth-token"
PUTER_SDK_BRIDGE_URL = os.environ.get("PUTER_SDK_BRIDGE_URL", "http://127.0.0.1:1")
FALLBACK_MODELS = [
    "openai/gpt-5.3-codex", "openai/gpt-5.5", "openai/gpt-5.4-nano",
    "openai/gpt-4.1", "openai/gpt-4o", "anthropic/claude-opus-5-5",
    "anthropic/claude-sonnet-5", "anthropic/claude-haiku-4.5",
    "google/gemini-3.8-flash", "x-ai/grok-4.7", "z-ai/glm-5.3",
    "deepseek/deepseek-v4-pro", "qwen/qwen3.8-max",
    "mistralai/mistral-small-2603", "moonshotai/kimi-k2.6",
]
CODEX_INSTRUCTIONS = (
    "You are Codex, an AI coding assistant. Work in the current project, "
    "use available tools to inspect and edit files, and follow repository instructions. "
    "Explain changes clearly and ask before destructive actions. "
    "ตอบผู้ใช้เป็นภาษาไทยเป็นค่าเริ่มต้น เว้นแต่ผู้ใช้ขอภาษาอื่น "
    "โดยคงชื่อไฟล์ คำสั่ง และโค้ดตามต้นฉบับ"
)

def make_model_entry(slug, priority):
    return {
        "slug": f"puter/{slug}", "display_name": f"Puter / {slug}", "description": "Puter model",
        "base_instructions": CODEX_INSTRUCTIONS,
        "supported_reasoning_levels": [], "shell_type": "unified_exec",
        "visibility": "list", "supported_in_api": True, "priority": priority,
        "availability_nux": None, "upgrade": None,
        "model_messages": {"instructions_template": CODEX_INSTRUCTIONS},
        "include_skills_usage_instructions": False,
        "include_plugin_usage_instructions": False,
        "include_apps_usage_instructions": False,
        "supports_reasoning_summary_parameter": False, "support_verbosity": False,
        "default_verbosity": None, "apply_patch_tool_type": "freeform",
        "truncation_policy": {"mode": "tokens", "limit": 32768},
        "effective_context_window_percent": 95,
        "experimental_supported_tools": [], "input_modalities": ["text", "image"],
    }

def models_catalog(token):
    request = Request(f"{PUTER_SDK_BRIDGE_URL}/models")
    try:
        with urlopen(request, timeout=30) as upstream:
            data = json.loads(upstream.read())
        raw_models = data.get("data", data.get("models", [])) if isinstance(data, dict) else []
        slugs = [item.get("id") for item in raw_models if isinstance(item, dict) and item.get("id")]
        if not slugs:
            slugs = FALLBACK_MODELS
    except (HTTPError, URLError, OSError, ValueError, AttributeError):
        slugs = []
    return {"models": [make_model_entry(slug, len(slugs) - i) for i, slug in enumerate(dict.fromkeys(slugs))]}


def as_text(value):
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        out = []
        for part in value:
            if not isinstance(part, dict):
                continue
            if part.get("type") in ("input_text", "text"):
                out.append(str(part.get("text", "")))
            elif part.get("type") in ("input_image", "image_url"):
                image = part.get("image_url")
                url = image.get("url") if isinstance(image, dict) else image
                if url:
                    out.append("[image]")
        return "".join(out)
    return str(value or "")


def convert_request(body):
    messages = []
    instructions = body.get("instructions")
    if instructions:
        messages.append({"role": "system", "content": instructions})
    entries = body.get("input", [])
    if isinstance(entries, str):
        entries = [{"role": "user", "content": entries}]
    tools = []
    custom_names = set()
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        typ = entry.get("type")
        if typ == "function_call_output":
            messages.append({"role": "tool", "tool_call_id": entry.get("call_id", ""), "content": as_text(entry.get("output"))})
        elif typ == "custom_tool_call_output":
            messages.append({"role": "tool", "tool_call_id": entry.get("call_id", ""), "content": as_text(entry.get("output"))})
        elif typ in ("function_call", "custom_tool_call"):
            arguments = entry.get("arguments", entry.get("input", "{}"))
            if not isinstance(arguments, str):
                arguments = json.dumps(arguments, ensure_ascii=False)
            messages.append({"role": "assistant", "content": "", "tool_calls": [{"id": entry.get("call_id", ""), "type": "function", "function": {"name": entry.get("name", ""), "arguments": arguments}}]})
        elif typ == "message" or "role" in entry:
            role = entry.get("role", "user")
            if role == "developer":
                role = "system"
            elif role not in ("system", "user", "assistant", "tool"):
                role = "user"
            content = as_text(entry.get("content", ""))
            if role == "assistant" and isinstance(content, list):
                content = "\n".join(x.get("text", "") for x in content if isinstance(x, dict))
            messages.append({"role": role, "content": content})
    for item in body.get("tools", []) or []:
        if not isinstance(item, dict):
            continue
        typ = item.get("type")
        if typ in ("function", "custom"):
            name = item.get("name", "")
            if typ == "custom":
                custom_names.add(name)
            schema = item.get("parameters") or item.get("format") or {"type": "object", "properties": {}}
            if typ == "custom":
                schema = {"type": "object", "properties": {"input": {"type": "string"}}, "required": ["input"], "additionalProperties": False}
            tools.append({"type": "function", "function": {"name": name, "description": item.get("description", ""), "parameters": schema}})
    model = body.get("model", "puter/qwen-flash")
    if model.startswith("puter/"):
        model = model.split("/", 1)[1]
    for message in messages:
        if message.get("role") == "developer":
            message["role"] = "system"
        elif message.get("role") not in ("system", "user", "assistant", "tool"):
            message["role"] = "user"
        if "content" not in message or message["content"] is None:
            message["content"] = ""
    payload = {"model": model, "messages": messages, "stream": False}
    if tools:
        payload["tools"] = tools
        payload["tool_choice"] = "auto"
    if body.get("temperature") is not None:
        payload["temperature"] = body["temperature"]
    if body.get("max_output_tokens"):
        payload["max_tokens"] = body["max_output_tokens"]
    for message in messages:
        if message.get("content") is None:
            message["content"] = ""
    return payload, custom_names


def response_object(chat, model, custom_names):
    choice = (chat.get("choices") or [{}])[0]
    msg = choice.get("message") or {}
    output = []
    content = msg.get("content")
    if content:
        if isinstance(content, list):
            text = "".join(p.get("text", "") for p in content if isinstance(p, dict))
        else:
            text = str(content)
        output.append({"id": "msg_" + str(chat.get("id", "local")), "type": "message", "status": "completed", "role": "assistant", "content": [{"type": "output_text", "text": text, "annotations": []}]})
    for call in msg.get("tool_calls") or []:
        fn = call.get("function") or {}
        name = fn.get("name", "")
        if name in custom_names:
            raw_input = fn.get("arguments", "{}")
            try:
                parsed_input = json.loads(raw_input)
                tool_input = parsed_input.get("input", raw_input) if isinstance(parsed_input, dict) else raw_input
            except (TypeError, json.JSONDecodeError):
                tool_input = raw_input
            output.append({"id": call.get("id", "call_local"), "type": "custom_tool_call", "call_id": call.get("id", "call_local"), "name": name, "input": tool_input, "status": "completed"})
        else:
            output.append({"id": call.get("id", "call_local"), "type": "function_call", "call_id": call.get("id", "call_local"), "name": name, "arguments": fn.get("arguments", "{}"), "status": "completed"})
    usage = chat.get("usage") or {}
    return {"id": "resp_" + str(chat.get("id", "local")), "object": "response", "created_at": chat.get("created", 0), "status": "completed", "error": None, "incomplete_details": None, "model": model, "output": output, "usage": {"input_tokens": usage.get("prompt_tokens", 0), "output_tokens": usage.get("completion_tokens", 0), "total_tokens": usage.get("total_tokens", 0)}}


def sse(response):
    rid = response["id"]
    events = [("response.created", {"type": "response.created", "response": {**response, "status": "in_progress", "output": []}}), ("response.in_progress", {"type": "response.in_progress", "response": {**response, "status": "in_progress", "output": []}})]
    for i, item in enumerate(response["output"]):
        events.append(("response.output_item.added", {"type": "response.output_item.added", "response_id": rid, "output_index": i, "item": {**item, "status": "in_progress"}}))
        if item["type"] == "message":
            text = item["content"][0]["text"]
            events.append(("response.content_part.added", {"type": "response.content_part.added", "response_id": rid, "item_id": item["id"], "output_index": i, "content_index": 0, "part": {"type": "output_text", "text": "", "annotations": []}}))
            if text:
                events.append(("response.output_text.delta", {"type": "response.output_text.delta", "response_id": rid, "item_id": item["id"], "output_index": i, "content_index": 0, "delta": text}))
            events.append(("response.output_text.done", {"type": "response.output_text.done", "response_id": rid, "item_id": item["id"], "output_index": i, "content_index": 0, "text": text}))
            events.append(("response.content_part.done", {"type": "response.content_part.done", "response_id": rid, "item_id": item["id"], "output_index": i, "content_index": 0, "part": item["content"][0]}))
        if item["type"] == "function_call":
            args = item.get("arguments", "{}")
            events.append(("response.function_call_arguments.delta", {"type": "response.function_call_arguments.delta", "response_id": rid, "item_id": item["id"], "output_index": i, "delta": args}))
            events.append(("response.function_call_arguments.done", {"type": "response.function_call_arguments.done", "response_id": rid, "item_id": item["id"], "output_index": i, "arguments": args}))
        elif item["type"] == "custom_tool_call":
            tool_input = item.get("input", "")
            events.append(("response.custom_tool_call_input.delta", {"type": "response.custom_tool_call_input.delta", "response_id": rid, "item_id": item["id"], "output_index": i, "delta": tool_input}))
            events.append(("response.custom_tool_call_input.done", {"type": "response.custom_tool_call_input.done", "response_id": rid, "item_id": item["id"], "output_index": i, "input": tool_input}))
        events.append(("response.output_item.done", {"type": "response.output_item.done", "response_id": rid, "output_index": i, "item": item}))
    events.append(("response.completed", {"type": "response.completed", "response": response}))
    return "".join(f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n" for event, data in events) + "data: [DONE]\n\n"


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    def log_message(self, *_args):
        pass
    def do_GET(self):
        if self.path.split("?", 1)[0].rstrip("/").endswith("/models"):
            try:
                token = TOKEN_FILE.read_text(encoding="utf-8").strip()
                data = json.dumps(models_catalog(token), ensure_ascii=False).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)
            except OSError as error:
                self.send_json_error(502, str(error))
            return
        self.send_error(404)
    def do_POST(self):
        try:
            length = int(self.headers.get("Content-Length", "0"))
            body = json.loads(self.rfile.read(length))
            token = TOKEN_FILE.read_text(encoding="utf-8").strip()
            payload, custom_names = convert_request(body)
            chat = None
            for attempt in range(3):
                request = Request(f"{PUTER_SDK_BRIDGE_URL}/chat", data=json.dumps(payload).encode(), headers={"Content-Type": "application/json"}, method="POST")
                try:
                    with urlopen(request, timeout=300) as upstream:
                        chat = json.loads(upstream.read())
                    break
                except HTTPError as error:
                    if error.code not in (429, 502, 503, 504) or attempt == 2:
                        raise
                    time.sleep(1 + attempt)
                except (URLError, OSError):
                    if attempt == 2:
                        raise
                    time.sleep(1 + attempt)
            result = response_object(chat, payload["model"], custom_names)
            if body.get("stream"):
                data = sse(result).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Cache-Control", "no-cache")
                self.send_header("Connection", "close")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)
            else:
                data = json.dumps(result, ensure_ascii=False).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)
        except HTTPError as e:
            details = e.read().decode("utf-8", "replace")[:4000]
            self.send_json_error(e.code, f"Puter API error: {details}")
        except (URLError, OSError, ValueError, KeyError, TypeError) as e:
            self.send_json_error(502, str(e))
    def send_json_error(self, code, message):
        data = json.dumps({"error": {"message": message, "type": "puter_proxy_error"}}).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


def main():
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server.daemon_threads = True
    print(f"http://127.0.0.1:{server.server_address[1]}/v1", flush=True)
    server.serve_forever()

if __name__ == "__main__":
    main()
