# Acemap 搜索接口协议

- 协议版本：`1`
- 状态：冻结（v2 已实现；v3 字段已冻结、未生成；M0 接口与迁移已实现）
- 适用分支：`feature/verify`
- 本文是输出、验证、缓存、图谱索引、排序器、特征、评估之间接口的**规范性说明**。实现代码与本文冲突时，v2 以现有代码和黄金文件为准并应立即修正文档；v3 以本文为准，首次实现时不得自行改字段。

---

## 0. 术语

| 术语 | 含义 |
| --- | --- |
| 规范性关键词 | “必须 / 禁止”为强制要求；“应当”为默认要求，偏离需在代码注释和测试中说明；“可以”为可选 |
| v2 输出 | 当前 `schema_version: 2` 的搜索结果文档 |
| v3 输出 | 规划中的 `schema_version: 3` 搜索结果文档，本文冻结其字段与不变量，但暂不生成 |
| 黄金文件 | 已提交、不得原地改写、用于字节级回放的 JSON 结果 |
| 字节一致 | 文件原始字节完全相同（UTF-8、LF、缩进、键顺序、尾随换行都相同） |
| `graph_id` | 图谱内容与清洗规则的稳定标识，用于缓存失效与结果溯源 |
| Ranker | 只做打分、排序、证据组装的纯计算组件，禁止联网和写文件 |

---

## 1. 通用不变量

以下规则对 v2 与 v3 均适用。

1. **原始证据必须保留**：每条结果保留 Acemap 原始记录 `paper`；ConceptNet 快照保留原始边 `metadata` 与 `assertion`；图谱路径必须能追溯到 assertion。
2. **增强必须可关闭**：不启用图谱扩展、或关闭个性化时，结果必须能退化为对应基线模式，不得依赖隐藏状态。
3. **未知结构必须显式失败**：Acemap 响应结构未知、索引清单缺失、Ranker 名称未知、配置字段非法时，必须抛出可诊断错误，禁止静默当作空结果。
4. **失败请求不得伪装成成功**：请求失败写入 `requests[]` 并反映到 `status`；不得删除失败记录。
5. **输出必须自描述**：仅凭输出文档中的 `settings` 与 `expansions` 必须能重建同一次搜索；v3 还要求内联 `ranker_config`，不得依赖之后被修改的配置文件。
6. **确定性**：同一输入、同一缓存、同一配置必须产生逐字节相同的输出。禁止在输出中写入当前时间、随机数、绝对路径或进程信息。
7. **版本不可原地修改**：已冻结黄金文件的版本禁止改写字段、顺序或数值。变更必须新增版本、新文件、新验证器。
8. **原始查询与规范化查询**：`query` 字段写入规范化后的查询（`text.normalize`：casefold、下划线转空格、压缩空白），原始输入不写入输出。
9. **论文身份**：所有去重和 qrels 使用 `records.identity()` 的结果作为 `key`，格式见 §9。
10. **JSON 键顺序**：`jsonio.write_json` 不排序键。对象字段的插入顺序是字节契约的一部分，修改插入顺序等同于修改输出协议。

---

## 2. 版本命名空间

各版本号互不替代，禁止混用：

| 常量 | 值 | 作用域 | 状态 |
| --- | --- | --- | --- |
| `OUTPUT_SCHEMA_V2` | `2` | 搜索结果文档 | 已实现、冻结 |
| `OUTPUT_SCHEMA_V3` | `3` | 搜索结果文档 | 已定义、未生成（M1） |
| `INDEX_SCHEMA_VERSION` | `1` | SQLite 图谱索引 `manifest.schema_version` | 已实现、冻结 |
| `SNAPSHOT_KEY_SCHEMA` | `2` | ConceptNet 快照缓存键 | 已实现并完成迁移 |
| `GRAPH_RULES_VERSION` | `"raw-v1"` | 当前原始断言索引 | 已实现；M2 清洗图使用 `"clean-v1"` |
| `RANKER_PROTOCOL_VERSION` | `1` | Ranker 接口形状 | 已实现（baseline/rrf；graph_rerank 待 M1） |
| `RECORD_FEATURE_VERSION` | `1` | 论文特征层 | 已实现 |
| `VERIFIER_VERSION` | `2` | 验证器行为 | 已实现（v2 回放 + v3 schema） |

`PROTOCOL_VERSION = 1` 覆盖本文全部接口。任何字段名、类型、必填性、排序规则、哈希公式的变化都必须提升 `PROTOCOL_VERSION`，并同步修改文档、验证器和测试。

---

## 3. 输出 v2 契约（已实现，冻结）

### 3.1 顶层对象

字段顺序即字节顺序：

