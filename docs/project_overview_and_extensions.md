# ScholarLink 项目介绍与五个拓展点

## 1. 项目概述

**ScholarLink: Personalized Scholarly Search and Retrieval-Augmented Question Answering via Knowledge Graph Curation and Mining** 面向 Acemap 学术搜索场景，目标是把普通的关键词检索扩展为可解释、可复现并能够逐步个性化的学术信息发现系统。

当前实现以用户查询为入口，先对查询进行规范化和意图识别，再从 ConceptNet 中寻找相关概念，使用原始词和扩展词调用 Acemap 搜索接口，最后完成论文去重、排序和证据记录。系统同时保留原始查询结果和扩展结果，因此可以在 `baseline`、`rrf` 与 `graph_rerank` 之间切换，进行消融比较。

项目的基础能力包括：

- 从 ConceptNet 5.7.0 assertions 构建本地英文 SQLite 索引，并支持在线 API、离线索引和快照缓存；
- 对查询执行 `qu-v1` 规范化、意图拆分和一跳概念扩展；
- 调用 Acemap 获取论文，按 DOI、Acemap ID 或规范化标题去重；
- 使用原始排序、加权倒数排名融合（RRF）和图谱重排生成可解释结果；
- 保存请求参数、图谱证据、缓存来源、配置哈希和排序分数组件，支持离线重放；
- 为图谱清洗、图算法分析、知识图谱 RAG 和显式授权的个性化搜索提供独立接口。

因此，ScholarLink 当前已经形成了一个以学术搜索为核心、以知识图谱为增强层、以证据追踪和离线复现为工程约束的基础系统。RAG 和个性化模块已经提供确定性接口与安全降级路径，后续可以接入实际生成模型或更丰富的用户画像数据。

## 2. 整体技术架构

```text
用户查询
  -> 查询规范化与意图识别（qu-v1）
  -> ConceptNet 一跳概念扩展
  -> Acemap 原始词与扩展词检索
  -> 论文去重与候选合并
  -> 图谱、文本、时间特征重排
  -> 结果解释与引用证据
  -> 可选：RAG 回答 / 个性化排序
```

实现主要使用 Python 3.10+ 标准库。ConceptNet 关系保存在 SQLite 索引中，在线请求和论文响应保存在 JSON 快照中；缓存机制使正式实验可以关闭网络并逐字节回放。命令行入口是 `acemap_search.py`，核心实现位于 `src/`，测试位于 `tests/`。

项目通过版本化 schema 和 provenance（来源溯源）字段记录每次处理的上下文。例如 v3 搜索结果会写入排序器配置、图谱版本、缓存来源、扩展词和 `score_components`；图谱清洗结果会记录原始 assertion、行号、许可证和清洗规则版本。这样既便于复现实验，也能解释某篇论文为什么进入结果集。

## 3. 五个拓展点

### 拓展点一：搜索精度与召回增强

`src/query_understanding.py` 实现查询规范化和 `qu-v1` 意图拆分。系统会清理输入文本，识别年份、短语和检索意图，并通过 ConceptNet 的 `Synonym`、`FormOf`、`DerivedFrom`、`RelatedTo` 等关系生成扩展候选。停用词、泛化词、单字符词和连接度过高的噪声节点会被过滤，以减少无关召回。

候选论文可以使用三种排序器：

- `baseline`：直接保留 Acemap 原始顺序，作为最简单的对照；
- `rrf`：对原始词和扩展词的结果做加权倒数排名融合；
- `graph_rerank`：在融合分数上继续加入图谱邻近度、关系类型、文本字段和时间新鲜度等特征。

每篇结果保存分数分解和证据，便于说明扩展来源。系统还提供独立的多跳扩展接口，可以沿着“查询 → 概念 → 论文 → 作者/机构 → 相关论文”继续发现候选，但多跳结果不会覆盖冻结的 v2/v3 搜索文件。该设计让新增召回能力可以单独开启，并可通过对照实验判断它是否真正提高质量。

### 拓展点二：知识图谱清洗与扩充

`src/graph_clean.py` 提供 `clean-v1` 清洗流程。它保留英文节点和允许关系，去除自环、无效 URI、空标签及非法权重，执行三元组去重，并合并大小写、下划线和词性后缀造成的重复节点。对于超高连接度节点，系统按照权重保留最多 `degree-cap` 条边，降低通用枢纽节点带来的噪声。

清洗过程保留原始 assertion、`source_line`、许可证和规则版本；扩展边默认写入独立的 `extensions` 表，只有显式传入参数并执行合并时才会进入查询边。这使领域词表、公开本体、论文概念和人工审核内容能够按来源、许可证和置信度管理。`results/graph_quality.json` 保存边数、节点数、重复组、超度节点、孤立节点及配置哈希等质量统计。

当前一次全量清洗的统计为：输入边 2,653,040 条，去重及过滤后输出边 2,460,898 条、节点 989,059 个；重复组 74,557 个，超度节点 339 个，因度上限移除边 113,125 条，孤立节点 1,879 个。领域词表覆盖率较低时，只能说明词表对通用 ConceptNet 的覆盖范围有限，不能直接等同于图谱质量下降。

