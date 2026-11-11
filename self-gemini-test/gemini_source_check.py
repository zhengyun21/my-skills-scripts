#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
gemini_source_check.py — Gemini 中转渠道纯度/判源测试
用法:
  python gemini_source_check.py --url https://4sapi.cn --key sk-xxx \
      [--models gemini-3.8-flash,gemini-3.7-flash] [--concurrency 4] [--out-dir ./probes]
输出:
  probes/gemini-source-check-{host}-{YYYYmmdd-HHMMSS}.txt
  (分层判读 + 每条证据附判断依据 + 原始请求响应报文)
仅用标准库。
"""
import argparse, base64, json, os, ssl, sys, threading, time, secrets, socket
# Windows 控制台/重定向默认 GBK, 打不出 ⓪/🟢 等字符会崩 -> 强制 UTF-8 输出
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from urllib.parse import urlparse
import urllib.request, urllib.error

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/131.0.0.0 Safari/537.36"
OFFICIAL_HOST = "generativelanguage.googleapis.com"
OAI_PATH = "/v1/chat/completions"  # 官方端点在 main() 改为 /v1beta/openai/chat/completions

def is_official(base):
    return urlparse(base).netloc == OFFICIAL_HOST
CTX = ssl.create_default_context()
CTX.check_hostname = False
CTX.verify_mode = ssl.CERT_NONE

RAW_LOG = []          # 原始报文区
RAW_LOCK = threading.Lock()
EVIDENCE = {}         # model -> [evidence lines]
DELIVERY = {}         # model -> [delivery findings]
NATIVE_DELIVERY = {}  # model -> [原生 generateContent 能力缺陷]
RAW_IDX = [0]

RAW_BODY_LIMIT = 6000

def _cap(s, limit=RAW_BODY_LIMIT):
    if limit and len(s) > limit:
        return s[:limit] + f"\n... [truncated {len(s) - limit} chars]"
    return s

def _fmt_body(s, limit=RAW_BODY_LIMIT):
    """JSON 报文美化: indent=2 + base64/thoughtSignature 脱敏; 非 JSON 原样(仅限长)"""
    if not s:
        return s
    try:
        obj = json.loads(s)
    except Exception:
        return _cap(s, limit)

    def scrub(x):
        if isinstance(x, str):
            if x.startswith("data:") and ";base64," in x:
                head, _, b64 = x.partition(";base64,")
                return f"{head};base64,<omitted {len(b64)} chars>"
            return x
        if isinstance(x, list):
            return [scrub(i) for i in x]
        if isinstance(x, dict):
            out = {}
            for k, v in x.items():
                if k in ("thoughtSignature", "thought_signature") and isinstance(v, str) and len(v) > 40:
                    out[k] = f"<omitted {len(v)} chars>"
                else:
                    out[k] = scrub(v)
            return out
        return x

    return _cap(json.dumps(scrub(obj), ensure_ascii=False, indent=2), limit)

def log_raw(tag, method, url, req_body, status, resp_body, note=""):
    with RAW_LOCK:
        RAW_IDX[0] += 1
        i = RAW_IDX[0]
    RAW_LOG.append(f"--- RAW#{i:03d} [{tag}] {method} {url} -> HTTP {status} {note}\n"
                   f">>> REQ:\n{_fmt_body(req_body)}\n<<< RESP:\n{_fmt_body(resp_body)}\n")

def http(base, method, path, payload=None, key="", timeout=120, headers=None, tag=""):
    url = base.rstrip("/") + path
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8") if payload is not None else None
    h = {"Content-Type": "application/json; charset=utf-8",
         "User-Agent": UA, "Accept": "application/json"}
    # 官方原生端点拒 Bearer+API key(期望 OAuth) -> 用 x-goog-api-key; 官方 OpenAI 兼容层与中转渠道仍用 Bearer
    if is_official(base) and "/openai/" not in path:
        h["x-goog-api-key"] = key
    else:
        h["Authorization"] = "Bearer " + key
    if headers: h.update(headers)
    r = urllib.request.Request(url, data=body, method=method, headers=h)
    raw, status = None, -1
    for attempt in range(2):  # 网络抖动重试一次
        try:
            with urllib.request.urlopen(r, timeout=timeout) as resp:
                raw = resp.read().decode("utf-8", "replace")
                status = resp.status
            break
        except urllib.error.HTTPError as e:
            raw = e.read().decode("utf-8", "replace"); status = e.code
            break
        except Exception as e:
            if attempt == 0:
                time.sleep(2); continue
            log_raw(tag, method, url, (body or b"").decode("utf-8", "replace"), -1, str(e)[:120], note="网络异常")
            return -1, {"__err__": str(e)[:120]}
    try: d = json.loads(raw)
    except Exception: d = {"__raw__": raw[:2000]}
    log_raw(tag, method, url, (body or b"").decode("utf-8", "replace"), status, raw)
    return status, d

def parts_text(d):
    out = []
    for c in d.get("candidates", []):
        for p in c.get("content", {}).get("parts", []):
            if not p.get("thought") and p.get("text"): out.append(p["text"])
    return " ".join(out)

def native_stream_text(chunks):
    """原生 SSE: 逐 chunk 拼接正文, 跳过 thought part; 用空串拼接避免拆散跨 chunk 的 nonce"""
    out = []
    for d in chunks:
        for c in d.get("candidates", []):
            for p in c.get("content", {}).get("parts", []):
                if not p.get("thought") and p.get("text"): out.append(p["text"])
    return "".join(out)

def _json_from_text(txt):
    """去掉 ``` / ```json 围栏后解析 JSON"""
    t = (txt or "").strip()
    if t.startswith("```"):
        if "\n" in t: t = t.split("\n", 1)[1]
        t = t.rsplit("```", 1)[0].strip()
    return json.loads(t)

def chat(base, key, model, payload_extra=None, timeout=120, tag="chat"):
    p = {"model": model, "messages": [{"role": "user", "content": "hi"}]}
    p.update(payload_extra or {})
    return http(base, "POST", OAI_PATH, p, key, timeout, tag=tag)

def gen(base, key, model, payload, timeout=120, tag="gen", api_prefix=None):
    path = (api_prefix or "/v1beta/models") + f"/{model}:generateContent"
    return http(base, "POST", path, payload, key, timeout, tag=tag)

def gen_retry(base, key, model, payload, timeout=120, tag="gen", api_prefix=None, attempts=3, backoff=2):
    """generateContent + 瞬时错误重试; 4xx(除429) 视为确定性结果直接返回。"""
    code, d = -1, {}
    for i in range(attempts):
        code, d = gen(base, key, model, payload, timeout,
                      tag=tag if i == 0 else f"{tag}#retry{i+1}", api_prefix=api_prefix)
        if code == 200 or (400 <= code < 500 and code != 429):
            return code, d
        if i < attempts - 1:
            time.sleep(backoff)
    return code, d

def gen_stream(base, key, model, payload, timeout=120, tag="gen_stream", api_prefix=None):
    """原生流式: :streamGenerateContent?alt=sse (google-genai SDK 同款路径)"""
    prefix = api_prefix or "/v1beta/models"
    url = base.rstrip("/") + prefix + f"/{model}:streamGenerateContent?alt=sse"
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    h = {"Content-Type": "application/json; charset=utf-8",
         "User-Agent": UA, "Accept": "text/event-stream"}
    if is_official(base):
        h["x-goog-api-key"] = key
    else:
        h["Authorization"] = "Bearer " + key
    r = urllib.request.Request(url, data=body, method="POST", headers=h)
    chunks, err, status = [], "", -1
    try:
        with urllib.request.urlopen(r, timeout=timeout) as resp:
            status = resp.status
            for raw in resp:
                line = raw.decode("utf-8", "replace").strip()
                if not line.startswith("data:"): continue
                s = line[5:].strip()
                if not s or s == "[DONE]": continue
                try: chunks.append(json.loads(s))
                except Exception: continue
    except urllib.error.HTTPError as e:
        status = e.code
        err = e.read().decode("utf-8", "replace")
    except Exception as e:
        err = str(e)[:160]
    shown = json.dumps(chunks[:3], ensure_ascii=False) if chunks else err
    log_raw(tag, "POST", url, body.decode("utf-8", "replace"), status, shown)
    return status, chunks, err

# ---------------- 探针 ----------------

def probe_sweep(base, key, model, S):
    """渠道级探活 + 回报模型名一致性 (429/5xx/网络错误退避重试, 瞬时限流不应误判探活失败)"""
    code, d = -1, {}
    for i in range(4):
        code, d = chat(base, key, model, {"messages": [{"role": "user", "content": "Reply with exactly: PONG"}]},
                       tag=f"sweep:{model}" if i == 0 else f"sweep:{model}#retry{i+1}")
        if code == 200 or (400 <= code < 500 and code != 429):
            break
        if i < 3: time.sleep(20 * (i + 1))
    if code == 200:
        said = (d.get("choices", [{}])[0].get("message", {}) or {}).get("content", "")
        echo = d.get("model", "")
        S["sweep"][model] = f"HTTP200 回报model={echo}"
        if echo and echo != model:
            add_delivery(model, f"渠道回报 model 字段与请求不符: 请求={model} 回报={echo}（中间层改写路由的迹象）")
    else:
        S["sweep"][model] = f"HTTP{code} {json.dumps(d, ensure_ascii=False)[:80]}"

def probe_nonce(base, key, model, S):
    """响应-请求一致性: 防串扰/缓存重放"""
    res = []
    for i in range(3):
        n = secrets.token_hex(8)
        code, d = chat(base, key, model, {"messages": [
            {"role": "user", "content": f"Reply with exactly this token and nothing else: {n}"}]},
            tag=f"nonce:{model}#{i+1}")
        c = (d.get("choices", [{}])[0].get("message", {}) or {}).get("content", "") if code == 200 else ""
        res.append("MATCH" if n in c else f"MISMATCH({c[:30]!r})")
    ok = sum(1 for r in res if r == "MATCH")
    S["nonce"][model] = f"{ok}/3 " + (" ".join(res))
    if ok < 3:
        add_delivery(model, f"nonce 一致性 {ok}/3 —— 存在响应串扰/缓存重放/空响应嫌疑（依据: 回复内容与请求内唯一随机数不对应）")

def probe_native(base, key, model, S):
    """原生端点发现"""
    for prefix in ["/v1beta/models", "/v1/models"]:
        code, d = gen(base, key, model, {"contents": [{"parts": [{"text": "Say OK"}]}]},
                      tag=f"native:{model}", api_prefix=prefix)
        if code == 200:
            S["native_prefix"] = prefix
            top, um = set(d.keys()), d.get("usageMetadata", {})
            mv = d.get("modelVersion", "")
            S["native"][model] = (f"顶层={sorted(top)} modelVersion={mv} "
                                  f"trafficType={um.get('trafficType')} serviceTier={um.get('serviceTier')} "
                                  f"createTime={'createTime' in top} thoughts={um.get('thoughtsTokenCount')}")
            if mv and mv != model:
                add_evidence(model, f"model_version_remapped={mv}",
                    f"渠道把请求模型 {model} 路由到上游变体 {mv} —— 模型名被中间层改写，交付物与请求名不符（-tiered 类后缀常见于聚合分发层）", 2, "flag")
            return True
    S["native"][model] = "无可用原生端点"
    return False

def probe_fields(base, key, model, S, n=12):
    """字段频次采样: 家族划分 + 混合池检测 + budget=0 合规"""
    if not S["native_prefix"]: return
    cnt, ptcs, mvs = Counter(), [], Counter()
    def one(_):
        code, d = gen(base, key, model,
            {"contents": [{"parts": [{"text": "Say OK"}]}],
             "generationConfig": {"thinkingConfig": {"thinkingBudget": 0}}},
            tag=f"fields:{model}", api_prefix=S.get("native_prefix"))
        if code != 200: return f"http_{code}"
        top, um = set(d.keys()), d.get("usageMetadata", {})
        ptcs.append(um.get("promptTokenCount"))
        mvs[d.get("modelVersion", "?")] += 1
        return ("tt" if "trafficType" in um else "") + ("|st" if "serviceTier" in um else "") + \
               ("|ct" if "createTime" in top else "") + ("|viol" if um.get("thoughtsTokenCount") not in (None, 0) else "")
    with ThreadPoolExecutor(max_workers=6) as ex:
        sigs = list(ex.map(one, range(n)))
    ok = sum(1 for s in sigs if not s.startswith("http_"))
    tt = sum(1 for s in sigs if "tt" in s); st = sum(1 for s in sigs if "st" in s)
    ct = sum(1 for s in sigs if "ct" in s); viol = sum(1 for s in sigs if "viol" in s)
    pt = Counter(ptcs)
    S["fields"][model] = (f"ok={ok}/{n} trafficType={tt} serviceTier={st} createTime={ct} "
                          f"budget0违规={viol} promptToken分布={dict(pt)} modelVersion={dict(mvs)}")
    if viol > n * 0.2:
        add_delivery(model, f"thinkingBudget=0 被无视 {viol}/{n} —— 关思考请求被上游忽略（依据: 请求了 thinkingBudget:0 但响应仍含 thoughtsTokenCount），"
                            f"你为不想要的思考 token 付费")
    bimodal = len([k for k in pt if k is not None]) >= 2 and max(pt.values()) < n * 0.9
    if bimodal:
        add_evidence(model, f"mixed_pool_promptToken={dict(pt)}",
            "promptTokenCount 双峰分布 —— 同一模型名下多个异质上游账号轮询（依据: 相同输入返回的注入开销不一致），"
            "该渠道行为不稳定（思考开关/延迟随命中账号漂移）", 2, "flag")
    fam = ("trafficType族" if tt >= n*0.8 and st <= n*0.2 else
           "serviceTier族" if st >= n*0.8 and tt <= n*0.2 else
           "混合池" if tt > 0 and st > 0 else "极简响应族(疑似CodeAssist/Antigravity后端)")
    S["family"][model] = fam
    add_evidence(model, f"field_family={fam}(tt={tt}/{n},st={st}/{n},ct={ct}/{n})",
        "族划分观察。官方proto一手核验(2026-10-10): traffic_type=8/create_time=12 仅定义于 Vertex proto, "
        "generativelanguage v1beta/v1alpha 均无; serviceTier/turnToken 两边 proto 均无 -> 不可归因仅记录", 1, "B")
    if tt >= n * 0.8 and ct >= n * 0.8:
        add_evidence(model, f"fields_proto=vertex_shape(tt={tt}/{n},ct={ct}/{n})",
            "响应稳定携带 trafficType+createTime —— 官方 proto 中二者仅 Vertex 侧定义(traffic_type=8, create_time=12; "
            "generativelanguage v1beta/v1alpha 无)。字段证据指向 Vertex; 若行为证据指向 Developer API 则构成冲突 -> "
            "判'无法定案'不强行归因（可能为 Vertex 后端+DeveloperAPI 语义文件层的混合内部通道, 未证实）", 4, "A")
    if st >= n * 0.8:
        add_evidence(model, f"serviceTier_only(st={st}/{n})",
            "serviceTier/turnToken 不在任何官方公开 proto 中 —— 不可归因字段, 仅记录不计分", 0, "观测")

GS_SCENE_URI = "gs://cloud-samples-data/generative-ai/image/city-street.png"
# 人工核验真图(768x1376): 曼哈顿路口俯拍, 黄色出租车/红色巴士/斑马线, 路面有粉色 "WISH YOU WERE HERE" 大字
GS_SCENE_EXPECTED = ["wish you were here", "aerial", "top-down", "top down", "overhead",
                     "intersection", "crosswalk", "taxi", "bus", "street", "city",
                     "building", "road", "traffic", "car", "俯拍", "航拍", "路口", "街", "城市", "建筑"]

def probe_gs_image(base, key, model, *, uri, mime, tag, evidence_key, weight, tier,
                   rationale, expected, prefix, bad_delivery=None):
    """读取真实公开 GCS 对象: 供 GCS 直读判源与原生多模态交付检查复用。
    返回 (code, text, result)；result 为写入报告的短字符串。"""
    code, d = gen_retry(base, key, model, {"contents": [{"parts": [
        {"fileData": {"fileUri": uri, "mimeType": mime}},
        {"text": "What is in this image? One short phrase."}]}]},
        tag=tag, api_prefix=prefix)
    if code != 200:
        if code == -1 or code == 429 or code >= 500:
            return code, "", f"INCONCLUSIVE(HTTP{code})"
        return code, "", f"HTTP{code}"
    txt = parts_text(d).strip()
    low = txt.lower()
    if txt and any(k in low for k in expected):
        add_evidence(model, f"{evidence_key}=READ_OK({txt[:60]!r})", rationale, weight, tier)
        return code, txt, f"READ_OK({txt[:60]!r})"
    if bad_delivery:
        add_native_delivery(model, bad_delivery.format(txt=txt[:80]))
    return code, txt, f"200但内容不符({txt[:40]!r})"

# ---------------- OpenAI 兼容层辅助 ----------------

FIXTURE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures")
VISION_CASES = [
    # (label, fixture 文件, mime, 真图人工核验后的关键词, prompt)
    ("daisy", "vision_daisy.b64", "image/jpeg",
     ["dais", "flower", "petal", "blossom", "雏菊", "花"], "What is in this image? One short phrase."),
    ("cat", "vision_cat.b64", "image/jpeg",
     ["cat", "kitten", "feline", "tabby", "猫", "雪", "snow"], "What is in this image? One short phrase."),
]
VISION_URL = "https://storage.googleapis.com/cloud-samples-data/generative-ai/image/daisy.jpg"
EFFORT_PUZZLE = ("A farmer has 17 sheep. All but 9 run away. How many sheep remain? "
                 "Work it out step by step, then give the final number.")
# E2E 真检索默认资源（Google 文档示例资源名; datastore/corpus 是项目私有资源, 未经证实公开可查,
# 多数分销渠道上游凭证无权访问 —— 任何失败一律记 inconclusive 不计分, 可用 --vertex-datastore/--vertex-rag-corpus 覆盖）
DEFAULT_VAIS_DATASTORE = "projects/cloud-ai-platform-docs/locations/global/collections/default_collection/dataStores/generative-ai-docs"
DEFAULT_RAG_CORPUS = "projects/cloud-ai-platform-docs/locations/global/ragCorpora/generative-ai-docs"

def _load_b64(name):
    try:
        with open(os.path.join(FIXTURE_DIR, name), "r", encoding="ascii") as f:
            return f.read().strip()
    except Exception:
        return None

def _oai_msg(d):
    """OpenAI 兼容响应 -> (content, tool_calls)"""
    try:
        m = d["choices"][0]["message"]
        return (m.get("content") or ""), (m.get("tool_calls") or [])
    except Exception:
        return "", []

def _image_tokens(d):
    u = d.get("usage") or {}
    for key in ("prompt_tokens_details", "input_tokens_details"):
        det = u.get(key)
        if isinstance(det, dict) and "image_tokens" in det:
            return det.get("image_tokens")
    return None

def _reasoning_tokens(d):
    u = d.get("usage") or {}
    rt = (u.get("completion_tokens_details") or {}).get("reasoning_tokens")
    if rt is None:
        rt = ((u.get("billing_usage") or {}).get("gemini_usage_metadata") or {}).get("thoughtsTokenCount")
    return rt

def _is_25(model):
    m = model.lower()
    return "2.5" in m and "pro" not in m

def probe_vision(base, key, model, S):
    """OpenAI 兼容 vision: base64 data URL 两张图 + daisy 的 https URL 形式"""
    res, failures, tested, passed = [], [], 0, 0
    for label, fname, mime, expected, prompt in VISION_CASES:
        b64 = _load_b64(fname)
        if not b64:
            res.append(f"{label}=INCONCLUSIVE(fixtures missing)")
            continue
        tested += 1
        content = [{"type": "text", "text": prompt},
                   {"type": "image_url", "image_url": {"url": f"data:{mime};base64,{b64}"}}]
        code, d = chat(base, key, model, {"messages": [{"role": "user", "content": content}]},
                       tag=f"oai_vision:{model}:{label}")
        if code != 200:
            res.append(f"{label}=HTTP{code}")
            failures.append(f"{label} HTTP{code}")
            continue
        txt, _ = _oai_msg(d)
        itok = _image_tokens(d)
        hit = bool(txt) and any(k in txt.lower() for k in expected)
        ok = hit and (itok is None or itok > 0)
        if ok:
            passed += 1
            res.append(f"{label}=OK({txt[:24]!r},img_tok={itok})")
        else:
            res.append(f"{label}=FAIL({txt[:24]!r},img_tok={itok})")
            failures.append(f"{label} 描述不符/图未处理")
    # URL 形式: 文档未承诺, 仅观测
    code, d = chat(base, key, model, {"messages": [{"role": "user", "content": [
        {"type": "text", "text": VISION_CASES[0][4]},
        {"type": "image_url", "image_url": {"url": VISION_URL}}]}]},
        tag=f"oai_vision_url:{model}")
    if code == 200:
        txt, _ = _oai_msg(d)
        itok = _image_tokens(d)
        url_ok = bool(txt) and any(k in txt.lower() for k in VISION_CASES[0][3]) and (itok is None or itok > 0)
        res.append(f"url={'OK' if url_ok else 'FAIL'}({txt[:20]!r},img_tok={itok})")
    else:
        res.append(f"url=HTTP{code}")
    S["openai_vision"][model] = " | ".join(res)
    if tested and passed < tested:
        add_delivery(model, f"OpenAI 视觉(base64) {passed}/{tested} —— " + "；".join(failures))

def probe_openai_tools(base, key, model, S):
    """OpenAI 兼容 tool_choice(auto 严格) / forced / none / parallel_tool_calls(观测)"""
    tools = [{"type": "function", "function": {
        "name": "get_weather", "description": "Get the current weather for a city.",
        "parameters": {"type": "object", "properties": {"city": {"type": "string"}}, "required": ["city"]}}}]
    res = {}
    code, d = chat(base, key, model, {"tools": tools, "tool_choice": "auto",
        "messages": [{"role": "user", "content": "Weather in Oslo? Use the get_weather tool."}]},
        tag=f"oai_choice_auto:{model}")
    _, tc = _oai_msg(d) if code == 200 else ("", [])
    auto_ok = code == 200
    res["auto"] = (("OK(called)" if tc else "OK(text)") if auto_ok else f"HTTP{code}")
    if not auto_ok:
        add_delivery(model, f"tool_choice=auto 被拒绝 HTTP{code} —— 文档明确支持的参数不可用")
    code, d = chat(base, key, model, {"tools": tools,
        "tool_choice": {"type": "function", "function": {"name": "get_weather"}},
        "messages": [{"role": "user", "content": "What is the weather in Oslo?"}]},
        tag=f"oai_choice_forced:{model}")
    _, tc = _oai_msg(d) if code == 200 else ("", [])
    forced_ok = code == 200 and any((t.get("function") or {}).get("name") == "get_weather" for t in tc)
    res["forced"] = "OK" if forced_ok else (f"FAIL(HTTP{code})" if code != 200 else "FAIL")
    code, d = chat(base, key, model, {"tools": tools, "tool_choice": "none",
        "messages": [{"role": "user", "content": "Weather in Oslo? Use the get_weather tool."}]},
        tag=f"oai_choice_none:{model}")
    _, tc = _oai_msg(d) if code == 200 else ("", [])
    none_ok = code == 200 and not tc
    res["none"] = "OK" if none_ok else (f"FAIL(HTTP{code})" if code != 200 else "FAIL")
    code, d = chat(base, key, model, {"tools": tools, "parallel_tool_calls": True,
        "messages": [{"role": "user", "content": "Get the weather for Oslo and Paris. Call get_weather for both cities."}]},
        tag=f"oai_parallel:{model}")
    _, tc = _oai_msg(d) if code == 200 else ("", [])
    par_ok = code == 200 and len(tc) >= 2
    res["parallel"] = f"OK({len(tc)}calls)" if par_ok else (f"HTTP{code}" if code != 200 else f"FAIL({len(tc)}calls)")
    S["openai_tools"][model] = " | ".join(f"{k}={v}" for k, v in res.items())

def probe_openai_extra(base, key, model, S):
    """extra_body 字段透传(线上为顶层 JSON): 功能验证优先, 非法值哨兵兜底"""
    def sentinel(payload, tag):
        code, d = chat(base, key, model, dict(payload, messages=[{"role": "user", "content": "Say OK"}]),
                       tag=f"oai_extra_{tag}:{model}")
        return code, json.dumps(d, ensure_ascii=False).lower()

    res = {}
    # thinking_config: 先看 low/high 是否真的产生思考差异; 宽松网关会接受非法 thinking_level
    toks = {}
    for lvl in ("low", "high"):
        code, d = chat(base, key, model, {"thinking_config": {"thinking_level": lvl, "include_thoughts": True},
            "messages": [{"role": "user", "content": EFFORT_PUZZLE}]}, tag=f"oai_think_lvl:{model}:{lvl}")
        toks[lvl] = _reasoning_tokens(d) if code == 200 else None
    lo, hi = toks.get("low"), toks.get("high")
    code, msg = sentinel({"thinking_config": {"thinking_level": "bogus_probe_level"}}, "thinking")
    think_sentinel_pass = code in (400, 422) and ("thinking" in msg or "level" in msg)
    if isinstance(lo, int) and isinstance(hi, int) and hi > lo:
        res["thinking"] = f"pass(functional low={lo},high={hi})"
    elif think_sentinel_pass:
        res["thinking"] = "pass(sentinel)"
    else:
        res["thinking"] = f"unverified(low={lo},high={hi},sentinel=HTTP{code})"
    # safety_settings: 文档只把它列在 images/generations; 本脚本不测图像生成端点
    res["safety"] = "N/A(doc: images only)"
    # cached_content: 不存在的资源名 / 错误类型, 任一触发上游校验即算透传
    cache_codes = []
    for payload, tag in [
        ({"cached_content": "cachedContents/nonexistent-probe-xyz"}, "cache_name"),
        ({"cached_content": 12345}, "cache_type")]:
        c, m = sentinel(payload, tag)
        cache_codes.append((c, ("cache" in m or "not found" in m)))
    if any(c in (400, 404, 422) and hit for c, hit in cache_codes):
        res["cache"] = "pass(sentinel)"
    elif all(c == 200 for c, _ in cache_codes):
        res["cache"] = "suspected_stripped(200x2)"
        add_delivery(model, "extra_body.cached_content 疑似被静默剥离 —— 不存在/错误类型均未触发上游校验")
    else:
        res["cache"] = f"unverified({[c for c, _ in cache_codes]})"
    S["openai_extra"][model] = " | ".join(f"{k}={v}" for k, v in res.items())

def probe_openai_compat(base, key, model, S):
    probe_vision(base, key, model, S)
    probe_openai_tools(base, key, model, S)
    probe_openai_extra(base, key, model, S)

def probe_files(base, key, model, S):
    """行为探针: 文件 URI 语义 (判源核心)"""
    if not S["native_prefix"]: return
    N = S["native_prefix"]
    # B: files/URI (Developer API Files 注册表专属资源)
    code, d = gen(base, key, model, {"contents": [{"parts": [{"fileData": {
        "fileUri": "https://generativelanguage.googleapis.com/v1beta/files/probe-fake-123",
        "mimeType": "image/png"}}]}]}, tag=f"probeB:{model}", api_prefix=S.get("native_prefix"))
    msg = json.dumps(d, ensure_ascii=False)
    if code == 200:
        add_evidence(model, "files_uri=accepted",
            "files/ URI 被接受且成功处理 —— Files 注册表资源可解析（依据: Files API 为 generativelanguage 独占, "
            "python-genai 文档 'Files are only supported in Gemini Developer API'）", 4, "A")
        S["files_uri"] = "accepted"
    elif "You do not have permission to access the File" in msg:
        add_evidence(model, "files_uri=registry_403",
            "Files 注册表专属错误文本 —— 上游真的查询了 Files 注册表（依据: 错误语义指向 generativelanguage Files 服务的权限模型，"
            "Vertex 无此服务）", 4, "A")
        S["files_uri"] = "registry_403"
    elif "The caller does not have permission" in msg or "permission" in msg.lower():
        add_evidence(model, "files_uri=generic_403",
            "通用 IAM 403 而非 Files 注册表错误 —— 上游无 Files 服务语义（依据: 与 8 个已证实 Developer API 渠道的专属错误文本不同族）", 2, "B")
        S["files_uri"] = "generic_403"
    else:
        add_evidence(model, f"files_uri=other(HTTP{code})", f"未识别的 files/URI 行为: {msg[:120]}", 0, "观测")

    # C1: 假桶 gs://
    code, d = gen(base, key, model, {"contents": [{"parts": [{"fileData": {
        "fileUri": "gs://probe-fake-bucket-xyz/img.png", "mimeType": "image/png"}}]}]},
        tag=f"probeC1:{model}", api_prefix=S.get("native_prefix"))
    msg = json.dumps(d, ensure_ascii=False)
    if "Register" in msg and "not supported" in msg:
        add_evidence(model, "gs_fake=registerfile_reject",
            "gs:// 被 'RegisterFile' 流程拒绝 —— Developer API 标志行为（依据: ai.google.dev 文件输入文档明确 gs:// 需先 files:register；"
            "Vertex 文档示范 gs:// 直连。双向文档核实）", 6, "A")
        S["gs_fake"] = "registerfile_reject"
    elif code in (400, 403, 500):
        add_evidence(model, f"gs_fake=generic(HTTP{code})",
            f"假桶返回通用错误而非 RegisterFile 拒绝 —— 缺少 Developer API 标志行为，疑似具备 GCS 直读路径（依据: 错误文本 {msg[:80]!r}）", 3, "B")
        S["gs_fake"] = f"generic_{code}"
    else:
        S["gs_fake"] = f"other_{code}"

    # C2: 真桶+不存在对象 (GCS 错误语义)
    code, d = gen(base, key, model, {"contents": [{"parts": [{"fileData": {
        "fileUri": "gs://cloud-samples-data/generative-ai/image/nonexistent-xyz-987.jpg",
        "mimeType": "image/jpeg"}}]}]}, tag=f"probeC2:{model}", api_prefix=S.get("native_prefix"))
    msg = json.dumps(d, ensure_ascii=False)
    if "No such object" in msg:
        add_evidence(model, "gs_missing_obj=gcs_native_404",
            "真桶缺对象返回 GCS 原生 'No such object' 文本 —— 拉取请求到达了真实 GCS（依据: 'No such object: bucket/object' 为 GCS XML API 标准错误）", 3, "B")
        S["gs_missing"] = "gcs_native_404"
    else:
        S["gs_missing"] = f"other({msg[:80]!r})"

    # C3: 阳性对照 —— 真实公开 GCS 图 (判源最硬证据)
    code, txt, res = probe_gs_image(
        base, key, model, uri="gs://cloud-samples-data/generative-ai/image/scones.jpg",
        mime="image/jpeg", tag=f"probeC3:{model}", evidence_key="gs_real_image",
        weight=6, tier="A",
        rationale="上游真实拉取并理解了 GCS 公开对象 —— GCS 直读能力实证（依据: Developer API 架构上不支持裸 gs:// 直读，"
                  "需 Files 注册；此能力仅 Vertex 原生具备。真图+正确描述无法靠改字段伪造）",
        expected=["scone", "pastry", "cookie", "blueberry", "food", "烘焙", "司康", "点心"],
        prefix=S.get("native_prefix"))
    S["gs_real"] = res

    # C3b: 复核 —— 第二张真实 GCS 图(人物+景物场景), 不重复计分; 识图错误记 ⓪b
    code, txt, res = probe_gs_image(
        base, key, model, uri=GS_SCENE_URI, mime="image/png", tag=f"probeC3b:{model}",
        evidence_key="gs_real_scene", weight=0, tier="观测",
        rationale="第二张真实 GCS 图读取成功（依据: 与 scones 同桶的公开对象，描述与真图内容一致；仅作 GCS 直读一致性复核，不重复计分）",
        expected=GS_SCENE_EXPECTED, prefix=S.get("native_prefix"),
        bad_delivery="多模态识图与图内容不符 —— 原生端点声称读取 gs:// 图但描述对不上，疑似编造/缓存串扰（描述: {txt}）")
    S["gs_scene"][model] = res

    # C4: https-404 → URL_ERROR-* 状态族 (Vertex URL抓取器指纹)
    code, d = gen(base, key, model, {"contents": [{"parts": [{"fileData": {
        "fileUri": "https://storage.googleapis.com/nonexistent-bucket-abc123-xyz/q.png",
        "mimeType": "image/png"}}]}]}, tag=f"probeC4:{model}", api_prefix=S.get("native_prefix"))
    msg = json.dumps(d, ensure_ascii=False)
    if "URL_ERROR" in msg:
        add_evidence(model, "https_404=URL_ERROR_status",
            "URL_ERROR-* 状态族（v3审计修正: AI Studio 行为渠道 uqapi 也出现此状态族 → 非 Vertex 独占, "
            "疑似 generativelanguage URL 抓取器词汇 → 降级为中性观测, 不计分）", 0, "观测")
        S["url_error"] = True

def probe_tools(base, key, model, S, datastore=None, rag_corpus=None):
    """tools[] proto 指纹 v3 —— 接受性探测 + Vertex 独占工具 E2E 强制调用验证

    官方 discovery 一手核验(2026-10-27, aiplatform v1beta1 vs generativelanguage v1beta):
      - Vertex Tool 独有: retrieval{vertexAiSearch, vertexRagStore, externalApi}、enterpriseWebSearch、parallelAiSearch
      - generativelanguage Tool 独有: fileSearch、mcpServers
    旧版把 vertexAiSearch/vertexRagStore 发在 Tool 顶层 —— 该写法对 Vertex 本身非法（正确位置是
    tools[0].retrieval.*），真 Vertex 也会报 Cannot find field，曾导致误判 devapi，已废弃。

    v3(2026-11-11, 官方文档一手核验): 接受≠执行 —— 翻译层白名单可放行工具名并返回 200
    （sui-xiang 实测放行全部 retrieval.* 而无任何检索发生），但 FIELD 拒绝文本不可伪造。
    因此 Vertex 独占工具被接受后立即追加 E2E 检索诱导请求，仅当响应携带 Vertex 独占
    grounding 元数据才算端到端执行:
      - vertexAiSearch: groundingChunks[].retrievedContext / retrievalQueries（仅 Vertex proto 定义,
        google_search 重路由填不出 -> A类）
      - vertexRagStore: 同上
      - enterpriseWebSearch/parallelAiSearch: webSearchQueries / groundingChunks[].web
        （与 devapi google_search 响应字段重叠, 重路由可伪造 -> B类）
    文档确认 grounding 元数据可能因低相关缺席 -> accepted_no_grounding 不判造假但也不构成 Vertex 证据。
    """
    if not S["native_prefix"]: return
    N = S["native_prefix"]
    ds = datastore if datastore is not None else DEFAULT_VAIS_DATASTORE
    rc = rag_corpus if rag_corpus is not None else DEFAULT_RAG_CORPUS

    def send(name, tool, retry=False):
        fn = gen_retry if retry else gen
        code, d = fn(base, key, model, {"contents": [{"parts": [{"text": "hi"}]}],
            "tools": [tool]}, tag=f"tools:{model}:{name}", api_prefix=N)
        return code, json.dumps(d, ensure_ascii=False)

    def classify(code, msg):
        if code == 200: return "ok"
        if "Cannot find field" in msg or "Unknown name" in msg: return "unknown_field"
        if "proto is invalid" in msg or "FIELD_INVALID" in msg: return "proto_invalid"
        return f"http{code}"

    def e2e_check(tag, tool, prompt, expect_ctx):
        """E2E 强制调用: 检索诱导提示词 + Vertex 独占 grounding 元数据硬标准。
        expect_ctx=True 认 retrievedContext/retrievalQueries; False 认 webSearchQueries/groundingChunks[].web"""
        code, d = gen_retry(base, key, model,
            {"contents": [{"parts": [{"text": prompt}]}], "tools": [tool]},
            tag=f"tools_e2e:{model}:{tag}", api_prefix=N)
        if code != 200:
            return f"inconclusive(HTTP{code})"
        gm = ((d.get("candidates") or [{}])[0].get("groundingMetadata") or {})
        ans = parts_text(d)[:60]
        if expect_ctx:
            has_ctx = any(c.get("retrievedContext") for c in (gm.get("groundingChunks") or []))
            if has_ctx or gm.get("retrievalQueries"):
                return f"E2E_OK({ans!r})"
        else:
            if gm.get("webSearchQueries") or any(c.get("web") for c in (gm.get("groundingChunks") or [])):
                return f"E2E_OK({ans!r})"
        return f"accepted_no_grounding({ans!r})"

    res = {}
    # Vertex 正证据组（正确形状; gen_retry 防瞬时 5xx 翻转分类）
    res["retrieval.vertexAiSearch"] = classify(*send("r_vais", {"retrieval": {"vertexAiSearch": {}}}, retry=True))
    res["retrieval.vertexRagStore"] = classify(*send("r_rag", {"retrieval": {"vertexRagStore": {}}}, retry=True))
    res["retrieval.externalApi"] = classify(*send("r_extapi", {"retrieval": {"externalApi": {}}}, retry=True))
    # AI Studio 独占负证据组
    res["fileSearch"] = classify(*send("fileSearch", {"fileSearch": {}}))
    res["mcpServers"] = classify(*send("mcpServers", {"mcpServers": []}))
    # 通用认知集 + 对照 + 弃用观测
    res["urlContext"] = classify(*send("urlContext", {"urlContext": {}}))
    res["google_search"] = classify(*send("google_search", {"google_search": {}}))
    res["codeExecution"] = classify(*send("codeExecution", {"codeExecution": {}}))
    res["zzFakeTool"] = classify(*send("zzFakeTool", {"zzFakeTool": {}}))
    gcode, gmsg = send("googleSearchRetrieval", {"googleSearchRetrieval": {}})
    res["googleSearchRetrieval"] = "deprecated_msg" if (gcode == 400 and "not supported" in gmsg) else classify(gcode, gmsg)
    # 版本指纹组（Vertex 独占 Tool 字段, 仅记录不计分）
    res["enterpriseWebSearch"] = classify(*send("entWebSearch", {"enterpriseWebSearch": {}}))
    res["parallelAiSearch"] = classify(*send("parAiSearch", {"parallelAiSearch": {}}))

    knownset = ",".join(k for k in ("urlContext", "google_search", "codeExecution") if res[k] == "ok") or "无"
    S["tools"][model] = (f"vertex_retrieval={res['retrieval.vertexAiSearch']}/{res['retrieval.vertexRagStore']}/{res['retrieval.externalApi']} "
                         f"as_fields={res['fileSearch']}/{res['mcpServers']} knownset={knownset} "
                         f"ctrl={res['zzFakeTool']} gsr={res['googleSearchRetrieval']} "
                         f"fp(ent={res['enterpriseWebSearch']},par={res['parallelAiSearch']})")

    if res["zzFakeTool"] == "ok":
        add_evidence(model, "tools_probe=invalidated",
            "⚠ tools 探针对本渠道作废：捏造的 zzFakeTool 也被接受 —— 翻译层静默剥离 tools，接受≠认识（依据: 无对照的'接受'不可采信）", 0, "作废")
        return

    # E2E 强制调用验证: 仅对被接受的 Vertex 独占工具追加（对照失效时不浪费请求）
    e2e = {}
    if res["retrieval.vertexAiSearch"] == "ok":
        e2e["vais"] = e2e_check("vais", {"retrieval": {"vertexAiSearch": {"datastore": ds}}},
                                "Search the datastore and summarize what you find in one line.", expect_ctx=True)
    if res["retrieval.vertexRagStore"] == "ok":
        e2e["rag"] = e2e_check("rag", {"retrieval": {"vertexRagStore": {"ragResources": [{"ragCorpus": rc}]}}},
                               "Search the corpus and summarize what you find in one line.", expect_ctx=True)
    if res["enterpriseWebSearch"] == "ok":
        e2e["ent"] = e2e_check("ent", {"enterpriseWebSearch": {}},
                               "When is the next total solar eclipse in the United States?", expect_ctx=False)
    if res["parallelAiSearch"] == "ok":
        e2e["par"] = e2e_check("par", {"parallelAiSearch": {}},
                               "Who won the most recent Formula 1 Grand Prix? Search for it.", expect_ctx=False)
    if e2e:
        S["tools"][model] += " e2e(" + ",".join(f"{k}={v}" for k, v in e2e.items()) + ")"

    rec = [k for k in ("retrieval.vertexAiSearch", "retrieval.vertexRagStore", "retrieval.externalApi")
           if res[k] in ("ok", "proto_invalid")]
    if rec:
        add_evidence(model, "tools_vertex_retrieval=accepted(" + ",".join(f"{k}={res[k]}" for k in rec) + ")",
            "正确形状 retrieval.* 被上游接受 —— 但接受≠执行: 翻译层白名单可放行工具名返回 200"
            "（依据: 2026-11-11 官方文档一手核验 + sui-xiang 实测放行 retrieval.* 而检索未发生），"
            "仅作观测不计分, 执行与否交由 E2E 验证", 0, "观测")
    elif res["retrieval.vertexAiSearch"] == "unknown_field" and res["retrieval.vertexRagStore"] == "unknown_field":
        add_evidence(model, "tools_vertex_retrieval=rejected(retrieval Cannot find field)",
            "retrieval 容器被 proto 拒绝 —— generativelanguage Tool 无此字段, Developer API 标志行为（依据: 官方 discovery 一手核验）", 4, "A")

    # E2E 计分: retrievedContext/retrievalQueries 仅 Vertex proto 定义, 白名单层放行 200 但
    # google_search 重路由填不出这些字段 -> A类; web 字段两边 proto 都有 -> B类
    ctx_ok = [k for k in ("vais", "rag") if e2e.get(k, "").startswith("E2E_OK")]
    ctx_ng = [k for k in ("vais", "rag") if e2e.get(k, "").startswith("accepted_no_grounding")]
    web_ok = [k for k in ("ent", "par") if e2e.get(k, "").startswith("E2E_OK")]
    if ctx_ok:
        add_evidence(model, "tools_vertex_e2e=OK(" + ",".join(f"{k}={e2e[k]}" for k in ctx_ok) + ")",
            "Vertex 独占工具端到端执行成功 —— groundingChunks[].retrievedContext/retrievalQueries 仅 Vertex proto 定义, "
            "开发者 API 无法产生, 翻译层重路由到 google_search 也填不出（依据: 2026-11-11 官方文档一手核验）", 4, "A")
    elif ctx_ng:
        add_evidence(model, "tools_vertex_e2e=accepted_no_grounding(" + ",".join(f"{k}={e2e[k]}" for k in ctx_ng) + ")",
            "⚠ 疑似白名单翻译层: retrieval 工具名被放行(200)但强制检索后无任何 Vertex 独占 grounding 元数据 —— "
            "接受≠执行（依据: 官方文档确认元数据可能因低相关缺席, 不判造假, 但不构成 Vertex 证据）", 0, "观测")
    if web_ok:
        add_evidence(model, "tools_vertex_e2e_web=OK(" + ",".join(f"{k}={e2e[k]}" for k in web_ok) + ")",
            "enterpriseWebSearch/parallelAiSearch 端到端执行成功（依据: webSearchQueries/groundingChunks[].web 与 devapi "
            "google_search 响应字段重叠, 翻译层重路由可伪造 -> 降为 B 类, 与 proto 级接受证据叠加时才构成强指向）", 2, "B")

    if res["fileSearch"] == "unknown_field" and res["mcpServers"] == "unknown_field" and res["urlContext"] == "ok":
        add_evidence(model, "tools_as_fields=missing_strict",
            "AI Studio 独占字段 fileSearch/mcpServers 双双 Cannot find field 且通用字段 urlContext 被接受 —— 上游 proto 为 Vertex 形状"
            "（依据: fileSearch/mcpServers 仅 generativelanguage Tool 定义, Vertex Tool 无）", 2, "B")
    elif res["fileSearch"] == "ok" or res["mcpServers"] == "ok":
        add_evidence(model, "tools_as_fields=present",
            "AI Studio 独占字段被接受 —— generativelanguage 较新字段集（依据: Vertex Tool 无 fileSearch/mcpServers）", 2, "B")

    if res["googleSearchRetrieval"] == "deprecated_msg":
        add_evidence(model, "gsr_deprecated=use_google_search",
            "google_search_retrieval 被产品层禁用并提示改用 google_search —— proto 认识该字段但新版后端停用（中性观测, 不计分）", 0, "观测")

def probe_params(base, key, model, S):
    """基础参数生效: max_tokens / stop / system prompt / temperature边界"""
    res = {}
    code, d = chat(base, key, model, {"max_tokens": 15,
        "messages": [{"role": "user", "content": "Count from 1 to 100, digits separated by commas."}]},
        tag=f"param_mt:{model}")
    fin = d.get("choices", [{}])[0].get("finish_reason") if code == 200 else None
    res["max_tokens=15"] = f"finish={fin}" + ("(生效)" if fin == "length" else "(未截断!)" if fin else f"(HTTP{code})")
    if code == 200 and fin != "length":
        add_delivery(model, "max_tokens 未生效（依据: 限15 token 的请求正常结束, finish_reason!=length）——长度控制不可信")
    code, d = chat(base, key, model, {"stop": ["。"], "reasoning_effort": "low",
        "messages": [{"role": "user", "content": "用一句话介绍北京。"}]}, tag=f"param_stop:{model}")
    c = d.get("choices", [{}])[0].get("message", {}).get("content", "") if code == 200 else ""
    stop_ok = code == 200 and "。" not in c
    res["stop=['。']"] = ("生效" if stop_ok else f"未生效({c[:30]!r})")
    if code == 200 and not stop_ok:
        add_delivery(model, "stop 参数未生效（依据: stop 序列 '。' 后内容仍被输出）")
    code, d = chat(base, key, model, {"messages": [
        {"role": "system", "content": "You must answer in English only, always."},
        {"role": "user", "content": "你叫什么名字？一句话。"}]}, tag=f"param_sys:{model}")
    c = d.get("choices", [{}])[0].get("message", {}).get("content", "") if code == 200 else ""
    has_cjk = any('一' <= ch <= '鿿' for ch in c)
    res["system英文限定"] = "遵循" if (code == 200 and c and not has_cjk) else f"存疑({c[:25]!r})"
    code, d = chat(base, key, model, {"temperature": 9.9,
        "messages": [{"role": "user", "content": "hi"}]}, tag=f"param_temp:{model}")
    msg = json.dumps(d, ensure_ascii=False)[:150]
    res["temperature=9.9"] = f"HTTP{code} {msg[:80]}" if code != 200 else "200(被clamp?交付缺陷!)"
    if code == 200:
        add_delivery(model, "temperature=9.9 被静默放行（依据: 越界参数未报错——上游或中间层悄悄 clamp，请求不等于交付）")
    S["params"][model] = " | ".join(f"{k}: {v}" for k, v in res.items())

def probe_tool_roundtrip(base, key, model, S):
    """工具结果回传: 模型能否消费 role:tool 消息并引用其中的数据"""
    code, d = chat(base, key, model, {"messages": [
        {"role": "user", "content": "Weather in Oslo? Use tool."},
        {"role": "assistant", "content": None, "tool_calls": [{"id": "call_probe_1",
            "type": "function", "function": {"name": "get_weather", "arguments": "{\"city\": \"Oslo\"}"}}]},
        {"role": "tool", "tool_call_id": "call_probe_1",
         "content": "{\"temp_c\": 7, \"condition\": \"snow\", \"humidity\": 88}"},
        {"role": "user", "content": "Summarize the weather in one short sentence."}]},
        tag=f"cap_roundtrip:{model}")
    c = (d.get("choices", [{}])[0].get("message", {}) or {}).get("content", "") if code == 200 else ""
    ok = code == 200 and ("7" in c or "snow" in c.lower() or "雪" in c)
    S["roundtrip"][model] = f"{'OK' if ok else 'FAIL'}({c[:40]!r})"
    if not ok:
        add_delivery(model, "工具结果回传失效（依据: 回传 role:tool 数据 7度/雪 后, 模型回复未引用任何工具数据——代理式 agent 流程在此渠道走不通）")

def probe_grounding(base, key, model, S):
    """Google Search grounding 可用性 (双时效问: Google CEO + OpenAI 最新模型)"""
    if not S["native_prefix"]: return
    code, d = gen(base, key, model, {"contents": [{"parts": [{"text":
        "What is the current CEO of Google and What is the newest LLM Openai Model? One line."}]}],
        "tools": [{"google_search": {}}]},
        tag=f"grounding:{model}", api_prefix=S.get("native_prefix"))
    if code != 200:
        S["grounding"][model] = f"HTTP{code}"
        add_delivery(model, f"google_search 工具请求失败 HTTP{code}")
        return
    cand = (d.get("candidates") or [{}])[0]
    gm = cand.get("groundingMetadata", {})
    n_chunks = len(gm.get("groundingChunks", []))
    txt = parts_text(d)[:400]
    # 响应侧 Vertex 独占指纹（零额外请求）: retrievalQueries/sourceFlaggingUris/ragChunk/documentName
    vonly = []
    if gm.get("retrievalQueries"): vonly.append("retrievalQueries")
    if gm.get("sourceFlaggingUris"): vonly.append("sourceFlaggingUris")
    for ch in gm.get("groundingChunks", []):
        rc = ch.get("retrievedContext") or {}
        if rc.get("documentName") or rc.get("ragChunk"):
            vonly.append("retrievedContext." + ("documentName" if rc.get("documentName") else "ragChunk")); break
    S["grounding"][model] = (f"groundingChunks={n_chunks} webSearchQueries={gm.get('webSearchQueries', [])[:2]} "
                             f"vertex_only={vonly or '无'} answer={txt!r}")
    if n_chunks == 0:
        add_delivery(model, f"google_search 名存实亡: 无 groundingMetadata（依据: 真实搜索会返回 webSearchQueries/groundingChunks 引用元数据；"
                            f"无则模型只能凭训练数据作答，时效性问题会被旧知识冒充——比拒答更有害）")
    else:
        add_evidence(model, f"grounding=real({n_chunks} chunks)",
            "搜索 grounding 真实可用（依据: 返回了 webSearchQueries+groundingChunks 引用元数据）", 1, "B")
        if vonly:
            add_evidence(model, f"grounding_fields=vertex_only({'/'.join(vonly)})",
                "grounding 响应携带 Vertex 独占字段（依据: retrievalQueries/sourceFlaggingUris/ragChunk/documentName 仅定义于 Vertex proto, "
                "generativelanguage 的 GroundingMetadata 无这些字段）", 2, "B")

def probe_capability(base, key, model, S):
    """交付能力: 工具调用/结构化/流式/多轮"""
    code, d = chat(base, key, model, {"messages": [{"role": "user", "content": "Weather in Oslo? Use tool."}],
        "tools": [{"type": "function", "function": {"name": "get_weather", "description": "Get weather",
        "parameters": {"type": "object", "properties": {"city": {"type": "string"}}, "required": ["city"]}}}]},
        tag=f"cap_tool:{model}")
    tc = d.get("choices", [{}])[0].get("message", {}).get("tool_calls") if code == 200 else None
    tool_ok = bool(tc)
    try: tool_args = json.loads(tc[0]["function"]["arguments"]) if tc else None
    except Exception: tool_args = None
    code2, d2 = chat(base, key, model, {"messages": [{"role": "user",
        "content": "Extract: 'Wang Lei, 36, pilot in Xi'an.'"}],
        "response_format": {"type": "json_schema", "json_schema": {"name": "p", "strict": True,
        "schema": {"type": "object", "properties": {"name": {"type": "string"}, "age": {"type": "integer"},
        "job": {"type": "string"}, "city": {"type": "string"}},
        "required": ["name", "age", "job", "city"], "additionalProperties": False}}}}, tag=f"cap_json:{model}")
    json_ok = False
    try:
        p = json.loads(d2["choices"][0]["message"]["content"])
        json_ok = p.get("name") and p.get("age") == 36 and p.get("job") and p.get("city")
    except Exception: pass
    nonce = secrets.token_hex(8)
    sreq = {"model": model, "stream": True, "stream_options": {"include_usage": True},
            "messages": [{"role": "user", "content": f"Repeat exactly: ST-{nonce}-END"}]}
    content, chunks, usage_seen = "", 0, False
    rq = urllib.request.Request(base.rstrip("/") + OAI_PATH,
        data=json.dumps(sreq).encode(), method="POST",
        headers={"Content-Type": "application/json",
                 "Authorization": "Bearer " + key, "User-Agent": UA})
    try:
        with urllib.request.urlopen(rq, timeout=120) as resp:
            for raw in resp:
                line = raw.decode("utf-8", "replace").strip()
                if not line.startswith("data: "): continue
                if line[6:] == "[DONE]": break
                c = json.loads(line[6:]); chunks += 1
                if c.get("usage"): usage_seen = True
                if c.get("choices") and c["choices"][0].get("delta", {}).get("content"):
                    content += c["choices"][0]["delta"]["content"]
        stream_ok = nonce in content
    except Exception as e:
        stream_ok = False; chunks = -1
    code4, d4 = chat(base, key, model, {"messages": [
        {"role": "user", "content": "My ox is named Doudou. Remember."},
        {"role": "assistant", "content": "Doudou, noted!"},
        {"role": "user", "content": "Ox name? One word."}]}, tag=f"cap_multi:{model}")
    multi_ok = "doudou" in (d4.get("choices", [{}])[0].get("message", {}).get("content", "").lower()) if code4 == 200 else False
    S["capability"][model] = (f"工具调用={'OK' if tool_ok else 'FAIL'}({tool_args}) "
                              f"json_schema={'OK' if json_ok else 'FAIL'} 流式={'OK' if stream_ok else 'FAIL'}({chunks}ch) "
                              f"多轮={'OK' if multi_ok else 'FAIL'}")
    if not tool_ok: add_delivery(model, "function calling 未触发（依据: 提供 tools 后未返回 tool_calls，代理式使用不可用）")
    if not json_ok: add_delivery(model, "json_schema 结构化输出失效（依据: response_format=strict schema 下返回内容不满足 schema——"
                                      "程序化解析场景不可用）")
    if not stream_ok: add_delivery(model, "流式输出异常（依据: stream=true 下拼接内容与请求 nonce 不对应）")
    if not multi_ok: add_delivery(model, "多轮上下文失效（依据: 前轮信息未被后轮引用）")
    S["openai_stream"][model] = f"include_usage={'OK' if usage_seen else 'FAIL'}({chunks}ch)"
    if chunks > 0 and not usage_seen:
        add_delivery(model, "stream_options.include_usage 未生效 —— 流式响应没有返回 usage chunk")

def probe_native_capability(base, key, model, S):
    """原生 generateContent 基础能力: 工具调用/工具回调/maxOutputTokens/stop/system/JSON/流式/多轮
    官方依据: v1beta GenerateContentRequest(tools/toolConfig/systemInstruction/contents),
    GenerationConfig(maxOutputTokens/stopSequences/responseMimeType/responseJsonSchema),
    Part(functionCall/functionResponse/thoughtSignature), Candidate.finishReason=MAX_TOKENS,
    models.streamGenerateContent?alt=sse。"""
    if not S["native_prefix"]: return
    P = S.get("native_prefix")
    res = {}
    fn = {"name": "get_weather", "description": "Get the current weather for a city.",
          "parameters": {"type": "object", "properties": {"city": {"type": "string"}},
                         "required": ["city"]}}
    tools = [{"functionDeclarations": [fn]}]
    user_text = "What is the weather in Oslo? Use the get_weather tool."

    # A. 工具调用: toolConfig.mode=ANY 强制调用 (官方枚举 ANY/AUTO/NONE/VALIDATED)
    try:
        payload = {"contents": [{"role": "user", "parts": [{"text": user_text}]}],
                   "tools": tools,
                   "toolConfig": {"functionCallingConfig": {"mode": "ANY",
                                                            "allowedFunctionNames": ["get_weather"]}}}
        code, d = gen(base, key, model, payload, tag=f"ncap_tool:{model}", api_prefix=P)
        msg = json.dumps(d, ensure_ascii=False)
        cand = (d.get("candidates") or [{}])[0]
        fr = cand.get("finishReason", "")
        fc_part, fc = None, None
        for p in (cand.get("content") or {}).get("parts", []):
            if p.get("functionCall"): fc_part, fc = p, p["functionCall"]; break
        if code == 200 and fc:
            name, args = fc.get("name"), fc.get("args")
            ok = name == "get_weather" and isinstance(args, dict) and "oslo" in json.dumps(args, ensure_ascii=False).lower()
            res["tool_call"] = f"{'OK' if ok else 'FAIL'}(name={name}, args={json.dumps(args, ensure_ascii=False)[:50]})"
            res["thoughtSignature"] = "present" if fc_part.get("thoughtSignature") else "absent"
            # B. 工具回调: 原样回传模型 Content(保留 functionCall/thoughtSignature) + functionResponse
            try:
                model_content = dict(cand.get("content") or {})
                model_content.setdefault("role", "model")
                fresp = {"name": "get_weather",
                         "response": {"temp_c": 7, "condition": "snow", "humidity": 88}}
                if fc.get("id"): fresp["id"] = fc["id"]
                cb_payload = {"contents": [
                    {"role": "user", "parts": [{"text": user_text}]},
                    model_content,
                    {"role": "user", "parts": [{"functionResponse": fresp}]},
                ], "tools": tools}
                code2, d2 = gen(base, key, model, cb_payload, tag=f"ncap_callback:{model}", api_prefix=P)
                cand2 = (d2.get("candidates") or [{}])[0]
                txt = parts_text(d2)
                cb_fr = cand2.get("finishReason", "")
                hit = any(k in txt.lower() for k in ["7", "snow", "雪", "cold", "freez"])
                again = any(p.get("functionCall") for p in ((cand2.get("content") or {}).get("parts") or []))
                if code2 == 200 and hit:
                    res["tool_callback"] = f"OK({txt[:40]!r})"
                elif code2 == 200 and again:
                    res["tool_callback"] = f"RETURNED_FUNCTION_CALL({cb_fr})"
                elif code2 == 200:
                    res["tool_callback"] = f"FAIL(no_tool_data, finishReason={cb_fr}, text={txt[:30]!r})"
                    add_native_delivery(model, f"原生工具回调失效（依据: 回传 functionResponse 的 7℃/雪 后，模型回复未引用工具数据，finishReason={cb_fr}）")
                elif "thought signature" in json.dumps(d2, ensure_ascii=False).lower():
                    res["tool_callback"] = f"FAIL(invalid_thought_signature, HTTP{code2})"
                    add_native_delivery(model, f"原生工具回调失败：上游拒绝回传的 thoughtSignature（{json.dumps(d2, ensure_ascii=False)[:120]}）"
                                                f"—— 首次 functionCall 与回调可能被路由到不同上游/账号，签名不可跨账号复用")
                else:
                    res["tool_callback"] = f"HTTP{code2}: {json.dumps(d2, ensure_ascii=False)[:80]}"
            except Exception as e:
                res["tool_callback"] = f"ERR({str(e)[:50]})"
        elif code == 200:
            res["tool_call"] = f"FAIL(no_functionCall, finishReason={fr})"
            if fr in ("MALFORMED_FUNCTION_CALL", "UNEXPECTED_TOOL_CALL", "MISSING_THOUGHT_SIGNATURE", "TOO_MANY_TOOL_CALLS"):
                add_native_delivery(model, f"原生工具调用异常 finishReason={fr}")
            else:
                add_native_delivery(model, f"原生工具调用未触发（依据: toolConfig.mode=ANY + allowedFunctionNames 强制调用，响应无 functionCall，finishReason={fr}）")
            res["tool_callback"] = "SKIP"
        elif "Cannot find field" in msg or "Unknown name" in msg:
            res["tool_call"] = f"UNSUPPORTED(HTTP{code}, proto拒绝)"
            res["tool_callback"] = "SKIP"
        else:
            res["tool_call"] = f"HTTP{code}: {msg[:80]}"
            res["tool_callback"] = "SKIP"
    except Exception as e:
        res["tool_call"] = f"ERR({str(e)[:50]})"
        res["tool_callback"] = "SKIP"

    # C. 最大输出截断: maxOutputTokens=15 -> finishReason=MAX_TOKENS
    try:
        payload = {"contents": [{"role": "user", "parts": [{"text": "Count from 1 to 100, digits separated by commas."}]}],
                   "generationConfig": {"maxOutputTokens": 15}}
        code, d = gen(base, key, model, payload, tag=f"ncap_max:{model}", api_prefix=P)
        fr = ((d.get("candidates") or [{}])[0]).get("finishReason", "")
        if code == 200 and fr == "MAX_TOKENS":
            res["maxOutputTokens"] = "OK(MAX_TOKENS)"
        elif code == 200:
            res["maxOutputTokens"] = f"FAIL(finishReason={fr})"
            add_native_delivery(model, f"原生 maxOutputTokens 未生效（依据: maxOutputTokens=15 的请求以 finishReason={fr} 结束，未出现 MAX_TOKENS）")
        else:
            res["maxOutputTokens"] = f"HTTP{code}: {json.dumps(d, ensure_ascii=False)[:70]}"
    except Exception as e:
        res["maxOutputTokens"] = f"ERR({str(e)[:50]})"

    # D. stopSequences: 停用序列不应出现在输出中
    try:
        payload = {"contents": [{"role": "user", "parts": [{"text": "Repeat this exact string and nothing else: ALPHA|OMEGA"}]}],
                   "generationConfig": {"stopSequences": ["|"]}}
        code, d = gen(base, key, model, payload, tag=f"ncap_stop:{model}", api_prefix=P)
        txt = parts_text(d)
        if code == 200:
            if "ALPHA" in txt and ("|" in txt or "OMEGA" in txt):
                res["stop"] = f"FAIL({txt[:40]!r})"
                add_native_delivery(model, f"原生 stopSequences 未生效（依据: 请求 stopSequences=['|']，输出仍含 '|' 或 OMEGA: {txt[:40]!r}）")
            elif "ALPHA" in txt:
                res["stop"] = f"OK({txt[:40]!r})"
            else:
                res["stop"] = f"INCONCLUSIVE({txt[:40]!r})"
        else:
            res["stop"] = f"HTTP{code}: {json.dumps(d, ensure_ascii=False)[:70]}"
    except Exception as e:
        res["stop"] = f"ERR({str(e)[:50]})"

    # E. systemInstruction: 英文限定
    try:
        payload = {"systemInstruction": {"parts": [{"text": "You must answer in English only, always."}]},
                   "contents": [{"role": "user", "parts": [{"text": "你叫什么名字？一句话。"}]}]}
        code, d = gen(base, key, model, payload, tag=f"ncap_sys:{model}", api_prefix=P)
        txt = parts_text(d)
        cjk = any('\u4e00' <= ch <= '\u9fff' for ch in txt)
        if code == 200 and txt and not cjk:
            res["system"] = "遵循"
        elif code == 200 and not txt:
            res["system"] = "INCONCLUSIVE(空回复)"
        elif code == 200:
            res["system"] = f"FAIL({txt[:30]!r})"
            add_native_delivery(model, f"原生 systemInstruction 未生效（依据: 英文限定下仍输出中文: {txt[:30]!r}）")
        else:
            res["system"] = f"HTTP{code}: {json.dumps(d, ensure_ascii=False)[:70]}"
    except Exception as e:
        res["system"] = f"ERR({str(e)[:50]})"

    # F. 结构化输出: responseJsonSchema -> responseSchema -> responseFormat.text.schema
    try:
        schema = {"type": "object",
                  "properties": {"name": {"type": "string"}, "age": {"type": "integer"},
                                 "job": {"type": "string"}, "city": {"type": "string"}},
                  "required": ["name", "age", "job", "city"]}
        prompt = "Extract: 'Wang Lei, 36, pilot in Xi'an.'"
        variants = [
            ("responseJsonSchema", {"responseMimeType": "application/json", "responseJsonSchema": schema}),
            ("responseSchema", {"responseMimeType": "application/json", "responseSchema": schema}),
            ("responseFormat", {"responseFormat": {"text": {"mimeType": "application/json", "schema": schema}}}),
        ]
        out, used, errs = None, None, []
        for label, gc in variants:
            code, d = gen(base, key, model, {"contents": [{"role": "user", "parts": [{"text": prompt}]}],
                                             "generationConfig": gc},
                          tag=f"ncap_json:{model}:{label}", api_prefix=P)
            if code == 200:
                out, used = d, label; break
            errs.append(f"{label}=HTTP{code}")
        if out is None:
            res["json"] = "UNSUPPORTED(" + ", ".join(errs) + ")"
        else:
            txt = parts_text(out)
            try:
                obj = _json_from_text(txt)
                ok = obj.get("name") and obj.get("age") == 36 and obj.get("job") and obj.get("city")
                res["json"] = f"{'OK' if ok else 'FAIL'}({used}, {txt[:50]!r})"
                if not ok:
                    add_native_delivery(model, f"原生结构化输出失效（依据: {used} 下 JSON 内容不满足 schema: {txt[:50]!r}）")
            except Exception:
                res["json"] = f"FAIL({used}, 非合法JSON: {txt[:50]!r})"
                add_native_delivery(model, f"原生结构化输出失效（依据: {used} 下返回内容不是合法 JSON: {txt[:50]!r}）")
    except Exception as e:
        res["json"] = f"ERR({str(e)[:50]})"

    # G. 原生流式: :streamGenerateContent?alt=sse
    try:
        nonce = secrets.token_hex(8)
        payload = {"contents": [{"role": "user", "parts": [{"text": f"Repeat exactly this token and nothing else: ST-{nonce}-END"}]}]}
        code, chunks, err = gen_stream(base, key, model, payload, tag=f"ncap_stream:{model}", api_prefix=P)
        txt = native_stream_text(chunks)
        ok = code == 200 and nonce in txt
        if ok:
            res["stream"] = f"OK({len(chunks)}ch)"
        elif code == 200:
            res["stream"] = f"FAIL({len(chunks)}ch)"
            add_native_delivery(model, f"原生流式异常（依据: :streamGenerateContent?alt=sse 下 chunk={len(chunks)}，拼接文本与 nonce 不对应，HTTP{code}）")
        else:
            res["stream"] = f"INCONCLUSIVE(HTTP{code}, {len(chunks)}ch)"
    except Exception as e:
        res["stream"] = f"ERR({str(e)[:50]})"

    # H. 多轮上下文: 原生 role 为 user/model
    try:
        payload = {"contents": [
            {"role": "user", "parts": [{"text": "My ox is named Doudou. Remember."}]},
            {"role": "model", "parts": [{"text": "Doudou, noted!"}]},
            {"role": "user", "parts": [{"text": "Ox name? One word."}]},
        ]}
        code, d = gen(base, key, model, payload, tag=f"ncap_multi:{model}", api_prefix=P)
        txt = parts_text(d).lower()
        ok = code == 200 and "doudou" in txt
        if ok:
            res["multiturn"] = f"OK({txt[:30]!r})"
        elif code == 200:
            res["multiturn"] = f"FAIL({txt[:30]!r})"
            add_native_delivery(model, "原生多轮上下文失效（依据: 前轮告知牛名 Doudou，后轮回复未引用）")
        else:
            res["multiturn"] = f"INCONCLUSIVE(HTTP{code}, {txt[:30]!r})"
    except Exception as e:
        res["multiturn"] = f"ERR({str(e)[:50]})"

    order = ["tool_call", "tool_callback", "maxOutputTokens", "stop", "system", "json",
             "stream", "multiturn", "thoughtSignature"]
    S["native_capability"][model] = " | ".join(f"{k}={res[k]}" for k in order if k in res)

def probe_effort(base, key, model, S):
    """reasoning_effort 梯度: 按文档映射(2.5 预算档 / 3.x-4 等级档)判读"""
    levels = (["none"] + ["minimal", "low", "medium", "high"]) if _is_25(model) else \
             ["minimal", "low", "medium", "high"]
    vals, codes = {}, {}
    for eff in levels:
        code, d = chat(base, key, model, {"reasoning_effort": eff,
            "messages": [{"role": "user", "content": EFFORT_PUZZLE}]}, tag=f"effort:{model}:{eff}")
        codes[eff] = code
        vals[eff] = _reasoning_tokens(d) if code == 200 else None
    S["effort"][model] = " | ".join(
        f"{k}={v if v is not None else 'None'}(HTTP{codes[k]})" for k, v in vals.items())
    if _is_25(model):
        n = vals.get("none")
        if isinstance(n, int) and n > 0:
            add_delivery(model, f"reasoning_effort=none 未关闭思考（{n} tokens；文档: 2.5 非 Pro 支持用 none 关闭）")
    lo, mid, hi = vals.get("low"), vals.get("medium"), vals.get("high")
    if all(isinstance(x, int) for x in (lo, mid, hi)):
        if lo == 0 and mid == 0 and hi == 0:
            S["effort"][model] += " [effort 未体现]"
        elif hi < lo * 0.6:
            add_delivery(model, f"reasoning_effort 梯度严重倒挂（high={hi} < low={lo} x0.6）")
        elif hi <= lo:
            S["effort"][model] += " [梯度不明显]"

def probe_errors_fixed(base, key, model, S):
    code, d = http(base, "POST", "/v1/chat/completions",
                   {"model": "no-such-xyz-999", "messages": [{"role": "user", "content": "hi"}]},
                   key, 60, tag="err_model")
    S["errors"] = f"错模型 HTTP{code}: {json.dumps(d, ensure_ascii=False)[:110]}"
    code2, d2 = chat(base, key, model, key_override="sk-wrong-key-000", tag="err_key")
    S["errors"] += f" | 错key HTTP{code2}: {json.dumps(d2, ensure_ascii=False)[:80]}"
    m = json.dumps(d, ensure_ascii=False)
    for kw, verdict in [("antigravity", "上游泄露 antigravity 字样 —— Antigravity/IDE 账号池实锤（依据: 上游内部错误外泄，含账号状态信息）"),
                        ("distributor", "渠道自身从上级分销商进货（依据: 错误含 distributor 标记）")]:
        if kw in m.lower():
            add_evidence(model, f"error_leak={kw}", verdict, 4, "A")

def add_evidence(model, kv, rationale, weight, tier):
    EVIDENCE.setdefault(model, []).append((kv, rationale, weight, tier))

def add_delivery(model, text):
    DELIVERY.setdefault(model, []).append(text)

def add_native_delivery(model, text):
    NATIVE_DELIVERY.setdefault(model, []).append(text)

# 修复 probe_errors 里的调用
def probe_errors_fixed(base, key, model, S):
    code, d = http(base, "POST", OAI_PATH,
                   {"model": "no-such-xyz-999", "messages": [{"role": "user", "content": "hi"}]},
                   key, 60, tag="err_model")
    S["errors"] = f"错模型 HTTP{code}: {json.dumps(d, ensure_ascii=False)[:110]}"
    r = urllib.request.Request(base.rstrip("/") + OAI_PATH,
        data=json.dumps({"model": model, "messages": [{"role": "user", "content": "hi"}]}).encode(),
        method="POST", headers={"Content-Type": "application/json", "Authorization": "Bearer sk-wrong-key-000",
                                "User-Agent": UA})
    try:
        with urllib.request.urlopen(r, timeout=60) as resp:
            code2 = resp.status; b2 = resp.read().decode()[:80]
    except urllib.error.HTTPError as e:
        code2 = e.code; b2 = e.read().decode()[:80]
    except Exception as e:
        code2 = -1; b2 = str(e)[:60]
    S["errors"] += f" | 错key HTTP{code2}: {b2}"
    m = json.dumps(d, ensure_ascii=False).lower()
    if "antigravity" in m:
        add_evidence(model, "error_leak=antigravity",
            "上游泄露 antigravity 字样 —— Antigravity/IDE OAuth 账号池实锤（依据: 上游内部错误外泄，含账号协议与状态信息，无法从外部伪造动机）", 4, "A")
    if "distributor" in m:
        add_evidence(model, "error_leak=distributor",
            "渠道自述从上级分销商(distributor)进货 —— 链路存在中间层（依据: 错误文案自带标记）", 1, "B")

# ---------------- 判源引擎 ----------------

def verdict_source(model):
    evs = EVIDENCE.get(model, [])
    kv = {e[0].split("=")[0]: e for e in evs}
    score = {"vertex": 0, "devapi": 0}
    lines = []
    def sc(side, pts, line=None):
        score[side] += pts
        if line: lines.append(line)
    e = kv.get("gs_real_image", None)
    if e and "READ_OK" in e[0]:
        sc("vertex", 6, f"├ gs_real_image={e[0].split('=',1)[1]} （GCS直读能力实证, Developer API 架构上不具备 → +6 A类）")
    e = kv.get("gs_fake")
    if e and "registerfile" in e[0]:
        sc("devapi", 6, f"├ gs_fake=RegisterFile拒绝 （Developer API 标志行为, 双向文档核实 → +6 A类）")
    elif e and "generic" in e[0]:
        sc("vertex", 3, f"├ gs_fake={e[0].split('=',1)[1]} （无 RegisterFile 拒绝 = 缺 Developer API 标志行为 → +3 B类）")
    e = kv.get("files_uri")
    if e and "registry_403" in e[0]:
        sc("devapi", 4, "├ files_uri=Files注册表专属403 （generativelanguage Files 服务语义 → +4 A类）")
    elif e and "generic_403" in e[0]:
        sc("vertex", 2, "├ files_uri=通用IAM403 （无 Files 服务语义 → +2 B类）")
    elif e and "accepted" in e[0]:
        sc("devapi", 4, "├ files_uri=accepted （Files 资源可解析 → +4 A类）")
    e = kv.get("https_404")
    if e: lines.append(f"├ {e[0]} （v3审计: 非Vertex独占, 中性观测 → 不计分）")
    e = kv.get("gs_missing_obj")
    if e and "gcs_native" in e[0]:
        sc("vertex", 2, "├ gs_missing_obj=GCS原生404文本 （拉取到达真实 GCS → +2 B类）")
    e = kv.get("grounding_fields")
    if e and "vertex_only" in e[0]:
        sc("vertex", 2, "├ grounding_fields=vertex_only （grounding 响应携带 retrievalQueries/sourceFlaggingUris/ragChunk 等 Vertex 独占字段 → +2 B类）")
    e = kv.get("vertex_retrieval_e2e")
    if e: lines.append(f"├ {e[0]} （旧版独立 E2E 证据, 已并入 tools_vertex_e2e → 不计分）")
    e = kv.get("tools_vertex_retrieval")
    if e and "recognized" in e[0]:
        lines.append(f"├ {e[0]} （v3审计: 接受≠执行, 翻译层白名单可放行工具名并返回 200 → 仅观测不计分, 执行与否交由 tools_vertex_e2e 验证）")
    elif e and "rejected" in e[0]:
        sc("devapi", 4, "├ tools_vertex_retrieval=rejected （retrieval 容器 Cannot find field —— generativelanguage proto 无此字段, Developer API 标志行为, 官方 proto 一手核验 → +4 A类）")
    e = kv.get("tools_vertex_e2e")
    if e and "OK(" in e[0]:
        sc("vertex", 4, "├ tools_vertex_e2e=OK （Vertex 独占工具端到端执行: groundingChunks[].retrievedContext/retrievalQueries 仅 Vertex proto 定义, "
                        "开发者 API 无法产生且 google_search 重路由填不出 → +4 A类）")
    elif e:
        lines.append(f"├ {e[0]} （工具被接受但强制检索无 Vertex 独占 grounding 元数据, 接受≠执行 → 不计分, 疑似白名单翻译层）")
    e = kv.get("tools_vertex_e2e_web")
    if e and "OK(" in e[0]:
        sc("vertex", 2, "├ tools_vertex_e2e_web=OK （entWebSearch/parallelAiSearch 端到端执行; webSearchQueries/web chunks 与 devapi google_search 响应重叠, "
                        "重路由可伪造 → 降权 +2 B类, 与 proto 级接受证据叠加才构成强指向）")
    e = kv.get("tools_as_fields")
    if e and "missing_strict" in e[0]:
        sc("vertex", 2, "├ tools_as_fields=missing_strict （fileSearch/mcpServers 双拒且 urlContext 接受 —— as 独占字段缺失, Vertex 形状自洽 → +2 B类）")
    elif e and "present" in e[0]:
        sc("devapi", 2, "├ tools_as_fields=present （fileSearch/mcpServers 被接受 —— generativelanguage 较新字段集 → +2 B类）")
    e = kv.get("tools_probe")
    if e and "invalidated" in e[0]:
        lines.append("⚠ tools 探针作废: zzFakeTool 对照被接受, 翻译层剥离 tools（判据失效, 不计分）")
    e = kv.get("gsr_deprecated")
    if e: lines.append(f"├ {e[0]} （中性观测 → 不计分）")
    e = kv.get("error_leak")
    if e:
        if "antigravity" in e[0]:
            lines.append("├ error_leak=antigravity （上游 Antigravity OAuth 池实锤 → 归入 CodeAssist 系, 覆盖其他判读）")
            return "Antigravity/CodeAssist 账号池（上游内部错误实锤）", "high", lines, score
    e = kv.get("error_leak=distributor")
    if e: lines.append("├ error_leak=distributor （链路有中间分销层 → 所有判据按'隔一层'打折）")
    e = kv.get("field_family")
    if e: lines.append(f"├ {e[0]} （族划分观察）")
    e = kv.get("fields_proto")
    fields_vertex = bool(e and "vertex_shape" in e[0])
    if fields_vertex:
        sc("vertex", 4, "├ fields_proto=vertex_shape （trafficType+createTime 仅 Vertex proto 定义, 官方一手核验 → +4 A类）")
    e = kv.get("serviceTier_only")
    if e: lines.append(f"├ {e[0]} （不可归因字段 → 不计分）")
    lines.append(f"├ score: vertex={score['vertex']} devapi={score['devapi']}")
    if fields_vertex and score["devapi"] >= 4 and score["vertex"] < score["devapi"]:
        return ("无法定案（字段-行为证据冲突: 字段形状=Vertex[官方proto一手], 行为=Developer API; "
                "疑似 Vertex 后端+DeveloperAPI 语义文件层的混合内部通道, 未证实）"), "low", lines, score
    if score["vertex"] >= 6 and score["vertex"] > score["devapi"] + 2:
        return "Vertex（GCS 直连能力实证）", "high" if score["vertex"] >= 9 else "medium-high", lines, score
    if score["devapi"] >= 6 and score["devapi"] > score["vertex"] + 2:
        return "Developer API（AI Studio 系）", "high" if score["devapi"] >= 9 else "medium-high", lines, score
    if score["vertex"] >= 3 and score["devapi"] >= 3:
        return "无法定案（行为证据冲突）", "low", lines, score
    return "无法定案（证据不足或判据被中间层失效）", "low", lines, score

def verdict_delivery(model):
    ds = DELIVERY.get(model, [])
    if not ds:
        return "🟢【交付合格】—— 请求参数与交付物对应, 未发现静默改写"
    sev = "🔴🔴" if len(ds) >= 3 else "🔴"
    head = f"{sev}【交付缺陷 x{len(ds)}】—— 以下请求-响应不对应:"
    return head + "\n" + "\n".join(f"             ⟹ {d}" for d in ds)

def verdict_native_delivery(model):
    ds = NATIVE_DELIVERY.get(model, [])
    if not ds:
        return "🟢【原生端点交付合格】—— 原生 generateContent 基础能力未发现静默改写"
    sev = "🔴🔴" if len(ds) >= 3 else "🔴"
    head = f"{sev}【原生端点缺陷 x{len(ds)}】—— 以下原生请求-响应不对应:"
    return head + "\n" + "\n".join(f"             ⟹ [原生] {d}" for d in ds)

# ---------------- 主流程 ----------------

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", required=True)
    ap.add_argument("--key", required=True)
    ap.add_argument("--models", default="gemini-3.8-flash")
    ap.add_argument("--concurrency", type=int, default=3)
    ap.add_argument("--samples", type=int, default=12)
    ap.add_argument("--out-dir", default="./probes")
    ap.add_argument("--vertex-datastore", default=None,
                    help="E2E 真检索 datastore 完整资源名; 缺省用内置 Google 文档示例（大概率 inconclusive）")
    ap.add_argument("--vertex-rag-corpus", default=None,
                    help="E2E 真检索 ragCorpus 完整资源名; 缺省用内置 Google 文档示例（大概率 inconclusive）")
    a = ap.parse_args()
    global OAI_PATH
    if is_official(a.url):
        OAI_PATH = "/v1beta/openai/chat/completions"
    host = urlparse(a.url).netloc.replace(":", "_")
    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    models = [m.strip() for m in a.models.split(",") if m.strip()]
    S = {"sweep": {}, "nonce": {}, "native": {}, "fields": {}, "family": {}, "tools": {},
         "grounding": {}, "capability": {}, "native_capability": {}, "effort": {}, "params": {},
         "roundtrip": {}, "gs_scene": {}, "openai_vision": {}, "openai_tools": {},
         "openai_stream": {}, "openai_extra": {}, "errors": "", "native_prefix": None}
    import os
    os.makedirs(a.out_dir, exist_ok=True)
    outfile = f"{a.out_dir}/gemini-source-check-{host}-{ts}.txt"

    L = []
    def P(s=""):
        L.append(s); print(s)

    P("############ 渠道总览 ############")
    P(f"目标: {a.url}  time={datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    if is_official(a.url):
        P("官方端点模式: 原生鉴权=x-goog-api-key, OpenAI 兼容层=/v1beta/openai/chat/completions")
    P(f"计划测试: {len(models)} 个模型, 并发={a.concurrency}, 采样={a.samples}")

    # 阶段1: 并发探活
    P("")
    P("— model_sweep（渠道级探活）—")
    with ThreadPoolExecutor(max_workers=max(a.concurrency, len(models))) as ex:
        list(ex.map(lambda m: probe_sweep(a.url, a.key, m, S), models))
    for m in models:
        P(f"  {m:28s} | {S['sweep'].get(m,'?')}")
    alive = [m for m in models if "HTTP200" in S["sweep"].get(m, "")]
    if not alive:
        P("所有模型探活失败, 终止。"); open(outfile, "w", encoding="utf-8").write("\n".join(L)); return

    # 阶段2: 每模型并发跑探针组
    for m in alive:
        P("")
        P("=" * 78)
        P(f"############ 探针组 —— {m} ############")
        probe_native(a.url, a.key, m, S)
        P(f"— native: {S['native'].get(m)}")
        tasks = [lambda mm=m: probe_nonce(a.url, a.key, mm, S),
                 lambda mm=m: probe_fields(a.url, a.key, mm, S, a.samples),
                 lambda mm=m: probe_files(a.url, a.key, mm, S),
                 lambda mm=m: probe_tools(a.url, a.key, mm, S, a.vertex_datastore, a.vertex_rag_corpus),
                 lambda mm=m: probe_grounding(a.url, a.key, mm, S),
                 lambda mm=m: probe_capability(a.url, a.key, mm, S),
                 lambda mm=m: probe_native_capability(a.url, a.key, mm, S),
                 lambda mm=m: probe_effort(a.url, a.key, mm, S),
                 lambda mm=m: probe_params(a.url, a.key, mm, S),
                 lambda mm=m: probe_tool_roundtrip(a.url, a.key, mm, S),
                 lambda mm=m: probe_openai_compat(a.url, a.key, mm, S),
                 lambda mm=m: probe_errors_fixed(a.url, a.key, mm, S)]
        with ThreadPoolExecutor(max_workers=a.concurrency) as ex:
            futs = [ex.submit(t) for t in tasks]
            for f in as_completed(futs):
                try: f.result()
                except Exception as e:
                    P(f"  [probe异常] {e}")
        for k in ["nonce", "fields", "tools", "grounding", "capability", "native_capability", "effort", "params", "roundtrip", "gs_scene", "openai_vision", "openai_tools", "openai_stream", "openai_extra"]:
            if m in S.get(k, {}):
                P(f"— {k}: {S[k][m]}")
        P(f"— errors: {S['errors']}")

    # 阶段3: 分层判读
    for m in alive:
        P("")
        P("=" * 78)
        P(f"############ 分层判读 (VERDICT) —— {m} ############")
        P(f"⓪ 交付判定   : {verdict_delivery(m)}")
        P(f"⓪b 原生交付判定: {verdict_native_delivery(m)}")
        src, conf, lines, score = verdict_source(m)
        P(f"① 渠道判源   : 【{src}】  置信度 {conf}")
        for ln in lines: P(f"             {ln}")
        P(f"② 探针明细   :")
        for k in ["native", "fields", "tools", "grounding", "capability", "native_capability", "effort", "params", "roundtrip", "gs_scene", "openai_vision", "openai_tools", "openai_stream", "openai_extra"]:
            if m in S.get(k, {}): P(f"             ├ {k}: {S[k][m]}")
        P(f"             ├ errors: {S['errors']}")
        P("③ 证据-依据对照:")
        for kv, why, w, tier in EVIDENCE.get(m, []):
            P(f"             ├ {kv}  [{tier}类 w={w}]")
            P(f"             │   依据: {why}")

    P("")
    P("=" * 78)
    P(f"############ 原始请求响应报文 (共{len(RAW_LOG)}条) ############")
    for entry in RAW_LOG:
        P(entry)

    open(outfile, "w", encoding="utf-8").write("\n".join(L))
    print(f"\n[报告已写入] {outfile}")

if __name__ == "__main__":
    main()
