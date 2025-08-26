import logging
import csv
import sqlite3
import json
import os
import traceback
import random
import time
import sys
from datetime import datetime, timedelta
from telegram import Update, KeyboardButton, ReplyKeyboardMarkup, WebAppInfo, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ApplicationBuilder, CommandHandler, MessageHandler, ContextTypes, filters, CallbackQueryHandler
from openai import OpenAI
from yookassa import Configuration, Payment
import uuid
import httpx
import asyncio

# Загрузка переменных окружения
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

os.environ.pop("HTTP_PROXY", None)
os.environ.pop("HTTPS_PROXY", None)
os.environ.pop("http_proxy", None)
os.environ.pop("https_proxy", None)


# Импорт интерпретаций таро
try:
    from tarot_interpretations import TAROT_DAY_INTERPRETATIONS, CONSULTATION_BLOCK
except ImportError:
    TAROT_DAY_INTERPRETATIONS = {}
    CONSULTATION_BLOCK = "💫 Для получения более детальной консультации обращайтесь: @nik_Anna_Er"

client = OpenAI()

# Константы
MAX_FREE_REQUESTS = 10
MAX_QUESTION_LENGTH = 500
MIN_QUESTION_LENGTH = 3
OPENAI_MAX_TOKENS = 700
API_RETRY_ATTEMPTS = 3
API_RETRY_DELAY = 2

# Бонус за подписку на канал
SUB_CHANNEL_USERNAME = os.getenv("SUB_CHANNEL_USERNAME", "SecretLoveMagic")  # без @
SUB_CHANNEL_LINK = f"https://t.me/SecretLoveMagic"
SUB_BONUS_AMOUNT = int(os.getenv("SUB_BONUS_AMOUNT", "5"))

PUBLIC_URL = os.getenv("PUBLIC_URL")  # например: https://<имя-сервиса>.onrender.com


# OpenAI настройки
# === OpenAI Chat settings (новые) ===
OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-4o-mini")
OPENAI_TEMPERATURE = float(os.getenv("OPENAI_TEMPERATURE", "0.7"))
OPENAI_TIMEOUT = float(os.getenv("OPENAI_TIMEOUT", "30.0"))  # сек
# OPENAI_MAX_TOKENS у тебя уже есть — оставь его как есть (700 или 600)




# Прокси (если необходимо)


# Логирование
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO,
    filename='bot.log'
)
logger = logging.getLogger(__name__)

# Настройки бота
ADMIN_ID = int(os.getenv('ADMIN_ID', '0'))
TOKEN = os.getenv('TELEGRAM_TOKEN', '7880322246:AAELUDeL3KQa0Gw9wfVRkKmWvQU_Xw74u0Q')
CHANNEL_ID = "-1002141657943"
CHANNEL_LINK = "https://t.me/taromagia"
OBOI_LINK = "https://t.me/taromagia/13"
TAROLOG_LINK = "https://t.me/nikAnnaEr"

# Тарифы
TARIFFS = {
    'pay10':       {'price': 100.00, 'name': 'Докупить 10',      'requests': 10, 'test_price': 1.00},
    'pay30':       {'price': 190.00, 'name': 'Докупить 30',      'requests': 30},
    'pay3_unlim':  {'price': 150.00, 'name': 'Безлимит 3 дня',   'days': 3},
    'pay14_unlim': {'price': 350.00, 'name': 'Безлимит 2 недели','days': 14},
    'pay30_unlim': {'price': 490.00, 'name': 'Безлимит месяц',   'days': 30}  # главный продукт
}


WELCOME_TEXT = """🔮 Добро пожаловать в мир Таро! 🔮

Здесь вы можете:
🌟 Получить карту дня для вдохновения
🃏 Задать вопрос и получить расклад на 3 карты  
📚 Изучить готовые расклады по категориям

Первые 10 гаданий бесплатно!

💫 Для персональных консультаций: @nik\\_Anna\\_Er"""

# YooKassa настройки
Configuration.account_id = os.getenv('YOOKASSA_ACCOUNT_ID')
Configuration.secret_key = os.getenv('YOOKASSA_SECRET_KEY')

def check_openai_setup():
    """Проверка настройки OpenAI API"""
    api_key = os.getenv('OPENAI_API_KEY')
    if not api_key:
        logger.error("OPENAI_API_KEY не установлен!")
        return False
    if not api_key.startswith('sk-'):
        logger.error("Неверный формат OPENAI_API_KEY!")
        return False
    logger.info("OpenAI API настроен правильно")
    return True

def init_db():
    """Инициализация базы данных"""
    try:
        conn = sqlite3.connect('botdata.db')
        conn.row_factory = sqlite3.Row

        # Создаем таблицу пользователей (если нет)
        conn.execute('''
            CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY,
                username TEXT,
                request_count INTEGER DEFAULT 0,
                is_subscribed BOOLEAN DEFAULT FALSE,
                is_banned BOOLEAN DEFAULT FALSE,
                join_date TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                paid_requests INTEGER DEFAULT 0,
                last_payment_id TEXT,
                subscription_end TEXT,
                referrer_id INTEGER,
                bonus_requests INTEGER DEFAULT 0
            )
        ''')

        # 🔹 Миграция: флажок "бонус за канал уже выдан" (выполняется один раз)
        try:
            conn.execute("ALTER TABLE users ADD COLUMN got_secretlovemagic INTEGER DEFAULT 0")
        except sqlite3.OperationalError:
            # колонка уже существует — ничего страшного
            pass

        # Индексы
        conn.execute("CREATE INDEX IF NOT EXISTS idx_users_user_id ON users(user_id)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_users_subscription_end ON users(subscription_end)")

        conn.commit()
        conn.close()
        logger.info("База данных инициализирована")
    except Exception as e:
        logger.error(f"Ошибка инициализации БД: {e}")


def get_db_connection():
    conn = sqlite3.connect('botdata.db')
    conn.row_factory = sqlite3.Row
    return conn

def get_user(user_id):
    with get_db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM users WHERE user_id = ?", (user_id,))
        return cursor.fetchone()

def update_user(user_id, username=None, increment_count=False):
    with get_db_connection() as conn:
        if username:
            conn.execute("INSERT OR IGNORE INTO users (user_id, username) VALUES (?, ?)", (user_id, username))
        if increment_count:
            conn.execute("UPDATE users SET request_count = request_count + 1 WHERE user_id = ?", (user_id,))

