# -*- coding: utf-8 -*-
"""
Nightly updater for backend/laws/kazakh_laws.json

Функции:
- Тянет страницы из дефолтного списка источников.
- Добывает основной текст (bs4), делает грубую очистку (регэкспы).
- При наличии GEMINI_API_KEY прогоняет через LLM-очиститель по кускам.
- Обновляет backend/laws/kazakh_laws.json (с тем же форматом: [{title,text,source}]).
- Коммитит изменения в GitHub Actions (см. workflow).

Важно:
- Ничего не «выдумывает»: LLM только чистит/нормализует текст (инструкция в prompt).
- Если сайт меняет разметку/блокирует, падать не будет — просто пропустит источник.
"""
from tenacity import retry, stop_after_attempt, wait_exponential
import logging
import os
import re
import json
import hashlib
import datetime as dt
from pathlib import Path
from typing import List, Dict, Optional
from urllib.parse import urljoin, urlparse

import requests
import chardet
from bs4 import BeautifulSoup, Tag
from playwright.sync_api import sync_playwright

def fetch_url_playwright(url, timeout=15000):
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)  # лучше headless=True для GitHub Actions
        page = browser.new_page()
        page.goto(url, timeout=timeout)
        html = page.content()
        browser.close()
        return html

# --- опционально LLM (автоматически отключится, если нет ключа) ---
USE_LLM = False
LLM_MODEL = os.getenv("LLM_MODEL", "gemini-1.5-flash")

try:
    import google.generativeai as genai
    if os.getenv("GEMINI_API_KEY"):
        genai.configure(api_key=os.getenv("GEMINI_API_KEY"))
        USE_LLM = True
except Exception:
    USE_LLM = False  # безопасно отключаем LLM

# ---- пути/конфиги ----
ROOT = Path(__file__).resolve().parents[1]  # корень репо
RAW_LAWS_JSON = Path(os.getenv("RAW_LAWS_JSON", str(ROOT / "backend" / "laws" / "kazakh_laws.json")))
NORMALIZED_LAWS = Path(os.getenv("NORMALIZED_LAWS", str(ROOT / "backend" / "laws" / "normalized.jsonl")))
DRY_RUN = os.getenv("DRY_RUN", "false").lower() == "true"

DEFAULT_SOURCES = [
    {"title": "Трудовой кодекс Республики Казахстан", "url": "https://adilet.zan.kz/rus/docs/K1500000414"},
    {"title": "Кодекс Республики Казахстан об административных правонарушениях", "url": "https://adilet.zan.kz/rus/docs/K1400000235"},
    {"title": "Гражданский процессуальный кодекс Республики Казахстан", "url": "https://adilet.zan.kz/rus/docs/K1500000377"},
    {"title": "Уголовный кодекс Республики Казахстан", "url": "https://adilet.zan.kz/rus/docs/K1400000226"},
]

# ---- константы для извлечения статей ----
ARTICLE_LINK_TEXT_RE = re.compile(r"стат(ья|\.|)\s*\d+", flags=re.I)

# ---- базовые утилиты ----
def sha256_text(s: str) -> str:
    return hashlib.sha256(s.encode("utf-8", errors="replace")).hexdigest()

def ensure_parent(p: Path):
    p.parent.mkdir(parents=True, exist_ok=True)

def read_file(p: Path) -> str:
    if not p.exists():
        return ""
    return p.read_text("utf-8")

def write_file(p: Path, text: str):
    ensure_parent(p)
    p.write_text(text, "utf-8", newline="\n")

def load_json_list(p: Path) -> List[Dict]:
    if not p.exists():
        return []
    try:
        return json.loads(read_file(p))
    except Exception:
        return []

def save_json_list(p: Path, items: List[Dict]):
    ensure_parent(p)
    with p.open("w", encoding="utf-8") as f:
        json.dump(items, f, ensure_ascii=False, indent=2)