| 顺序 | 字段 | 类型 | 必须 | 约束 |
| --- | --- | --- | --- | --- |
| 1 | `schema_version` | integer | 是 | 必须等于 `2` |
| 2 | `domain` | string | 是 | 必须为 `"query-time English ConceptNet"` |
| 3 | `endpoint` | string | 是 | 必须为 `constants.ENDPOINT` 的当前值 |
| 4 | `settings` | object | 是 | 见 §3.2 |
| 5 | `summary` | object | 是 | 见 §3.3 |
| 6 | `searches` | array | 是 | 顺序与查询文件顺序一致，见 §3.4 |

### 3.2 `settings`

| 顺序 | 字段 | 类型 | 约束 |
| --- | --- | --- | --- |
| 1 | `knowledge` | string | `auto` / `local` / `online` |
| 2 | `size_per_term` | integer | 1–100 |
| 3 | `max_expansions` | integer | ≥ 1 |
| 4 | `lexical_forms` | string | v2 固定为 `"ai -> artificial intelligence; exact phrases only"` |
| 5 | `fusion` | string | v2 固定为 `"weighted RRF: original=1, expansion=0.5, k=60"` |

v2 的 `fusion` 是描述性字符串，不是可执行配置。重放逻辑必须按 v2 规则硬编码 RRF，不得从该字符串解析权重。

### 3.3 `summary`

| 顺序 | 字段 | 类型 | 约束 |
| --- | --- | --- | --- |
| 1 | `queries` | integer | 等于 `searches` 长度 |
| 2 | `successful_queries` | integer | `status == "ok"` 的行数 |
| 3 | `successful_with_enhancement` | integer | `status == "ok"` 且 `enhancement_applied == true` 的行数 |

### 3.4 `searches[]` 行

| 顺序 | 字段 | 类型 | 约束 |
| --- | --- | --- | --- |
| 1 | `query` | string | 规范化查询，非空；同一文档内不得重复 |
| 2 | `status` | string | `ok` / `partial` / `failed`，见 §3.5 |
| 3 | `enhancement_applied` | boolean | 必须等于 `bool(expansions)` |
| 4 | `expansions` | array | 见 §3.6 |
| 5 | `warning` | string \| null | 见 §3.5 |
| 6 | `requests` | array | 见 §3.7，顺序为原词后接扩展词 |
| 7 | `metrics` | object | 见 §3.8 |
| 8 | `results` | array | 见 §3.9 |
| 9 | `conceptnet` | object | 见 §3.10，**必须位于行末** |

### 3.5 `status` 与 `warning`

- 原词请求（`requests[0]`）失败 → `status = "failed"`。
- 原词成功、任一扩展词失败 → `status = "partial"`。
- 全部成功 → `status = "ok"`。
- 无扩展词时，`warning` 最终必须为以下二者之一：
  - 有可用 ConceptNet 图谱（`conceptnet.backend != "none"`）：`"ConceptNet has no usable exact/normalized concept; original search only"`
  - 图谱不可用：`"ConceptNet unavailable; original search only"`
- 有扩展词时 `warning` 必须为 `null`。
- `search_one` 内部的临时中文 warning 会被 `run_search` 覆盖，最终文档以本节为准。

### 3.6 `expansions[]`

来自 `edges.rank_neighbors`，字段顺序：

| 顺序 | 字段 | 类型 | 约束 |
| --- | --- | --- | --- |
| 1 | `term` | string | 不得等于 query |
| 2 | `evidence` | object | 完整 ConceptNet 边，字段见 §3.10 |
| 3 | `domain_support_seeds` | array | 动态图固定为 `[]` |

顺序必须为：先按最佳边的 `edge_order` 升序（`-weight`、`assertion`、`subject_uri`、`object_uri`），再按 `term` 升序。最多 `settings.max_expansions` 条。无匹配概念时为空数组。

### 3.7 `requests[]`

成功项字段顺序：

| 顺序 | 字段 | 类型 |
| --- | --- | --- |
| 1 | `term` | string |
| 2 | `status` | string，固定 `"ok"` |
| 3 | `count` | integer，该请求去重前的记录数 |
| 4 | `cache_file` | string，Acemap 缓存文件名 |

失败项字段顺序：

| 顺序 | 字段 | 类型 |
| --- | --- | --- |
| 1 | `term` | string |
| 2 | `status` | string，固定 `"failed"` |
| 3 | `error` | string |

约束：

- 顺序必须为 `[query] + [expansions[].term]`，不得重排。
- 同一行内 `term` 不得重复。
- 失败请求不得写入 `count` 或 `cache_file`。

### 3.8 `metrics`

| 顺序 | 字段 | 定义 |
| --- | --- | --- |
| 1 | `baseline_unique` | 仅原词请求返回的唯一 `key` 数 |
| 2 | `enhanced_unique` | 原词与全部成功扩展词合并后的唯一 `key` 数 |
| 3 | `additional_unique` | `enhanced_unique` 中不属于 `baseline_unique` 的数量 |
| 4 | `failed_requests` | `requests[].status == "failed"` 的数量 |

