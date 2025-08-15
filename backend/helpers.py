# -*- coding: utf-8 -*-
"""
Поиск по базе законов + сборка HTML ответа.
Без внешних зависимостей, только stdlib.
"""

import os
import json
import re
import html
from typing import List, Dict, Tuple
from collections import Counter

# -------------------- загрузка базы --------------------

def load_laws(path: str) -> List[Dict]:
    """
    Ожидаем JSON: [{ "title": "...", "text": "...", "source": "..." }, ...]
    """
    if not os.path.exists(path):
        alt = os.path.join(os.path.dirname(__file__), path)
        if os.path.exists(alt):
            path = alt
        else:
            raise FileNotFoundError(f"Не найден файл законов: {path}")

    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)

    cleaned = []
    for item in data:
        title = (item.get("title") or "").strip()
        text = (item.get("text") or "").strip()
        source = (item.get("source") or "").strip()
        if not text:
            continue
        cleaned.append({"title": title, "text": text, "source": source})
    return cleaned

# -------------------- токены / стоп-слова --------------------

_WORD_RE = re.compile(r"[а-яёa-z0-9]+", re.IGNORECASE)

STOPWORDS_RU = {
    "и","в","во","не","что","он","она","оно","они","а","но","то","на","я","мы","вы",
    "с","со","как","к","ко","о","об","от","по","за","из","у","для","при","над","под",
    "через","или","ли","же","бы","это","так","все","всё","его","ее","её","их",
    "есть","нет","будет","быть","тем","чтобы","который","которая","которые",
    "правильно","правильный","работа","работы","работать","делать","нужно","можно",
    "про","например","если","когда","где","какие","какой","какая","каков","каково",
    "как-то","как-нибудь","прошу","подскажите","расскажите"
}

def _tokens(s: str, drop_stop=True) -> List[str]:
    toks = [t.lower() for t in _WORD_RE.findall(s)]
    if drop_stop:
        toks = [t for t in toks if t not in STOPWORDS_RU and len(t) > 2]
    return toks

# -------------------- намерения / ключевые темы --------------------

INTENT_PATTERNS = [
    {
        "name": "termination",
        "triggers": [r"увол", r"увольн", r"расторжен", r"прекращен"],
        "boost_words": [r"труд", r"работодател", r"работник", r"договор", r"контракт", r"приказ", r"заявлен"],
    },
    # При необходимости добавляйте новые темы
]

def detect_intent(question: str) -> Dict:
    q = question.lower()
    for intent in INTENT_PATTERNS:
        if any(re.search(p, q) for p in intent["triggers"]):
            return intent
    return {"name": "generic", "triggers": [], "boost_words": []}

# -------------------- скоринг статей --------------------

SERVICE_NOISE_PREFIXES = (
    "примечание изпи", "примечание рцпи", "содержание", "оглавление",
)

def _service_noise_penalty(text: str) -> float:
    t = text[:180].lower().strip()
    return -2.0 if any(t.startswith(p) for p in SERVICE_NOISE_PREFIXES) else 0.0

def _contains_any(patterns: List[str], text: str) -> bool:
    tl = text.lower()
    return any(re.search(p, tl) for p in patterns)

def _count_hits(patterns: List[str], text: str) -> int:
    tl = text.lower()
    return sum(1 for p in patterns if re.search(p, tl))

def _dot(a: Counter, b: Counter) -> int:
    return sum(min(a[k], b.get(k, 0)) for k in a)

def _score_article(q_tokens: Counter, art: Dict, intent: Dict) -> float:
    # токены статьи
    t_title = Counter(_tokens(art.get("title", ""), drop_stop=True))
    t_text  = Counter(_tokens(art.get("text", ""),  drop_stop=True))

    # базовый скор: совпадения в заголовке важнее
    title_part = 2.5 * _dot(q_tokens, t_title)
    text_part  = 1.0 * _dot(q_tokens, t_text)

    score = title_part + text_part

    # буст по теме (если нашли намерение)
    if intent["name"] != "generic":
        # наличие «тематических» слов
        topic_hits_title = _count_hits(intent["boost_words"], art.get("title", ""))
        topic_hits_text  = _count_hits(intent["boost_words"], art.get("text", ""))
        # наличие триггеров самой темы (увол/прекращ/расторж)
        trig_hits_title = _count_hits(intent["triggers"], art.get("title", ""))
        trig_hits_text  = _count_hits(intent["triggers"], art.get("text", ""))

        score += 1.5 * topic_hits_title + 0.8 * topic_hits_text
        score += 2.0 * trig_hits_title + 1.2 * trig_hits_text

    # штраф за «служебные» куски
    score += _service_noise_penalty(art.get("text", ""))

    return score

