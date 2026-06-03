#!/usr/bin/env python3
"""
Импорт УК РФ из Obsidian vault в Neo4j.
- Узлы: Статья, Глава, Раздел, Концепт
- Рёбра: BELONGS_TO (статья→глава, глава→раздел), REFERENCES (статья↔статья), RELATES_TO (концепт↔статья)
"""

import re, sys, json, glob, os
from pathlib import Path
from neo4j import GraphDatabase

NEO4J_URI = "bolt://localhost:7687"
NEO4J_USER = "neo4j"
NEO4J_PASS = "ukrf2026pass"

VAULT = Path("/home/clawd/uk-rf-vault")
ARTICLES_DIR = VAULT / "03-Resources" / "УК-РФ" / "Статьи"
MOC_DIR = VAULT / "06-MOC"
CONCEPTS_DIR = VAULT / "04-Concepts"
STRUCTURE_JSON = VAULT / "03-Resources" / "УК-РФ" / "structure.json"

# ─── Парсинг YAML frontmatter ───
def parse_frontmatter(text):
    """Простой парсер YAML frontmatter из .md текста."""
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
        # Простой key: value для однострочных значений
        m = re.match(r'^(\w[\w_]*)\s*:\s*"?([^"]*)"?\s*$', line)
        if m:
            fm[m.group(1)] = m.group(2).strip()
        else:
            # Списки: статьи: / связанные_концепты: (пропускаем — парсим отдельно)
            pass
    
    # Парсинг списков из frontmatter
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


# ─── Парсинг статей ───
def parse_articles():
    """Парсинг всех .md файлов статей."""
    articles = {}
    for f in sorted(ARTICLES_DIR.glob("ст.*.md")):
        name = f.stem  # ст.105
        text = f.read_text(encoding="utf-8")
        fm, body = parse_frontmatter(text)
        
        # Извлечение ссылок [[ст.XXX]]
        refs = re.findall(r'\[\[(ст\.[\d.]+)\]\]', body)
        
        articles[name] = {
            "filename": name,
            "номер": fm.get("номер", name.replace("ст.", "")),
            "заголовок": fm.get("заголовок", ""),
            "раздел": fm.get("раздел", ""),
            "глава": fm.get("глава", ""),
            "тип": fm.get("тип", ""),
            "статус": fm.get("статус", "действует"),
            "references": refs,
            "body_length": len(body)
        }
    return articles


# ─── Парсинг структуры из structure.json ───
def parse_structure():
    """Иерархия: Раздел → Глава → Статьи."""
    with open(STRUCTURE_JSON, "r", encoding="utf-8") as f:
        data = json.load(f)
    
    sections = {}  # раздел → {название, главы}
    chapters = {}  # глава → {название, раздел, статьи}
    
    # structure.json — dict with keys: sections, chapters, articles
    raw_sections = data.get("sections", [])
    raw_chapters = data.get("chapters", [])
    raw_articles = data.get("articles", [])
    
    # Определяем принадлежность глав к разделам через URL
    chapter_to_section = {}
    for ch in raw_chapters:
        ch_url = ch.get("url", "")
        # /uk/Razdel-I/Glava-2/ → раздел I
        m = re.search(r'Razdel-([IVXLCDM]+)/', ch_url)
        if m:
            chapter_to_section[ch.get("title", "")] = m.group(1)
    
    for idx, section in enumerate(raw_sections):
        sec_title = section.get("title", "")
        sec_num = str(idx + 1)  # Римские номера через URL
        # Извлечём римский номер из URL
        m = re.search(r'Razdel-([IVXLCDM]+)/', section.get("url", ""))
        if m:
            sec_num = m.group(1)
        sections[sec_num] = {"название": sec_title, "главы": []}
    
    for ch in raw_chapters:
        ch_title = ch.get("title", "")
        # Извлечём номер главы из title
        m = re.search(r'Глава\s+(\d+)', ch_title)
        ch_num = m.group(1) if m else str(ch.get("number", ""))
        
        # Определяем раздел
        sec_num = chapter_to_section.get(ch_title, "")
        if not sec_num:
            # Из URL главы извлекаем раздел
            m2 = re.search(r'Razdel-([IVXLCDM]+)/', ch.get("url", ""))
            sec_num = m2.group(1) if m2 else ""
        
        # Статьи главы — из raw_articles, у которых URL содержит URL главы
        ch_url = ch.get("url", "")
        arts = [a for a in raw_articles if ch_url and ch_url.rstrip("/") in a.get("url", "")]
        articles_list = [str(a.get("number", "")) for a in arts]
        
        if sec_num and sec_num in sections:
            sections[sec_num]["главы"].append(ch_num)
        
        chapters[ch_num] = {
            "название": ch_title,
            "раздел": sec_num,
            "статьи": articles_list
        }
    
    return sections, chapters


