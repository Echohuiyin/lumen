#!/usr/bin/env python3
"""
案例检索脚本 - 从Chroma向量库检索最相关的案例
定长向量化策略：每篇案例1个向量（头400字符+标题注入）
距离度量：cosine
输出：Top-K JSON
"""

import os
import sys
import json
import time
import sqlite3
from pathlib import Path
from typing import List, Dict, Any, Optional

from config_loader import get_embedding_config

# ============================================================
# sqlite3兼容补丁：Chroma要求sqlite3 >= 3.35.0
# ============================================================

def _apply_sqlite3_compatibility_patch():
    """
    应用sqlite3兼容补丁，解决Chroma的sqlite3版本要求问题

    Chroma要求sqlite3 >= 3.35.0，但系统默认sqlite3可能版本较低
    此补丁尝试使用pysqlite3-binary（如果可用）替换系统sqlite3
    """
    import sqlite3

    # 检查当前sqlite3版本
    current_version = sqlite3.sqlite_version
    required_version = "3.35.0"

    # 版本比较
    def version_ge(v1, v2):
        parts1 = [int(x) for x in v1.split('.')]
        parts2 = [int(x) for x in v2.split('.')]
        for i in range(max(len(parts1), len(parts2))):
            p1 = parts1[i] if i < len(parts1) else 0
            p2 = parts2[i] if i < len(parts2) else 0
            if p1 > p2:
                return True
            if p1 < p2:
                return False
        return True

    if version_ge(current_version, required_version):
        # 版本满足要求，应用线程安全patch
        _apply_sqlite3_thread_patch()
        return

    # 版本不满足，尝试使用pysqlite3-binary
    try:
        import pysqlite3 as pysqlite3_module
        # 检查pysqlite3版本
        if version_ge(pysqlite3_module.sqlite_version, required_version):
            # 替换sqlite3模块
            sys.modules["sqlite3"] = pysqlite3_module
            # 应用线程安全patch到新模块
            import sqlite3  # 重新导入获取pysqlite3
            _apply_sqlite3_thread_patch()
            return
    except ImportError:
        pass

    # 无法满足版本要求，打印警告（静默模式）
    _apply_sqlite3_thread_patch()


def _apply_sqlite3_thread_patch():
    """应用sqlite3线程安全patch"""
    import sqlite3

    if not hasattr(sqlite3, '_original_connect_saved'):
        sqlite3._original_connect_saved = sqlite3.connect

    def _patched_connect(database, **kwargs):
        kwargs['check_same_thread'] = False
        return sqlite3._original_connect_saved(database, **kwargs)

    sqlite3.connect = _patched_connect


# 在导入时应用补丁
_apply_sqlite3_compatibility_patch()


def get_query_embedding(query: str, config: Dict) -> List[float]:
    """将查询文本转为向量 (使用本地OpenAI兼容服务)"""
    from openai import OpenAI

    ec = get_embedding_config(config)

    client = OpenAI(
        base_url=ec["base_url"],
        api_key=ec["api_key"],
        timeout=ec["timeout"]
    )

    try:
        response = client.embeddings.create(
            model=ec["model"],
            input=query
        )
        return response.data[0].embedding
    except Exception as e:
        raise Exception(f"Failed to generate embedding from {ec['base_url']}: {str(e)}")

