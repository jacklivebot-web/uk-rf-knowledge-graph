#!/usr/bin/env python3
"""
LegalGraphRAG — LLM-слой с принудительным цитированием.
Получает вопрос → retrieves контекст из API → LLM отвечает с [[ст.XXX]] ссылками.
"""

import os, json, requests
from flask import Flask, request, jsonify, Response
from flask_cors import CORS

# ─── Конфиг ───
API_URL = os.getenv("API_URL", "http://localhost:5000/api/v1")
LLM_PROVIDER = os.getenv("LLM_PROVIDER", "openai")  # openai | openrouter | local
LLM_API_KEY = os.getenv("LLM_API_KEY", "")
LLM_MODEL = os.getenv("LLM_MODEL", "gpt-4o-mini")
LLM_BASE_URL = os.getenv("LLM_BASE_URL", "https://api.openai.com/v1")
HOST = os.getenv("API_HOST", "0.0.0.0")
PORT = int(os.getenv("API_PORT", "5001"))

SYSTEM_PROMPT = """Ты — юридический ассистент по Уголовному кодексу РФ. 

ЖЁСТКИЕ ПРАВИЛА:
1. КАЖДОЕ утверждение о нормах права ДОЛЖНО сопровождаться ссылкой на статью в формате [[ст.XXX]]
2. Если ты не можешь подтвердить факт конкретной статьёй УК РФ — скажи «Не могу подтвердить источником»
3. НЕ придумывай номера статей. Используй ТОЛЬКО статьи из предоставленного контекста
4. Если вопрос выходить за рамки УК РФ — честно скажи об этом
5. Объясняй сложные юридические формулировки простым языком
6. Указывай статус статьи (действует/утратила силу), если известен

Формат ответа:
- Основной ответ с ссылками [[ст.XXX]]
- Раздел «Источники» со списком всех упомянутых статей
- При необходимости — «Ограничения ответа»"""


app = Flask(__name__)
CORS(app)


def api_get(endpoint):
    """Запрос к внутреннему API."""
    try:
        r = requests.get(f"{API_URL}{endpoint}", timeout=10)
        return r.json() if r.status_code == 200 else None
    except:
        return None


def api_post(endpoint, data):
    """POST-запрос к внутреннему API."""
    try:
        r = requests.post(f"{API_URL}{endpoint}", json=data, timeout=15)
        return r.json() if r.status_code == 200 else None
    except:
        return None


def retrieve_context(query):
    """Сбор контекста из всех слоёв GraphRAG."""
    context = {"articles": [], "concepts": [], "graph_paths": []}
    
    # 1. Семантический поиск
    search = api_post("/search", {
        "query": query,
        "limit": 7,
        "include_text": True,
        "graph_depth": 2,
        "search_type": "all"
    })
    
    if search:
        context["articles"] = search.get("articles", [])
        context["concepts"] = search.get("concepts", [])
        context["graph_paths"] = search.get("graph_paths", [])
    
    # 2. Для топ-статей — подробная информация (ссылки, концепты)
    for art in context["articles"][:3]:
        num = art.get("номер", "")
        if num:
            detail = api_get(f"/article/{num}")
            if detail:
                art["ссылки_на"] = detail.get("ссылки_на", [])
                art["ссылаются_на"] = detail.get("ссылаются_на", [])
                art["концепты"] = detail.get("концепты", [])
                # Добавляем текст статьи если его нет
                if "текст" not in art and "текст" in detail:
                    art["текст"] = detail["текст"]
    
    return context


def build_prompt(query, context):
    """Формирование промпта с контекстом для LLM."""
    parts = [f"Вопрос: {query}\n\n=== КОНТЕКСТ ИЗ БАЗЫ ЗНАНИЙ УК РФ ===\n"]
    
    # Статьи
    if context["articles"]:
        parts.append("## Найденные статьи:")
        for art in context["articles"]:
            num = art.get("номер", "?")
            title = art.get("заголовок", "")
            relevance = art.get("relevance", 0)
            parts.append(f"\n### [[ст.{num}]] {title} (релевантность: {relevance:.2f})")
            
            if art.get("текст"):
                text = art["текст"][:1500]  # Ограничение длины
                parts.append(text)
            
            if art.get("ссылки_на"):
                refs = ", ".join(f"[[ст.{r['номер']}]]" for r in art["ссылки_на"][:5])
                parts.append(f"\nСсылается на: {refs}")
            
            if art.get("ссылаются_на"):
                refs = ", ".join(f"[[ст.{r['номер']}]]" for r in art["ссылаются_на"][:5])
                parts.append(f"Ссылаются на эту: {refs}")
            
            if art.get("концепты"):
                parts.append(f"Связанные концепты: {', '.join(art['концепты'])}")
    
    # Концепты
    if context["concepts"]:
        parts.append("\n## Связанные концепты:")
        for c in context["concepts"]:
            parts.append(f"- {c['понятие']} (релевантность: {c.get('relevance', 0):.2f})")
    
    # Графовые пути
    if context["graph_paths"]:
        parts.append("\n## Связи в графе:")
        for path in context["graph_paths"]:
            from_art = path.get("from", "?")
            conns = path.get("connections", [])
            if conns:
                conn_str = ", ".join(f"[[ст.{c['номер']}]]" for c in conns[:5])
                parts.append(f"От [[ст.{from_art}]]: {conn_str}")
    
    parts.append("\n=== КОНЕЦ КОНТЕКСТА ===")
    parts.append(f"\nОтветь на вопрос, используя ТОЛЬКО предоставленный контекст. "
                 f"Обязательно цитируй статьи как [[ст.XXX]]. "
                 f"Если в контексте нет ответа — так и скажи.")
    
    return "\n".join(parts)


