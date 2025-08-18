# -*- coding: utf-8 -*-
import os
from dotenv import load_dotenv  # Add this
load_dotenv(os.path.join(os.path.dirname(__file__), '.env'))
import json
import time
import logging
from typing import Dict, Tuple, List, Optional
from concurrent.futures import ThreadPoolExecutor, TimeoutError
import psycopg2
from psycopg2.extras import Json
from flask import Flask, request, jsonify, make_response
from flask_cors import CORS
import threading
from flask_cors import cross_origin

from helpers import (
    init_index,
    search_laws,
    build_html_answer,
    call_llm,
    web_enrich_official_sources,
    sanitize_html,  # <-- добавили
    load_jsonl,  # <-- добавили
)



LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()
logging.basicConfig(level=LOG_LEVEL, format="%(asctime)s - %(levelname)s - %(message)s")
log = logging.getLogger(__name__)

app = Flask(__name__)

# Убедитесь что пути совпадают с фронтендом
LAWS_PATH = os.path.join(os.path.dirname(__file__), "laws", "normalized.jsonl")

def check_files():
    if not os.path.exists(LAWS_PATH):
        log.error(f"❌ Файл normalized.jsonl не найден по пути: {LAWS_PATH}")
    else:
        log.info(f"✅ Файл normalized.jsonl найден, размер: {os.path.getsize(LAWS_PATH)/1024:.1f} KB")

# Call the function directly after app creation
check_files()

# CORS configuration
allowed_origins = [
    "https://teg-ai-lawyer.netlify.app",
    "http://127.0.0.1:5500",
    "http://localhost:5500"
]

if os.getenv("FLASK_ENV") == "development":
    allowed_origins.append("*")

CORS(app, 
     origins=allowed_origins, 
     supports_credentials=True,
     allow_headers=["Content-Type", "Authorization"],
     methods=["GET", "POST", "OPTIONS"])

log.info(f"✅ CORS включён для: {allowed_origins}")

# Lazy loading для индекса законов
class LazyIndex:
    def __init__(self):
        self._docs = None
        self._index = None
        self._lock = threading.Lock()
        self._initialized = False
        self._error = None
    
    def _init_index(self):
        """Инициализация индекса при первом обращении"""
        if self._initialized:
            return
        
        with self._lock:
            if self._initialized:  # Double-check locking
                return
            
            try:
                log.info("🔄 Инициализация индекса законов...")
                self._docs, self._index = init_index()
                self._initialized = True
                log.info("✅ Индекс готов: %d фрагментов", len(self._docs))
            except Exception as e:
                log.exception("Index init failed")
                self._error = e
                log.error("❌ Ошибка инициализации индекса: %s", e)
                raise
    
    @property
    def docs(self):
        if not self._initialized:
            self._init_index()
        if self._error:
            raise self._error
        return self._docs
    
    @property
    def index(self):
        if not self._initialized:
            self._init_index()
        if self._error:
            raise self._error
        return self._index
    
    def is_ready(self) -> bool:
        """Проверка готовности индекса"""
        return self._initialized and self._error is None

# Глобальный экземпляр lazy index
LAZY_INDEX = LazyIndex()
LAZY_INDEX._init_index()  # Принудительная инициализация при старте

# Database setup
DB_DSN = os.getenv("DATABASE_URL")
DB = None
if DB_DSN:
    try:
        DB = psycopg2.connect(DB_DSN)
        with DB, DB.cursor() as cur:
            cur.execute("""
            create table if not exists qa_logs (
              id bigserial primary key,
              ts timestamptz default now(),
              question text not null,
              answer_html text not null,
              intent text,
              matches jsonb
            )
            """)
        log.info("✅ DB logging enabled")
    except Exception as e:
        log.warning("DB connect failed: %s", e)
        DB = None

def _log_qa(question: str, answer_html: str, intent: str, matches: List[Dict]):
    if not DB:
        return
    try:
        with DB, DB.cursor() as cur:
            cur.execute(
                "insert into qa_logs(question, answer_html, intent, matches) values (%s, %s, %s, %s)",
                (question, answer_html, intent, Json(matches))
            )
    except Exception as e:
        log.warning("DB log failed: %s", e)

# Параметры LLM
_executor = ThreadPoolExecutor(max_workers=4)
LLM_TIMEOUT_SEC = int(os.getenv("LLM_TIMEOUT_SEC", "28"))  # короткий таймаут против 504