# ─── Парсинг концепт-нот ───
def parse_concepts():
    """Парсинг концепт-нот из 04-Concepts/."""
    concepts = {}
    for f in sorted(CONCEPTS_DIR.glob("*.md")):
        name = f.stem
        text = f.read_text(encoding="utf-8")
        fm, body = parse_frontmatter(text)
        
        # Ссылки на статьи [[ст.XXX]]
        article_refs = re.findall(r'\[\[(ст\.[\d.]+)\]\]', body)
        # Ссылки на другие концепты (не [[ст.]] — остальные [[...]])
        concept_refs = [m[0] for m in re.findall(r'\[\[([^\]]+)\]\]', body) 
                       if not m.startswith("ст.")]
        
        concepts[name] = {
            "понятие": fm.get("понятие", name),
            "статьи": fm.get("статьи", article_refs),
            "связанные_концепты": fm.get("связанные_концепты", concept_refs),
        }
    return concepts


# ─── Импорт в Neo4j ───
def import_to_neo4j(articles, sections, chapters, concepts):
    driver = GraphDatabase.driver(NEO4J_URI, auth=(NEO4J_USER, NEO4J_PASS))
    
    with driver.session() as session:
        # Очистка
        session.run("MATCH (n) DETACH DELETE n")
        print("✓ БД очищена")
        
        # 1. Создание узлов-разделов
        for sec_num, sec_data in sections.items():
            session.run("""
                MERGE (s:Раздел {номер: $num})
                SET s.название = $title
            """, num=sec_num, title=sec_data["название"])
        print(f"✓ Разделы: {len(sections)}")
        
        # 2. Создание узлов-глав
        for ch_num, ch_data in chapters.items():
            session.run("""
                MERGE (g:Глава {номер: $num})
                SET g.название = $title
            """, num=ch_num, title=ch_data["название"])
            
            # Связь Глава → Раздел
            session.run("""
                MATCH (g:Глава {номер: $ch})
                MATCH (s:Раздел {номер: $sec})
                MERGE (g)-[:BELONGS_TO]->(s)
            """, ch=ch_num, sec=ch_data["раздел"])
        print(f"✓ Главы: {len(chapters)}")
        
        # 3. Создание узлов-статей
        articles_created = 0
        for art_name, art_data in articles.items():
            session.run("""
                MERGE (a:Статья {номер: $num})
                SET a.заголовок = $title,
                    a.раздел = $sec,
                    a.глава = $ch,
                    a.статус = $status
            """, 
                num=art_data["номер"],
                title=art_data["заголовок"],
                sec=art_data["раздел"],
                ch=art_data["глава"],
                status=art_data["статус"]
            )
            articles_created += 1
        
        # Связь Статья → Глава
        for art_name, art_data in articles.items():
            if art_data["глава"]:
                session.run("""
                    MATCH (a:Статья {номер: $art})
                    MATCH (g:Глава {номер: $ch})
                    MERGE (a)-[:BELONGS_TO]->(g)
                """, art=art_data["номер"], ch=art_data["глава"])
        
        print(f"✓ Статьи: {articles_created}")
        
        # 4. Перекрёстные ссылки (Статья → Статья)
        refs_created = 0
        for art_name, art_data in articles.items():
            for ref in art_data.get("references", []):
                ref_num = ref.replace("ст.", "")
                session.run("""
                    MATCH (a:Статья {номер: $from_num})
                    MATCH (b:Статья {номер: $to_num})
                    MERGE (a)-[:REFERENCES]->(b)
                """, from_num=art_data["номер"], to_num=ref_num)
                refs_created += 1
        print(f"✓ Перекрёстные ссылки: {refs_created}")
        
        # 5. Концепт-ноты
        concepts_created = 0
        concept_rels = 0
        for c_name, c_data in concepts.items():
            session.run("""
                MERGE (c:Концепт {название: $name})
            """, name=c_data["понятие"])
            concepts_created += 1
            
            # Концепт → Статья
            for art_ref in c_data.get("статьи", []):
                art_num = art_ref.replace("ст.", "").strip()
                result = session.run("""
                    MATCH (c:Концепт {название: $name})
                    MATCH (a:Статья {номер: $num})
                    MERGE (c)-[:RELATES_TO]->(a)
                    RETURN count(*) as cnt
                """, name=c_data["понятие"], num=art_num)
                rec = result.single()
                if rec:
                    concept_rels += rec["cnt"]
            
            # Концепт → Концепт
            for related in c_data.get("связанные_концепты", []):
                session.run("""
                    MATCH (c1:Концепт {название: $name})
                    MERGE (c2:Концепт {название: $related})
                    MERGE (c1)-[:CONNECTS]->(c2)
                """, name=c_data["понятие"], related=related)
        
        print(f"✓ Концепты: {concepts_created}, связей со статьями: {concept_rels}")
    
    driver.close()
    return articles_created, refs_created, concepts_created


