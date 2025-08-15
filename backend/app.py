"""Основной веб‑сервер для Kaz Legal Bot.

Этот модуль реализует API, позволяющий отправлять текстовые запросы
искусственному интеллекту, загружать документы для анализа,
получать историю переписки и список существующих сессий. Код
содержит несколько исправлений по сравнению с исходной версией:

* Исправлена CORS‑обработка ``OPTIONS`` для произвольных путей.
* ``system_instruction`` формируется как f‑строка, чтобы включать
  динамический контекст с релевантными законами.
* Исправлена обработка исключений при загрузке изображений (импорт
  ``UnidentifiedImageError``).
* В ``clean_and_format_html`` добавлена проверка наличия
  предыдущих элементов, чтобы избежать ``IndexError``.
"""


from flask import Flask, request, jsonify, Response, stream_with_context, make_response
import google.generativeai as genai
import os
import json
import re
import bleach
from concurrent.futures import ThreadPoolExecutor
from PIL import Image, UnidentifiedImageError
import io
from docx import Document
from PyPDF2 import PdfReader
from PyPDF2.errors import PdfReadError
import logging
from lxml import html
from dotenv import load_dotenv
from helpers.helpers import expand_keywords, build_snippet
# jamspell is optional. It requires a C++ build toolchain (SWIG/gcc) which may be
# unavailable in some deployment environments (e.g., Railway). Attempt to import
# jamspell and fall back to None if it cannot be imported. The rest of the code
# handles the missing spell‑corrector gracefully.
try:
    import jamspell  # type: ignore
except ImportError:
    jamspell = None
import unittest

def build_law_index():
    """Строит инвертированный индекс для быстрого поиска по законам."""
    global LAW_INDEX
    LAW_INDEX = {}
    for i, law_article in enumerate(LAW_DB):
        text = law_article.get("text", "") + " " + law_article.get("title", "")
        words = re.findall(r'\b\w+\b', text.lower())
        for word in words:
            if word not in LAW_INDEX:
                LAW_INDEX[word] = []
            if i not in LAW_INDEX[word]:
                LAW_INDEX[word].append(i)
    logging.info("✅ Индекс законов успешно построен.")

def validate_session_id(session_id: str) -> bool:
    """Проверяет валидность session_id."""
    return bool(re.match(r'^[a-zA-Z0-9_-]{1,50}$', session_id))

# Загрузка переменных окружения из .env
load_dotenv()

# Настройка логирования
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

app = Flask(__name__)
# Ограничиваем размер загружаемых файлов (по умолчанию 16 МБ)
app.config["MAX_CONTENT_LENGTH"] = int(os.getenv("MAX_CONTENT_LENGTH", 16 * 1024 * 1024))

# Настройка CORS
cors_origins = os.getenv("CORS_ORIGINS", "https://ai-lawyer-tau.vercel.app,http://localhost:5000,http://127.0.0.1:5000,http://localhost:3000").split(",")
logging.info(f"✅ CORS configured for origins: {cors_origins}")

def add_cors_headers(response):
    """Добавляет CORS‑заголовки к ответу."""
    origin = request.headers.get("Origin", "")
    # Поддержка file:// протокола для локального тестирования
    if origin in cors_origins or origin == "null":
       response.headers["Access-Control-Allow-Origin"] = origin if origin != "null" else "*"
    else:
        # если запрашивающий origin неизвестен, используем первый разрешённый
        response.headers["Access-Control-Allow-Origin"] = cors_origins[0]
    response.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
    response.headers["Access-Control-Allow-Headers"] = "Content-Type, Authorization"
    response.headers["Access-Control-Allow-Credentials"] = "true"
    response.headers["Access-Control-Max-Age"] = "86400"
    logging.info(f"Response headers: {response.headers}")
    return response

@app.after_request
def apply_cors(response):
    """Функция‑обертка, вызываемая после каждого запроса, чтобы
    автоматически добавлять CORS‑заголовки."""
    return add_cors_headers(response)

@app.route("/<path:path>", methods=["OPTIONS"])
def handle_options(path):
    """Обрабатывает предварительные CORS‑запросы для любых путей."""
    response = make_response()
    return add_cors_headers(response)

# Инициализация AI и базы законов
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "")
if not GEMINI_API_KEY:
    logging.error("❌ GEMINI_API_KEY не установлен. Приложение не может запуститься.")
    raise EnvironmentError("GEMINI_API_KEY is not set.")
