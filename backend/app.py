# -*- coding: utf-8 -*-
import os, time, json, logging
from typing import Dict, Tuple, List
from concurrent.futures import ThreadPoolExecutor, TimeoutError

from flask import Flask, request, jsonify, make_response
from flask_cors import CORS

from helpers import init_index, search_laws, build_html_answer, call_llm

LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()
logging.basicConfig(level=LOG_LEVEL, format="%(asctime)s - %(levelname)s - %(message)s")
log = logging.getLogger(__name__)

app = Flask(__name__)

FRONTEND_ORIGIN = os.getenv("FRONTEND_ORIGIN", "")
if FRONTEND_ORIGIN:
    CORS(app, origins=[FRONTEND_ORIGIN], supports_credentials=True)
    log.info(f"✅ CORS включён для: {FRONTEND_ORIGIN}")
else:
    CORS(app, supports_credentials=True)
    log.warning("⚠️  FRONTEND_ORIGIN не задан — CORS открыт для всех (dev only).")

# единожды грузим индекс
DOCS, INDEX = init_index()
log.info("✅ Индекс готов: %d фрагментов", len(DOCS))

def _preview_bytes(b: bytes, limit: int = 500) -> str:
    try:
        t = b.decode("utf-8", errors="replace")
    except Exception:
        return f"<{len(b)} bytes, decode failed>"
    t = t.strip().replace("\n", "\\n")
    return (t[:limit] + ("…" if len(t) > limit else "")) or "<empty>"

def get_json_payload() -> Tuple[Dict, Dict]:
    headers = {k.lower(): v for k, v in request.headers.items()}
    ctype = headers.get("content-type", "")
    raw = request.get_data()
    dbg = {"content_type": ctype, "body_preview": _preview_bytes(raw), "json_error": None}
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

@app.route("/health", methods=["GET"])
@app.route("/api/health", methods=["GET"])
def health():
    return jsonify({"ok": True, "laws_count": len(DOCS), "llm": True, "message": "alive"})

_executor = ThreadPoolExecutor(max_workers=4)
LLM_TIMEOUT_SEC = int(os.getenv("LLM_TIMEOUT_SEC", "30"))

def _handle_ask():
    started = time.time()
    payload, dbg = get_json_payload()
    q = (payload.get("question") or "").strip()
    if not q:
        return json_error(400, "MISSING_FIELD", "Поле 'question' обязательно и не должно быть пустым.", dbg)
    log.info("👤 Вопрос: %s", q)

    hits, intent = search_laws(q, DOCS, INDEX, top_k=5)
    log.info("🔎 Найдено совпадений: %d", len(hits))

    # LLM с таймаутом
    llm_html = ""
    try:
        fut = _executor.submit(call_llm, q, hits)
        llm_html = fut.result(timeout=LLM_TIMEOUT_SEC) or ""
    except TimeoutError:
        log.error("⏳ LLM timeout (%ss) — отдаю rule-based fallback", LLM_TIMEOUT_SEC)
    except Exception as e:
        log.exception("LLM fail: %s", e)

    answer_html = llm_html.strip() or build_html_answer(q, hits, intent)
    took = int((time.time() - started) * 1000)
    log.info("✅ Ответ готов (%d симв) за %d мс", len(answer_html), took)
    return jsonify({
        "ok": True,
        "answer_html": answer_html,
        "matches": [
            {"article_title": r.get("article_title"), "source": r.get("source"), "score": s}
            for r, s in hits
        ],
        "intent": intent.get("name"),
        "took_ms": took
    })

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
    port = int(os.getenv("PORT", "5000"))
    app.run(host="0.0.0.0", port=port)
