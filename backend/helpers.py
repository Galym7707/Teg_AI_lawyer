# -*- coding: utf-8 -*-
import os
import re
import json
import logging
from typing import List, Dict, Tuple, Optional

import google.generativeai as genai
from rank_bm25 import BM25Okapi
import requests  # опционально для web-обогащения

log = logging.getLogger(__name__)

# =========================
# HTML санитайзер
# =========================
def sanitize_html(html: str) -> str:
    """
    Чистим разметку и вырезаем любые рекомендации «обратиться к юристу/адвокату/специалисту».
    Также схлопываем лишние переносы и пустые элементы.
    """
    import re
    if not html:
        return ""

    h = html

    # Нормализация переносов и пустых параграфов
    h = re.sub(r'(\r\n|\r)', '\n', h)
    h = re.sub(r'\n{3,}', '\n\n', h)
    h = re.sub(r'(<p>\s*</p>)+', '', h, flags=re.I)
    h = re.sub(r'(<br\s*/?>\s*){2,}', '<br>', h, flags=re.I)
    h = re.sub(r'<li>\s*<p>(.*?)</p>\s*</li>', r'<li>\1</li>', h, flags=re.I | re.S)
    h = re.sub(r'>\s+<', '><', h)

    # БАН: «обратитесь/рекомендуется/советуем … к юристу/адвокату/специалисту»
    banned = [
        r'проконсультируйтесь\s+с\s+юрист[а-яё]+',
        r'обратит[её]сь\s+к\s+(квалифицированн(ому|ым)\s+)?юрист[а-яё]+',
        r'обратит[её]сь\s+к\s+адвокат[а-яё]+',
        r'обратит[её]сь\s+к\s+специалист[а-яё]+',
        r'(рекомендуется|советую|советуем)\s+обратит[её]сь\s+к\s+(квалифицированн(ому|ым)\s+)?юрист[а-яё]+',
        r'получить\s+консультаци(ю|и)\s+юрист[а-яё]+',
        r'для\s+составления\s+исков(ого|ых)\s+заявлени[яй]\s+.*(рекомендуется|лучше)\s+обратит[её]сь\s+к\s+юрист[а-яё]+',
        r'образец\s+искового\s+заявления\s+(найти\s+сложно|можно\s+найти\s+в\s+интернет[еа])',
    ]
    replacement_main = (
        "Если потребуется судебная защита, подготовьте исковое заявление по требованиям ГПК РК. "
        "Ниже дан базовый каркас; адаптируйте под свою ситуацию."
    )
    for pat in banned:
        h = re.sub(pat, replacement_main, h, flags=re.I)

    # Ещё один мягкий синоним
    h = re.sub(r'получить\s+юридическ(ую|ие)\s+консультац(ию|ии)[^.<]*\.', replacement_main + '.', h, flags=re.I)

    return h.strip()

# =========================
# Загрузка корпуса
# =========================
_WORD_RE = re.compile(r"[a-zA-Zа-яА-ЯёЁ0-9\-]+")

def _tok(s: str) -> List[str]:
    return _WORD_RE.findall((s or "").lower())

def _read_jsonl(path: str) -> List[Dict]:
    items = []
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            items.append(json.loads(line))
    return items

def load_laws_json(path: str) -> List[Dict]:
    if not os.path.isabs(path):
        base = os.path.dirname(os.path.abspath(__file__))
        path = os.path.join(base, path)
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)

def load_normalized_or_fallback() -> List[Dict]:
    """
    Пытаемся взять laws/normalized.jsonl (нормализованные фрагменты).
    Если файла нет — читаем сырой kazakh_laws.json.
    """
    base = os.path.dirname(os.path.abspath(__file__))
    norm_path = os.path.join(base, "laws", "normalized.jsonl")
    if os.path.exists(norm_path):
        docs = _read_jsonl(norm_path)
        log.info("✅ Используем normalized.jsonl: %d фрагментов", len(docs))
        return docs

    raw_path = os.path.join(base, "laws", "kazakh_laws.json")
    raw = load_laws_json(raw_path)
    docs = []
    for x in raw:
        docs.append({
            "law_title": (x.get("title") or "").strip() or "Без названия",
            "article_title": (x.get("title") or "").strip(),
            "source": x.get("source"),
            "plain_text": (x.get("text") or "").strip()
        })
    log.warning("⚠️ normalized.jsonl не найден — работаем по сырому корпусу (%d записей)", len(docs))
    return docs

