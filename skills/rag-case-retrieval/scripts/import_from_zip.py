#!/usr/bin/env python3
"""
ZIP包导入脚本（高性能版本）- 递归扫描ZIP包，内存流处理，批量向量化
优化：
- 直接内存流处理ZIP内容，避免磁盘IO
- 批量提取Markdown文档
- 并行化向量化请求
- 流式写入Chroma
"""

import os
import sys
import json
import time
import zipfile
import re
import hashlib
from pathlib import Path
from typing import List, Dict, Any, Iterator, Optional
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor, as_completed
import threading

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
            print(f"  ℹ️ 使用pysqlite3 (版本 {pysqlite3_module.sqlite_version}) 替换系统sqlite3")
            # 应用线程安全patch到新模块
            import sqlite3  # 重新导入获取pysqlite3
            _apply_sqlite3_thread_patch()
            return
    except ImportError:
        pass

    # 无法满足版本要求，打印警告
    print(f"  ⚠️ sqlite3版本 {current_version} < {required_version}，Chroma可能无法正常工作")
    print(f"  建议: pip install pysqlite3-binary")
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


# ============================================================
# 标题和内容处理（保持原有逻辑）
# ============================================================

def extract_wiki_title_from_filename(file_path: str) -> str:
    """从Wiki风格文件名提取标题"""
    filename = Path(file_path).stem
    filename = re.sub(r'^[\d]+[-_]', '', filename)
    filename = re.sub(r'^第[\d]+[章节篇][-_]', '', filename)
    filename = re.sub(r'[_\-]+', ' ', filename)
    filename = filename.strip()

    if re.match(r'^[一-鿿]+$', filename):
        return filename

    words = filename.split()
    title_words = []
    for word in words:
        if word.isupper():
            title_words.append(word)
        else:
            title_words.append(word.capitalize())

    return ' '.join(title_words)


def extract_markdown_title(content: str, file_path: str) -> str:
    """从Markdown内容提取标题（Wiki文件名优先）"""
    wiki_title = extract_wiki_title_from_filename(file_path)
    if wiki_title and len(wiki_title) > 2:
        return wiki_title

    frontmatter_match = re.match(r'^---\s*\n(.*?)\n---\s*\n', content, re.DOTALL)
    if frontmatter_match:
        frontmatter = frontmatter_match.group(1)
        title_match = re.search(r'^title:\s*(.+)$', frontmatter, re.MULTILINE)
        if title_match:
            return title_match.group(1).strip().strip('"').strip("'")

    heading_match = re.search(r'^#\s+(.+)$', content, re.MULTILINE)
    if heading_match:
        return heading_match.group(1).strip()

    return Path(file_path).stem


def extract_markdown_metadata(content: str) -> Dict[str, Any]:
    """从Markdown frontmatter提取元数据"""
    metadata = {}
    frontmatter_match = re.match(r'^---\s*\n(.*?)\n---\s*\n', content, re.DOTALL)
    if frontmatter_match:
        frontmatter = frontmatter_match.group(1)
        fields = ['date', 'category', 'tags', 'author', 'source', 'keywords']
        for field in fields:
            match = re.search(rf'^{field}:\s*(.+)$', frontmatter, re.MULTILINE)
            if match:
                value = match.group(1).strip().strip('"').strip("'")
                if field == 'tags' and value.startswith('['):
                    value = [t.strip().strip('"').strip("'") for t in value[1:-1].split(',')]
                metadata[field] = value
    return metadata


def clean_markdown_content(content: str) -> str:
    """清理Markdown内容"""
    content = re.sub(r'^---\s*\n.*?\n---\s*\n', '', content, flags=re.DOTALL)
    content = re.sub(r'```[\w]*\n', '', content)
    content = re.sub(r'```', '', content)
    content = re.sub(r'`([^`]+)`', r'\1', content)
    content = re.sub(r'!\[.*?\]\(.*?\)', '', content)
    content = re.sub(r'\[([^\]]+)\]\([^\)]+\)', r'\1', content)
    content = re.sub(r'<[^>]+>', '', content)
    content = re.sub(r'^#{1,6}\s+', '', content, flags=re.MULTILINE)
    content = re.sub(r'^[\*\-\+]\s+', '', content, flags=re.MULTILINE)
    content = re.sub(r'^\d+\.\s+', '', content, flags=re.MULTILINE)
    content = re.sub(r'\n{3,}', '\n\n', content)
    return content.strip()


