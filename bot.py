import os
import asyncio
import aiohttp
import json
import logging
from datetime import datetime, timedelta
from typing import Optional, List, Dict, Any

from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import Command
from dotenv import load_dotenv

# Библиотека Supabase
from supabase import create_client, Client

# ═══ НАСТРОЙКА ЛОГИРОВАНИЯ ═══
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

load_dotenv()

# ═══ КОНФИГУРАЦИЯ ═══
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
GROQ_API_KEY = os.getenv("GROQ_API_KEY")
SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")

if not all([TELEGRAM_TOKEN, GROQ_API_KEY, SUPABASE_URL, SUPABASE_KEY]):
    raise ValueError("❌ ОШИБКА: Проверьте переменные окружения в Railway!")

# Инициализация Supabase
try:
    supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)
    logger.info("✅ Подключение к Supabase успешно")
except Exception as e:
    logger.error(f"❌ Критическая ошибка Supabase: {e}")
    raise e

# Настройки AI
GROQ_URL = "https://api.groq.com/openai/v1/chat/completions"
WHISPER_URL = "https://api.groq.com/openai/v1/audio/transcriptions"
TEXT_MODEL = "llama-3.1-8b-instant"
WHISPER_MODEL = "whisper-large-v3-turbo"

# ═══ БАЗА ДАННЫХ (STRICT MODE) ═══

class Database:
    @staticmethod
    def ensure_user(tg_id: int, username: str, full_name: str = None):
        """Создает или обновляет пользователя. Возвращает True если ок."""
        try:
            # Пробуем вставить, если есть конфликт - обновляем last_active
            data, count = supabase.table("users").upsert({
                "telegram_id": tg_id,
                "username": username,
                "full_name": full_name,
                "last_active": datetime.now().isoformat()
            }, on_conflict="telegram_id").execute()
            return True
        except Exception as e:
            logger.error(f"DB User Error: {e}")
            return False

    @staticmethod
    def add_task(tg_id: int, content: str, due_date: Optional[str] = None) -> bool:
        """Сохраняет задачу. Возвращает True если успешно."""
        try:
            data = {
                "telegram_id": tg_id,
                "content": content,
                "status": "pending",
                "created_at": datetime.now().isoformat()
            }
            if due_date:
                data["due_date"] = due_date
            
            response = supabase.table("tasks").insert(data).execute()
            logger.info(f"✅ Задача сохранена в БД: {content}")
            return True
        except Exception as e:
            logger.error(f"❌ Ошибка сохранения задачи: {e}")
            return False

    @staticmethod
    def get_tasks(tg_id: int) -> List[Dict]:
        """Получает ТОЛЬКО реальные задачи из БД. Никакой генерации."""
        try:
            # Берем задачи за сегодня и завтра для наглядности
            now = datetime.now()
            tomorrow = now + timedelta(days=1)
            
            response = supabase.table("tasks").select("*")\
                .eq("telegram_id", tg_id)\
                .eq("status", "pending")\
                .gte("created_at", now.replace(hour=0, minute=0, second=0).isoformat())\
                .lte("created_at", tomorrow.replace(hour=23, minute=59, second=59).isoformat())\
                .order("created_at", desc=True)\
                .execute()
            
            return response.data if response.data else []
        except Exception as e:
            logger.error(f"DB Get Tasks Error: {e}")
            return []

    @staticmethod
    def add_history(tg_id: int, role: str, agent: str, message: str):
        try:
            supabase.table("chat_history").insert({
                "telegram_id": tg_id,
                "role": role,
                "agent_type": agent,
                "message": message
            }).execute()
        except Exception as e:
            logger.error(f"History Error: {e}")

db = Database()

# ═══ ПРОМПТЫ АГЕНТОВ ═══

