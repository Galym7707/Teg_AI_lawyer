import re

def expand_keywords(query: str, synonyms: dict) -> list[str]:
    """Расширяет ключевые слова запроса с использованием словаря синонимов.

    Args:
        query: Исходный запрос пользователя.
        synonyms: Словарь синонимов, где ключ - основное слово, значение - список синонимов.

    Returns:
        Список расширенных ключевых слов.
    """
    expanded = set()
    words = query.lower().split()
    for word in words:
        found = False
        for main_word, syn_list in synonyms.items():
            if word == main_word.lower() or word in [s.lower() for s in syn_list]:
                expanded.add(main_word)
                for s in syn_list:
                    expanded.add(s)
                found = True
                break
        if not found:
            expanded.add(word)
    return list(expanded)

def build_snippet(text: str, keywords: list[str], max_length: int = 500) -> str:
    """Извлекает релевантный сниппет из текста на основе ключевых слов.

    Args:
        text: Полный текст статьи закона.
        keywords: Список ключевых слов для поиска.
        max_length: Максимальная длина сниппета.

    Returns:
        Сниппет текста, содержащий ключевые слова.
    """
    if not keywords:
        return text[:max_length] + "..." if len(text) > max_length else text

    # Создаем регулярное выражение для поиска любого из ключевых слов
    # Использование re.escape для обработки спецсимволов в ключевых словах
    pattern = r"\b(?:" + "|".join(re.escape(k) for k in keywords) + r")\b"
    matches = list(re.finditer(pattern, text, re.IGNORECASE))

    if not matches:
        return text[:max_length] + "..." if len(text) > max_length else text

    best_snippet = ""
    for match in matches:
        start_index = max(0, match.start() - max_length // 2)
        end_index = min(len(text), match.end() + max_length // 2)
        snippet = text[start_index:end_index]

        # Проверяем, что сниппет не начинается или не заканчивается посреди слова
        if start_index > 0 and not text[start_index-1].isspace():
            start_index = text.find(" ", start_index) + 1
        if end_index < len(text) and not text[end_index].isspace():
            end_index = text.rfind(" ", 0, end_index)

        snippet = text[start_index:end_index].strip()

        if len(snippet) > len(best_snippet):
            best_snippet = snippet

    if not best_snippet:
        return text[:max_length] + "..." if len(text) > max_length else text

    return best_snippet + "..." if len(best_snippet) > max_length else best_snippet