# ============================================================
# 内存流ZIP处理（核心优化）
# ============================================================

def extract_markdown_from_zip_stream(zf: zipfile.ZipFile, member_path: str,
                                     source_zip: str) -> Optional[Dict[str, Any]]:
    """
    直接从ZIP内存流提取Markdown文档（无磁盘IO）

    Args:
        zf: ZipFile对象（已打开）
        member_path: ZIP内成员路径
        source_zip: 来源ZIP文件名

    Returns:
        案例字典或None
    """
    try:
        with zf.open(member_path) as f:
            raw_content = f.read().decode('utf-8', errors='ignore')

        # 提取标题（优先Wiki文件名）
        title = extract_markdown_title(raw_content, member_path)

        # 提取元数据
        metadata = extract_markdown_metadata(raw_content)

        # 清理内容
        clean_content = clean_markdown_content(raw_content)

        if len(clean_content) < 50:  # 过滤太短的文档
            return None

        # 添加来源信息
        metadata['source_zip'] = source_zip
        metadata['source_path'] = member_path

        # 生成唯一ID（基于来源ZIP+路径hash，消除跨ZIP冲突）
        # 使用source_zip路径+member_path组合生成hash
        unique_key = f"{source_zip}:{member_path}"
        full_hash = hashlib.md5(unique_key.encode()).hexdigest()[:12]
        # ID格式: sanitized_path + hash（包含ZIP来源信息）
        path_sanitized = re.sub(r'[^\w]', '_', member_path.replace('.md', ''))[:50]
        case_id = f"{path_sanitized}_{full_hash}"

        return {
            "id": case_id,
            "title": title,
            "content": clean_content,
            "metadata": metadata
        }

    except Exception as e:
        return None


def iterate_zip_markdown(zip_path: str, max_depth: int = 10,
                         depth: int = 0) -> Iterator[Dict[str, Any]]:
    """
    递归迭代ZIP中的Markdown文件（内存流处理）

    Args:
        zip_path: ZIP文件路径
        max_depth: 最大递归深度
        depth: 当前深度

    Yields:
        案例字典
    """
    if depth > max_depth:
        return

    try:
        with zipfile.ZipFile(zip_path, 'r') as zf:
            for member in zf.namelist():
                if member.endswith('/'):
                    continue

                # Markdown文件直接提取
                if member.lower().endswith('.md'):
                    case = extract_markdown_from_zip_stream(zf, member, zip_path)
                    if case:
                        yield case

                # 嵌套ZIP递归处理（写入临时文件，处理后删除）
                elif member.lower().endswith('.zip'):
                    import tempfile
                    with zf.open(member) as source:
                        # 写入临时ZIP文件
                        with tempfile.NamedTemporaryFile(suffix='.zip', delete=False) as tmp:
                            tmp.write(source.read())
                            tmp_path = tmp.name

                    try:
                        # 递归迭代嵌套ZIP
                        for case in iterate_zip_markdown(tmp_path, max_depth, depth + 1):
                            case['metadata']['nested_from'] = member
                            yield case
                    finally:
                        # 清理临时文件
                        os.unlink(tmp_path)

    except zipfile.BadZipFile:
        pass
    except Exception as e:
        print(f"  ⚠️ ZIP处理失败: {zip_path} - {str(e)}")


# ============================================================
# 并行向量化
# ============================================================

