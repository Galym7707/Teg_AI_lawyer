# -*- coding: utf-8 -*-
import os
import json
import re
import logging
from typing import List, Dict, Tuple

from rank_bm25 import BM25Okapi

import google.generativeai as genai

log = logging.getLogger(__name__)

# ---------- Загрузка нормализованного корпуса ----------
def _read_jsonl(path: str) -> List[Dict]:
    items = []
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            items.append(json.loads(line))
    return items

def load_normalized(path: str) -> List[Dict]:
    if not os.path.isabs(path):
        base = os.path.dirname(os.path.abspath(__file__))
        path = os.path.join(base, path)
    if not os.path.exists(path):
        log.warning("normalized.jsonl не найден: %s", path)
        return []
    items = _read_jsonl(path)
    return items

# fallback — оригинальная база, если normalized нет
def load_laws(path: str) -> List[Dict]:
    if not os.path.isabs(path):
        base = os.path.dirname(os.path.abspath(__file__))
        path = os.path.join(base, path)
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)

# ---------- Токенизация ----------
WORD_RE = re.compile(r"[a-zA-Zа-яА-ЯёЁ0-9\-]+")

def _tok(s: str) -> List[str]:
    return WORD_RE.findall(s.lower())

# ---------- Индексация BM25 ----------
class LawIndex:
    def __init__(self, docs: List[Dict]):
        self.docs = docs
        # индекс строим по plain_summary если есть, иначе по plain_text
        corpus = []
        for d in docs:
            text = d.get("plain_summary") or d.get("plain_text") or ""
            corpus.append(_tok(text))
        self.bm25 = BM25Okapi(corpus)

    def search(self, query: str, top_k: int = 5) -> List[Tuple[Dict, float]]:
        q = _tok(query or "")
        if not q:
            return []
        scores = self.bm25.get_scores(q)
        idx_scores = sorted(enumerate(scores), key=lambda x: x[1], reverse=True)[:top_k]
        return [(self.docs[i], float(s)) for i, s in idx_scores if s > 0.0]

# ---------- Построение ответа (fallback) ----------
def build_html_answer(question: str, hits: List[Tuple[Dict, float]], intent: Dict) -> str:
    parts = [f"<h3>Юридическая оценка</h3>"]
    if hits:
        parts.append("<p>Релевантные нормы по вашему вопросу:</p>")
        parts.append("<ul>")
        for rec, score in hits:
            title = rec.get("article_title") or rec.get("law_title") or "Без названия"
            src = rec.get("source") or ""
            parts.append(f"<li><strong>{title}</strong>{' — ' + src if src else ''}</li>")
        parts.append("</ul>")
        # краткие тезисы (если есть plain_summary)
        bullets = []
        for rec, _ in hits[:2]:
            summ = (rec.get("plain_summary") or "").strip()
            if summ:
                bullets.append(summ)
        if bullets:
            parts.append("<h3>По сути</h3>")
            for b in bullets:
                parts.append(f"<p>{b}</p>")
    else:
        parts.append("<p>Прямых совпадений не найдено. Ниже — общий алгоритм по типовой практике РК.</p>")

    parts.append("<h3>Что делать пошагово</h3>")
    parts.append("<ul>"
                 "<li>Опишите ситуацию (даты, стороны, документы, статусы).</li>"
                 "<li>Подготовьте базовые документы (удостоверение личности, договоры, переписку).</li>"
                 "<li>Следуйте требованиям соответствующих статей, при необходимости оформите заявление/уведомление.</li>"
                 "</ul>")
    return "\n".join(parts)

# ---------- Поиск ----------
def init_index() -> Tuple[List[Dict], LawIndex]:
    norm = load_normalized("laws/normalized.jsonl")
    if norm:
        log.info("✅ Используем нормализованный корпус: %d фрагментов", len(norm))
        return norm, LawIndex(norm)
    # фоллбек
    raw = load_laws("laws/kazakh_laws.json")
    # приведём к унифицированному виду
    docs = []
    for x in raw:
        docs.append({
            "law_title": (x.get("title") or "").strip() or "Без названия",
            "article_title": (x.get("title") or "").strip(),
            "source": x.get("source"),
            "plain_text": (x.get("text") or "").strip()
        })
    log.info("⚠️ normalized.jsonl нет. Работаем по сырому корпусу: %d", len(docs))
    return docs, LawIndex(docs)

def search_laws(question: str, docs: List[Dict], index: LawIndex, top_k: int = 5):
    hits = index.search(question, top_k=top_k)
    intent = {"name": "generic"}
    return hits, intent

# ---------- ИИ ----------
def _init_llm():
    api_key = os.getenv("GEMINI_API_KEY")
    if not api_key:
        log.warning("GEMINI_API_KEY не задан — LLM отключён.")
        return None
    genai.configure(api_key=api_key)
    model_name = os.getenv("GEMINI_MODEL", "gemini-1.5-flash")
    model = genai.GenerativeModel(
        model_name=model_name,
        generation_config={"temperature": 0.25, "max_output_tokens": 1600},
        system_instruction=(
            "Ты — ИИ-юрист по законодательству РК. Отвечай строго в ЧИСТОМ HTML "
            "(<p>, <ul>, <ol>, <li>, <strong>, <em>, <h3>, <a>), без Markdown. "
            "Структура: "
            "<h3>Юридическая оценка</h3> коротко и предметно; "
            "<h3>Что делать пошагово</h3> 3–8 коротких шагов; "
            "<h3>Нормативные основания</h3> перечисли статьи/акты; "
            "<h3>Шаблоны/документы</h3> что подготовить. "
            "Не советуй «обратиться к юристу», дай сам практические шаги. "
            "Если точных норм нет — дай общий алгоритм (типовая практика Казахстана)."
        )
    )
    log.info("🤖 Gemini готов: %s", model_name)
    return model

_MODEL = _init_llm()

def call_llm(question: str, hits: List[Tuple[Dict, float]]) -> str:
    if _MODEL is None:
        return ""
    # компактный HTML-контекст
    ctx_parts = []
    for rec, _ in hits[:3]:
        t = rec.get("plain_summary") or rec.get("plain_text") or ""
        if len(t) > 1200:
            t = t[:1200] + "…"
        art = rec.get("article_title") or rec.get("law_title") or "Без названия"
        src = rec.get("source") or ""
        ctx_parts.append(f"<p><strong>{art}</strong>{(' — ' + src) if src else ''}</p><p>{t}</p>")
    ctx_html = "\n".join(ctx_parts) or "<p>Точных совпадений не найдено.</p>"

    prompt = (
        "<h3>Вопрос пользователя</h3>"
        f"<p>{question}</p>"
        "<h3>Релевантные выдержки</h3>"
        f"{ctx_html}"
        "<p>Собери итог только в чистом HTML.</p>"
    )
    try:
        r = _MODEL.generate_content(prompt)
        txt = (r.text or "").strip()
        # На всякий — если вернёт Markdown, подчищаем
        txt = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", txt)
        txt = txt.replace("\n", "<br>")
        return txt
    except Exception as e:
        log.exception("LLM error: %s", e)
        return ""
