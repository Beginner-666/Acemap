const researchTopics = [
  ['artificial intelligence', '人工智能', 'ai'],
  ['machine learning', '机器学习', 'ml'],
  ['deep learning', '深度学习'],
  ['large language models', '大语言模型', 'llm', 'llms', '大模型', 'large language model'],
  ['retrieval augmented generation', '检索增强生成', 'rag'],
  ['natural language processing', '自然语言处理', 'nlp'],
  ['computer vision', '计算机视觉', 'cv'],
  ['multimodal learning', '多模态学习', '多模态', 'multimodal', 'vision language'],
  ['reinforcement learning', '强化学习', 'rl'],
  ['graph neural networks', '图神经网络', 'gnn', 'gnns', 'graph neural network'],
  ['knowledge graphs', '知识图谱', 'kg', 'knowledge graph'],
  ['robotics', '机器人学', '机器人', 'robot'],
  ['embodied ai', '具身智能', '具身人工智能'],
  ['autonomous vehicles', '自动驾驶', '无人驾驶', 'self driving'],
  ['information retrieval', '信息检索', 'search engines', '搜索引擎'],
  ['recommender systems', '推荐系统', 'recommendation systems', 'recommender system'],
  ['data mining', '数据挖掘'],
  ['data science', '数据科学'],
  ['data engineering', '数据工程', 'data pipelines', 'etl'],
  ['database systems', '数据库系统', '数据库', 'databases', 'database', 'dbms'],
  ['distributed systems', '分布式系统', '分布式计算', 'distributed computing'],
  ['cloud computing', '云计算', '云原生', 'cloud native'],
  ['computer networks', '计算机网络', '网络通信', 'networking'],
  ['cybersecurity', '网络安全', '信息安全', 'information security', 'cyber security'],
  ['applied cryptography', '应用密码学', '密码学', 'cryptography'],
  ['software engineering', '软件工程', '软件开发', 'software development'],
  ['programming languages', '编程语言', '程序设计语言'],
  ['compilers', '编译器', 'compiler', 'compiler optimization', '编译优化'],
  ['operating systems', '操作系统', 'operating system'],
  ['computer architecture', '计算机体系结构', '计算机架构'],
  ['parallel computing', '并行计算', 'high performance computing', 'hpc', '高性能计算'],
  ['human computer interaction', '人机交互', 'hci'],
  ['computer graphics', '计算机图形学', '图形学'],
  ['virtual reality', '虚拟现实', 'vr'],
  ['speech recognition', '语音识别', 'asr'],
  ['algorithms', '算法', 'algorithm design', '算法设计'],
  ['formal verification', '形式化验证', 'formal methods', '形式化方法'],
  ['edge computing', '边缘计算', 'edge ai'],
  ['internet of things', '物联网', 'iot'],
  ['quantum computing', '量子计算'],
];

