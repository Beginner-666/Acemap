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
  - `conceptnet_provider.py`：在线/本地 ConceptNet 邻居查询与快照缓存。
  - `search.py`：查询扩展、加权倒数排名融合与结果组装。
  - `cli.py`：命令行解析与入口。
- `acemap_search.py`：兼容启动器，转发至 `src.cli`，保留 `build-index`/`search` 的原有调用方式。
- `verify_results.py`：核对正式结果及图谱依据，禁止联网后重放并逐字节比较。
- `config/dynamic_queries.txt`：三组人工智能领域示例查询。
- `data/raw/`：自行下载的 ConceptNet 原始文件，不纳入版本控制。
- `data/conceptnet_en.sqlite`：本地构建的可复用索引，不纳入版本控制。
- `data/conceptnet_cache/`：正式查询实际使用的图谱关系与来源快照。
- `data/acemap_cache/`：正式实验引用的 Acemap 请求参数和原始响应。
- `results/dynamic_queries.json`：真实示例结果。
- `results/dynamic_verification.json`：保存的核验统计。
- `tests/`：十一项回归测试，包含跨平台 JSON 换行一致性与动态图谱入口检查。

## 快速验证（无需完整图谱、无需联网）

需要 Python 3.10+，无第三方依赖。

```powershell
git clone https://github.com/Beginner-666/Acemap.git
cd Acemap
python -B verify_results.py
python -B -m unittest discover -s tests -v
```

核验脚本使用仓库中的两类真实快照，禁止 ConceptNet 和 Acemap 网络调用，生成 `results/dynamic_queries_replay.json` 并与原始结果逐字节比较。该重放文件由程序生成，不纳入版本控制。

## 运行

搜索程序需要 Python 3.10+，仅用标准库。已提供索引时直接搜索：

```powershell
python .\acemap_search.py search --query "artificial intelligence"
python .\acemap_search.py search --queries .\config\dynamic_queries.txt --output .\results\new_search.json
```

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
python -B -m unittest discover -s tests -v
```

核验脚本检查查询覆盖、图谱证据、实际扩展词、请求状态和去重键，并禁止两个网络入口执行重放。不能匹配到概念是可记录的覆盖限制，不会被错误计作增强成功。

2026-11-16 的真实结果：`robotics` 和 `artificial intelligence` 分别由 10 条增加至 40 条候选；`embodied ai` 及缩写展开形式未匹配到可用关系，仍为 10 条。三组请求成功，离线重放逐字节一致。重构后重新核验，通过与原结果逐字节比较；当前十一项测试通过。JSON 输出统一为 LF 换行，使 Git 下载后的文件与 Windows 上的重放输出也能逐字节比较。候选数量增加不代表精确率或召回率提高，结果中可能包含较宽泛的期刊或会议条目。

## 数据来源

ConceptNet 关系来自官方公开 assertions 文件，各边原始来源与许可随 `metadata` 保留；动态索引的源文件哈希保存在图谱快照的 `source` 中。Acemap 文献元数据和摘要来自上述搜索接口，仓库中的快照记录了请求参数、采集时间和原始响应，仅作为可复现实验数据保留，不改变原数据的权利归属。