genai.configure(api_key=GEMINI_API_KEY)
model = genai.GenerativeModel('gemini-1.5-flash', generation_config={"response_mime_type": "text/plain", "temperature": 0.7})
vision_model = genai.GenerativeModel("gemini-1.5-flash")

# Инициализация JamSpell для коррекции текста.
# Если jamspell не установлен или ru.bin отсутствует, отключаем коррекцию.
if jamspell is not None:
    try:
        _jsp = jamspell.TSpellCorrector()
        if _jsp.LoadLangModel("ru.bin"):
            jsp = _jsp
            logging.info("✅ Модель JamSpell успешно загружена.")
        else:
            logging.warning("⚠️ Файл ru.bin не найден. Орфографическая коррекция отключена.")
            jsp = None
    except Exception as e:
        logging.warning(f"⚠️ Ошибка при загрузке JamSpell: {e}. Орфографическая коррекция отключена.")
        jsp = None
else:
    logging.warning("⚠️ Библиотека jamspell не установлена. Орфографическая коррекция отключена.")
    jsp = None

LAW_DB: list = []
LAW_INDEX: dict = {}
LEGAL_SYNONYMS = {
    'увольнение': ['уволен', 'увольняет', 'сокращение', 'расторжение договора', 'прекращение трудового договора', 'расчет', 'увольнение'],
    'отпуск': ['отпускные', 'ежегодный отпуск', 'трудовой отпуск', 'больничный', 'декретный отпуск'],
    'зарплата': ['заработная плата', 'оплата труда', 'выплата', 'аванс', 'расчет', 'оклад', 'премия'],
    'трудовой договор': ['трудовой контракт', 'договор', 'соглашение о труде', 'контракт'],
    'работодатель': ['компания', 'фирма', 'предприятие', 'начальник', 'руководство', 'организация'],
    'работник': ['сотрудник', 'персонал', 'служащий', 'подчиненный'],
    'ИП': ['индивидуальный предприниматель', 'предприниматель', 'ИПшник', 'частник'],
    'УСН': ['упрощенная система налогообложения', 'упрощенка'],
    'налог': ['налоги', 'налоговый', 'сбор', 'пошлина', 'НДС', 'КПН', 'ИПН', 'социальный налог', 'отчисления', 'взносы'],
    'ЕНП': ['единый совокупный платеж'],
    'патент': ['специальный налоговый режим на основе патента'],
    'декларация': ['налоговая декларация', 'отчетность'],
    'срок': ['сроки', 'период', 'дата'],
    'штраф': ['пени', 'взыскание'],
    'развод': ['расторжение брака', 'развод', 'алименты', 'раздел имущества'],
    'брак': ['женитьба', 'семейный союз', 'супружество'],
    'алименты': ['выплаты на ребенка', 'содержание'],
    'имущество': ['недвижимость', 'активы', 'собственность'],
    'кража': ['хищение', 'воровство'],
    'мошенничество': ['обман', 'афера'],
    'преступление': ['правонарушение', 'уголовное дело'],
    'наказание': ['срок', 'тюрьма', 'штраф', 'лишение свободы'],
    'нарушение': ['проступок', 'правонарушение'],
    'протокол': ['административный протокол'],
    'договор': ['контракт', 'соглашение'],
    'возмещение ущерба': ['компенсация', 'возмещение убытков'],
    'иск': ['исковое заявление', 'судебный иск'],
    'собственность': ['право собственности', 'имущество'],
    'закон': ['кодекс', 'нормативный акт', 'постановление', 'правила'],
    'статья': ['пункт', 'часть', 'подпункт'],
    'суд': ['судебный орган', 'правосудие', 'истец', 'ответчик'],
    'жалоба': ['обращение', 'заявление', 'петиция'],
    'консультация': ['совет', 'помощь', 'разъяснение'],
    'документ': ['бумага', 'справка', 'акт', 'удостоверение'],
    # Дополнение для всех кодексов Казахстана
    'преступление': ['правонарушение', 'уголовное дело', 'деяние', 'злодеяние'],
    'наказание': ['санкция', 'взыскание', 'кара', 'репрессия'],
    'кража': ['хищение', 'воровство', 'грабёж', 'разбой'],
    'мошенничество': ['обман', 'афера', 'подлог', 'фальсификация'],
    'убийство': ['умышленное убийство', 'неосторожное убийство', 'покушение на убийство'],
    'насилие': ['физическое насилие', 'психологическое насилие', 'сексуальное насилие'],
    'следствие': ['расследование', 'дознание', 'предварительное следствие'],
    'дознание': ['следствие', 'расследование', 'предварительное дознание'],
    'судебный процесс': ['судебное разбирательство', 'суд', 'процесс'],
    'доказательства': ['улики', 'свидетельства', 'материалы дела'],
    'приговор': ['решение суда', 'вердикт', 'постановление'],
    'договор': ['контракт', 'соглашение', 'пакт', 'договорённость'],
    'собственность': ['имущество', 'владение', 'право собственности'],
    'обязательство': ['долг', 'ответственность', 'обязанность'],
    'право': ['законное право', 'юридическое право', 'привилегия'],
    'сделка': ['операция', 'транзакция', 'соглашение'],
    'иск': ['исковое заявление', 'судебный иск', 'претензия'],
    'судебное разбирательство': ['суд', 'процесс', 'слушание'],
    'решение суда': ['приговор', 'постановление', 'вердикт'],
    'апелляция': ['обжалование', 'апелляционная жалоба', 'вторая инстанция'],
    'кассация': ['кассационная жалоба', 'третья инстанция', 'надзор'],
    'административное правонарушение': ['административный проступок', 'нарушение'],
    'штраф': ['денежное взыскание', 'пени', 'санкция'],
    'административный арест': ['задержание', 'арест', 'лишение свободы'],
    'протокол': ['административный протокол', 'документ', 'акт'],
    'трудовой договор': ['контракт', 'соглашение о труде', 'договор'],
    'работодатель': ['наниматель', 'компания', 'организация'],
    'работник': ['сотрудник', 'служащий', 'персонал'],
    'зарплата': ['оплата труда', 'заработная плата', 'вознаграждение'],
    'отпуск': ['каникулы', 'отдых', 'трудовой отпуск'],
    'увольнение': ['прекращение трудового договора', 'расторжение контракта'],
    'налог': ['сбор', 'пошлина', 'обязательный платёж'],
    'налоговая декларация': ['отчётность', 'декларация о доходах'],
    'НДС': ['налог на добавленную стоимость', 'НДС'],
    'КПН': ['корпоративный подоходный налог', 'КПН'],
    'ИПН': ['индивидуальный подоходный налог', 'ИПН'],
    'социальный налог': ['соцналог', 'отчисления'],
    'медицинская помощь': ['лечение', 'уход', 'медицинские услуги'],
    'здравоохранение': ['медицина', 'здравоохранение'],
    'пациент': ['больной', 'клиент', 'пациент'],
    'врач': ['доктор', 'медик', 'специалист'],
    'лекарство': ['препарат', 'медикамент', 'средство'],
    'недра': ['ресурсы', 'ископаемые', 'полезные ископаемые'],
    'добыча': ['извлечение', 'разработка', 'эксплуатация'],
    'ресурсы': ['природные ресурсы', 'запасы', 'богатства'],
    'лицензия': ['разрешение', 'право', 'сертификат'],
    'контракт': ['договор', 'соглашение', 'пакт'],
    'жилье': ['квартира', 'дом', 'недвижимость'],
    'аренда': ['наём', 'прокат', 'аренда'],
    'собственность': ['владение', 'право собственности', 'имущество'],
    'бюджет': ['финансовый план', 'смета', 'бюджет'],
    'расходы': ['затраты', 'издержки', 'траты'],
    'доходы': ['прибыль', 'заработок', 'выручка'],
    'дефицит': ['недостаток', 'дефицит', 'недостача'],
    'финансирование': ['денежное обеспечение', 'финансирование'],
    'таможня': ['таможенный контроль', 'таможенный пост'],
    'импорт': ['ввоз', 'импорт'],
    'экспорт': ['вывоз', 'экспорт'],
    'пошлина': ['таможенная пошлина', 'налог'],
    'предприниматель': ['бизнесмен', 'делец', 'коммерсант'],
    'бизнес': ['предпринимательство', 'дело', 'коммерция'],
    'компания': ['фирма', 'организация', 'предприятие'],
    'регистрация': ['оформление', 'запись', 'регистрация'],
    'выборы': ['голосование', 'избрание', 'выборы'],
    'кандидат': ['претендент', 'участник', 'кандидат'],
    'избиратель': ['голосующий', 'электорат', 'избиратель'],
    'бюллетень': ['избирательный бюллетень', 'голосовательный лист'],
    'брак': ['супружество', 'семейный союз', 'брак'],
    'развод': ['расторжение брака', 'развод'],
    'алименты': ['выплаты на ребенка', 'содержание'],
    'опека': ['попечительство', 'забота', 'опека'],
    'усыновление': ['удочерение', 'принятие в семью', 'усыновление'],
    'экология': ['окружающая среда', 'природа', 'экология'],
    'загрязнение': ['заражение', 'загрязнение', 'отравление'],
    'охрана природы': ['защита природы', 'природоохранная деятельность'],
    'военная служба': ['служба в армии', 'военная обязанность'],
    'военнослужащий': ['солдат', 'офицер', 'военный'],
    'призыв': ['мобилизация', 'набор', 'призыв'],
    'контракт': ['договор', 'соглашение', 'пакт'],
    'звание': ['ранг', 'чин', 'звание'],
}