# ---- скачивание и извлечение текста ----
def fetch_url(url: str, timeout=40) -> Optional[str]:
    try:
        headers = {
            "User-Agent": "Mozilla/5.0 (KazLegalBot Update Script; +https://github.com/your-repo)"
        }
        resp = requests.get(url, headers=headers, timeout=timeout)
        resp.raise_for_status()
        content = resp.content
        # корректная декодировка
        enc = chardet.detect(content).get("encoding") or resp.encoding or "utf-8"
        return content.decode(enc, errors="replace")
    except Exception as e:
        print(f"[WARN] Не удалось скачать {url}: {e}")
        return None

@retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=4, max=10))
def fetch_egov_laws() -> List[Dict]:
    """Получает список НПА с data.egov.kz с retry-логикой."""
    url = "https://data.egov.kz/api/v4/dataset"
    params = {
        "source": json.dumps({
            "query": {"match": {"category": "Нормативные правовые акты"}},
            "size": 100  # Увеличил лимит
        })
    }
    
    response = requests.get(url, params=params, timeout=15)
    response.raise_for_status()  # Проверка на ошибки HTTP
    data = response.json()
    
    # Исправление 1: Правильная обработка формата ответа API
    if not isinstance(data, dict):
        print(f"[WARN] API вернул не словарь: {type(data)}")
        return []
    
    # Безопасное извлечение hits с проверкой структуры
    hits = data.get("hits", [])
    if not isinstance(hits, list):
        print(f"[WARN] Поле 'hits' не является списком: {type(hits)}")
        return []
    
    result = []
    for item in hits:
        # Исправление 2: Проверка структуры данных
        if not isinstance(item, dict):
            print(f"[WARN] Элемент в hits не является словарем: {type(item)}")
            continue
        
        title = item.get("title", "Без названия")
        url = item.get("download_url")
        
        if not url:
            print(f"[WARN] Нет download_url для элемента: {title}")
            continue
        
        result.append({
            "title": title,
            "url": url,
            "updated_at": item.get("last_updated", ""),
            "source": "data.egov.kz"  # Добавил источник
        })
    
    return result

def extract_main_text(html: str) -> str:
    soup = BeautifulSoup(html, "lxml")
    # уберём шум
    for tag in soup(["script", "style", "noscript", "nav", "header", "footer", "iframe"]):
        tag.decompose()
    # эвристика: берём либо <main>/<article>, либо body
    cand = soup.find("main") or soup.find("article") or soup.body or soup
    text = cand.get_text(separator="\n", strip=True)
    return text

def extract_article_links_from_toc(html: str, base_url: str) -> list[dict]:
    """
    Находит в странице ссылки на статьи (оглавление).
    Возвращает список dict: {"article_title": "...", "href": "полный_URL", "anchor": "...", "is_same_page": True/False}
    """
    soup = BeautifulSoup(html, "lxml")
    links = []
    # 1) быстро искать явные <a> с текстом 'Статья'
    for a in soup.find_all('a', href=True):
        txt = (a.get_text(" ", strip=True) or "")
        if ARTICLE_LINK_TEXT_RE.search(txt):
            href = a['href'].strip()
            full = urljoin(base_url, href)
            is_same = urlparse(full).path == urlparse(base_url).path
            links.append({"article_title": txt, "href": full, "is_same_page": is_same, "raw_href": href})
    if links:
        return links

    # 2) fallback: искать в списках/оглавлении тексты типа "Статья 1. ..." без <a>
    # На многих страницах элементы оглавления — это просто текстовые <li> или <p>
    for el in soup.find_all(['li','p','div','span']):
        txt = (el.get_text(" ", strip=True) or "")
        if ARTICLE_LINK_TEXT_RE.match(txt):
            # попытаемся найти вложенный <a>
            a = el.find('a', href=True)
            if a:
                href = a['href']
                full = urljoin(base_url, href)
                links.append({"article_title": txt, "href": full, "is_same_page": urlparse(full).path == urlparse(base_url).path, "raw_href": href})
    return links

