import json
import math
import os
import re
from typing import Dict, List, Tuple, Iterable, Set

# -----------------------------
# Текстовые утилиты
# -----------------------------

_RU_SPLIT = re.compile(r"[^\w]+", re.UNICODE)
_SENT_SPLIT = re.compile(r"(?<=[.!?])\s+")

# Очень короткий список стоп-слов (минимум для рабочих кейсов)
_STOP = {
    "и", "в", "во", "на", "о", "об", "от", "по", "для", "с", "со", "к",
    "из", "за", "как", "что", "это", "или", "а", "но", "же", "бы", "ли",
    "при", "над", "под", "до", "после", "без", "про", "у", "мы", "вы",
    "они", "он", "она", "оно", "их", "его", "ее", "мой", "твой", "ваш",
    "наш", "тот", "эта", "эти", "то", "те", "все", "не", "нет", "да",
}

# Синонимы/родственные термины — короткий, но полезный набор
_SYNONYMS = {
    "увольнение": {"уволиться", "расторжение", "прекращение", "сокращение", "расторжение трудового договора", "увольнение"},
    "зарплата": {"заработная плата", "оплата труда", "оклад", "премия", "долг по зарплате", "задолженность"},
    "отпуск": {"ежегодный отпуск", "трудовой отпуск", "отпускные", "больничный"},
    "трудовой договор": {"контракт", "договор", "соглашение", "прием на работу"},
    "работодатель": {"компания", "организация", "наниматель"},
    "работник": {"сотрудник", "служащий"},
    "алимент": {"алименты", "содержание"},
    "развод": {"расторжение брака"},
    "недвижимость": {"имущество", "собственность"},
    "штраф": {"пени", "взыскание", "санкция"},
}

def tokenize(text: str) -> List[str]:
    if not text:
        return []
    toks = [t.lower() for t in _RU_SPLIT.split(text) if t]
    return [t for t in toks if t not in _STOP and len(t) > 1]

def expand_keywords(words: Iterable[str]) -> Set[str]:
    out = set()
    for w in words:
        out.add(w)
        for head, syns in _SYNONYMS.items():
            if w == head or w in syns:
                out.add(head)
                out |= syns
    return out

def sentences(text: str) -> List[str]:
    text = text.strip()
    if not text:
        return []
    # делим по предложениям
    return _SENT_SPLIT.split(text)

# -----------------------------
# Загрузка законов и индекс
# -----------------------------

class LawArticle:
    __slots__ = ("id", "title", "text", "source", "tokens", "ttf")
    def __init__(self, idx: int, raw: Dict):
        self.id = idx
        self.title = raw.get("title") or raw.get("name") or "Без названия"
        body = raw.get("text") or raw.get("content") or ""
        self.text = body
        self.source = raw.get("source") or raw.get("link") or ""
        # токены для скоринга
        self.tokens = tokenize(f"{self.title} {self.text}")
        self.ttf = {}  # term -> term frequency в статье
        for t in self.tokens:
            self.ttf[t] = self.ttf.get(t, 0) + 1

def load_laws(path: str) -> List[LawArticle]:
    if not os.path.exists(path):
        return []
    with open(path, "r", encoding="utf-8") as f:
        raw = json.load(f)
    laws = [LawArticle(i, rec) for i, rec in enumerate(raw)]
    return laws

class LawIndex:
    def __init__(self, laws: List[LawArticle]):
        self.laws = laws
        # обратный индекс: term -> [doc_id, ...]
        self.inv: Dict[str, List[int]] = {}
        for art in laws:
            for t in art.ttf.keys():
                self.inv.setdefault(t, []).append(art.id)
        # idf
        N = len(laws) or 1
        self.idf: Dict[str, float] = {}
        for term, posting in self.inv.items():
            df = len(set(posting))
            # сглаженный idf
            self.idf[term] = math.log(1 + (N - df + 0.5) / (df + 0.5))

    def bm25_score(self, query_terms: List[str], art: LawArticle, k1=1.5, b=0.75) -> float:
        # упрощённый BM25
        if not art.tokens:
            return 0.0
        L = len(art.tokens)
        avgL = max(1.0, sum(len(a.tokens) for a in self.laws) / (len(self.laws) or 1))
        score = 0.0
        for q in query_terms:
            tf = art.ttf.get(q, 0)
            if not tf:
                continue
            idf = self.idf.get(q, 0.0)
            denom = tf + k1 * (1 - b + b * L / avgL)
            score += idf * (tf * (k1 + 1)) / denom
        # лёгкий бонус за совпадение в заголовке
        title_bonus = sum(1 for q in query_terms if q in tokenize(art.title))
        if title_bonus:
            score *= (1.0 + 0.25 * title_bonus)
        return score

    def search(self, query: str, top_k: int = 6) -> List[Tuple[LawArticle, float]]:
        base = tokenize(query)
        if not base:
            return []
        expanded = expand_keywords(base)
        q_terms = list(expanded)
        scored: List[Tuple[LawArticle, float]] = []
        for art in self.laws:
            s = self.bm25_score(q_terms, art)
            # бонус за фразовые совпадения (грубая эвристика)
            low = (art.title + " " + art.text).lower()
            for kw in base:
                if f" {kw} " in f" {low} ":
                    s += 0.2
            if s > 0:
                scored.append((art, s))
        scored.sort(key=lambda x: x[1], reverse=True)
        return scored[:top_k]

# -----------------------------
# Сниппеты и HTML-контекст
# -----------------------------

def make_snippet(text: str, keyset: Set[str], window_chars: int = 220) -> str:
    if not text:
        return ""
    low = text.lower()
    hits: List[Tuple[int, int]] = []
    for kw in keyset:
        for m in re.finditer(r"\b" + re.escape(kw) + r"\b", low):
            s = max(0, m.start() - window_chars)
            e = min(len(text), m.end() + window_chars)
            hits.append((s, e))
    if not hits:
        return text[:2 * window_chars].strip()
    # слить пересечения
    hits.sort()
    merged = [hits[0]]
    for s, e in hits[1:]:
        ps, pe = merged[-1]
        if s <= pe + 30:
            merged[-1] = (ps, max(pe, e))
        else:
            merged.append((s, e))
    parts = []
    for s, e in merged[:3]:
        frag = text[s:e].strip()
        if s > 0:
            frag = "…" + frag
        if e < len(text):
            frag = frag + "…"
        parts.append(frag)
    return " ".join(parts)

def laws_to_html_context(results: List[Tuple[LawArticle, float]], query: str) -> str:
    if not results:
        return ""
    keys = expand_keywords(tokenize(query))
    lines = []
    lines.append('<h3>Релевантные законы</h3>')
    lines.append('<ul>')
    for art, _score in results:
        src = f' (<a href="{art.source}" target="_blank" rel="noopener">источник</a>)' if art.source else ""
        snip = make_snippet(art.text, keys)
        lines.append(f'<li><strong>{escape_html(art.title)}</strong>{src}<br>{escape_html(snip)}</li>')
    lines.append('</ul>')
    return "\n".join(lines)

def escape_html(s: str) -> str:
    return (
        s.replace("&", "&amp;")
         .replace("<", "&lt;")
         .replace(">", "&gt;")
    )
