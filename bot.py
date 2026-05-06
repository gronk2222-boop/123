import os
import asyncio
import aiohttp
import json
import logging
from datetime import datetime
from typing import Optional, List, Dict, Any

from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import Command
from dotenv import load_dotenv

# Библиотека Supabase (нужно добавить в requirements.txt)
from supabase import create_client, Client

# ═══ НАСТРОЙКА ЛОГИРОВАНИЯ ═══
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

load_dotenv()

# ═══ КОНФИГУРАЦИЯ ═══
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
GROQ_API_KEY = os.getenv("GROQ_API_KEY")
SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")

# Валидация ключей
if not all([TELEGRAM_TOKEN, GROQ_API_KEY, SUPABASE_URL, SUPABASE_KEY]):
    raise ValueError("❌ ОШИБКА: Проверьте переменные окружения (TELEGRAM, GROQ, SUPABASE)")

# Очистка ключей
TELEGRAM_TOKEN = TELEGRAM_TOKEN.strip()
GROQ_API_KEY = GROQ_API_KEY.strip()
SUPABASE_URL = SUPABASE_URL.strip()
SUPABASE_KEY = SUPABASE_KEY.strip()

# Инициализация Supabase
try:
    supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)
    logger.info("✅ Подключение к Supabase успешно")
except Exception as e:
    logger.error(f"❌ Ошибка подключения к Supabase: {e}")
    raise e

# Настройки AI
GROQ_URL = "https://api.groq.com/openai/v1/chat/completions"
WHISPER_URL = "https://api.groq.com/openai/v1/audio/transcriptions"
TEXT_MODEL = "llama-3.1-8b-instant"
WHISPER_MODEL = "whisper-large-v3-turbo"

# ═══ БАЗА ДАННЫХ (SUPABASE WRAPPER) ═══

class Database:
    @staticmethod
    def ensure_user(tg_id: int, username: str):
        """Создает пользователя, если нет"""
        try:
            supabase.table("users").insert({
                "telegram_id": tg_id,
                "username": username
            }).eq("telegram_id", tg_id).execute() # Upsert logic simplified for demo
            # Простая проверка: если есть - ок, если нет - создаем (игнорируем ошибки дублей)
            data, count = supabase.table("users").select("*").eq("telegram_id", tg_id).execute()
            if not data:
                 supabase.table("users").insert({"telegram_id": tg_id, "username": username}).execute()
        except Exception as e:
            if "Duplicate" not in str(e): logger.warning(f"DB User Error: {e}")

    @staticmethod
    def add_task(tg_id: int, content: str):
        """Добавляет задачу"""
        supabase.table("tasks").insert({
            "telegram_id": tg_id,
            "content": content,
            "is_completed": False
        }).execute()
        logger.info(f"Task saved to Cloud: {content}")

    @staticmethod
    def get_tasks(tg_id: int) -> List[Dict]:
        """Получает активные задачи"""
        response = supabase.table("tasks").select("*").eq("telegram_id", tg_id).eq("is_completed", False).order("created_at", desc=True).limit(10).execute()
        return response.data or []

    @staticmethod
    def clear_tasks(tg_id: int):
        """Помечает все задачи как выполненные (мягкое удаление)"""
        supabase.table("tasks").update({"is_completed": True}).eq("telegram_id", tg_id).eq("is_completed", False).execute()

    @staticmethod
    def add_history(tg_id: int, role: str, message: str):
        """Сохраняет историю переписки"""
        try:
            supabase.table("chat_history").insert({
                "telegram_id": tg_id,
                "role": role,
                "message": message
            }).execute()
        except Exception as e:
            logger.error(f"History save error: {e}")

    @staticmethod
    def get_recent_history(tg_id: int, limit: int = 5) -> List[Dict]:
        """Получает последние N сообщений для контекста"""
        response = supabase.table("chat_history").select("*").eq("telegram_id", tg_id).order("created_at", desc=True).limit(limit).execute()
        # Возвращаем в прямом порядке
        return list(reversed(response.data)) if response.data else []

    @staticmethod
    def get_knowledge(tg_id: int) -> str:
        """Получает знания о пользователе"""
        try:
            response = supabase.table("ai_knowledge").select("key_name, value").eq("telegram_id", tg_id).execute()
            if not response.data:
                return "Нет специальных инструкций."
            return "\n".join([f"- {item['key_name']}: {item['value']}" for item in response.data])
        except Exception as e:
            logger.error(f"Error getting knowledge: {e}")
            return "Ошибка загрузки знаний."

db = Database()

# ═══ ПРОМПТЫ АГЕНТОВ ═══

