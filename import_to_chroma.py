#!/usr/bin/env python3
"""
Векторный импорт УК РФ в ChromaDB с гибридным поиском.
- Collection: uk_rf_articles
- Embeddings: sentence-transformers (multilingual-e5-small — бесплатная, локальная)
- Метаданные: номер, заголовок, раздел, глава, статус
- Гибридный поиск: вектор + BM25 (where-фильтры)
"""

import re, json, glob, os
from pathlib import Path
import chromadb
from chromadb.utils import embedding_functions

VAULT = Path("/home/clawd/uk-rf-vault")
ARTICLES_DIR = VAULT / "03-Resources" / "УК-РФ" / "Статьи"
CONCEPTS_DIR = VAULT / "04-Concepts"
CHROMA_DIR = VAULT / ".chroma"

# ─── Парсинг YAML frontmatter ───
def parse_frontmatter(text):
    fm = {}
    if not text.startswith("---"):
        return fm, text
    end = text.find("---", 3)
    if end == -1:
        return fm, text
    yaml_block = text[3:end].strip()
    body = text[end+3:].strip()
    
    for line in yaml_block.split("\n"):
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        m = re.match(r'^(\w[\w_]*)\s*:\s*"?([^"]*)"?\s*$', line)
        if m:
            fm[m.group(1)] = m.group(2).strip()
    
    # Парсинг списков
    list_fields = {}
    current_key = None
    for line in yaml_block.split("\n"):
        stripped = line.strip()
        if re.match(r'^[\w_]+\s*:\s*$', stripped):
            current_key = stripped.rstrip(":").strip()
            list_fields[current_key] = []
        elif stripped.startswith("- ") and current_key:
            val = stripped[2:].strip().strip('"').strip("'")
            list_fields[current_key].append(val)
        else:
            current_key = None
    fm.update(list_fields)
    return fm, body


def load_articles():
    """Парсинг статей из .md файлов."""
    docs, metas, ids = [], [], []
    for f in sorted(ARTICLES_DIR.glob("ст.*.md")):
        name = f.stem
        text = f.read_text(encoding="utf-8")
        fm, body = parse_frontmatter(text)
        
        # Документ = заголовок + тело (без frontmatter)
        title = fm.get("заголовок", "")
        full_text = f"ст.{fm.get('номер', '')}. {title}\n\n{body}"
        
        doc_id = f"art_{fm.get('номер', name.replace('ст.', ''))}"
        
        meta = {
            "тип": "статья",
            "номер": fm.get("номер", ""),
            "заголовок": title[:200],  # Chroma limit
            "раздел": fm.get("раздел", ""),
            "глава": fm.get("глава", ""),
            "статус": fm.get("статус", "действует"),
        }
        
        docs.append(full_text)
        metas.append(meta)
        ids.append(doc_id)
    
    return ids, docs, metas


def load_concepts():
    """Парсинг концепт-нот."""
    docs, metas, ids = [], [], []
    for f in sorted(CONCEPTS_DIR.glob("*.md")):
        name = f.stem
        text = f.read_text(encoding="utf-8")
        fm, body = parse_frontmatter(text)
        
        concept_name = fm.get("понятие", name.replace("-", " "))
        full_text = f"Концепт: {concept_name}\n\n{body}"
        
        doc_id = f"concept_{name}"
        
        # Статьи как строка для фильтрации
        articles_list = fm.get("статьи", [])
        
        meta = {
            "тип": "концепт",
            "понятие": concept_name[:200],
            "статьи": ", ".join(articles_list) if isinstance(articles_list, list) else str(articles_list),
        }
        
        docs.append(full_text)
        metas.append(meta)
        ids.append(doc_id)
    
    return ids, docs, metas


