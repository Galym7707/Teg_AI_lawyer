# -*- coding: utf-8 -*-
"""
Вспомогательные функции: загрузка законов, индекс, поиск, подготовка HTML-контекста.
Не требует внешних библиотек, работает быстро на 20–200 документах.
"""

from __future__ import annotations
import json
import os
import re
import html
from typing import List, Dict, Tuple


# --------------------------- Токенизация и нормализация ---------------------------

_RUS_STOP = {
    # мини-набор стоп-слов (можно расширять по мере надобности)
    "и", "в", "во", "на", "но", "да", "что", "как", "к", "ко", "от", "по", "за", "для",
    "с", "со", "у", "о", "об", "из", "не", "ни", "ли", "же", "бы", "же", "то", "это",
    "а", "или", "при", "над", "без", "под", "до", "после", "между", "при", "про", "надо",
}

_SYNONYMS = {
    # очень компактный синонимический словарик для русских форм
    "уволиться": {"уволиться", "увольнение", "уволен", "уволить", "расторжение", "прекращение"},
    "договор": {"договор", "контракт", "соглашение"},
    "работа": {"работа", "служба", "труд", "работодатель", "служащий", "работник"},
    "жалоба": {"жалоба", "заявление", "обращение", "претензия"},
    "штраф": {"штраф", "ответственность", "санкция", "наказание"},
    "суд": {"суд", "судебный", "исковое", "иск"},
}

_TOKEN_RE = re.compile(r"[A-Za-zА-Яа-яЁё0-9]{2,}", re.UNICODE)


def escape_html(s: str) -> str:
    return html.escape(s, quote=True)


def _normalize(text: str) -> str:
    return " ".join(_TOKEN_RE.findall(text.lower()))


def _tokens(text: str) -> List[str]:
    return [t for t in _TOKEN_RE.findall(text.lower()) if t not in _RUS_STOP]


def _expand_with_synonyms(tokens: List[str]) -> List[str]:
    out = list(tokens)
    for t in tokens:
        for root, syns in _SYNONYMS.items():
            if t in syns:
                out.extend(list(syns))
    return out


# --------------------------- Загрузка законов ---------------------------

def load_laws(laws_path: str) -> List[Dict]:
    """
    Ожидается JSON-массив объектов вида:
      { "title": "...", "text": "...", "source": "https://..." }
    """
    if not os.path.exists(laws_path):
        raise FileNotFoundError(f"LAWS_PATH not found: {laws_path}")

    with open(laws_path, "r", encoding="utf-8") as f:
        data = json.load(f)

    docs = []
    for i, item in enumerate(data):
        title = str(item.get("title", "")).strip()
        text = str(item.get("text", "")).strip()
        source = str(item.get("source", "")).strip()

        if not text:
            continue

        docs.append({
            "id": i,
            "title": title or f"Без названия #{i+1}",
            "text": text,
            "source": source,
        })
    return docs


# --------------------------- Индекс и скоринг ---------------------------