def call_llm(system_prompt, user_message):
    """Вызов LLM через API."""
    try:
        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {LLM_API_KEY}"
        }
        payload = {
            "model": LLM_MODEL,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_message}
            ],
            "temperature": 0.1,  # Низкая температура для точности
            "max_tokens": 2000
        }
        
        r = requests.post(
            f"{LLM_BASE_URL}/chat/completions",
            headers=headers,
            json=payload,
            timeout=30
        )
        
        if r.status_code == 200:
            data = r.json()
            return data["choices"][0]["message"]["content"]
        else:
            return f"Ошибка LLM ({r.status_code}): {r.text[:200]}"
    except Exception as e:
        return f"Ошибка вызова LLM: {str(e)}"


def extract_citations(text):
    """Извлечение всех [[ст.XXX]] из ответа."""
    import re
    return list(set(re.findall(r'\[\[ст\.[\d.]+\]\]', text)))


def format_sources(citations, context):
    """Форматирование списка источников с деталями."""
    sources = []
    seen = set()
    for cit in citations:
        num = cit.replace("[[ст.", "").replace("]]", "")
        if num in seen:
            continue
        seen.add(num)
        
        # Ищем в контексте
        for art in context.get("articles", []):
            if art.get("номер") == num:
                sources.append({
                    "статья": num,
                    "заголовок": art.get("заголовок", ""),
                    "статус": art.get("статус", "действует"),
                    "релевантность": art.get("relevance", 0)
                })
                break
        else:
            # Не в контексте — запросим отдельно
            sources.append({"статья": num, "заголовок": "", "статус": "", "релевантность": 0})
    
    return sources


# ─── Эндпоинты ───

@app.route("/api/v2/ask", methods=["POST"])
def ask():
    """
    Главный эндпоинт: вопрос → ответ с цитированием.
    
    Body: {
        "query": "что грозит за кражу?",
        "provider": "openai",       // опционально
        "model": "gpt-4o-mini",    // опционально
        "stream": false             // опционально
    }
    """
    data = request.get_json() or {}
    query = data.get("query", "").strip()
    
    if not query:
        return jsonify({"error": "query обязателен"}), 400
    
    # Получаем контекст из GraphRAG
    context = retrieve_context(query)
    
    if not context["articles"] and not context["concepts"]:
        return jsonify({
            "answer": "Не удалось найти релевантные статьи в базе знаний. Попробуйте переформулировать вопрос.",
            "sources": [],
            "query": query
        })
    
    # Формируем промпт
    prompt = build_prompt(query, context)
    
    # Вызываем LLM
    provider = data.get("provider", LLM_PROVIDER)
    model = data.get("model", LLM_MODEL)
    
    llm_answer = call_llm(SYSTEM_PROMPT, prompt)
    
    # Извлекаем цитирования
    citations = extract_citations(llm_answer)
    sources = format_sources(citations, context)
    
    return jsonify({
        "answer": llm_answer,
        "sources": sources,
        "citations": citations,
        "context_articles": len(context["articles"]),
        "context_concepts": len(context["concepts"]),
        "query": query,
        "model": model,
        "provider": provider
    })


@app.route("/api/v2/context", methods=["POST"])
def get_context():
    """Только контекст без LLM — для отладки."""
    data = request.get_json() or {}
    query = data.get("query", "").strip()
    
    if not query:
        return jsonify({"error": "query обязателен"}), 400
    
    context = retrieve_context(query)
    prompt = build_prompt(query, context)
    
    return jsonify({
        "context": context,
        "prompt_length": len(prompt),
        "query": query
    })


@app.route("/api/v2/health", methods=["GET"])
def health():
    """Проверка доступности всех компонентов."""
    api_health = api_get("/health")
    llm_status = "unknown"
    
    if LLM_API_KEY:
        llm_status = "configured"
    
    return jsonify({
        "status": "ok" if api_health else "partial",
        "graph_api": api_health,
        "llm": {
            "provider": LLM_PROVIDER,
            "model": LLM_MODEL,
            "status": llm_status
        }
    })


if __name__ == "__main__":
    print(f"LegalGraphRAG v2 — LLM layer")
    print(f"  Graph API: {API_URL}")
    print(f"  LLM: {LLM_PROVIDER}/{LLM_MODEL}")
    app.run(host=HOST, port=PORT, debug=False)