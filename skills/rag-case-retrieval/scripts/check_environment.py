#!/usr/bin/env python3
"""
环境检查脚本 - 验证Chroma连接和依赖
"""

import os
import sys
import json
import sqlite3
from pathlib import Path
from typing import Dict

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
        return True, current_version

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
            return True, pysqlite3_module.sqlite_version
    except ImportError:
        pass

    # 无法满足版本要求，返回False
    _apply_sqlite3_thread_patch()
    return False, current_version


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
_sqlite3_ok, _sqlite3_version = _apply_sqlite3_compatibility_patch()

def check_dependencies():
    """检查Python依赖"""
    missing = []
    try:
        import chromadb
    except ImportError:
        missing.append("chromadb")

    try:
        import openai
    except ImportError:
        missing.append("openai")

    return missing

def check_chroma_local(chroma_path=None):
    """检查Chroma本地持久化存储"""
    try:
        import chromadb
        from pathlib import Path

        if chroma_path is None:
            chroma_path = str(Path.home() / ".local" / "share" / "chroma_rag")

        client = chromadb.PersistentClient(path=chroma_path)
        # 测试连接（尝试获取collections列表）
        collections = client.list_collections()
        return True, f"Chroma本地存储正常 (路径: {chroma_path}, collections: {len(collections)})"
    except Exception as e:
        return False, f"无法访问Chroma本地存储: {str(e)}"

def check_embedding_service(config: Dict):
    """检查嵌入服务连接"""
    from openai import OpenAI

    ec = get_embedding_config(config)

    # 测试连接通过生成一个测试嵌入
    try:
        client = OpenAI(
            base_url=ec["base_url"],
            api_key=ec["api_key"],
            timeout=ec["timeout"]
        )

        response = client.embeddings.create(
            model=ec["model"],
            input="test"
        )

        if response.data and len(response.data) > 0:
            embedding_dim = len(response.data[0].embedding)
            return True, f"嵌入服务连接正常 (模型: {ec['model']}, 维度: {embedding_dim}, 端点: {ec['base_url']})"
        else:
            return False, f"嵌入服务响应异常: 未返回向量数据"

    except Exception as e:
        error_msg = str(e)
        suggestions = []

        if "Connection refused" in error_msg or "connect" in error_msg.lower():
            suggestions = [
                "请确保嵌入服务正在运行",
                f"检查 {ec['base_url']} 是否可访问",
                "如果是Ollama: ollama serve",
                "如果是text-embeddings-inference: 检查Docker容器状态"
            ]
        elif "model" in error_msg.lower() and "not found" in error_msg.lower():
            suggestions = [
                f"请拉取模型: ollama pull {ec['model']}",
                f"或设置EMBEDDING_MODEL环境变量为可用模型"
            ]
        else:
            suggestions = [
                f"检查服务配置: {ec['base_url']}",
                "验证模型名称是否正确",
                "检查服务日志"
            ]

        return False, f"无法连接嵌入服务: {error_msg}\n建议:\n" + "\n".join(f"  - {s}" for s in suggestions)

def check_collection(collection_name="cases", chroma_path=None):
    """检查Collection状态（本地持久化模式）"""
    try:
        import chromadb
        from pathlib import Path

        if chroma_path is None:
            chroma_path = str(Path.home() / ".local" / "share" / "chroma_rag")

        client = chromadb.PersistentClient(path=chroma_path)
        collection = client.get_collection(name=collection_name)
        count = collection.count()
        metadata = collection.metadata or {}
        return True, {
            "name": collection_name,
            "count": count,
            "metadata": metadata,
            "path": chroma_path,
            "distance_metric": metadata.get("hnsw:space", "unknown"),
            "embedding_model": metadata.get("embedding_model", "unknown"),
            "embedding_dimension": metadata.get("embedding_dimension", "unknown"),
            "vectorization_strategy": metadata.get("vectorization_strategy", "unknown")
        }
    except Exception as e:
        return False, str(e)

def main():
    print("=" * 60)
    print("RAG案例检索 - 环境检查")
    print("=" * 60)

    # 显示sqlite3版本信息（使用全局变量）
    global _sqlite3_ok, _sqlite3_version
    if _sqlite3_ok:
        print(f"sqlite3版本: {_sqlite3_version} ✅ (满足Chroma >= 3.35.0要求)")
    else:
        print(f"sqlite3版本: {_sqlite3_version} ⚠️ (低于3.35.0，建议安装pysqlite3-binary)")

    # 先读取配置
    config_path = Path.home() / ".claude" / "skills" / "rag-case-retrieval" / "config.json"
    config = {}
    if config_path.exists():
        with open(config_path) as f:
            config = json.load(f)
        print(f"配置文件: {config_path}")
    else:
        config = {
            "vectorization": {
                "head_chars": 400,
                "title_injection": True
            }
        }
        print("配置文件: 未找到，使用默认配置")

    # 显示配置信息
    ec = get_embedding_config(config)
    vec_config = config.get("vectorization", {})
    chroma_config = config.get("chroma", {})
    print(f"\n当前配置:")
    print(f"  嵌入模型: {ec['model']}")
    print(f"  向量维度: {ec['dimension']}")
    print(f"  向量化策略: 定长 (头{vec_config.get('head_chars', 400)}字符 + 标题注入)")
    if chroma_config.get("path"):
        print(f"  Chroma路径: {chroma_config['path']}")
    else:
        print(f"  Chroma路径: ~/.local/share/chroma_rag (默认)")

    # 1. 检查依赖
    print("\n[1/4] 检查Python依赖...")
    missing = check_dependencies()
    if missing:
        print(f"  ❌ 缺少依赖: {', '.join(missing)}")
        print(f"  安装命令: pip install {' '.join(missing)}")
        return 1
    print("  ✅ 所有依赖已安装")

    # 2. 检查嵌入服务
    print("\n[2/4] 检查嵌入服务...")
    success, msg = check_embedding_service(config)
    if success:
        print(f"  ✅ {msg}")
    else:
        print(f"  ❌ {msg}")
        return 1

    # 3. 检查Chroma本地存储
    print("\n[3/4] 检查Chroma本地存储...")
    chroma_path = config.get("chroma", {}).get("path", None)
    if chroma_path:
        print(f"  配置路径: {chroma_path}")
    else:
        print(f"  默认路径: ~/.local/share/chroma_rag")

    success, msg = check_chroma_local(chroma_path)
    if success:
        print(f"  ✅ {msg}")
    else:
        print(f"  ❌ {msg}")
        return 1

    # 4. 检查Collection
    print("\n[4/4] 检查Collection状态...")
    collection_name = config.get("chroma", {}).get("collection_name", "cases")

    success, result = check_collection(collection_name, chroma_path)
    if success:
        print(f"  ✅ Collection '{result['name']}' 存在")
        print(f"     - 文档数量: {result['count']}")
        print(f"     - 距离度量: {result.get('distance_metric', 'cosine')}")
        print(f"     - 嵌入模型: {result.get('embedding_model', 'unknown')}")
        print(f"     - 向量维度: {result.get('embedding_dimension', 'unknown')}")
        print(f"     - 向量化策略: {result.get('vectorization_strategy', 'unknown')}")
    else:
        print(f"  ⚠️  Collection '{collection_name}' 不存在或为空")
        print("     需要先导入案例数据")

    print("\n" + "=" * 60)
    print("环境检查完成")
    print("=" * 60)
    return 0

if __name__ == "__main__":
    sys.exit(main())