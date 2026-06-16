# RAG案例检索 - 数据结构定义

## 1. 案例数据结构 (Case Document)

### 必需字段
```json
{
  "id": "string - 唯一标识符",
  "title": "string - 案例标题",
  "content": "string - 案例主要内容"
}
```

### 可选字段
```json
{
  "category": "string - 分类（如：安全、性能、功能）",
  "tags": ["string"] - 标签列表",
  "created_at": "string - ISO 8601格式日期",
  "updated_at": "string - 最后更新时间",
  "source": "string - 数据来源",
  "author": "string - 作者",
  "metadata": {
    "any": "额外的元数据字段"
  }
}
```

### 完整示例
```json
{
  "id": "case_2024_001",
  "title": "JWT令牌过期处理不当导致认证失败",
  "content": "在某次生产环境部署后，用户反馈频繁出现认证失败。经排查发现是JWT刷新机制存在竞态条件...",
  "category": "安全",
  "tags": ["JWT", "认证", "竞态条件"],
  "created_at": "2024-03-15T10:30:00Z",
  "source": "生产事故报告",
  "author": "张三",
  "metadata": {
    "severity": "high",
    "resolved": true,
    "related_issues": ["issue_123", "issue_456"]
  }
}
```

## 2. Chroma Collection结构

### 向量元数据
存储在Chroma中的每个向量包含：
```python
{
  "id": "文档ID或块ID",
  "embedding": [float, ...],  # 1536维向量（OpenAI）
  "metadata": {
    "doc_id": "原始文档ID",
    "title": "案例标题",
    "category": "分类",
    "created_at": "创建时间",
    "chunk_index": 0,  # 分块索引
    "chunk_total": 1   # 总块数
  },
  "document": "实际文本内容"
}
```

### Collection配置
```python
collection_config = {
    "name": "cases",
    "metadata": {
        "description": "案例检索向量库",
        "embedding_model": "text-embedding-3-small",
        "created_at": "ISO 8601时间",
        "hnsw:space": "cosine"  # 相似度度量
    }
}
```

## 3. 检索请求结构

### 基础检索
```json
{
  "query": "查找用户认证失败的案例",
  "top_k": 3,
  "min_similarity": 0.7
}
```

### 带过滤条件的检索
```json
{
  "query": "数据库性能优化案例",
  "top_k": 5,
  "min_similarity": 0.6,
  "filters": {
    "category": "性能",
    "created_at": {
      "$gte": "2024-01-01"
    },
    "tags": {
      "$contains": "MySQL"
    }
  }
}
```

### 过滤操作符
```python
# 相等/包含
{"field": "value"}
{"field": {"$eq": "value"}}
{"field": {"$ne": "value"}}

# 数值比较
{"field": {"$gt": 10}}
{"field": {"$gte": 10}}
{"field": {"$lt": 100}}
{"field": {"$lte": 100}}

# 数组操作
{"tags": {"$contains": "标签名"}}
{"tags": {"$in": ["标签1", "标签2"]}}

# 逻辑组合
{"$and": [{"field1": "value1"}, {"field2": "value2"}]}
{"$or": [{"field1": "value1"}, {"field2": "value2"}]}
```

## 4. 检索结果结构

### 成功响应
```json
{
  "status": "success",
  "query": "用户查询文本",
  "retrieval_config": {
    "top_k": 3,
    "min_similarity": 0.7,
    "filters": {},
    "embedding_model": "text-embedding-3-small"
  },
  "results": [
    {
      "id": "case_001",
      "title": "案例标题",
      "content": "案例内容...",
      "content_preview": "前200字预览...",
      "similarity_score": 0.85,
      "metadata": {
        "category": "安全",
        "tags": ["JWT", "认证"],
        "created_at": "2024-03-15T10:30:00Z"
      },
      "chunk_index": 0,
      "chunk_total": 1
    }
  ],
  "summary": {
    "total_found": 15,
    "above_threshold": 3,
    "returned": 3,
    "retrieval_time_ms": 245,
    "embedding_time_ms": 120,
    "search_time_ms": 125
  }
}
```

