import os
import asyncio
import aiohttp
import json
import logging
import re
from datetime import datetime
from typing import Optional, List, Dict, Any

from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import Command
from dotenv import load_dotenv
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

if not all([TELEGRAM_TOKEN, GROQ_API_KEY, SUPABASE_URL, SUPABASE_KEY]):
    raise ValueError("❌ ОШИБКА: Проверьте переменные окружения")

TELEGRAM_TOKEN = TELEGRAM_TOKEN.strip()
GROQ_API_KEY = GROQ_API_KEY.strip()
SUPABASE_URL = SUPABASE_URL.strip()
SUPABASE_KEY = SUPABASE_KEY.strip()

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

# ═══ УТИЛИТЫ ═══

def clean_html_tags(text: str) -> str:
    """Удаляет неподдерживаемые Telegram теги (div, span, class), оставляя b, i, code, pre"""
    if not text:
        return ""
    # Удаляем теги с атрибутами class, style и т.д. (частая проблема LLM)
    text = re.sub(r'<(\w+)([^>]*)>', lambda m: f'<{m.group(1)}>' if m.group(1) in ['b', 'i', 'u', 's', 'code', 'pre', 'a'] else '', text)
    # Заменяем оставшиеся недопустимые теги на пустоту или эквивалент
    # div, p, span, h1-h6, ul, li -> убираем теги, оставляем текст
    text = re.sub(r'</?(div|p|span|h[1-6]|ul|ol|li|br)[^>]*>', '\n', text)
    # Убираем двойные переносы строк
    text = re.sub(r'\n\s*\n', '\n\n', text)
    return text.strip()

# ═══ БАЗА ДАННЫХ ═══

class Database:
    @staticmethod
    def ensure_user(tg_id: int, username: str, full_name: str = None):
        try:
            data, _ = supabase.table("users").select("id").eq("telegram_id", tg_id).execute()
            if not 
                supabase.table("users").insert({
                    "telegram_id": tg_id,
                    "username": username,
                    "full_name": full_name
                }).execute()
            else:
                supabase.table("users").update({"last_active": datetime.now().isoformat()}).eq("telegram_id", tg_id).execute()
        except Exception as e:
            logger.error(f"DB User Error: {e}")

    @staticmethod
    def add_task(tg_id: int, content: str, due_date: str = None):
        try:
            task_data = {
                "telegram_id": tg_id,
                "content": content,
                "status": "pending",
                "created_at": datetime.now().isoformat()
            }
            if due_date:
                task_data["due_date"] = due_date
            supabase.table("tasks").insert(task_data).execute()
            logger.info(f"✅ Task saved: {content}")
            return True
        except Exception as e:
            logger.error(f"DB Task Error: {e}")
            return False

    @staticmethod
    def get_tasks(tg_id: int) -> List[Dict]:
        try:
            response = supabase.table("tasks").select("*").eq("telegram_id", tg_id).eq("status", "pending").order("created_at", desc=True).execute()
            return response.data if response.data else []
        except Exception as e:
            logger.error(f"DB Get Tasks Error: {e}")
            return []

    @staticmethod
    def clear_tasks(tg_id: int):
        try:
            supabase.table("tasks").update({"status": "completed", "completed_at": datetime.now().isoformat()}).eq("telegram_id", tg_id).eq("status", "pending").execute()
        except Exception as e:
            logger.error(f"DB Clear Error: {e}")

    @staticmethod
    def add_history(tg_id: int, role: str, message: str, agent: str = "orchestrator"):
        try:
            supabase.table("chat_history").insert({
                "telegram_id": tg_id,
                "role": role,
                "agent_type": agent,
                "message": message
            }).execute()
        except Exception as e:
            logger.error(f"DB History Error: {e}")

    @staticmethod
    def get_recent_history(tg_id: int, limit: int = 5) -> List[Dict]:
        try:
            response = supabase.table("chat_history").select("*").eq("telegram_id", tg_id).order("created_at", desc=True).limit(limit).execute()
            if not response.
                return []
            clean_history = []
            for item in response.
                clean_history.append({"role": item['role'], "content": item['message']})
            return list(reversed(clean_history))
        except Exception as e:
            logger.error(f"DB History Get Error: {e}")
            return []