def retrieve_cases(query_embedding: List[float],
                  collection_name: str = "cases",
                  chroma_path: str = None,
                  top_k: int = 3,
                  min_similarity: float = 0.7,
                  filters: Optional[Dict] = None) -> List[Dict]:
    """
    从Chroma检索最相关的案例（cosine距离）
    使用PersistentClient本地持久化模式（无需Docker）

    Args:
        query_embedding: 查询向量
        collection_name: Collection名称
        chroma_path: Chroma本地存储路径（默认: ~/.local/share/chroma_rag）
        top_k: 返回数量
        min_similarity: 最小相似度阈值
        filters: 元数据过滤条件

    Returns:
        案例列表
    """
    import chromadb
    from pathlib import Path

    # 设置默认存储路径
    if chroma_path is None:
        chroma_path = str(Path.home() / ".local" / "share" / "chroma_rag")

    client = chromadb.PersistentClient(path=chroma_path)

    try:
        collection = client.get_collection(name=collection_name)
    except Exception as e:
        raise Exception(f"Collection '{collection_name}' 不存在: {str(e)}")

    # Chroma查询（使用cosine距离，返回距离值）
    results = collection.query(
        query_embeddings=[query_embedding],
        n_results=top_k * 2,  # 获取更多结果用于过滤
        where=filters if filters else None,
        include=["documents", "metadatas", "distances"]
    )

    # 处理结果
    cases = []

    if not results['ids'] or not results['ids'][0]:
        return cases

    for i in range(len(results['ids'][0])):
        distance = results['distances'][0][i]

        # cosine距离转换为相似度 (1 - distance)
        similarity = 1 - distance

        # 过滤低相似度
        if similarity < min_similarity:
            continue

        case = {
            "id": results['ids'][0][i],
            "title": results['metadatas'][0][i].get('title', ''),
            "content": results['documents'][0][i],
            "similarity_score": round(similarity, 4),
            "distance": round(distance, 4),
            "metadata": {}
        }

        # 提取其他元数据
        for key, value in results['metadatas'][0][i].items():
            if key not in ['doc_id', 'title', 'vector_strategy']:
                case['metadata'][key] = value

        cases.append(case)

        # 达到top_k数量就停止
        if len(cases) >= top_k:
            break

    # 按相似度排序
    cases.sort(key=lambda x: x['similarity_score'], reverse=True)

    return cases

def format_output(query: str, cases: List[Dict], config: Dict,
                  retrieval_time: float, embedding_time: float,
                  top_k: int, min_similarity: float) -> Dict:
    """格式化输出结果为Top-K JSON"""
    output = {
        "status": "success" if cases else "no_results",
        "query": query,
        "retrieval_config": {
            "top_k": top_k,
            "min_similarity": min_similarity,
            "filters": config.get("filters", {}),
            "embedding_model": get_embedding_config(config)["model"],
            "distance_metric": "cosine"
        },
        "results": cases,
        "summary": {
            "total_found": len(cases),
            "above_threshold": len([c for c in cases if c['similarity_score'] >= min_similarity]),
            "returned": len(cases),
            "retrieval_time_ms": int(retrieval_time * 1000),
            "embedding_time_ms": int(embedding_time * 1000),
            "search_time_ms": int((retrieval_time - embedding_time) * 1000) if retrieval_time > embedding_time else 0
        }
    }

    # 如果没有结果，添加建议
    if not cases:
        output["message"] = f"未找到相似度高于{min_similarity}的结果"
        output["suggestions"] = [
            "尝试降低相似度阈值 (--min-similarity)",
            "使用更通用的查询词",
            "检查过滤条件是否过于严格",
            "确认案例库中是否有相关案例"
        ]

    return output