# =========================
# Индекс BM25
# =========================
class LawIndex:
    def __init__(self, docs: List[Dict]):
        self.docs = docs
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

def init_index() -> Tuple[List[Dict], LawIndex]:
    docs = load_normalized_or_fallback()
    return docs, LawIndex(docs)

# =========================
# Детекция намерений
# =========================
_INTENT_PATTERNS = [
    ("resignation", r"\b(уволить|увольня|уволиться|расторгнуть\s+труд|заявлени[ея]\s+об\s+уволн)\b"),
    ("register_ip", r"\b(открыть\s+ип|регистрац(ия|ии)\s+ип|как\s+оформить\s+ип|индивидуальн(ый|ого)\s+предпринимател[ья])\b"),
    ("sick_leave", r"\b(больничн(ый|ого)|лист\s+нетрудоспособности)\b"),
    ("maternity", r"\b(декрет|по\s+уходу\s+за\s+ребенком|рожден)\b"),
    ("vacation", r"\b(отпуск(а|ные)?|неиспользованн(ый|ые)\s+отпуск)\b"),
]

def detect_intent(question: str) -> str:
    q = (question or "").lower()
    for name, pat in _INTENT_PATTERNS:
        if re.search(pat, q):
            return name
    return "generic"

# =========================
# Шаблоны документов
# =========================
def _template_resignation() -> str:
    return (
        "<h3>Шаблон заявления об увольнении по собственному желанию</h3>"
        "<pre>"
        "Руководителю ________________________________\n"
        "(наименование организации)\n\n"
        "от _________________________________________\n"
        "(ФИО работника, должность)\n\n"
        "ЗАЯВЛЕНИЕ\n\n"
        "Прошу уволить меня по собственному желанию с ______________ 20__ г.\n"
        "Трудовой договор от __.__.20__ № ____ прошу расторгнуть на основании\n"
        "трудового законодательства.\n\n"
        "С порядком расчёта и передачей трудовых документов ознакомлен(а).\n\n"
        "«___»__________20__ г.          _______________/____________/\n"
        "                                 (подпись)       (ФИО)\n"
        "</pre>"
    )

def _template_register_ip() -> str:
    return (
        "<h3>Шаблон перечня данных для регистрации ИП</h3>"
        "<ul>"
        "<li><strong>ИИН</strong>, ФИО, адрес регистрации.</li>"
        "<li><strong>Вид деятельности</strong> (ОКЭД).</li>"
        "<li><strong>Режим налогообложения</strong> (упрощённый/патент/проч.).</li>"
        "<li><strong>Контакты</strong> (телефон, email).</li>"
        "<li><strong>Банковские реквизиты</strong> (после открытия счёта).</li>"
        "</ul>"
        "<p>Подача заявления: портал eGov или ЦОН. Срок: обычно 1 рабочий день.</p>"
    )

def template_for_intent(intent: str) -> str:
    if intent == "resignation":
        return _template_resignation()
    if intent == "register_ip":
        return _template_register_ip()
    return ""

# =========================
# Веб-обогащение (опционально)
# =========================
def web_enrich_official_sources(query: str, limit: int = 3) -> List[Dict]:
    """
    Если есть SERPAPI_KEY (или GOOGLE_API_KEY + GOOGLE_CSE_ID), подтягиваем 1–3 ссылки
    с adilet.zan.kz / egov.kz / gov.kz. Если ключей нет — возвращаем [] без ошибок.
    """
    res: List[Dict] = []

    serp_key = os.getenv("SERPAPI_KEY")
    if serp_key:
        try:
            q = f"site:adilet.zan.kz OR site:egov.kz OR site:gov.kz {query}"
            r = requests.get(
                "https://serpapi.com/search.json",
                params={"engine": "google", "q": q, "num": limit, "hl": "ru", "gl": "kz", "api_key": serp_key},
                timeout=8,
            )
            j = r.json()
            for it in (j.get("organic_results") or [])[:limit]:
                res.append({"title": it.get("title"), "link": it.get("link"), "snippet": it.get("snippet")})
            return res
        except Exception as e:
            log.warning("SERPAPI failed: %s", e)

    g_key = os.getenv("GOOGLE_API_KEY")
    cse_id = os.getenv("GOOGLE_CSE_ID")
    if g_key and cse_id:
        try:
            q = f"{query} site:adilet.zan.kz OR site:egov.kz OR site:gov.kz"
            r = requests.get(
                "https://www.googleapis.com/customsearch/v1",
                params={"key": g_key, "cx": cse_id, "q": q, "num": limit, "hl": "ru"},
                timeout=8,
            )
            j = r.json()
            for it in (j.get("items") or [])[:limit]:
                res.append({"title": it.get("title"), "link": it.get("link"), "snippet": it.get("snippet")})
            return res
        except Exception as e:
            log.warning("Google CSE failed: %s", e)

    return res

