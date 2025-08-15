import json
import logging
import os
import re
from datetime import datetime
from typing import Dict, List

from flask import Flask, jsonify, make_response, request, Response
from flask import stream_with_context
from flask_cors import CORS
from dotenv import load_dotenv

import google.generativeai as genai

from helpers import load_laws, LawIndex, laws_to_html_context, escape_html

# -----------------------------
# БАЗОВАЯ НАСТРОЙКА
# -----------------------------

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s"
)

GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "").strip()
if not GEMINI_API_KEY:
    raise RuntimeError("GEMINI_API_KEY не задан в окружении.")

genai.configure(api_key=GEMINI_API_KEY)

MODEL_NAME = os.getenv("GEMINI_MODEL", "gemini-1.5-flash")  # быстрый вариант
TEMPERATURE = float(os.getenv("LLM_TEMPERATURE", "0.2"))

LAWS_PATH = os.getenv("LAWS_PATH", "backend/laws/kazakh_laws.json")

# CORS: список разрешённых origins
CORS_ORIGINS = [o.strip() for o in os.getenv(
    "CORS_ORIGINS",
    "http://localhost:5173,http://localhost:3000,https://*.vercel.app,https://*.netlify.app"
).split(",") if o.strip()]

app = Flask(__name__)
CORS(app, resources={r"/*": {"origins": CORS_ORIGINS}}, supports_credentials=True)

# -----------------------------
# ДАННЫЕ В ПАМЯТИ (без БД)
# -----------------------------

SESSIONS: Dict[str, List[Dict]] = {}   # session_id -> [{role, content, ts}]
SUMMARY: Dict[str, Dict] = {}          # session_id -> {id, title, updated_at}

# -----------------------------
# ЗАКОНЫ И ПОИСК
# -----------------------------

logging.info("Загрузка базы законов…")
_LAWS = load_laws(LAWS_PATH)
_INDEX = LawIndex(_LAWS)
logging.info("✅ Индекс законов готов: %d статей", len(_LAWS))

# -----------------------------
# СИСТЕМНЫЙ ПРОМПТ
# -----------------------------

SYSTEM_PROMPT_BASE = """
Ты — полноценный ИИ-юрист по законодательству исключительно Республики Казахстан.
Представляй себя как официального юриста, всегда пытайся помочь клиенту. Никогда не говори что не можешь ответить.
Отвечай кратко, структурированно и строго в HTML. Используй только эти теги:
<p>, <ul>, <li>, <strong>, <em>, <h3>, <a>, <br>. Без Markdown.

Всегда соблюдай структуру:
<h3>Юридическая оценка ситуации</h3>
<p>Чётко укажи, какие нормы могут применяться и почему, без общих «дисклеймеров».</p>

<h3>Действия</h3>
<ul>
<li>Пошагово: что сделать прямо сейчас.</li>
<li>Какие документы/доказательства собрать.</li>
<li>Сроки и куда обращаться.</li>
</ul>

<h3>Рекомендации</h3>
<ul>
<li>Полезные советы, лайфхаки, распространённые ошибки.</li>
</ul>

Если недостаточно данных — вставь:
<h3>Необходимая информация</h3>
<ul>
<li><strong>Что нужно уточнить:</strong> перечисли пунктами.</li>
</ul>

Если вопрос связан с угрозой жизни/здоровья — добавь:
<h3>Экстренные контакты</h3>
<ul>
<li>Полиция: <strong>102</strong></li>
<li>Единый номер экстренных служб: <strong>112</strong></li>
</ul>

Если у тебя есть контекст законов — процитируй релевантные статьи понятным языком и вставь ссылки.
Никаких «эта информация носит общий характер» — сразу по делу.
"""

# Фильтр для авто-удаления нежелательных «общих дисклеймеров»
DISCLAIMER_RX = re.compile(
    r"(важно|обратите внимание|не является юридической консультацией|"
    r"обратитесь к квалифицированному юристу|данная информация носит общий характер)",
    re.IGNORECASE
)

# -----------------------------
# ВСПОМОГАТЕЛЬНОЕ
# -----------------------------

def add_cors_headers(resp):
    origin = request.headers.get("Origin")
    if origin and any(origin.endswith(allowed.split("//")[-1]) or origin == allowed for allowed in CORS_ORIGINS):
        resp.headers["Access-Control-Allow-Origin"] = origin
    elif CORS_ORIGINS:
        resp.headers["Access-Control-Allow-Origin"] = CORS_ORIGINS[0]
    resp.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS, DELETE"
    resp.headers["Access-Control-Allow-Headers"] = "Content-Type, Authorization"
    resp.headers["Access-Control-Allow-Credentials"] = "true"
    resp.headers["Access-Control-Max-Age"] = "86400"
    return resp

@app.after_request
def _after(resp):
    return add_cors_headers(resp)

@app.route("/<path:_>", methods=["OPTIONS"])
def _cors_preflight(_):
    return add_cors_headers(make_response("", 204))

def sanitize_html(text: str) -> str:
    t = DISCLAIMER_RX.sub("", text or "")
    t = t.strip()
    # если модель прислала не HTML — обернём простыми <p>
    if "<" not in t and ">" not in t:
        t = "<p>" + escape_html(t).replace("\n\n", "</p><p>").replace("\n", "<br>") + "</p>"
    return t