### 3.9 `results[]`

字段顺序：

| 顺序 | 字段 | 类型 | 约束 |
| --- | --- | --- | --- |
| 1 | `key` | string | `records.identity(paper)`，文档内唯一 |
| 2 | `paper` | object | Acemap 原始记录，必须原样保留 |
| 3 | `fusion_score` | number | 有限非负数，定义见下 |
| 4 | `matches` | array | 至少 1 项；字段顺序 `term`、`rank`、`is_original` |

去重与打分规则（v2 硬编码）：

```text
terms = [query] + [expansion.term ...]
对每个 term 按返回顺序编号 rank = 1..N
同一 term 内重复 key 只保留首次出现
跨 term 合并到同一 key
fusion_score = Σ (1.0 if is_original else 0.5) / (60 + rank)
```

- `rank` 为该记录在对应 term 返回列表中的位置，从 1 开始。
- `is_original` 当且仅当 `term == query`。
- 同一结果在同一 term 下最多一个 `matches` 项。
- 排序必须为 `(-fusion_score, key)` 升序；分数相同按 `key` 字典序。
- 不得因重复记录重复累加分数。

### 3.10 `conceptnet` 快照

字段顺序：

| 顺序 | 字段 | 类型 | 约束 |
| --- | --- | --- | --- |
| 1 | `query` | string | 必须等于行 `query` |
| 2 | `matched_concept` | string \| null | 命中的规范化概念；未命中为 `null` |
| 3 | `forms_checked` | array[string] | 依次尝试的词形；首项为规范化 query |
| 4 | `backend` | string | `online` / `local` / `none` |
| 5 | `source` | object \| null | local 为索引 `manifest`；online 为 `{url, view, limit}`；none 为 `null` |
| 6 | `diagnostics` | array[string] | 按发生顺序；不得删除 |
| 7 | `edges` | array[edge] | 按 §3.10.1 排序 |
| 8 | `scope` | string | 固定为 `"up to 1000 edges per endpoint; permitted positive relations, English"` |

#### 3.10.1 `edge`

字段顺序：`assertion`、`relation`、`subject`、`object`、`subject_uri`、`object_uri`、`weight`、`metadata`。

- local 索引中的边还包含 `source_line`，位于 `metadata` 之前的既有字段顺序内，必须原样保留。
- 排序键：`(-weight, assertion, subject_uri, object_uri)`。
- 只允许 `conceptnet_index.RELATIONS` 中的关系、两端均为英语、`subject != object`、`weight` 有限且 ≥ 1。
- `metadata` 必须保存原始 API 边或原始 assertion 解析对象，不得裁剪。

### 3.11 序列化

必须使用 `src.jsonio.write_json`：

```python
json.dumps(value, ensure_ascii=False, indent=2) + "\n"
```

- UTF-8 无 BOM。
- 仅 LF 换行，文件以单个 `\n` 结尾。
- 不排序键、不排序数组（除协议明确规定的排序）。
- 原子写入：先写 `.tmp` 再替换。

### 3.12 v2 黄金文件

| 文件 | 状态 | sha256 |
| --- | --- | --- |
| `results/dynamic_queries.json` | 冻结，不得原地重新生成 | `b768aa7682cde189a926e460b3f885482d2af8e4b8764dbdb795c3a2b336cf2b` |

- `results/dynamic_queries_replay.json` 是验证器生成的可丢弃副本，不提交为基准。
- `results/dynamic_verification.json` 是验证报告，可重新生成，但不得改变其字段协议。

---

## 4. 输出 v3 契约（已定义，待 M1 实现）

v3 是**新文档**，不得把 v2 黄金文件升级为 v3。v3 首次生成时，必须同时提交新黄金文件、验证器版本和测试。

### 4.1 顶层与 `settings`

顶层字段顺序保持 v2 的 `schema_version`、`domain`、`endpoint`、`settings`、`summary`、`searches`，其中 `schema_version` 必须为 `3`。

`settings` 字段顺序：

| 顺序 | 字段 | 类型 | 必须 | 约束 |
| --- | --- | --- | --- | --- |
| 1 | `knowledge` | string | 是 | 同 v2 |
| 2 | `size_per_term` | integer | 是 | 同 v2 |
| 3 | `max_expansions` | integer | 是 | 同 v2 |
| 4 | `lexical_forms` | string | 是 | 同 v2 |
| 5 | `fusion` | object | 是 | `{k, original_weight, expansion_weight}`，见 §4.2 |
| 6 | `ranker` | string | 是 | `baseline` / `rrf` / `graph_rerank` |
| 7 | `ranker_version` | string | 是 | 非空；与实现注册表一致 |
| 8 | `ranker_config` | object | 是 | 已解析的完整配置，见 §4.2 |
| 9 | `config_hash` | string | 是 | 见 §4.3 |
| 10 | `graph_version` | string | 是 | local 为 `g-...`；online 为 `online:...`；无图为 `"none"` |
| 11 | `snapshot_key_schema` | integer | 是 | 必须等于 `2` |
| 12 | `record_feature_version` | integer | 是 | 必须等于 `1` |