def register_user(user_id, username=None, first_name=None, last_name=None, referrer_id=None):
    """Регистрация нового пользователя"""
    with get_db_connection() as conn:
        conn.execute("""
            INSERT OR IGNORE INTO users (user_id, username, referrer_id) 
            VALUES (?, ?, ?)
        """, (user_id, username, referrer_id))
        
        # Добавляем бонус рефереру
        if referrer_id and referrer_id != user_id:
            conn.execute("""
                UPDATE users SET bonus_requests = bonus_requests + 5 
                WHERE user_id = ?
            """, (referrer_id,))
            logger.info(f"Добавлено 5 бонусных запросов пользователю {referrer_id}")

def get_user_data(user_id):
    """Получение данных пользователя"""
    user = get_user(user_id)
    if not user:
        return None
    
    # Расчет оставшихся запросов
    free_requests = max(0, MAX_FREE_REQUESTS - user['request_count'])
    paid_requests = user['paid_requests'] or 0
    bonus_requests = user['bonus_requests'] or 0
    
    return {
        'user_id': user['user_id'],
        'username': user['username'],
        'is_subscribed': user['is_subscribed'],
        'is_banned': user['is_banned'],
        'subscription_end': user['subscription_end'],
        'free_requests': free_requests,
        'paid_requests': paid_requests,
        'bonus_requests': bonus_requests,
        'total_requests': free_requests + paid_requests + bonus_requests
    }

def is_subscription_active(user_id, subscription_end_str):
    """Проверка активности подписки"""
    if not subscription_end_str:
        return False
    
    try:
        sub_end_date = datetime.strptime(subscription_end_str, '%Y-%m-%d %H:%M:%S')
        if datetime.now() > sub_end_date:
            with get_db_connection() as conn:
                conn.execute("UPDATE users SET is_subscribed = 0, subscription_end = NULL WHERE user_id = ?", (user_id,))
            return False
        return True
    except ValueError:
        return False

def save_user_id(user_id):
    """Сохранение ID пользователя"""
    if not os.path.exists('user_ids.txt'):
        open('user_ids.txt', 'w').close()
    
    with open('user_ids.txt', 'r') as file:
        existing_ids = file.read().splitlines()
    
    if str(user_id) not in existing_ids:
        with open('user_ids.txt', 'a') as file:
            file.write(f"{user_id}\n")
        logger.info(f"Добавлен новый пользователь: {user_id}")

def can_make_request(user_data, user_id):
    """Проверка возможности сделать запрос"""
    if user_id == ADMIN_ID or user_data['is_subscribed']:
        return True
    
    free_remaining = max(0, MAX_FREE_REQUESTS - user_data['request_count'])
    paid_requests = user_data['paid_requests'] or 0
    bonus_requests = user_data['bonus_requests'] or 0
    
    return (free_remaining + paid_requests + bonus_requests) > 0

def get_all_user_ids() -> list[int]:
    with get_db_connection() as conn:
        rows = conn.execute("SELECT user_id FROM users").fetchall()
        return [int(r["user_id"]) for r in rows if r["user_id"] is not None]


async def broadcast(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # только админ
    if update.effective_user.id != ADMIN_ID:
        await update.message.reply_text("❌ Эта команда только для администратора.")
        return

    # текст берём из аргументов команды
    text = " ".join(context.args) if context.args else ""
    if not text.strip():
        await update.message.reply_text("Формат: /broadcast ваш текст для рассылки")
        return

    user_ids = get_all_user_ids()
    ok = fail = 0
    # маленькая задержка, чтобы не упереться в лимиты
    for uid in user_ids:
        try:
            await context.bot.sendMessage(chat_id=uid, text=text, disable_web_page_preview=True)
            ok += 1
        except Exception:
            fail += 1
        await asyncio.sleep(0.05)  # ≈20 сообщений/сек

    await update.message.reply_text(f"Готово. ✅ {ok} | ❌ {fail} | Всего: {len(user_ids)}")


async def handle_all(update: Update, context: ContextTypes.DEFAULT_TYPE):
    logger.info(f"ALL_HANDLER update: {update}")
    if getattr(update.message, 'web_app_data', None):
        logger.info(f"web_app_data: {update.message.web_app_data.data}")
    if getattr(update, 'callback_query', None):
        logger.info(f"callback_query: {update.callback_query}")



async def deduct_user_request(user_id: int) -> bool:
    """Списание запроса пользователя. Возвращает True если успешно, False если нет запросов"""
    if user_id == ADMIN_ID:
        return True
    
    with get_db_connection() as conn:
        user_data = get_user(user_id)
        
        if not user_data:
            return False
        
        # Проверяем активную подписку
        if user_data['is_subscribed'] and check_subscription_expiry(user_data):
            return True
        
        free_remaining = max(0, MAX_FREE_REQUESTS - user_data['request_count'])
        bonus_requests = user_data['bonus_requests'] or 0
        paid_requests = user_data['paid_requests'] or 0
        
        if free_remaining > 0:
            # Используем бесплатный запрос
            conn.execute("UPDATE users SET request_count = request_count + 1 WHERE user_id = ?", (user_id,))
            logger.info(f"Использован бесплатный запрос для пользователя {user_id}")
            return True
        elif bonus_requests > 0:
            # Используем бонусный запрос
            conn.execute("UPDATE users SET bonus_requests = bonus_requests - 1 WHERE user_id = ?", (user_id,))
            logger.info(f"Использован бонусный запрос для пользователя {user_id}")
            return True
        elif paid_requests > 0:
            # Используем платный запрос
            conn.execute("UPDATE users SET paid_requests = paid_requests - 1 WHERE user_id = ?", (user_id,))
            logger.info(f"Использован платный запрос для пользователя {user_id}")
            return True
        else:
            logger.warning(f"У пользователя {user_id} закончились запросы")
            return False

async def refund_user_request(user_id: int):
    """Возврат запроса при ошибке"""
    if user_id == ADMIN_ID:
        return
    
    with get_db_connection() as conn:
        user_data = get_user(user_id)
        if user_data['is_subscribed']:
            return
        
        # Возвращаем запрос (уменьшаем счетчик)
        if user_data['request_count'] > 0:
            conn.execute("UPDATE users SET request_count = request_count - 1 WHERE user_id = ?", (user_id,))
            logger.info(f"Возвращен запрос пользователю {user_id}")

def check_subscription_expiry(user_data):
    """Проверка активности подписки"""
    if not user_data['is_subscribed']:
        return False
    
    subscription_end_str = user_data['subscription_end']
    if not subscription_end_str:
        return False
    
    try:
        sub_end_date = datetime.strptime(subscription_end_str, '%Y-%m-%d %H:%M:%S')
        if datetime.now() > sub_end_date:
            with get_db_connection() as conn:
                conn.execute("UPDATE users SET is_subscribed = 0, subscription_end = NULL WHERE user_id = ?", (user_data['user_id'],))
            return False
        return True
    except ValueError as e:
        logger.warning(f"Ошибка формата даты подписки для пользователя {user_data['user_id']}: {subscription_end_str}")
        return False

def log_request(user_id, username, question, cards):
    """Логирование запроса в CSV"""
    try:
        timestamp = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        cards_str = ', '.join(cards) if isinstance(cards, list) else str(cards)
        
        file_exists = os.path.isfile('user_requests.csv')
        with open('user_requests.csv', 'a', newline='', encoding='utf-8') as file:
            writer = csv.writer(file)
            if not file_exists:
                writer.writerow(['timestamp', 'user_id', 'username', 'question', 'cards'])
            writer.writerow([timestamp, user_id, username, question, cards_str])
        
        logger.info(f"Запрос записан в лог: {user_id} - {question[:50]}...")
    except Exception as e:
        logger.error(f"Ошибка записи в лог: {e}")

def build_ref_link(user_id: int, bot_username: str) -> str:
    # username приходит как 'MyBot' или '@MyBot' — нормализуем
    bot_username = bot_username.lstrip('@')
    return f"https://t.me/{bot_username}?start=ref_{user_id}"


# Клавиатуры
def main_keyboard():
    return ReplyKeyboardMarkup([
        [KeyboardButton("🌟 Карта дня")],
        [KeyboardButton("🃏 Задать вопрос")],
        [KeyboardButton("📚 Готовые расклады")],
        [KeyboardButton("🎁 +5 за подписку"), KeyboardButton("💳 Подписка")],
        [KeyboardButton("ℹ️ Помощь")],
        [KeyboardButton("🔗 Реферальная ссылка")]
    ], resize_keyboard=True)


def ready_spreads_keyboard():
    """Клавиатура готовых раскладов"""
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("💕 Любовь и отношения", callback_data="category_love")],
        [InlineKeyboardButton("💼 Карьера и финансы", callback_data="category_career")], 
        [InlineKeyboardButton("🌱 Личностный рост", callback_data="category_growth")],
        [InlineKeyboardButton("⬅️ Назад", callback_data="back_main")]
    ])

