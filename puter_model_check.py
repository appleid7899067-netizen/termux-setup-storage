#!/usr/bin/env python3
"""Check which Puter models actually work through the local Responses proxy."""
import argparse
import concurrent.futures
import json
import re
import sys
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

DEFAULT_PROXY = "http://127.0.0.1:36949/v1"

TOOL = {
    "type": "function",
    "name": "record_result",
    "description": "Record a short result string.",
    "parameters": {
        "type": "object",
        "properties": {"value": {"type": "string"}},
        "required": ["value"],
        "additionalProperties": False,
    },
}


def post_json(url, payload, timeout):
    request = Request(url, data=json.dumps(payload).encode(), headers={"Content-Type": "application/json"}, method="POST")
    with urlopen(request, timeout=timeout) as response:
        return json.loads(response.read())


def check(proxy_url, model, timeout):
    body = {
        "model": model,
        "instructions": "You are a terse test harness. Reply with tool calls when available.",
        "input": [{"type": "message", "role": "user", "content": [{"type": "input_text", "text": "Call record_result with value 'pong'. If tools are unavailable, reply pong."}]}],
        "tools": [TOOL],
        "stream": False,
        "max_output_tokens": 300,
    }
    try:
        result = post_json(f"{proxy_url}/responses", body, timeout)
    except HTTPError as error:
        detail = error.read().decode("utf-8", "replace")[:200]
        return {"model": model, "status": "ERROR", "detail": f"http {error.code}: {detail}"}
    except (URLError, OSError, ValueError) as error:
        return {"model": model, "status": "ERROR", "detail": str(error)[:200]}

    output = result.get("output") or []
    kinds = [item.get("type") for item in output]
    text = "".join(part.get("text", "") for item in output if item.get("type") == "message" for part in item.get("content") or [])
    tool_used = "function_call" in kinds or "custom_tool_call" in kinds
    if not output:
        return {"model": model, "status": "EMPTY", "detail": json.dumps(result, ensure_ascii=False)[:200]}
    if "pong" not in text.lower() and not tool_used:
        return {"model": model, "status": "BAD", "detail": (text or str(kinds))[:160].replace("\n", " ")}
    usage = result.get("usage") or {}
    return {"model": model, "status": "TOOL" if tool_used else "TEXT", "detail": f"in={usage.get('input_tokens', 0)} out={usage.get('output_tokens', 0)}"}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--proxy", default=None)
    parser.add_argument("--bridge", default=None)
    parser.add_argument("--timeout", type=float, default=90)
    parser.add_argument("--workers", type=int, default=4)
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--pattern", default="")
    parser.add_argument("--models", nargs="*", default=[], help="explicit ids; defaults to the full bridge catalog")
    parser.add_argument("--json", default="")
    args = parser.parse_args()

    bridge = args.bridge or re.sub(r"/v1/?$", "", args.proxy or DEFAULT_PROXY)
    models = args.models
    if not models:
        with urlopen(f"{bridge}/models", timeout=60) as response:
            data = json.loads(response.read())
        items = data.get("data") or data.get("models") or []
        grouped = {}
        for item in items:
            mid = item.get("id")
            if not mid:
                continue
            provider = item.get("provider") or "?"
            grouped.setdefault(provider, []).append(mid)
        models = [f"{p}/{m}" for p in sorted(grouped) for m in grouped[p]] if args.pattern else []
        flat = [m for group in grouped.values() for m in group]
        models = models or flat
    if args.pattern:
        rx = re.compile(args.pattern, re.I)
        models = [m for m in models if rx.search(m)]
    if args.limit:
        models = models[: args.limit]

    results = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=args.workers) as pool:
        for row in pool.map(lambda m: check(args.proxy or DEFAULT_PROXY, m, args.timeout), models):
            results.append(row)
            print(f"{row['status']:5} {row['model']:45} {row['detail']}", flush=True)

    summary = {}
    for row in results:
        summary[row["status"]] = summary.get(row["status"], 0) + 1
    print("\n== summary ==")
    for key in ("TOOL", "TEXT", "BAD", "EMPTY", "ERROR"):
        if key in summary:
            print(f"{key:6} {summary[key]}")
    if args.json:
        with open(args.json, "w", encoding="utf-8") as handle:
            json.dump(results, handle, ensure_ascii=False, indent=1)
    return 0


if __name__ == "__main__":
    sys.exit(main())
