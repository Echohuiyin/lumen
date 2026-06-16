---
name: rag-case-retrieval
description: |
  从Chroma向量数据库检索最相关的案例。当用户需要：
  - 查找相似案例或历史案例
  - 进行语义搜索
  - 从案例库中检索匹配内容
  - 进行RAG（检索增强生成）相关的查询

  使用此技能。即使没有明确提到"RAG"、"向量检索"或"案例检索"，只要语义上需要相似案例匹配就应触发。

compatibility:
  tools: [Bash, Read, Write, AskUserQuestion]
  dependencies:
    - chromadb
    - openai
    - python >= 3.8

---

# RAG案例检索技能

此技能提供完整的向量检索流程：从数据源导入案例到Chroma向量数据库，并支持语义检索返回最相关的案例。

## 核心特性

### 向量化策略

- **定长向量**: 每篇案例生成1个向量（不分块）
- **内容截取**: 标题注入 + 内容前400字符
- **优势**: 简化索引、提高检索一致性、避免分块碎片化

### 嵌入模型

| 参数 | 默认值 | 说明 |
|------|--------|------|
| 模型 | `bge-large-zh` | 中文嵌入模型 |
| 维度 | 1024 | 向量维度 |
| Token上限 | 512 | 输入长度限制 |
| 距离度量 | cosine | 向量相似度计算 |

### 检索接口

CLI工具输出 Top-K JSON 结构：

```json
{
  "status": "success",
  "query": "查询文本",
  "retrieval_config": {
    "top_k": 3,
    "min_similarity": 0.7,
    "embedding_model": "bge-large-zh",
    "distance_metric": "cosine"
  },
  "results": [
    {
      "id": "case_001",
      "title": "案例标题",
      "content": "案例内容",
      "similarity_score": 0.85,
      "distance": 0.15,
      "metadata": {}
    }
  ],
  "summary": {
    "total_found": 3,
    "retrieval_time_ms": 245,
    "embedding_time_ms": 120
  }
}
```

## 前置条件

用户需要提供或系统已配置：
1. **Chroma服务地址**（默认：`http://localhost:8000`）
2. **嵌入服务**（支持本地或云端的OpenAI兼容API）
3. **案例数据源信息**（数据库连接或文件路径）
4. **Collection名称**（默认：`cases`）

### 支持的嵌入服务

此技能支持任何实现 OpenAI `/v1/embeddings` API 端点的服务：

#### 本地服务模式（推荐，数据不外泄）

| 服务 | base_url | 模型示例 | 安装方式 |
|------|----------|----------|----------|
| **Ollama** (推荐) | `http://localhost:11434/v1` | `bge-large-zh`, `nomic-embed-text` | `ollama pull bge-large-zh` |
| **LocalAI** | `http://localhost:8080/v1` | 多种嵌入模型 | Docker部署 |
| **text-embeddings-inference** | `http://localhost:8080/v1` | Hugging Face模型 | Docker: `ghcr.io/huggingface/text-embeddings-inference` |

#### 云服务模式

| 服务类型 | base_url | api_key |
|----------|----------|---------|
| OpenAI官方 | `https://api.openai.com/v1` | `sk-xxxxxx` |
| 国内兼容服务 | `https://xxx.api.com/v1` | 对应API密钥 |

#### 配置示例

**本地 Ollama 配置（默认）**:
```json
{
  "embedding": {
    "base_url": "http://localhost:11434/v1",
    "model": "bge-large-zh",
    "api_key": "not-required",
    "dimension": 1024,
    "max_tokens": 512
  }
}
```

## 工作流程

### 步骤1: 环境检查

```bash
python scripts/check_environment.py
```

输出：连接状态、依赖状态、Collection信息

### 步骤2: 案例导入（首次使用）

**JSON/CSV导入**:
```bash
python scripts/import_cases.py --source json --file cases.json
python scripts/import_cases.py --source csv --file cases.csv
python scripts/import_cases.py --source database --config config.json
```