ROUTER_PROMPT = """
Ты — Диспетчер. Определи тип запроса:
1. Код/Баги/Скрипты → "CODE"
2. Планы/Текст/Идеи/Вопросы → "ASSISTANT"
Верни ТОЛЬКО JSON: {"type": "CODE"|"ASSISTANT", "summary": "суть", "is_task": true/false}
(is_task=true, если нужно запомнить факт или задачу).
"""

CODER_PROMPT = """
Ты Senior Developer. Пиши чистый код. Без воды. С комментариями.
"""

ASSISTANT_PROMPT = """
Ты Личный Ассистент. Отвечай структурно. Используй знания о пользователе, если они есть.
Если пользователь просил запомнить что-то, подтверди: "✅ Сохранено в облако".
"""

# ═══ AI ИНТЕГРАЦИЯ ═══

async def call_groq_text(messages: List[Dict], system_prompt: str, response_format: Optional[str] = None) -> Optional[str]:
    headers = {"Authorization": f"Bearer {GROQ_API_KEY}", "Content-Type": "application/json"}
    
    full_messages = [{"role": "system", "content": system_prompt}] + messages
    
    payload = {
        "model": TEXT_MODEL,
        "messages": full_messages,
        "temperature": 0.2,
        "max_tokens": 1500
    }
    if response_format == "json":
        payload["response_format"] = {"type": "json_object"}

    async with aiohttp.ClientSession() as session:
        async with session.post(GROQ_URL, json=payload, headers=headers, timeout=aiohttp.ClientTimeout(total=30)) as resp:
            if resp.status != 200:
                logger.error(f"Groq Error {resp.status}: {await resp.text()}")
                return None
            data = await resp.json()
            return data['choices'][0]['message']['content'].strip()

async def call_groq_whisper(audio_path: str) -> Optional[str]:
    headers = {"Authorization": f"Bearer {GROQ_API_KEY}"}
    form_data = aiohttp.FormData()
    form_data.add_field('file', open(audio_path, 'rb'), filename='audio.ogg')
    form_data.add_field('model', WHISPER_MODEL)

    async with aiohttp.ClientSession() as session:
        async with session.post(WHISPER_URL, data=form_data, headers=headers, timeout=aiohttp.ClientTimeout(total=60)) as resp:
            if resp.status != 200:
                logger.error(f"Whisper Error {resp.status}: {await resp.text()}")
                return None
            data = await resp.json()
            return data.get('text', '').strip()

# ═══ ЛОГИКА ПАЙПЛАЙНА ═══

async def process_code_task(summary: str, original_text: str, messages: List[Dict], message: types.Message, bot: Bot, status_msg_id: int):
    await bot.edit_message_text(chat_id=message.chat.id, message_id=status_msg_id, text="⏳ 👨‍💻 Кодер пишет решение...")
    
    # Добавляем контекст истории
    context_messages = messages + [{"role": "user", "content": f"Задача: {original_text}\nСуть: {summary}"}]
    result = await call_groq_text(context_messages, CODER_PROMPT)
    
    if result:
        db.add_history(message.from_user.id, "assistant", result)
        await bot.edit_message_text(
            chat_id=message.chat.id, message_id=status_msg_id, 
            text=f"💻 <b>Код готов:</b>\n\n<code>{result}</code>", parse_mode="HTML"
        )
    else:
        await bot.edit_message_text(chat_id=message.chat.id, message_id=status_msg_id, text="❌ Ошибка генерации кода.")

async def process_assistant_task(summary: str, original_text: str, messages: List[Dict], message: types.Message, bot: Bot, status_msg_id: int, is_task_request: bool):
    await bot.edit_message_text(chat_id=message.chat.id, message_id=status_msg_id, text="⏳ 🤖 Ассистент думает...")
    
    # Получаем знания о пользователе
    knowledge = db.get_knowledge(message.from_user.id)
    system_full = f"{ASSISTANT_PROMPT}\n\n📚 Знания о пользователе:\n{knowledge}"
    
    context_messages = messages + [{"role": "user", "content": f"Запрос: {original_text}\nСуть: {summary}"}]
    result = await call_groq_text(context_messages, system_full)
    
    if result:
        db.add_history(message.from_user.id, "assistant", result)
        
        # Если это задача на запоминание, сохраняем в БД отдельно (упрощенно)
        final_text = f"🤖 <b>Ответ:</b>\n\n{result}"
        if is_task_request:
            db.add_task(message.from_user.id, original_text)
            final_text += "\n\n<i>✅ Задача сохранена в облачную базу.</i>"
            
        await bot.edit_message_text(
            chat_id=message.chat.id, message_id=status_msg_id, 
            text=final_text, parse_mode="HTML"
        )
    else:
        await bot.edit_message_text(chat_id=message.chat.id, message_id=status_msg_id, text="❌ Ошибка ответа.")