# Оркестратор решает, кто работает
ORCHESTRATOR_PROMPT = """
Ты — Orchestrator. Определи intent запроса:
- 'TASK_CREATE': Пользователь просит запомнить задачу, встречу, звонок (есть время/действие).
- 'TASK_LIST': Пользователь спрашивает "какие задачи", "что на завтра", "план".
- 'CODE': Программирование.
- 'CHAT': Обычный разговор, советы, анализ.

Верни JSON: {"intent": "TASK_CREATE"|"TASK_LIST"|"CODE"|"CHAT", "agent": "ASSISTANT"|"CODER"|"COACH", "summary": "суть"}
"""

ASSISTANT_PROMPT = """
Ты — Personal Assistant. 
Правила:
1. Если создаешь задачу: подтверди сохранение и скажи "Задача добавлена в базу".
2. Не выдумывай факты. Если не знаешь — спроси.
3. Стиль: кратко, по делу, без воды.
"""

CODER_PROMPT = "Ты Senior Dev. Пиши чистый код. Без лишних слов."

# ═══ AI ФУНКЦИИ ═══

async def call_groq(messages: List[Dict], system: str, is_json=False) -> Optional[str]:
    headers = {"Authorization": f"Bearer {GROQ_API_KEY}", "Content-Type": "application/json"}
    payload = {
        "model": TEXT_MODEL,
        "messages": [{"role": "system", "content": system}] + messages,
        "temperature": 0.2,
        "max_tokens": 1000
    }
    if is_json:
        payload["response_format"] = {"type": "json_object"}

    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(GROQ_URL, json=payload, headers=headers, timeout=aiohttp.ClientTimeout(total=20)) as resp:
                if resp.status != 200:
                    logger.error(f"Groq Error: {await resp.text()}")
                    return None
                data = await resp.json()
                return data['choices'][0]['message']['content'].strip()
    except Exception as e:
        logger.error(f"Network Error: {e}")
        return None

# ═══ ЛОГИКА ═══

async def handle_task_creation(text: str, tg_id: int, message: types.Message, bot: Bot, status_id: int):
    """Обрабатывает создание задачи"""
    # 1. Спрашиваем у ИИ детали (время, суть)
    details = await call_groq(
        [{"role": "user", "content": text}], 
        "Извлеки из текста: 1. Суть задачи (коротко). 2. Дату/время (если есть, формат ISO YYYY-MM-DD HH:MM). Верни JSON: {'task_text': '...', 'due_date': '...'}",
        is_json=True
    )
    
    task_text = text
    due_date = None
    
    if details:
        try:
            clean = details.replace("```json", "").replace("```", "")
            parsed = json.loads(clean)
            task_text = parsed.get('task_text', text)
            due_date = parsed.get('due_date')
        except:
            pass

    # 2. СОХРАНЯЕМ В БАЗУ (Критический момент)
    success = db.add_task(tg_id, task_text, due_date)
    
    if success:
        await bot.edit_message_text(
            chat_id=message.chat.id, message_id=status_id,
            text=f"✅ <b>Задача принята!</b>\n\n📝 <b>Суть:</b> {task_text}\n⏰ <b>Когда:</b> {due_date or 'Без точной даты'}\n\n<i>Сохранено в облачную базу Supabase.</i>",
            parse_mode="HTML"
        )
        db.add_history(tg_id, "user", "assistant", f"Created task: {task_text}")
    else:
        await bot.edit_message_text(chat_id=message.chat.id, message_id=status_id, text="❌ Ошибка базы данных. Задача не сохранена.")

async def handle_task_list(tg_id: int, message: types.Message, bot: Bot, status_id: int):
    """Показывает задачи ТОЛЬКО из БД"""
    tasks = db.get_tasks(tg_id)
    
    if not tasks:
        text = "📭 На ближайшие дни задач в базе нет.\nНапиши: 'Позвонить клиенту завтра в 12:00', чтобы добавить."
    else:
        text = "📋 <b>Ваши задачи (из базы):</b>\n\n"
        for i, t in enumerate(tasks, 1):
            due = t.get('due_date', '')
            if due:
                due_str = f"⏰ {due[:16]}"
            else:
                due_str = "🕒 Скоро"
            text += f"{i}. {t['content']}\n   {due_str}\n\n"
    
    await bot.edit_message_text(chat_id=message.chat.id, message_id=status_id, text=text, parse_mode="HTML")

