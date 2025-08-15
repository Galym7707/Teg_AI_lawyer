# -*- coding: utf-8 -*-
import os
import time
import json
import logging
from typing import Dict, Tuple, List
from concurrent.futures import ThreadPoolExecutor, TimeoutError

from flask import Flask, request, jsonify, make_response
from flask_cors import CORS

from helpers import load_laws, search_laws, build_html_answer, call_llm

# ---------- ЛОГИ ----------
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

# ---------- APP ----------
app = Flask(__name__)

FRONTEND_ORIGIN = os.getenv("FRONTEND_ORIGIN", "")
if FRONTEND_ORIGIN:
    CORS(app, origins=[FRONTEND_ORIGIN], supports_credentials=True)
    log.info(f"✅ CORS включён для: {FRONTEND_ORIGIN}")
else:
    CORS(app, supports_credentials=True)
    log.warning("⚠️  FRONTEND_ORIGIN не задан — CORS открыт для всех (dev only).")

# ---------- ЗАКОНЫ ----------
LAWS_PATH = os.getenv("LAWS_PATH", "laws/kazakh_laws.json")
log.info("Загрузка базы законов…")
try:
    LAWS = load_laws(LAWS_PATH)
    log.info(f"✅ Загружено {len(LAWS)} статей из базы законов")
except Exception as e:
    log.exception("❌ Не удалось загрузить базу законов")
    LAWS = []

# на всякий случай построим индекс сразу через вызов search_laws с пустым запросом не нужно
log.info(f"✅ Индекс законов готов: {len(LAWS)} статей")

# ---------- УТИЛИТЫ ----------
def get_json_payload() -> Tuple[Dict, Dict]:
    headers = {k.lower(): v for k, v in request.headers.items()}
    ctype = headers.get("content-type", "")
    raw = request.get_data()
    dbg = {
        "content_type": ctype,
        "body_preview": _preview_bytes(raw),
        "json_error": None
    }
    log.debug("➡️  %s %s | CT: %s | H: %s | body: %s",
              request.method, request.path, ctype, headers, dbg["body_preview"])
    payload = {}
    if raw:
        try:
            payload = json.loads(raw.decode("utf-8", errors="replace"))
        except Exception as e:
            dbg["json_error"] = str(e)
    return payload, dbg

def json_error(status: int, code: str, message: str, debug: Dict = None):
    body = {"ok": False, "error": {"code": code, "message": message}}
    if debug:
        body["debug"] = debug
    resp = make_response(jsonify(body), status)
    resp.headers["Content-Type"] = "application/json; charset=utf-8"
    return resp

LLM_TIMEOUT_SEC = int(os.getenv("LLM_TIMEOUT_SEC", "30"))
_executor = ThreadPoolExecutor(max_workers=4)

def llm_with_timeout(question: str, hits: List[Tuple[Dict, float]]):
    return call_llm(question, hits)

# ---------- МАРШРУТЫ ----------
@app.route("/health", methods=["GET"])
@app.route("/api/health", methods=["GET"])
def health():
    return jsonify({"ok": True, "laws_count": len(LAWS), "llm": True, "message": "alive"})

def _handle_ask():
    started = time.time()
    payload, dbg = get_json_payload()
    q = (payload.get("question") or "").strip()
    if not q:
        log.warning("❌ MISSING_FIELD question. dbg=%s", dbg)
        return json_error(400, "MISSING_FIELD", "Поле 'question' обязательно и не должно быть пустым.", dbg)

    log.info("👤 Вопрос: %s", q)

    # 1) быстрый поиск по законам
    hits, intent = search_laws(q, LAWS, top_k=3)
    log.info("🔎 Поиск вернул %d совпадений, intent=%s", len(hits), intent.get("name"))

    # 2) LLM с таймаутом
    llm_html = ""
    try:
        fut = _executor.submit(llm_with_timeout, q, hits)
        llm_html = fut.result(timeout=LLM_TIMEOUT_SEC) or ""
    except TimeoutError:
        log.error("⏳ LLM timeout (%ss) — отдаю только rule-based", LLM_TIMEOUT_SEC)
    except Exception as e:
        log.exception("❌ Ошибка LLM: %s", e)

    # 3) сборка HTML (LLM-ответ приоритетнее, но упадём на rule-based если пусто)
    answer_html = llm_html.strip() or build_html_answer(q, hits, intent)
    elapsed = (time.time() - started) * 1000
    log.info("✅ Готов ответ (%d симв), за %.0f мс", len(answer_html), elapsed)

    return jsonify({
        "ok": True,
        "answer_html": answer_html,
        "matches": [{"title": a.get("title"), "source": a.get("source"), "score": s} for a, s in hits],
        "intent": intent.get("name"),
        "took_ms": round(elapsed)
    })

# поддерживаем И БЕЗ префикса, И с префиксом /api
@app.route("/ask", methods=["POST", "OPTIONS"])
@app.route("/api/ask", methods=["POST", "OPTIONS"])
def ask():
    if request.method == "OPTIONS":
        return ("", 204)
    try:
        return _handle_ask()
    except Exception as e:
        log.exception("🔥 INTERNAL_ERROR in /ask: %s", e)
        return json_error(500, "INTERNAL_ERROR", str(e))

@app.route("/", methods=["GET"])
def root_404():
    return make_response("Not Found", 404)

if __name__ == "__main__":
    # Dev run. В проде запускаем gunicorn (см. ниже).
    port = int(os.getenv("PORT", "5000"))
    app.run(host="0.0.0.0", port=port)