executor = ThreadPoolExecutor(max_workers=4)

def load_law_db(path: str = "laws/kazakh_laws.json") -> None:
    """Загружает базу данных законов из файла и строит индекс."""
    global LAW_DB
    if os.path.exists(path):
        with open(path, 'r', encoding='utf-8') as f:
            LAW_DB = json.load(f)
        logging.info(f"✅ Загружено {len(LAW_DB)} статей из базы законов.")
        build_law_index()
    else:
        logging.warning(f"⚠️ База законов не найдена по пути: {path}. Поиск будет ограничен.")


# Загрузить базу законов при старте
load_law_db()



def clean_and_format_html(text: str) -> str:
    """Преобразует сырой текст с маркерами SECTION и LIST_ITEM в структурированный HTML."""
    # Убираем лишние пустые строки
    text = re.sub(r'\s*\n\s*\n\s*', '\n\n', text).strip()
    
    # заменяем **жирный** и *курсив* на HTML, чтобы избавить вывод от звёздочек
    text = re.sub(r'\*\*(.*?)\*\*', r'<strong>\1</strong>', text)
    text = re.sub(r'\*(.*?)\*', r'<em>\1</em>', text)
  
    # Орфография (если есть jamspell)
    if jsp is not None:
        try:
            text = jsp.FixFragment(text)
        except Exception as e:
            logging.warning(f"⚠️ Ошибка JamSpell: {e}. Продолжаем без исправления.")

    lines = text.split('\n\n')
    formatted: list[str] = []
    in_list = False
    last_section = ''

    # Заголовки
    expected_sections = {
        'юридическая оценка': 'Юридическая оценка ситуации',
        'действие': 'Действие',
        'рекомендации': 'Рекомендации',
        'необходимая информация': 'Необходимая информация',
        'экстренные контакты': 'Экстренные контакты',
        'релевантные законы': 'Релевантные законы',
    }

    # Переименование меток в различных разделах
    recommendations_labels = {
        'напишите работодателю': 'Письменное требование',
        'обратитесь в территориальное': 'Обращение в инспекцию труда',
        'подготовьте исковое': 'Исковое заявление',
        'собирайте все': 'Документы',
        'сообщите о случившемся': 'Уведомление родителей',
        'обратитесь в полицию': 'Обращение в полицию',
        'обратитесь в медицинское учреждение': 'Медицинский осмотр',
        'сохраните все доказательства': 'Сбор доказательств',
        'по возможности соберите': 'Свидетельские показания',
        'рассмотрите возможность': 'Жалоба в органы образования',
    }
    info_labels = {
        'ваш трудовой договор': 'Трудовой договор',
        'точная сумма задолженности': 'Сумма задолженности',
        'дата последней выплаты': 'Дата последней выплаты',
        'наличие каких-либо соглашений': 'Соглашения о задержке',
        'причины задержки': 'Причины задержки',
        'подробное описание инцидента': 'Описание инцидента',
        'степень тяжести полученных травм': 'Степень травм',
        'свидетели': 'Свидетели',
        'данные об учителе': 'Данные об учителе',
        'данные о школе': 'Данные о школе',
    }

    for raw_line in lines:
        line = raw_line.strip()
        if not line:
            continue

        # Заголовок раздела
        if line.lower().startswith('section:') or line.lower() in expected_sections:
            if in_list:
                formatted.append('</ul>')
                in_list = False

            heading = line.replace('SECTION:', '').strip()
            human_heading = expected_sections.get(heading.lower(), heading)
            formatted.append(f'<h3>{human_heading}</h3>')
            last_section = heading.lower()

            # Пояснительные тексты для разделов
            if last_section == 'юридическая оценка':
                formatted.append('<p><em>Это предварительная юридическая оценка. Для получения точной информации обратитесь к юристу.</em></p>')
            elif last_section == 'действие':
                formatted.append('<p><em>Рекомендуемые шаги, которые вы можете предпринять.</em></p>')
            elif last_section == 'рекомендации':
                formatted.append('<p><em>Пошаговые рекомендации для решения вашей ситуации.</em></p>')
            elif last_section == 'необходимая информация':
                formatted.append('<p><em>Какая информация потребуется для дальнейших действий.</em></p>')
            elif last_section == 'экстренные контакты':
                formatted.append('<p><em>Куда обратиться в экстренных случаях.</em></p>')
            elif last_section == 'релевантные законы':
                formatted.append('<p><em>Выдержки из законодательства, относящиеся к вашей ситуации.</em></p>')

        # Элементы списка
        elif line.lower().startswith('list_item:'):
            if not in_list:
                formatted.append('<ul>')
                in_list = True
            item_text = line.replace('LIST_ITEM:', '').strip()

            # Переименование меток в зависимости от раздела
            if last_section == 'рекомендации':
                item_text = recommendations_labels.get(item_text.lower(), item_text)
            elif last_section == 'необходимая информация':
                item_text = info_labels.get(item_text.lower(), item_text)

            formatted.append(f'<li>{item_text}</li>')

        # Обычный текст
        else:
            if in_list:
                formatted.append('</ul>')
                in_list = False
            formatted.append(f'<p>{line}</p>')

    if in_list:
        formatted.append('</ul>')

    return "\n".join(formatted)