def setup_chroma():
    """Создание и заполнение ChromaDB."""
    # Локальная модель эмбеддингов (бесплатная!)
    print("Загрузка модели эмбеддингов...")
    embed_fn = embedding_functions.SentenceTransformerEmbeddingFunction(
        model_name="intfloat/multilingual-e5-small"
    )
    
    # Persistent Chroma
    client = chromadb.PersistentClient(path=str(CHROMA_DIR))
    
    # Коллекция статей
    print("Создание коллекции uk_rf_articles...")
    col_articles = client.get_or_create_collection(
        name="uk_rf_articles",
        embedding_function=embed_fn,
        metadata={"hnsw:space": "cosine"}
    )
    
    # Загрузка и импорт статей
    art_ids, art_docs, art_metas = load_articles()
    print(f"Импорт {len(art_docs)} статей...")
    
    # Batch insert (Chroma limit: 5461 per batch)
    batch_size = 500
    for i in range(0, len(art_ids), batch_size):
        batch_ids = art_ids[i:i+batch_size]
        batch_docs = art_docs[i:i+batch_size]
        batch_metas = art_metas[i:i+batch_size]
        col_articles.upsert(
            ids=batch_ids,
            documents=batch_docs,
            metadatas=batch_metas
        )
        print(f"  → {i+len(batch_ids)}/{len(art_ids)}")
    
    # Коллекция концептов
    print("Создание коллекции uk_rf_concepts...")
    col_concepts = client.get_or_create_collection(
        name="uk_rf_concepts",
        embedding_function=embed_fn,
        metadata={"hnsw:space": "cosine"}
    )
    
    con_ids, con_docs, con_metas = load_concepts()
    if con_docs:
        print(f"Импорт {len(con_docs)} концептов...")
        col_concepts.upsert(
            ids=con_ids,
            documents=con_docs,
            metadatas=con_metas
        )
    
    return client, col_articles, col_concepts


def hybrid_search(collection, query, n_results=5, where=None):
    """Гибридный поиск: векторный + метаданные-фильтр."""
    results = collection.query(
        query_texts=[query],
        n_results=n_results,
        where=where,
        include=["documents", "metadatas", "distances"]
    )
    return results


def verify_chroma(client, col_articles, col_concepts):
    """Проверка гибридного поиска."""
    print("\n═══ Верификация векторного поиска ═══")
    
    # 1. Семантический поиск: убийство
    results = hybrid_search(col_articles, "убийство при превышении пределов необходимой обороны", n_results=5)
    print("\n🔍 «убийство при превышении пределов необходимой обороны»:")
    for i, (doc, meta, dist) in enumerate(zip(results["documents"][0], results["metadatas"][0], results["distances"][0])):
        print(f"  {i+1}. ст.{meta['номер']} — {meta['заголовок'][:60]} (dist={dist:.3f})")
    
    # 2. Семантический поиск: хищение
    results = hybrid_search(col_articles, "тайное хищение чужого имущества", n_results=5)
    print("\n🔍 «тайное хищение чужого имущества»:")
    for i, (doc, meta, dist) in enumerate(zip(results["documents"][0], results["metadatas"][0], results["distances"][0])):
        print(f"  {i+1}. ст.{meta['номер']} — {meta['заголовок'][:60]} (dist={dist:.3f})")
    
    # 3. Гибридный: вектор + фильтр по разделу
    results = hybrid_search(col_articles, "наказание за преступление", n_results=5,
                           where={"раздел": "III"})
    print("\n🔍 «наказание за преступление» (фильтр: Раздел III):")
    for i, (doc, meta, dist) in enumerate(zip(results["documents"][0], results["metadatas"][0], results["distances"][0])):
        print(f"  {i+1}. ст.{meta['номер']} — {meta['заголовок'][:60]} (раздел={meta['раздел']}, dist={dist:.3f})")
    
    # 4. Концепт-поиск
    results = hybrid_search(col_concepts, "форма вины при совершении преступления", n_results=3)
    print("\n🔍 «форма вины при совершении преступления» (концепты):")
    for i, (doc, meta, dist) in enumerate(zip(results["documents"][0], results["metadatas"][0], results["distances"][0])):
        print(f"  {i+1}. {meta['понятие']} (dist={dist:.3f})")
    
    # 5. Статистика
    art_count = col_articles.count()
    con_count = col_concepts.count()
    print(f"\n📊 Статистика ChromaDB:")
    print(f"  Статей: {art_count}")
    print(f"  Концептов: {con_count}")
    
    # Размер векторов
    peek = col_articles.peek(limit=1)
    if peek and peek.get("embeddings"):
        dim = len(peek["embeddings"][0])
        print(f"  Размерность эмбеддингов: {dim}")


if __name__ == "__main__":
    print("═══ Настройка ChromaDB для УК РФ ═══\n")
    client, col_articles, col_concepts = setup_chroma()
    verify_chroma(client, col_articles, col_concepts)
    print("\n✅ ChromaDB полностью настроена!")