def love_spreads_keyboard():
    """Клавиатура раскладов на любовь"""
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("💭 Что он/она думает обо мне?", callback_data="spread_love_thoughts")],
        [InlineKeyboardButton("🔮 Перспективы отношений", callback_data="spread_love_prospects")],
        [InlineKeyboardButton("👻 Почему он/она исчез?", callback_data="spread_love_disappeared")],
        [InlineKeyboardButton("🪞 Зеркало отношений", callback_data="spread_love_mirror")],
        [InlineKeyboardButton("❓ Стоит ли продолжать отношения?", callback_data="spread_love_continue")],
        [InlineKeyboardButton("✨ Как привлечь любовь?", callback_data="spread_love_attract")],
        [InlineKeyboardButton("💔 Как пережить расставание", callback_data="spread_love_breakup")],
        [InlineKeyboardButton("⬅️ Назад к категориям", callback_data="back_categories")]
    ])

def career_spreads_keyboard():
    """Клавиатура раскладов на карьеру"""
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("💼 Перспективы на работе", callback_data="spread_career_prospects")],
        [InlineKeyboardButton("💰 Финансовое благополучие", callback_data="spread_career_money")],
        [InlineKeyboardButton("🎯 Выбор профессии", callback_data="spread_career_choice")],
        [InlineKeyboardButton("📈 Карьерный рост", callback_data="spread_career_growth")],
        [InlineKeyboardButton("🤝 Отношения с коллегами", callback_data="spread_career_colleagues")],
        [InlineKeyboardButton("⬅️ Назад к категориям", callback_data="back_categories")]
    ])

def growth_spreads_keyboard():
    """Клавиатура раскладов на личностный рост"""
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🎯 Жизненные цели", callback_data="spread_growth_goals")],
        [InlineKeyboardButton("🔄 Внутренние изменения", callback_data="spread_growth_changes")],
        [InlineKeyboardButton("🌟 Таланты и способности", callback_data="spread_growth_talents")],
        [InlineKeyboardButton("⚖️ Кармические задачи", callback_data="spread_growth_karma")],
        [InlineKeyboardButton("🧘 Духовное развитие", callback_data="spread_growth_spiritual")],
        [InlineKeyboardButton("⬅️ Назад к категориям", callback_data="back_categories")]
    ])

def subscription_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("💎 Докупить 10 — 100₽", callback_data="pay10")],
        [InlineKeyboardButton("✨ Докупить 30 — 190₽", callback_data="pay30")],
        [InlineKeyboardButton("🌟 Безлимит 3 дня — 150₽", callback_data="pay3_unlim")],
        [InlineKeyboardButton("💫 Безлимит 2 недели — 350₽", callback_data="pay14_unlim")],
        [InlineKeyboardButton("🔥 Безлимит месяц — 490₽ (рекомендуем)", callback_data="pay30_unlim")]
    ])