SYSTEM_PROMPT = """
Ты — ИИ-юрист по законодательству Республики Казахстан. Формат ответа — строго HTML (p, ul/li, strong, h3, br). 
Никогда не используй Markdown, не выводи <html> и <body>.

Правила:
• Не перенаправляй пользователя к «юристу» и не советуй «обратиться к специалисту». Помогай здесь: шаги, формы, шаблоны.
• Если недостаточно данных — сначала дай общую законную схему и чёткий план, затем перечисли, какие сведения нужны (раздел «Что уточнить»).
• Если в базе законов нет точных совпадений — можно отвечать на основе общих принципов, но не «отказывай». 
• В конце не добавляй дисклеймеров «это не юридическая консультация».
• Пиши просто, короткими абзацами. Без воды.

Оформление:
1) <h3>Юридическая оценка</h3> + 1–2 абзаца сути.
2) <h3>Что делать пошагово</h3> + маркированный список.
3) <h3>Шаблоны/документы</h3> + либо структура, либо сгенерируй короткий текст внутри <pre class="code-block"><code>…</code></pre>.
4) <h3>Нормативные основания</h3> — только названия/статьи и ссылки (если есть).
5) <h3>Ответьте мне на следующие вопросы для точного разъяснения вашей ситуации: </h3> + пояснение: «Для качественного разъяснения вашей ситуации…» и 3–6 вопросов.
"""

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
            decoded = raw.decode("utf-8", errors="replace")
            log.info(f"📥 Декодированный запрос: {decoded}")  # Log raw decoded data
            payload = json.loads(decoded)
            if not isinstance(payload, dict):
                log.error(f"❌ Payload не является словарем: {payload}")
                dbg["json_error"] = "Payload is not a dictionary"
                payload = {}
        except Exception as e:
            log.error(f"❌ Ошибка парсинга JSON: {str(e)}")
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
    start_time = time.time()
    timeout = 30  # сек
    while not LAZY_INDEX.is_ready() and (time.time() - start_time) < timeout:
        time.sleep(1)
    
    llm_ready = bool(os.getenv("GEMINI_API_KEY"))
    index_ready = LAZY_INDEX.is_ready()
    laws_count = len(LAZY_INDEX.docs) if index_ready else 0
    
    return jsonify({
        "ok": index_ready,
        "laws_count": laws_count,
        "index_ready": index_ready,
        "llm": llm_ready,
        "message": "ready" if index_ready else "initializing"
    })

def _handle_ask():
    started = time.time()
    payload, dbg = get_json_payload()
    question = (payload.get("question") or "").strip()
    if not question:
        return json_error(400, "MISSING_FIELD", "Поле 'question' обязательно и не должно быть пустым.", dbg)

    log.info("👤 Вопрос: %s", question)

    # Проверяем готовность индекса
    if not LAZY_INDEX.is_ready():
        return json_error(503, "INDEX_NOT_READY", "Индекс законов ещё не готов. Попробуйте через несколько секунд.")

    hits, intent = search_laws(question, LAZY_INDEX.docs, LAZY_INDEX.index, top_k=5)
    log.info("🔎 Совпадений: %d | intent: %s", len(hits), intent['type'])

    # Веб-обогащение (если заданы ключи)
    web_sources: List[Dict] = []
    try:
        web_sources = web_enrich_official_sources(question, limit=3)
        if web_sources:
            log.info("🌐 Веб-источники: %d", len(web_sources))
    except Exception as e:
        log.warning("web_enrich_official_sources failed: %s", e)

    # LLM с таймаутом
    llm_html = ""
    try:
        fut = _executor.submit(call_llm, question, hits, intent, web_sources)
        llm_html = fut.result(timeout=LLM_TIMEOUT_SEC) or ""
    except TimeoutError:
        log.error("⏳ LLM timeout (%ss) — отдаём rule-based fallback", LLM_TIMEOUT_SEC)
    except Exception as e:
        log.exception("LLM fail: %s", e)

    # Финальная сборка + санитайзер
    answer_html = (llm_html.strip() or build_html_answer(question, hits, intent)).strip()
    answer_html = sanitize_html(answer_html)  # <-- главное исправление
    took = int((time.time() - started) * 1000)
    log.info("✅ Ответ готов (%d симв) за %d мс", len(answer_html), took)

    _log_qa(question, answer_html, intent, [
        {
            "article_title": r.get("article_title"),
            "law_title": r.get("law_title"),
            "source": r.get("source"),
            "score": s
        }
        for r, s in hits
    ])

    return jsonify({
        "ok": True,
        "answer_html": answer_html,
        "matches": [
            {
                "article_title": r.get("article_title"),
                "law_title": r.get("law_title"),
                "source": r.get("source"),
                "score": s
            }
            for r, s in hits
        ],
        "intent": intent,
        "took_ms": took
    })

#@cross_origin(origins=["http://127.0.0.1:5500", "http://localhost:5500"], supports_credentials=True)
@app.route("/api/ask", methods=["POST", "OPTIONS"])
@cross_origin(origins=["https://teg-ai-lawyer.netlify.app"])
def ask_question():
    if request.method == "OPTIONS":
        # Handle preflight request
        return ("", 204)
    
    try:
        data = request.get_json(silent=True)
        log.info(f"📥 Получен JSON payload: {data}")  # Log the payload
        return _handle_ask()  # Delegate to _handle_ask
    except Exception as e:
        log.error(f"❌ Произошла непредвиденная ошибка: {str(e)}", exc_info=True)
        return jsonify({
            "error": {
                "code": "INTERNAL_ERROR",
                "message": str(e)
            },
            "ok": False
        }), 500

@app.route("/", methods=["GET"])
def root_404():
    return make_response("Not Found", 404)

if __name__ == "__main__":
    port = int(os.getenv("PORT", "5000"))
    app.run(host="0.0.0.0", port=port)