// Related paper terminology is used for matching evidence; query aliases stay specific.
const researchSignals = {
  'artificial intelligence': ['artificial general intelligence', 'generative ai', '智能体', '人工通用智能'],
  'machine learning': ['supervised learning', 'unsupervised learning', 'self supervised learning', 'transfer learning', 'federated learning', '监督学习', '自监督学习', '迁移学习', '联邦学习'],
  'deep learning': ['deep neural network', 'deep neural networks', 'convolutional neural network', 'cnn', '深度神经网络', '卷积神经网络'],
  'large language models': ['large language model', 'language models', 'foundation model', 'foundation models', 'gpt', 'chatgpt', 'llama', 'qwen', '语言模型', '基础模型'],
  'retrieval augmented generation': ['retrieval augmentation', 'retrieval enhanced generation', 'retrieval augmented language model', 'retrieval augmented language models', 'graphrag', '检索增强', '知识增强生成'],
  'natural language processing': ['text classification', 'sentiment analysis', 'machine translation', 'named entity recognition', '文本分类', '情感分析', '机器翻译', '命名实体识别'],
  'computer vision': ['image classification', 'object detection', 'image segmentation', 'visual recognition', '图像分类', '目标检测', '图像分割', '视觉识别'],
  'multimodal learning': ['vision language model', 'vision language models', 'visual question answering', 'cross modal', 'multimodal models', '视觉语言模型', '跨模态'],
  'reinforcement learning': ['policy optimization', 'q learning', 'deep q network', 'actor critic', 'rlhf', 'reinforcement learning from human feedback', '策略优化', '人类反馈强化学习'],
  'graph neural networks': ['graph convolutional network', 'graph convolutional networks', 'graph attention network', 'graph attention networks', 'graph representation learning', '图卷积网络', '图注意力网络'],
  'knowledge graphs': ['knowledge graph completion', 'knowledge graph embedding', 'entity linking', 'relation extraction', '知识图谱补全', '知识表示', '实体链接', '关系抽取'],
  'robotics': ['robots', 'robotic', 'robot manipulation', 'motion planning', 'robot navigation', 'simultaneous localization and mapping', 'slam', '机器人操控', '运动规划', '机器人导航'],
  'embodied ai': ['embodied intelligence', 'embodied agents', 'embodied agent', 'vision language action', '具身智能体', '视觉语言动作'],
  'autonomous vehicles': ['autonomous driving', 'autonomous vehicle', 'self driving cars', 'autonomous navigation', '自动驾驶车辆', '自主驾驶'],
  'information retrieval': ['document retrieval', 'dense retrieval', 'semantic search', 'search engine', 'learning to rank', '文档检索', '稠密检索', '语义搜索', '学习排序'],
  'recommender systems': ['recommendation system', 'recommendation model', 'collaborative filtering', 'sequential recommendation', '推荐模型', '协同过滤', '序列推荐'],
  'data mining': ['frequent pattern mining', 'association rule mining', 'anomaly detection', '聚类分析', '频繁模式挖掘', '关联规则挖掘', '异常检测'],
  'data science': ['data analysis', 'statistical analysis', 'predictive modeling', '数据分析', '统计分析', '预测建模'],
  'data engineering': ['data pipeline', 'data integration', 'data warehouse', 'data lake', 'stream processing', '数据管道', '数据集成', '数据仓库', '数据湖', '流处理'],
  'database systems': ['query optimization', 'text to sql', 'sql', 'relational database', 'vector database', 'vector databases', 'transaction processing', '数据库查询', '查询优化', '向量数据库', '事务处理'],
  'distributed systems': ['distributed system', 'distributed storage', 'distributed training', 'distributed consensus', 'fault tolerance', '分布式存储', '分布式训练', '分布式共识', '容错'],
  'cloud computing': ['cloud infrastructure', 'serverless', 'kubernetes', 'container orchestration', 'microservices', '云基础设施', '无服务器计算', '容器编排', '微服务'],
  'computer networks': ['computer network', 'network protocol', 'network protocols', 'network routing', 'congestion control', 'software defined networking', '网络协议', '网络路由', '拥塞控制', '软件定义网络'],
  'cybersecurity': ['security', 'ai safety', 'model safety', 'prompt injection', 'prompt injections', 'jailbreak', 'jailbreaks', 'jailbreaking', 'adversarial attack', 'adversarial attacks', 'backdoor', 'backdoors', 'vulnerability', 'vulnerabilities', 'intrusion detection', 'malware', 'privacy preserving', 'data privacy', '大模型安全', '模型安全', '提示注入', '越狱', '对抗攻击', '后门攻击', '入侵检测', '隐私保护'],
  'applied cryptography': ['cryptographic', 'encryption', 'homomorphic encryption', 'secure multiparty computation', 'zero knowledge', '加密', '同态加密', '安全多方计算', '零知识证明'],
  'software engineering': ['code generation', 'program repair', 'software testing', 'software maintenance', 'bug detection', '代码生成', '程序修复', '软件测试', '软件维护', '缺陷检测'],
  'programming languages': ['programming language', 'type system', 'type systems', 'program semantics', 'program synthesis', '类型系统', '程序语义', '程序合成'],
  'compilers': ['code optimization', 'compiler optimizations', 'compiler construction', 'code compilation', 'llvm', '代码优化', '编译器优化', '编译器构造'],
  'operating systems': ['kernel', 'kernels', 'process scheduling', 'memory management', 'file system', 'file systems', '内核', '进程调度', '内存管理', '文件系统'],
  'computer architecture': ['hardware architecture', 'processor architecture', 'accelerator design', 'hardware acceleration', '硬件架构', '处理器架构', '加速器设计', '硬件加速'],
  'parallel computing': ['gpu computing', 'parallel processing', 'parallel algorithms', 'gpu acceleration', '并行处理', '并行算法', 'gpu加速'],
  'human computer interaction': ['user interface', 'user experience', 'usability', 'human ai interaction', 'human centered', '用户界面', '用户体验', '可用性', '人机协作'],
  'computer graphics': ['rendering', 'ray tracing', '3d reconstruction', 'neural radiance fields', 'gaussian splatting', '渲染', '光线追踪', '三维重建', '高斯泼溅'],
  'virtual reality': ['immersive environments', 'immersive environment', 'head mounted display', 'immersive virtual', '沉浸式环境', '头戴显示器'],
  'speech recognition': ['automatic speech recognition', 'speech to text', 'speech transcription', '语音转文字', '自动语音识别', '语音转写'],
  'algorithms': ['algorithm', 'algorithmic', 'computational complexity', 'combinatorial optimization', '计算复杂性', '组合优化'],
  'formal verification': ['model checking', 'theorem proving', 'program verification', 'proof assistant', '模型检查', '定理证明', '程序验证', '证明助手'],
  'edge computing': ['edge inference', 'on device', 'mobile edge computing', '端侧推理', '端侧部署', '移动边缘计算'],
  'internet of things': ['sensor networks', 'sensor network', 'wireless sensors', '物联设备', '传感器网络', '无线传感器'],
  'quantum computing': ['quantum computation', 'quantum algorithm', 'quantum algorithms', 'quantum circuit', 'qubit', '量子计算算法', '量子电路', '量子比特'],
};