def extract_article_text_by_anchor_or_header(page_html: str, locator_href: str, base_url: str) -> str:
    """
    Если locator_href — это '...#anchor' (или просто '#anchor'), найдём соответствующий элемент по id/name
    и соберём текст до следующего заголовка того же/высшего уровня. Если locator_href — full URL,
    будем возвращать весь текст страницы (или применять heuristics).
    """
    soup = BeautifulSoup(page_html, "lxml")
    parsed = urlparse(locator_href)
    anchor = parsed.fragment or None

    if anchor:
        # 1) найти элемент с id=anchor
        target = soup.find(id=anchor) or soup.find(attrs={"name": anchor})
        if target:
            # собираем текст: начинаем от target и берём следующие sibling-ы пока не встретим заголовок того же уровня
            parts = []
            # если target — заголовок, добавим его текст
            if isinstance(target, Tag):
                title_text = target.get_text(" ", strip=True)
                if title_text:
                    parts.append(title_text)
            # проходим siblings
            for sib in target.next_siblings:
                if isinstance(sib, Tag) and sib.name and re.match(r"h[1-6]", sib.name, flags=re.I):
                    # встретили следующий заголовок — заканчиваем
                    break
                # собрать текст контента
                if isinstance(sib, Tag):
                    parts.append(sib.get_text("\n", strip=True))
                elif isinstance(sib, str):
                    parts.append(sib.strip())
            return "\n\n".join(p for p in parts if p).strip()

    # Нет anchor или не нашли — попробуем найти заголовок с текстом "Статья N"
    # Ищем заголовки с похожим текстом
    for h in soup.find_all(re.compile(r"h[1-6]")):
        txt = h.get_text(" ", strip=True)
        if ARTICLE_LINK_TEXT_RE.search(txt):
            # найдена статья, берем текст между этим заголовком и следующим заголовком того же уровня
            parts = [txt]
            for sib in h.next_siblings:
                if isinstance(sib, Tag) and sib.name and re.match(r"h[1-6]", sib.name, flags=re.I):
                    break
                if isinstance(sib, Tag):
                    parts.append(sib.get_text("\n", strip=True))
                elif isinstance(sib, str):
                    parts.append(sib.strip())
            return "\n\n".join(p for p in parts if p).strip()

    # fallback: просто вернём весь видимый текст
    main = soup.find("main") or soup.find("article") or soup.body or soup
    return main.get_text("\n", strip=True)

# ---- грубая очистка (без LLM) ----
REMOVE_PATTERNS = [
    r"(?im)^\s*СОДЕРЖАНИЕ\s*$.*?(?=^\S|\Z)",             # блок "СОДЕРЖАНИЕ" (пока простой эвристикой)
    r"(?im)^\s*Примечание\s+(ИЗПИ|РЦПИ)!?.*$",           # примечания издателя
    r"(?im)^\s*Сноска\..*$",                             # сноски
    r"(?im)^\s*Вводится в действие.*$",                  # вводные блоки (часто не нужны для поиска норм)
    r"(?im)^\s*Примечание.*вводится.*$",                 # прочие примечания
]

def coarse_cleanup(s: str) -> str:
    s = s.replace("\r\n", "\n").replace("\r", "\n")
    # убираем мусорные блоки
    for pat in REMOVE_PATTERNS:
        s = re.sub(pat, "", s, flags=re.DOTALL)
    # схлопываем повторяющиеся пустые строки
    s = re.sub(r"\n{3,}", "\n\n", s)
    # убираем случайные пробелы в начале/конце
    s = s.strip()
    return s

