#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""probe_tools v3（接受性探测 + Vertex 独占工具 E2E 强制调用验证）/ verdict_source 离线契约测试。

全程无网络: mock 掉 gsc.gen / gsc.gen_retry / gsc.log_raw，按 (tools[0] JSON 指纹, prompt) 回放 canned 响应。
v3 契约(2026-11-11): 接受≠执行 —— 翻译层白名单可放行工具名返回 200, 因此:
  - tools_vertex_retrieval=accepted(...) 降为观测(0分), 仅 proto 语义级拒绝(rejected)保留 devapi +4 A
  - E2E_OK(检索诱导后命中 Vertex 独占 grounding 元数据)才计分: ctx 字段 +4 A / web 字段 +2 B
"""
import importlib.util
import json
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
spec = importlib.util.spec_from_file_location("gsc_vtools", os.path.join(HERE, "gemini_source_check.py"))
gsc = importlib.util.module_from_spec(spec)
sys.modules["gsc_vtools"] = gsc
spec.loader.exec_module(gsc)

OK_BODY = {"candidates": [{"content": {"role": "model", "parts": [{"text": "hi"}]},
                           "finishReason": "STOP"}]}
FIELD_INVALID = {"error": {"code": 400, "message":
    "The GenerateContentRequest proto is invalid:\n"
    "  * tools[0].retrieval.vertex_ai_search.datastore: [FIELD_INVALID] datastore and engine are mutually exclusive"}}
def cnf(name):
    return {"error": {"code": 400,
            "message": f'Invalid JSON payload received. Unknown name "{name}" at \'tools[0]\': Cannot find field.'}}
GSR_DEPRECATED = {"error": {"code": 400,
    "message": "Unable to submit request because google_search_retrieval is not supported; "
               "please use google_search field instead."}}

# E2E 成功响应: Vertex 独占 ctx 字段(retrievedContext/retrievalQueries) / web 字段(webSearchQueries/web chunks)
GROUND_BODY_CTX = {"candidates": [{"content": {"role": "model", "parts": [{"text": "doc summary"}]},
                                   "finishReason": "STOP",
                                   "groundingMetadata": {"retrievalQueries": ["q"],
                                                         "groundingChunks": [{"retrievedContext": {"title": "t"}}]}}]}
GROUND_BODY_WEB = {"candidates": [{"content": {"role": "model", "parts": [{"text": "web answer"}]},
                                   "finishReason": "STOP",
                                   "groundingMetadata": {"webSearchQueries": ["next solar eclipse"],
                                                         "groundingChunks": [{"web": {"uri": "https://x", "title": "t"}}]}}]}

# E2E 提示词(与 gemini_source_check.py probe_tools 内联字面量一致)
P_DATASTORE = "Search the datastore and summarize what you find in one line."
P_CORPUS = "Search the corpus and summarize what you find in one line."
P_ECLIPSE = "When is the next total solar eclipse in the United States?"
P_F1 = "Who won the most recent Formula 1 Grand Prix? Search for it."
RAG_E2E_TOOL = {"retrieval": {"vertexRagStore": {"ragResources": [{"ragCorpus": gsc.DEFAULT_RAG_CORPUS}]}}}


def K(tool_obj, prompt="hi"):
    """matrix key: (tools[0] JSON 指纹, prompt 文本)"""
    return (json.dumps(tool_obj, sort_keys=True), prompt)


def make_responder(matrix, calls=None):
    """matrix: {(tools[0] JSON, prompt): (code, body)}; 其余请求返回 200 OK_BODY"""
    default = (200, OK_BODY)
    def respond(base, key, model, payload, timeout=120, tag="gen", api_prefix=None, **kw):
        tj = json.dumps(payload.get("tools", [{}])[0], sort_keys=True)
        pt = (payload.get("contents") or [{}])[0].get("parts", [{}])[0].get("text", "")
        k = (tj, pt)
        if calls is not None: calls.append(k)
        return matrix.get(k, default)
    return respond


class VertexToolsTestBase(unittest.TestCase):
    def setUp(self):
        self._old = (gsc.gen, gsc.gen_retry, gsc.gen_stream, gsc.log_raw,
                     gsc.EVIDENCE, gsc.DELIVERY, gsc.NATIVE_DELIVERY, gsc.RAW_LOG)
        gsc.log_raw = lambda *a, **k: None
        gsc.EVIDENCE, gsc.DELIVERY, gsc.NATIVE_DELIVERY = {}, {}, {}

    def tearDown(self):
        (gsc.gen, gsc.gen_retry, gsc.gen_stream, gsc.log_raw,
         gsc.EVIDENCE, gsc.DELIVERY, gsc.NATIVE_DELIVERY, gsc.RAW_LOG) = self._old

    @staticmethod
    def run_probe_tools(matrix):
        gsc.gen = gsc.gen_retry = make_responder(matrix)
        S = {"native_prefix": "/v1beta/models", "tools": {}, "grounding": {}}
        gsc.probe_tools("https://x", "k", "m", S)
        return S

    @staticmethod
    def ev_map():
        return {e[0].split("=")[0]: e for e in gsc.EVIDENCE.get("m", [])}


class TestVertexChannelFixture(VertexToolsTestBase):
    def test_vertex_fixture_scores_vertex_with_zero_devapi(self):
        matrix = {K(obj): r for obj, r in [
            ({"retrieval": {"vertexAiSearch": {}}}, (400, FIELD_INVALID)),
            ({"retrieval": {"vertexRagStore": {}}}, (200, OK_BODY)),
            ({"retrieval": {"externalApi": {}}}, (200, OK_BODY)),
            ({"fileSearch": {}}, (400, cnf("fileSearch"))),
            ({"mcpServers": []}, (400, cnf("mcpServers"))),
            ({"zzFakeTool": {}}, (400, cnf("zzFakeTool"))),
            ({"googleSearchRetrieval": {}}, (400, GSR_DEPRECATED)),
        ]}
        # E2E: rag ctx 命中 / ent+par web 命中
        matrix[K(RAG_E2E_TOOL, P_CORPUS)] = (200, GROUND_BODY_CTX)
        matrix[K({"enterpriseWebSearch": {}}, P_ECLIPSE)] = (200, GROUND_BODY_WEB)
        matrix[K({"parallelAiSearch": {}}, P_F1)] = (200, GROUND_BODY_WEB)
        S = self.run_probe_tools(matrix)
        self.assertIn("vertex_retrieval=proto_invalid/ok/ok", S["tools"]["m"])
        self.assertIn("as_fields=unknown_field/unknown_field", S["tools"]["m"])
        self.assertIn("e2e(rag=E2E_OK", S["tools"]["m"])
        ev = self.ev_map()
        # v3: 接受仅观测, E2E 才计分
        self.assertIn("tools_vertex_retrieval", ev)
        self.assertEqual((ev["tools_vertex_retrieval"][2], ev["tools_vertex_retrieval"][3]), (0, "观测"))
        self.assertIn("accepted", ev["tools_vertex_retrieval"][0])
        self.assertEqual((ev["tools_vertex_e2e"][2], ev["tools_vertex_e2e"][3]), (4, "A"))
        self.assertEqual((ev["tools_vertex_e2e_web"][2], ev["tools_vertex_e2e_web"][3]), (2, "B"))
        self.assertEqual(ev["tools_as_fields"][2], 2)
        self.assertEqual(ev["gsr_deprecated"][2], 0)
        src, conf, lines, score = gsc.verdict_source("m")
        self.assertTrue(src.startswith("Vertex"))
        self.assertGreaterEqual(score["vertex"], 8)  # as_fields 2 + e2e 4 + e2e_web 2
        self.assertEqual(score["devapi"], 0)


class TestAsChannelFixture(VertexToolsTestBase):
    def test_as_fixture_scores_devapi_with_zero_vertex(self):
        matrix = {K(obj): r for obj, r in [
            ({"retrieval": {"vertexAiSearch": {}}}, (400, cnf("retrieval"))),
            ({"retrieval": {"vertexRagStore": {}}}, (400, cnf("retrieval"))),
            ({"retrieval": {"externalApi": {}}}, (400, cnf("retrieval"))),
            ({"fileSearch": {}}, (200, OK_BODY)),
            ({"mcpServers": []}, (200, OK_BODY)),
            ({"zzFakeTool": {}}, (400, cnf("zzFakeTool"))),
            ({"googleSearchRetrieval": {}}, (400, cnf("googleSearchRetrieval"))),
            ({"enterpriseWebSearch": {}}, (400, cnf("enterpriseWebSearch"))),
            ({"parallelAiSearch": {}}, (400, cnf("parallelAiSearch"))),
        ]}
        S = self.run_probe_tools(matrix)
        ev = self.ev_map()
        self.assertEqual((ev["tools_vertex_retrieval"][2], ev["tools_vertex_retrieval"][3]), (4, "A"))
        self.assertEqual(ev["tools_as_fields"][0], "tools_as_fields=present")
        # Vertex 工具全部被拒 -> 不触发 E2E
        self.assertNotIn("tools_vertex_e2e", ev)
        self.assertNotIn("e2e(", S["tools"]["m"])
        src, conf, lines, score = gsc.verdict_source("m")
        self.assertTrue(src.startswith("Developer API"))
        self.assertEqual(score["vertex"], 0)
        self.assertGreaterEqual(score["devapi"], 6)


class TestWhitelistChannelFixture(VertexToolsTestBase):
    """sui-xiang 形态: 白名单翻译层放行全部 Vertex 工具名(200)但检索不发生。"""

    def test_accepted_without_grounding_is_observation_only(self):
        S = self.run_probe_tools({})  # 全部 200 OK_BODY(无 grounding 元数据), zzFakeTool 也 200 -> 作废分支
        # 作废对照在前: 全 200 时探针整体作废, 不做 E2E
        ev = self.ev_map()
        self.assertIn("tools_probe", ev)

    def test_whitelist_accepts_tools_but_no_grounding_is_unscored(self):
        matrix = {K({"zzFakeTool": {}}): (400, cnf("zzFakeTool"))}  # 对照正常拒绝, 其余默认 200
        S = self.run_probe_tools(matrix)
        ev = self.ev_map()
        self.assertIn("tools_vertex_retrieval", ev)
        self.assertEqual(ev["tools_vertex_retrieval"][2], 0)
        self.assertIn("tools_vertex_e2e=accepted_no_grounding", ev["tools_vertex_e2e"][0])
        self.assertEqual(ev["tools_vertex_e2e"][2], 0)
        self.assertIn("e2e(vais=accepted_no_grounding", S["tools"]["m"])
        src, conf, lines, score = gsc.verdict_source("m")
        self.assertEqual(score["vertex"], 0)
        self.assertTrue(src.startswith("无法定案"))


class TestRegressionLegacyNoVertexFields(VertexToolsTestBase):
    def test_legacy_top_level_rejection_must_not_credit_devapi(self):
        # 旧版引擎曾因顶层 vertexAiSearch Cannot find field 给 devapi +2 —— 修正后必须归零
        gsc.EVIDENCE["m"] = [("tools_proto=no_vertex_fields(strict)", "旧版误判证据", 2, "B")]
        src, conf, lines, score = gsc.verdict_source("m")
        self.assertEqual(score["devapi"], 0)
        self.assertEqual(score["vertex"], 0)


class TestInvalidatedControl(VertexToolsTestBase):
    def test_fake_tool_accepted_invalidates_probe_without_scoring(self):
        S = self.run_probe_tools({})  # 全部 200 → zzFakeTool 也被接受
        ev = self.ev_map()
        self.assertIn("tools_probe", ev)
        self.assertEqual(ev["tools_probe"][2], 0)
        self.assertNotIn("tools_vertex_retrieval", ev)
        self.assertIn("ctrl=ok", S["tools"]["m"])
        src, conf, lines, score = gsc.verdict_source("m")
        self.assertEqual((score["vertex"], score["devapi"]), (0, 0))
        self.assertTrue(src.startswith("无法定案"))


class TestE2EWithinProbeTools(VertexToolsTestBase):
    """v3: E2E 已并入 probe_tools —— 权限失败 inconclusive 不计分; ctx 命中 +4 A。"""

    def _run(self, e2e_matrix):
        matrix = {K(obj): r for obj, r in [
            ({"fileSearch": {}}, (400, cnf("fileSearch"))),
            ({"mcpServers": []}, (400, cnf("mcpServers"))),
            ({"zzFakeTool": {}}, (400, cnf("zzFakeTool"))),
            ({"googleSearchRetrieval": {}}, (400, cnf("googleSearchRetrieval"))),
        ]}
        matrix.update(e2e_matrix)  # 验收性 Vertex 工具默认 200, E2E 结果由 e2e_matrix 决定
        return self.run_probe_tools(matrix)

    def test_permission_failure_is_inconclusive_and_unscored(self):
        S = self._run({K(RAG_E2E_TOOL, P_CORPUS): (403, {"error": {"message": "Permission denied"}})})
        self.assertIn("rag=inconclusive(HTTP403)", S["tools"]["m"])
        ev = self.ev_map()
        # vais E2E 默认 200 无 grounding -> 0 分白名单观测; rag 403 -> inconclusive; 无任何 E2E 计分
        self.assertEqual(ev["tools_vertex_e2e"][2], 0)
        self.assertNotIn("OK(", ev["tools_vertex_e2e"][0])
        self.assertNotIn("tools_vertex_e2e_web", ev)
        _, _, _, score = gsc.verdict_source("m")
        self.assertEqual(score["vertex"], 2)  # 仅 as_fields=missing_strict +2, E2E 零贡献

    def test_real_grounding_hits_scores_a4(self):
        S = self._run({K(RAG_E2E_TOOL, P_CORPUS): (200, GROUND_BODY_CTX)})
        self.assertIn("rag=E2E_OK", S["tools"]["m"])
        ev = self.ev_map()
        self.assertEqual((ev["tools_vertex_e2e"][2], ev["tools_vertex_e2e"][3]), (4, "A"))
        _, _, _, score = gsc.verdict_source("m")
        self.assertEqual(score["vertex"], 6)  # as_fields=missing_strict 2 + e2e ctx 4
        self.assertEqual(score["devapi"], 0)


class TestGroundingResponseFingerprint(VertexToolsTestBase):
    def _run(self, gm_extra):
        body = {"candidates": [{"content": {"role": "model", "parts": [{"text": "Sundar Pichai"}]},
                                "finishReason": "STOP",
                                "groundingMetadata": dict({
                                    "webSearchQueries": ["current CEO of Google"],
                                    "groundingChunks": [{"web": {"uri": "https://x", "title": "t"}}],
                                    "groundingSupports": [{"segment": {}}]}, **gm_extra)}]}
        gsc.gen = lambda *a, **k: (200, body)
        S = {"native_prefix": "/v1beta/models", "tools": {}, "grounding": {}}
        gsc.probe_grounding("https://x", "k", "m", S)
        return S

    def test_vertex_only_fields_are_credited(self):
        S = self._run({"retrievalQueries": ["ceo google"]})
        ev = {e[0].split("=")[0]: e for e in gsc.EVIDENCE["m"]}
        self.assertIn("grounding_fields", ev)
        self.assertEqual(ev["grounding_fields"][2], 2)
        self.assertIn("vertex_only=['retrievalQueries']", S["grounding"]["m"])
        _, _, _, score = gsc.verdict_source("m")
        self.assertEqual(score["vertex"], 2)
        self.assertEqual(score["devapi"], 0)

    def test_plain_web_grounding_is_not_credited(self):
        S = self._run({})
        self.assertNotIn("grounding_fields", {e[0].split("=")[0]: e for e in gsc.EVIDENCE["m"]})
        self.assertIn("vertex_only=无", S["grounding"]["m"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
