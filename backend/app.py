# -*- coding: utf-8 -*-
import os
import time
import re
import logging
from typing import Dict, List, Tuple
from flask import Flask, request, jsonify, make_response, Blueprint
from flask_cors import CORS
import bleach

from helpers import load_laws, search_laws, detect_intent

# ========== ЛОГИ ==========
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()
logging.basicConfig(level=LOG_LEVEL, format="%(asctime)s - %(levelname)s - %(message)s")
log = logging.getLogger(__name__)

def _preview_bytes(b: bytes, limit: int = 600) -> str:
    try:
        t = b.decode("utf-8", errors="replace")
    except Exception:
        return f"<{len(b)} bytes, decode failed>"
    t = t.strip().replace("\n", "\\n")
    return (t[:limit] + ("…" if len(t) > limit else "")) or "<empty>"

# ========== APP / CORS ==========
app = Flask(__name__)
api = Blueprint("api", __name__, url_prefix="/api")

FRONTEND_ORIGIN = os.getenv("FRONTEND_ORIGIN", "")
if FRONTEND_ORIGIN:
    CORS(app, origins=[FRONTEND_ORIGIN], supports_credentials=True)
    log.info(f"✅ CORS включён для: {FRONTEND_ORIGIN}")
else:
    CORS(app, supports_credentials=True)
    log.warning("⚠️ FRONTEND_ORIGIN не задан — CORS открыт для всех (dev only).")

# ========== БАЗА ЗАКОНОВ ==========
LAWS_PATH = os.getenv("LAWS_PATH", "laws/kazakh_laws.json")
try:
    LAWS = load_laws(LAWS_PATH)
    log.info(f"✅ Индекс законов готов: {len(LAWS)} статей")
except Exception as e:
    LAWS = []
    log.exception(f"❌ Не удалось загрузить законы: {e}")

# ========== LLM (Gemini) ==========
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "").strip()
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-1.5-flash")
MODEL = None

SYSTEM_PROMPT = """
Ты — ИИ-юрист, специализирующийся на законодательстве Республики Казахстан.
Всегда отвечай СТРОГО в ЧИСТОМ HTML (без Markdown), используя только теги: <p>, <ul>, <li>, <strong>, <em>, <h3>, <a>, <br>.

СТИЛЬ И СОДЕРЖАНИЕ:
- Без воды и общих фраз. Сначала краткая <strong>Юридическая оценка</strong>, затем чёткий <strong>Алгоритм действий</strong> и <strong>Перечень документов</strong> (если применимо).
- Ссылайся на нормы из переданного контекста: указывай <strong>название акта</strong> и <strong>статью/раздел</strong>. Если есть source — добавь ссылку через <a>.
- Если точной нормы в контексте нет, дай аккуратный общий ответ по применимым актам РК (без выдумок) и укажи, каких данных не хватает.
- НИКОГДА не используй фразы вида «обратитесь к юристу / проконсультируйтесь у юриста / обратитесь в ЦОН для уточнения». Вместо этого давай конкретные шаги, формы, куда нажать/куда подать, какие поля заполнить.
- Каждая мысль — отдельный <p>. Списки — через <ul><li>... </li></ul>.

Шаблон структуры:
<h3>Юридическая оценка</h3>
<p>Коротко по сути вопроса и применимых актах.</p>

<h3>Что делать пошагово</h3>
<ul>
<li><strong>Шаг 1.</strong> ...</li>
<li><strong>Шаг 2.</strong> ...</li>
</ul>

<h3>Необходимые документы</h3>
<ul>
<li>...</li>
</ul>

<h3>Нормативные основания</h3>
<ul>
<li><strong>Название акта</strong> — статья/пункт. <a href="URL">Источник</a></li>
</ul>
"""

def _llm_bootstrap():
    global MODEL
    if MODEL is not None:
        return
    if not GEMINI_API_KEY:
        raise RuntimeError("GEMINI_API_KEY не задан — ИИ не сможет работать.")
    import google.generativeai as genai
    genai.configure(api_key=GEMINI_API_KEY)
    MODEL = genai.GenerativeModel(GEMINI_MODEL, system_instruction=SYSTEM_PROMPT)
    log.info(f"🤖 Gemini инициализирован: {GEMINI_MODEL}")

def _sanitize_html(html: str) -> str:
    allowed_tags = ["p", "ul", "li", "strong", "em", "h3", "a", "br"]
    allowed_attrs = {"a": ["href", "title", "target", "rel"]}
    cleaned = bleach.clean(html, tags=allowed_tags, attributes=allowed_attrs, strip=True)
    return cleaned.replace("<a ", '<a target="_blank" rel="noopener noreferrer" ')

_REFERRAL_RE = re.compile(
    r"(обратит(есь|е|ся)\s+к\s+юристу|проконсультируйт[еся]+\s+у\s+юриста|"
    r"обратит[ье]+\s+в\s+цон|"
    r"рекомендуется\s+обратиться\s+к\s+квалифицированному\s+юристу)",
    re.IGNORECASE
)

