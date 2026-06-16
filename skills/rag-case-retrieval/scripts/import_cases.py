#!/usr/bin/env python3
"""
案例导入脚本 - 从数据源导入案例到Chroma向量库
支持定长向量化策略：每篇案例1个向量（头400字符+标题注入）
"""

import os
import sys
import json
import time
from pathlib import Path
from typing import List, Dict, Any, Optional
from datetime import datetime

from config_loader import get_embedding_config

def get_embeddings(texts: List[str], config: Dict) -> List[List[float]]:
    """生成文本向量 (使用本地OpenAI兼容服务)"""
    from openai import OpenAI

    ec = get_embedding_config(config)

    client = OpenAI(
        base_url=ec["base_url"],
        api_key=ec["api_key"],
        timeout=ec["timeout"]
    )

    all_embeddings = []

    for i in range(0, len(texts), ec["batch_size"]):
        batch = texts[i:i + ec["batch_size"]]
        try:
            response = client.embeddings.create(
                model=ec["model"],
                input=batch
            )
            batch_embeddings = [item.embedding for item in response.data]
            all_embeddings.extend(batch_embeddings)
        except Exception as e:
            raise Exception(f"Failed to generate embeddings from {ec['base_url']}: {str(e)}")

    return all_embeddings

def prepare_fixed_length_text(title: str, content: str, config: Dict) -> str:
    """
    准备定长文本用于向量化
    策略：标题注入 + 头400字符

    Args:
        title: 案例标题
        content: 案例内容
        config: 配置字典

    Returns:
        定长文本
    """
    vec_config = config.get("vectorization", {})
    head_chars = vec_config.get("head_chars", 400)
    title_injection = vec_config.get("title_injection", True)

    # 清理文本
    title = title.strip() if title else ""
    content = content.strip() if content else ""

    # 构建向量文本
    if title_injection and title:
        # 标题注入格式: "标题\n\n内容前N字符"
        text = f"{title}\n\n{content[:head_chars]}"
    else:
        text = content[:head_chars]

    return text

def import_from_database(connection_config: Dict, query: str, mapping: Dict) -> List[Dict]:
    """从数据库导入案例"""
    import psycopg2
    from psycopg2.extras import RealDictCursor

    conn = psycopg2.connect(
        host=connection_config.get("host", "localhost"),
        port=connection_config.get("port", 5432),
        database=connection_config.get("database"),
        user=connection_config.get("user"),
        password=connection_config.get("password")
    )

    cursor = conn.cursor(cursor_factory=RealDictCursor)
    cursor.execute(query)
    rows = cursor.fetchall()

    cases = []
    for row in rows:
        case = {
            "id": str(row.get(mapping.get("id", "id"))),
            "title": row.get(mapping.get("title", "title"), ""),
            "content": row.get(mapping.get("content", "content"), ""),
            "metadata": {}
        }

        # 添加可选字段
        for field in ["category", "tags", "created_at", "source", "author"]:
            if field in mapping and mapping[field] in row:
                case["metadata"][field] = row[mapping[field]]

        cases.append(case)

    cursor.close()
    conn.close()

    return cases

def import_from_json(file_path: str) -> List[Dict]:
    """从JSON文件导入案例"""
    with open(file_path, 'r', encoding='utf-8') as f:
        data = json.load(f)

    if isinstance(data, list):
        return data
    elif isinstance(data, dict) and "cases" in data:
        return data["cases"]
    else:
        raise ValueError("JSON格式不正确，期望列表或包含'cases'键的对象")

def import_from_csv(file_path: str, mapping: Dict) -> List[Dict]:
    """从CSV文件导入案例"""
    import csv

    cases = []
    with open(file_path, 'r', encoding='utf-8') as f:
        reader = csv.DictReader(f)
        for row in reader:
            case = {
                "id": str(row.get(mapping.get("id", "id"), len(cases))),
                "title": row.get(mapping.get("title", "title"), ""),
                "content": row.get(mapping.get("content", "content"), ""),
                "metadata": {}
            }

            for field in ["category", "tags", "created_at"]:
                if field in mapping and mapping[field] in row:
                    case["metadata"][field] = row[mapping[field]]

            cases.append(case)

    return cases

