# -*- coding: utf-8 -*-
"""
Kaz Legal Bot – Flask backend
Оптимизированный поиск законов + HTML-ответы + fallback на Adilet
"""

import os
import re
import json
import time
import logging
from typing import List, Dict, Any, Iterable, Tuple
from difflib import get_close_matches

import requests
from bs4 import BeautifulSoup

from flask import Flask, request, Response, jsonify, stream_with_context
from flask_cors import CORS

# Локальные утилиты
from helpers import (
    normalize_text,
    tokenize,
    expand_keywords,
    build_law_index,
)

# ----------------------- Логирование -----------------------
logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO"),
    format="%(asctime)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger("kaz-legal-bot")

# ----------------------- Flask & CORS ----------------------
app = Flask(__name__)

def _parse_origins(raw: str) -> List[str]:
    if not raw:
        return ["*"]
    parts = [p.strip() for p in raw.split(",") if p.strip()]
    return parts or ["*"]

CORS_ORIGINS = _parse_origins(os.getenv("CORS_ORIGINS", ""))
CORS(app, resources={r"/*": {"origins": CORS_ORIGINS}}, supports_credentials=True)
logger.info("✅ CORS configured for origins: %s", CORS_ORIGINS)

# ----------------------- Законы / Индекс -------------------
LAWS_PATH = os.getenv("LAWS_PATH", os.path.join("backend", "laws", "kazakh_laws.json"))

LAW_DB: List[Dict[str, Any]] = []
LAW_INDEX: Dict[str, set] = {}  # word -> set(article_idx)

def load_laws() -> None:
    global LAW_DB
    if not os.path.isfile(LAWS_PATH):
        raise FileNotFoundError(f"Не найден файл законов: {LAWS_PATH}")
    with open(LAWS_PATH, "r", encoding="utf-8") as f:
        data = json.load(f)

    if isinstance(data, dict) and "laws" in data:
        LAW_DB = data["laws"]
    elif isinstance(data, list):
        LAW_DB = data
    else:
        raise ValueError("Неверный формат файла законов.")

    for it in LAW_DB:
        it.setdefault("title", "")
        it.setdefault("text", "")
        it.setdefault("source", "")

    logger.info("✅ Загружено %d статей из базы законов.", len(LAW_DB))

def build_index() -> None:
    global LAW_INDEX
    LAW_INDEX = build_law_index(LAW_DB)
    logger.info("✅ Индекс законов успешно построен. Всего ключей: %d", len(LAW_INDEX))

# Инициализация при старте
load_laws()
build_index()

# ----------------------- Поиск по локальной БД -------------
def correct_keyword(word: str, keys: Iterable[str], cutoff: float = 0.78) -> str:
    matches = get_close_matches(word, list(keys), n=1, cutoff=cutoff)
    return matches[0] if matches else word

def find_local_candidates(query: str, top_k: int = 6) -> List[int]:
    if not query.strip():
        return []

    expanded = expand_keywords(query)
    logger.info("🔎 Расширенные ключевые слова: %s", list(expanded)[:12])

    scores: Dict[int, float] = {}
    index_keys = LAW_INDEX.keys()

    for raw in expanded:
        key = raw
        if key not in LAW_INDEX:
            key = correct_keyword(key, index_keys)
        if key not in LAW_INDEX:
            continue

        for idx in LAW_INDEX[key]:
            scores[idx] = scores.get(idx, 0.0) + 1.0
            title = LAW_DB[idx].get("title", "")
            if key in normalize_text(title):
                scores[idx] += 1.2

    if not scores:
        return []

    ranked = sorted(scores.items(), key=lambda x: x[1], reverse=True)
    return [idx for idx, _ in ranked[:top_k]]

# ----------------------- Внешний источник (Adilet) ---------
ADILET_BASE = "https://adilet.zan.kz"
ADILET_SEARCH = f"{ADILET_BASE}/rus/search/docs?query="

