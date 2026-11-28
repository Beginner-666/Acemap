# Acemap 搜索增强后续工作实现文档

本文面向 `feature/verify` 分支，规划 `docs/objective.md` 中“后续目标”的实现路径。当前分支已经完成先导目标：查询关键词、读取 ConceptNet 一跳邻居、调用 Acemap 论文搜索接口、合并去重并使用加权倒数排名融合（RRF）输出结果。后续工作应在这个可复现基线之上逐步增加精度、知识图谱质量、图算法、RAG 和个性化能力。

## 一、当前基线与后续约束

当前代码的主要职责如下：

| 模块 | 当前职责 | 后续扩展位置 |
| --- | --- | --- |
| `src/conceptnet_provider.py` | 在线或 SQLite ConceptNet 邻居查询、快照缓存 | 增加清洗后的图谱、版本和来源校验 |
| `src/conceptnet_index.py` | 从 ConceptNet assertions 构建英文 SQLite 索引 | 增加清洗、去重、关系统计和分区索引 |
| `src/edges.py` | 一跳邻居排序 | 增加图算法分数、关系类型和置信度融合 |
| `src/acemap_client.py` | Acemap 请求、重试、原始响应缓存 | 增加分页、批处理、速率限制和字段适配 |
| `src/records.py` | 响应解析、论文身份键和去重 | 增加关键词、作者、机构、摘要和时间字段提取 |
| `src/search.py` | 扩展词搜索、RRF 融合、结果输出 | 增加候选筛选、重排、解释和用户画像分数 |
| `verify_results.py` | 离线回放和字节级复现验证 | 增加评估集、指标和模型版本验证 |

后续实现必须保留以下约束：

1. 原始 Acemap 响应和图谱证据继续保存，所有扩展结果都能追溯到来源。
2. ConceptNet 在线 API 不能成为唯一依赖。正式实验使用离线索引或快照，联网只用于数据更新。
3. 保留原始词搜索结果，图谱扩展和后续重排都必须能够关闭，便于做消融实验。
4. 输出中记录查询配置、图谱版本、排序器版本、缓存文件和失败请求；未知响应结构必须显式失败。
5. 不把“返回更多论文”直接当作质量提高。质量结论必须由标注或公开相关性数据支持。

## 二、推荐的总体架构

后续搜索流程分为六个阶段：

```text
用户查询
  -> 查询规范化与意图识别
  -> ConceptNet/领域图谱候选概念生成
  -> Acemap 原始词与扩展词召回
  -> 规则过滤与去重
  -> 图谱、文本和个性化特征重排
  -> 结果解释、引用和可选 RAG 回答
```

建议把排序逻辑从 `src/search.py` 中抽象成统一接口：

```python
class Ranker:
    def rank(self, query, records, graph_context, user_context=None):
        """Return records with score, evidence and explanation."""
```

至少实现三个可切换排序器：

- `baseline`：只使用 Acemap 原始排序，用于对照；
- `rrf`：当前原词/扩展词的加权 RRF；
- `graph_rerank`：在 RRF 基础上加入图谱邻近度、关系类型、文本相似度和时间衰减。

输出建议升级为 `schema_version: 3`，保留现有字段，并为每篇结果增加：

```json
{
  "score": 0.0,
  "score_components": {
    "acemap": 0.0,
    "graph": 0.0,
    "text": 0.0,
    "personalization": 0.0
  },
  "evidence": {
    "matched_terms": [],
    "graph_paths": [],
    "matched_authors": [],
    "matched_institutions": []
  }
}
```

## 三、阶段一：改进搜索精度与召回

### 3.1 查询理解与候选扩展

先完成可解释的规则版本，再评估是否需要大模型：

1. 规范化大小写、下划线、连字符和复数形式；保留原始查询。
2. 用 ConceptNet 的 `Synonym`、`FormOf`、`DerivedFrom`、`RelatedTo` 生成候选。
3. 对每个候选保存关系类型、边权重、路径长度和原始 URI。
4. 过滤泛化词、单字符词、停用词和与领域无关的高连接度节点。
5. 为每个扩展词设置上限和最小置信度，避免一次查询触发过多 Acemap 请求。

查询意图理解可以作为可选预处理器。第一版只处理“主题词 + 时间/任务限定词”的形式，例如将 `recent research on plate tectonics` 拆成主题 `plate tectonics`、时间过滤 `recent` 和任务 `research`。拆解失败时回退原始词搜索，不得阻塞基础搜索。

### 3.2 结果筛选与重排

对每篇 Acemap 记录抽取以下字段：标题、摘要、关键词、Concepts、作者、机构、年份和 DOI。候选分数建议从下式开始：