db = Database()

# ═══ ПРОМПТЫ ═══

ROUTER_PROMPT = """
Ты — Orchestrator. Определи тип запроса: CODE, COACH, ASSISTANT.
Верни ТОЛЬКО JSON: {"type": "CODE"|"COACH"|"ASSISTANT", "summary": "суть"}
"""

CODER_PROMPT = """
Ты Senior Developer. Пиши чистый код.
Используй Markdown для форматирования (```, **жирный**). НЕ используй HTML теги (<div>, <span>).
"""

COACH_PROMPT = """
Ты жесткий бизнес-тренер. Только факты и шаги.
Используй Markdown (**жирный**, списки). НЕ используй HTML теги (<div>, <span>, <class>).
"""

ASSISTANT_PROMPT = """
Ты личный ассистент. Не выдумывай задачи.
Используй Markdown. НЕ используй HTML теги.
Если задач нет в контексте — пиши, что их нет.
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

# ═══ ОБРАБОТЧИКИ АГЕНТОВ ═══

async def send_safe_message(bot: Bot, chat_id: int, message_id: int, text: str, parse_mode: str = "HTML"):
    """Отправляет сообщение, очищая его от битых тегов"""
    clean_text = clean_html_tags(text)
    # Если после очистки текст пуст, возвращаем исходный (на всякий случай)
    if not clean_text:
        clean_text = text
    
    try:
        await bot.edit_message_text(chat_id=chat_id, message_id=message_id, text=clean_text, parse_mode=parse_mode)
    except Exception as e:
        logger.warning(f"Parse error, sending as plain text: {e}")
        # Фолбэк: отправляем без разметки
        await bot.edit_message_text(chat_id=chat_id, message_id=message_id, text=clean_text.replace('<', '').replace('>', ''))

async def process_code_task(summary: str, original_text: str, messages: List[Dict], message: types.Message, bot: Bot, status_msg_id: int):
    await bot.edit_message_text(chat_id=message.chat.id, message_id=status_msg_id, text="⏳ 👨‍💻 Кодер пишет...")
    context = messages + [{"role": "user", "content": f"Задача: {original_text}"}]
    result = await call_groq_text(context, CODER_PROMPT)
    if result:
        db.add_history(message.from_user.id, "assistant", result, "coder")
        # Для кода лучше использовать Markdown, но мы используем HTML обертку с тегом <code>
        # Заменим тройные кавычки на тег <pre> для HTML режима
        formatted_result = result.replace("```python", "<pre>").replace("```", "</pre>")
        await send_safe_message(bot, message.chat.id, status_msg_id, f"💻 <b>Код:</b>\n\n{formatted_result}")
    else:
        await bot.edit_message_text(chat_id=message.chat.id, message_id=status_msg_id, text="❌ Ошибка генерации кода.")

async def process_coach_task(summary: str, original_text: str, messages: List[Dict], message: types.Message, bot: Bot, status_msg_id: int):
    await bot.edit_message_text(chat_id=message.chat.id, message_id=status_msg_id, text="⏳ 📈 Тренер анализирует...")
    context = messages + [{"role": "user", "content": original_text}]
    result = await call_groq_text(context, COACH_PROMPT)
    if result:
        db.add_history(message.from_user.id, "assistant", result, "coach")
        await send_safe_message(bot, message.chat.id, status_msg_id, f"🔥 <b>Вердикт тренера:</b>\n\n{result}")
    else:
        await bot.edit_message_text(chat_id=message.chat.id, message_id=status_msg_id, text="❌ Ошибка ответа тренера.")

async def process_assistant_task(summary: str, original_text: str, messages: List[Dict], message: types.Message, bot: Bot, status_msg_id: int, is_task_creation: bool = False):
    await bot.edit_message_text(chat_id=message.chat.id, message_id=status_msg_id, text="⏳ 🤖 Ассистент думает...")
    
    real_tasks = db.get_tasks(message.from_user.id)
    tasks_context = ""
    if real_tasks:
        tasks_list = "\n".join([f"- {t['content']}" for t in real_tasks])
        tasks_context = f"\n\n[СИСТЕМА: Активные задачи:\n{tasks_list}]"
    else:
        tasks_context = "\n\n[СИСТЕМА: Задач нет. Не выдумывай.]"

    context_text = f"{original_text}{tasks_context}"
    context_messages = messages + [{"role": "user", "content": context_text}]
    
    result = await call_groq_text(context_messages, ASSISTANT_PROMPT)
    
    if result:
        db.add_history(message.from_user.id, "assistant", result, "assistant")
        final_text = f"🤖 <b>Ответ:</b>\n\n{result}"
        
        if is_task_creation:
            success = db.add_task(message.from_user.id, original_text)
            if success:
                final_text += "\n\n✅ <i>Задача сохранена в базу.</i>"
            else:
                final_text += "\n\n⚠️ <i>Ошибка сохранения.</i>"
            
        await send_safe_message(bot, message.chat.id, status_msg_id, final_text)
    else:
        await bot.edit_message_text(chat_id=message.chat.id, message_id=status_msg_id, text="❌ Ошибка ответа.")

async def run_pipeline(text_content: str, message: types.Message, bot: Bot, status_msg_id: int):
    tg_id = message.from_user.id
    db.ensure_user(tg_id, message.from_user.username, message.from_user.full_name)
    db.add_history(tg_id, "user", text_content, "user")
    
    history = db.get_recent_history(tg_id, limit=4)
    
    try:
        router_input = history + [{"role": "user", "content": text_content}]
        router_json = await call_groq_text(router_input, ROUTER_PROMPT, response_format="json")
        
        if not router_json: raise Exception("Роутер молчит")
        
        clean_json = router_json.replace("```json", "").replace("```", "").strip()
        decision = json.loads(clean_json)
        
        task_type = decision.get("type", "ASSISTANT")
        summary = decision.get("summary", "")
        logger.info(f"🔀 Маршрут: {task_type}")

        is_task_creation = any(word in text_content.lower() for word in ["добавь задачу", "напомни", "поставь задачу", "запланируй", "сохрани задачу"])

        if task_type == "CODE":
            await process_code_task(summary, text_content, history, message, bot, status_msg_id)
        elif task_type == "COACH":
            await process_coach_task(summary, text_content, history, message, bot, status_msg_id)
        else:
            await process_assistant_task(summary, text_content, history, message, bot, status_msg_id, is_task_creation=is_task_creation)
            
    except Exception as e:
        logger.error(f"Pipeline error: {e}")
        await bot.edit_message_text(chat_id=message.chat.id, message_id=status_msg_id, text=f"❌ Сбой: {str(e)}")

# ═══ HANDLERS ═══
bot = Bot(token=TELEGRAM_TOKEN)
dp = Dispatcher()

@dp.message(Command("start"))
async def cmd_start(message: types.Message):
    await message.answer("🚀 <b>Мультиагентный ИИ запущен!</b>\n\nКоманды:\n/tasks — задачи\n/clear — очистить")

@dp.message(Command("tasks"))
async def cmd_tasks(message: types.Message):
    tasks = db.get_tasks(message.from_user.id)
    if not tasks:
        await message.answer("📭 Задач нет.")
        return
    text = "📋 <b>Ваши задачи:</b>\n\n" + "\n".join([f"{i}. {t['content']}" for i, t in enumerate(tasks, 1)])
    await message.answer(text, parse_mode="HTML")

@dp.message(Command("clear"))
async def cmd_clear(message: types.Message):
    db.clear_tasks(message.from_user.id)
    await message.answer("🗑️ Задачи архивированы.")

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
            await bot.edit_message_text(chat_id=message.chat.id, message_id=status_msg.message_id, text=f"🎤 <b>Вы сказали:</b> <i>{text}</i>\n\n⏳ Думаю...", parse_mode="HTML")
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
    logger.info("🚀 Бот запущен!")
    try:
        await dp.start_polling(bot)
    finally:
        await bot.session.close()

if __name__ == "__main__":
    asyncio.run(main())