# =========================
# Rule-based HTML (фоллбек)
# =========================
def build_html_answer(question: str,
                      hits: List[Tuple[Dict, float]],
                      intent: str,
                      web_sources: Optional[List[Dict]] = None) -> str:
    parts: List[str] = []
    parts.append("<h3>Юридическая оценка</h3>")

    if hits:
        parts.append("<p>Ниже — выдержки и рекомендации, релевантные вашему вопросу.</p>")
    else:
        parts.append("<p>Точных совпадений в базе не найдено. Привожу практический алгоритм по типовой практике РК.</p>")

    if hits:
        parts.append("<ul>")
        for rec, _ in hits[:5]:
            t = rec.get("article_title") or rec.get("law_title") or "Без названия"
            src = rec.get("source") or ""
            if src:
                parts.append(f'<li><strong>{t}</strong> — <a href="{src}" target="_blank">{src}</a></li>')
            else:
                parts.append(f"<li><strong>{t}</strong></li>")
        parts.append("</ul>")

    parts.append("<h3>Что делать пошагово</h3>")
    if intent == "resignation":
        parts.append(
            "<ul>"
            "<li>Подготовьте заявление об увольнении с указанной датой.</li>"
            "<li>Передайте работодателю под подпись / в канцелярию — получите отметку о приёме.</li>"
            "<li>Отработайте срок предупреждения (обычно 14 календарных дней, если не согласовано иное).</li>"
            "<li>В день увольнения получите расчёт и трудовые документы.</li>"
            "</ul>"
        )
    elif intent == "register_ip":
        parts.append(
            "<ul>"
            "<li>Определите вид деятельности (ОКЭД) и режим налогообложения.</li>"
            "<li>Подайте уведомление о начале деятельности через портал eGov или в ЦОН.</li>"
            "<li>Откройте счёт в банке, при необходимости зарегистрируйте онлайн-ККМ.</li>"
            "</ul>"
        )
    else:
        parts.append(
            "<ul>"
            "<li>Соберите факты и документы (даты, участники, переписка, договоры).</li>"
            "<li>Найдите применимые нормы и оформите заявление/ходатайство по требованиям закона.</li>"
            "<li>Соблюдайте сроки и порядок подачи.</li>"
            "</ul>"
        )

    # Блок «Что уточнить» (если вопрос общий)
    if intent == "generic":
        parts.append(
            "<h3>Что уточнить</h3>"
            "<ul>"
            "<li>Кто участники и какие отношения (договор, трудовые, семейные и т. п.).</li>"
            "<li>Ключевые даты и документы.</li>"
            "<li>Какая цель: расторгнуть, взыскать, обжаловать и пр.</li>"
            "</ul>"
        )

    tpl = template_for_intent(intent)
    if tpl:
        parts.append(tpl)

    if web_sources:
        parts.append("<h3>Официальные источники</h3><ul>")
        for s in web_sources:
            title = s.get("title") or s.get("link") or "Источник"
            link = s.get("link") or "#"
            parts.append(f'<li><a href="{link}" target="_blank">{title}</a></li>')
        parts.append("</ul>")

    return sanitize_html("\n".join(parts))

