# -*- coding: utf-8 -*-
"""
Вспомогательные функции для Kaz Legal Bot:
- нормализация текста
- токенизация
- расширение запроса синонимами
- построение инвертированного индекса
"""

import re
from typing import List, Dict, Any, Set

# ----------------------- Нормализация / токены -------------
_norm_map = str.maketrans({"Ё": "Е", "ё": "е"})

def normalize_text(s: str) -> str:
    s = (s or "").translate(_norm_map)
    s = s.lower()
    # убираем лишнее, оставляем буквы/цифры и пробел
    s = re.sub(r"[^\w\s]", " ", s, flags=re.U)
    s = re.sub(r"\s+", " ", s).strip()
    return s

_token_re = re.compile(r"[0-9a-zA-Zа-яА-ЯёЁ]+", re.U)

def tokenize(s: str) -> List[str]:
    s = normalize_text(s)
    return _token_re.findall(s)

# ----------------------- Синонимы --------------------------
# Минимальный, но полезный словарь. При желании дополняйте.
LEGAL_SYNONYMS: Dict[str, List[str]] = {
    # трудовые отношения / увольнение
    "увольнение": [
        "уволиться", "уволиться с работы", "уволицца", "расторжение трудового договора",
        "прекращение трудового договора", "уволен", "увольнять", "увольнении",
        "dismissal", "termination of employment",
    ],
    "заявление": ["заявление об увольнении", "письменное заявление", "уведомление"],
    "отработка": ["без отработки", "предупреждение за 1 месяц", "срок предупреждения"],
    "компенсация": ["выплаты", "выходное пособие", "компенсационные выплаты", "расчет"],
    "зарплата": ["заработная плата", "выплата зарплаты", "удержания"],
    "отпуск": ["компенсация за отпуск", "неиспользованный отпуск"],
    "инспекция": ["трудовая инспекция", "госинспекция труда", "жалоба"],
    # общие
    "суд": ["исковое заявление", "судебный порядок", "судебное разбирательство"],
    "срок": ["сроки", "дедлайн", "период"],
}

def expand_keywords(query: str) -> Set[str]:
    """
    Берём токены запроса + синонимы + базовые формы (в лоб).
    """
    base = set(tokenize(query))
    expanded: Set[str] = set(base)

    # Добавляем синонимы для каждого слова
    for token in list(base):
        # прямое совпадение
        if token in LEGAL_SYNONYMS:
            expanded.update(tokenize(" ".join(LEGAL_SYNONYMS[token])))

        # обратное сопоставление (если token попал как синоним другого ключа)
        for head, syns in LEGAL_SYNONYMS.items():
            for syn in syns:
                if token == normalize_text(syn):
                    expanded.add(head)

    # Ещё раз прогон по токенайзеру — на случай составных выражений
    out: Set[str] = set()
    for item in expanded:
        out.update(tokenize(item))

    # отбрасываем слишком короткие слова
    out = {w for w in out if len(w) >= 3}
    return out

# ----------------------- Индекс ----------------------------
def build_law_index(laws: List[Dict[str, Any]], text_limit_chars: int = 6000) -> Dict[str, Set[int]]:
    """
    Инвертированный индекс по title + части текста.
    Ограничиваемся первыми N символами, чтобы не раздувать память.
    """
    index: Dict[str, Set[int]] = {}

    for i, law in enumerate(laws):
        title = law.get("title", "")
        text = law.get("text", "")

        # индексируем заголовок полностью
        for tok in tokenize(title):
            index.setdefault(tok, set()).add(i)

        # и первые N символов текста, чтобы ловить ключевые термины
        if text:
            fragment = text[:text_limit_chars]
            for tok in tokenize(fragment):
                index.setdefault(tok, set()).add(i)

    return index

# ----------------------- Утилита описания ------------------
def short_first_sentence(text: str, max_chars: int = 200) -> str:
    """
    Первая фраза для краткого описания (если когда-нибудь понадобится).
    Сейчас не используется (мы не цитируем длинные тексты),
    но оставлено на будущее.
    """
    if not text:
        return ""
    clean = re.sub(r"\s+", " ", text).strip()
    # до первой точки/перевода строки
    m = re.search(r"[\.!\?]\s", clean)
    sent = clean if not m else clean[: m.end()].strip()
    if len(sent) > max_chars:
        sent = sent[:max_chars].rstrip() + "…"
    return sent