# Готовые расклады
READY_SPREADS = {
    # Любовь
    "spread_love_thoughts": {
        "title": "💭 Что он/она думает обо мне?",
        "description": "Расклад поможет понять мысли и чувства интересующего вас человека",
        "positions": ["Его/её мысли о вас", "Скрытые чувства", "Что мешает сближению"]
    },
    "spread_love_prospects": {
        "title": "🔮 Перспективы отношений", 
        "description": "Узнайте, что ждет ваши отношения в будущем",
        "positions": ["Текущее состояние", "Возможные препятствия", "Итог отношений"]
    },
    "spread_love_disappeared": {
        "title": "👻 Почему он/она исчез?",
        "description": "Понимание причин внезапного исчезновения партнера",
        "positions": ["Истинная причина", "Его/её состояние", "Стоит ли ждать возвращения"]
    },
    "spread_love_mirror": {
        "title": "🪞 Зеркало отношений",
        "description": "Глубокий анализ динамики ваших отношений",
        "positions": ["Ваш вклад в отношения", "Вклад партнера", "Общий потенциал"]
    },
    "spread_love_continue": {
        "title": "❓ Стоит ли продолжать отношения?",
        "description": "Поможет принять важное решение о будущем отношений",
        "positions": ["Плюсы продолжения", "Минусы продолжения", "Совет карт"]
    },
    "spread_love_attract": {
        "title": "✨ Как привлечь любовь?",
        "description": "Советы по привлечению новой любви в вашу жизнь",
        "positions": ["Что мешает любви", "Что нужно изменить", "Как действовать"]
    },
    "spread_love_breakup": {
        "title": "💔 Как пережить расставание",
        "description": "Поддержка в трудный период расставания",
        "positions": ["Причина боли", "Урок расставания", "Путь к исцелению"]
    },
    
    # Карьера
    "spread_career_prospects": {
        "title": "💼 Перспективы на работе",
        "description": "Ваши профессиональные перспективы",
        "positions": ["Текущая ситуация", "Скрытые возможности", "Рекомендации"]
    },
    "spread_career_money": {
        "title": "💰 Финансовое благополучие",
        "description": "Финансовые перспективы и возможности",
        "positions": ["Источники дохода", "Препятствия", "Путь к изобилию"]
    },
    "spread_career_choice": {
        "title": "🎯 Выбор профессии",
        "description": "Поможет определиться с профессиональным путем",
        "positions": ["Ваши сильные стороны", "Подходящая сфера", "Первые шаги"]
    },
    "spread_career_growth": {
        "title": "📈 Карьерный рост",
        "description": "Возможности карьерного развития",
        "positions": ["Текущие навыки", "Что развивать", "Возможности роста"]
    },
    "spread_career_colleagues": {
        "title": "🤝 Отношения с коллегами",
        "description": "Улучшение рабочих отношений",
        "positions": ["Атмосфера в коллективе", "Ваша роль", "Как улучшить отношения"]
    },
    
    # Личностный рост
    "spread_growth_goals": {
        "title": "🎯 Жизненные цели",
        "description": "Определение и достижение жизненных целей",
        "positions": ["Истинные желания", "Препятствия", "Путь к цели"]
    },
    "spread_growth_changes": {
        "title": "🔄 Внутренние изменения",
        "description": "Процесс личностной трансформации",
        "positions": ["Что уходит", "Что приходит", "Как принять изменения"]
    },
    "spread_growth_talents": {
        "title": "🌟 Таланты и способности",
        "description": "Раскрытие скрытых талантов",
        "positions": ["Скрытый талент", "Как развить", "Где применить"]
    },
    "spread_growth_karma": {
        "title": "⚖️ Кармические задачи",
        "description": "Понимание жизненных уроков",
        "positions": ["Кармическая задача", "Урок для души", "Путь освобождения"]
    },
    "spread_growth_spiritual": {
        "title": "🧘 Духовное развитие",
        "description": "Духовный путь и развитие",
        "positions": ["Текущий уровень", "Следующий шаг", "Духовная цель"]
    }
}

# Функции для работы с OpenAI
async def get_tarot_reading(prompt: str) -> str:
    """
    Вызывает OpenAI и возвращает текст интерпретации.
    Ключ берётся из переменной окружения OPENAI_API_KEY (нигде в коде не нужен).
    """
    logger.info(f"Отправка запроса в OpenAI: prompt={prompt[:120]}...")

    model_name = os.getenv("OPENAI_MODEL", "gpt-4o-mini")  # можно сменить в .env при желании
    system_prompt = (
        "Ты - профессиональный таролог. Давай краткие, но содержательные интерпретации карт таро. "
        "1. Начинай с краткого ответа на вопрос, затем расшифровка каждой карты 2-3 предложения. "
        "2. Используй мистический, но понятный язык. 3. Давай практические советы. "
        "4. Будь позитивным, но честным. 3-4 абзаца максимум. "
        "5. Не используй эмодзи, кроме одного в конце. Пиши от второго лица. Обращайся на 'Вы'. "
        "Заканчивай пожеланием удачи."
    )

    last_error = None
    for attempt in range(API_RETRY_ATTEMPTS):
        try:
            client = OpenAI()  # ключ возьмётся из OPENAI_API_KEY
            response = client.chat.completions.create(
                model=model_name,
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": prompt}
                ],
                max_tokens=OPENAI_MAX_TOKENS,
                temperature=0.7
            )
            result = (response.choices[0].message.content or "").strip()
            finish_reason = getattr(response.choices[0], "finish_reason", "n/a")
            logger.info(f"OpenAI finish_reason={finish_reason}, len={len(result)}")

            if not result:
                raise RuntimeError("Пустой ответ от API")

            return result

        except Exception as e:
            last_error = e
            logger.error(f"Ошибка OpenAI (попытка {attempt + 1}/{API_RETRY_ATTEMPTS}): {e}")
            if attempt < API_RETRY_ATTEMPTS - 1:
                await asyncio.sleep(API_RETRY_DELAY * (attempt + 1))

    # Если все попытки упали — пробрасываем последнюю ошибку
    raise last_error if last_error else RuntimeError("Неизвестная ошибка OpenAI")






# Валидация
def is_valid_question(text):
    """Проверка валидности вопроса"""
    if not text:
        return False, "Вопрос не может быть пустым."
    
    if len(text) < MIN_QUESTION_LENGTH:
        return False, f"Вопрос слишком короткий. Минимум {MIN_QUESTION_LENGTH} символа."
    
    if len(text) > MAX_QUESTION_LENGTH:
        return False, f"Вопрос слишком длинный. Максимум {MAX_QUESTION_LENGTH} символов."
    
    return True, ""

def is_valid_cards(cards):
    """Проверка валидности выбранных карт"""
    if not cards:
        return False, "Карты не выбраны."
    
    if not isinstance(cards, list):
        return False, "Неверный формат карт."
    
    if len(cards) != 3:
        return False, "Должно быть выбрано ровно 3 карты."
    
    return True, ""

# Обработчики команд
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработчик команды /start"""
    user = update.effective_user
    args = context.args
    
    referrer_id = None
    if args and args[0].startswith('ref_'):
        try:
            referrer_id = int(args[0].replace('ref_', ''))
            logger.info(f"Новый пользователь {user.id} пришел по реферальной ссылке от {referrer_id}")
        except ValueError:
            pass
    
    register_user(user.id, user.username, user.first_name, user.last_name, referrer_id)
    
    await update.message.reply_text(
        WELCOME_TEXT,
        reply_markup=main_keyboard(),
        parse_mode='Markdown'
    )

async def handle_card_of_day(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработчик карты дня"""
    context.user_data['is_card_of_day'] = True
    context.user_data['question'] = 'Карта дня'

    await update.message.reply_text(
        "🌟 *Карта дня*\n\nВыберите одну карту, которая будет сопровождать вас сегодня:",
        reply_markup=ReplyKeyboardMarkup([
            [KeyboardButton("🔮 Выбрать карту дня", 
                        web_app=WebAppInfo(url="https://gabanna81.github.io/taro-daily/"))]
        ], resize_keyboard=True),
        parse_mode='Markdown'
    )