**ZIP包导入（高性能版）**:
```bash
# 单个ZIP文件
python scripts/import_from_zip.py --zip cases.zip

# 多个ZIP文件（并行处理）
python scripts/import_from_zip.py --zip archive1.zip archive2.zip

# 自定义Collection和批量大小
python scripts/import_from_zip.py --zip docs.zip --collection knowledge_base --batch-size 500
```

ZIP导入性能优化：
- **内存流处理**: 直接从ZIP内存流读取，避免磁盘IO
- **并行向量化**: 多线程批量生成向量
- **流式写入**: 批量写入Chroma，减少网络请求
- **sqlite3 monkey-patch**: 解决Chroma多线程安全问题

Wiki文件名标题提取：
- `001-introduction.md` → `Introduction`
- `kernel_crash_analysis.md` → `Kernel Crash Analysis`
- `第1章_概述.md` → `概述`

**向量化处理**：
1. 读取案例数据
2. 提取定长文本：标题 + 内容前400字符
3. 生成1024维向量（bge-large-zh）
4. 存储到Chroma（cosine距离）

### 步骤3: 语义检索

```bash
python scripts/retrieve_cases.py "查询文本" --top-k 5 --min-similarity 0.6
python scripts/retrieve_cases.py "kernel crash" --output results.json
```

**检索流程**：
1. 将查询转为向量
2. Chroma向量相似度搜索（cosine距离）
3. 过滤低相似度结果
4. 输出Top-K JSON

## 配置管理

配置存储在 `~/.claude/skills/rag-case-retrieval/config.json`：

```json
{
  "chroma": {
    "host": "http://localhost:8000",
    "collection_name": "cases"
  },
  "embedding": {
    "base_url": "http://localhost:11434/v1",
    "model": "bge-large-zh",
    "dimension": 1024,
    "max_tokens": 512
  },
  "retrieval": {
    "default_top_k": 3,
    "min_similarity": 0.7,
    "distance_metric": "cosine"
  },
  "vectorization": {
    "head_chars": 400,
    "title_injection": true
  }
}
```

### 配置参数说明

| 字段 | 说明 | 默认值 |
|------|------|--------|
| `embedding.model` | 嵌入模型名称 | `bge-large-zh` |
| `embedding.dimension` | 向量维度 | 1024 |
| `embedding.max_tokens` | Token上限 | 512 |
| `vectorization.head_chars` | 内容截取长度 | 400 |
| `vectorization.title_injection` | 是否注入标题 | true |
| `retrieval.distance_metric` | 距离度量 | cosine |

## 使用示例

**基础检索**:
```bash
$ python scripts/retrieve_cases.py "用户认证失败"

Top-K JSON 输出:
{
  "status": "success",
  "results": [
    {"id": "auth_042", "title": "JWT令牌过期...", "similarity_score": 0.89}
  ]
}
```

**自定义参数**:
```bash
$ python scripts/retrieve_cases.py "kernel panic" --top-k 10 --min-similarity 0.5
```

**保存结果**:
```bash
$ python scripts/retrieve_cases.py "内存泄漏" --output results.json
```

## 错误处理

1. **Chroma连接失败**
   - 检查Docker服务状态
   - 启动命令：`docker run -d -p 8000:8000 chromadb/chroma`

2. **嵌入服务连接失败**
   - 验证服务运行状态（如 `ollama serve`）
   - 拉取模型：`ollama pull bge-large-zh`

3. **模型不存在**
   - 确认模型已安装
   - 修改config.json中的 `embedding.model`

4. **向量维度不匹配**
   - 更换模型时需删除旧Collection重新导入

5. **无匹配结果**
   - 降低相似度阈值
   - 扩大检索范围

## 与分块策略对比

| 特性 | 定长向量（当前） | 分块策略 |
|------|------------------|----------|
| 向量数量 | 每案例1个 | 每案例多个 |
| 索引复杂度 | 低 | 高 |
| 检索一致性 | 高 | 可能碎片化 |
| 适用场景 | 中短文档 | 长文档 |

## 最佳实践

1. **查询优化**: 使用具体、描述性的查询词
2. **阈值调整**: 根据场景调整相似度阈值
3. **数据维护**: 定期更新案例库
4. **监控质量**: 关注检索效果，必要时调整截取长度