def _enforce_style(html: str) -> str:
    # Вычищаем реферальные фразы и смягчаем то, что просит «куда-то обратиться».
    html = _REFERRAL_RE.sub(
        "используйте предложенные ниже шаги и шаблоны — этого достаточно для самостоятельного оформления",
        html,
    )
    # Убираем пустые <p></p>
    html = re.sub(r"<p>\s*</p>", "", html)
    return html

def _build_llm_context(question: str, matches: List[Tuple[Dict, float]], intent_name: str) -> str:
    def cut(s: str, n: int = 900) -> str:
        s = (s or "").strip()
        return (s[:n] + "…") if len(s) > n else s

    lines = [f"Вопрос пользователя: {question}", ""]
    lines.append("Контекст (релевантные нормы и фрагменты):")
    for art, score in matches:
        title = art.get("title") or "Без названия"
        src = art.get("source") or ""
        frag = cut(art.get("text", ""))
        lines.append(f"- {title} | источник: {src if src else '—'}")
        if frag:
            lines.append(f"  Фрагмент: {frag}")
        lines.append("")
    if not matches:
        lines.append("Подходящих норм в локальной базе не найдено.")
    # Жёсткое требование к стилю
    lines.append(
        "\nСформируй ИТОГОВЫЙ ответ строго по шаблону и правилам из system_instruction. "
        "Не используй фразы про «обратиться к юристу». Дай чёткие шаги, документы и ссылки (если есть)."
    )
    return "\n".join(lines)

def _generate_with_llm(question: str, matches: List[Tuple[Dict, float]], intent_name: str) -> str:
    _llm_bootstrap()
    prompt = _build_llm_context(question, matches, intent_name)
    last_err = None
    for i in range(3):
        try:
            resp = MODEL.generate_content(
                prompt,
                generation_config={"temperature": 0.2, "max_output_tokens": 2048},
            )
            txt = (resp.text or "").strip()
            if not txt:
                raise RuntimeError("Пустой ответ от модели")
            html = _sanitize_html(txt)
            html = _enforce_style(html)
            return html
        except Exception as e:
            last_err = e
            log.warning(f"LLM attempt {i+1}/3 failed: {e}")
            time.sleep(0.8 * (i + 1))
    raise RuntimeError(f"LLM недоступна после ретраев: {last_err}")

# ========== ВСПОМОГАТЕЛЬНОЕ ==========
def _get_json_payload() -> (Dict, Dict):
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
            import json
            payload = json.loads(raw.decode("utf-8", errors="replace"))
    except Exception as e:
        err = f"JSON parse failed: {e}"

    return payload or {}, {"content_type": ctype, "body_preview": _preview_bytes(raw), "json_error": err}

def _json_error(status: int, code: str, message: str, debug: Dict = None):
    body = {"ok": False, "error": {"code": code, "message": message}}
    if debug:
        body["debug"] = debug
    resp = make_response(jsonify(body), status)
    resp.headers["Content-Type"] = "application/json; charset=utf-8"
    return resp

# ========== РОУТЫ ==========
@api.route("/health", methods=["GET"])
def api_health():
    return jsonify({"ok": True, "laws_count": len(LAWS), "llm": bool(GEMINI_API_KEY), "message": "alive"})

@api.route("/ask", methods=["POST", "OPTIONS"])
def api_ask():
    if request.method == "OPTIONS":
        return ("", 204)

    payload, dbg = _get_json_payload()
    question = (payload.get("question") or "").strip()
    if not question:
        return _json_error(400, "MISSING_FIELD", "Поле 'question' обязательно и не должно быть пустым.", dbg)

    try:
        intent = detect_intent(question)
        matches = search_laws(question, LAWS, top_k=3, intent=intent)
        used_llm = True
        try:
            answer_html = _generate_with_llm(question, matches, intent.get("name"))
        except Exception as e:
            used_llm = False
            log.error(f"❌ LLM недоступна, отдаём фолбэк: {e}")
            answer_html = _sanitize_html("""
                <p><strong>Извините, сервис генерации ответа временно недоступен.</strong></p>
                <p>Попробуйте повторить запрос позже. Вопрос сохранён на сервере.</p>
            """)

        return jsonify({
            "ok": True,
            "answer_html": answer_html,
            "matches": [{"title": a.get("title"), "source": a.get("source")} for a, _ in matches],
            "intent": intent.get("name"),
            "used_llm": used_llm
        })
    except Exception as e:
        log.exception("❌ Ошибка при обработке вопроса")
        return _json_error(500, "INTERNAL_ERROR", str(e))

# Корневой
@app.route("/", methods=["GET"])
def root():
    return make_response("Backend is up. Use /api/ask", 200)

# Регистрируем blueprint
app.register_blueprint(api)

if __name__ == "__main__":
    port = int(os.getenv("PORT", "5000"))
    app.run(host="0.0.0.0", port=port)