async def handle_subscription(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработчик подписки"""
    user_data = get_user_data(update.effective_user.id)
    if not user_data:
        await update.message.reply_text("❌ Ошибка получения данных пользователя.")
        return
    
    free_requests = user_data['free_requests']
    paid_requests = user_data['paid_requests']
    bonus_requests = user_data['bonus_requests']
    subscription_end = user_data['subscription_end']
    
    status_text = f"📊 **Ваш статус:**\n\n"
    status_text += f"🆓 Бесплатных гаданий: {free_requests}\n"
    status_text += f"💎 Платных гаданий: {paid_requests}\n"
    status_text += f"🎁 Бонусных гаданий: {bonus_requests}\n"
    
    if subscription_end and is_subscription_active(update.effective_user.id, subscription_end):
        end_date = datetime.strptime(subscription_end, '%Y-%m-%d %H:%M:%S')
        status_text += f"✨ Безлимитная подписка до: {end_date.strftime('%d.%m.%Y %H:%M')}\n"
    else:
        status_text += "📅 Активной подписки нет\n"
    
    status_text += "\n💳 **Выберите тариф:**"
    
    await update.message.reply_text(
        status_text,
        reply_markup=subscription_keyboard(),
        parse_mode='Markdown'
    )

async def show_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Показать справку"""
    help_text = """ℹ️ **Справка по боту**

🔮 **Как пользоваться:**
• 🌟 Карта дня - получите карту-совет на день
• 🃏 Задать вопрос - персональное гадание на 3 карты
• 📚 Готовые расклады - тематические расклады по категориям

💡 **Советы:**
• Формулируйте вопросы четко и конкретно
• Сосредоточьтесь на вопросе при выборе карт
• Первые 10 гаданий бесплатно

📞 **Поддержка:** @nik\\_Anna\\_Er

🌟 **Получите больше:**
• Приглашайте друзей и получайте бонусы
• Оформите подписку для неограниченных гаданий"""

    await update.message.reply_text(help_text, parse_mode='Markdown')

async def handle_webapp(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработчик данных из веб-приложения"""
    logger.info("=== ДИАГНОСТИКА handle_webapp ===")
    logger.info(f"context.user_data = {dict(context.user_data)}")
    logger.info(f"Есть question? {bool(context.user_data.get('question'))}")
    logger.info(f"Есть spread_positions? {bool(context.user_data.get('spread_positions'))}")
    try:
        data = json.loads(update.effective_message.web_app_data.data)
        user = update.effective_user
        
        cards = data.get('cards', [])
        if not cards:
            await update.message.reply_text("❌ Карты не выбраны.")
            return
        
        user_data = get_user_data(user.id)
        if not user_data:
            await update.message.reply_text("❌ Ошибка получения данных пользователя.")
            return
        
        if user_data['is_banned']:
            await update.message.reply_text("❌ Ваш аккаунт заблокирован.")
            return
        
        is_card_of_day = context.user_data.get('is_card_of_day', False)
        
        if is_card_of_day:
            # Карта дня - одна карта
            if len(cards) < 1:
                await update.message.reply_text("❌ Карта не выбрана.")
                context.user_data.clear()
                return
            
            selected_card = cards[0]
            
            # Проверка запросов
            if not await deduct_user_request(user.id):
                await update.message.reply_text(
                    "❌ У вас закончились бесплатные гадания! 💰 Оформите подписку для продолжения.",
                    reply_markup=subscription_keyboard()
                )
                context.user_data.clear()
                return
            
            processing_message = await update.message.reply_text("🔮 Расшифровываю карту дня...")
            
            try:
                # Базовая интерпретация карты дня
                base_interpretation = TAROT_DAY_INTERPRETATIONS.get(
                    selected_card.lower(), 
                    "🌟 Ваша карта дня выбрана. Настройтесь на позитивную волну и следуйте интуиции!"
                )
                interpretation = f"{base_interpretation}\n\n{CONSULTATION_BLOCK}"
                
            except Exception as e:
                logger.error(f"Ошибка получения интерпретации карты дня: {e}")
                interpretation = "🌟 Ваша карта дня выбрана. Настройтесь на позитивную волну и следуйте интуиции!"
            
            log_request(user.id, user.username, "Карта дня", [selected_card])
            
            await processing_message.edit_text(interpretation)
            await update.message.reply_text("Выберите действие:", reply_markup=main_keyboard())
        
        else:
            # ВСЕ ОСТАЛЬНЫЕ ГАДАНИЯ (обычные + готовые расклады) - ОДИНАКОВАЯ ЛОГИКА
            question = context.user_data.get('question')
            if not question:
                await update.message.reply_text("❌ Вопрос не найден. Начните заново.")
                context.user_data.clear()
                return
            
            # Проверка карт
            is_valid, error_message = is_valid_cards(cards)
            if not is_valid:
                await update.message.reply_text(f"❌ {error_message}")
                context.user_data.clear()
                return
            
            # Проверка запросов
            if not await deduct_user_request(user.id):
                await update.message.reply_text(
                    "❌ У вас закончились бесплатные гадания! 💰 Оформите подписку для продолжения.",
                    reply_markup=subscription_keyboard()
                )
                context.user_data.clear()
                return
            
            processing_message = await update.message.reply_text("🔮 Расшифровываю карты...")
            
            try:
                # Проверяем есть ли позиции расклада (готовый расклад или обычное гадание)
                spread_positions = context.user_data.get('spread_positions')
                
                if spread_positions:
                    # ГОТОВЫЙ РАСКЛАД - промпт с позициями
                    position_descriptions = []
                    for i, (card, position) in enumerate(zip(cards, spread_positions), 1):
                        position_descriptions.append(f"{i}. {position}: {card}")
                    
                    prompt = (
                        f"Вопрос: {question}\n"  
                        f"Выпавшие карты таро: {', '.join(cards)}.\n"
                        f"Позиции расклада: {'; '.join(position_descriptions)}.\n"
                        f"Дай детальную интерпретацию расклада для каждой позиции отдельно, затем общий вывод."
                    )
                else:
                    # ОБЫЧНОЕ ГАДАНИЕ - стандартный промпт
                    prompt = f"Вопрос: {question}\nВыпавшие карты таро: {', '.join(cards)}.\nДай детальную интерпретацию расклада."
                
                interpretation = await get_tarot_reading(prompt)
                final_text = f"{interpretation}\n\n{CONSULTATION_BLOCK}"
                
                log_request(user.id, user.username, question, cards)
                
                await processing_message.edit_text(final_text)
                await update.message.reply_text("Выберите действие:", reply_markup=main_keyboard())
                
            except Exception as e:
                logger.error(f"Ошибка получения интерпретации: {e}")
                await refund_user_request(user.id)
                await processing_message.edit_text(
                    "❌ Произошла ошибка при расшифровке карт. Запрос возвращен. Попробуйте позже."
                )
        
        context.user_data.clear()
        
    except json.JSONDecodeError:
        await update.message.reply_text("❌ Ошибка обработки данных.")
        logger.error("Ошибка декодирования JSON из веб-приложения")
    except Exception as e:
        await update.message.reply_text("❌ Произошла ошибка. Попробуйте позже.")
        logger.error(f"Ошибка обработки веб-приложения: {e}")
        context.user_data.clear()

async def handle_callback_query(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработчик inline кнопок"""
    query = update.callback_query
    await query.answer()
    
    data = query.data
    
    # Обработка категорий раскладов
    
    if data == "category_love":
        await query.edit_message_text(
            "💕 **Расклады на любовь и отношения**\n\n"
            "Выберите интересующий вас расклад:",
            reply_markup=love_spreads_keyboard(),
            parse_mode='Markdown'
        )
    
    elif data == "show_ref_text":
        bot_username = (context.bot.username or "").lstrip("@")
        link = build_ref_link(update.effective_user.id, bot_username)
        await query.answer()  # закрыть "часики"
        await query.message.reply_text(link, disable_web_page_preview=True)
        return
    
    elif data == "category_career":
        await query.edit_message_text(
            "💼 **Расклады на карьеру и финансы**\n\n"
            "Выберите интересующий вас расклад:",
            reply_markup=career_spreads_keyboard(),
            parse_mode='Markdown'
        )
    
    elif data == "check_sub_secretlovemagic":
        await check_sub_bonus(update, context)
        return

    
    elif data == "category_growth":
        await query.edit_message_text(
            "🌱 **Расклады на личностный рост**\n\n"
            "Выберите интересующий вас расклад:",
            reply_markup=growth_spreads_keyboard(),
            parse_mode='Markdown'
        )
    
    elif data == "back_categories":
        await query.edit_message_text(
            "🔮 **Выберите категорию гадания:**\n\n"
            "💕 Любовь - отношения, чувства, романтика\n"
            "💼 Карьера - работа, финансы, профессия\n" 
            "🌱 Личностный рост - саморазвитие, духовность",
            reply_markup=ready_spreads_keyboard(),
            parse_mode='Markdown'
        )
    
    elif data == "back_main":
        await query.edit_message_text(
            "Возвращаемся в главное меню 🏠"
    )        
    elif data.startswith('spread'):
        await handle_ready_spread(update, context, data)    
        
    
    # Обработка конкретных раскладов
    elif data.startswith("spread_"):
        await handle_ready_spread(update, context, data)
    
    # Обработка платежей
    elif data.startswith("pay"):
        await process_payment(update, context)
        

async def handle_ready_spread(update: Update, context: ContextTypes.DEFAULT_TYPE, spread_key: str):
    """Обработчик готовых раскладов (через обычную ReplyKeyboard с webapp)"""
    logger.info(f"=== ДИАГНОСТИКА handle_ready_spread {spread_key} ===")
    query = update.callback_query

    if spread_key not in READY_SPREADS:
        await query.edit_message_text("❌ Расклад не найден.")
        return

    spread = READY_SPREADS[spread_key]

    # Сохраняем название расклада как вопрос + позиции отдельно
    context.user_data['question'] = spread['title']
    context.user_data['spread_positions'] = spread['positions']
    context.user_data['state'] = 'awaiting_cards'

    logger.info(f"СОХРАНИЛИ в context.user_data: {dict(context.user_data)}")

    # 1. Оповещаем о выбранном раскладе (заменяет edit_message_text)
    await query.edit_message_text(
        f"🔮 *Выбран расклад:*\n"
        f"{spread['title']}\n\n"
        f"📝 {spread['description']}",
        parse_mode='Markdown'
    )

    # 2. Отправляем новое сообщение с позицией и кнопкой WebApp (ReplyKeyboard)
    await query.message.reply_text(
        f"🃏 *Позиции расклада:*\n"
        f"1️⃣ {spread['positions'][0]}\n"
        f"2️⃣ {spread['positions'][1]}\n"
        f"3️⃣ {spread['positions'][2]}\n\n"
        f"Нажмите кнопку ниже, чтобы выбрать 3 карты для этого расклада:",
        reply_markup=ReplyKeyboardMarkup([
            [KeyboardButton("🔮 Выбрать карты", web_app=WebAppInfo(url="https://gabanna81.github.io/taro/"))]
        ], resize_keyboard=True),
        parse_mode='Markdown'
    )


async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработчик текстовых сообщений"""
    text = update.message.text.strip()
    user = update.effective_user
    
    if text == "🌟 Карта дня":
        await handle_card_of_day(update, context)
        return
    
    if text == "🎁 +5 за подписку":
        await show_sub_bonus(update, context)
        return

    
    if text == "📚 Готовые расклады":
        await update.message.reply_text(
            "🔮 **Выберите категорию гадания:**\n\n"
            "💕 Любовь - отношения, чувства, романтика\n"
            "💼 Карьера - работа, финансы, профессия\n" 
            "🌱 Личностный рост - саморазвитие, духовность",
            reply_markup=ready_spreads_keyboard(),
            parse_mode='Markdown'
        )
        return
    
    if text == "🔗 Реферальная ссылка":
    # 1) строим ссылку по реальному имени бота
        bot_username = (context.bot.username or "").lstrip("@")
        link = build_ref_link(user.id, bot_username)

    # 2) делаем кликабельную кнопку (не зависит от Markdown)
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("✨ Открыть бота по ссылке", url=link)]
    ])

    # 3) шлём ТЕКСТ без parse_mode, чтобы ничего не сломать
        await update.message.reply_text(
            f"🎁 Ваша реферальная ссылка:\n{link}\n\n"
            f"📢 Поделитесь ей с друзьями!\n"
            f"За каждого нового пользователя вы получите 5 бонусных гаданий.\n\n"
            f"💡 Просто отправьте эту ссылку друзьям или нажмите кнопку ниже.",
            reply_markup=kb,
            disable_web_page_preview=True  # чтобы не подтягивалась превьюшка
        )
        return

    
    if text == "ℹ️ Помощь":
        await show_help(update, context)
        return
    
    if text == "💳 Подписка":
        await handle_subscription(update, context)
        return
    
    if text == "🃏 Задать вопрос":
        await update.message.reply_text(
            "🔮 **Задайте свой вопрос**\n\n"
            "Напишите вопрос, на который хотите получить ответ карт Таро.\n\n"
            "💡 **Советы для хорошего вопроса:**\n"
            "• Будьте конкретными\n"
            "• Избегайте вопросов \"да/нет\"\n"
            "• Сосредоточьтесь на том, что вас действительно волнует\n\n"
            "📝 Напишите ваш вопрос:",
            parse_mode='Markdown'
        )
        context.user_data['state'] = 'awaiting_question'
        return
    
    # Обработка состояний
    state = context.user_data.get('state')
    
    if state == 'awaiting_question':
        # Валидация вопроса
        is_valid, error_message = is_valid_question(text)
        if not is_valid:
            await update.message.reply_text(f"❌ {error_message}")
            return
        
        context.user_data['question'] = text
        context.user_data['state'] = 'awaiting_cards'
        
        await update.message.reply_text(
            f"✅ **Ваш вопрос принят:**\n_{text}_\n\n"
            f"🃏 Теперь выберите 3 карты таро для гадания:",
            reply_markup=ReplyKeyboardMarkup([
                [KeyboardButton("🔮 Выбрать карты", 
                              web_app=WebAppInfo(url="https://gabanna81.github.io/taro/"))]
            ], resize_keyboard=True),
            parse_mode='Markdown'
        )
        return
    
    # Если сообщение не распознано
    await update.message.reply_text(
        "🤔 Не понимаю команду. Воспользуйтесь кнопками меню:",
        reply_markup=main_keyboard()
    )

async def process_payment(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработка платежей"""
    query = update.callback_query
    tariff_key = query.data
    
    if tariff_key not in TARIFFS:
        await query.answer("❌ Неверный тариф")
        return
    
    tariff = TARIFFS[tariff_key]
    user_id = update.effective_user.id
    
    try:
        payment_url = await create_payment(user_id, tariff_key, tariff)
        if payment_url:
            keyboard = InlineKeyboardMarkup([
                [InlineKeyboardButton("💳 Оплатить", url=payment_url)],
                [InlineKeyboardButton("❌ Отмена", callback_data="back_main")]
            ])
            
            await query.edit_message_text(
                f"💳 **Оплата тарифа**\n\n"
                f"📦 Тариф: {tariff['name']}\n"
                f"💰 Стоимость: {tariff['price']}₽\n\n"
                f"Нажмите кнопку для оплаты:",
                reply_markup=keyboard,
                parse_mode='Markdown'
            )
        else:
            await query.answer("❌ Ошибка создания платежа")
    except Exception as e:
        logger.error(f"Ошибка обработки платежа: {e}")
        await query.answer("❌ Ошибка обработки платежа")
# ====== АДМИН-КОМАНДЫ ДЛЯ РУЧНОГО НАЧИСЛЕНИЯ ======

def _ensure_user_exists(user_id: int):
    """Если пользователя ещё нет в БД — создаём пустую запись"""
    if not get_user(user_id):
        register_user(user_id)

async def _admin_guard(update: Update) -> bool:
    """Проверяем, что команду вызвал админ"""
    uid = update.effective_user.id if update.effective_user else 0
    if uid != ADMIN_ID:
        if update.message:
            await update.message.reply_text("❌ Эта команда доступна только администратору.")
        elif update.callback_query:
            await update.callback_query.answer("Недостаточно прав", show_alert=True)
        return False
    return True

async def add_paid(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    /add_paid <user_id> <сколько>
    Начисляет платные гадания пользователю (увеличивает users.paid_requests).
    """
    if not await _admin_guard(update):
        return
    try:
        target_id = int(context.args[0])
        amount = int(context.args[1])
        assert amount > 0
    except Exception:
        await update.message.reply_text("Формат: /add_paid <user_id> <сколько>  (например: /add_paid 123456789 10)")
        return

    _ensure_user_exists(target_id)
    with get_db_connection() as conn:
        conn.execute(
            "UPDATE users SET paid_requests = COALESCE(paid_requests,0) + ? WHERE user_id = ?",
            (amount, target_id)
        )
    await update.message.reply_text(f"✅ Начислено {amount} платных гаданий пользователю {target_id}.")

async def add_bonus(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    /add_bonus <user_id> <сколько>
    Начисляет бонусные гадания (users.bonus_requests).
    """
    if not await _admin_guard(update):
        return
    try:
        target_id = int(context.args[0])
        amount = int(context.args[1])
        assert amount > 0
    except Exception:
        await update.message.reply_text("Формат: /add_bonus <user_id> <сколько>  (например: /add_bonus 123456789 5)")
        return

    _ensure_user_exists(target_id)
    with get_db_connection() as conn:
        conn.execute(
            "UPDATE users SET bonus_requests = COALESCE(bonus_requests,0) + ? WHERE user_id = ?",
            (amount, target_id)
        )
    await update.message.reply_text(f"✅ Начислено {amount} бонусных гаданий пользователю {target_id}.")

async def reset_free(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    /reset_free <user_id>
    Обнуляет счётчик израсходованных бесплатных (users.request_count=0) → снова доступно MAX_FREE_REQUESTS.
    """
    if not await _admin_guard(update):
        return
    try:
        target_id = int(context.args[0])
    except Exception:
        await update.message.reply_text("Формат: /reset_free <user_id>  (например: /reset_free 123456789)")
        return

    _ensure_user_exists(target_id)
    with get_db_connection() as conn:
        conn.execute("UPDATE users SET request_count = 0 WHERE user_id = ?", (target_id,))
    await update.message.reply_text(f"✅ Бесплатные гадания сброшены пользователю {target_id} (снова доступно {MAX_FREE_REQUESTS}).")

async def add_sub(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    /add_sub <user_id> <дней>
    Включает/продлевает подписку: users.is_subscribed=1 и двигает users.subscription_end.
    """
    if not await _admin_guard(update):
        return
    try:
        target_id = int(context.args[0])
        days = int(context.args[1])
        assert days > 0
    except Exception:
        await update.message.reply_text("Формат: /add_sub <user_id> <дней>  (например: /add_sub 123456789 7)")
        return

    _ensure_user_exists(target_id)
    # читаем текущий конец подписки
    user = get_user(target_id)
    now = datetime.now()
    base = now
    try:
        if user and user['subscription_end']:
            current_end = datetime.strptime(user['subscription_end'], '%Y-%m-%d %H:%M:%S')
            # если подписка ещё активна — продлеваем от текущего конца
            if current_end > now:
                base = current_end
    except Exception:
        pass

    new_end = base + timedelta(days=days)
    with get_db_connection() as conn:
        conn.execute(
            "UPDATE users SET is_subscribed = 1, subscription_end = ? WHERE user_id = ?",
            (new_end.strftime('%Y-%m-%d %H:%M:%S'), target_id)
        )
    await update.message.reply_text(
        f"✅ Подписка пользователю {target_id} продлена/выдана до {new_end.strftime('%d.%m.%Y %H:%M')}."
    )
# ====== КОНЕЦ БЛОКА АДМИН-КОМАНД ======


async def show_sub_bonus(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    u = get_user(user.id)
    already = (u['got_secretlovemagic'] if u else 0)
    if already:
        await update.message.reply_text("✅ Бонус уже начислялся ранее.")
        return

    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("🔗 Открыть канал", url=SUB_CHANNEL_LINK)],
        [InlineKeyboardButton("✅ Я уже подписан(а)", callback_data="check_sub_secretlovemagic")]
    ])
    await update.message.reply_text(
        f"Подпишитесь на канал и получите +{SUB_BONUS_AMOUNT} бонусных гаданий.\n\n"
        f"1) Нажмите «Открыть канал» и подпишитесь\n"
        f"2) Вернитесь сюда и нажмите «✅ Я уже подписан(а)» — бот проверит и начислит бонус.",
        reply_markup=kb
    )


async def is_user_subscribed_to_channel(bot, user_id: int) -> bool | None:
    """
    True — подписан, False — нет, None — не удалось проверить (например, бот не видит участников).
    ВАЖНО: бот должен быть админом публичного канала @SecretLoveMagic.
    """
    chat_ref = f"@{SUB_CHANNEL_USERNAME}"
    try:
        member = await bot.get_chat_member(chat_id=chat_ref, user_id=user_id)
        return getattr(member, "status", "left") in ("member", "administrator", "creator")
    except Exception as e:
        logger.error(f"get_chat_member failed for {chat_ref}, user {user_id}: {e}")
        return None

async def check_sub_bonus(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Проверка и единоразовое начисление бонуса"""
    query = update.callback_query
    user_id = update.effective_user.id

    u = get_user(user_id)
    if u and (u['got_secretlovemagic'] or 0) == 1:
        await query.answer("Бонус уже был начислён ранее.", show_alert=True)
        return

    status = await is_user_subscribed_to_channel(context.bot, user_id)
    if status is None:
        await query.answer("Не удалось проверить подписку. Убедитесь, что бот — админ канала.", show_alert=True)
        return
    if not status:
        await query.answer("Вы ещё не подписаны на канал.", show_alert=True)
        return

    # подписан → начисляем один раз
    with get_db_connection() as conn:
        conn.execute(
            "UPDATE users SET bonus_requests = COALESCE(bonus_requests,0) + ?, got_secretlovemagic = 1 WHERE user_id = ?",
            (SUB_BONUS_AMOUNT, user_id)
        )

    await query.edit_message_text(f"✅ Подписка подтверждена! Начислено +{SUB_BONUS_AMOUNT} бонусных гаданий. Спасибо!")


async def create_payment(user_id, tariff_key, tariff):
    """Создание платежа через YooKassa"""
    try:
        payment_id = str(uuid.uuid4())
        
        # Используем тестовую цену если есть
        price = tariff.get('test_price', tariff['price'])
        
        payment = Payment.create({
            "amount": {
                "value": f"{price:.2f}",
                "currency": "RUB"
            },
            "confirmation": {
                "type": "redirect",
                "return_url": "https://t.me/tarot247bot"
            },
            "capture": True,
            "description": f"Оплата тарифа {tariff['name']}",
            "metadata": {
                'user_id': user_id,
                'tariff': tariff_key
            }
        }, payment_id)
        
        # Сохраняем информацию о платеже
        conn = sqlite3.connect('botdata.db')
        conn.execute('''
            CREATE TABLE IF NOT EXISTS payments (
                payment_id TEXT PRIMARY KEY,
                user_id INTEGER,
                tariff TEXT,
                amount REAL,
                status TEXT DEFAULT 'pending',
                created_at TEXT DEFAULT CURRENT_TIMESTAMP
            )
        ''')
        conn.execute('''
            INSERT INTO payments (payment_id, user_id, tariff, amount)
            VALUES (?, ?, ?, ?)
        ''', (payment_id, user_id, tariff_key, price))
        conn.commit()
        conn.close()
        
        return payment.confirmation.confirmation_url
        
    except Exception as e:
        logger.error(f"Ошибка создания платежа: {e}")
        return None

def main():
    """Главная функция запуска бота"""
    if not check_openai_setup():
        print("❌ OpenAI API не настроен! Установите OPENAI_API_KEY")
        return

    init_db()

    app = ApplicationBuilder().token(TOKEN).build()

    # Регистрация обработчиков
    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(filters.StatusUpdate.WEB_APP_DATA, handle_webapp))
    app.add_handler(CallbackQueryHandler(handle_callback_query))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))

    # Админ-команды (оставляем, как у тебя)
    app.add_handler(CommandHandler("add_paid", add_paid))
    app.add_handler(CommandHandler("add_bonus", add_bonus))
    app.add_handler(CommandHandler("reset_free", reset_free))
    app.add_handler(CommandHandler("add_sub", add_sub))
    app.add_handler(CommandHandler("broadcast", broadcast))

    logger.info("🤖 Таро бот запущен!")
    print("🤖 Таро бот запущен!")

    # === ВЫБОР РЕЖИМА ЗАПУСКА ===
    USE_WEBHOOK = os.getenv("USE_WEBHOOK", "0") == "1"
    if USE_WEBHOOK:
        # Для Render (или другого хостинга с публичным URL)
        PORT = int(os.environ.get("PORT", "8080"))
        app.run_webhook(
            listen="0.0.0.0",
            port=PORT,
            url_path=TOKEN,
            webhook_url=f"{PUBLIC_URL}/{TOKEN}",
            drop_pending_updates=True
        )
    else:
        # Для запуска на компьютере
        app.run_polling(drop_pending_updates=True)

    
    
if __name__ == '__main__':
    main()