# 后三阶段实现说明

后三阶段采用独立输出协议，避免修改已经冻结的 v2/v3 搜索结果。

## 阶段三：图算法

`src/graph_algorithms.py` 提供纯函数接口：

- `pagerank`：带查询种子的个性化 PageRank；
- `weighted_shortest_paths`：以 `1 / weight` 为边代价的加权路径；
- `common_neighbors` 和 `adamic_adar`：局部结构相似度；
- `community_scores`：确定性的局部社区相似度；
- `meta_path_scores`：按固定关系序列遍历图谱。

`graph-analyze` 命令读取 JSON 边文件或包含 `edges` 数组的快照，输出
`graph-analysis-v1`。候选项包含归一化分数、算法参数和可复现路径。算法不联网、不读取隐式全局状态，结果按分数和规范化词条稳定排序。

```powershell
python .\acemap_search.py graph-analyze --edges .\path\edges.json `
  --seeds robotics --algorithm pagerank --output .\results\graph_analysis.json
```

## 阶段四：知识图谱 RAG

`src/rag.py` 的 `build_context` 对图谱事实、论文标题和摘要进行清洗与数量限制。默认 `extractive_answer` 生成确定性回答，每条声明都带有图谱 assertion 或论文引用。可选生成器必须返回带 citations 的声明，并且引用必须存在于上下文；生成器失败时自动回退到证据回答。

`rag` 命令输出 `rag-v1`，其中 `quality.grounded`、`citation_count` 和
`insufficient_evidence` 可用于离线检查。

```powershell
python .\acemap_search.py rag --query "robotics" `
  --results .\results\dynamic_queries_v3.json --output .\results\rag.json
```

## 阶段五：个性化

`src/profile.py` 只处理调用方显式传入的历史记录。没有 `consent=True` 时画像保持禁用；有授权时按固定 `reference_year` 和半衰期计算权重，提取作者、机构、概念、主题和关键词兴趣。`personalize_results` 将基础分数和画像分数归一化后按

```text
score = (1 - alpha) * base_score + alpha * profile_score
```

合成，并为每个结果记录匹配的画像特征。输出协议为 `personalized-v1`，输入 v2/v3 文件不会被覆盖。未授权或冷启动时 `alpha` 的有效值为零。

```powershell
python .\acemap_search.py personalize --results .\results\dynamic_queries_v3.json `
  --history .\config\history.json --consent --alpha 0.2 `
  --output .\results\personalized.json
```

新增测试位于 `tests/test_later_stages.py`，覆盖算法确定性、RAG 引用约束、生成失败降级、画像授权、时间衰减、个性化公式和命令行入口。
