# -*- coding: utf-8 -*-
"""
Kaz Legal Bot — Flask backend
- Алиасы роутов: /ask и /chat (оба POST)
- Здоровый CORS
- Жирный дебаг входящих запросов (заголовки, превью тела)
- Понятные JSON-ошибки с кодами
- Загрузка законов из JSON (LAWS_PATH или ./laws/kazakh_laws.json)
- Поиск: токенизация + взвешенное совпадение по title/text, n-best
- HTML-ответ (без Markdown)
"""
import os
import json
import logging
from typing import List, Dict, Tuple
from flask import Flask, request, jsonify, make_response
from flask_cors import CORS
from collections import Counter
import re
import html

# ------------------------- ЛОГГИРОВАНИЕ -------------------------
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()
logging.basicConfig(
    level=LOG_LEVEL,
    format="%(asctime)s - %(levelname)s - %(message)s"
)
log = logging.getLogger(__name__)

def _preview_bytes(b: bytes, limit: int = 600) -> str:
    try:
        t = b.decode("utf-8", errors="replace")
    except Exception:
        return f"<{len(b)} bytes, decode failed>"
    t = t.strip().replace("\n", "\\n")
    return (t[:limit] + ("…" if len(t) > limit else "")) or "<empty>"

# ------------------------- ПРИЛОЖЕНИЕ -------------------------
app = Flask(__name__)

# CORS: явно укажите домен фронта (Netlify/Vercel)
FRONTEND_ORIGIN = os.getenv("FRONTEND_ORIGIN", "")
if FRONTEND_ORIGIN:
    CORS(app, origins=[FRONTEND_ORIGIN], supports_credentials=True)
    log.info(f"✅ CORS включён для: {FRONTEND_ORIGIN}")
else:
    # на самый край – разрешить всё (на проде лучше задать FRONTEND_ORIGIN)
    CORS(app, supports_credentials=True)
    log.warning("⚠️  FRONTEND_ORIGIN не задан — CORS открыт для всех (dev only).")

# ------------------------- ЗАКОНЫ -------------------------
DEFAULT_LAWS_PATH = os.getenv("LAWS_PATH", "laws/kazakh_laws.json")

def load_laws(path: str) -> List[Dict]:
    log.info("Загрузка базы законов…")
    if not os.path.exists(path):
        # Попробуем относительный путь от корня приложения
        alt = os.path.join(os.path.dirname(__file__), path)
        if os.path.exists(alt):
            path = alt
        else:
            raise FileNotFoundError(f"Не найден файл законов: {path}")

    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)

    # Ожидаем список объектов с полями: title, text, source
    cleaned = []
    for i, item in enumerate(data):
        title = (item.get("title") or "").strip()
        text  = (item.get("text")  or "").strip()
        source = (item.get("source") or "").strip()
        if not text:
            continue
        cleaned.append({"title": title, "text": text, "source": source})
    return cleaned

try:
    LAWS: List[Dict] = load_laws(DEFAULT_LAWS_PATH)
    log.info(f"✅ Индекс законов готов: {len(LAWS)} статей")
except Exception as e:
    LAWS = []
    log.exception(f"❌ Не удалось загрузить законы: {e}")

# ------------------------- ПОИСК -------------------------
_WORD_RE = re.compile(r"[а-яёa-z0-9]+", re.IGNORECASE)

def _tokens(s: str) -> List[str]:
    return _WORD_RE.findall(s.lower())

def _score_article(q_tokens: Counter, art: Dict) -> float:
    """Взвешенное совпадение: title – в 2 раза важнее.
    Плюс небольшой бонус за длину пересечения (уникальные совпавшие токены)."""
    t_title = Counter(_tokens(art.get("title", "")))
    t_text  = Counter(_tokens(art.get("text", "")))

    # скалярное произведение
    def dot(a: Counter, b: Counter) -> int:
        return sum(min(a[k], b.get(k, 0)) for k in a)

    title_part = 2.0 * dot(q_tokens, t_title)
    text_part  = 1.0 * dot(q_tokens, t_text)

    overlap = len(set(q_tokens) & set(t_title.keys() | t_text.keys()))
    return title_part + text_part + 0.2 * overlap

def search_laws(question: str, top_k: int = 3) -> List[Tuple[Dict, float]]:
    if not LAWS:
        return []
    q_tokens = Counter(_tokens(question))
    scored = [(art, _score_article(q_tokens, art)) for art in LAWS]
    scored.sort(key=lambda x: x[1], reverse=True)
    # отбросим «мусор» с нулевым скором
    scored = [x for x in scored if x[1] > 0]
    return scored[:top_k]