### 4.2 `ranker_config`

必须内联、不得只存文件路径。下面的示例是 `graph_rerank` 的配置；`baseline` / `rrf` 必须使用 `{acemap: 1.0, graph: 0, text: 0, freshness: 0, personalization: 0}` 的权重向量，以保证 `score == acemap`。字段与类型：

```json
{
  "fusion": {
    "k": 60,
    "original_weight": 1.0,
    "expansion_weight": 0.5
  },
  "weights": {
    "acemap": 0.45,
    "graph": 0.25,
    "text": 0.20,
    "freshness": 0.10,
    "personalization": 0.0
  },
  "graph": {
    "max_path_length": 1,
    "relation_whitelist": ["/r/IsA", "/r/PartOf", "/r/HasA", "/r/RelatedTo",
                           "/r/Synonym", "/r/DerivedFrom", "/r/FormOf"],
    "degree_cap": null,
    "min_edge_weight": 1
  },
  "freshness": {
    "half_life_years": 5.0,
    "enabled_by_default": false
  },
  "personalization": {
    "alpha": 0.0
  }
}
```

约束：

- `settings.fusion` 必须与 `ranker_config.fusion` 完全相等（`k`、`original_weight`、`expansion_weight`）。
- `ranker ∈ {baseline, rrf}` 时，`weights` 必须为 `{acemap: 1.0, graph: 0, text: 0, freshness: 0, personalization: 0}`。
- `ranker == graph_rerank` 时，`weights` 按本节示例/配置解析，且必须满足求和约束。
- 所有数值必须有限；`weights` 各项非负。
- `personalization.alpha` ∈ `[0, 1]`。
- `weights.personalization` 必须等于 `personalization.alpha`；其余四项之和必须等于 `1 - alpha`（误差 ≤ 1e-9）。
- `weights` 之和必须等于 `1.0`（误差 ≤ 1e-9）。
- `fusion.k` ≥ 1，`fusion.original_weight` / `expansion_weight` ≥ 0。
- `graph.max_path_length` ≥ 1；`graph.degree_cap` 为 `null` 或 ≥ 1；`min_edge_weight` ≥ 0。
- `freshness.half_life_years` > 0。
- 关闭个性化时 `alpha == 0` 且 `weights.personalization == 0`；此时最终 `score` 必须与关闭个性化前逐值相同。

### 4.3 `config_hash`

```text
canonical = json.dumps(ranker_config, ensure_ascii=False,
                       sort_keys=True, separators=(",", ":"))
config_hash = "sha256:" + sha256(("ranker-config-v1\0" + canonical).encode("utf-8")).hexdigest()
```

验证器必须用文档内联的 `ranker_config` 重新计算并比对 `config_hash`；不得读取当前 `config/ranking.json` 代替。

### 4.4 `results[]`

字段顺序：

| 顺序 | 字段 | 类型 | 必须 | 约束 |
| --- | --- | --- | --- | --- |
| 1 | `key` | string | 是 | 同 v2；文档内唯一 |
| 2 | `paper` | object | 是 | Acemap 原始记录 |
| 3 | `score` | number | 是 | 有限、非负；加权和见 §4.5 |
| 4 | `score_components` | object | 是 | 见 §4.5 |
| 5 | `evidence` | object | 是 | 见 §4.6 |
| 6 | `matches` | array | 是 | 同 v2，保留检索来源 |

排序必须为 `(-score, key)` 升序。

### 4.5 `score_components` 与 `score`

`score_components` 必须包含且仅包含以下字段，顺序即字节顺序：

| 顺序 | 字段 | 类型 | 范围 |
| --- | --- | --- | --- |
| 1 | `acemap` | number | `[0, 1]` |
| 2 | `graph` | number | `[0, 1]` |
| 3 | `text` | number | `[0, 1]` |
| 4 | `freshness` | number | `[0, 1]` |
| 5 | `personalization` | number | `[0, 1]` |

```text
score = weights.acemap        * acemap
      + weights.graph         * graph
      + weights.text          * text
      + weights.freshness     * freshness
      + weights.personalization * personalization
```

误差必须 ≤ 1e-9。

Ranker 约束：

- `baseline`：`graph = text = freshness = personalization = 0`；`score == acemap`；结果顺序必须与 Acemap 原词返回顺序一致。
- `rrf`：`graph = text = freshness = personalization = 0`；`score == acemap`；`acemap` 由 v2 的 `fusion_score` 归一化得到，结果顺序必须与 v2 RRF 顺序一致。
- `graph_rerank`：允许五个分量非零；缺少图谱上下文时 `graph = 0`，并必须在 `warning` 中说明，不得静默切换 ranker。
- 个性化关闭时 `personalization` 必须为 `0.0`。