@app.route("/chat", methods=["POST"])
def chat():
    """Обрабатывает запросы чата, взаимодействует с Gemini API и возвращает стриминговый ответ."""
    data = request.json
    user_message = data.get("message")
    session_id = data.get("session_id")

    if not user_message:
        return jsonify({"error": "Сообщение не может быть пустым"}), 400
    if not session_id or not validate_session_id(session_id):
        return jsonify({"error": "Недопустимый session_id"}), 400

    logging.info(f"Получено сообщение от {session_id}: {user_message}")

    # Коррекция орфографии, если JamSpell доступен
    if jsp is not None:
        try:
            user_message = jsp.FixFragment(user_message)
            logging.info(f"Сообщение после коррекции: {user_message}")
        except Exception as e:
            logging.warning(f"⚠️ Ошибка JamSpell при коррекции сообщения: {e}")

    # Поиск релевантных законов
    relevant_laws = []
    if LAW_DB:
        keywords = expand_keywords(user_message, LEGAL_SYNONYMS)
        logging.info(f"Ключевые слова для поиска: {keywords}")
        found_indices = set()
        for keyword in keywords:
            if keyword in LAW_INDEX:
                found_indices.update(LAW_INDEX[keyword])
        
        # Сортируем по релевантности (например, по количеству совпадений или просто по индексу)
        sorted_indices = sorted(list(found_indices), key=lambda x: LAW_DB[x].get("relevance", 0), reverse=True)
        
        # Ограничиваем количество релевантных законов
        for idx in sorted_indices[:5]:  # Берем до 5 самых релевантных
            law_article = LAW_DB[idx]
            snippet = build_snippet(user_message, law_article.get("text", ""))
            relevant_laws.append(f"\n\n---Закон---\nНазвание: {law_article.get('title', 'Неизвестно')}\nТекст: {snippet}\n---\n")

    system_instruction = (
        "Ты — Kaz Legal Bot, эксперт по законодательству Казахстана. "
        "Твоя задача — предоставлять точные и полезные юридические консультации, "
        "основанные исключительно на законодательстве Республики Казахстан. "
        "Отвечай на русском языке. "
        "Если вопрос выходит за рамки законодательства Казахстана или твоей компетенции, "
        "вежливо сообщи об этом и предложи обратиться к квалифицированному юристу. "
        "Структурируй свои ответы, используя следующие разделы, если это применимо: "
        "SECTION: Юридическая оценка ситуации, SECTION: Действие, SECTION: Рекомендации, "
        "SECTION: Необходимая информация, SECTION: Экстренные контакты, SECTION: Релевантные законы. "
        "Используй LIST_ITEM: для каждого пункта в списках. "
        "Форматируй текст с помощью **жирного** и *курсива* для выделения ключевых моментов. "
        "\n\n" +
        (f"Релевантные законы для контекста:\n{''.join(relevant_laws)}\n" if relevant_laws else "") +
        "\n\n" +
        "Пример ответа:\n" +
        "SECTION: Юридическая оценка ситуации\n" +
        "<Оценка ситуации на основе законодательства РК>\n" +
        "SECTION: Действие\n" +
        "LIST_ITEM: <Первое действие>\n" +
        "LIST_ITEM: <Второе действие>\n" +
        "SECTION: Рекомендации\n" +
        "LIST_ITEM: <Первая рекомендация>\n" +
        "LIST_ITEM: <Вторая рекомендация>\n" +
        "SECTION: Релевантные законы\n" +
        "LIST_ITEM: <Название закона, статья>\n" +
        "LIST_ITEM: <Название закона, статья>\n"
    )

    chat_session = model.start_chat(history=[])

    def generate_content_stream():
        full_response_content = ""
        try:
            # Отправляем системную инструкцию и сообщение пользователя
            response_stream = chat_session.send_message(system_instruction + user_message, stream=True)
            for chunk in response_stream:
                if chunk.text:
                    full_response_content += chunk.text
                    yield clean_and_format_html(full_response_content)
        except Exception as e:
            logging.error(f"❌ Ошибка при генерации контента: {e}")
            yield clean_and_format_html(f"Произошла ошибка при обработке вашего запроса: {e}")

    return Response(stream_with_context(generate_content_stream()), mimetype="text/html")


