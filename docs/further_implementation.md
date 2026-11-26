# ScholarLink further 分支实现方案

## 1. 分支目标

`further` 分支把 ScholarLink 从静态实验结果展示扩展为一条可以交互演示的研究发现链路：

```text
研究问题
  -> 意图识别
  -> ConceptNet 概念扩展
  -> Acemap 论文检索
  -> OpenAlex 元数据/引用补充（可选）
  -> baseline / RRF / graph_rerank 对比
  -> 论文、作者、机构和主题关系探索
  -> 证据摘要或 DeepSeek RAG 回答（可选）
  -> 授权画像下的个性化排序
```

默认仍然是离线优先模式。没有 API Key 时，页面使用仓库中的快照和本地证据摘要，所有原有验证流程继续可用。

## 2. 已实现的接口层

新增 `src/api_clients.py`，包含三个部分：

- `OpenAlexClient`：按查询或论文标识获取元数据、摘要、作者、机构、主题和引用信息；
- `DeepSeekClient`：调用 OpenAI 兼容的 `/chat/completions` 接口；
- `build_deepseek_messages`：将论文和图谱证据组织成 evidence-first 提示词。

接口层只使用 Python 标准库，不包含密钥，不会在导入时发起网络请求。`DeepSeekClient` 要求密钥来自环境变量，默认变量名为 `DEEPSEEK_API_KEY`。生成结果仍应交给 `src/rag.py` 的引用校验和降级逻辑处理。

API 配置模板位于 `config/api.example.json`：

```json
{
  "schema_version": "api-config-v1",
  "mode": "offline",
  "providers": {
    "openalex": {
      "enabled": false,
      "base_url": "https://api.openalex.org",
      "mailto": ""
    },
    "deepseek": {
      "enabled": false,
      "base_url": "",
      "chat_path": "/chat/completions",
      "model": "deepseek-chat",
      "api_key_env": "DEEPSEEK_API_KEY"
    }
  }
}
```

复制为 `config/api.local.json` 后填写自己的地址；该文件已加入 `.gitignore`，不会被提交。校验配置时不会联网，也不会打印密钥：

```powershell
python -m src check-api
python -m src check-api --config config/api.local.json
python -m src check-api --config config/api.local.json --output results/api_status.json
```

## 3. LLM 开关和回答模式

前端提供三种模式：

| 模式 | 网络调用 | 行为 |
| --- | --- | --- |
| `off` | 否 | 只展示检索、图谱和排序，不生成回答 |
| `extractive` | 否 | 从论文摘要和图谱事实生成确定性的证据摘要 |
| `llm` | 是（配置后） | 调用 DeepSeek，要求每条事实陈述携带证据引用 |

API 未配置、网络请求失败或回答引用不在上下文中时，系统应回退为 `extractive`，并在结果中记录 `generation.mode` 和错误原因。无论是否打开 LLM，论文检索和图谱浏览都应该保持可用。

建议把回答模式写入每次演示会话：

```json
{
  "answer_mode": "extractive",
  "generation": {
    "provider": "none",
    "grounded": true,
    "citation_count": 5
  }
}
```

## 4. 可视化完整链路

`docs/visualization/index.html` 已更新为双语交互式演示，默认从现有快照加载：

1. 查询输入和查询意图标签：主题、领域、时间和研究目标；
2. ConceptNet 概念与论文关系图；
3. Baseline、RRF、Graph Rerank 排名切换；
4. 论文排名变化和分数分解；
5. 作者、机构、主题、摘要和证据路径详情；
6. `off`、`extractive`、`DeepSeek` 三种回答模式；
7. 两个演示研究画像及其对排名的影响；
8. Acemap、ConceptNet、OpenAlex 和 DeepSeek 的来源/配置状态；
9. 离线快照与在线 API 模式提示。

页面默认使用 `robotics` 作为有完整图谱证据的演示查询。等 OpenAlex 或 DeepSeek 配置完成后，再把在线返回结果接入相同的展示结构。在线模式必须经过后端代理，不能把密钥放入 HTML。

运行可视化页面：

```powershell
cd Acemap
python -m http.server 8000
```

浏览器打开：

```text
http://localhost:8000/docs/visualization/
```

## 5. 推荐的后端接入方式

浏览器只访问项目自己的服务，第三方 API 由后端适配器访问。建议后续提供以下接口：

```text
GET  /api/search?q=robotics&ranker=graph_rerank
GET  /api/works/{id}
GET  /api/graph/{id}
POST /api/rag
POST /api/personalize
```

`POST /api/rag` 的请求至少包含 `query`、`papers`、`graph_facts` 和 `answer_mode`。后端只把经过限制的证据上下文发送给 DeepSeek，不发送未经筛选的完整数据库或用户历史。

OpenAlex 适合补充论文元数据和引用关系，Acemap 继续负责主检索。Semantic Scholar 可以作为后续的可选引用补充源，Crossref 主要用于 DOI 校验，不应让多个 API 同时决定最终排序。

## 6. 数据、缓存与可复现性

每个在线响应都应保存请求参数、采集时间、响应摘要和 provider 名称。建议沿用现有快照结构：

```json
{
  "provider": "openalex",
  "request": "...",
  "retrieved_at": "...",
  "response_hash": "...",
  "raw_response": {}
}
```

回归测试和论文实验使用 `--cache-only` 或固定 JSON 快照；在线模式只用于交互演示和数据更新。这样 API 不稳定、限流或字段变化不会破坏已有的 v2/v3 结果。

## 7. 验收标准

- 不填 API 配置时，页面可以正常使用离线快照；
- 关闭 LLM 时，检索、图谱和证据摘要仍然可用；
- DeepSeek 调用失败时自动回退到证据摘要；
- 回答中的引用必须来自当前论文或图谱证据；
- 排序器切换能显示排名变化，而不是只替换标题；
- 个性化关闭或未授权时，个性化分量为 0；
- API 配置检查不发起网络请求、不打印密钥；
- `verify_results.py` 和完整回归测试继续通过。

## 8. 验证命令

```powershell
python -m src check-api
python .\verify_results.py
python -B -m unittest discover -s tests -v
```

当前分支只提供 OpenAlex 和 DeepSeek 的安全接口、离线回退和可视化接入点。真正的服务地址、模型名和 `DEEPSEEK_API_KEY` 由使用者在本地配置。
