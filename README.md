# ScholarLink

**Personalized Scholarly Search and Retrieval-Augmented Question Answering via Knowledge Graph Curation and Mining**

ScholarLink 是一个面向学术检索的可复现原型。系统以 Acemap 论文搜索为基础，用 ConceptNet 做查询概念扩展，再通过多种排序器、图数据挖掘、证据约束的 RAG 和显式授权的研究画像，把一次检索组织成一条可以解释的流程：

```text
研究问题
  → 查询理解
  → ConceptNet 概念扩展
  → Acemap 论文召回
  → baseline / RRF / graph rerank
  → 作者、机构、主题和图关系探索
  → 带引用的研究总结
  → 可选的个性化排序
```

## 1. 项目能做什么

- 输入关键词或自然语言研究问题，解析主题、领域、时间等查询意图。
- 从 ConceptNet 读取一跳邻居，扩展同义词和相关概念，并保留扩展来源。
- 调用或读取 Acemap/OpenAlex 风格的论文结果，按 DOI、论文 ID 和规范化标题去重。
- 对比原始顺序、RRF 融合和 `graph_rerank`，解释某篇论文为什么排名变化。
- 在局部图上运行 PageRank、加权路径、Common Neighbors、Adamic-Adar、社区和元路径分析。
- 根据论文和知识图谱证据生成带引用的抽取式回答；可选接入 DeepSeek，失败时自动回退到证据摘要。
- 只有在用户显式授权并提供历史记录时才启用研究画像和个性化排序。
- 提供中英双语可视化页面，以及供页面调用的本地 API 网关。

## 2. 五个拓展阶段

### 阶段一：查询理解与检索重排

`src/query_understanding.py` 将查询拆为原词、领域词、时间约束和候选扩展；`src/rankers.py` 提供 `baseline`、`rrf` 和 `graph_rerank`。阶段一保留原始 v2 输出，v3 同时利用文本、图谱和时间新鲜度特征，便于做消融实验。

### 阶段二：知识图谱清洗与扩充

`clean-graph` 会过滤非英文和低权重边、合并重复三元组、限制高阶节点度数，并写出 `graph-quality-v1` 质量报告。扩展边默认写入独立表，只有显式传入 `--merge-extensions` 才会进入查询边集合。

### 阶段三：图数据挖掘

`graph-analyze` 在局部快照上运行 PageRank、加权路径、邻居相似度、社区和元路径算法。输出包含算法参数、候选节点、分数和可追溯路径，可用于解释概念为什么被扩展。

### 阶段四：知识图谱增强 RAG

`src/rag.py` 将图谱事实和论文元数据整理为带来源的证据上下文。默认使用确定性的抽取式回答，并检查引用是否来自输入证据；配置 DeepSeek 后可切换生成式回答，模型不可用或输出缺少引用时自动回退。

### 阶段五：个性化学术搜索

`src/profile.py` 从用户主动提供的历史记录中构建作者、机构、主题和关键词画像，并使用时间衰减调整排序。没有 `--consent` 时画像权重为零；空历史示例只用于展示协议和关闭状态。

## 3. 后续拓展方向

1. 接入 OpenAlex 的完整作者、机构、主题和引用网络，并用 Semantic Scholar/Crossref 做可选补充和 DOI 校验。
2. 在本地 API 网关后接入 DeepSeek 或其他兼容 `/chat/completions` 的模型，增加流式回答、模型选择和 token 用量记录。
3. 将当前局部图分析扩展为按领域预计算的图索引，并增加人工审核的 qrels、时间切分和误差分析。
4. 让可视化支持任意研究问题的实时查询，展示“问题 → 意图 → 概念 → 论文 → 排名解释 → 关系图 → 引用回答 → 画像切换”的完整链路。
5. 增加用户可删除的研究历史、画像导出和多用户隔离，并把隐私同意状态写入审计日志。

## 4. 环境要求

- Python 3.10 或更高版本
- Windows PowerShell、Linux 或 macOS 均可
- 核心离线流程只使用 Python 标准库
- 在线论文、ConceptNet 或 DeepSeek 功能需要网络和相应配置

```powershell
git clone https://github.com/Beginner-666/Acemap.git
cd Acemap
```

## 5. 最快复现：不联网验证

仓库中的正式结果是冻结快照。验证脚本不会请求 ConceptNet、Acemap 或 LLM，也不会修改正式结果：

```powershell
python -B verify_results.py
python -B -m unittest discover -s tests -v
```

验证内容包括输出协议、查询覆盖、图谱证据、去重、失败回退、排序器、RAG、画像和图清洗规则。`results/*_replay.json` 属于临时重放文件，不纳入版本控制。

## 6. 从原始数据重建本地索引

从 ConceptNet 官方下载页面获取 `conceptnet-assertions-5.7.0.csv.gz`，放入 `data/raw/`。原始压缩包、SQLite 数据库和缓存都可重建，因此默认不提交 GitHub。

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

本项目已生成过一份 `clean-v1` 快照：输入边约 265 万条，清洗后约 246 万条，节点约 99 万个；具体计数、规则和 `graph_id` 以 [`results/graph_quality.json`](results/graph_quality.json) 为准。默认搜索仍使用原始索引，只有显式传入 `--index data/conceptnet_clean.sqlite` 才使用清洗图。

## 7. 生成搜索、图算法和 RAG 结果

下面的命令可以使用已有缓存离线运行；第一次处理新查询时需要在线访问 ConceptNet 和论文服务。

