# -*- coding: utf-8 -*-
import os
import re
import json
import logging
import html
import time
from typing import List, Dict, Tuple, Optional

import google.generativeai as genai
from rank_bm25 import BM25Okapi
import requests  # опционально для web-обогащения
import bleach

log = logging.getLogger(__name__)

# =========================
# HTML санитайзер
# =========================

FORBIDDEN_REFERRALS = [
    r"обратит[ьс][яе][ ]+к[ ]+квалифицированн?[оы]?[мй]?[ ]+юрист[ауеом]",
    r"обратитесь[ ]+к[ ]+юрист[ауеом]",
    r"рекомендуется[ ]+обратиться[ ]+к[ ]+юрист[ауеом]",
    r"лучше[ ]+обратиться[ ]+к[ ]+юрист[ауеом]",
    r"необходимо[ ]+обратиться[ ]+к[ ]+юрист[ауеом]",
]

# какие теги разрешаем рендерить как HTML (остальное экранируется)
_ALLOWED_TAGS = [
    "p", "ul", "ol", "li", "strong", "em", "br", "h3", "h4", "blockquote",
    "pre", "code", "hr", "span", "small", "a"
]
_ALLOWED_ATTRS = {
    "span": ["class"],
    "pre": ["class"],
    "code": ["class"],
    "a": ["href", "target", "rel"]
}

def enforce_rules(html: str) -> str:
    """Убираем «идите к юристу», чистим мусор, нормализуем отступы."""
    text = html

    # 1) вырезаем любые намёки «идите к юристу»
    for rx in FORBIDDEN_REFERRALS:
        text = re.sub(rx, "я помогу подготовить всё здесь, в этом чате", text, flags=re.I)

    # 2) если вдруг LLM прислал оболочку <html>/<body> — просто выбрасываем её
    text = re.sub(r"</?(?:html|head|body)[^>]*>", "", text, flags=re.I)

    # 3) убираем лишние пустые абзацы/переводы строк
    text = re.sub(r"(\s*<br\s*/?>\s*){3,}", "<br>", text, flags=re.I)
    text = re.sub(r"\n{3,}", "\n\n", text)

    # 4) убрать пустые параграфы
    text = re.sub(r"<p>\s*(?:&nbsp;)?\s*</p>", "", text, flags=re.I)
    # сжать подряд идущие пустые параграфы
    text = re.sub(r"(?:<p>\s*</p>){2,}", "", text, flags=re.I)

    return text.strip()

def sanitize_html(html: str) -> str:
    return bleach.clean(
        html,
        tags=_ALLOWED_TAGS,
        attributes=_ALLOWED_ATTRS,
        strip=True
    )

def postprocess_html(html: str) -> str:
    """Последовательность: правила -> sanitize -> финальный легкий рефайн."""
    html = enforce_rules(html)
    html = sanitize_html(html)
    # финальная полировка пробелов
    html = re.sub(r"\s+</li>", "</li>", html)
    html = re.sub(r"\s+</p>", "</p>", html)
    return html

