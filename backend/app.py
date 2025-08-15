# -*- coding: utf-8 -*-
"""
Flask-бэкенд для Teg AI Lawyer (минимум зависимостей, согласованный API).
Маршруты:
  GET  /api/health
  POST /api/ask    -> принимает JSON { question, session_id? }, отдаёт JSON { html, used_articles }
"""

from __future__ import annotations
import os
import json
import logging
from typing import Dict, Any

from flask import Flask, request, jsonify, make_response
from flask_cors import CORS

# Законы и поиск
from helpers import load_laws, LawIndex, laws_to_html_context, escape_html

# Gemini (google-generativeai)
import google.generativeai as genai


# --------------------------- Логгер ---------------------------

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
)
log = logging.getLogger(__name__)


# --------------------------- Конфигурация ---------------------------

GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "").strip()
LAWS_PATH = os.getenv("LAWS_PATH", "laws/kazakh_laws.json").strip()

# CORS: разрешаем Netlify/Vercel домен или всё (по необходимости)
_front_origin = os.getenv("FRONT_ORIGIN", "").strip()
if not _front_origin:
    # можно перечислить несколько через запятую
    _front_origin = os.getenv("CORS_ORIGINS", "https://teg-ai-lawyer.netlify.app").split(",")[0].strip()

app = Flask(__name__)
CORS(app, resources={r"/api/*": {"origins": _front_origin}},
     supports_credentials=True)

# --------------------------- Загрузка законов и инициализация модели ---------------------------

try:
    log.info("Загрузка базы законов…")
    LAWS = load_laws(LAWS_PATH)
    INDEX = LawIndex(LAWS)
    log.info("✅ Индекс законов готов: %d статей", len(LAWS))
except Exception as e:
    log.exception("❌ Не удалось загрузить базу законов: %s", e)
    LAWS, INDEX = [], None

MODEL = None
if GEMINI_API_KEY:
    try:
        genai.configure(api_key=GEMINI_API_KEY)
        MODEL = genai.GenerativeModel("gemini-1.5-flash")
    except Exception as e:
        log.exception("❌ Не удалось инициализировать модель: %s", e)
else:
    log.warning("⚠️ GEMINI_API_KEY не задан — ответы модели недоступны")


# --------------------------- Системная инструкция ---------------------------

def build_system_instruction(law_context_html: str) -> str:
    """
    Жёстко заставляем модель отвечать ТОЛЬКО в HTML и по казахстанскому праву.
    """
    return (
        "<p><strong>Роль:</strong> Ты — полноценный и официальный ИИ-юрист по законодательству исключительно Республики Казахстан.</p>"
        "<p><strong>Задача:</strong> Дай чёткий ответ на вопрос пользователя, основываясь на законах РК.</p>"
        "<p><strong>Правила:</strong></p>"
        "<ul>"
        "<li>Дай чёткую юридическую оценку: какие нормы применимы, есть ли нарушение, какая ответственность.</li>"
        "<li>Сразу после оценки — что делать: шаги, куда идти/писать, какие документы приложить.</li>"
        "<li>Дай практические и реальные советы. Также типичные ошибки и как их избежать</li>"
        "<li><strong>Строго запрещён Markdown</strong>; используем только HTML-теги: "
        "&lt;p&gt;, &lt;ul&gt;&lt;li&gt;, &lt;strong&gt;, &lt;em&gt;, &lt;h3&gt;.</li>"
        "<li>Если уверенных оснований из законов нет — скажи об этом и предложи уточняющие вопросы, но всё равно дай базовый общий алгоритм действий, и попытайся хоть-как то помочь</li>"
        "</ul>"
        f"<h3>Контекст из базы законов (фрагменты):</h3>{law_context_html or '<p><em>Подходящих фрагментов не найдено.</em></p>'}"
    )


# --------------------------- Маршруты ---------------------------

@app.route("/api/health", methods=["GET"])
def api_health():
    return jsonify({
        "ok": True,
        "laws_loaded": len(LAWS),
        "model_ready": bool(MODEL),
    })


def _get_json_body() -> Dict[str, Any]:
    """
    Принимаем JSON, а если пришло form-data/текст — пытаемся вытянуть question оттуда,
    чтобы не падать на фронтовых мелочах.
    """
    if request.is_json:
        try:
            return request.get_json(silent=True) or {}
        except Exception:
            return {}
    # form-data
    if request.form:
        return {"question": request.form.get("question", ""), "session_id": request.form.get("session_id", "")}
    # сырой текст (на всякий случай)
    data = request.get_data(as_text=True) or ""
    if data.strip():
        return {"question": data.strip()}
    return {}


@app.route("/api/ask", methods=["POST"])
def api_ask():
    if INDEX is None:
        return jsonify({"error": "Законодательная база не загружена"}), 503

    body = _get_json_body()
    question = (body.get("question") or "").strip()
    session_id = (body.get("session_id") or "").strip()

    if not question:
        return jsonify({"error": "Поле 'question' обязательно"}), 400

    log.info("Вопрос (%s): %s", session_id or "-", question)

    # Поиск подходящих статей
    results = INDEX.search(question, top_k=5, min_score=1.0)
    context_html, used_articles = laws_to_html_context(results)

    sys_prompt = build_system_instruction(context_html)

    if not MODEL:
        # Без модели всё равно вернём понятный HTML-ответ (off-model режим)
        fallback = (
            "<p><strong>Извините, модель временно недоступна.</strong></p>"
            "<p>Ниже — фрагменты из релевантных законов, на основании которых вы можете ориентироваться:</p>"
            f"{context_html or '<p>Подходящих фрагментов не найдено.</p>'}"
        )
        return jsonify({"html": fallback, "used_articles": used_articles})

    try:
        # Собираем один HTML-запрос для модели: инструкция + вопрос.
        prompt_html = (
            f"{sys_prompt}"
            f"<h3>Вопрос пользователя</h3>"
            f"<p>{escape_html(question)}</p>"
            "<p><em>Ответь строго в чистом HTML (без markdown), структурируй разделами и списками.</em></p>"
        )

        resp = MODEL.generate_content(prompt_html)
        # API иногда отдаёт неочевидные структуры; берём .text
        answer = (getattr(resp, "text", None) or "").strip()
        if not answer:
            # в редких случаях текст в candidates
            try:
                cands = getattr(resp, "candidates", []) or []
                for c in cands:
                    parts = c.get("content", {}).get("parts") or []
                    for p in parts:
                        if "text" in p and p["text"].strip():
                            answer = p["text"].strip()
                            break
                    if answer:
                        break
            except Exception:
                pass

        if not answer:
            answer = "<p>Извините, не удалось сформировать ответ. Попробуйте переформулировать вопрос.</p>"

        # Мини-гигиена: если модель вдруг прислала markdown, не трогаем (фронт покажет как есть),
        # но просили HTML — в большинстве случаев модель соблюдает.
        return jsonify({"html": answer, "used_articles": used_articles})

    except Exception as e:
        msg = str(e)
        log.exception("❌ Ошибка генерации: %s", msg)
        if "503" in msg or "overloaded" in msg.lower():
            return jsonify({"error": "503 The model is overloaded. Please try again later."}), 503
        return jsonify({"error": "Internal error while generating the answer."}), 500


# --------------------------- Точка входа ---------------------------

if __name__ == "__main__":
    port = int(os.getenv("PORT", "5000"))
    app.run(host="0.0.0.0", port=port)