async def run_pipeline(text_content: str, message: types.Message, bot: Bot, status_msg_id: int):
    tg_id = message.from_user.id
    db.ensure_user(tg_id, message.from_user.username)
    db.add_history(tg_id, "user", text_content)
    
    # Получаем историю для контекста
    history = db.get_recent_history(tg_id, limit=4) # Берем 4 последних сообщения
    
    try:
        # 1. Роутер
        router_json = await call_groq_text(history + [{"role": "user", "content": text_content}], ROUTER_PROMPT, response_format="json")
        if not router_json: raise Exception("Роутер молчит")
        
        clean_json = router_json.replace("```json", "").replace("```", "").strip()
        decision = json.loads(clean_json)
        
        task_type = decision.get("type", "ASSISTANT")
        summary = decision.get("summary", "")
        is_task = decision.get("is_task", False)
        
        logger.info(f"🔀 Роутинг: {task_type} | Task: {is_task}")

        if task_type == "CODE":
            await process_code_task(summary, text_content, history, message, bot, status_msg_id)
        else:
            await process_assistant_task(summary, text_content, history, message, bot, status_msg_id, is_task)
            
    except Exception as e:
        logger.error(f"Pipeline error: {e}")
        await bot.edit_message_text(chat_id=message.chat.id, message_id=status_msg_id, text=f"❌ Сбой: {str(e)}")

# ═══ HANDLERS ═══
bot = Bot(token=TELEGRAM_TOKEN)
dp = Dispatcher()

@dp.message(Command("start"))
async def cmd_start(message: types.Message):
    await message.answer(
        "🚀 <b>Мультиагентный ИИ с облачной памятью!</b>\n\n"
        "Я умею:\n"
        "• Писать код (Кодер)\n"
        "• Планировать и общаться (Ассистент)\n"
        "• Слушать голос (Whisper)\n"
        "• Помнить всё в Supabase (Облако)\n\n"
        "Команды:\n"
        "/tasks — мои задачи из облака\n"
        "/history — история переписки\n"
        "/clear — очистить задачи"
    )

@dp.message(Command("tasks"))
async def cmd_tasks(message: types.Message):
    tasks = db.get_tasks(message.from_user.id)
    if not tasks:
        await message.answer("📭 Задач в облаке нет.")
        return
    
    text = "📋 <b>Ваши задачи (Supabase):</b>\n\n"
    for i, t in enumerate(tasks, 1):
        text += f"{i}. {t['content']}\n"
    await message.answer(text, parse_mode="HTML")

@dp.message(Command("history"))
async def cmd_history(message: types.Message):
    history = db.get_recent_history(message.from_user.id, limit=10)
    if not history:
        await message.answer("📭 История пуста.")
        return
    
    text = "📜 <b>История:</b>\n\n"
    for h in history[-5:]: # Показываем последние 5
        emoji = "👤" if h['role'] == 'user' else "🤖"
        text += f"{emoji}: {h['message'][:100]}...\n"
    await message.answer(text)

@dp.message(Command("clear"))
async def cmd_clear(message: types.Message):
    db.clear_tasks(message.from_user.id)
    await message.answer("🗑️ Все задачи отмечены как выполненные в облаке.")

@dp.message(F.voice)
async def handle_voice(message: types.Message):
    status_msg = await message.answer("⏳ 🎧 Слушаю...")
    file_id = message.voice.file_id
    file = await bot.get_file(file_id)
    file_path = f"voice_{message.message_id}.ogg"
    
    try:
        await bot.download_file(file.file_path, file_path)
        text = await call_groq_whisper(file_path)
        if text:
            await bot.edit_message_text(
                chat_id=message.chat.id, message_id=status_msg.message_id,
                text=f"🎤 <b>Вы сказали:</b> <i>{text}</i>\n\n⏳ Думаю...", parse_mode="HTML"
            )
            await run_pipeline(text, message, bot, status_msg.message_id)
        else:
            await bot.edit_message_text(chat_id=message.chat.id, message_id=status_msg.message_id, text="❌ Не расслышал.")
    finally:
        if os.path.exists(file_path):
            os.remove(file_path)

@dp.message()
async def handle_text(message: types.Message):
    if not message.text: return
    status_msg = await message.answer("⏳ 🧠 Думаю...")
    await run_pipeline(message.text, message, bot, status_msg.message_id)

# ═══ ЗАПУСК ═══
async def main():
    await bot.session.close()
    logger.info("🚀 Бот запущен с Supabase Cloud!")
    try:
        await dp.start_polling(bot)
    finally:
        await bot.session.close()

if __name__ == "__main__":
    asyncio.run(main())
