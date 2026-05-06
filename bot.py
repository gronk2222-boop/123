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

from supabase import create_client, Client, PostgrestError

# ═══ НАСТРОЙКА ЛОГИРОВАНИЯ ═══
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

load_dotenv()

# ═══ КОНФИГУРАЦИЯ ═══
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
GROQ_API_KEY = os.getenv("GROQ_API_KEY")
SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")

# Валидация и очистка
if not all([TELEGRAM_TOKEN, GROQ_API_KEY, SUPABASE_URL, SUPABASE_KEY]):
    raise ValueError("❌ КРИТИЧЕСКАЯ ОШИБКА: Отсутствуют переменные окружения!")

TELEGRAM_TOKEN = TELEGRAM_TOKEN.strip()
GROQ_API_KEY = GROQ_API_KEY.strip()
SUPABASE_URL = SUPABASE_URL.strip()
SUPABASE_KEY = SUPABASE_KEY.strip()

# Проверка формата URL
if not SUPABASE_URL.startswith("https://") or not SUPABASE_URL.endswith(".supabase.co"):
    logger.error(f"⚠️ Неверный формат SUPABASE_URL: {SUPABASE_URL}")
    logger.error("Он должен выглядеть как https://xxxxx.supabase.co")

# Инициализация клиента
try:
    supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)
    logger.info("✅ Клиент Supabase создан")
except Exception as e:
    logger.error(f"❌ Ошибка инициализации Supabase: {e}")
    raise e

# Настройки AI
GROQ_URL = "https://api.groq.com/openai/v1/chat/completions"
WHISPER_URL = "https://api.groq.com/openai/v1/audio/transcriptions"
TEXT_MODEL = "llama-3.1-8b-instant"
WHISPER_MODEL = "whisper-large-v3-turbo"

# ═══ БАЗА ДАННЫХ (С ОБРАБОТКОЙ ОШИБОК) ═══

class Database:
    @staticmethod
    def ensure_user(tg_id: int, username: str):
        try:
            # Пробуем вставить, игнорируем дубликаты
            data, count = supabase.table("users").insert({
                "telegram_id": tg_id,
                "username": username
            }).execute()
            logger.info(f"User {tg_id} checked/created in DB")
        except Exception as e:
            logger.error(f"DB Error (ensure_user): {str(e)}")

    @staticmethod
    def add_task(tg_id: int, content: str, due_date: Optional[datetime] = None) -> bool:
        try:
            payload = {
                "telegram_id": tg_id,
                "content": content,
                "status": "pending"
            }
            if due_date:
                payload["due_date"] = due_date.isoformat()
            
            response = supabase.table("tasks").insert(payload).execute()
            logger.info(f"✅ Task saved: {content}")
            return True
        except PostgrestError as e:
            logger.error(f"❌ Postgrest Error (add_task): {e.message}")
            return False
        except Exception as e:
            logger.error(f"❌ General DB Error (add_task): {str(e)}")
            return False

    @staticmethod
    def get_tasks(tg_id: int) -> List[Dict]:
        try:
            response = supabase.table("tasks").select("*").eq("telegram_id", tg_id).eq("status", "pending").order("due_date", nullsfirst=False).execute()
            return response.data if response.data else []
        except Exception as e:
            logger.error(f"DB Error (get_tasks): {str(e)}")
            return []

    @staticmethod
    def clear_tasks(tg_id: int):
        try:
            supabase.table("tasks").update({"status": "completed"}).eq("telegram_id", tg_id).eq("status", "pending").execute()
            logger.info(f"Tasks cleared for {tg_id}")
        except Exception as e:
            logger.error(f"DB Error (clear_tasks): {str(e)}")

    @staticmethod
    def add_history(tg_id: int, role: str, message: str, agent: str = "unknown"):
        try:
            supabase.table("chat_history").insert({
                "telegram_id": tg_id,
                "role": role,
                "agent_type": agent,
                "message": message
            }).execute()
        except Exception as e:
            # Логируем ошибку, но не прерываем работу бота
            logger.warning(f"History save failed: {e}")

    @staticmethod
    def get_recent_history(tg_id: int, limit: int = 5) -> List[Dict]:
        try:
            response = supabase.table("chat_history").select("*").eq("telegram_id", tg_id).order("created_at", desc=True).limit(limit).execute()
            return list(reversed(response.data)) if response.data else []
        except Exception as e:
            logger.error(f"DB Error (get_history): {e}")
            return []

