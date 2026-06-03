#!/usr/bin/env python3
"""
LegalGraphRAG API — обёртка над Neo4j + ChromaDB.
Гибридный поиск: вектор (семантика) + граф (связи) + текст (Obsidian).
"""

import os, re, json
from flask import Flask, request, jsonify
from flask_cors import CORS
from neo4j import GraphDatabase
import chromadb
from chromadb.utils import embedding_functions

# ─── Конфиг ───
NEO4J_URI = os.getenv("NEO4J_URI", "bolt://localhost:7687")
NEO4J_USER = os.getenv("NEO4J_USER", "neo4j")
NEO4J_PASS = os.getenv("NEO4J_PASS", "ukrf2026pass")
CHROMA_DIR = os.getenv("CHROMA_DIR", "/home/clawd/uk-rf-vault/.chroma")
VAULT_DIR = os.getenv("VAULT_DIR", "/home/clawd/uk-rf-vault")
EMBED_MODEL = os.getenv("EMBED_MODEL", "intfloat/multilingual-e5-small")
HOST = os.getenv("API_HOST", "0.0.0.0")
PORT = int(os.getenv("API_PORT", "5000"))

# ─── Инициализация ───
app = Flask(__name__)
CORS(app)

print("Загрузка ChromaDB...")
embed_fn = embedding_functions.SentenceTransformerEmbeddingFunction(model_name=EMBED_MODEL)
chroma_client = chromadb.PersistentClient(path=CHROMA_DIR)
col_articles = chroma_client.get_or_create_collection(
    name="uk_rf_articles", embedding_function=embed_fn, metadata={"hnsw:space": "cosine"}
)
col_concepts = chroma_client.get_or_create_collection(
    name="uk_rf_concepts", embedding_function=embed_fn, metadata={"hnsw:space": "cosine"}
)

print("Подключение к Neo4j...")
neo4j_driver = GraphDatabase.driver(NEO4J_URI, auth=(NEO4J_USER, NEO4J_PASS))

print(f"✅ API готово: {col_articles.count()} статей, {col_concepts.count()} концептов")


# ─── Утилиты ───
def read_article_md(article_num):
    """Чтение .md файла статьи из vault."""
    for name in [f"ст.{article_num}", f"ст.{article_num.replace('.', '')}"]:
        path = os.path.join(VAULT_DIR, "03-Resources", "УК-РФ", "Статьи", f"{name}.md")
        if os.path.exists(path):
            with open(path, "r", encoding="utf-8") as f:
                return f.read()
    return None


def strip_frontmatter(text):
    """Убрать YAML frontmatter из .md текста."""
    if text.startswith("---"):
        end = text.find("---", 3)
        if end != -1:
            return text[end+3:].strip()
    return text


# ─── Эндпоинты ───

@app.route("/api/v1/health", methods=["GET"])
def health():
    """Проверка доступности API."""
    try:
        with neo4j_driver.session() as session:
            stats = {}
            for label in ["Статья", "Глава", "Раздел", "Концепт"]:
                r = session.run(f"MATCH (n:{label}) RETURN count(n) as c")
                stats[label] = r.single()["c"]
        return jsonify({
            "status": "ok",
            "neo4j": stats,
            "chroma_articles": col_articles.count(),
            "chroma_concepts": col_concepts.count()
        })
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500