def search_laws(question: str, laws: List[Dict], top_k: int = 3) -> Tuple[List[Tuple[Dict, float]], Dict]:
    """Возвращает [(article, score), ...] и словарь intent."""
    intent = detect_intent(question)
    if not laws:
        return [], intent

    q_tokens = Counter(_tokens(question, drop_stop=True))
    if not q_tokens:
        return [], intent

    scored = [(art, _score_article(q_tokens, art, intent)) for art in laws]
    scored.sort(key=lambda x: x[1], reverse=True)
    scored = [x for x in scored if x[1] > 0]

    # отсечём откровенный мусор (не ниже 45% от лучшего)
    if scored:
        top = scored[0][1]
        thresh = max(1.0, top * 0.45)
        scored = [x for x in scored if x[1] >= thresh]

    return scored[:top_k], intent

# -------------------- сниппет по ключевым словам --------------------

def _extract_snippet(text: str, patterns: List[str], max_len: int = 420) -> str:
    """
    Ищем предложение, где встречается один из patterns, возвращаем короткий фрагмент.
    Если не нашли — первые осмысленные 420 символов.
    """
    t = text.strip()
    # Разобьём на псевдо-предложения
    sentences = re.split(r"(?<=[\.\!\?])\s+", t)
    for sent in sentences:
        if _contains_any(patterns, sent):
            cut = sent.strip()
            return (cut[:max_len] + "…") if len(cut) > max_len else cut

    # запасной вариант: пропустить вступительные «Примечание/Содержание»
    head = t
    for pref in SERVICE_NOISE_PREFIXES:
        pref_low = pref.lower()
        if head.lower().startswith(pref_low):
            head = head[len(pref):].lstrip(":—- \n\r")
            break

    head = head.strip()
    return (head[:max_len] + "…") if len(head) > max_len else head

# -------------------- HTML ответ --------------------

def _esc(s: str) -> str:
    return html.escape(s, quote=True)

def _practical_steps(intent_name: str) -> str:
    if intent_name == "termination":
        # без спорных сроков/цифр — общая канва действий
        return (
            "<h3>Практические шаги</h3>"
            "<ul>"
            "<li><strong>Подготовьте письменное заявление</strong> на расторжение трудового договора (укажите ФИО, должность, дату, причину/основание, подпись).</li>"
            "<li><strong>Передайте заявление работодателю</strong> (под роспись на копии или через канцелярию/ЭДО) и сохраните подтверждение.</li>"
            "<li><strong>Урегулируйте расчёты</strong>: заработная плата, компенсации, передача имущества/дел.</li>"
            "<li><strong>Проверьте приказ</strong> об увольнении и формулировку основания.</li>"
            "<li><strong>Получите документы</strong> при увольнении (копия приказа, справки о доходах и др.).</li>"
            "</ul>"
        )
    return ""

def build_html_answer(question: str, hits: List[Tuple[Dict, float]], intent: Dict) -> str:
    q_html = _esc(question)
    if not hits:
        return (
            "<h3>Предварительная консультация</h3>"
            f"<p>По запросу <em>«{q_html}»</em> прямых совпадений в базе не найдено. "
            "Опишите, пожалуйста, детали (вид договора, роли сторон, даты) — тогда подберу точные нормы.</p>"
        )

    parts = [
        "<h3>Анализ по базе законов РК</h3>",
        f"<p><strong>Ваш вопрос:</strong> {q_html}</p>",
        "<ol>"
    ]

    for art, score in hits:
        title = _esc(art.get("title") or "Без названия")
        source = _esc(art.get("source") or "")
        # для сниппета используем триггеры темы + буст-слова
        patterns = intent.get("triggers", []) + intent.get("boost_words", [])
        raw_snippet = _extract_snippet(art.get("text", ""), patterns, max_len=520)
        snippet = _esc(raw_snippet)

        parts.append(
            "<li>"
            f"<p><strong>Норма:</strong> {title}"
            + (f" (<a href=\"{source}\" target=\"_blank\" rel=\"noopener\">источник</a>)" if source else "")
            + "</p>"
            f"<p><em>Фрагмент:</em> {snippet}</p>"
            "</li>"
        )

    parts.append("</ol>")

    # Практические шаги для известных сценариев
    steps_html = _practical_steps(intent.get("name", ""))
    if steps_html:
        parts.append(steps_html)

    parts.append(
        "<p><strong>Важно:</strong> автоматическая выдача может включать близкие, но не идентичные нормы. "
        "Если уточните обстоятельства (самовольная/по соглашению/инициатива работодателя и т.п.), подберу точные статьи.</p>"
    )

    return "".join(parts)