db = Database()

# ═══ ПРОМПТЫ АГЕНТОВ ═══

ROUTER_PROMPT = """
Ты — Оркестратор. Определи тип запроса и нужно ли создать задачу.
Верни ТОЛЬКО JSON: 
{
  "agent": "COACH" | "ASSISTANT" | "SEARCH" | "ANALYTICS",
  "is_task": true/false, 
  "task_content": "текст задачи" или null,
  "task_time": "YYYY-MM-DDTHH:MM:SS" или null
}
Правила:
- Если пользователь говорит "напомни", "поставь задачу", "встреча завтра" → is_task: true.
- Извлеки дату и время для task_time в формате ISO. Если время не указано, ставь null.
"""

AGENT_PROMPTS = {
    "COACH": "Ты жесткий бизнес-тренер. Никакой воды. Только факты, скрипты, метрики. Цель: рост дохода.",
    "ASSISTANT": "Ты личный ассистент. Структурируй, планируй, напоминай. Будь краток.",
    "SEARCH": "Ты поисковик. Давай факты с источниками. Если не знаешь — говори.",
    "ANALYTICS": "Ты аналитик. Ищи паттерны в данных. Давай инсайты."
}

# ═══ AI ФУНКЦИИ ═══

async def call_groq(messages: List[Dict], system_prompt: str, response_format: Optional[str] = None) -> Optional[str]:
    headers = {"Authorization": f"Bearer {GROQ_API_KEY}", "Content-Type": "application/json"}
    full_msgs = [{"role": "system", "content": system_prompt}] + messages
    
    payload = {
        "model": TEXT_MODEL,
        "messages": full_msgs,
        "temperature": 0.2,
        "max_tokens": 1000
    }
    if response_format == "json":
        payload["response_format"] = {"type": "json_object"}

    async with aiohttp.ClientSession() as session:
        try:
            async with session.post(GROQ_URL, json=payload, headers=headers, timeout=aiohttp.ClientTimeout(total=20)) as resp:
                if resp.status != 200:
                    logger.error(f"Groq API Error {resp.status}: {await resp.text()}")
                    return None
                data = await resp.json()
                return data['choices'][0]['message']['content'].strip()
        except Exception as e:
            logger.error(f"Network error: {e}")
            return None

async def call_whisper(audio_path: str) -> Optional[str]:
    headers = {"Authorization": f"Bearer {GROQ_API_KEY}"}
    form_data = aiohttp.FormData()
    form_data.add_field('file', open(audio_path, 'rb'), filename='audio.ogg')
    form_data.add_field('model', WHISPER_MODEL)

    async with aiohttp.ClientSession() as session:
        async with session.post(WHISPER_URL, data=form_data, headers=headers, timeout=aiohttp.ClientTimeout(total=60)) as resp:
            if resp.status != 200:
                return None
            data = await resp.json()
            return data.get('text', '').strip()

# ═══ ЛОГИКА ═══

async def handle_task_creation(text: str, tg_id: int, bot: Bot, msg_id: int) -> bool:
    """Проверяет текст на наличие задачи и сохраняет её"""
    router_resp = await call_groq(
        [{"role": "user", "content": text}], 
        ROUTER_PROMPT, 
        response_format="json"
    )
    
    if not router_resp:
        return False
        
    try:
        clean_json = router_resp.replace("```json", "").replace("```", "").strip()
        decision = json.loads(clean_json)
        
        if decision.get("is_task"):
            content = decision.get("task_content", text)
            due_str = decision.get("task_time")
            due_date = None
            
            if due_str:
                try:
                    due_date = datetime.fromisoformat(due_str.replace('Z', '+00:00'))
                except:
                    pass
            
            success = db.add_task(tg_id, content, due_date)
            return success
    except Exception as e:
        logger.error(f"Task parsing error: {e}")
    
    return False