def search_adilet_titles(query: str, max_results: int = 5, timeout: int = 8) -> List[Dict[str, str]]:
    results: List[Dict[str, str]] = []
    try:
        url = ADILET_SEARCH + requests.utils.quote(query)
        headers = {"User-Agent": "KazLegalBot/1.0"}
        r = requests.get(url, headers=headers, timeout=timeout)
        r.raise_for_status()
        soup = BeautifulSoup(r.text, "html.parser")

        for a in soup.find_all("a", href=True):
            href = a["href"]
            if "/rus/docs/" in href:
                title = re.sub(r"\s+", " ", a.get_text(strip=True))
                if not title:
                    continue
                full_url = href if href.startswith("http") else ADILET_BASE + href
                results.append({"title": title, "source": full_url})
                if len(results) >= max_results:
                    break
    except Exception as e:
        logger.warning("⚠️ Adilet недоступен: %s", e)

    return results

# ----------------------- Системная инструкция --------------
def build_system_instruction(law_titles: List[Dict[str, str]]) -> str:
    """
    Возвращает ЖЁСТКИЕ правила для модели + список релевантных названий законов.
    ВАЖНО: просим выводить ТОЛЬКО HTML.
    """
    lines = []
    lines.append(
        "SYSTEM:\n"
        "Ты — полноценный ИИ-юрист по законодательству исключительно Республики Казахстан.\n"
        "Представляй себя как официального юриста, всегда пытайся помочь клиенту. Никогда не говори что не можешь ответить.\n"
        "Правила ответа:\n"
        "1) Формат вывода — ТОЛЬКО HTML. Никакого Markdown и **звёздочек**. "
        "Используй <p>, <ul>, <li>, <strong>, <em>, <h3>, <br>.\n"
        "2) Сначала дай краткую юридическую оценку (что применимо, нарушено ли право, ответственность).\n"
        "3) Затем — чёткие пошаговые действия (куда идти/писать, какие документы, сроки/риски).\n"
        "4) Ссылайся на НПА РК по возможности, НО не вставляй длинных цитат; достаточно названия статьи/кодекса.\n"
        "5) Если данных мало — сначала дай универсальный алгоритм действий, затем задавай уточняющие вопросы списком.\n"
        "6) Не отправляй пользователя «к другому юристу»; дай максимум практических шагов здесь.\n"
        "7) Если контекст не покрывает вопрос, обобщай безопасно и явно укажи, что приведены общие рекомендации.\n"
        "8) В конце добавь раздел <h3>Релевантные законы</h3> со списком названий (без больших цитат).\n"
    )

    if law_titles:
        lines.append("Релевантные законы (названия для ориентира, без цитат):")
        for i, l in enumerate(law_titles, 1):
            ttl = (l.get("title") or "").strip()
            src = (l.get("source") or "").strip()
            if ttl:
                if src:
                    lines.append(f"{i}. {ttl} — {src}")
                else:
                    lines.append(f"{i}. {ttl}")

    # Жёстко заякорим форматирование:
    lines.append(
        "\nВсегда возвращай валидный HTML. Пример мини-шаблона:\n"
        "<h3>Краткий вывод</h3>\n"
        "<p>…</p>\n"
        "<h3>Что делать</h3>\n"
        "<ul><li>Шаг 1…</li><li>Шаг 2…</li></ul>\n"
        "<h3>Уточните</h3>\n"
        "<ul><li>Вопрос 1…</li></ul>\n"
        "<h3>Релевантные законы</h3>\n"
        "<ul><li>Название акта/статьи…</li></ul>\n"
    )
    return "\n".join(lines)

# ----------------------- Модель / Фолбэк -------------------
USE_MODEL = os.getenv("GEMINI_MODEL", "gemini-1.5-flash")

def generate_with_gemini(prompt: str) -> str:
    import google.generativeai as genai
    api_key = os.getenv("GEMINI_API_KEY", "").strip()
    if not api_key:
        raise RuntimeError("GEMINI_API_KEY is empty")

    genai.configure(api_key=api_key)
    model = genai.GenerativeModel(USE_MODEL)
    resp = model.generate_content(prompt)
    if not resp or not resp.text:
        raise RuntimeError("Empty model response")
    return resp.text

def ensure_html(s: str) -> str:
    """
    Если модель вдруг вернула не-HTML/markdown – аккуратно оборачиваем в <p>.
    """
    if not s:
        return "<p>Нет данных.</p>"
    if re.search(r"</?[a-z][\s\S]*>", s, flags=re.I):
        return s
    # простая замена переводов строк на абзацы
    parts = [p.strip() for p in re.split(r"\n{2,}", s) if p.strip()]
    if not parts:
        return "<p>Нет данных.</p>"
    return "".join(f"<p>{p.replace('\n','<br>')}</p>" for p in parts)