def store_to_chroma(cases: List[Dict], config: Dict,
                   collection_name: str = "cases",
                   chroma_path: str = None) -> Dict:
    """
    将案例存储到Chroma（定长向量化，每篇案例1个向量）
    使用PersistentClient本地持久化模式（无需Docker）

    Args:
        cases: 案例列表
        config: 配置字典
        collection_name: Collection名称
        chroma_path: Chroma本地存储路径（默认: ~/.local/share/chroma_rag）

    Returns:
        统计信息
    """
    import chromadb
    from pathlib import Path

    # 设置默认存储路径
    if chroma_path is None:
        chroma_path = str(Path.home() / ".local" / "share" / "chroma_rag")

    client = chromadb.PersistentClient(path=chroma_path)

    embedding_model = get_embedding_config(config)["model"]
    embedding_dimension = get_embedding_config(config)["dimension"]

    # 创建或获取collection（使用cosine距离）
    try:
        collection = client.get_collection(name=collection_name)
        print(f"  使用已有Collection: {collection_name}")
    except:
        collection = client.create_collection(
            name=collection_name,
            metadata={
                "hnsw:space": "cosine",
                "description": "案例检索向量库",
                "embedding_model": embedding_model,
                "embedding_dimension": embedding_dimension,
                "vectorization_strategy": "fixed_length",
                "created_at": datetime.now().isoformat()
            }
        )
        print(f"  创建新Collection: {collection_name} (cosine距离)")

    stats = {
        "total_cases": len(cases),
        "successful": 0,
        "failed": 0,
        "errors": []
    }

    # 准备批量数据
    all_ids = []
    all_texts = []
    all_metadatas = []
    all_documents = []

    for case in cases:
        try:
            # 验证必需字段
            if not case.get("id") or not case.get("content"):
                stats["failed"] += 1
                stats["errors"].append({
                    "case_id": case.get("id", "unknown"),
                    "error": "Missing required fields (id or content)"
                })
                continue

            # 准备定长文本
            vector_text = prepare_fixed_length_text(
                case.get("title", ""),
                case.get("content", ""),
                config
            )

            # 构建元数据
            metadata = {
                "doc_id": case["id"],
                "title": case.get("title", "")[:500],  # 限制元数据长度
                "content_length": len(case.get("content", "")),
                "vector_strategy": "fixed_length"
            }

            # 添加额外元数据
            if "metadata" in case:
                for key, value in case["metadata"].items():
                    if isinstance(value, (str, int, float, bool)):
                        metadata[key] = value

            all_ids.append(case["id"])
            all_texts.append(vector_text)
            all_metadatas.append(metadata)
            all_documents.append(case.get("content", "")[:2000])  # 存储原始内容预览

            stats["successful"] += 1

        except Exception as e:
            stats["failed"] += 1
            stats["errors"].append({
                "case_id": case.get("id", "unknown"),
                "error": str(e)
            })

    # 批量生成向量
    if all_texts:
        print(f"  生成向量中... ({len(all_texts)} 条案例)")
        try:
            all_embeddings = get_embeddings(all_texts, config)
        except Exception as e:
            print(f"  ❌ 向量生成失败: {str(e)}")
            stats["errors"].append({"error": f"Embedding failed: {str(e)}"})
            return stats

        # 批量添加到Chroma
        print(f"  存储到Chroma...")
        batch_size = 100
        for i in range(0, len(all_ids), batch_size):
            collection.add(
                ids=all_ids[i:i + batch_size],
                embeddings=all_embeddings[i:i + batch_size],
                metadatas=all_metadatas[i:i + batch_size],
                documents=all_documents[i:i + batch_size]
            )

    return stats

def main():
    """主函数 - 从命令行或配置文件读取参数"""
    import argparse

    parser = argparse.ArgumentParser(
        description="导入案例到Chroma向量库（定长向量化策略）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
示例:
  %(prog)s --source json --file cases.json
  %(prog)s --source csv --file cases.csv
  %(prog)s --source database --config config.json

向量策略:
  每篇案例生成1个向量，包含：标题注入 + 内容前400字符
  默认模型: bge-large-zh (1024维)
  距离度量: cosine
        """
    )
    parser.add_argument("--source", help="数据源类型: database, json, csv")
    parser.add_argument("--file", help="JSON或CSV文件路径")
    parser.add_argument("--config", help="配置文件路径")
    parser.add_argument("--collection", default="cases", help="Collection名称")

    args = parser.parse_args()

    # 读取配置
    if args.config:
        with open(args.config) as f:
            config = json.load(f)
    else:
        # 尝试从默认配置读取
        config_path = Path.home() / ".claude" / "skills" / "rag-case-retrieval" / "config.json"
        if config_path.exists():
            with open(config_path) as f:
                config = json.load(f)
        else:
            print("❌ 未找到配置文件，请使用--config参数或创建默认配置")
            return 1

    # 打印配置信息
    print("=" * 60)
    print("案例导入 - 定长向量化策略")
    print("=" * 60)
    ec = get_embedding_config(config)
    vec_config = config.get("vectorization", {})
    print(f"嵌入模型: {ec['model']}")
    print(f"向量维度: {ec['dimension']}")
    print(f"距离度量: cosine")
    print(f"向量化策略: 定长 (头{vec_config.get('head_chars', 400)}字符 + 标题注入)")
    print("=" * 60)

    # 读取案例
    cases = []

    if args.source == "json" and args.file:
        print(f"\n从JSON文件导入: {args.file}")
        cases = import_from_json(args.file)
    elif args.source == "csv" and args.file:
        print(f"从CSV文件导入: {args.file}")
        mapping = config.get("import_mapping", {})
        cases = import_from_csv(args.file, mapping)
    elif args.source == "database":
        print("从数据库导入...")
        db_config = config.get("database", {})
        query = config.get("import_query", "SELECT * FROM cases")
        mapping = config.get("import_mapping", {})
        cases = import_from_database(db_config, query, mapping)
    else:
        print("❌ 请指定数据源类型和文件路径")
        print("   示例: --source json --file cases.json")
        return 1

    print(f"读取到 {len(cases)} 条案例")

    if not cases:
        print("❌ 没有读取到任何案例")
        return 1

    # 存储到Chroma（本地持久化模式）
    chroma_path = config.get("chroma", {}).get("path", None)

    print(f"\n导入到Chroma...")
    if chroma_path:
        print(f"  存储路径: {chroma_path}")
    else:
        print(f"  存储路径: ~/.local/share/chroma_rag (默认)")
    start_time = time.time()
    stats = store_to_chroma(
        cases,
        config=config,
        collection_name=args.collection,
        chroma_path=chroma_path
    )
    elapsed = time.time() - start_time

    # 输出统计
    print("\n" + "=" * 60)
    print("导入完成:")
    print(f"  ✅ 成功: {stats['successful']}")
    print(f"  ❌ 失败: {stats['failed']}")
    print(f"  ⏱️  耗时: {elapsed:.2f}s")

    if stats['errors']:
        print(f"\n错误详情 ({len(stats['errors'])} 条):")
        for error in stats['errors'][:5]:  # 只显示前5个
            print(f"  - {error.get('case_id', 'unknown')}: {error.get('error', 'unknown')}")

    print("=" * 60)
    return 0

if __name__ == "__main__":
    sys.exit(main())