### 4.6 `evidence`

字段顺序：

| 顺序 | 字段 | 类型 | 约束 |
| --- | --- | --- | --- |
| 1 | `matched_terms` | array[string] | 必须是 `{query} ∪ expansions[].term` 的子集；可空 |
| 2 | `graph_paths` | array[path] | 可空；每个 path 见下 |
| 3 | `matched_authors` | array[author] | 可空；必须能在 `paper.authorships` 中找到对应 ID 或姓名 |
| 4 | `matched_institutions` | array[institution] | 可空；必须能在 `paper.authorships[].institutions` 中找到 |

`graph_paths[]` 字段顺序：

| 顺序 | 字段 | 类型 | 约束 |
| --- | --- | --- | --- |
| 1 | `nodes` | array[string] | 长度 ≥ 2；概念标签 |
| 2 | `relations` | array[string] | 长度等于 `len(nodes) - 1`；关系 URI |
| 3 | `weight` | number | 有限非负 |
| 4 | `score` | number | `[0, 1]` |
| 5 | `assertions` | array[string] | 非空；每个 assertion 必须能在该查询 `conceptnet.edges[].assertion` 中找到；若路径来自其他图版本，必须同时满足 `settings.graph_version` 可解析 |

`author` / `institution` 字段：`id`（string 或 null）、`name`（string）。顺序按匹配强度降序，强度相同按 `name` 升序。

### 4.7 v3 失败与降级

- `requests`、`status`、`warning` 语义同 v2。
- Ranker 计算失败必须显式记录：在行级 `warning` 中写明原因；不得输出伪造分数。
- `graph_rerank` 在无图谱证据时仍必须输出结果，`graph = 0`，并保留 `acemap/text/freshness` 分量。
- 未知 `ranker` 名称必须在 CLI 参数校验阶段失败，不得回退到默认 ranker。

### 4.8 v3 黄金文件

| 文件 | 状态 |
| --- | --- |
| `results/dynamic_queries_v3.json` | 尚未生成；M1 首次生成后冻结 |
| `results/dynamic_verification_v3.json` | 验证报告；不得覆盖 v2 报告 |

v3 首次生成时必须同时满足：

- 与 v2 使用同一组查询（`config/dynamic_queries.txt`）或明确记录独立评估集；
- 验证器能仅凭文档内联配置完成离线重放；
- 字节一致。

---

## 5. Ranker 接口

### 5.1 签名

```python
class Ranker(Protocol):
    name: str        # "baseline" | "rrf" | "graph_rerank"
    version: str     # 非空，例如 "rrf-v1"

    def rank(self, query, term_results, graph_context=None):
        """返回 {"results": [...], "metrics": {...}}。"""
```

### 5.2 输入 `term_results`

有序数组，顺序为原词后接扩展词：

```json
{
  "term": "robotics",
  "is_original": true,
  "status": "ok",
  "records": [{"...": "Acemap 原始记录"}],
  "cache_file": "abc.json",
  "error": null
}
```

- 失败项：`status = "failed"`、`records = []`、`cache_file = null`、`error` 为非空字符串。
- Ranker 禁止修改输入对象、禁止联网、禁止读写文件、禁止使用随机数或当前时间。
- `graph_context` 由调用方提供，至少包含 `graph_version`、`edges` 或预计算分数；为 `None` 时 `graph_rerank` 必须按 §4.7 降级。

### 5.3 输出

- `results`：v3 结果数组，字段见 §4.4。
- `metrics`：至少包含 `baseline_unique`、`enhanced_unique`、`additional_unique`、`failed_requests`，含义同 v2。
- 排序与去重规则必须由 `version` 固定；修改公式必须提升 `version`，并在代码中保留旧版本实现以支持旧黄金文件重放。

### 5.4 已冻结的版本

| `name` | `version` | 规则 |
| --- | --- | --- |
| `baseline` | `baseline-v1` | 仅使用原词请求；保留 Acemap 顺序；`acemap = (1 / (60 + rank)) / max_i(1 / (60 + rank_i))`，其余分量 0；`score == acemap` |
| `rrf` | `rrf-v1` | 复用 v2 的 `fusion_score`（k=60，原词 1.0，扩展 0.5）；`acemap = fusion_score / max(fusion_score)`（最大值为 0 时取 0）；其余分量 0；`score == acemap` |
| `graph_rerank` | `graph-rerank-v1` | 使用 §4.2 配置与 §4.5 公式；首次实现时冻结具体归一化，必须记录在实现说明中 |

`baseline` 与 `rrf` 的去重、`matches` 生成规则与 v2 相同。

---

## 6. ConceptNet 快照缓存接口

### 6.1 当前实现（v1，待迁移）

