#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""针对性重试：验证 nonce / 工具回传 / 原生多轮的失败是否为瞬时 502/429 抖动。"""
import json, secrets, ssl, time, urllib.request, urllib.error, sys, os

BASE = os.environ.get("PROBE_BASE", "http://185.8.106.38:9955")
KEY = os.environ.get("PROBE_KEY", "sk-YOUR-KEY")
MODEL = os.environ.get("PROBE_MODEL", "gemini-4-argon")
N = int(sys.argv[1]) if len(sys.argv) > 1 else 5
CTX = ssl.create_default_context(); CTX.check_hostname = False; CTX.verify_mode = ssl.CERT_NONE


def call(method, path, payload):
    body = json.dumps(payload, ensure_ascii=False).encode()
    req = urllib.request.Request(BASE + path, data=body, method=method,
                                 headers={"Content-Type": "application/json",
                                          "Authorization": "Bearer " + KEY})
    try:
        with urllib.request.urlopen(req, timeout=120, context=CTX) as r:
            return r.status, r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "replace")
    except Exception as e:
        return -1, str(e)[:120]


def chat(payload):
    return call("POST", "/v1/chat/completions", {"model": MODEL, **payload})


print(f"=== nonce x{N} ===")
for i in range(N):
    tok = secrets.token_hex(8)
    code, raw = chat({"messages": [{"role": "user", "content": f"Reply with exactly this token and nothing else: {tok}"}]})
    try:
        c = json.loads(raw).get("choices", [{}])[0].get("message", {}).get("content", "") if code == 200 else ""
    except Exception:
        c = ""
    print(f"  #{i+1} HTTP{code} expect={tok} got={c[:40]!r} -> {'MATCH' if tok in (c or '') else 'MISS'}")
    time.sleep(1)

print(f"=== tool roundtrip x{N} ===")
for i in range(N):
    code, raw = chat({"messages": [
        {"role": "user", "content": "Weather in Oslo? Use tool."},
        {"role": "assistant", "content": None, "tool_calls": [{"id": "call_probe_1", "type": "function",
            "function": {"name": "get_weather", "arguments": "{\"city\": \"Oslo\"}"}}]},
        {"role": "tool", "tool_call_id": "call_probe_1",
         "content": "{\"temp_c\": 7, \"condition\": \"snow\", \"humidity\": 88}"},
        {"role": "user", "content": "Summarize the weather in one short sentence."}]})
    try:
        c = json.loads(raw).get("choices", [{}])[0].get("message", {}).get("content", "") if code == 200 else ""
    except Exception:
        c = ""
    ok = code == 200 and ("7" in (c or "") or "snow" in (c or "").lower() or "雪" in (c or ""))
    print(f"  #{i+1} HTTP{code} -> {'OK' if ok else 'FAIL'} {c[:70]!r}")
    time.sleep(1)

print(f"=== native multiturn x{N} ===")
for i in range(N):
    code, raw = call("POST", f"/v1beta/models/{MODEL}:generateContent", {"contents": [
        {"role": "user", "parts": [{"text": "My ox is named Doudou. Remember."}]},
        {"role": "model", "parts": [{"text": "Doudou, noted!"}]},
        {"role": "user", "parts": [{"text": "Ox name? One word."}]}]})
    try:
        parts = json.loads(raw).get("candidates", [{}])[0].get("content", {}).get("parts", []) if code == 200 else []
        c = " ".join(p.get("text", "") for p in parts if not p.get("thought"))
    except Exception:
        c = ""
    print(f"  #{i+1} HTTP{code} -> {'OK' if 'Doudou' in c else 'MISS'} {c[:50]!r}")
    time.sleep(1)