class EmbeddingBatchProcessor:
    """批量并行向量化处理器"""

    def __init__(self, config: Dict, batch_size: int = 50, parallel_workers: int = 4):
        self.config = config
        self.batch_size = batch_size
        self.parallel_workers = parallel_workers
        self._client = None
        self._lock = threading.Lock()

    def _get_client(self):
        """懒加载OpenAI客户端"""
        if self._client is None:
            from openai import OpenAI
            ec = get_embedding_config(self.config)
            self._client = OpenAI(
                base_url=ec["base_url"],
                api_key=ec["api_key"],
                timeout=ec["timeout"]
            )
        return self._client

    def embed_batch(self, texts: List[str]) -> List[List[float]]:
        """生成一批向量"""
        client = self._get_client()
        model = get_embedding_config(self.config)["model"]

        response = client.embeddings.create(model=model, input=texts)
        return [item.embedding for item in response.data]

    def embed_all(self, texts: List[str], progress_interval: int = 100) -> List[List[float]]:
        """
        并行生成所有向量

        Args:
            texts: 文本列表
            progress_interval: 进度输出间隔

        Returns:
            向量列表
        """
        if len(texts) <= self.batch_size:
            return self.embed_batch(texts)

        # 分批并行处理
        batches = [texts[i:i + self.batch_size] for i in range(0, len(texts), self.batch_size)]
        all_embeddings = []

        with ThreadPoolExecutor(max_workers=self.parallel_workers) as executor:
            futures = {executor.submit(self.embed_batch, batch): i
                       for i, batch in enumerate(batches)}

            completed = 0
            for future in as_completed(futures):
                batch_idx = futures[future]
                embeddings = future.result()
                all_embeddings.extend(embeddings)
                completed += 1

                if completed % (self.parallel_workers * 2) == 0 or completed == len(batches):
                    print(f"    向量化进度: {len(all_embeddings)}/{len(texts)}")

        return all_embeddings


# ============================================================
# Chroma存储（带sqlite3 monkey-patch）
# ============================================================

def apply_sqlite3_monkey_patch():
    """
    应用sqlite3 monkey-patch解决Chroma线程安全问题

    Chroma底层使用sqlite3，多线程环境下需要此patch
    正确实现：将原始connect保存到模块属性，然后替换为patched版本
    """
    import sqlite3

    # 保存原始函数到模块属性（避免闭包引用问题）
    if not hasattr(sqlite3, '_original_connect_saved'):
        sqlite3._original_connect_saved = sqlite3.connect

    def _patched_connect(database, **kwargs):
        """Monkey-patched sqlite3.connect with thread safety"""
        kwargs['check_same_thread'] = False
        return sqlite3._original_connect_saved(database, **kwargs)

    sqlite3.connect = _patched_connect


def prepare_fixed_length_text(title: str, content: str, config: Dict) -> str:
    """准备定长文本用于向量化"""
    vec_config = config.get("vectorization", {})
    head_chars = vec_config.get("head_chars", 400)
    title_injection = vec_config.get("title_injection", True)

    title = title.strip() if title else ""
    content = content.strip() if content else ""

    if title_injection and title:
        return f"{title}\n\n{content[:head_chars]}"
    return content[:head_chars]


def store_to_chroma_stream(cases: Iterator[Dict[str, Any]], config: Dict,
                           collection_name: str = "cases",
                           chroma_path: str = None,
                           batch_size: int = 200) -> Dict:
    """
    流式存储案例到Chroma（批量写入优化）
    使用PersistentClient本地持久化模式（无需Docker）

    Args:
        cases: 案例迭代器
        config: 配置
        collection_name: Collection名称
        chroma_path: Chroma本地存储路径（默认: ~/.local/share/chroma_rag）
        batch_size: 批量写入大小

    Returns:
        统计信息
    """
    apply_sqlite3_monkey_patch()

    import chromadb
    from pathlib import Path

    # 设置默认存储路径
    if chroma_path is None:
        chroma_path = str(Path.home() / ".local" / "share" / "chroma_rag")

    client = chromadb.PersistentClient(path=chroma_path)

    embedding_model = get_embedding_config(config)["model"]
    embedding_dimension = get_embedding_config(config)["dimension"]

    # 创建或获取collection
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
                "created_at": datetime.now().isoformat()
            }
        )
        print(f"  创建新Collection: {collection_name}")

    # 收集案例并批量处理
    batch_cases = []
    stats = {"total": 0, "successful": 0, "failed": 0}

    for case in cases:
        if not case or not case.get("id") or not case.get("content"):
            stats["failed"] += 1
            continue

        batch_cases.append(case)
        stats["total"] += 1

        # 达到批量大小时处理
        if len(batch_cases) >= batch_size:
            _process_and_store_batch(collection, batch_cases, config, stats)
            batch_cases = []
            print(f"    已处理: {stats['successful']}/{stats['total']}")

    # 处理剩余批次
    if batch_cases:
        _process_and_store_batch(collection, batch_cases, config, stats)

    return stats


