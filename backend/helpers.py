# -*- coding: utf-8 -*-
import os
import json
import logging
import re
from typing import List, Dict, Tuple

import google.generativeai as genai

log = logging.getLogger(__name__)

# ---------- LAWS ----------
def load_laws(path: str) -> List[Dict]:
    if not os.path.isabs(path):
        base = os.path.dirname(os.path.abspath(__file__))
        path = os.path.join(base, path)
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)
    # ожидаем список элементов с title, text, source
    cleaned = []
    for item in data:
        t = (item.get("text") or "").strip()
        title = (item.get("title") or "").strip()
        source = item.get("source")
        if not t:
            continue
        cleaned.append({"title": title or "Без названия", "text": t, "source": source})
    return cleaned

def _tokenize(s: str) -> List[str]:
    return re.findall(r"[a-zA-Zа-яА-ЯёЁ0-9\-]+", s.lower())

def _score(query_tokens: List[str], text: str) -> float:
    if not text:
        return 0.0
    tt = _tokenize(text)
    if not tt:
        return 0.0
    qset = set(query_tokens)
    tset = set(tt)
    inter = len(qset & tset)
    return inter / (len(qset) + 1e-6)

def search_laws(question: str, laws: List[Dict], top_k: int = 3) -> Tuple[List[Tuple[Dict, float]], Dict]:
    qtokens = _tokenize(question)
    scored = []
    for art in laws:
        score = (
            _score(qtokens, art.get("title", "")) * 2.0 +
            _score(qtokens, art.get("text", "")) * 1.0
        )
        if score > 0:
            scored.append((art, score))
    scored.sort(key=lambda x: x[1], reverse=True)
    hits = scored[:top_k]
    intent = {"name": "generic"}
    return hits, intent

def build_html_answer(question: str, hits: List[Tuple[Dict, float]], intent: Dict) -> str:
    # простой fallback-ответ по совпадениям
    parts = [f"<h3>Юридическая оценка</h3>"]
    if hits:
        parts.append(f"<p>Ниже — нормы, которые релевантны вашему вопросу.</p>")
        parts.append("<ul>")
        for art, s in hits:
            title = art.get("title", "Без названия")
            src = art.get("source") or ""
            parts.append(f"<li><strong>{title}</strong>{' — ' + src if src else ''}</li>")
        parts.append("</ul>")
    else:
        parts.append("<p>По вашей формулировке прямых совпадений в базе не найдено. "
                     "Могу дать общий алгоритм действий и список документов.</p>")

    # минимальный практический блок
    parts.append("<h3>Что делать</h3>")
    parts.append("<ul>"
                 "<li>Сформулируйте цель и ситуацию в деталях (даты, стороны, документы).</li>"
                 "<li>Подготовьте базовые документы (удостоверение личности, договоры, переписку).</li>"
                 "<li>Следуйте пошаговым действиям, указанным в законодательстве и инструкциях госпорталов.</li>"
                 "</ul>")
    return "\n".join(parts)

# ---------- LLM ----------
def _init_llm():
    api_key = os.getenv("GEMINI_API_KEY")
    if not api_key:
        log.warning("GEMINI_API_KEY не задан — LLM будет отключён.")
        return None
    genai.configure(api_key=api_key)
    model_name = os.getenv("GEMINI_MODEL", "gemini-1.5-flash")
    model = genai.GenerativeModel(
        model_name=model_name,
        generation_config={"temperature": 0.3, "max_output_tokens": 2048},
        system_instruction=(
            "Ты — ИИ-юрист по законодательству Республики Казахстан. "
            "Отвечай строго в ЧИСТОМ HTML (без Markdown): <p>, <ul>, <ol>, <li>, <strong>, <em>, <h3>, <a>. "
            "Структура ответа: "
            "1) <h3>Юридическая оценка</h3> — коротко и по делу. "
            "2) <h3>Что делать пошагово</h3> — 3–7 чётких шагов. "
            "3) <h3>Нормативные основания</h3> — конкретные статьи/акты, если уместно. "
            "4) <h3>Шаблоны/документы</h3> — перечисли, что подготовить. "
            "Не пиши «обратитесь к юристу», дай сам максимально практические советы. "
            "Если в переданных фрагментах нет точных норм — дай общий, но прикладной алгоритм по стандартной практике Казахстана."
        )
    )
    log.info("🤖 Gemini инициализирован: %s", model_name)
    return model

_MODEL = _init_llm()

def call_llm(question: str, hits: List[Tuple[Dict, float]]) -> str:
    if _MODEL is None:
        return ""
    # компактный контекст
    ctx = []
    for art, score in hits[:3]:
        title = art.get("title", "")
        src = art.get("source") or ""
        frag = art.get("text", "")
        if len(frag) > 1200:
            frag = frag[:1200] + "…"
        ctx.append(f"<p><strong>{title}</strong> ({src})</p><p>{frag}</p>")
    context_html = "\n".join(ctx)

    prompt = (
        "<h3>Вопрос пользователя</h3>"
        f"<p>{question}</p>"
        "<h3>Релевантные фрагменты (если есть)</h3>"
        f"{context_html or '<p>Нет точных совпадений в базе фрагментов.</p>'}"
        "<p>Сформируй итоговый ответ строго в HTML (без Markdown), по структуре из системной инструкции.</p>"
    )

    resp = _MODEL.generate_content(prompt)
    if not getattr(resp, "text", None):
        return ""
    # Google может вернуть Markdown — в системке уже попросили HTML, но на всякий случай:
    txt = resp.text.strip()
    # простая очистка **...** -> <strong>…</strong>
    txt = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", txt)
    txt = txt.replace("\n", "<br>")
    return txt