def verify_graph():
    """Проверка графа через Cypher-запросы."""
    driver = GraphDatabase.driver(NEO4J_URI, auth=(NEO4J_USER, NEO4J_PASS))
    
    with driver.session() as session:
        stats = {}
        for label in ["Статья", "Глава", "Раздел", "Концепт"]:
            result = session.run(f"MATCH (n:{label}) RETURN count(n) as cnt")
            stats[label] = result.single()["cnt"]
        
        result = session.run("MATCH ()-[r:REFERENCES]->() RETURN count(r) as cnt")
        stats["REFERENCES"] = result.single()["cnt"]
        
        result = session.run("MATCH ()-[r:BELONGS_TO]->() RETURN count(r) as cnt")
        stats["BELONGS_TO"] = result.single()["cnt"]
        
        result = session.run("MATCH ()-[r:RELATES_TO]->() RETURN count(r) as cnt")
        stats["RELATES_TO"] = result.single()["cnt"]
        
        # Тест: многошаговый обход от ст.105
        result = session.run("""
            MATCH (a:Статья {номер: '105'})-[:REFERENCES*1..3]-(b:Статья)
            RETURN DISTINCT b.номер as num, b.заголовок as title
            LIMIT 10
        """)
        multi_hop = [(r["num"], r["title"]) for r in result]
        
        # Тест: концепт → статьи
        result = session.run("""
            MATCH (c:Концепт {название: 'Умысел'})-[:RELATES_TO]->(a:Статья)
            RETURN a.номер as num, a.заголовок as title
        """)
        concept_articles = [(r["num"], r["title"]) for r in result]
    
    driver.close()
    return stats, multi_hop, concept_articles


if __name__ == "__main__":
    print("═══ Парсинг vault... ═══")
    articles = parse_articles()
    print(f"Статей: {len(articles)}")
    
    sections, chapters = parse_structure()
    print(f"Разделов: {len(sections)}, Глав: {len(chapters)}")
    
    concepts = parse_concepts()
    print(f"Концептов: {len(concepts)}")
    
    print("\n═══ Импорт в Neo4j... ═══")
    a, r, c = import_to_neo4j(articles, sections, chapters, concepts)
    
    print("\n═══ Верификация графа ═══")
    stats, multi_hop, concept_articles = verify_graph()
    
    print(f"\n📊 Статистика графа:")
    for k, v in stats.items():
        print(f"  {k}: {v}")
    
    print(f"\n🔍 Многошаговый обход от ст.105 (1-3 шага):")
    for num, title in multi_hop:
        print(f"  ст.{num} — {title}")
    
    print(f"\n💡 Концепт «Умысел» → статьи:")
    for num, title in concept_articles:
        print(f"  ст.{num} — {title}")