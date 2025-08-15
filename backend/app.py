# -*- coding: utf-8 -*-
import os
import json
import logging
from typing import Dict, Tuple, List
from flask import Flask, request, jsonify, make_response
from flask_cors import CORS

# === Gemini ===
import google.generativeai as genai

from helpers import (
    load_laws,
    search_laws,
    detect_intent,
    get_playbook_html,
)

# ---------- логирование ----------
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()
logging.basicConfig(level=LOG_LEVEL, format="%(asctime)s - %(levelname)s - %(message)s")
log = logging.getLogger(__name__)

def _preview_bytes(b: bytes, limit: int = 800) -> str:
    try:
        t = b.decode("utf-8", errors="replace")
    except Exception:
        return f"<{len(b)} bytes, decode failed>"
    t = t.strip().replace("\n", "\\n")
    return (t[:limit] + ("…" if len(t) > limit else "")) or "<empty>"

# ---------- Flask ----------
app = Flask(__name__)

FRONTEND_ORIGIN = os.getenv("FRONTEND_ORIGIN", "*")
CORS(app, origins=[FRONTEND_ORIGIN] if FRONTEND_ORIGIN != "*" else "*", supports_credentials=True)
if FRONTEND_ORIGIN and FRONTEND_ORIGIN != "*":
    log.info(f"✅ CORS включён для: {FRONTEND_ORIGIN}")
else:
    log.warning("⚠️  FRONTEND_ORIGIN не задан — CORS открыт для всех (dev only).")

@app.after_request
def _after(resp):
    # Дублируем заголовки на всякий случай (Netlify/Proxy капризны)
    origin = FRONTEND_ORIGIN if FRONTEND_ORIGIN else "*"
    resp.headers["Access-Control-Allow-Origin"] = origin
    resp.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
    resp.headers["Access-Control-Allow-Headers"] = "Content-Type, Authorization"
    resp.headers["Access-Control-Allow-Credentials"] = "true"
    logging.debug("⬅️  %s %s -> %s CT:%s",
                  request.method, request.path, resp.status_code,
                  resp.headers.get("Content-Type"))
    return resp

# ---------- Загрузка законов ----------
LAWS_PATH = os.getenv("LAWS_PATH", "laws/kazakh_laws.json")
try:
    LAWS = load_laws(LAWS_PATH)
    log.info(f"✅ Индекс законов готов: {len(LAWS)} статей")
except Exception as e:
    LAWS = []
    log.exception(f"❌ Не удалось загрузить законы: {e}")

# ---------- Инициализация Gemini ----------
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "").strip()
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-1.5-flash")  # попробуйте 'gemini-1.5-pro' если доступно
LLM_ENABLED = bool(GEMINI_API_KEY)

if LLM_ENABLED:
    genai.configure(api_key=GEMINI_API_KEY)
    try:
        model = genai.GenerativeModel(
            model_name=GEMINI_MODEL,
            system_instruction="""
Ты — ИИ-юрист, специализирующийся на законодательстве Республики Казахстан.
Твоя задача — давать точные, практичные и полезные ответы. **Форматируй ответ только в HTML**.

Жёсткие требования:
1) НИКОГДА не ограничивайся фразой «обратитесь к юристу». Если риск высокий — кратко предупреди, но всё равно дай пошаговый план действий.
2) Если конкретной нормы в присланных фрагментах нет — дополни ответ «общей практикой» (отметь это), но всё равно:
   • дай «Юридическую оценку»;
   • дай «Что делать пошагово» (короткие императивные шаги);
   • дай «Документы и ссылки» (если есть).
3) Всегда выводи ответ **в чистом HTML**, без Markdown, со структурой:
   <h3>Юридическая оценка</h3>
   <p>…</p>
   <h3>Что делать пошагово</h3>
   <ul><li>Шаг 1: …</li> …</ul>
   <h3>Документы и ссылки</h3>
   <ul>…</ul>
   <details><summary>Использованные нормы</summary>…</details>  (если нормы есть)
4) Если опираешься на общую практику или типовые процедуры — явно пометь это фразой «По общей практике в РК: …».
5) Избегай лишней «воды» и общих советов. Конкретика важнее.

Важное про стиль:
- Используй <p>, <ul>, <ol>, <li>, <strong>, <em>, <h3>, <details>, <summary>, <a>.
- Не используй **Markdown**.
- Пиши кратко, по делу, без канцелярита.
""".strip()
        )
        log.info(f"🤖 Gemini инициализирован: {GEMINI_MODEL}")
    except Exception:
        log.exception("❌ Не удалось инициализировать Gemini")
        LLM_ENABLED = False
else:
    log.warning("⚠️ GEMINI_API_KEY не задан — LLM отключён.")

# ---------- Утилиты ----------
def get_json_payload() -> Tuple[Dict, Dict]:
    headers = {k.lower(): v for k, v in request.headers.items()}
    ctype = headers.get("content-type", "")
    raw = request.get_data()
    log.debug(
        "➡️  Incoming %s %s | CT: %s | H: %s | body: %s",
        request.method, request.path, ctype, headers, _preview_bytes(raw)
    )

    payload = None
    err = None
    try:
        payload = request.get_json(silent=True, force=False)
        if payload is None and raw:
            payload = json.loads(raw.decode("utf-8", errors="replace"))
    except Exception as e:
        err = f"JSON parse failed: {e}"

    return payload or {}, {"content_type": ctype, "body_preview": _preview_bytes(raw), "json_error": err}