async def process_request(text: str, message: types.Message, bot: Bot, status_msg_id: int):
    tg_id = message.from_user.id
    db.ensure_user(tg_id, message.from_user.username)
    db.add_history(tg_id, "user", text, "orchestrator")
    
    # 1. Попытка сохранить задачу
    task_created = await handle_task_creation(text, tg_id, bot, status_msg_id)
    
    # 2. Получение контекста
    history = db.get_recent_history(tg_id, limit=4)
    
    # 3. Определение агента (упрощенно)
    # В идеале здесь тоже нужен LLM-роутер, но для скорости сделаем эвристику или простой вызов
    # Для примера используем тот же роутер для выбора агента
    router_resp = await call_groq(
        [{"role": "user", "content": f"Выбери агента для: {text}"}],
        "Верни JSON: {'agent': 'COACH'|'ASSISTANT'|'SEARCH'}",
        response_format="json"
    )
    
    agent_name = "ASSISTANT"
    if router_resp:
        try:
            d = json.loads(router_resp.replace("```json","").replace("```",""))
            agent_name = d.get("agent", "ASSISTANT")
        except: pass
    
    system_prompt = AGENT_PROMPTS.get(agent_name, AGENT_PROMPTS["ASSISTANT"])
    
    # Добавляем инструкции про задачи
    if agent_name == "ASSISTANT":
        tasks = db.get_tasks(tg_id)
        if tasks:
            task_list = "\n".join([f"- {t['content']} ({t.get('due_date', 'без даты')})" for t in tasks])
            system_prompt += f"\n\n📋 ТЕКУЩИЕ ЗАДАЧИ ПОЛЬЗОВАТЕЛЯ (НЕ ВЫДУМЫВАЙ НОВЫЕ, ИСПОЛЬЗУЙ ЭТИ):\n{task_list}"
        elif task_created:
            system_prompt += "\n\nТолько что была добавлена новая задача. Подтверди это пользователю."

    # 4. Генерация ответа
    response_text = await call_groq(history + [{"role": "user", "content": text}], system_prompt)
    
    if response_text:
        db.add_history(tg_id, "assistant", response_text, agent_name.lower())
        
        final_msg = f"🤖 {agent_name}:\n\n{response_text}"
        if task_created:
            final_msg += "\n\n✅ <b>Задача сохранена в базу!</b>"
            
        await bot.edit_message_text(chat_id=tg_id, message_id=status_msg_id, text=final_msg, parse_mode="HTML")
    else:
        await bot.edit_message_text(chat_id=tg_id, message_id=status_msg_id, text="❌ Ошибка связи с ИИ")

# ═══ HANDLERS ═══
bot = Bot(token=TELEGRAM_TOKEN)
dp = Dispatcher()

@dp.message(Command("start"))
async def cmd_start(message: types.Message):
    await message.answer("🚀 Бот запущен! База данных подключена.\nПиши задачи или вопросы.")

@dp.message(Command("tasks"))
async def cmd_tasks(message: types.Message):
    tasks = db.get_tasks(message.from_user.id)
    if not tasks:
        await message.answer("📭 Задач нет.")
        return
    txt = "📋 <b>Твои задачи:</b>\n\n"
    for t in tasks:
        dt = t.get('due_date', '')[:16] if t.get('due_date') else ''
        txt += f"• {t['content']} <i>({dt})</i>\n"
    await message.answer(txt, parse_mode="HTML")

@dp.message(Command("clear"))
async def cmd_clear(message: types.Message):
    db.clear_tasks(message.from_user.id)
    await message.answer("🗑️ Задачи архивированы.")

@dp.message(F.voice)
async def handle_voice(message: types.Message):
    status = await message.answer("🎧 Слушаю...")
    file = await bot.get_file(message.voice.file_id)
    path = f"voice_{message.message_id}.ogg"
    await bot.download_file(file.file_path, path)
    
    text = await call_whisper(path)
    if os.path.exists(path): os.remove(path)
    
    if text:
        await bot.edit_message_text(f"🎤 Вы сказали: <i>{text}</i>", chat_id=message.chat.id, message_id=status.message_id, parse_mode="HTML")
        await process_request(text, message, bot, status.message_id)
    else:
        await bot.edit_message_text("❌ Не расслышал", chat_id=message.chat.id, message_id=status.message_id)

@dp.message()
async def handle_text(message: types.Message):
    if not message.text: return
    status = await message.answer("⏳ Думаю...")
    await process_request(message.text, message, bot, status.message_id)

async def main():
    await bot.session.close()
    logger.info("🚀 Start polling...")
    try:
        await dp.start_polling(bot)
    finally:
        await bot.session.close()

if __name__ == "__main__":
    asyncio.run(main())