### 拓展点三：知识图谱挖掘算法

`src/graph_algorithms.py` 为局部图谱分析提供统一接口，支持 Personalized PageRank、加权最短路径、Common Neighbors、Adamic-Adar、社区相似度以及带关系类型约束的 meta-path。算法可以使用查询概念作为种子，在局部子图中计算候选概念的相关性，并返回路径证据。

各算法使用统一的结果结构：

```json
{
  "term": "...",
  "score": 0.0,
  "path": [],
  "algorithm": "...",
  "parameters": {}
}
```

这些分数可以用于生成更可靠的扩展词、解释概念之间的关系，或作为论文重排的图特征。工程上采用快照边、局部子图或预计算结果，避免每个查询都扫描完整 ConceptNet。算法输出中的参数和路径也会随结果保存，从而支持复现和人工检查。

### 拓展点四：基于知识图谱的 RAG

`src/rag.py` 将图谱事实和论文检索结果组织成带来源的证据上下文。流程包括：解析用户问题、取得图谱事实和关系路径、取得 Acemap 论文候选、用 DOI/Acemap ID/论文片段/assertion 建立上下文，再生成带引用的回答。

统一上下文包含以下字段：

```json
{
  "query": "...",
  "graph_facts": [],
  "papers": [],
  "generation": {}
}
```

当前默认使用确定性的抽取式回答：每条陈述都必须关联图谱来源或论文引用。外部生成器是可选的；如果生成器失败、返回格式不正确，或引用不在证据上下文中，系统会自动回退到抽取式回答。证据不足时输出明确的“证据不足”，避免把没有来源的内容包装成确定结论。该设计体现 evidence-first 原则，并为接入大语言模型保留了清晰的引用约束。

### 拓展点五：个性化学术搜索

`src/profile.py` 根据用户明确提供并授权的搜索历史、收藏或兴趣记录构建画像。系统从历史论文中提取概念、关键词、作者、机构和主题，并使用时间衰减降低较旧记录的影响。论文的画像匹配分数会与基础搜索分数融合：

```text
final_score = (1 - alpha) * graph_rerank_score + alpha * profile_score
```

冷启动、未获得 consent、历史为空或用户关闭个性化时，实际使用的 `alpha` 自动为 0，结果退化为基础排序。每篇论文还会保留命中的画像特征，用户可以理解排序变化的原因。画像模块不读取隐藏的系统数据，也不发起网络请求；历史记录、时间衰减参数和授权状态均写入输出，便于审计和复现。

## 4. 当前结果与验证方式

仓库中的主要实验产物包括：

- `results/dynamic_queries_v3.json`：v3 `graph_rerank` 搜索结果；
- `results/dynamic_queries_multihop.json`：独立多跳扩展结果；
- `results/evaluation_report.json`：不同排序器的指标对比；
- `results/graph_quality.json`：`clean-v1` 图谱清洗质量报告。

在 40 条固定查询和规则辅助 qrels 草稿上，`graph_rerank` 的 nDCG@10 从 0.8626 提升到 0.8879，Recall@10 从 0.3528 提升到 0.3606。qrels 仍需人工复核，因此这些数值适合用于排序器消融比较，不能单独作为最终检索质量结论。

基础验证命令如下：

```powershell
python .\verify_results.py
python -B -m unittest discover -s tests -v
```

前一个命令执行离线结果和图谱证据回放，后一个命令运行回归测试，覆盖输出协议、失败路径、排序器、查询理解、图谱清洗、RAG、个性化以及跨平台 JSON 换行等行为。

## 5. 主要模块索引

| 模块 | 作用 |
| --- | --- |
| `src/conceptnet_provider.py` | 在线/本地 ConceptNet 查询与快照缓存 |
| `src/conceptnet_index.py` | ConceptNet assertions 的 SQLite 索引 |
| `src/conceptnet_client.py`、`src/acemap_client.py` | 网络请求、重试和响应缓存 |
| `src/query_understanding.py` | 查询规范化、意图拆分和候选过滤 |
| `src/rankers.py` | baseline、RRF 与 graph_rerank |
| `src/graph_clean.py` | 图谱清洗、去重、扩展导入和质量报告 |
| `src/graph_algorithms.py` | PageRank、路径、邻居、社区和 meta-path |
| `src/rag.py` | 证据上下文、引用约束和生成降级 |
| `src/profile.py` | consent、兴趣画像、时间衰减和个性化排序 |
| `src/evaluation.py` | Precision、Recall、nDCG、MRR 和评估报告 |
| `src/verification.py` | v2/v3 输出校验与离线字节回放 |
| `acemap_search.py` | 兼容命令行启动器 |

相关接口约定和数据字段详见 [`interface_protocol.md`](interface_protocol.md)，阶段性实现说明详见 [`followup_implementation.md`](followup_implementation.md) 与 [`later_stages_implementation.md`](later_stages_implementation.md)。