# ---- LLM-очистка (опционально) ----
LLM_SYSTEM = (
    "Ты — помощник по НОРМАЛИЗАЦИИ юридических текстов РК. "
    "Очисти фрагмент НПА от оглавлений («СОДЕРЖАНИЕ»), издательских примечаний («Примечание ИЗПИ/РЦПИ»), "
    "технических сносок («Сноска.») и лишнего «шума». "
    "Сохрани ТОЛЬКО НОРМАТИВНЫЙ ТЕКСТ: наименования разделов/глав/статей/частей/пунктов и их номера. "
    "Ничего НЕ ВЫДУМЫВАЙ, не сокращай и не перефразируй нормы. Если сомневаешься — оставь фрагмент как есть. "
    "Не добавляй HTML/Markdown, верни чистый текст на русском с исходными переносами строк."
)

def llm_clean_chunk(chunk: str, title: str, max_retries: int = 3, retry_delay: float = 2.0) -> tuple[str, bool]:
    """
    Очищает фрагмент текста через LLM с повторными попытками.
    Возвращает (очищенный_текст, требует_ручной_проверки)
    """
    if not USE_LLM:
        return chunk, False
    
    for attempt in range(max_retries):
        try:
            model = genai.GenerativeModel(LLM_MODEL, system_instruction=LLM_SYSTEM)
            prompt = (
                f"Текст относится к акту: «{title}».\n"
                "Очисти фрагмент ниже и верни чистый нормативный текст без посторонних комментариев.\n"
                "<LAW_CHUNK>\n" + chunk + "\n</LAW_CHUNK>"
            )
            res = model.generate_content(prompt, request_options={"timeout": 60})
            out = (res.text or "").strip()
            if out:
                return out, False  # успешно очищено
            else:
                print(f"[WARN] LLM вернул пустой результат для {title}")
                return chunk, True  # требует проверки
                
        except Exception as e:
            if attempt < max_retries - 1:
                print(f"[WARN] LLM попытка {attempt + 1}/{max_retries} не удалась: {e}")
                import time
                time.sleep(retry_delay * (attempt + 1))  # экспоненциальная задержка
            else:
                print(f"[ERROR] LLM очистка не удалась после {max_retries} попыток: {e}")
                return chunk, True  # требует ручной проверки
    
    return chunk, True  # fallback

def llm_cleanup_full(text: str, title: str, max_chars=8000) -> str:
    # режем на куски, чтобы не упираться в лимиты
    if not USE_LLM:
        return text
    chunks = []
    buf = []
    cur_len = 0
    for line in text.splitlines():
        line_len = len(line) + 1
        if cur_len + line_len > max_chars and buf:
            chunks.append("\n".join(buf))
            buf = [line]
            cur_len = line_len
        else:
            buf.append(line)
            cur_len += line_len
    if buf:
        chunks.append("\n".join(buf))

    cleaned_parts = []
    needs_review = False
    for i, c in enumerate(chunks, 1):
        print(f"[LLM] Очистка части {i}/{len(chunks)} ({len(c)} chars)")
        cleaned_chunk, chunk_needs_review = llm_clean_chunk(c, title)
        cleaned_parts.append(cleaned_chunk)
        if chunk_needs_review:
            needs_review = True
    
    cleaned = "\n".join(cleaned_parts)
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned).strip()
    
    if needs_review:
        print(f"[WARN] Текст '{title}' требует ручной проверки из-за ошибок LLM")
    
    return cleaned

# ---- обновление JSON ----
def upsert_entry(items: List[Dict], title: str, text: str, source: str) -> bool:
    """
    Обновляет или добавляет запись. Возвращает True, если что-то изменилось.
    """
    norm_title = title.strip()
    for it in items:
        if it.get("title", "").strip().lower() == norm_title.lower():
            # сравним по хэшам — чтобы не коммитить каждую мелочь
            old_hash = sha256_text(it.get("text", ""))
            new_hash = sha256_text(text)
            if old_hash != new_hash or it.get("source") != source:
                it["text"] = text
                it["source"] = source
                it["updated_at"] = dt.datetime.utcnow().isoformat() + "Z"
                return True
            return False
    # новая запись
    items.append({
        "title": norm_title,
        "text": text,
        "source": source,
        "updated_at": dt.datetime.utcnow().isoformat() + "Z"
    })
    return True