@app.route("/upload_document", methods=["POST"])
def upload_document():
    """Загружает и обрабатывает документ (PDF, DOCX, TXT) для анализа."""
    if "file" not in request.files:
        return jsonify({"error": "Файл не найден"}), 400

    file = request.files["file"]
    if file.filename == "":
        return jsonify({"error": "Файл не выбран"}), 400

    if file:
        filename = file.filename
        file_extension = os.path.splitext(filename)[1].lower()
        text_content = ""

        try:
            if file_extension == ".pdf":
                reader = PdfReader(file.stream)
                for page in reader.pages:
                    text_content += page.extract_text() or ""
            elif file_extension == ".docx":
                document = Document(file.stream)
                for para in document.paragraphs:
                    text_content += para.text + "\n"
            elif file_extension == ".txt":
                text_content = file.stream.read().decode("utf-8")
            else:
                return jsonify({"error": "Неподдерживаемый формат файла. Поддерживаются PDF, DOCX, TXT."}), 400

            # Здесь можно добавить логику для отправки text_content в Gemini API для анализа
            # Например, сохранить в базу данных или сразу отправить в модель
            logging.info(f"Документ {filename} успешно загружен и обработан. Размер текста: {len(text_content)} символов.")
            return jsonify({"message": "Документ успешно загружен и обработан", "content_length": len(text_content)}), 200

        except PdfReadError:
            return jsonify({"error": "Не удалось прочитать PDF файл. Возможно, он поврежден или защищен."}), 400
        except Exception as e:
            logging.error(f"Ошибка при обработке документа: {e}")
            return jsonify({"error": f"Ошибка при обработке документа: {e}"}), 500