def json_error(status: int, code: str, message: str, debug: Dict = None):
    body = {"ok": False, "error": {"code": code, "message": message}}
    if debug:
        body["debug"] = debug
    resp = make_response(jsonify(body), status)
    resp.headers["Content-Type"] = "application/json; charset=utf-8"
    return resp

# ---------- Роуты ----------
@app.route("/api/health", methods=["GET"])
def api_health():
    return jsonify({"ok": True, "laws_count": len(LAWS), "llm": LLM_ENABLED, "message": "alive"})

@app.route("/api/echo", methods=["POST", "OPTIONS"])
def api_echo():
    if request.method == "OPTIONS":
        return ("", 204)
    payload, dbg = get_json_payload()
    return jsonify({"ok": True, "received": payload, "debug": dbg})

def _call_llm(question: str, intent: str, law_hits: List[Tuple[Dict, float]]) -> str:
    """
    Собираем подсказку для модели: вопрос, playbook по теме и совпавшие нормы.
    Модель возвращает готовый HTML.
    """
    playbook_html = get_playbook_html(intent)

    norms_html = ""
    if law_hits:
        norms_items = []
        for art, score in law_hits:
            t = (art.get("title") or "").strip()
            s = (art.get("source") or "").strip()
            frag = (art.get("text") or "")[:800].strip().replace("\n", " ")
            norms_items.append(
                f'<li><strong>{t}</strong> — score {score:.2f} '
                f'{(" | " + f"<a href=\"%s\" target=\"_blank\">источник</a>" % s) if s else ""}'
                f'<br><em>Фрагмент:</em> {frag}</li>'
            )
        norms_html = "<details><summary>Нормы, найденные по базе</summary><ul>" + "".join(norms_items) + "</ul></details>"

    user_prompt = f"""
Вопрос пользователя (РК): {question}

{('<h3>Контекст по теме</h3>' + playbook_html) if playbook_html else ''}

{norms_html if norms_html else '<!-- норм в базе не найдено; всё равно дай практические шаги по общей практике РК -->'}

Сформируй итоговый ответ строго в **HTML**, по правилам из system_instruction.
""".strip()

    log.debug("🧠 Prompt to LLM (preview): %s", user_prompt[:1200])

    resp = model.generate_content(
        contents=[{"role": "user", "parts": [{"text": user_prompt}]}],
        generation_config={
            "temperature": 0.2,
            "top_p": 0.9,
            "top_k": 40,
            "max_output_tokens": 1536,
        },
        safety_settings=[  # деликатные темы пропускаем, но с нейтральной подачей
            {"category": "HARM_CATEGORY_HARASSMENT", "threshold": "BLOCK_NONE"},
            {"category": "HARM_CATEGORY_HATE_SPEECH", "threshold": "BLOCK_NONE"},
            {"category": "HARM_CATEGORY_SEXUAL", "threshold": "BLOCK_NONE"},
            {"category": "HARM_CATEGORY_DANGEROUS", "threshold": "BLOCK_NONE"},
        ],
    )
    html = (resp.text or "").strip()
    if not html:
        html = "<p><strong>Техническая заметка:</strong> не удалось получить развёрнутый ответ от модели. Ниже — минимально полезная информация.</p>"
        html += playbook_html or ""
    return html

def _pipeline_answer(question: str) -> Dict:
    # 1) намерение
    intent = detect_intent(question)
    # 2) поиск норм
    hits, _ = search_laws(question, LAWS, top_k=3)
    # 3) LLM
    if LLM_ENABLED:
        answer_html = _call_llm(question, intent["name"], hits)
    else:
        # Фолбэк без модели — хотя вы просили «модель всегда включена», оставлю на всякий
        from helpers import build_minimal_html_answer
        answer_html = build_minimal_html_answer(question, hits, intent)
    return {
        "answer_html": answer_html,
        "matches": [{"title": a.get("title"), "source": a.get("source"), "score": s} for a, s in hits],
        "intent": intent["name"],
    }

@app.route("/api/ask", methods=["POST", "OPTIONS"])
def api_ask():
    if request.method == "OPTIONS":
        return ("", 204)
    payload, dbg = get_json_payload()
    question = (payload.get("question") or "").strip()
    if not question:
        return json_error(400, "MISSING_FIELD", "Поле 'question' обязательно и не должно быть пустым.", dbg)
    try:
        result = _pipeline_answer(question)
        return jsonify({"ok": True, **result})
    except Exception as e:
        log.exception("❌ Ошибка при обработке вопроса")
        return json_error(500, "INTERNAL_ERROR", str(e))

# Алиас без /api — на случай кривого прокси
@app.route("/ask", methods=["POST", "OPTIONS"])
def ask_alias():
    return api_ask()

@app.route("/api/healthz", methods=["GET"])
def healthz_alias():
    return api_health()

@app.route("/", methods=["GET"])
def root_404():
    return make_response("Not Found", 404)

if __name__ == "__main__":
    port = int(os.getenv("PORT", "5000"))
    app.run(host="0.0.0.0", port=port)
