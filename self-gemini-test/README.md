# self-gemini-test — Gemini 中转渠道纯度/判源测试

对 Gemini 中转渠道做黑盒探测，回答三个问题：**上游是 Developer API（AI Studio 系）还是 Vertex AI**、**请求参数与交付物是否对应**（静默改写/参数失效）、**同一模型名下是否混池**。仅用标准库，Windows/Linux 通用。

## 用法

```bash
python -I gemini_source_check.py --url https://中转域名 --key sk-xxx \
    [--models gemini-3.8-flash,gemini-3.7-flash] \
    [--vertex-datastore projects/.../dataStores/...] \
    [--vertex-rag-corpus projects/.../ragCorpora/...]
```

- 输出：`probes/gemini-source-check-{host}-{时间戳}.txt`（分层判读 + 证据依据 + 全部原始请求响应报文）
- `--vertex-datastore/--vertex-rag-corpus` 缺省用 Google 文档示例资源（多数渠道凭证无权访问，失败记 inconclusive 不计分）
- 离线契约测试：`python -m pytest test_vertex_tools.py`（全程 mock，无网络请求）

## 判读结构

1. **交付判定**：请求-响应不对应的静默改写（stop/max_tokens 失效、json_schema 违约、vision 编造、cached_content 被剥离等）
2. **判源评分**：Developer API vs Vertex 的证据计分，冲突时输出「无法定案」并给出冲突原因
3. **证据-依据对照**：每条证据附判据出处（官方 proto discovery / 文档一手核验日期）

## v3 核心规则：Vertex 独占工具「接受 ≠ 执行」

翻译层白名单可以放行工具名并返回 200（实测渠道 sui-xiang 放行全部 `retrieval.*` 而检索未发生），但 proto 语义级拒绝文本（`Cannot find field`）不可伪造。因此：

| 证据 | 判定 |
|---|---|
| `retrieval.*` 被 proto 拒绝（Cannot find field） | devapi +4 A |
| Vertex 独占工具被接受（200） | **0 分观测**，疑似白名单 |
| E2E 强制检索后命中 `retrievedContext`/`retrievalQueries` | vertex +4 A（仅 Vertex proto 定义，google_search 重路由填不出） |
| E2E 命中 `webSearchQueries`/`groundingChunks[].web` | vertex +2 B（字段与 devapi 重叠，降权） |

E2E 判定全部以元数据字段为硬标准，答案内容切题只记录不判定。

## 其他判据（A 类硬证据）

- `gs://` 假桶被 `FileService.RegisterFile` 拒绝 → Developer API 标志行为 +6
- `files/` URI 返回 Files 注册表专属 403 → Developer API +4
- 真实公开 GCS 图直读成功 → Vertex +6（Developer API 架构上不支持裸 gs://）
- 错误报文泄露 `antigravity` → Antigravity/CodeAssist 账号池实锤

## 注意

- Windows 控制台/重定向默认 GBK，脚本已内置 `sys.stdout.reconfigure(encoding="utf-8")`，无需手动设环境变量
- 渠道探活失败（401/无可用渠道）会在阶段 1 终止，报告照常落盘
- `probes/` 内的历史报告为各渠道实测底账，不参与测试逻辑