```python
key = sha256((knowledge + "\0" + term).encode()).hexdigest()
path = cache_dir / (key + ".json")
```

- `knowledge` ∈ `{auto, local, online}`。
- 快照内容是 `conceptnet` 对象（§3.10），不含缓存元数据。
- 该键不含图谱版本，切换索引后旧快照仍会命中；这是 M0 必须迁移的原因。

### 6.2 目标键（`SNAPSHOT_KEY_SCHEMA = 2`）

```text
source_id(local)  = "local:" + graph_id
source_id(online) = "online:conceptnet-api:limit=1000:v1"

key = sha256(f"v2\0{knowledge}\0{source_id}\0{term}").hexdigest()
path = cache_dir / (key + ".json")
```

- `knowledge` 必须保留在键中，因为 `auto` 与 `local` 的 `diagnostics` 可能不同，不能合并。
- `knowledge=auto` 查找顺序：先 `auto` + online 键，再 `auto` + local 键；写入时落到实际成功后端的键。
- `knowledge=local` 只查 `local:<graph_id>`；`knowledge=online` 只查 online 键。
- 快照载荷**不得**增加 `graph_id` 字段；图谱版本只存在于文件名与 provider 属性中，避免改变 v2 输出字节。
- 读取时必须校验 `snapshot["query"] == term`；不匹配立即失败。
- 失败请求不得写入缓存（现有语义）。

### 6.3 `graph_id`

```text
payload = {
  "source_name": manifest["source_name"],
  "source_sha256": manifest["source_sha256"],
  "language": manifest["language"],
  "relations": sorted(manifest["relations"]),
  "min_weight": manifest["min_weight"],
  "rules_version": "raw-v1" | "clean-v1"
}
graph_id = "g-" + sha256(canonical_json(payload)).hexdigest()[:16]
```

- `canonical_json`：UTF-8、`sort_keys=True`、`separators=(",", ":")`、无尾随换行。
- 当前索引无 `graph_id` 元数据时，必须能由 `manifest` 推导；索引缺失时使用 `"g-missing"` 哨兵，且不得写入 local 快照。
- 推导必须惰性执行：online 模式不得因索引缺失而失败或改变 `diagnostics`。

### 6.4 迁移规则（M0 已实现）

- 旧文件模式识别：对 `mode ∈ {auto, local, online}` 计算旧键 `sha256(mode + "\0" + query)`，与文件名匹配；无法匹配则报告并跳过。
- 新键冲突：若目标已存在且内容不同，必须中止，禁止静默覆盖。
- 迁移必须幂等：第二次执行不得改名、不得改内容。
- 必须生成迁移清单（旧名、新名、query、backend、graph_id、sha256、动作）并支持回滚。
- 迁移不得修改快照载荷；迁移后 `results/dynamic_queries.json` 仍须字节级回放通过。

---

## 7. 图谱索引接口

### 7.1 结构

```sql
CREATE TABLE metadata (key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE edges (assertion TEXT, subject TEXT, object TEXT, weight REAL, evidence TEXT);
CREATE INDEX by_subject ON edges(subject, weight DESC, assertion);
CREATE INDEX by_object  ON edges(object, weight DESC, assertion);
```

### 7.2 `manifest`

`metadata` 中键 `manifest` 必须为 JSON 对象，必需字段：

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `schema_version` | integer | 必须为 `1` |
| `source_name` | string | 数据源名称 |
| `source_url` | string | 数据源地址 |
| `source_sha256` | string | 原始断言文件哈希 |
| `source_file` | string | 文件名 |
| `rows_scanned` | integer | 扫描行数 |
| `edges_indexed` | integer | 入索引边数 |
| `language` | string | 当前为 `"en"` |
| `relations` | array[string] | 允许关系列表 |
| `min_weight` | number | 最小权重 |

**禁止**修改既有键或语义。新增信息必须放在独立 `metadata` 行（如 `graph_id`、`rules_version`、`parent_graph_id`），因为 local 快照的 `source` 直接嵌入 `manifest`，改 manifest 会改变 v2 输出字节。

### 7.3 读取

- 必须使用 `by_subject` / `by_object` 两个端点查询，避免全表 OR 扫描。
- 查询顺序：`ORDER BY weight DESC, assertion LIMIT 1000`。
- 两端结果按 `assertion` 去重；重复时保留同一条证据。
- 边在返回前必须按 §3.10.1 排序。

### 7.4 校验

索引校验器必须检查：

1. 表与列结构完全匹配 §7.1；
2. `manifest` 必需字段齐全、类型正确、`schema_version == 1`；
3. `count(edges) == manifest.edges_indexed`；
4. 抽样 `evidence` 可解析，且 `assertion/subject/object/relation/weight/source_line` 与列一致；
5. `by_subject`、`by_object` 索引存在；
6. 若 `metadata.graph_id` 存在，必须与 §6.3 推导值一致。

