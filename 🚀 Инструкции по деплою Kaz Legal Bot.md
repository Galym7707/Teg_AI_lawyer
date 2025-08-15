# 🚀 Инструкции по деплою Kaz Legal Bot

## Пошаговое руководство по развертыванию

### 1. Подготовка к деплою

#### Получение API ключей
1. **Google Gemini API**:
   - Перейдите на [Google AI Studio](https://makersuite.google.com/app/apikey)
   - Создайте новый API ключ
   - Сохраните ключ для использования в бэкенде

2. **MongoDB (опционально)**:
   - Создайте аккаунт на [MongoDB Atlas](https://www.mongodb.com/cloud/atlas)
   - Создайте новый кластер
   - Получите строку подключения

### 2. Деплой бэкенда на Railway

#### Шаг 1: Подготовка репозитория
1. Создайте репозиторий на GitHub
2. Загрузите все файлы проекта
3. Убедитесь, что файл `backend/laws/kazakh_laws.json` содержит данные

#### Шаг 2: Настройка Railway
1. Зайдите на [Railway.app](https://railway.app)
2. Войдите через GitHub
3. Нажмите "New Project" → "Deploy from GitHub repo"
4. Выберите ваш репозиторий
5. В настройках проекта:
   - **Root Directory**: `backend`
   - **Start Command**: `python app.py`

#### Шаг 3: Переменные окружения Railway
Добавьте следующие переменные в настройках Railway:

```
GEMINI_API_KEY=your_actual_gemini_api_key
MONGO_URI=your_mongodb_connection_string (опционально)
CORS_ORIGINS=https://your-frontend-domain.vercel.app
PORT=5000
```

#### Шаг 4: Деплой
1. Railway автоматически развернет ваш бэкенд
2. Скопируйте URL вашего бэкенда (например: `https://your-app.railway.app`)

### 3. Деплой фронтенда на Vercel

#### Шаг 1: Настройка Vercel
1. Зайдите на [Vercel.com](https://vercel.com)
2. Войдите через GitHub
3. Нажмите "New Project"
4. Выберите ваш репозиторий
5. В настройках проекта:
   - **Framework Preset**: Other
   - **Root Directory**: `frontend`
   - **Build Command**: (оставьте пустым)
   - **Output Directory**: (оставьте пустым)

#### Шаг 2: Переменные окружения Vercel
Добавьте переменную окружения:

```
BACKEND_URL=https://your-app.railway.app
```

#### Шаг 3: Деплой
1. Нажмите "Deploy"
2. Vercel автоматически развернет ваш фронтенд
3. Получите URL фронтенда (например: `https://your-app.vercel.app`)

### 4. Финальная настройка

#### Обновление CORS в бэкенде
1. Вернитесь в настройки Railway
2. Обновите переменную `CORS_ORIGINS`:
```
CORS_ORIGINS=https://your-app.vercel.app,http://localhost:3000
```
3. Перезапустите бэкенд

### 5. Проверка работы

1. Откройте ваш фронтенд по URL Vercel
2. Попробуйте задать вопрос
3. Убедитесь, что ответы приходят в реальном времени

## 🔧 Альтернативные варианты деплоя

### Бэкенд

#### Heroku
```bash
# Создайте Procfile в папке backend
echo "web: python app.py" > Procfile

# Деплой через Heroku CLI
heroku create your-app-name
heroku config:set GEMINI_API_KEY=your_key
git subtree push --prefix backend heroku main
```

#### DigitalOcean App Platform
1. Создайте новое приложение
2. Подключите GitHub репозиторий
3. Выберите папку `backend`
4. Настройте переменные окружения

### Фронтенд

#### Netlify
1. Подключите репозиторий к Netlify
2. Установите:
   - **Base directory**: `frontend`
   - **Build command**: (пусто)
   - **Publish directory**: `frontend`
3. Добавьте переменную `BACKEND_URL`

#### GitHub Pages (только для статики)
```bash
# В папке frontend
git subtree push --prefix frontend origin gh-pages
```

## 🐛 Решение проблем

### Ошибка CORS
**Проблема**: Фронтенд не может подключиться к бэкенду

**Решение**:
1. Убедитесь, что URL фронтенда добавлен в `CORS_ORIGINS`
2. Проверьте, что `BACKEND_URL` правильно настроен в Vercel

### Ошибка API ключа
**Проблема**: "GEMINI_API_KEY не установлен"

**Решение**:
1. Проверьте, что API ключ добавлен в переменные Railway
2. Убедитесь, что ключ валиден и активен

### Ошибка MongoDB
**Проблема**: Не удается подключиться к базе данных

**Решение**:
1. MongoDB не обязательна - приложение работает без неё
2. Проверьте строку подключения `MONGO_URI`
3. Убедитесь, что IP адрес Railway добавлен в whitelist MongoDB

### Медленные ответы
**Проблема**: AI отвечает слишком медленно

**Решение**:
1. Проверьте лимиты Gemini API
2. Убедитесь, что бэкенд не "засыпает" (Railway может усыплять неактивные приложения)

## 📊 Мониторинг

### Railway
- Логи доступны в панели Railway
- Метрики использования ресурсов
- Автоматические бэкапы

### Vercel
- Analytics доступны в панели Vercel
- Логи функций
- Метрики производительности

## 🔄 Обновления

### Обновление бэкенда
1. Внесите изменения в код
2. Сделайте commit и push в GitHub
3. Railway автоматически пересоберет приложение

### Обновление фронтенда
1. Внесите изменения в код
2. Сделайте commit и push в GitHub
3. Vercel автоматически пересоберет приложение

## 💰 Стоимость

### Railway
- Бесплатный план: 500 часов в месяц
- Pro план: $5/месяц за проект

### Vercel
- Hobby план: Бесплатно для личных проектов
- Pro план: $20/месяц для коммерческих проектов

### Google Gemini API
- Бесплатный лимит: 15 запросов в минуту
- Платные планы: от $0.00025 за 1K символов

---

**Готово!** Ваш Kaz Legal Bot теперь доступен в интернете 🎉

