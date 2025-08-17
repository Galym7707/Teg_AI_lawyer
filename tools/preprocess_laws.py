# -*- coding: utf-8 -*-
import os
import re
import json
import logging
from pathlib import Path

logging.basicConfig(level=logging.INFO)
log = logging.getLogger(__name__)

# Константы путей (корректные для корневой структуры)
ROOT = Path(__file__).parent
RAW_LAWS = ROOT / "backend" / "laws" / "kazakh_laws.json"
NORMALIZED_LAWS = ROOT / "backend" / "laws" / "normalized.jsonl"

# Регулярки для обработки
ARTICLE_PAT = re.compile(r"(Статья \d+[^\n]*)\n", re.IGNORECASE)
NOISE_PATTERNS = [r"сноска\..*", r"примечание.*", r"СОДЕРЖАНИЕ.*"]

def clean_text(text: str) -> str:
    """Глубокая очистка текста"""
    for pattern in NOISE_PATTERNS:
        text = re.sub(pattern, "", text, flags=re.DOTALL|re.IGNORECASE)
    return re.sub(r"\n{3,}", "\n\n", text).strip()

def process_law(law: Dict) -> List[Dict]:
    """Обработка одного закона"""
    articles = []
    text = clean_text(law.get("text", ""))
    
    for match in ARTICLE_PAT.finditer(text):
        article = {
            "law_title": law.get("title", "Без названия"),
            "article_title": match.group(1),
            "source": law.get("source", ""),
            "plain_text": text[match.start():match.end() + 2000]  # Берем ~2000 символов после заголовка
        }
        articles.append(article)
    
    return articles or [{
        "law_title": law.get("title", "Без названия"),
        "article_title": "Общие положения",
        "source": law.get("source", ""),
        "plain_text": text[:5000]  # Ограничиваем размер
    }]

def main():
    """Основной конвейер обработки"""
    log.info(f"🔧 Загрузка сырых законов из {RAW_LAWS}")
    
    if not RAW_LAWS.exists():
        raise FileNotFoundError(f"Файл {RAW_LAWS} не найден!")
    
    with open(RAW_LAWS, "r", encoding="utf-8") as f:
        laws = json.load(f)
    
    NORMALIZED_LAWS.parent.mkdir(parents=True, exist_ok=True)
    
    with open(NORMALIZED_LAWS, "w", encoding="utf-8") as out:
        for law in laws:
            for article in process_law(law):
                json.dump(article, out, ensure_ascii=False)
                out.write("\n")
    
    log.info(f"✅ Успешно обработано {len(laws)} законов. Результат в {NORMALIZED_LAWS}")

if __name__ == "__main__":
    main()