def _process_and_store_batch(collection, cases: List[Dict], config: Dict, stats: Dict):
    """处理并存储一个批次"""
    # 准备向量文本
    texts = [
        prepare_fixed_length_text(c.get("title", ""), c.get("content", ""), config)
        for c in cases
    ]

    # 生成向量（使用批量处理器）
    processor = EmbeddingBatchProcessor(config)
    embeddings = processor.embed_all(texts)

    # 准备元数据
    ids = [c["id"] for c in cases]
    metadatas = []
    documents = []

    for case in cases:
        metadata = {
            "doc_id": case["id"],
            "title": case.get("title", "")[:500],
            "content_length": len(case.get("content", ""))
        }
        for key, value in case.get("metadata", {}).items():
            if isinstance(value, (str, int, float, bool)):
                metadata[key] = value
            elif isinstance(value, list):
                metadata[key] = json.dumps(value)
        metadatas.append(metadata)
        documents.append(case.get("content", "")[:2000])

    # 写入Chroma
    collection.add(
        ids=ids,
        embeddings=embeddings,
        metadatas=metadatas,
        documents=documents
    )

    stats["successful"] += len(cases)


# ============================================================
# 主流程
# ============================================================

def main():
    """主函数"""
    import argparse

    parser = argparse.ArgumentParser(
        description="高性能ZIP包Markdown导入（内存流处理，并行向量化）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
性能优化：
  - 内存流处理ZIP，避免磁盘IO
  - 并行批量向量化
  - 流式写入Chroma

示例:
  %(prog)s --zip cases.zip
  %(prog)s --zip archive1.zip archive2.zip --collection docs
        """
    )
    parser.add_argument("--zip", required=True, nargs='+', help="ZIP文件路径")
    parser.add_argument("--collection", default="cases", help="Collection名称")
    parser.add_argument("--max-depth", type=int, default=10, help="ZIP递归深度")
    parser.add_argument("--batch-size", type=int, default=200, help="批量处理大小")
    parser.add_argument("--config", help="配置文件路径")

    args = parser.parse_args()

    # 读取配置
    if args.config:
        with open(args.config) as f:
            config = json.load(f)
    else:
        config_path = Path.home() / ".claude" / "skills" / "rag-case-retrieval" / "config.json"
        if config_path.exists():
            with open(config_path) as f:
                config = json.load(f)
        else:
            config = {
                "vectorization": {
                    "head_chars": 400,
                    "title_injection": True
                }
            }

    print("=" * 60)
    print("高性能ZIP包导入")
    print("=" * 60)
    print(f"批量大小: {args.batch_size}")
    print(f"模型: {get_embedding_config(config)['model']}")
    print("=" * 60)

    # 统计ZIP文件
    valid_zips = []
    for zip_path in args.zip:
        if Path(zip_path).exists():
            valid_zips.append(zip_path)
        else:
            print(f"⚠️ ZIP不存在: {zip_path}")

    if not valid_zips:
        print("❌ 无有效ZIP文件")
        return 1

    print(f"\n处理 {len(valid_zips)} 个ZIP文件...")

    # 创建案例迭代器（合并所有ZIP）
    def case_iterator():
        for zip_path in valid_zips:
            print(f"  扫描: {Path(zip_path).name}")
            for case in iterate_zip_markdown(zip_path, args.max_depth):
                yield case

    # 流式存储到Chroma（本地持久化模式）
    chroma_path = config.get("chroma", {}).get("path", None)

    print(f"\n导入到Chroma...")
    if chroma_path:
        print(f"  存储路径: {chroma_path}")
    else:
        print(f"  存储路径: ~/.local/share/chroma_rag (默认)")

    start_time = time.time()
    stats = store_to_chroma_stream(
        case_iterator(),
        config=config,
        collection_name=args.collection,
        chroma_path=chroma_path,
        batch_size=args.batch_size
    )
    elapsed = time.time() - start_time

    print("\n" + "=" * 60)
    print("导入完成:")
    print(f"  ✅ 成功: {stats['successful']}")
    print(f"  ❌ 失败: {stats['failed']}")
    print(f"  ⏱️  耗时: {elapsed:.2f}s")
    print(f"  📊 速率: {stats['successful']/elapsed:.1f} cases/s")
    print("=" * 60)

    return 0


if __name__ == "__main__":
    sys.exit(main())