全量扫描可以作为独立命令，但不得成为每次查询的前置步骤。

### 7.5 M2 清洗图约束

- 必须可被现有 provider 读取，或 provider 与清洗图在同一次提交中一起升级。
- 必须保留 `edges.evidence` 的完整原始 assertion 与 `source_line`。
- 删除边与标记低置信度边必须二选一并写入清洗规则；不得默默改变查询时候选集。
- 必须记录 `rules_version`、`parent_graph_id`、许可证与来源。
- 清洗必须离线可复现；逻辑内容哈希一致优先于 SQLite 文件字节一致。

---

## 8. 论文特征接口

`records.identity()` 与 `records.extract_records()` 的现有输出不得改变。新增 `paper_features(record)` 只读、容错、确定性。

字段：

| 字段 | 类型 | 缺失时 |
| --- | --- | --- |
| `title` | string | `""` |
| `abstract` | string | `""` |
| `publication_date` | string \| null | `null` |
| `year` | integer \| null | `null` |
| `doi` | string \| null | `null` |
| `keywords` | array[string] | `[]` |
| `concepts` | array[object] | `[]` |
| `topics` | array[object] | `[]` |
| `authors` | array[object] | `[]` |
| `institutions` | array[object] | `[]` |
| `venue` | string \| null | `null` |
| `cited_by_count` | integer \| null | `null` |

规则：

- `year` 优先取 `publication_year`；否则从 `publication_date` 的前 4 位解析；非法值为 `null`。
- `doi` 必须与 `records.identity()` 使用同一个归一化函数，去除 `https://doi.org/`、`http://dx.doi.org/`、`doi:` 前缀并 casefold。
- `authors` 来自 `authorships[].author`，保留 `id`、`display_name`、`orcid`、`author_position`。
- `institutions` 来自 `authorships[].institutions`，可能为空；保留 `id`、`display_name`、`ror`、`country_code`、`type`、`lineage`。
- `concepts` 保留 `id`、`display_name`、`score`、`level`、`wikidata`。
- `topics` 保留 `display_name` 及 `field/subfield/domain.display_name`。
- 列表保持 API 顺序；重复项按首次出现保留。
- 任何类型异常必须降级为空值，不得让搜索失败。
- `keywords` 与 `institutions` 实测可能为空，所有下游打分必须能在缺失时工作。

---

## 9. 评估与 qrels 接口

### 9.1 查询集

`config/evaluation_queries.json`：

```json
{
  "queries": [
    {
      "id": "q001",
      "text": "robotics",
      "topic": "robotics",
      "time_filter": null,
      "task": "research",
      "notes": ""
    }
  ]
}
```

- `id` 唯一；`text` 经 `text.normalize` 后唯一。
- `time_filter` 为 `null`、`"recent"` 或明确年份区间。
- 评估集与 `config/dynamic_queries.txt` 分离；后者仅用于演示与字节回放。

### 9.2 qrels

`config/qrels.json`：

```json
{
  "q001": {
    "doi:10.1234/example": 3,
    "title:example paper": 1
  }
}
```

- `doc_key` 必须是 `records.identity()` 的输出格式：`doi:`、`id:`、`title:` 或 `record:`。
- grade ∈ `{0, 1, 2, 3}`。
- 未标注文档在指标计算中按 0 处理，但必须在报告中记录未标注数量。

### 9.3 评估配置

`config/evaluation.json`：

```json
{
  "k_values": [5, 10],
  "metrics": ["precision", "recall", "ndcg", "mrr"],
  "seed": 0,
  "splits": {"type": "time", "test_after": "2025-01-01"}
}
```

- 时间切分必须使用论文/行为的真实时间字段，禁止用未来数据构造过去画像。
- 固定查询集、固定 qrels、固定随机种子；任何变更提升评估配置版本。

---

## 10. 验证协议

### 10.1 v2 验证（当前必须通过）

`verify_results.py` 必须保持以下行为：

1. 读取 `--results`，要求 `schema_version == 2`。
2. 校验查询集合与 `--queries` 完全一致（规范化、去重、忽略注释）。
3. 用 `settings` 构造重放参数：`cache_only=True`、`refresh=False`、`retries=0`。
4. 禁用网络（patch `src.net.urlopen`），禁止任何请求。
5. 对每个查询重新计算 ConceptNet 快照与 expansions，必须与保存值完全相等；`enhancement_applied` 必须与 expansions 是否为空一致。
6. 要求每行 `status == "ok"`、所有请求成功，且 `conceptnet.backend != "none"`。
7. 要求结果 `key` 非空、唯一。
8. 用 `search.run_search` 重放，输出文件必须与原文件**字节一致**。
9. 报告字段与路径保持现状：`passed`、`byte_identical`、`network_forbidden`、`index_bytes`、`queries`、`sha256`；写入 `results/dynamic_verification.json`。