def fallback_answer(question: str, laws: List[Dict[str, str]]) -> str:
    items = []
    items.append("<h3>Краткий вывод</h3>")
    items.append("<p>Я подготовил общий план действий по вашему вопросу. Нижые — безопасные шаги без ссылок на конкретные нормы.</p>")
    items.append("<h3>Что делать</h3>")
    items.append("<ul>")
    items.append("<li>Кратко опишите ситуацию (даты, стороны, документы).</li>")
    items.append("<li>Определите цель (например, прекратить трудовой договор без отработки / получить расчёт).</li>")
    items.append("<li>Проверьте договор и локальные акты работодателя на спецусловия.</li>")
    items.append("<li>Подайте письменное заявление/претензию, соблюдая сроки уведомления.</li>")
    items.append("<li>При необходимости — обращение в трудовую инспекцию или суд.</li>")
    items.append("</ul>")
    items.append("<h3>Релевантные законы</h3>")
    if laws:
        items.append("<ul>")
        for l in laws:
            ttl = (l.get("title") or "").strip()
            src = (l.get("source") or "").strip()
            if ttl:
                if src:
                    items.append(f"<li>{ttl} — <a href=\"{src}\">{src}</a></li>")
                else:
                    items.append(f"<li>{ttl}</li>")
        items.append("</ul>")
    else:
        items.append("<p>Подходящих статей в локальной базе не найдено. Уточните детали запроса.</p>")
    return "".join(items)

def stream_text_chunks(text: str, chunk_size: int = 1200) -> Iterable[str]:
    text = text.strip()
    for i in range(0, len(text), chunk_size):
        yield text[i:i + chunk_size]
        time.sleep(0.01)

# ----------------------- HTTP Handlers ---------------------
@app.route("/health", methods=["GET"])
def health():
    return jsonify({"status": "ok", "laws": len(LAW_DB), "index_keys": len(LAW_INDEX)})

@app.route("/chat", methods=["POST"])
def chat():
    try:
        payload = request.get_json(force=True, silent=False) or {}
        question = (payload.get("question") or payload.get("message") or "").strip()
        if not question:
            return jsonify({"error": "Empty question"}), 400

        logger.info("🗨️ Вопрос: %s", question)

        # 1) Локальный поиск
        local_ids = find_local_candidates(question, top_k=6)

        # 2) Внешние источники при нехватке локального контекста
        external = []
        if not local_ids:
            external = search_adilet_titles(question, max_results=5)
        elif len(local_ids) < 3:
            external = search_adilet_titles(question, max_results=3)

        # 3) Готовим список названий (title + source)
        context_titles: List[Dict[str, str]] = []
        for idx in local_ids:
            law = LAW_DB[idx]
            context_titles.append({"title": law.get("title", ""), "source": law.get("source", "")})
        # добавим внешние, избегая дублей по названию
        for e in external:
            if e.get("title") and not any(c["title"] == e["title"] for c in context_titles):
                context_titles.append({"title": e["title"], "source": e.get("source", "")})
        context_titles = context_titles[:6]

        # 4) Системная инструкция + вопрос
        system_instruction = build_system_instruction(context_titles)
        prompt = (
            f"{system_instruction}\n\n"
            f"USER QUESTION (отвечай ТОЛЬКО HTML):\n{question}\n"
        )

        # 5) Генерация
        try:
            raw = generate_with_gemini(prompt)
            text = ensure_html(raw)
        except Exception as e:
            logger.error("❌ Ошибка генерации: %s", e)
            text = fallback_answer(question, context_titles)

        # 6) Стриминг HTML
        def _gen():
            for chunk in stream_text_chunks(text):
                yield chunk

        return Response(stream_with_context(_gen()), mimetype="text/html; charset=utf-8")

    except Exception as e:
        logger.exception("Ошибка /chat: %s", e)
        return jsonify({"error": str(e)}), 500


if __name__ == "__main__":
    port = int(os.getenv("PORT", "5000"))
    app.run(host="0.0.0.0", port=port, debug=False)
