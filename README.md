# Acemap：基于 ConceptNet 的搜索增强

本项目实现基于 ConceptNet 的动态查询扩展。输入关键词后，程序查找相关概念，调用 Acemap 搜索接口，再合并、去重并排序。包含人工智能领域的三组真实查询及离线响应快照，可在不下载完整图谱的情况下复现实验。

## 目录

- `src/`：重构后的实现，按职责拆分为独立模块：
  - `constants.py`：仓库路径、接口地址与默认缓存位置。
  - `jsonio.py`：JSON 原子写入与 SHA256 摘要。
  - `text.py`：标签规范化与 ConceptNet URI 解析。
  - `edges.py`：边排序与查询时动态邻居排序。
  - `net.py`：统一网络出口，核验时可一次性禁止全部联网。
  - `records.py`：Acemap 响应结构解析与论文去重键。
  - `acemap_client.py`：Acemap 请求、重试与响应缓存。
  - `conceptnet_index.py`：通用英文 SQLite 索引构建（动态图谱机制）。
  - `graph_index.py`：图谱清单、`graph_id`、读取与结构校验契约。
  - `conceptnet_provider.py`：在线/本地 ConceptNet 邻居查询与快照缓存。
  - `cache_migration.py`：ConceptNet 快照缓存键迁移、幂等重跑与回滚。
  - `rankers.py`：Ranker 协议及冻结的 baseline/RRF/graph_rerank 实现。
  - `query_understanding.py`：v3 查询意图拆解与扩展候选过滤（`qu-v1`）。
  - `multihop.py`：可选的一跳作者/机构扩展与 `multihop-v1` 文档。
  - `config.py`：排序配置的严格加载与校验。
  - `paper_features.py`：论文年份、DOI、作者、机构、主题等特征提取。
  - `evaluation.py`：评估查询、qrels、Precision/Recall/nDCG/MRR 与 `evaluate` 报告。
  - `verification.py`：v2/v3 输出校验与离线字节回放。
  - `versions.py`：协议版本常量与规范化哈希。
  - `search.py`：查询扩展、加权倒数排名融合与结果组装。
  - `cli.py`：命令行解析与入口。
- `acemap_search.py`：兼容启动器，转发至 `src.cli`，保留 `build-index`/`search` 的原有调用方式。
- `verify_results.py`：核对正式结果及图谱依据，禁止联网后重放并逐字节比较。
- `docs/interface_protocol.md`：v2/v3 输出、Ranker、缓存、图谱索引与评估接口协议。
- `config/dynamic_queries.txt`：三组人工智能领域示例查询。
- `config/ranking.json`：排序权重与融合参数。
- `config/multihop.json`：多跳权重、种子上限与单跳请求数。
- `config/evaluation.json`：评估 K 值、指标、相关性阈值与时间切分。
- `config/evaluation_queries.json` / `config/evaluation_queries.txt`：40 条固定评估查询。
- `config/qrels.json`：40 条查询的规则辅助 qrels 草稿（待人工复核）。
- `data/raw/`：自行下载的 ConceptNet 原始文件，不纳入版本控制。
- `data/conceptnet_en.sqlite`：本地构建的可复用索引，不纳入版本控制。
- `data/conceptnet_cache/`：正式查询实际使用的图谱关系与来源快照。
- `data/acemap_cache/`：正式实验引用的 Acemap 请求参数和原始响应。
- `results/dynamic_queries.json`：冻结的 v2 真实示例结果。
- `results/dynamic_queries_v3.json`：冻结的阶段一 v3 `graph_rerank` 结果。
- `results/dynamic_queries_multihop.json`：可选多跳扩展结果（`multihop-v1`）。
- `results/eval_baseline.json` / `eval_rrf.json` / `eval_graph_rerank.json`：40 条评估查询的三种排序结果。
- `results/evaluation_report.json`：三种 ranker 的指标与对比报告。
- `results/qrels_review.json`：qrels 候选池与建议分数，供人工复核。
- `results/dynamic_verification.json` / `dynamic_verification_v3.json`：保存的核验统计。
- `results/migrations/`：缓存键与图谱元数据迁移清单。
- `scripts/m0_migrate.py`：M0 数据迁移命令（dry-run / apply / rollback）。
- `tests/`：67 项回归测试，包含输出协议、失败路径、Ranker 等价性、查询理解、多跳扩展、评估指标、全局度过滤、缓存迁移、图谱校验与跨平台 JSON 换行检查。