class LawIndex:
    def __init__(self, laws: List[Dict]):
        self.docs = laws
        self.norm = []     # нормализованный текст
        self.tokens = []   # токены (со стоп-словами выкинутыми)
        self.inv = {}      # инвертированный индекс: токен -> set(doc_ids)

        for doc in self.docs:
            nrm = _normalize(doc["text"])
            toks = _tokens(doc["text"])
            self.norm.append(nrm)
            self.tokens.append(toks)
            for t in set(toks):
                self.inv.setdefault(t, set()).add(doc["id"])

    def _candidate_docs(self, query_tokens: List[str]) -> List[int]:
        """
        Находит кандидатов по пересечению токенов и их синонимов.
        """
        expanded = _expand_with_synonyms(query_tokens)
        cand_sets = []
        for t in set(expanded):
            cand_sets.append(self.inv.get(t, set()))
        if not cand_sets:
            return []
        # объединяем (union), а не пересечение, чтобы не терять кандидатов
        cands = set()
        for s in cand_sets:
            cands |= s
        return list(cands)

    def _score(self, doc_id: int, query: str, q_tokens: List[str]) -> float:
        """
        Грубая, но практичная метрика релевантности:
        - совпадения по токенам (+1 за уникальный токен запроса, встречающийся в документе)
        - бонус за фразы запроса (подстроки длиной 2–4 токена)
        - лёгкий бонус за упоминание ключевых «юридических слов» (увольнение/договор/и т.д.)
        """
        doc_toks = set(self.tokens[doc_id])
        base = 0.0

        uniq_q = set(_expand_with_synonyms(q_tokens))
        for t in uniq_q:
            if t in doc_toks:
                base += 1.0

        # N-граммы (2..4) — если фраза встречается как подстрока
        doc_norm = self.norm[doc_id]
        qtoks = q_tokens[:]
        for n in (4, 3, 2):
            if len(qtoks) >= n:
                for i in range(0, len(qtoks) - n + 1):
                    phrase = " ".join(qtoks[i:i+n])
                    if phrase and phrase in doc_norm:
                        base += 1.5 * (n - 1)  # длиннее фраза — больше вес

        # бонус за юридические триггеры
        for root in _SYNONYMS:
            if any(t in doc_toks for t in _SYNONYMS[root]):
                base += 0.2

        return base

    def search(self, query: str, top_k: int = 5, min_score: float = 1.0) -> List[Tuple[Dict, float, str]]:
        """
        Возвращает список кортежей (doc, score, snippet_html)
        """
        q = query.strip()
        if not q:
            return []

        q_tokens = _tokens(q)
        cand_ids = self._candidate_docs(q_tokens)
        if not cand_ids:
            return []

        scored: List[Tuple[int, float]] = []
        for doc_id in cand_ids:
            sc = self._score(doc_id, q, q_tokens)
            if sc > 0:
                scored.append((doc_id, sc))

        if not scored:
            return []

        scored.sort(key=lambda x: x[1], reverse=True)
        out = []
        for doc_id, sc in scored[:top_k]:
            doc = self.docs[doc_id]
            snippet = make_snippet(doc["text"], q, q_tokens)
            out.append((doc, sc, snippet))
        # фильтр нижнего порога — чтобы не лепить нерелевант
        return [(d, s, sn) for (d, s, sn) in out if s >= min_score]


def make_snippet(text: str, query: str, q_tokens: List[str], max_len: int = 600) -> str:
    """
    Делает HTML-сниппет с подсветкой совпадений.
    """
    safe = escape_html(text)
    # подсветка ключевых слов
    hl_tokens = sorted(set(_expand_with_synonyms(q_tokens)), key=len, reverse=True)
    for t in hl_tokens:
        if len(t) < 3:
            continue
        safe = re.sub(rf"(?i)\b({re.escape(t)})\b", r"<mark>\1</mark>", safe)

    if len(safe) <= max_len:
        return safe

    # пытаемся найти фрагмент вокруг первого ключа
    first = None
    for t in hl_tokens:
        m = re.search(rf"(?i)\b{re.escape(t)}\b", safe)
        if m:
            first = m.start()
            break
    if first is None:
        return safe[:max_len] + "..."

    left = max(0, first - max_len // 2)
    right = min(len(safe), left + max_len)
    frag = safe[left:right]
    if left > 0:
        frag = "..." + frag
    if right < len(safe):
        frag = frag + "..."
    return frag


# --------------------------- Подготовка HTML-контекста ---------------------------

def laws_to_html_context(results: List[Tuple[Dict, float, str]]) -> Tuple[str, List[Dict]]:
    """
    Из результатов поиска делает HTML-контекст для подсказки модели
    и компактный список использованных источников для фронта.
    """
    if not results:
        return "", []

    sections = []
    used = []
    for doc, score, snippet in results:
        title = escape_html(doc["title"])
        source = escape_html(doc["source"])
        sections.append(
            f"<section>"
            f"<h3>{title}</h3>"
            f"<p><em>Источник:</em> <a href=\"{source}\" target=\"_blank\" rel=\"noopener\">{source}</a></p>"
            f"<p>{snippet}</p>"
            f"</section>"
        )
        used.append({"title": doc["title"], "source": doc["source"], "score": round(score, 2)})

    html_ctx = "\n".join(sections)
    return html_ctx, used