def main():
    ensure_parent(RAW_LAWS_JSON)
    items = load_json_list(RAW_LAWS_JSON)

    # 1. Загрузка данных из дефолтного списка источников
    sources = DEFAULT_SOURCES.copy()
    print(f"[INFO] Всего локальных источников: {len(sources)}")

    # 2. Загрузка данных с data.egov.kz
    egov_laws = fetch_egov_laws()
    print(f"[INFO] Получено НПА с data.egov.kz: {len(egov_laws)}")
    sources.extend(egov_laws)  # Объединяем источники

    total_changes = 0
    for s in sources:
        title = s.get("title") or ""
        url = s.get("url") or ""
        if not title or not url:
            print(f"[WARN] Пропуск: нет title/url в {s}")
            continue

        print(f"[FETCH] {title} ← {url}")
        html = fetch_url(url)
        if not html:
            print(f"[WARN] Пропуск {title}: не скачалось")
            continue

        # 1) сначала пытаемся найти оглавление/ссылки на статьи
        article_links = extract_article_links_from_toc(html, url)

        if article_links:
            print(f"[INFO] Найдено {len(article_links)} статей в {title}")
            # проходим по ссылкам (лучше с небольшим параллелизмом, но с rate-limit)
            for link_info in article_links:
                art_title = link_info['article_title']
                href = link_info['href']
                is_same = link_info.get('is_same_page', False)

                if is_same or (urlparse(href).netloc == urlparse(url).netloc and urlparse(href).path == urlparse(url).path):
                    # якорь на той же странице — используем исходный html
                    art_text = extract_article_text_by_anchor_or_header(html, link_info['raw_href'], url)
                else:
                    # отдельная страница — скачиваем
                    sub_html = fetch_url(href)
                    if not sub_html:
                        # fallback: пропустить или взять заголовок без текста
                        print(f"[WARN] Не удалось скачать статью {art_title}")
                        continue
                    art_text = extract_article_text_by_anchor_or_header(sub_html, href, href)

                # доп. грубая очистка
                art_text = coarse_cleanup(art_text)

                # опционально: прогнать через LLM очистку (llm_cleanup_full)
                art_text = llm_cleanup_full(art_text, title=art_title) if USE_LLM else art_text

                # Упсерт: сохраняем каждую статью как отдельный элемент (title -> название кодекса, article_title -> название статьи)
                changed = upsert_entry(items, title=f"{title} — {art_title}", text=art_text, source=href)
                if changed:
                    total_changes += 1
                    print(f"[OK] Обновлена статья: {art_title}")
                else:
                    print(f"[OK] Статья без изменений: {art_title}")
        else:
            # fallback: обрабатываем как раньше — весь документ целиком
            print(f"[INFO] Статьи не найдены, обрабатываем весь документ: {title}")
            raw_text = extract_main_text(html)
            step1 = coarse_cleanup(raw_text)
            step2 = llm_cleanup_full(step1, title) if USE_LLM else step1

            changed = upsert_entry(items, title=title, text=step2, source=url)
            if changed:
                total_changes += 1
                print(f"[OK] Обновлено: {title}")
            else:
                print(f"[OK] Без изменений: {title}")

    # сортируем стабильно по title
    items_sorted = sorted(items, key=lambda x: (x.get("title") or "").lower())

    if DRY_RUN:
        print("[DRY] Сохранять не будем (DRY_RUN=true). Изменений:", total_changes)
        return

    # сохраняем — пусть в git определяет, есть ли реальные изменения
    save_json_list(RAW_LAWS_JSON, items_sorted)
    print(f"[DONE] Готово. Изменений: {total_changes}. Файл: {RAW_LAWS_JSON}")

if __name__ == "__main__":
    main()