@app.route("/upload_image", methods=["POST"])
def upload_image():
    """Загружает и обрабатывает изображение для анализа с помощью vision_model."""
    if "file" not in request.files:
        return jsonify({"error": "Изображение не найдено"}), 400

    file = request.files["file"]
    if file.filename == "":
        return jsonify({"error": "Изображение не выбрано"}), 400

    if file:
        try:
            image_bytes = file.read()
            image = Image.open(io.BytesIO(image_bytes))

            # Отправка изображения в vision_model
            # Здесь можно добавить текстовый запрос к изображению, если нужно
            prompt_parts = [
                image,
                "Опиши, что изображено на картинке, и если это документ, извлеки ключевую информацию."
            ]
            response = vision_model.generate_content(prompt_parts)
            logging.info(f"Изображение успешно обработано. Ответ AI: {response.text[:100]}...")
            return jsonify({"message": "Изображение успешно обработано", "ai_response": response.text}), 200
        except UnidentifiedImageError:
            return jsonify({"error": "Не удалось распознать формат изображения. Убедитесь, что это действительное изображение."}), 400
        except Exception as e:
            logging.error(f"Ошибка при обработке изображения: {e}")
            return jsonify({"error": f"Ошибка при обработке изображения: {e}"}), 500


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=os.getenv("PORT", 5000))