## 快速验证（无需完整图谱、无需联网）

需要 Python 3.10+，无第三方依赖。

```powershell
git clone https://github.com/Beginner-666/Acemap.git
cd Acemap
python -B verify_results.py
python -B -m unittest discover -s tests -v
```

核验脚本使用仓库中的两类真实快照，禁止 ConceptNet 和 Acemap 网络调用，生成 `results/dynamic_queries_replay.json` 并与原始结果逐字节比较。该重放文件由程序生成，不纳入版本控制。

## 接口协议与 M0 迁移

v2/v3 输出字段、Ranker 接口、ConceptNet 快照缓存键、图谱索引契约与评估输入格式见 [`docs/interface_protocol.md`](docs/interface_protocol.md)。v2 黄金文件 `results/dynamic_queries.json` 已冻结；v3 黄金文件 `results/dynamic_queries_v3.json` 已生成并冻结，多跳使用独立的 `multihop-v1` 文档。

缓存键与图谱元数据迁移：

```bash
python scripts/m0_migrate.py cache-key --dry-run
python scripts/m0_migrate.py cache-key --apply
python scripts/m0_migrate.py graph-id --apply
```

迁移清单写入 `results/migrations/`，`cache-key --rollback` 可恢复旧文件名。

## 运行

搜索程序需要 Python 3.10+，仅用标准库。已提供索引时直接搜索：

```powershell
python .\acemap_search.py search --query "artificial intelligence"
python .\acemap_search.py search --queries .\config\dynamic_queries.txt --output .\results\new_search.json
```

默认命令仍是冻结的 v2 输出；阶段一新增能力全部通过显式开关启用：

```powershell
# v3：查询理解 + 图谱/文本/时间重排
python .\acemap_search.py search --queries .\config\dynamic_queries.txt `
  --schema-version 3 --ranker graph_rerank --cache-only `
  --output .\results\dynamic_queries_v3.json

# 多跳：仅对首轮结果做一跳作者/机构扩展，独立输出
python .\acemap_search.py multihop --results .\results\dynamic_queries_v3.json `
  --output .\results\dynamic_queries_multihop.json --cache-only
