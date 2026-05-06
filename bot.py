import os
import asyncio
import aiohttp
import json
import logging
from datetime import datetime, timedelta
from typing import List, Dict, Any, Optional

from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import Command
from dotenv import load_dotenv
from supabase import create_client, Client

# ═══ НАСТРОЙКА ЛОГИРОВАНИЯ ═══
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

load_dotenv()

# ═══ КОНФИГУРАЦИЯ ═══
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "").strip()
GROQ_API_KEY = os.getenv("GROQ_API_KEY", "").strip()
SUPABASE_URL = os.getenv("SUPABASE_URL", "").strip()
SUPABASE_KEY = os.getenv("SUPABASE_KEY", "").strip()

if not all([TELEGRAM_TOKEN, GROQ_API_KEY, SUPABASE_URL, SUPABASE_KEY]):
    raise ValueError("❌ ОШИБКА: Проверь переменные окружения в Railway!")

# Инициализация Supabase
try:
    supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)
    logger.info("✅ Supabase connected")
except Exception as e:
    logger.error(f"❌ Supabase error: {e}")
    raise e

# Настройки AI
GROQ_URL = "https://api.groq.com/openai/v1/chat/completions"
WHISPER_URL = "https://api.groq.com/openai/v1/audio/transcriptions"
TEXT_MODEL = "llama-3.1-8b-instant" # Или llama-3.3-70b-versatile если доступен
WHISPER_MODEL = "whisper-large-v3-turbo"

# ═══ БАЗА ДАННЫХ (WRAPPER) ═══
class DB:
    @staticmethod
    def ensure_user(tg_id: int, username: str, full_name: str = ""):
        try:
            # Проверяем есть ли пользователь
            resp = supabase.table("users").select("id").eq("telegram_id", tg_id).execute()
            if not resp.data:
                supabase.table("users").insert({
                    "telegram_id": tg_id,
                    "username": username,
                    "full_name": full_name
                }).execute()
            else:
                # Обновляем last_active
                supabase.table("users").update({"last_active": datetime.now().isoformat()}).eq("telegram_id", tg_id).execute()
        except Exception as e:
            logger.error(f"DB User Error: {e}")

    @staticmethod
    def add_task(tg_id: int, content: str, due_date: Optional[str] = None):
        try:
            supabase.table("tasks").insert({
                "telegram_id": tg_id,
                "content": content,
                "due_date": due_date,
                "status": "pending"
            }).execute()
            logger.info(f"Task saved: {content}")
        except Exception as e:
            logger.error(f"Task Error: {e}")

    @staticmethod
    def get_tasks(tg_id: int) -> List[Dict]:
        try:
            resp = supabase.table("tasks").select("*").eq("telegram_id", tg_id).eq("status", "pending").order("created_at", desc=True).limit(10).execute()
            return resp.data or []
        except Exception as e:
            logger.error(f"Get Tasks Error: {e}")
            return []

    @staticmethod
    def clear_tasks(tg_id: int):
        try:
            supabase.table("tasks").update({"status": "completed", "completed_at": datetime.now().isoformat()}).eq("telegram_id", tg_id).eq("status", "pending").execute()
        except Exception as e:
            logger.error(f"Clear Tasks Error: {e}")

    @staticmethod
    def add_history(tg_id: int, role: str, message: str, agent: str = "orchestrator"):
        try:
            supabase.table("chat_history").insert({
                "telegram_id": tg_id,
                "role": role,
                "message": message,
                "agent_type": agent
            }).execute()
        except Exception as e:
            logger.error(f"History Error: {e}")

    @staticmethod
    def get_history(tg_id: int, limit: int = 10) -> List[Dict]:
        try:
            resp = supabase.table("chat_history").select("*").eq("telegram_id", tg_id).order("created_at", desc=True).limit(limit).execute()
            return list(reversed(resp.data)) if resp.data else []
        except Exception as e:
            logger.error(f"Get History Error: {e}")
            return []

    @staticmethod
    def save_knowledge(tg_id: int, category: str, key: str, value: str):
        try:
            # Upsert логика: обновляем если есть, иначе создаем
            # Сначала пробуем найти
            resp = supabase.table("ai_knowledge").select("id").eq("telegram_id", tg_id).eq("category", category).eq("key_name", key).execute()
            if resp.data:
                supabase.table("ai_knowledge").update({"value": value, "updated_at": datetime.now().isoformat()}).eq("id", resp.data[0]['id']).execute()
            else:
                supabase.table("ai_knowledge").insert({
                    "telegram_id": tg_id,
                    "category": category,
                    "key_name": key,
                    "value": value
                }).execute()
        except Exception as e:
            logger.error(f"Knowledge Error: {e}")

    @staticmethod
    def get_knowledge(tg_id: int) -> str:
        try:
            resp = supabase.table("ai_knowledge").select("category, key_name, value").eq("telegram_id", tg_id).execute()
            if not resp.data:
                return "Нет сохраненных предпочтений."
            return "\n".join([f"- [{item['category']}] {item['key_name']}: {item['value']}" for item in resp.data])
        except Exception as e:
            logger.error(f"Get Knowledge Error: {e}")
            return "Ошибка загрузки знаний."