// A focused retrieval term for each selectable occupation, in the same order.
const roleSearchTerms = ['software engineering', 'user interface', 'machine learning',
  'data analysis', 'data engineering', 'security', 'cloud computing'];

const researchRoles = [
  [['后端工程师', '后端开发', 'backend engineer', 'back end engineer', 'back end developer', 'backend developer'],
    ['database systems', 'distributed systems', 'software engineering']],
  [['前端工程师', '前端开发', 'frontend engineer', 'front end engineer', 'front end developer', 'frontend developer'],
    ['human computer interaction', 'software engineering', 'computer graphics']],
  [['算法工程师', '机器学习工程师', 'ml engineer', 'machine learning engineer', 'ai engineer', '人工智能工程师'],
    ['machine learning', 'deep learning', 'algorithms']],
  [['数据科学家', '数据分析师', 'data scientist', 'data analyst'], ['data science', 'data mining', 'machine learning']],
  [['数据工程师', 'data engineer'], ['data engineering', 'database systems', 'distributed systems']],
  [['安全工程师', '安全研究员', 'security engineer', 'security researcher'], ['cybersecurity', 'applied cryptography']],
  [['运维工程师', '运维开发', 'devops', 'sre', '站点可靠性工程师'], ['cloud computing', 'distributed systems', 'computer networks']],
];

function normalizeTerms(value) {
  return String(value ?? '').normalize('NFKC').toLowerCase()
    .replace(/[^\p{L}\p{N}]+/gu, ' ').trim();
}

function termOccurs(normalizedText, value) {
  const term = normalizeTerms(value);
  if (!term) return false;
  return /\p{Script=Han}/u.test(term)
    ? normalizedText.includes(term)
    : ` ${normalizedText} `.includes(` ${term} `);
}

const topicAliases = new Map(researchTopics.map(([topic, ...aliases]) => [topic, [topic, ...aliases]]));
const topicByAlias = new Map(researchTopics.flatMap(group => group.map(alias => [normalizeTerms(alias), group[0]])));

function selectedProfile(roleIndexes, topics) {
  const interests = new Map();
  for (const index of roleIndexes) {
    const role = researchRoles[index];
    if (!role) continue;
    for (const term of role[1]) interests.set(term, {input: role[0][0], term, weight: 0.5, role: true, preference: false});
  }
  for (const term of topics) {
    if (topicAliases.has(term)) interests.set(term, {...interests.get(term), input: term, term, weight: 1, preference: true});
  }
  return [...interests.values()];
}

function matchedInterests(fields, interests) {
  const text = fields.map(normalizeTerms);
  return interests.flatMap(interest => {
    const aliases = [...(topicAliases.get(interest.term) || [interest.term]),
      ...(researchSignals[interest.term] || [])];
    let evidence = null;
    text.forEach((field, index) => {
      const alias = aliases.find(term => termOccurs(field, term));
      if (!alias) return;
      // Title and metadata describe the subject; abstract mentions are weaker evidence.
      const strength = index < 2 ? 1 : index === 2 ? 0.25 : 0.8;
      if (!evidence || strength > evidence.strength) {
        evidence = {strength, alias, field: index < 2 ? 'title' : index === 2 ? 'abstract' : 'metadata'};
      }
    });
    return evidence ? [{...interest, ...evidence}] : [];
  });
}

function alignResearchQuery(query) {
  const normalized = normalizeTerms(query);
  const exact = topicByAlias.get(normalized);
  if (exact) return exact;
  // Match known Chinese labels inside a question, preferring longer phrases.
  const matches = [...topicByAlias].filter(([alias]) => /\p{Script=Han}/u.test(alias))
    .sort(([a], [b]) => b.length - a.length);
  let remaining = normalized;
  const topics = new Set();
  for (const [alias, topic] of matches) {
    if (!remaining.includes(alias)) continue;
    topics.add(topic);
    remaining = remaining.replaceAll(alias, ' ');
  }
  return [...topics].join(' ') || query;
}