def build_html_answer(question: str, hits, intent: dict) -> str:
    """
    Рендерим итоговый HTML-ответ. Тут же:
    - не вставляем <html>/<body>;
    - даём аккуратный «шаблон/структуру» без сырого HTML;
    - добавляем явное пояснение к «Что уточнить».
    """
    # краткое введение (можешь подменить на своё)
    intro = (
        "<h3>Юридическая оценка</h3>"
        "<p>Ниже я даю практические шаги и заготовки документов по вашему запросу. "
        "Если потребуется — я уточню детали и помогу адаптировать формулировки здесь, без направлений к третьим лицам.</p>"
    )

    # «Что делать» всегда есть
    steps = [
        "Кратко зафиксируйте, что произошло и чего вы хотите добиться (результат).",
        "Подготовьте и подайте документ по ситуации (заявление/претензия/исковое — подскажу ниже).",
        "Соберите подтверждения: переписка, акты, фото/видео, свидетельские показания — всё храните копиями.",
        "Отслеживайте сроки (на обжалование, уведомление и т.д.) — при необходимости напомню конкретные нормы.",
    ]
    steps_html = "<h3>Что делать пошагово</h3><ul>" + "".join(f"<li>{s}</li>" for s in steps) + "</ul>"

    # аккуратный «шаблон»: структура, а не сырой HTML
    template_html = """
<h3>Шаблоны/документы</h3>
<p><strong>Быстрая структура документа (адаптируйте под вашу ситуацию):</strong></p>
<ul>
  <li>«Шапка» адресата (куда подаёте) и ваши данные.</li>
  <li>Краткое и чёткое описание ситуации (факты по датам).</li>
  <li>Нормативные основания (перечень статей/норм, на которые ссылаетесь).</li>
  <li>Ваши требования (что просите сделать, в какие сроки).</li>
  <li>Список приложений (доказательства, копии документов).</li>
  <li>Дата и подпись.</li>
</ul>
<p class="muted">Нужно — сгенерирую готовый текст прямо здесь по вашим исходным данным.</p>
""".strip()

    # Если есть совпадения по базе — покажем ссылки/названия (без сырого текста закона)
    laws_block = ""
    if hits:
        items = []
        for art, score in hits:
            t = bleach.clean(art.get("title", ""), strip=True)
            src = bleach.clean(art.get("source", ""), strip=True)
            if t:
                if src:
                    items.append(f"<li>{t} — <a href=\"{src}\" target=\"_blank\" rel=\"noopener\">источник</a></li>")
                else:
                    items.append(f"<li>{t}</li>")
        if items:
            laws_block = "<h3>Нормативные основания</h3><ul>" + "".join(items) + "</ul>"

    # «Что уточнить» — теперь с явным пояснением ЗАЧЕМ
    clarify_intro = (
        "<h3>Что уточнить</h3>"
        "<p class=\"muted\">Для качественного разъяснения вашей ситуации ответьте, пожалуйста, на несколько вопросов:</p>"
    )
    clarify_points = intent.get("clarify_points") or []
    if not clarify_points:
        # базовый набор, если модель не прислала свои
        clarify_points = [
            "Какова официальная причина/формулировка в документах?",
            "Какие даты и участники ключевых действий?",
            "Что вы уже предпринимали и какие есть ответы/отказы?",
            "Какие доказательства у вас на руках?",
        ]
    clarify_html = clarify_intro + "<ul>" + "".join(f"<li>{bleach.clean(p, strip=True)}</li>" for p in clarify_points) + "</ul>"

    html = intro + steps_html + template_html + laws_block + clarify_html
    return postprocess_html(html)

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
    start = time.time()
    base = os.path.dirname(os.path.abspath(__file__))
    norm_path = os.path.join(base, "laws", "normalized.jsonl")
    if os.path.exists(norm_path):
        docs = _read_jsonl(norm_path)
        log.info("✅ Используем normalized.jsonl: %d фрагментов", len(docs))
        log.info(f"✅ Индекс загружен за {time.time()-start:.2f} сек")
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
    log.info(f"✅ Индекс загружен за {time.time()-start:.2f} сек")
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
    try:
        laws_path = os.path.join(os.path.dirname(__file__), "laws", "kazakh_laws.json")
        if not os.path.exists(laws_path):
            raise FileNotFoundError(f"Файл законов не найден: {laws_path}")
        docs = load_normalized_or_fallback()
        return docs, LawIndex(docs)
    except Exception as e:
        log.error(f"❌ Critical: {str(e)}")
        raise

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
        # Корректное экранирование HTML и обработка переносов строк
        txt = html.escape(txt)
        txt = txt.replace("&lt;br&gt;", "<br>")  # Восстанавливаем <br> после экранирования
        txt = txt.replace("&lt;/p&gt;", "</p>")  # Восстанавливаем </p> после экранирования
        txt = txt.replace("&lt;p&gt;", "<p>")    # Восстанавливаем <p> после экранирования
        txt = txt.replace("&lt;strong&gt;", "<strong>")  # Восстанавливаем <strong> после экранирования
        txt = txt.replace("&lt;/strong&gt;", "</strong>")  # Восстанавливаем </strong> после экранирования
        txt = txt.replace("&lt;ul&gt;", "<ul>")  # Восстанавливаем <ul> после экранирования
        txt = txt.replace("&lt;/ul&gt;", "</ul>")  # Восстанавливаем </ul> после экранирования
        txt = txt.replace("&lt;li&gt;", "<li>")  # Восстанавливаем <li> после экранирования
        txt = txt.replace("&lt;/li&gt;", "</li>")  # Восстанавливаем </li> после экранирования
        txt = txt.replace("&lt;h3&gt;", "<h3>")  # Восстанавливаем <h3> после экранирования
        txt = txt.replace("&lt;/h3&gt;", "</h3>")  # Восстанавливаем </h3> после экранирования
        txt = txt.replace("&lt;pre&gt;", "<pre>")  # Восстанавливаем <pre> после экранирования
        txt = txt.replace("&lt;/pre&gt;", "</pre>")  # Восстанавливаем </pre> после экранирования
        txt = txt.replace("&lt;a&gt;", "<a>")  # Восстанавливаем <a> после экранирования
        txt = txt.replace("&lt;/a&gt;", "</a>")  # Восстанавливаем </a> после экранирования
        txt = txt.replace("&lt;em&gt;", "<em>")  # Восстанавливаем <em> после экранирования
        txt = txt.replace("&lt;/em&gt;", "</em>")  # Восстанавливаем </em> после экранирования
        txt = txt.replace("&lt;ol&gt;", "<ol>")  # Восстанавливаем <ol> после экранирования
        txt = txt.replace("&lt;/ol&gt;", "</ol>")  # Восстанавливаем </ol> после экранирования
        txt = txt.replace("&lt;blockquote&gt;", "<blockquote>")  # Восстанавливаем <blockquote> после экранирования
        txt = txt.replace("&lt;/blockquote&gt;", "</blockquote>")  # Восстанавливаем </blockquote> после экранирования
        txt = txt.replace("&lt;code&gt;", "<code>")  # Восстанавливаем <code> после экранирования
        txt = txt.replace("&lt;/code&gt;", "</code>")  # Восстанавливаем </code> после экранирования
        txt = txt.replace("&lt;hr&gt;", "<hr>")  # Восстанавливаем <hr> после экранирования
        txt = txt.replace("&lt;span&gt;", "<span>")  # Восстанавливаем <span> после экранирования
        txt = txt.replace("&lt;/span&gt;", "</span>")  # Восстанавливаем </span> после экранирования
        txt = txt.replace("&lt;small&gt;", "<small>")  # Восстанавливаем <small> после экранирования
        txt = txt.replace("&lt;/small&gt;", "</small>")  # Восстанавливаем </small> после экранирования
        txt = txt.replace("&lt;h4&gt;", "<h4>")  # Восстанавливаем <h4> после экранирования
        txt = txt.replace("&lt;/h4&gt;", "</h4>")  # Восстанавливаем </h4> после экранирования
        # Заменяем переносы строк на <br> только для обычного текста
        txt = re.sub(r'(?<!&lt;)(?<!<)(?<!>)\n(?!&gt;)(?!>)', '<br>', txt)
        return postprocess_html(txt)
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
