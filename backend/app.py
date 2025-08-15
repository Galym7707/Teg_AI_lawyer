# -*- coding: utf-8 -*-
import os
import time
import logging
from typing import Dict, List, Tuple
from flask import Flask, request, jsonify, make_response
from flask_cors import CORS

import bleach  # для безопасной очистки HTML от LLM
from helpers import load_laws, search_laws  # локальный поиск (контекст для LLM)

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
FRONTEND_ORIGIN = os.getenv("FRONTEND_ORIGIN", "")
if FRONTEND_ORIGIN:
    CORS(app, origins=[FRONTEND_ORIGIN], supports_credentials=True)
    log.info(f"✅ CORS включён для: {FRONTEND_ORIGIN}")
else:
    CORS(app, supports_credentials=True)
    log.warning("⚠️ FRONTEND_ORIGIN не задан — CORS открыт для всех (dev only).")

# ========== ЗАКОНЫ (КАЗ) ==========
LAWS_PATH = os.getenv("LAWS_PATH", "laws/kazakh_laws.json")
try:
    LAWS = load_laws(LAWS_PATH)
    log.info(f"✅ Индекс законов готов: {len(LAWS)} статей")
except Exception as e:
    LAWS = []
    log.exception(f"❌ Не удалось загрузить законы: {e}")

# ========== LLM (Gemini — ВСЕГДА ИСПОЛЬЗУЕМ) ==========
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "").strip()
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-1.5-flash")
MODEL = None