@app.route("/api/v1/search", methods=["POST"])
def hybrid_search():
    """
    Гибридный поиск: вектор + граф + текст.
    
    Body: {
        "query": "тайное хищение чужого имущества",
        "limit": 10,
        "include_text": false,
        "graph_depth": 0,
        "filter": {"раздел": "III"},
        "search_type": "all" | "articles" | "concepts"
    }
    """
    data = request.get_json() or {}
    query = data.get("query", "")
    limit = min(data.get("limit", 10), 50)
    include_text = data.get("include_text", False)
    graph_depth = data.get("graph_depth", 0)
    where_filter = data.get("filter")
    search_type = data.get("search_type", "all")
    
    if not query:
        return jsonify({"error": "query обязателен"}), 400
    
    results = {"query": query, "articles": [], "concepts": [], "graph_paths": []}
    
    # 1. Векторный поиск по статьям
    if search_type in ("all", "articles"):
        try:
            chroma_results = col_articles.query(
                query_texts=[query],
                n_results=limit,
                where=where_filter,
                include=["documents", "metadatas", "distances"]
            )
            if chroma_results and chroma_results["documents"]:
                for doc, meta, dist in zip(
                    chroma_results["documents"][0],
                    chroma_results["metadatas"][0],
                    chroma_results["distances"][0]
                ):
                    art = {"номер": meta.get("номер", ""), "заголовок": meta.get("заголовок", "")}
                    art["relevance"] = round(1 - dist, 3)
                    if include_text:
                        md = read_article_md(meta.get("номер", ""))
                        if md:
                            art["текст"] = strip_frontmatter(md)
                    results["articles"].append(art)
        except Exception as e:
            results["articles_error"] = str(e)
    
    # 2. Векторный поиск по концептам
    if search_type in ("all", "concepts"):
        try:
            concept_results = col_concepts.query(
                query_texts=[query],
                n_results=min(limit, 5),
                include=["documents", "metadatas", "distances"]
            )
            if concept_results and concept_results["documents"]:
                for doc, meta, dist in zip(
                    concept_results["documents"][0],
                    concept_results["metadatas"][0],
                    concept_results["distances"][0]
                ):
                    results["concepts"].append({
                        "понятие": meta.get("понятие", ""),
                        "relevance": round(1 - dist, 3)
                    })
        except Exception as e:
            results["concepts_error"] = str(e)
    
    # 3. Графовый обход
    if graph_depth > 0 and results["articles"]:
        with neo4j_driver.session() as session:
            for art in results["articles"][:3]:
                art_num = art["номер"]
                try:
                    cypher = f"""
                        MATCH (a:Статья {{номер: $num}})-[:REFERENCES*1..{graph_depth}]-(b:Статья)
                        RETURN DISTINCT b.номер as num, b.заголовок as title,
                               length(shortestPath((a)-[*]-(b))) as dist
                        ORDER BY dist
                        LIMIT 10
                    """
                    rows = session.run(cypher, num=art_num)
                    path = {
                        "from": art_num,
                        "connections": [
                            {"номер": r["num"], "заголовок": r["title"], "шагов": r["dist"]}
                            for r in rows
                        ]
                    }
                    results["graph_paths"].append(path)
                except Exception as e:
                    results["graph_paths"].append({"from": art_num, "error": str(e)})
    
    return jsonify(results)


@app.route("/api/v1/article/<article_num>", methods=["GET"])
def get_article(article_num):
    """Получить статью по номеру (текст + метаданные + связи)."""
    with neo4j_driver.session() as session:
        result = session.run("""
            MATCH (a:Статья {номер: $num})
            RETURN a.номер as num, a.заголовок as title, a.раздел as sec,
                   a.глава as ch, a.статус as status
        """, num=article_num)
        record = result.single()
        if not record:
            return jsonify({"error": f"Статья {article_num} не найдена"}), 404
        
        article = {
            "номер": record["num"],
            "заголовок": record["title"],
            "раздел": record["sec"],
            "глава": record["ch"],
            "статус": record["status"]
        }
        
        # Ссылки ОТ этой статьи
        refs_out = session.run("""
            MATCH (a:Статья {номер: $num})-[:REFERENCES]->(b:Статья)
            RETURN b.номер as num, b.заголовок as title
        """, num=article_num)
        article["ссылки_на"] = [{"номер": r["num"], "заголовок": r["title"]} for r in refs_out]
        
        # Ссылки НА эту статью
        refs_in = session.run("""
            MATCH (b:Статья)-[:REFERENCES]->(a:Статья {номер: $num})
            RETURN b.номер as num, b.заголовок as title
        """, num=article_num)
        article["ссылаются_на"] = [{"номер": r["num"], "заголовок": r["title"]} for r in refs_in]
        
        # Связанные концепты
        concepts = session.run("""
            MATCH (c:Концепт)-[:RELATES_TO]->(a:Статья {номер: $num})
            RETURN c.название as name
        """, num=article_num)
        article["концепты"] = [r["name"] for r in concepts]
        
        # Текст из vault
        md = read_article_md(article_num)
        if md:
            article["текст"] = strip_frontmatter(md)
        
        return jsonify(article)