任何接口迁移完成后，上述 9 条必须全部成立。

### 10.2 v3 验证（实现 v3 时新增）

除 v2 的离线与字节要求外，还必须：

1. 校验 §4.1–§4.6 的全部字段、类型、顺序与范围。
2. 用文档内联 `ranker_config` 重算 `config_hash`。
3. 校验 `score` 与分量加权和（ε ≤ 1e-9）。
4. 校验 `matched_terms ⊆ query ∪ expansions`。
5. 校验每个 `graph_paths[].assertions` 能在该查询 `conceptnet.edges` 或声明的 `graph_version` 中解析。
6. 校验 `matched_authors/institutions` 能在 `paper` 中找到。
7. 用对应 `ranker_version` 的实现重放；旧版本实现不得删除。
8. 报告写入 `results/dynamic_verification_v3.json`，不得覆盖 v2 报告。

### 10.3 篡改测试

验证器必须覆盖以下负例：

- 修改 `schema_version`；
- 删除必需字段；
- 修改 `fusion_score` / `score`；
- 修改 `score_components` 使加权和不等于 `score`；
- 删除 `evidence.graph_paths[].assertions` 或指向不存在的 assertion；
- 修改 `config_hash`；
- 修改查询集合；
- 在 v2 输出中增加 v3 字段。

每个负例必须使验证失败，不得静默通过。

### 10.4 迁移验证

- 缓存键迁移：`--dry-run` 不写文件；`--apply` 幂等；`--rollback` 恢复旧名；迁移前后 `verify_results.py` 字节一致。
- 图谱元数据迁移：不得修改 `manifest`；校验器必须通过；v2 输出字节不变。

### 10.5 接口迁移验收命令

接口迁移完成时，必须记录以下命令的退出码与输出：

```bash
python -m unittest discover -s tests
python verify_results.py
sha256sum results/dynamic_queries.json
```

判据：

- 单元测试全部通过；
- `verify_results.py` 输出 `Verification passed: ... replay byte-identical.` 且退出码为 0；
- `results/dynamic_queries.json` 的 sha256 仍为 `b768aa7682cde189a926e460b3f885482d2af8e4b8764dbdb795c3a2b336cf2b`；
- `results/dynamic_queries_v3.json` 在本次迁移中**不得出现**；v3 只允许存在协议与符合性测试。

---

## 11. 变更控制

### 11.1 版本升级规则

| 变更 | 必须动作 |
| --- | --- |
| 新增结果字段 | 新 `schema_version`，新黄金文件，新验证器分支 |
| 修改字段名/类型/顺序 | 新 `schema_version`，禁止原地改 |
| 修改 RRF 权重或 k | 新 `ranker_version`；保留旧实现；v2 黄金文件不动 |
| 修改图谱内容/清洗规则 | 新 `graph_id`、新 `rules_version`；旧快照不覆盖 |
| 修改缓存键公式 | 提升 `SNAPSHOT_KEY_SCHEMA`；提供迁移与回滚 |
| 修改特征字段 | 提升 `RECORD_FEATURE_VERSION` |
| 修改评估集/qrels | 提升评估配置版本；保留旧版本以复现历史指标 |

### 11.2 提交约束

任何涉及输出的提交必须同时包含：

1. 本文对应章节的更新（若接口变化）；
2. 实现代码；
3. 正向与负向测试；
4. 黄金文件（仅新增版本时）；
5. 验证命令的实际输出证据。

禁止删除或重新生成 v2 黄金文件来“让测试通过”。

### 11.3 状态矩阵

| 接口 | 协议 | 实现 | 验证 |
| --- | --- | --- | --- |
| 输出 v2 | 冻结 | 已实现 | `verify_results.py` 字节回放 |
| 输出 v3 | 冻结字段 | 待 M1 | v3 schema 校验已实现；回放待 M1 |
| Ranker 接口 | 冻结 | 已实现（baseline/rrf） | 等价性与形状测试 |
| 快照缓存键 v2 | 冻结公式 | 已实现并完成迁移 | 迁移 + 回放 |
| 图谱索引读取 | 冻结 | 已实现 | 校验器测试 |
| 论文特征 | 冻结字段 | 已实现 | 单测 |
| 评估/qrels | 冻结字段 | 加载器已实现；数据待 M1 | 加载器测试 |

---

## 12. 非目标

本文不规定：

- 具体的 BM25/TF-IDF 实现与文本归一化细节（由 `text_score` 的 ranker 版本固定）；
- Personalized PageRank、社区发现、加权最短路径的具体算法；
- RAG 的生成模型、提示词与上下文裁剪策略；
- 个性化向量的具体维度与训练方式；
- Acemap 作者/机构接口的字段适配。

这些内容在各自里程碑中定义，但不得改变本文冻结的字段名、必填性、排序规则、哈希公式与版本语义。