```

### 增强开关

| 目标 | 开关 | 行为 |
| --- | --- | --- |
| 保持原有 v2 行为 | 省略 `--schema-version 3` | 原词 + 扩展词加权 RRF，输出字节冻结 |
| 只做原词对照 | `--schema-version 3 --ranker baseline` | 不发起扩展请求，保留 Acemap 顺序 |
| 只做 RRF 对照 | `--schema-version 3 --ranker rrf` | 复用 v2 扩展选择与融合分数 |
| 开启阶段一重排 | `--schema-version 3 --ranker graph_rerank` | 查询理解、图谱/文本/时间重排 |
| 关闭/开启多跳 | 不执行 / 执行 `multihop` 子命令 | 独立输出，不修改 v2/v3 文件 |
| 关闭/开启个性化 | `personalization.alpha = 0` | 当前阶段一固定为 0 |

`baseline`/`rrf`/`graph_rerank` 均可通过 `--ranker` 直接切换；`graph_rerank` 的完整配置会内联到 v3 输出，便于复现。

### 参数调节

| 文件 | 主要参数 | 作用 |
| --- | --- | --- |
| `config/ranking.json` | `weights.*`、`fusion.*`、`graph.degree_cap`、`graph.min_edge_weight`、`freshness.*` | 排序权重、RRF 参数、扩展过滤与时间衰减 |
| `config/multihop.json` | `hop_weight`、`max_authors`、`max_institutions`、`seed_limit`、`size_per_hop`、`min_seed_score` | 多跳强度、种子数量和触发阈值 |
| `config/evaluation.json` | `k_values`、`metrics`、`relevance_threshold`、`splits` | 评估切分与指标 |

另有命令行开关：`--max-expansions`（扩展词上限）、`--size`（每个词条数）、`--knowledge`（`auto/local/online`）、`--ranking-config`（替换排序配置）、`--cache-only` / `--refresh`。修改 JSON 即可调参，不需要改代码；新配置会改变 `config_hash` 并写在输出中。

`acemap_search.py` 只是兼容入口，`python -m src ...` 与上述命令等价。默认 `--knowledge auto` 优先查询 ConceptNet 在线 API，不可用时回退本地索引。`--knowledge local` 只用本地索引，`--knowledge online` 只用在线服务。`--cache-only` 禁止联网并要求已有 ConceptNet 邻居快照和论文缓存；`--refresh` 会覆盖对应查询缓存，保留正式实验时应使用另一缓存目录。

索引不存在时，从原始文件一次构建：

```powershell
python .\acemap_search.py build-index --assertions .\data\raw\conceptnet-assertions-5.7.0.csv.gz
```

原始数据：ConceptNet 5.7.0 assertions，获取说明为 <https://github.com/commonsense/conceptnet5/wiki/Downloads>。原始文件为 gzip 压缩的制表符分隔文本。索引保留七类允许关系、英语两端及权重至少 1 的边，共 2,653,040 条；文件约 1.83 GiB。各边保留数据来源和许可，索引 metadata 记录源文件 SHA256。搜索时每个方向最多读取 1000 条邻边。

名称按规范化结果精确匹配，支持 `ai → artificial intelligence` 缩写展开。不存在完整概念时不会把单个泛化词冒充原概念，输出明确标记未增强。默认选取最多三个不同邻居，按权重及 URI 排序。原始词与扩展词各取第一页十条，按 DOI、ID、规范化标题依次确定去重键；融合分数为各路 `w/(60+rank)` 之和，原词权重 1，扩展词 0.5。

重构后只保留动态图谱机制：`build-index` 一次性构建通用英文索引，`search` 在查询时按概念读取一跳邻居并扩展。原先 `build-graph` 的固定领域子图构建路径，以及 `--knowledge graph`、`--graph` 参数均已移除；除这些图谱构建选项外，`build-index` 与 `search` 的其余命令行参数和输出结构保持不变。

## 复现与验证

```powershell
python .\verify_results.py
python .\verify_results.py --results .\results\dynamic_queries_v3.json
python -B -m unittest discover -s tests -v

# 40 条固定查询的三种 ranker 对比
python -m src evaluate --results baseline=results/eval_baseline.json `
  --results rrf=results/eval_rrf.json `
  --results graph_rerank=results/eval_graph_rerank.json `
  --qrels-provenance "ai-assisted rubric; human review pending"
```

核验脚本检查查询覆盖、图谱证据、实际扩展词、请求状态和去重键，并禁止两个网络入口执行重放。不能匹配到概念是可记录的覆盖限制，不会被错误计作增强成功。v3 校验还会用文档内联的 `ranker_config` 重算 `config_hash` 和每个 `score`，并逐字节回放 `results/dynamic_queries_v3.json`。

2026-11-16 的真实结果：`robotics` 和 `artificial intelligence` 分别由 10 条增加至 40 条候选；`embodied ai` 及缩写展开形式未匹配到可用关系，仍为 10 条。三组请求成功，离线重放逐字节一致。接口迁移后重新核验，仍与原结果逐字节比较通过；阶段一实现后 v2 复核仍逐字节一致，当前 67 项测试通过。JSON 输出统一为 LF 换行，使 Git 下载后的文件与 Windows 上的重放输出也能逐字节比较。

40 条固定查询、`relevance_threshold=1` 的规则辅助 qrels 上，`graph_rerank` 相对 `baseline`：`nDCG@10` 0.8626 → 0.8879，`Recall@10` 0.3528 → 0.3606；`rrf` 为 0.8576 / 0.3499。该 qrels 是规则辅助草稿（会议/论文集、通用短标题、survey 意图和显式年份均已降级或过滤），仍需人工复核；指标用于消融对比，不应单独作为质量结论。候选数量增加不代表精确率或召回率提高。

## 数据来源

ConceptNet 关系来自官方公开 assertions 文件，各边原始来源与许可随 `metadata` 保留；动态索引的源文件哈希保存在图谱快照的 `source` 中。Acemap 文献元数据和摘要来自上述搜索接口，仓库中的快照记录了请求参数、采集时间和原始响应，仅作为可复现实验数据保留，不改变原数据的权利归属。