@app.route("/api/v1/graph/path", methods=["GET"])
def graph_path():
    """
    Кратчайший путь между двумя статьями.
    ?from=158&to=105&max_depth=5
    """
    from_num = request.args.get("from", "")
    to_num = request.args.get("to", "")
    max_depth = int(request.args.get("max_depth", "5"))
    
    if not from_num or not to_num:
        return jsonify({"error": "Параметры from и to обязательны"}), 400
    
    with neo4j_driver.session() as session:
        try:
            # depth нельзя параметризовать — подставляем безопасно (int)
            safe_depth = max(1, min(max_depth, 10))
            result = session.run(
                f"MATCH path = shortestPath((a:Статья {{номер: $from_num}})-[*..{safe_depth}]-(b:Статья {{номер: $to_num}})) "
                "RETURN [n in nodes(path) | {"
                "тип: labels(n)[0], "
                "номер: coalesce(n.номер, n.название), "
                "заголовок: coalesce(n.заголовок, '')"
                "}] as nodes, "
                "[r in relationships(path) | type(r)] as rels "
                "LIMIT 3",
                from_num=from_num, to_num=to_num
            )
            
            paths = []
            for record in result:
                paths.append({"узлы": record["nodes"], "связи": record["rels"]})
            
            return jsonify({"from": from_num, "to": to_num, "пути": paths})
        except Exception as e:
            return jsonify({"error": str(e)}), 500


@app.route("/api/v1/graph/hubs", methods=["GET"])
def graph_hubs():
    """Топ-статей по числу входящих ссылок (центры графа)."""
    limit = min(int(request.args.get("limit", "10")), 50)
    
    with neo4j_driver.session() as session:
        result = session.run("""
            MATCH (a:Статья)<-[:REFERENCES]-(b:Статья)
            WITH a, count(b) as refs
            ORDER BY refs DESC
            LIMIT $lim
            RETURN a.номер as num, a.заголовок as title, refs
        """, lim=limit)
        
        hubs = [{"номер": r["num"], "заголовок": r["title"], "входящих_ссылок": r["refs"]} for r in result]
        return jsonify(hubs)


@app.route("/api/v1/concept/<name>", methods=["GET"])
def get_concept(name):
    """Получить концепт-ноту и ссылающиеся статьи. name URL-encoded."""
    from urllib.parse import unquote
    decoded_name = unquote(name)
    
    with neo4j_driver.session() as session:
        result = session.run("""
            MATCH (c:Концепт {название: $name})
            OPTIONAL MATCH (c)-[:RELATES_TO]->(a:Статья)
            OPTIONAL MATCH (c)-[:CONNECTS]->(c2:Концепт)
            RETURN c.название as name,
                   collect(DISTINCT {номер: a.номер, заголовок: a.заголовок}) as articles,
                   collect(DISTINCT c2.название) as related
        """, name=decoded_name)
        record = result.single()
        if not record:
            return jsonify({"error": f"Концепт '{name}' не найден"}), 404
        
        articles = [a for a in record["articles"] if a.get("номер")]
        related = [r for r in record["related"] if r]
        
        return jsonify({
            "понятие": record["name"],
            "статьи": articles,
            "связанные_концепты": related
        })


@app.route("/api/v1/stats", methods=["GET"])
def stats():
    """Общая статистика базы знаний."""
    with neo4j_driver.session() as session:
        graph_stats = {}
        for label in ["Статья", "Глава", "Раздел", "Концепт"]:
            r = session.run(f"MATCH (n:{label}) RETURN count(n) as c")
            graph_stats[label] = r.single()["c"]
        
        for rel in ["REFERENCES", "BELONGS_TO", "RELATES_TO", "CONNECTS"]:
            r = session.run(f"MATCH ()-[r:{rel}]->() RETURN count(r) as c")
            graph_stats[rel] = r.single()["c"]
    
    return jsonify({
        "neo4j": graph_stats,
        "chroma_articles": col_articles.count(),
        "chroma_concepts": col_concepts.count(),
        "vault_path": VAULT_DIR
    })


if __name__ == "__main__":
    app.run(host=HOST, port=PORT, debug=False)