db = DB()

# ═══ ПРОМПТЫ АГЕНТОВ ═══

SYSTEM_ORCHESTRATOR = """
Ты — Orchestrator. Твоя задача: определить намерение пользователя и выбрать одного из специализированных агентов.
Доступные агенты:
1. CODE_EXPERT: Написание кода, отладка, объяснение технологий.
2. BUSINESS_COACH: Продажи, стратегия, доход, жесткая обратная связь, эффективность.
3. PERSONAL_ASSISTANT: Планирование, задачи, рутина, черновики писем.
4. SEARCH_AGENT: Поиск актуальной информации в интернете (симуляция), факты, новости.
5. ANALYTICS_AGENT: Анализ данных, метрик, переписки, выявление паттернов.
6. LEARNING_AGENT: Запоминание предпочтений пользователя, адаптация стиля.

Верни ТОЛЬКО JSON:
{"agent": "NAME", "reason": "почему выбран этот агент", "task_summary": "краткая суть"}
"""

AGENT_PROMPTS = {
    "CODE_EXPERT": "Ты Senior Developer. Пиши чистый, рабочий код. Без воды. С комментариями. Только решение.",
    "BUSINESS_COACH": """
    Ты жесткий Бизнес-тренер. Никакой мотивационной воды.
    - Фокус на метрики, деньги, действия.
    - Если видишь саботаж — дави фактами.
    - Формат: 🎯 Вывод, 📋 План, 💡 Инструмент, ✅ Шаг.
    """,
    "PERSONAL_ASSISTANT": """
    Ты личный ассистент. Стиль: деловой, краткий, проактивный.
    - Структурируй ответ.
    - Если видишь задачу с датой — предлагай напоминание.
    - Формат: Суть -> Детали -> Следующий шаг.
    """,
    "SEARCH_AGENT": """
    Ты Search Agent. Имитируй поиск актуальных данных.
    - Давай факты, цифры, даты.
    - Указывай источники (даже если симулируешь, делай вид что проверил).
    - Формат: 🔍 Запрос, 🎯 Ответ, 🔗 Источники.
    """,
    "ANALYTICS_AGENT": """
    Ты Аналитик. Превращай хаос в инсайты.
    - Ищи паттерны в поведении/продажах.
    - Формат: 📊 Отчет, 📈 Метрики, 💡 Инсайты, 🚀 Рекомендация.
    """,
    "LEARNING_AGENT": """
    Ты Learning Agent. Запоминай предпочтения пользователя.
    - Если пользователь хвалит/ругает стиль — фиксируй.
    - Адаптируй ответ под его стиль (кратко/подробно).
    """,
}

# ═══ AI ФУНКЦИИ ═══

async def call_groq(messages: List[Dict], system: str, json_mode: bool = False) -> Optional[str]:
    headers = {"Authorization": f"Bearer {GROQ_API_KEY}", "Content-Type": "application/json"}
    full_msgs = [{"role": "system", "content": system}] + messages
    
    payload = {
        "model": TEXT_MODEL,
        "messages": full_msgs,
        "temperature": 0.2,
        "max_tokens": 1500
    }
    if json_mode:
        payload["response_format"] = {"type": "json_object"}

    async with aiohttp.ClientSession() as session:
        try:
            async with session.post(GROQ_URL, json=payload, headers=headers, timeout=aiohttp.ClientTimeout(total=30)) as resp:
                if resp.status != 200:
                    err = await resp.text()
                    logger.error(f"Groq Error {resp.status}: {err}")
                    return None
                data = await resp.json()
                return data['choices'][0]['message']['content'].strip()
        except Exception as e:
            logger.error(f"Network Error: {e}")
            return None

async def call_whisper(file_path: str) -> Optional[str]:
    headers = {"Authorization": f"Bearer {GROQ_API_KEY}"}
    form = aiohttp.FormData()
    form.add_field('file', open(file_path, 'rb'), filename='audio.ogg')
    form.add_field('model', WHISPER_MODEL)
    
    async with aiohttp.ClientSession() as session:
        try:
            async with session.post(WHISPER_URL, data=form, headers=headers, timeout=aiohttp.ClientTimeout(total=60)) as resp:
                if resp.status != 200: return None
                data = await resp.json()
                return data.get('text', '').strip()
        except Exception as e:
            logger.error(f"Whisper Error: {e}")
            return None

# ═══ ЛОГИКА ПАЙПЛАЙНА ═══