# ------------------------- ОТВЕТ -------------------------
def _html_escape(s: str) -> str:
    return html.escape(s, quote=True)

def build_html_answer(question: str, hits: List[Tuple[Dict, float]]) -> str:
    if not hits:
        # допускаем ответ без прямых совпадений — короткая справка + просьба уточнить
        return (
            "<h3>Предварительная консультация</h3>"
            f"<p>К сожалению, в предоставленной базе не нашлось прямых совпадений по вашему запросу: "
            f"<em>{_html_escape(question)}</em>.</p>"
            "<p>Опишите, пожалуйста, детали ситуации поконкретнее (даты, участники, документы). "
            "Я попробую сузить поиск и дать точные ссылки на нормы права.</p>"
        )

    parts = [
        "<h3>Анализ по базе законов РК</h3>",
        f"<p><strong>Ваш вопрос:</strong> {_html_escape(question)}</p>",
        "<ol>"
    ]
    for art, score in hits:
        title  = _html_escape(art.get("title") or "Без названия")
        source = _html_escape(art.get("source") or "")
        text   = _html_escape(art.get("text")[:1000])  # превью текста (безопасный HTML)
        parts.append(
            "<li>"
            f"<p><strong>Норма:</strong> {title}"
            + (f" (<a href=\"{source}\" target=\"_blank\" rel=\"noopener\">источник</a>)" if source else "")
            + f"</p>"
            f"<p><em>Фрагмент:</em> {text}…</p>"
            "</li>"
        )
    parts.append("</ol>")
    parts.append(
        "<p><strong>Важно:</strong> это автоматическая выборка по ключевым словам. "
        "Если расскажете детали (вид договора, даты, статусы сторон), смогу точнее сослаться на нужные статьи.</p>"
    )
    return "".join(parts)

# ------------------------- УТИЛИТЫ HTTP -------------------------
def get_json_payload() -> Tuple[Dict, Dict]:
    """Пытаемся корректно разобрать JSON и даём полезную диагностику."""
    headers = {k.lower(): v for k, v in request.headers.items()}
    ctype = headers.get("content-type", "")
    raw = request.get_data()  # bytes
    log.debug(
        "➡️  Incoming %s %s | CT: %s | H: %s | body: %s",
        request.method, request.path, ctype, headers, _preview_bytes(raw)
    )

    payload = None
    err = None
    try:
        payload = request.get_json(silent=True, force=False)
        if payload is None and raw:
            # иногда фронт присылает text/plain с JSON внутри
            payload = json.loads(raw.decode("utf-8", errors="replace"))
    except Exception as e:
        err = f"JSON parse failed: {e}"

    return payload or {}, {
        "content_type": ctype,
        "body_preview": _preview_bytes(raw),
        "json_error": err,
    }

def json_error(status: int, code: str, message: str, debug: Dict = None):
    body = {"ok": False, "error": {"code": code, "message": message}}
    if debug:
        body["debug"] = debug
    resp = make_response(jsonify(body), status)
    resp.headers["Content-Type"] = "application/json; charset=utf-8"
    return resp

# ------------------------- РОУТЫ -------------------------
@app.route("/health", methods=["GET"])
def health():
    return jsonify({
        "ok": True,
        "laws_count": len(LAWS),
        "message": "alive"
    })

# Оба алиаса ведут к одному обработчику
@app.route("/ask", methods=["POST", "OPTIONS"])
@app.route("/chat", methods=["POST", "OPTIONS"])
def ask():
    if request.method == "OPTIONS":
        # preflight
        return ("", 204)

    payload, dbg = get_json_payload()
    question = (payload.get("question") or "").strip()

    if not question:
        return json_error(
            400, "MISSING_FIELD",
            "Поле 'question' обязательно и не должно быть пустым.",
            debug=dbg | {"payload_keys": list(payload.keys())}
        )

    try:
        hits = search_laws(question, top_k=3)
        html_answer = build_html_answer(question, hits)
        return jsonify({
            "ok": True,
            "answer_html": html_answer,
            "matches": [
                {
                    "title": a.get("title"),
                    "source": a.get("source"),
                    "score": s
                } for a, s in hits
            ]
        })
    except Exception as e:
        log.exception("❌ Ошибка при обработке вопроса")
        return json_error(500, "INTERNAL_ERROR", str(e))

# «корень» можно оставить 404 — Railway иногда стучится GET /
@app.route("/", methods=["GET"])
def root_404():
    return make_response("Not Found", 404)

# ------------------------- ЗАПУСК -------------------------
if __name__ == "__main__":
    port = int(os.getenv("PORT", "5000"))
    app.run(host="0.0.0.0", port=port)