```text
score(d|q) =
  0.45 * acemap_score(d)
  + 0.25 * graph_score(d, q)
  + 0.20 * text_score(d, q)
  + 0.10 * freshness_score(d)
```

具体定义：

- `acemap_score`：将 Acemap 返回名次转换为 `1 / (60 + rank)`，再归一化；
- `graph_score`：论文关键词或 ConceptNet 概念与查询节点的最大加权路径分数；
- `text_score`：查询与标题、摘要、关键词的 BM25 或 TF-IDF 相似度；
- `freshness_score`：只在用户指定“近期”时启用，按年份指数衰减。

所有系数都放入配置文件，不写死在排序函数中。重排结果必须保留每个分量，便于解释和调参。

### 3.3 多跳增强

在首轮结果中抽取作者和机构 ID，再调用 Acemap 作者/机构接口获得候选论文。多跳只在首轮结果达到最低相关性时触发：

```text
query -> concept -> paper -> author/institution -> related papers
```

建议限制为一跳扩展、最多 2 位高相关作者和 2 个高相关机构，并对多跳结果使用低于原始词的权重。结果中记录路径，例如 `query -> robotics -> paper -> author -> paper`，避免用户误以为多跳结果是直接匹配。

## 四、阶段二：知识图谱清洗和扩充

新增 `src/graph_clean.py`，输入原始 assertions，输出带版本信息的清洗数据集和统计报告。

### 4.1 清洗规则

第一版固定以下规则并记录每条规则删除的数量：

- 只保留英语两端和允许关系；
- 删除自环、空标签、非法 URI、非有限权重和低于阈值的边；
- 对规范化后的 `(subject, relation, object)` 去重，保留最高权重和完整来源列表；
- 合并大小写、下划线和 POS 后缀造成的重复节点，但保留原始 URI；
- 对极高连接度的泛化节点设置度上限，避免它们主导扩展；
- 删除与目标领域无关的边，或将其标记为低置信度而非直接丢弃；
- 保存 `source_line`、原始 assertion、数据集、许可证和清洗规则版本。

### 4.2 图谱扩充

扩充来源按可靠性排序：领域术语表、公开领域本体、论文关键词/Concepts、人工审核结果，最后才考虑通用在线 API。新增边必须包含：

```text
subject, relation, object, weight, source, license, confidence, created_at
```

新增数据写入独立文件或表，不能覆盖原始 ConceptNet。每轮扩充生成 manifest，记录输入文件哈希、规则版本、边数、节点数和许可证。

### 4.3 数据质量报告

每次构建索引输出：关系分布、节点度分布、孤立节点数、重复率、跨语言边数、低权重边数和领域覆盖率。对清洗前后各抽取固定样本人工检查，并将样本及结论保存到 `results/graph_quality.json`。

## 五、阶段三：在图谱上应用图数据挖掘算法

新增 `src/graph_algorithms.py`，先实现可解释、可离线复现的算法：

1. **Personalized PageRank**：以查询概念为种子，得到多跳节点重要性；
2. **加权最短路径**：把边权转换为代价，优先保留短且高权重的路径；
3. **Common Neighbors / Adamic-Adar**：发现两个候选概念的结构相似性；
4. **社区发现**：使用连通分量或 Louvain 等算法识别领域子社区；
5. **关系类型约束的路径搜索**：限制 `IsA -> RelatedTo` 等合法元路径，减少语义漂移。

算法输出统一为 `term, score, path, algorithm, parameters`。搜索阶段只读取预计算结果或小范围局部子图，避免每次查询扫描整张图。

算法评估至少包含：

- 与一跳权重排序的扩展词重合率；
- 人工标注的相关/不相关比例；
- `Precision@K`、`Recall@K`、`nDCG@K` 和 `MRR`；
- 查询延迟、索引大小和缓存命中率。

## 六、阶段四：基于知识图谱的 RAG

RAG 应作为独立命令或独立模块，不能替换可验证的论文检索结果。建议新增 `src/rag.py`，流程如下：

1. 解析用户问题，得到主题、限制条件和时间范围；
2. 从图谱检索相关概念、关系和最短证据路径；
3. 从 Acemap 获取论文候选，并按 `graph_rerank` 排序；
4. 选择带 DOI/ID 的论文片段和图谱事实作为上下文；
5. 让生成模型只基于上下文生成回答；
6. 为每个结论附论文 DOI/Acemap ID 或图谱 assertion 引用；
7. 上下文不足时返回“证据不足”，不补写未检索到的事实。

建议定义统一上下文格式：

```json
{
  "query": "...",
  "graph_facts": [{"subject": "...", "relation": "...", "object": "...", "source": "..."}],
  "papers": [{"id": "...", "title": "...", "doi": "...", "snippet": "..."}],
  "generation": {"model": "...", "prompt_version": "..."}
}
```

