# ScholarLink 在线 API 与数据处理指南

## 启动可视化与 API

在 `Acemap` 目录执行：

```powershell
python -m src serve --host 127.0.0.1 --port 8000
```

浏览器访问：

```text
http://127.0.0.1:8000/docs/visualization/
```

该命令同时提供静态页面和本地 API 网关：

```text
GET  /api/health
GET  /api/status
GET  /api/search?q=robotics&ranker=graph_rerank
GET  /api/works/{openalex_id}
GET  /api/graph/{concept}
POST /api/rag
```

浏览器只访问本地网关，第三方密钥保留在 Python 进程中。直接使用 `python -m http.server` 仍然可以打开离线快照，但不会提供这些 API 路由。

## 配置 OpenAlex 和 DeepSeek

```powershell
Copy-Item config/api.example.json config/api.local.json -Force
```

编辑 `config/api.local.json`，按需启用 provider。DeepSeek 的 `base_url` 应填写兼容 `/chat/completions` 的服务地址，密钥通过环境变量提供：

```powershell
$env:DEEPSEEK_API_KEY = "填写自己的密钥"
python -m src check-api --config config/api.local.json
python -m src serve --host 127.0.0.1 --port 8000
```

配置检查不会发起网络请求。回答模式选择 `DeepSeek API` 后，页面会把当前查询、论文和图谱证据发送给 `/api/rag`；服务不可用时自动回退到带引用的 extractive 摘要。

## 从原始 ConceptNet 重建数据

原始文件应位于 `data/raw/conceptnet-assertions-5.7.0.csv.gz`。完整重建命令如下：

```powershell
python .\acemap_search.py build-index `
  --assertions .\data\raw\conceptnet-assertions-5.7.0.csv.gz `
  --output .\data\conceptnet_en.sqlite

python -m src clean-graph `
  --input .\data\conceptnet_en.sqlite `
  --output .\data\conceptnet_clean.sqlite `
  --quality-report .\results\graph_quality.json `
  --degree-cap 500 --min-weight 1
```

当前已生成的清洗图版本为 `g-5d13ee8117eae85f`，包含 2,460,898 条边和 989,059 个节点。`data/*.sqlite`、原始压缩包和缓存属于可重建数据，不纳入 Git 版本控制。

## 生成查询、图算法和 RAG 结果

有缓存时可以完全离线重放：

```powershell
python .\acemap_search.py search `
  --queries .\config\dynamic_queries.txt `
  --schema-version 3 --ranker graph_rerank `
  --knowledge local --index .\data\conceptnet_en.sqlite `
  --cache-only --output .\results\dynamic_queries_v3.json

python .\acemap_search.py multihop `
  --results .\results\dynamic_queries_v3.json `
  --output .\results\dynamic_queries_multihop.json --cache-only

python .\acemap_search.py graph-analyze `
  --edges .\data\conceptnet_cache\<robotics-snapshot>.json `
  --seeds robotics --algorithm pagerank `
  --output .\results\graph_analysis.json

python .\acemap_search.py rag --query robotics `
  --results .\results\dynamic_queries_v3.json `
  --output .\results\rag.json
```

个性化结果必须由用户明确授权历史记录后生成；没有历史记录时只能生成禁用状态的 profile：

```powershell
python .\acemap_search.py profile --history .\config\history.json --consent `
  --output .\results\profile.json
python .\acemap_search.py personalize `
  --results .\results\dynamic_queries_v3.json `
  --history .\config\history.json --consent `
  --output .\results\personalized.json
```

## 验证

```powershell
python -B verify_results.py
python -B -m unittest discover -s tests -v
```

验证会禁止网络并检查 ConceptNet、Acemap 缓存能否逐字节重放。当前本地缓存已经覆盖三组演示查询；新的任意研究问题需要在线网关访问 ConceptNet 和 Acemap，首次成功请求会写入本地缓存。
