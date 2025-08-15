# -*- coding: utf-8 -*-
import os
import re
import json
import logging
from typing import List, Dict, Tuple

log = logging.getLogger(__name__)

# ============== Загрузка базы ==============
def load_laws(path: str) -> List[Dict]:
    """
    Ожидает JSON-массив объектов: { "title": "...", "text": "...", "source": "..." }
    """
    if not os.path.isabs(path):
        base_dir = os.path.dirname(__file__)
        path = os.path.join(base_dir, path)
    if not os.path.exists(path):
        raise FileNotFoundError(f"laws file not found: {path}")

    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)
    # нормализуем
    out = []
    for i, it in enumerate(data):
        out.append({
            "title": (it.get("title") or "").strip(),
            "text": (it.get("text") or "").strip(),
            "source": (it.get("source") or "").strip(),
        })
    log.info("✅ Загружено %d статей из базы законов", len(out))
    return out

# ============== Поиск по базе (простой, но надёжный) ==============
WORD_RE = re.compile(r"[а-яёa-z0-9\-]+", re.IGNORECASE)

def _tokenize(text: str) -> List[str]:
    return WORD_RE.findall(text.lower())

def _score(query_tokens: List[str], doc: Dict) -> float:
    """
    Простой скоринг: совпадения в title весят x3, в source x2, в тексте x1.
    Плюс бонус за точные биграммы из вопроса.
    """
    title = (doc.get("title") or "").lower()
    body  = (doc.get("text") or "").lower()
    src   = (doc.get("source") or "").lower()

    score = 0.0
    for t in query_tokens:
        if t in title:  score += 3.0
        if t in src:    score += 2.0
        if t in body:   score += 1.0

    # биграммы
    bigrams = [f"{query_tokens[i]} {query_tokens[i+1]}" for i in range(len(query_tokens)-1)]
    for bg in bigrams:
        if bg and (bg in title or bg in body):
            score += 2.0
    return score

def search_laws(query: str, laws: List[Dict], top_k: int = 3) -> Tuple[List[Tuple[Dict, float]], Dict]:
    q = query.strip().lower()
    tokens = _tokenize(q)
    if not tokens:
        return [], {"name": "other"}

    # намерение — передадим модели и playbook’у
    intent = detect_intent(q)

    scored = []
    for art in laws:
        s = _score(tokens, art)
        if s > 0:
            scored.append((art, s))
    scored.sort(key=lambda x: x[1], reverse=True)
    return scored[:top_k], intent

# ============== Намерения + Playbooks ==============
INTENT_PATTERNS = [
    ("fire_self", [r"\bувол(ить|ился|иться|яюсь|ьня)\b", r"\bрасторг(нуть|ать)\b", r"\bуволиться\b"]),
    ("register_ip", [r"\b(как )?(открыть|оформить)\b.*\bип\b", r"\bрегистрац(ия|ию)\b.*\bип\b"]),
    ("debt", [r"\bвзыскан(ие|и|ья)\b|\bдолг\b|\bрасписка\b"]),
    ("family", [r"\bразвод\b|\bалименты\b"]),
    ("other", [r".*"]),
]

def detect_intent(text: str) -> Dict:
    for name, pats in INTENT_PATTERNS:
        for p in pats:
            if re.search(p, text, flags=re.IGNORECASE):
                return {"name": name}
    return {"name": "other"}

def get_playbook_html(intent_name: str) -> str:
    """
    Короткие «скелеты» ответа, чтобы модель не «тормозила».
    """
    if intent_name == "fire_self":
        return """
<p><em>По общей практике в РК при увольнении по инициативе работника:</em></p>
<h4>Мини-чек-лист</h4>
<ul>
  <li>Подготовить заявление об увольнении (Ф.И.О., должность, дата, подпись, желаемая дата увольнения).</li>
  <li>Срок предупреждения: как правило, 1 месяц (уточняется ТК РК и договором).</li>
  <li>Передать заявление работодателю под роспись или направить по корпоративной почте/ЭЦП.</li>
  <li>Отработать установленный срок или согласовать сокращение срока по соглашению сторон.</li>
  <li>В день увольнения — получить окончательный расчёт и документы.</li>
</ul>
""".strip()
    if intent_name == "register_ip":
        return """
<p><em>По общей практике в РК при регистрации ИП:</em></p>
<h4>Мини-чек-лист</h4>
<ul>
  <li>Определить коды деятельности и систему налогообложения (обычно специальный порядок для малого бизнеса).</li>
  <li>Подготовить удостоверение личности/ЭЦП.</li>
  <li>Подать уведомление о начале деятельности (через eGov/елицензирование) — онлайн-регистрация.</li>
  <li>Получить подтверждение регистрации в личном кабинете.</li>
  <li>При необходимости — открыть счёт, уведомить банки/контрагентов.</li>
</ul>
""".strip()
    return ""  # для прочего — пусто

# ============== Фолбэк без модели (на всякий) ==============
def build_minimal_html_answer(question: str, hits: List[Tuple[Dict, float]], intent: Dict) -> str:
    if hits:
        items = []
        for a, s in hits:
            t = (a.get("title") or "").strip()
            src = (a.get("source") or "").strip()
            items.append(f'<li><strong>{t}</strong>{(" — " + f"<a href=\"{src}\" target=\"_blank\">источник</a>" if src else "")} (score {s:.2f})</li>')
        return f"""
<h3>Юридическая оценка</h3>
<p>Найдены нормы по вашему вопросу. Ознакомьтесь с ними и уточните детали, чтобы я составил пошаговый план.</p>
<h3>Нормы</h3>
<ul>{''.join(items)}</ul>
""".strip()
    else:
        # даже без норм даём минимум пользы
        return f"""
<h3>Юридическая оценка</h3>
<p>В базе не найдено прямых совпадений, но ниже — практические шаги по общей практике РК.</p>
{get_playbook_html(intent.get("name", "other"))}
""".strip()