评估重点是引用正确率、答案覆盖率、未支持陈述率和重复检索一致性。模型调用失败时仍应输出检索结果和证据，不影响基础搜索。

## 七、阶段五：个性化搜索

个性化功能只使用用户明确授权的搜索历史或收藏，不采集与搜索无关的个人信息。建议从内容向量开始，不直接训练复杂模型：

1. 为用户历史中点击、收藏或下载的论文提取 ConceptNet 概念、关键词、作者和机构；
2. 按行为类型和时间衰减计算用户兴趣向量；
3. 将候选论文映射到同一概念空间；
4. 在排序中加入小权重的个性化分数；
5. 提供“关闭个性化”和“清除历史”选项。

推荐分数可以从以下形式开始：

```text
final_score = (1 - alpha) * graph_rerank_score + alpha * profile_score
```

`alpha` 默认较小，并在没有足够历史时自动降为 0。评估使用按时间切分的历史数据，禁止用未来行为构造过去的用户画像；指标包括 Recall@K、nDCG@K、覆盖率、多样性和新颖性。

## 八、实施顺序与验收标准

### 里程碑 M1：搜索重排

- 新增 `baseline`、`rrf`、`graph_rerank` 三种模式；
- 每条结果输出分数分量和图谱证据；
- 固定 30～50 个查询及人工相关性标注；
- 至少在 `nDCG@10` 或 `Recall@10` 上相对 baseline 有可解释改进；
- 原有 `verify_results.py` 和全部回归测试继续通过。

### 里程碑 M2：图谱质量

- 完成清洗脚本、manifest 和质量报告；
- 清洗过程离线可运行，输入哈希相同时输出字节一致；
- 记录每类过滤规则和抽样人工检查结果；
- 新索引可被 `ConceptNet` provider 读取，并保留原始证据。

### 里程碑 M3：图算法

- 至少实现 Personalized PageRank 或加权最短路径之一；
- 对比一跳排序和图算法排序的离线指标；
- 限制查询时间和内存，保存算法参数及版本。

### 里程碑 M4：RAG 原型

- 输出图谱事实、论文上下文和带来源回答；
- 无模型或模型失败时仍能输出检索证据；
- 对固定问题集统计引用正确率和未支持陈述率。

### 里程碑 M5：个性化原型

- 使用脱敏的合成历史完成端到端演示；
- 无历史用户与关闭个性化时结果等于非个性化模式；
- 通过时间切分评估，并报告覆盖率和多样性变化。

## 九、建议的新增文件和命令

建议新增以下文件，保持现有模块职责清晰：

```text
src/graph_clean.py       # 图谱清洗、去重和质量统计
src/graph_algorithms.py  # PageRank、路径和社区算法
src/rankers.py           # baseline/RRF/graph_rerank
src/evaluation.py        # qrels、指标和消融实验
src/rag.py               # 证据上下文和生成接口
src/profile.py           # 用户兴趣向量和隐私控制
config/ranking.json      # 排序权重和阈值
config/evaluation.json   # 查询集、K 值和数据切分
results/                 # 指标、质量报告和实验 manifest
```

推荐命令接口：

```powershell
python .\acemap_search.py build-index --assertions .\data\raw\conceptnet-assertions-5.7.0.csv.gz
python .\acemap_search.py clean-graph --input .\data\conceptnet_en.sqlite --output .\data\conceptnet_clean.sqlite
python .\acemap_search.py search --query "robotics" --ranker graph_rerank --cache-only
python .\acemap_search.py evaluate --queries .\config\evaluation_queries.json --qrels .\config\qrels.json
python .\acemap_search.py rag --query "recent research on robotics"
```

命令可以分阶段实现；在新命令尚未完成前，不应改变现有 `search` 和离线回放的行为。

## 十、风险与应对

| 风险 | 应对 |
| --- | --- |
| ConceptNet 在线 API 不稳定 | 以离线索引和快照为正式依赖，在线只作更新或对照 |
| 泛化节点导致语义漂移 | 关系白名单、度上限、路径约束和人工抽样 |
| 扩展词增加请求量 | 每查询扩展数上限、批量缓存、重试和速率限制 |
| Acemap 响应结构变化 | 保存原始响应，解析失败显式报错并补充 fixture |
| RAG 产生无依据回答 | 只允许使用带来源上下文，统计未支持陈述率 |
| 个性化数据泄露 | 默认关闭、数据脱敏、时间切分评估和清除接口 |
| 指标提升来自数据泄漏 | 固定查询集、独立标注集和严格的时间/用户切分 |

优先完成 M1 和 M2，再决定是否进入 RAG 或个性化。这样可以先证明搜索排序和图谱数据质量确实改善，再引入更复杂的模型和用户数据。