### 无结果响应
```json
{
  "status": "no_results",
  "query": "查询文本",
  "message": "未找到相似度高于0.7的结果",
  "suggestions": [
    "尝试降低相似度阈值",
    "使用更通用的查询词",
    "检查过滤条件是否过于严格"
  ],
  "best_match": {
    "similarity_score": 0.65,
    "title": "最接近的案例"
  }
}
```

### 错误响应
```json
{
  "status": "error",
  "error_code": "CHROMA_CONNECTION_FAILED",
  "message": "无法连接到Chroma服务",
  "details": {
    "host": "http://localhost:8000",
    "timeout_ms": 5000
  },
  "suggested_fix": "请确保Chroma服务正在运行：docker run -p 8000:8000 chromadb/chroma"
}
```

## 5. 配置文件结构

### config.json
```json
{
  "chroma": {
    "host": "http://localhost:8000",
    "timeout": 30,
    "retry_attempts": 3
  },
  "embedding": {
    "model": "text-embedding-3-small",
    "batch_size": 100,
    "max_retries": 3
  },
  "retrieval": {
    "default_top_k": 3,
    "min_similarity": 0.7,
    "include_fields": ["id", "title", "content", "metadata"]
  },
  "chunking": {
    "strategy": "semantic",
    "max_chunk_size": 1000,
    "overlap": 200,
    "min_chunk_size": 100
  },
  "data_sources": {
    "default_type": "database",
    "supported_types": ["database", "file", "api"]
  }
}
```

## 6. 数据导入结构

### 数据源配置
```json
{
  "source_type": "database",
  "connection": {
    "type": "postgresql",
    "host": "localhost",
    "port": 5432,
    "database": "cases_db",
    "user": "user",
    "password": "pass"
  },
  "query": "SELECT * FROM cases WHERE created_at > '2024-01-01'",
  "mapping": {
    "id": "case_id",
    "title": "case_title",
    "content": "case_description",
    "category": "category_name",
    "tags": "tag_list"
  }
}
```

### 导入进度
```json
{
  "import_id": "imp_20240315_001",
  "status": "in_progress",
  "total_records": 1000,
  "processed": 450,
  "successful": 448,
  "failed": 2,
  "errors": [
    {
      "record_id": "case_123",
      "error": "Missing required field: content"
    }
  ],
  "started_at": "2024-03-15T10:00:00Z",
  "estimated_completion": "2024-03-15T10:15:00Z"
}
```

## 7. 统计信息结构

### Collection统计
```json
{
  "collection_name": "cases",
  "total_documents": 5000,
  "total_chunks": 8500,
  "avg_chunk_size": 750,
  "categories": {
    "安全": 1200,
    "性能": 800,
    "功能": 3000
  },
  "date_range": {
    "earliest": "2020-01-01",
    "latest": "2024-03-15"
  },
  "index_size_mb": 256,
  "last_updated": "2024-03-15T10:00:00Z"
}
```

### 检索统计
```json
{
  "period": "2024-03-01 to 2024-03-15",
  "total_queries": 1523,
  "avg_results_per_query": 2.8,
  "avg_similarity_score": 0.78,
  "avg_retrieval_time_ms": 180,
  "queries_with_results": 1498,
  "queries_without_results": 25,
  "popular_queries": [
    {"query": "认证失败", "count": 150},
    {"query": "性能优化", "count": 120}
  ]
}
```

## 8. API接口定义

### 检索API
```python
POST /api/retrieve
Content-Type: application/json

Request Body:
{
  "query": "string",
  "top_k": "int (default: 3)",
  "min_similarity": "float (default: 0.7)",
  "filters": "object (optional)"
}

Response:
{
  "status": "success",
  "results": [...],
  "summary": {...}
}
```

### 导入API
```python
POST /api/import
Content-Type: application/json

Request Body:
{
  "source": {...},
  "options": {
    "batch_size": 100,
    "update_existing": false,
    "validate_only": false
  }
}

Response:
{
  "import_id": "string",
  "status": "started"
}
```

### 状态API
```python
GET /api/status
Response:
{
  "chroma_connected": true,
  "collection_count": 5000,
  "last_import": "2024-03-15T10:00:00Z",
  "embedding_model": "text-embedding-3-small"
}
```