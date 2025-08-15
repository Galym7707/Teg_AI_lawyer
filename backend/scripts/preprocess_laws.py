# -*- coding: utf-8 -*-
import os
import re
import json
import argparse
from typing import List, Dict, Iterable, Tuple

# optional LLM summarization
USE_GEMINI = False
try:
    import google.generativeai as genai
    if os.getenv("GEMINI_API_KEY"):
        genai.configure(api_key=os.getenv("GEMINI_API_KEY"))
        USE_GEMINI = True
except Exception:
    USE_GEMINI = False


ARTICLE_PAT = re.compile(r"(статья\s+\d+[^\n]*?)\n", re.IGNORECASE)
CUTLINES = (
    "сноска.", "примечание", "оглавление", "содержание", "примечание ицпи", "примечание изпи",
)

def _clean_lines(text: str) -> str:
    lines = [l.rstrip() for l in text.splitlines()]
    out = []
    for ln in lines:
        ln_stripped = ln.strip()
        low = ln_stripped.lower()
        if not ln_stripped:
            out.append("")
            continue
        # фильтруем мусорные строки и сервисные ремарки
        if any(low.startswith(x) for x in CUTLINES):
            continue
        # убираем множественные пробелы
        ln_stripped = re.sub(r"\s+", " ", ln_stripped)
        out.append(ln_stripped)
    # склеиваем, остаются одинарные \n
    text = "\n".join(out)
    # двойные пробелы
    text = re.sub(r" {2,}", " ", text).strip()
    return text

def _split_articles(title: str, text: str) -> List[Tuple[str, str]]:
    """
    Возвращает [(article_title, article_body), ...]
    Если паттерн «Статья N» не находит — вернём всю вещь как один блок.
    """
    t = _clean_lines(text)
    # ищем маркеры «Статья N»
    matches = list(ARTICLE_PAT.finditer(t))
    if not matches:
        return [(title or "Без названия", t)]
    parts = []
    for i, m in enumerate(matches):
        start = m.start()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(t)
        block = t[start:end].strip()
        # заголовок = первая строка блока
        first_nl = block.find("\n")
        art_title = block[:first_nl] if first_nl > 0 else block
        art_body = block[first_nl + 1 :] if first_nl > 0 else ""
        parts.append((art_title.strip(), art_body.strip()))
    return parts

def _summarize(text: str) -> str:
    if not USE_GEMINI:
        return ""
    model = genai.GenerativeModel(
        "gemini-1.5-flash",
        generation_config={"temperature": 0.2, "max_output_tokens": 256},
        system_instruction=(
            "Суммаризируй юридический фрагмент на простом русском для непрофессионала. "
            "Формат: краткий абзац и 3–6 пунктов. Без Markdown, только обычный текст."
        )
    )
    txt = text
    if len(txt) > 2000:
        txt = txt[:2000] + "…"
    prompt = f"Текст нормы:\n{txt}\n\nСделай краткое резюме + пункты."
    try:
        r = model.generate_content(prompt)
        return (r.text or "").strip()
    except Exception:
        return ""

def process(laws_path: str, out_path: str, do_summarize: bool):
    with open(laws_path, "r", encoding="utf-8") as f:
        data = json.load(f)

    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    out = open(out_path, "w", encoding="utf-8")

    total_art = 0
    for obj in data:
        title = (obj.get("title") or "").strip() or "Без названия"
        source = (obj.get("source") or "").strip()
        text = obj.get("text") or ""
        if not text.strip():
            continue

        articles = _split_articles(title, text)
        for art_title, art_body in articles:
            plain = _clean_lines(art_body or "")
            if not plain:
                continue
            rec = {
                "law_title": title,
                "article_title": art_title,
                "source": source,
                "plain_text": plain
            }
            if do_summarize:
                rec["plain_summary"] = _summarize(plain)
            out.write(json.dumps(rec, ensure_ascii=False) + "\n")
            total_art += 1

    out.close()
    print(f"✅ Готово: {total_art} нормализованных фрагментов → {out_path}")

if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--in", dest="infile", default="laws/kazakh_laws.json")
    ap.add_argument("--out", dest="outfile", default="laws/normalized.jsonl")
    ap.add_argument("--summarize", action="store_true", help="Предварительно сделать короткие резюме через Gemini")
    args = ap.parse_args()
    process(args.infile, args.outfile, args.summarize)