async def process_request(text: str, message: types.Message, bot: Bot, status_msg_id: int):
    tg_id = message.from_user.id
    db.ensure_user(tg_id, message.from_user.username, message.from_user.full_name)
    
    # 1. Получаем контекст (история + знания)
    history = db.get_history(tg_id, limit=6)
    knowledge = db.get_knowledge(tg_id)
    
    # 2. Оркестратор выбирает агента
    router_msgs = history + [{"role": "user", "content": f"User Knowledge:\n{knowledge}\n\nRequest: {text}"}]
    router_resp = await call_groq(router_msgs, SYSTEM_ORCHESTRATOR, json_mode=True)
    
    if not router_resp:
        await bot.edit_message_text(chat_id=tg_id, message_id=status_msg_id, text="❌ Ошибка мышления (Router)")
        return

    try:
        clean_json = router_resp.replace("```json", "").replace("```", "").strip()
        decision = json.loads(clean_json)
        agent_name = decision.get("agent", "PERSONAL_ASSISTANT")
        reason = decision.get("reason", "")
        summary = decision.get("task_summary", text)
        
        logger.info(f"🤖 Orchestrator chose: {agent_name} ({reason})")
        
        await bot.edit_message_text(chat_id=tg_id, message_id=status_msg_id, text=f"⏳ Активирован агент: {agent_name}...")
        
        # 3. Вызов выбранного агента
        agent_prompt = AGENT_PROMPTS.get(agent_name, AGENT_PROMPTS["PERSONAL_ASSISTANT"])
        agent_msgs = history + [{"role": "user", "content": f"Task: {summary}\nOriginal: {text}"}]
        
        response_text = await call_groq(agent_msgs, agent_prompt)
        
        if not response_text:
            await bot.edit_message_text(chat_id=tg_id, message_id=status_msg_id, text="❌ Агент не ответил.")
            return

        # 4. Сохранение результата и анализ на наличие задач
        db.add_history(tg_id, "assistant", response_text, agent=agent_name)
        
        # Проверка: нужно ли сохранить задачу или знание? (простая эвристика)
        if "напомн" in response_text.lower() or "задач" in response_text.lower() or "план" in response_text.lower():
             # Можно добавить отдельный вызов LLM для экстракции задачи, пока упрощенно
             pass 
             
        # Формирование финального ответа
        final_msg = f"🤖 <b>{agent_name}</b>:\n\n{response_text}"
        await bot.edit_message_text(chat_id=tg_id, message_id=status_msg_id, text=final_msg, parse_mode="HTML")

    except json.JSONDecodeError:
        logger.error("JSON Decode Error")
        await bot.edit_message_text(chat_id=tg_id, message_id=status_msg_id, text="❌ Ошибка формата ответа.")
    except Exception as e:
        logger.error(f"Pipeline Error: {e}")
        await bot.edit_message_text(chat_id=tg_id, message_id=status_msg_id, text=f"❌ Сбой: {str(e)}")

# ═══ HANDLERS ═══
bot = Bot(token=TELEGRAM_TOKEN)
dp = Dispatcher()

@dp.message(Command("start"))
async def cmd_start(message: types.Message):
    await message.answer(
        "🚀 <b>Мультиагентная система активирована!</b>\n\n"
        "Доступные агенты:\n"
        "👨‍💻 Code Expert\n"
        "💼 Business Coach\n"
        "📅 Personal Assistant\n"
        "🔍 Search Agent\n"
        "📊 Analytics Agent\n"
        "🧠 Learning Agent\n\n"
        "Просто напиши задачу, я сам выберу исполнителя."
    )

@dp.message(Command("tasks"))
async def cmd_tasks(message: types.Message):
    tasks = db.get_tasks(message.from_user.id)
    if not tasks:
        await message.answer("📭 Задач нет.")
        return
    txt = "📋 <b>Ваши задачи:</b>\n"
    for t in tasks:
        txt += f"• {t['content']}\n"
    await message.answer(txt, parse_mode="HTML")

@dp.message(Command("clear"))
async def cmd_clear(message: types.Message):
    db.clear_tasks(message.from_user.id)
    await message.answer("🗑️ Задачи архивированы.")

@dp.message(F.voice)
async def handle_voice(message: types.Message):
    status = await message.answer("⏳ 🎧 Слушаю...")
    file_id = message.voice.file_id
    file = await bot.get_file(file_id)
    path = f"voice_{message.message_id}.ogg"
    try:
        await bot.download_file(file.file_path, path)
        text = await call_whisper(path)
        if text:
            await bot.edit_message_text(chat_id=message.chat.id, message_id=status.message_id, text=f"🎤 {text}\n\n⏳ Думаю...")
            await process_request(text, message, bot, status.message_id)
        else:
            await bot.edit_message_text(chat_id=message.chat.id, message_id=status.message_id, text="❌ Не расслышал.")
    finally:
        if os.path.exists(path): os.remove(path)

@dp.message()
async def handle_text(message: types.Message):
    if not message.text: return
    status = await message.answer("⏳ 🧠 Думаю...")
    await process_request(message.text, message, bot, status.message_id)

# ═══ ЗАПУСК ═══
async def main():
    await bot.session.close()
    logger.info("🚀 Bot started with Multi-Agent System")
    try:
        await dp.start_polling(bot)
    finally:
        await bot.session.close()

if __name__ == "__main__":
    asyncio.run(main())
