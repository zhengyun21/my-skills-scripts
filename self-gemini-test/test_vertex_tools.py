#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""probe_tools v2（正确形状 retrieval.*）/ verdict_source / probe_vertex_retrieval 离线契约测试。

全程无网络: mock 掉 gsc.gen / gsc.gen_retry / gsc.log_raw，按 tools[0] 的 JSON 指纹回放 canned 响应。
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


def make_responder(matrix, calls=None):
    """matrix: {tools[0] 的 JSON: (code, body)}; 其余工具返回 200 OK_BODY"""
    default = (200, OK_BODY)
    def respond(base, key, model, payload, timeout=120, tag="gen", api_prefix=None, **kw):
        tj = json.dumps(payload.get("tools", [{}])[0], sort_keys=True)
        if calls is not None: calls.append(tj)
        return matrix.get(tj, default)
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
        S = {"native_prefix": "/v1beta/models", "tools": {}, "vertex_retrieval": {}, "grounding": {}}
        gsc.probe_tools("https://x", "k", "m", S)
        return S


class TestVertexChannelFixture(VertexToolsTestBase):
    def test_vertex_fixture_scores_vertex_with_zero_devapi(self):
        matrix = {json.dumps(obj, sort_keys=True): r for obj, r in [
            ({"retrieval": {"vertexAiSearch": {}}}, (400, FIELD_INVALID)),
            ({"retrieval": {"vertexRagStore": {}}}, (200, OK_BODY)),
            ({"retrieval": {"externalApi": {}}}, (200, OK_BODY)),
            ({"fileSearch": {}}, (400, cnf("fileSearch"))),
            ({"mcpServers": []}, (400, cnf("mcpServers"))),
            ({"zzFakeTool": {}}, (400, cnf("zzFakeTool"))),
            ({"googleSearchRetrieval": {}}, (400, GSR_DEPRECATED)),
        ]}
        S = self.run_probe_tools(matrix)
        self.assertIn("vertex_retrieval=proto_invalid/ok/ok", S["tools"]["m"])
        self.assertIn("as_fields=unknown_field/unknown_field", S["tools"]["m"])
        ev = {e[0].split("=")[0]: e for e in gsc.EVIDENCE["m"]}
        self.assertIn("tools_vertex_retrieval", ev)
        self.assertEqual((ev["tools_vertex_retrieval"][2], ev["tools_vertex_retrieval"][3]), (5, "A"))
        self.assertEqual(ev["tools_as_fields"][2], 2)
        self.assertEqual(ev["gsr_deprecated"][2], 0)
        src, conf, lines, score = gsc.verdict_source("m")
        self.assertTrue(src.startswith("Vertex"))
        self.assertGreaterEqual(score["vertex"], 7)
        self.assertEqual(score["devapi"], 0)


class TestAsChannelFixture(VertexToolsTestBase):
    def test_as_fixture_scores_devapi_with_zero_vertex(self):
        matrix = {json.dumps(obj, sort_keys=True): r for obj, r in [
            ({"retrieval": {"vertexAiSearch": {}}}, (400, cnf("retrieval"))),
            ({"retrieval": {"vertexRagStore": {}}}, (400, cnf("retrieval"))),
            ({"retrieval": {"externalApi": {}}}, (400, cnf("retrieval"))),
            ({"fileSearch": {}}, (200, OK_BODY)),
            ({"mcpServers": []}, (200, OK_BODY)),
            ({"zzFakeTool": {}}, (400, cnf("zzFakeTool"))),
            ({"googleSearchRetrieval": {}}, (400, cnf("googleSearchRetrieval"))),
        ]}
        S = self.run_probe_tools(matrix)
        ev = {e[0].split("=")[0]: e for e in gsc.EVIDENCE["m"]}
        self.assertEqual((ev["tools_vertex_retrieval"][2], ev["tools_vertex_retrieval"][3]), (4, "A"))
        self.assertEqual(ev["tools_as_fields"][0], "tools_as_fields=present")
        src, conf, lines, score = gsc.verdict_source("m")
        self.assertTrue(src.startswith("Developer API"))
        self.assertEqual(score["vertex"], 0)
        self.assertGreaterEqual(score["devapi"], 6)


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
        ev = {e[0].split("=")[0]: e for e in gsc.EVIDENCE["m"]}
        self.assertIn("tools_probe", ev)
        self.assertEqual(ev["tools_probe"][2], 0)
        self.assertNotIn("tools_vertex_retrieval", ev)
        self.assertIn("ctrl=ok", S["tools"]["m"])
        src, conf, lines, score = gsc.verdict_source("m")
        self.assertEqual((score["vertex"], score["devapi"]), (0, 0))
        self.assertTrue(src.startswith("无法定案"))


class TestE2ERetrieval(VertexToolsTestBase):
    def setUp(self):
        super().setUp()
        self.S = {"native_prefix": "/v1beta/models", "tools": {}, "vertex_retrieval": {}, "grounding": {}}

    def test_permission_failure_is_inconclusive_and_unscored(self):
        gsc.gen_retry = lambda *a, **k: (403, {"error": {"message": "Permission denied"}})
        gsc.probe_vertex_retrieval("https://x", "k", "m", self.S)
        self.assertEqual(self.S["vertex_retrieval"]["m"], "vais=inconclusive(HTTP403) rag=inconclusive(HTTP403)")
        self.assertNotIn("m", gsc.EVIDENCE)

    def test_real_grounding_hits_scores_a4(self):
        body = {"candidates": [{"content": {"role": "model", "parts": [{"text": "doc"}]},
                                "finishReason": "STOP",
                                "groundingMetadata": {"retrievalQueries": ["q"],
                                                      "groundingChunks": [{"retrievedContext": {"title": "t"}}]}}]}
        gsc.gen_retry = lambda *a, **k: (200, body)
        gsc.probe_vertex_retrieval("https://x", "k", "m", self.S)
        self.assertIn("vais=E2E_OK(ctx=True,q=True)", self.S["vertex_retrieval"]["m"])
        ev = {e[0].split("=")[0]: e for e in gsc.EVIDENCE["m"]}
        self.assertEqual((ev["vertex_retrieval_e2e"][2], ev["vertex_retrieval_e2e"][3]), (4, "A"))
        _, _, _, score = gsc.verdict_source("m")
        self.assertEqual(score["vertex"], 4)
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
        S = {"native_prefix": "/v1beta/models", "tools": {}, "vertex_retrieval": {}, "grounding": {}}
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