def push_message(session_id: str, role: str, content: str):
    SESSIONS.setdefault(session_id, []).append({
        "role": role,
        "content": content,
        "ts": datetime.utcnow().isoformat() + "Z",
    })
    # заголовок для списка чатов — первая реплика пользователя
    if session_id not in SUMMARY:
        title = content.strip()
        title = re.sub(r"\s+", " ", title)
        SUMMARY[session_id] = {
            "id": session_id,
            "title": (title[:60] + "…") if len(title) > 60 else title,
            "updated_at": datetime.utcnow().isoformat() + "Z",
        }
    else:
        SUMMARY[session_id]["updated_at"] = datetime.utcnow().isoformat() + "Z"

# -----------------------------
# LLM ВЫЗОВ
# -----------------------------

def call_llm(html_system: str, user_prompt: str) -> str:
    model = genai.GenerativeModel(
        MODEL_NAME,
        generation_config={
            "temperature": TEMPERATURE,
            "response_mime_type": "text/plain",  # получаем текст и сами нормализуем в HTML
        }
    )
    # Генерируем единым куском (фронт может читать как "стрим", но тут будет один чанк — это ок)
    resp = model.generate_content(
        [
            {"role": "user", "parts": [html_system.strip()]},
            {"role": "user", "parts": [user_prompt]},
        ]
    )
    text = (resp.text or "").strip()
    return sanitize_html(text)

# -----------------------------
# ЭНДПОИНТЫ
# -----------------------------

@app.route("/health", methods=["GET"])
@app.route("/api/health", methods=["GET"])
def health():
    info = {
        "status": "ok",
        "model": MODEL_NAME,
        "laws_count": len(_LAWS),
        "time": datetime.utcnow().isoformat() + "Z",
    }
    return add_cors_headers(jsonify(info)), 200

@app.route("/get-all-sessions-summary", methods=["GET"])
@app.route("/api/get-all-sessions-summary", methods=["GET"])
def get_sessions():
    out = list(SUMMARY.values())
    out.sort(key=lambda x: x["updated_at"], reverse=True)
    return add_cors_headers(jsonify({"sessions": out})), 200

@app.route("/get-history", methods=["GET"])
@app.route("/api/get-history", methods=["GET"])
def get_history():
    session_id = request.args.get("session_id", "").strip()
    history = SESSIONS.get(session_id, [])
    return add_cors_headers(jsonify({"history": history})), 200

@app.route("/delete-session", methods=["DELETE"])
@app.route("/api/delete-session", methods=["DELETE"])
def delete_session():
    session_id = request.args.get("session_id", "").strip()
    SESSIONS.pop(session_id, None)
    SUMMARY.pop(session_id, None)
    return add_cors_headers(jsonify({"status": "ok"})), 200

@app.route("/ask", methods=["POST"])
@app.route("/api/ask", methods=["POST"])
def ask():
    try:
        data = request.get_json(force=True) or {}
        question = (data.get("question") or "").strip()
        session_id = (data.get("session_id") or "default").strip()
        if not question:
            return add_cors_headers(jsonify({"error": "Пустой вопрос"})), 400

        push_message(session_id, "user", question)

        # Поиск законов
        results = _INDEX.search(question, top_k=6)
        law_context_html = laws_to_html_context(results, question)

        system_prompt = SYSTEM_PROMPT_BASE
        if law_context_html:
            system_prompt += "\n" + law_context_html

        answer_html = call_llm(system_prompt, question)

        push_message(session_id, "model", answer_html)

        # отдаём «как стрим», но фактически одним куском (совместимо с фронтом)
        def gen():
            yield answer_html

        return add_cors_headers(Response(stream_with_context(gen()), mimetype="text/html"))

    except Exception as e:
        logging.exception("Ошибка в /ask")
        err = f"<p>Произошла ошибка при обработке запроса: {escape_html(str(e))}</p>"
        return add_cors_headers(Response(err, mimetype="text/html")), 200

# Опционально: загрузка документов (минимальная заглушка без OCR/vision,
# чтобы интерфейс не ломался; при желании можно подключить обработку PDF/Docx)
@app.route("/upload-document", methods=["POST"])
@app.route("/api/upload-document", methods=["POST"])
def upload_document():
    try:
        session_id = (request.form.get("session_id") or "default").strip()
        user_question = (request.form.get("question") or "").strip()
        f = request.files.get("file")

        descr = []
        if f:
            descr.append(f"Файл: {escape_html(f.filename)} (тип: {escape_html(f.mimetype or 'unknown')})")
        if user_question:
            descr.append(f"Комментарий: {escape_html(user_question)}")
        text_for_search = " ".join(descr) or "Документ без описания"

        push_message(session_id, "user", text_for_search)

        results = _INDEX.search(user_question or f.filename if f else "", top_k=6)
        law_context_html = laws_to_html_context(results, user_question or f.filename or "")

        system_prompt = SYSTEM_PROMPT_BASE
        if law_context_html:
            system_prompt += "\n" + law_context_html

        prompt = (user_question or "Проанализируй загруженный документ и дай рекомендации в рамках законов РК.")
        answer_html = call_llm(system_prompt, prompt)
        push_message(session_id, "model", answer_html)

        def gen():
            yield answer_html

        return add_cors_headers(Response(stream_with_context(gen()), mimetype="text/html"))
    except Exception as e:
        logging.exception("Ошибка в /upload-document")
        err = f"<p>Произошла ошибка при обработке документа: {escape_html(str(e))}</p>"
        return add_cors_headers(Response(err, mimetype="text/html")), 200

# Корень — просто 404 с подсказкой
@app.route("/", methods=["GET"])
def root():
    return add_cors_headers(Response("<p>Backend OK. Используйте /api/ask, /api/health и т.д.</p>", mimetype="text/html")), 404

if __name__ == "__main__":
    port = int(os.getenv("PORT", "5000"))
    app.run(host="0.0.0.0", port=port)