# =========================
# LLM (Gemini)
# =========================
def _init_llm():
    api_key = os.getenv("GEMINI_API_KEY")
    if not api_key:
        log.warning("GEMINI_API_KEY не задан — LLM отключён.")
        return None
    try:
        genai.configure(api_key=api_key)
    except Exception as e:
        log.warning("Gemini configure failed: %s", e)
        return None

    model_name = os.getenv("GEMINI_MODEL", "gemini-1.5-flash")
    model = genai.GenerativeModel(
        model_name,
        generation_config={
            "temperature": float(os.getenv("LLM_TEMPERATURE", "0.1")),
            "max_output_tokens": int(os.getenv("LLM_MAX_TOKENS", "1400")),
        },
        system_instruction=(
            "Ты — ИИ-юрист по законодательству Республики Казахстан. "
            "Всегда отвечай СТРОГО в ЧИСТОМ HTML (<p>, <ul>, <ol>, <li>, <strong>, <em>, <h3>, <a>), без Markdown.\n"
            "ЗАПРЕЩЕНО использовать формулировки вроде «обратитесь к юристу/адвокату/специалисту» "
            "или «найдите образец в интернете». Вместо этого дай конкретные шаги, ссылки на нормы и готовые шаблоны.\n"
            "Структура ответа:\n"
            "1) <h3>Юридическая оценка</h3> — по сути, кратко.\n"
            "2) <h3>Что делать пошагово</h3> — 3–8 коротких действий.\n"
            "3) <h3>Нормативные основания</h3> — перечисли акты/статьи, если они есть в контексте.\n"
            "4) <h3>Шаблоны/документы</h3> — при необходимости СГЕНЕРИРУЙ полноценный шаблон в <pre>…</pre>.\n"
            "5) Если данных недостаточно, добавь блок <h3>Что уточнить</h3> со списком конкретных вопросов.\n"
        )
    )
    log.info("🤖 Gemini готов: %s", model_name)
    return model

_MODEL = _init_llm()

def call_llm(question: str,
             hits: List[Tuple[Dict, float]],
             intent: str,
             web_sources: Optional[List[Dict]] = None) -> str:
    if _MODEL is None:
        return ""

    # Компактный HTML-контекст из корпуса
    ctx_parts: List[str] = []
    for rec, _ in hits[:3]:
        art = rec.get("article_title") or rec.get("law_title") or "Без названия"
        src = rec.get("source") or ""
        txt = (rec.get("plain_summary") or rec.get("plain_text") or "").strip()
        if len(txt) > 1200:
            txt = txt[:1200] + "…"
        ctx_parts.append(f"<p><strong>{art}</strong>{(' — ' + src) if src else ''}</p><p>{txt}</p>")

    # Подсказка под намерение
    template_hint = ""
    if intent == "resignation":
        template_hint = (
            "<p>Если вопрос про увольнение — ОБЯЗАТЕЛЬНО включи полный шаблон заявления об увольнении в <pre>…</pre>.</p>"
        )
    elif intent == "register_ip":
        template_hint = (
            "<p>Если вопрос про регистрацию ИП — дай чек-лист и шаблон перечня данных для подачи через eGov.</p>"
        )

    source_html = ""
    if web_sources:
        items = []
        for s in web_sources[:3]:
            title = s.get("title") or s.get("link")
            link = s.get("link")
            items.append(f'<li><a href="{link}" target="_blank">{title}</a></li>')
        if items:
            source_html = "<h3>Официальные источники (для справки)</h3><ul>" + "".join(items) + "</ul>"

    prompt = (
        "<h3>Вопрос пользователя</h3>"
        f"<p>{question}</p>"
        "<h3>Релевантные выдержки</h3>"
        + ("\n".join(ctx_parts) if ctx_parts else "<p>Точных совпадений не найдено.</p>")
        + template_hint
        + source_html
        + "<p>Собери финальный ответ строго в ЧИСТОМ HTML без Markdown.</p>"
    )

    try:
        r = _MODEL.generate_content(prompt)
        txt = (r.text or "").strip()
        # На всякий случай заменим **...** → <strong>…</strong>
        txt = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", txt)
        txt = txt.replace("\n", "<br>")
        return sanitize_html(txt)
    except Exception as e:
        log.exception("LLM error: %s", e)
        return ""
    
# =========================
# Поиск
# =========================
def search_laws(question: str, docs: List[Dict], index: LawIndex, top_k: int = 5):
    hits = index.search(question, top_k=top_k)
    intent = detect_intent(question)
    return hits, intent