SYSTEM_PROMPT = """
Ты — ИИ-юрист, специализирующийся исключительно на законодательстве Республики Казахстан.
Отвечай СТРОГО в ЧИСТОМ HTML (без Markdown), используя только: <p>, <ul>, <li>, <strong>, <em>, <h3>, <a>.
Правила:
1) Сначала краткая ЮРИДИЧЕСКАЯ ОЦЕНКА по сути вопроса.
2) Затем — ПРАКТИЧЕСКИЕ ШАГИ (алгоритм, куда идти/что писать).
3) Если в контексте есть нормы — укажи название акта и статью/раздел, добавь ссылку, если передана.
4) Если точных норм нет — дай аккуратный общий разбор по ТК РК/ГК РК и попроси нужные уточнения, но всё равно предложи шаги.
5) Не используй **звёздочки** и Markdown, только HTML. Каждый абзац — <p>...</p>.
6) Пиши профессионально, ясно и по делу. Не уходи в «я всего лишь ИИ».
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

def _build_llm_context(question: str, matches: List[Tuple[Dict, float]], intent_name: str) -> str:
    """
    Компактный текстовый контекст: список норм (title, source) + релевантный фрагмент.
    """
    def _snippet(text: str, max_len: int = 700) -> str:
        import re
        t = (text or "").strip()
        if not t:
            return ""
        sentences = re.split(r"(?<=[\.\!\?])\s+", t)
        # простая эвристика по теме, чтобы ловить увольнение и т.п.
        topic = {
            "termination": ["увол", "увольн", "расторжен", "прекращен", "труд", "работодател", "работник", "договор"],
            "rental": ["аренд", "найм", "квартир", "жиль", "съем", "арендодатель", "арендатор"],
        }.get((intent_name or "generic").lower(), [])
        for s in sentences:
            if any(k in s.lower() for k in topic):
                s = s.strip()
                return (s[:max_len] + "…") if len(s) > max_len else s
        head = t
        return (head[:max_len] + "…") if len(head) > max_len else head

    lines = [f"Вопрос пользователя: {question}", "", "Релевантные нормы и фрагменты:"]
    for art, score in matches:
        ttl = art.get("title") or "Без названия"
        src = art.get("source") or ""
        snp = _snippet(art.get("text", ""))
        lines.append(f"- {ttl} | источник: {src if src else '—'}")
        if snp:
            lines.append(f"  Фрагмент: {snp}")
        lines.append("")
    if not matches:
        lines.append("Подходящих норм не найдено в локальной базе. Дай общий ответ по действующему праву РК.")
    return "\n".join(lines)

def _sanitize_html(html: str) -> str:
    allowed_tags = ["p", "ul", "li", "strong", "em", "h3", "a", "br"]
    allowed_attrs = {"a": ["href", "title", "target", "rel"]}
    cleaned = bleach.clean(html, tags=allowed_tags, attributes=allowed_attrs, strip=True)
    # Гарантируем, что ссылки не открывают уязвимости
    return cleaned.replace("<a ", '<a target="_blank" rel="noopener noreferrer" ')

def _generate_with_llm(question: str, matches: List[Tuple[Dict, float]], intent_name: str) -> str:
    _llm_bootstrap()
    user_prompt = (
        _build_llm_context(question, matches, intent_name)
        + "\n\nСформируй итоговый ответ СТРОГО в HTML по правилам из system_instruction."
    )
    last_err = None
    for i in range(3):
        try:
            resp = MODEL.generate_content(
                user_prompt,
                generation_config={"temperature": 0.2, "max_output_tokens": 2048},
            )
            txt = (resp.text or "").strip()
            if not txt:
                raise RuntimeError("Пустой ответ от модели")
            return _sanitize_html(txt)
        except Exception as e:
            last_err = e
            log.warning(f"LLM attempt {i+1}/3 failed: {e}")
            time.sleep(0.8 * (i + 1))
    raise RuntimeError(f"LLM недоступна после ретраев: {last_err}")

# ========== ВСПОМОГАТЕЛЬНОЕ ==========
def get_json_payload() -> (Dict, Dict):
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

def json_error(status: int, code: str, message: str, debug: Dict = None):
    body = {"ok": False, "error": {"code": code, "message": message}}
    if debug:
        body["debug"] = debug
    resp = make_response(jsonify(body), status)
    resp.headers["Content-Type"] = "application/json; charset=utf-8"
    return resp

# ========== РОУТЫ ==========
@app.route("/health", methods=["GET"])
def health():
    return jsonify({"ok": True, "laws_count": len(LAWS), "llm": True, "message": "alive"})

@app.route("/ask", methods=["POST", "OPTIONS"])
@app.route("/chat", methods=["POST", "OPTIONS"])
def ask():
    if request.method == "OPTIONS":
        return ("", 204)

    payload, dbg = get_json_payload()
    question = (payload.get("question") or "").strip()
    if not question:
        return json_error(400, "MISSING_FIELD", "Поле 'question' обязательно и не должно быть пустым.", dbg)

    try:
        matches, intent = search_laws(question, LAWS, top_k=3)
        intent_name = (intent or {}).get("name") or "generic"

        # ВСЕГДА вызываем LLM
        used_llm = True
        try:
            answer_html = _generate_with_llm(question, matches, intent_name)
        except Exception as e:
            used_llm = False
            log.error(f"❌ LLM недоступна, отдаём фолбэк: {e}")
            answer_html = _sanitize_html(f"""
                <p><strong>Извините, сервис генерации ответа временно недоступен.</strong></p>
                <p>Мы уже работаем над восстановлением. Попробуйте повторить запрос чуть позже.</p>
                <h3>Временные рекомендации</h3>
                <ul>
                    <li>Сформулируйте вопрос максимально конкретно (кто/что/когда/какие документы есть).</li>
                    <li>Если вопрос срочный, укажите сроки и статус (например, «испытательный срок», «аренда жилья» и т.д.).</li>
                </ul>
            """)

        return jsonify({
            "ok": True,
            "answer_html": answer_html,
            "matches": [{"title": a.get("title"), "source": a.get("source")} for a, _ in matches],
            "intent": intent_name,
            "used_llm": used_llm
        })
    except Exception as e:
        log.exception("❌ Ошибка при обработке вопроса")
        return json_error(500, "INTERNAL_ERROR", str(e))

@app.route("/", methods=["GET"])
def root_404():
    return make_response("Not Found", 404)

if __name__ == "__main__":
    port = int(os.getenv("PORT", "5000"))
    app.run(host="0.0.0.0", port=port)
