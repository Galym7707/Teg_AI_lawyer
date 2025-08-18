import os
from dotenv import load_dotenv
import logging
import google.generativeai as genai

# Set up logging
logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
log = logging.getLogger(__name__)

def test_gemini_key():
    """Test if GEMINI_API_KEY is set and can be used with google-generativeai."""
    env_path = os.path.join(os.path.dirname(__file__), '.env')
    log.info(f"🔍 Проверяем .env файл по пути: {env_path}")
    if not os.path.exists(env_path):
        log.error(f"❌ Файл .env не найден по пути: {env_path}")
        return False
    
    loaded = load_dotenv(env_path)
    log.info(f"✅ Результат загрузки .env: {loaded}")
    
    api_key = os.getenv("GEMINI_API_KEY")
    if not api_key:
        log.error("❌ GEMINI_API_KEY не задан")
        return False
    
    log.info("✅ GEMINI_API_KEY найден: %s", api_key[:4] + "..." + api_key[-4:])
    
    try:
        genai.configure(api_key=api_key)
        log.info("✅ Gemini API успешно сконфигурирован")
        model = genai.GenerativeModel('gemini-1.5-pro')
        response = model.generate_content("Тестовый запрос: что такое закон?")
        log.info("✅ Ответ от Gemini: %s", response.text[:100] + "...")
        return True
    except Exception as e:
        log.error("❌ Ошибка при обращении к Gemini API: %s", str(e))
        return False

if __name__ == "__main__":
    log.info("🔄 Запуск теста GEMINI_API_KEY")
    test_gemini_key()