def main():
    """主函数 - 执行案例检索"""
    import argparse

    parser = argparse.ArgumentParser(
        description="从向量库检索案例（cosine距离）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
示例:
  %(prog)s "用户认证失败"
  %(prog)s "kernel crash" --top-k 5 --min-similarity 0.6
  %(prog)s "内存泄漏" --output results.json

输出格式:
  Top-K JSON，包含: status, query, results, summary
  每个结果包含: id, title, content, similarity_score, distance, metadata
        """
    )
    parser.add_argument("query", help="查询文本")
    parser.add_argument("--top-k", type=int, default=None, help="返回案例数量 (默认: 3)")
    parser.add_argument("--min-similarity", type=float, default=None, help="最小相似度阈值 (默认: 0.7)")
    parser.add_argument("--filters", help="过滤条件(JSON格式)")
    parser.add_argument("--output", help="输出文件路径")
    parser.add_argument("--collection", default="cases", help="Collection名称")

    args = parser.parse_args()

    # 读取配置
    config_path = Path.home() / ".claude" / "skills" / "rag-case-retrieval" / "config.json"
    if config_path.exists():
        with open(config_path) as f:
            config = json.load(f)
    else:
        config = {
            "chroma": {"host": "http://localhost:8000"},
            "retrieval": {
                "default_top_k": 3,
                "min_similarity": 0.7,
                "distance_metric": "cosine"
            }
        }

    # 解析过滤条件
    filters = None
    if args.filters:
        filters = json.loads(args.filters)

    # 合并命令行参数
    top_k = args.top_k if args.top_k else config.get("retrieval", {}).get("default_top_k", 3)
    min_similarity = args.min_similarity if args.min_similarity else config.get("retrieval", {}).get("min_similarity", 0.7)

    chroma_path = config.get("chroma", {}).get("path", None)
    collection_name = args.collection
    ec = get_embedding_config(config)

    # 打印检索信息
    print("=" * 60)
    print("案例检索")
    print("=" * 60)
    print(f"查询: {args.query}")
    print(f"参数: top_k={top_k}, min_similarity={min_similarity}")
    print(f"模型: {ec['model']} ({ec['dimension']}维)")
    print(f"距离: cosine")
    if chroma_path:
        print(f"存储: {chroma_path}")
    else:
        print(f"存储: ~/.local/share/chroma_rag (默认)")
    print("=" * 60)

    # 生成查询向量
    print("\n[1/2] 生成查询向量...")
    start_embedding = time.time()
    try:
        query_embedding = get_query_embedding(args.query, config)
        embedding_time = time.time() - start_embedding
        print(f"  ✅ 完成 ({embedding_time:.2f}s, 维度: {len(query_embedding)})")
    except Exception as e:
        print(f"  ❌ 向量生成失败: {str(e)}")
        output = {
            "status": "error",
            "error_code": "EMBEDDING_FAILED",
            "message": str(e),
            "query": args.query
        }
        print("\n错误输出:")
        print(json.dumps(output, indent=2, ensure_ascii=False))
        return 1

    # 检索案例
    print("\n[2/2] 检索案例...")
    start_retrieval = time.time()
    try:
        cases = retrieve_cases(
            query_embedding,
            collection_name=collection_name,
            chroma_path=chroma_path,
            top_k=top_k,
            min_similarity=min_similarity,
            filters=filters
        )
    except Exception as e:
        print(f"  ❌ 检索失败: {str(e)}")
        output = {
            "status": "error",
            "error_code": "RETRIEVAL_FAILED",
            "message": str(e),
            "query": args.query
        }
        print("\n错误输出:")
        print(json.dumps(output, indent=2, ensure_ascii=False))
        return 1

    retrieval_time = time.time() - start_retrieval
    print(f"  ✅ 找到 {len(cases)} 条案例 ({retrieval_time:.2f}s)")

    # 格式化输出
    output = format_output(args.query, cases, config, retrieval_time, embedding_time,
                          top_k, min_similarity)

    # 显示结果摘要
    print("\n" + "=" * 60)
    print("检索结果摘要:")
    print("=" * 60)

    if cases:
        for i, case in enumerate(cases, 1):
            print(f"\n[{i}] ID: {case['id']}")
            print(f"    标题: {case['title'][:50]}...")
            print(f"    相似度: {case['similarity_score']:.4f} (距离: {case['distance']:.4f})")
            if case['metadata']:
                print(f"    元数据: {json.dumps(case['metadata'], ensure_ascii=False)[:100]}...")
    else:
        print("\n无匹配结果")

    # 保存到文件
    if args.output:
        with open(args.output, 'w', encoding='utf-8') as f:
            json.dump(output, f, indent=2, ensure_ascii=False)
        print(f"\n✅ 结果已保存到: {args.output}")

    # 输出完整JSON
    print("\n" + "=" * 60)
    print("Top-K JSON 输出:")
    print("=" * 60)
    print(json.dumps(output, indent=2, ensure_ascii=False))

    return 0

if __name__ == "__main__":
    sys.exit(main())