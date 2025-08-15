# -*- coding: utf-8 -*-
import json
import os
import re
from typing import Dict, List, Tuple

# ===== Загрузка базы =====
def load_laws(path: str) -> List[Dict]:
    if not os.path.exists(path):
        raise FileNotFoundError(f"LAWS_PATH not found: {path}")
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)
    laws = []
    for it in data:
        laws.append({
            "title": (it.get("title") or "").strip(),
            "text": (it.get("text") or "").strip(),
            "source": (it.get("source") or "").strip(),
        })
    return laws

# ===== Нормализация и токены =====
_WORD_RE = re.compile(r"[A-Za-zА-Яа-яЁё0-9\-]+", re.UNICODE)

def _tokens(s: str) -> List[str]:
    return [w.lower() for w in _WORD_RE.findall(s or "")]

def _contains_phrase(text: str, phrases: List[str]) -> bool:
    t = (text or "").lower()
    return any(p in t for p in phrases)

# ===== Интенты =====
def detect_intent(question: str) -> Dict:
    q = (question or "").lower()

    # Регистрация ИП
    if any(k in q for k in [
        "ип", "индивидуальный предприниматель", "открыть ип", "регистрация ип",
        "как оформить ип", "как открыть ип", "elicense", "egov", "e-gov"
    ]):
        return {"name": "register_ip"}

    # Увольнение/расторжение
    if any(k in q for k in [
        "уволить", "увольнение", "уволиться", "расторгнуть", "прекращение труд", "испытательный срок"
    ]):
        return {"name": "termination"}

    # Аренда/наём
    if any(k in q for k in [
        "аренда", "арендатор", "арендодатель", "наём", "жильё", "квартира", "найм жилья"
    ]):
        return {"name": "rental"}

    return {"name": "generic"}

# ===== Поиск законов =====
def search_laws(question: str, laws: List[Dict], top_k: int = 3, intent: Dict = None) -> List[Tuple[Dict, float]]:
    """
    Очень лёгкий скорер без сторонних библиотек:
    - пересечение токенов вопроса и текста;
    - буст за совпадения в title;
    - спец-бусты под интенты.
    """
    if not laws:
        return []

    intent = intent or {"name": "generic"}
    name = intent.get("name", "generic")

    q_tokens = _tokens(question)
    q_set = set(q_tokens)

    def score_one(item: Dict) -> float:
        title = item.get("title", "")
        text = item.get("text", "")

        t_tokens = set(_tokens(text))
        title_tokens = set(_tokens(title))

        # базовая схожесть: доля пересечений (очень грубо)
        overlap = len(q_set & t_tokens)
        title_overlap = len(q_set & title_tokens)

        s = 0.6 * overlap + 1.2 * title_overlap

        # интентные бусты
        if name == "register_ip":
            if _contains_phrase(title, ["ип", "индивидуальный предприниматель", "уведомление", "регистрация"]):
                s += 6.0
            if _contains_phrase(text, ["elicense", "уведомление о начале деятельности", "eGov", "налоговая"]):
                s += 3.5

        if name == "termination":
            if _contains_phrase(title, ["труд", "уволь", "расторж", "прекращен"]):
                s += 5.0
            if _contains_phrase(text, ["увольн", "трудовой договор", "приказ", "статья", "работодатель", "работник"]):
                s += 2.5

        if name == "rental":
            if _contains_phrase(title, ["аренда", "наём", "жиль"]):
                s += 5.0
            if _contains_phrase(text, ["арендатор", "арендодатель", "договор аренды", "выселение", "коммунальные"]):
                s += 2.0

        # небольшой буст за короткие, "практичные" статьи
        length = len(text)
        if 800 < length < 8000:
            s += 0.8

        return s

    scored = [(itm, score_one(itm)) for itm in laws]
    scored.sort(key=lambda x: x[1], reverse=True)
    return scored[: max(1, top_k)]