```powershell
# 阶段一：v3 查询理解与 graph rerank
python .\acemap_search.py search `
  --queries .\config\dynamic_queries.txt `
  --schema-version 3 --ranker graph_rerank `
  --knowledge local --index .\data\conceptnet_en.sqlite `
  --cache-only --output .\results\dynamic_queries_v3.json

# 一跳作者/机构扩展
python .\acemap_search.py multihop `
  --results .\results\dynamic_queries_v3.json `
  --output .\results\dynamic_queries_multihop.json --cache-only

# 阶段三：对 ConceptNet 局部快照运行图算法
python .\acemap_search.py graph-analyze `
  --edges .\data\conceptnet_cache\<snapshot>.json `
  --seeds robotics --algorithm pagerank `
  --output .\results\graph_analysis.json

# 阶段四：生成带证据的离线回答
python .\acemap_search.py rag --query robotics `
  --results .\results\dynamic_queries_v3.json `
  --output .\results\rag.json

# 阶段五：显式授权后构建画像并应用个性化排序
python .\acemap_search.py profile `
  --history .\config\history.json --consent `
  --output .\results\profile.json
python .\acemap_search.py personalize `
  --results .\results\dynamic_queries_v3.json `
  --history .\config\history.json --consent `
  --output .\results\personalized.json
```

`--cache-only` 会禁止网络请求；`--knowledge local` 只读取本地 ConceptNet 索引；`--knowledge auto` 会优先在线、失败后回退本地；`--knowledge online` 强制使用在线服务。

## 8. 启动可视化和本地 API

直接双击 HTML 只能查看离线快照。要启用任意研究问题查询、论文详情、图谱查询和可选 LLM，应在项目根目录启动本地网关：

```powershell
python -m src serve --host 127.0.0.1 --port 8000
```

浏览器打开 <http://127.0.0.1:8000/docs/visualization/>。网关提供：

```text
GET  /api/health
GET  /api/status
GET  /api/search?q=robotics&ranker=graph_rerank
GET  /api/works/{openalex_id}
GET  /api/graph/{concept}
POST /api/rag
```

浏览器只访问本地网关，第三方密钥留在 Python 进程中。完整接口和数据流见 [`docs/online_and_data_pipeline.md`](docs/online_and_data_pipeline.md)。

## 9. 配置可选 API

复制模板并填写自己的配置：

```powershell
Copy-Item config/api.example.json config/api.local.json -Force
$env:DEEPSEEK_API_KEY = "填写自己的密钥"
python -m src check-api --config config/api.local.json
```

`config/api.local.json`、`.env` 和密钥均已加入忽略规则，不要提交到仓库。DeepSeek 的 `base_url` 应指向兼容 `/chat/completions` 的服务地址。API 配置检查不会发起网络请求，也不会打印密钥；页面选择 DeepSeek 模式后，服务不可用会自动回退到带引用的抽取式回答。

## 10. 结果文件

以下结果适合提交到 GitHub，既能展示功能，也能支持离线复现：

| 文件 | 用途 |
| --- | --- |
| `dynamic_queries*.json` | v2/v3 查询、概念扩展和多跳结果 |
| `eval_baseline.json` / `eval_rrf.json` / `eval_graph_rerank.json` | 三种排序器在固定查询集上的结果 |
| `evaluation_report.json` | Precision、Recall、nDCG、MRR 等对比报告 |
| `qrels_review.json` | qrels 候选和人工复核信息 |
| `dynamic_verification*.json` | 正式结果校验统计 |
| `graph_quality.json` | 清洗规则、图规模和质量统计 |
| `graph_analysis.json` | PageRank 等图算法的可追溯候选 |
| `rag.json` | 带图谱/论文引用的 RAG 示例 |
| `results/migrations/` | 缓存键和图版本迁移清单 |

`profile.json` 和 `personalized.json` 是空历史、未授权状态的协议示例；它们可以保留用于说明隐私开关，但不代表真实用户个性化效果。原始数据、SQLite、API 缓存和临时 replay 文件不提交。

## 11. 目录结构

```text
Acemap/
├─ src/                         核心搜索、图算法、RAG、画像和 API 网关
├─ config/                      排序、评估、查询和 API 模板
├─ data/raw/                    用户下载的原始 ConceptNet 文件
├─ data/*sqlite                 本地可重建索引（被 gitignore 忽略）
├─ data/*cache                  论文/图谱请求缓存（被 gitignore 忽略）
├─ results/                     冻结结果和质量报告
├─ docs/visualization/          双语可视化页面
├─ docs/                       协议、实现和数据处理文档
├─ tests/                       回归测试
└─ verify_results.py            离线结果校验入口
```

## 12. 数据、许可和当前限制

ConceptNet assertions 来自官方公开数据，边的来源和许可信息随索引保留；论文元数据来自 Acemap/OpenAlex 风格接口。使用和再分发数据时请遵守对应数据源的许可条款。

当前 qrels 仍包含规则辅助和人工复核待办项，评估数字用于比较排序器，不能单独视为绝对质量结论。完整 ConceptNet 和任意新研究问题需要本地数据或在线服务；GitHub 版本只包含代码、文档、轻量结果和可验证快照，不包含约 1.96 GB/3.76 GB 的 SQLite 数据库。

## 13. 相关文档

- [`docs/objective.md`](docs/objective.md)：原始任务目标
- [`docs/followup_implementation.md`](docs/followup_implementation.md)：五个阶段的实现路线
- [`docs/further_implementation.md`](docs/further_implementation.md)：`further` 分支方案、API 开关和可视化链路
- [`docs/interface_protocol.md`](docs/interface_protocol.md)：输出协议和版本约束
- [`docs/online_and_data_pipeline.md`](docs/online_and_data_pipeline.md)：在线 API 与完整数据处理
- [`docs/project_overview_and_extensions.md`](docs/project_overview_and_extensions.md)：项目介绍和拓展点

本次只整理文档和版本控制规则，未执行 `git push`。