async def handle_general_chat(text: str, tg_id: int, message: types.Message, bot: Bot, status_id: int, agent_type: str):
    """Обычный чат или код"""
    system = CODER_PROMPT if agent_type == "CODER" else ASSISTANT_PROMPT
    
    response = await call_groq([{"role": "user", "content": text}], system)
    
    if response:
        # Если в ответе ИИ упоминается создание задачи, пробуем сохранить (опционально)
        # Но основной фокус на ответе
        await bot.edit_message_text(
            chat_id=message.chat.id, message_id=status_id,
            text=f"🤖 <b>{agent_type}:</b>\n\n{response}",
            parse_mode="HTML"
        )
        db.add_history(tg_id, "user", agent_type, response)
    else:
        await bot.edit_message_text(chat_id=message.chat.id, message_id=status_id, text="❌ Ошибка связи с ИИ.")

async def process_message(text: str, message: types.Message, bot: Bot, status_id: int):
    tg_id = message.from_user.id
    
    # 1. Гарантируем юзера в БД
    db.ensure_user(tg_id, message.from_user.username, message.from_user.full_name)
    
    # 2. Определяем намерение
    intent_data = await call_groq(
        [{"role": "user", "content": text}], 
        ORCHESTRATOR_PROMPT, 
        is_json=True
    )
    
    intent = "CHAT"
    agent = "ASSISTANT"
    
    if intent_data:
        try:
            clean = intent_data.replace("```json", "").replace("```", "")
            parsed = json.loads(clean)
            intent = parsed.get("intent", "CHAT")
            agent = parsed.get("agent", "ASSISTANT")
            logger.info(f"🔀 Intent: {intent}, Agent: {agent}")
        except:
            pass

    # 3. Маршрутизация
    if intent == "TASK_CREATE":
        await handle_task_creation(text, tg_id, message, bot, status_id)
    elif intent == "TASK_LIST":
        await handle_task_list(tg_id, message, bot, status_id)
    else:
        await handle_general_chat(text, tg_id, message, bot, status_id, agent)

# ═══ HANDLERS ═══
bot = Bot(token=TELEGRAM_TOKEN)
dp = Dispatcher()

@dp.message(Command("start"))
async def cmd_start(message: types.Message):
    db.ensure_user(message.from_user.id, message.from_user.username)
    await message.answer(
        "🚀 <b>Мультиагентный ассистент готов!</b>\n\n"
        "Я умею:\n"
        "• ✅ <b>Сохранять задачи:</b> 'Встреча завтра в 14:00'\n"
        "• 📋 <b>Показывать список:</b> 'Какие задачи на завтра?'\n"
        "• 💻 <b>Писать код:</b> 'Напиши парсер'\n"
        "• 🧠 <b>Анализировать:</b> 'Как продать дороже?'\n\n"
        "Все данные хранятся в Supabase."
    )

@dp.message(F.voice)
async def handle_voice(message: types.Message):
    status_msg = await message.answer("⏳ Слушаю...")
    file = await bot.get_file(message.voice.file_id)
    path = f"voice_{message.message_id}.ogg"
    await bot.download_file(file.file_path, path)
    
    try:
        # Транскрибация (упрощенно)
        text = "Голосовое сообщение (требуется настройка Whisper)" 
        # Для краткости примера здесь заглушка, логика Whisper как в прошлом коде
        await bot.edit_message_text(chat_id=message.chat.id, message_id=status_msg.message_id, text=text)
    finally:
        if os.path.exists(path): os.remove(path)

@dp.message()
async def handle_all(message: types.Message):
    if not message.text: return
    status_msg = await message.answer("⏳ Думаю...")
    await process_message(message.text, message, bot, status_msg.message_id)

# ═══ ЗАПУСК ═══
async def main():
    logger.info("🚀 Бот запущен (Strict DB Mode)")
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
