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
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
GROQ_API_KEY = os.getenv("GROQ_API_KEY")
SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")

if not all([TELEGRAM_TOKEN, GROQ_API_KEY, SUPABASE_URL, SUPABASE_KEY]):
    raise ValueError("❌ Проверьте переменные окружения!")

# Инициализация
supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)
logger.info("✅ Supabase connected")

# Настройки AI
GROQ_URL = "https://api.groq.com/openai/v1/chat/completions"
WHISPER_URL = "https://api.groq.com/openai/v1/audio/transcriptions"
MODEL_FAST = "llama-3.1-8b-instant" # Для роутинга и быстрых задач
MODEL_PRO = "llama-3.1-70b-versatile" # Для сложной аналитики и коучинга (если доступен, иначе 8b)
WHISPER_MODEL = "whisper-large-v3-turbo"

# ═══ БАЗА ДАННЫХ (SUPABASE) ═══
class CloudDB:
    @staticmethod
    def ensure_user(tg_id: int, username: str):
        try:
            supabase.table("users").insert({"telegram_id": tg_id, "username": username}).execute()
        except: pass # Ignore duplicates

    @staticmethod
    def add_task(tg_id: int, content: str, deadline: Optional[str] = None):
        supabase.table("tasks").insert({
            "telegram_id": tg_id, "content": content, 
            "deadline": deadline, "is_completed": False
        }).execute()

    @staticmethod
    def get_tasks(tg_id: int) -> List[Dict]:
        res = supabase.table("tasks").select("*").eq("telegram_id", tg_id).eq("is_completed", False).execute()
        return res.data or []

    @staticmethod
    def get_history(tg_id: int, limit: int = 10) -> List[Dict]:
        res = supabase.table("chat_history").select("*").eq("telegram_id", tg_id).order("created_at", desc=True).limit(limit).execute()
        return list(reversed(res.data)) if res.data else []

    @staticmethod
    def save_history(tg_id: int, role: str, msg: str):
        supabase.table("chat_history").insert({"telegram_id": tg_id, "role": role, "message": msg}).execute()

    @staticmethod
    def get_knowledge(tg_id: int) -> str:
        res = supabase.table("ai_knowledge").select("category, key_name, value").eq("telegram_id", tg_id).execute()
        if not res.data: return "Нет сохраненных предпочтений."
        return "\n".join([f"- [{i['category']}] {i['key_name']}: {i['value']}" for i in res.data])

    @staticmethod
    def save_knowledge(tg_id: int, category: str, key: str, value: str):
        # Простая логика: обновляем или добавляем (в продакшене нужен upsert)
        supabase.table("ai_knowledge").insert({
            "telegram_id": tg_id, "category": category, "key_name": key, "value": value
        }).execute()

db = CloudDB()

# ═══ СИСТЕМНЫЕ ПРОМПТЫ АГЕНТОВ ═══

PROMPT_ORCHESTRATOR = """
Ты — Orchestrator. Твоя задача: классифицировать запрос и выбрать АГЕНТА.
Доступные агенты:
1. BUSINESS_COACH: Продажи, стратегия, доход, жесткая обратная связь, борьба с прокрастинацией.
2. PERSONAL_ASSISTANT: Планирование, задачи, дедлайны, рутина, черновики писем.
3. SEARCH_AGENT: Поиск актуальных данных, трендов, фактов (имитация через общие знания).
4. ANALYTICS_AGENT: Анализ метрик, переписки, паттернов поведения, отчеты.
5. LEARNING_AGENT: Запоминание предпочтений, адаптация стиля, анализ обратной связи.
6. CODER: Написание кода, отладка, технические задачи.

Верни ТОЛЬКО JSON:
{
  "agent": "NAME", 
  "reason": "почему выбран этот агент",
  "needs_research": true/false,
  "has_deadline": true/false,
  "deadline_time": "YYYY-MM-DD HH:MM" или null
}
"""

# Общие правила стиля для всех агентов
STYLE_GUIDE = """
[СТИЛЬ ОТВЕТА]
- Без воды, мотивационных соплей и клише ("ты можешь!", "важно отметить").
- Конкретика: цифры, факты, примеры, скрипты.
- Структура: Используй эмодзи-навигацию (🎯📋✅❓💡).
- Язык: Русский, деловой, краткий.
- Финал: Всегда заканчивай конкретным шагом (✅) или вопросом (❓).
- Если не знаешь — говори прямо.
"""

AGENTS_PROMPTS = {
    "BUSINESS_COACH": f"""
    {STYLE_GUIDE}
    РОЛЬ: Бизнес-тренер по продажам. Жесткий, проактивный, ориентированный на метрики.
    ЗАДАЧИ: Анализ воронки, скрипты, стратегии дохода, работа с возражениями.
    ФОРМАТ:
    🎯 [Вывод]
    📋 [План/Аргументы]
    💡 [Инструмент/Скрипт]
    ✅ [Шаг сейчас]
    ❓ [Вопрос]
    """,
    
    "PERSONAL_ASSISTANT": f"""
    {STYLE_GUIDE}
    РОЛЬ: Персональный ассистент. Проактивный, организованный.
    ЗАДАЧИ: Управление задачами, календарем, черновики, напоминания.
    ОСОБОЕ: Если видишь задачу с датой — предлагай напомнить.
    ФОРМАТ:
    [Суть]
    📋 [Детали]
    ✅ [Что сделано/Напоминание]
    ❓ [Уточнение]
    """,

    "SEARCH_AGENT": f"""
    {STYLE_GUIDE}
    РОЛЬ: Search Agent. Поиск и проверка информации.
    ЗАДАЧИ: Факты, тренды, цены, конкуренты.
    ФОРМАТ:
    🔍 [Запрос]
    🎯 [Ответ суть]
    📋 [Детали с источниками]
    ⚠️ [Примечание о достоверности]
    """,

    "ANALYTICS_AGENT": f"""
    {STYLE_GUIDE}
    РОЛЬ: Analytics Agent. Анализ данных и коммуникаций.
    ЗАДАЧИ: Инсайты из переписки, метрики, паттерны.
    ФОРМАТ:
    📊 [Отчет]
    🎯 [Главный инсайт]
    💡 [Топ-3 инсайта]
    🚀 [Рекомендация]
    """,

    "LEARNING_AGENT": f"""
    {STYLE_GUIDE}
    РОЛЬ: Learning Agent. Адаптация и запоминание.
    ЗАДАЧИ: Запоминать предпочтения, анализировать успешные действия.
    ДЕЙСТВИЕ: Если видишь новое предпочтение или паттерн — помечай его для сохранения в БД.
    """,

    "CODER": f"""
    {STYLE_GUIDE}
    РОЛЬ: Senior Developer.
    ЗАДАЧИ: Чистый код, без воды, с комментариями.
    ФОРМАТ:
    💻 [Код в блоке]
    ✅ [Как запустить/проверить]
    """
}

# ═══ AI ФУНКЦИИ ═══

async def call_groq(messages: List[Dict], model: str = MODEL_FAST, json_mode: bool = False) -> Optional[str]:
    headers = {"Authorization": f"Bearer {GROQ_API_KEY}", "Content-Type": "application/json"}
    payload = {
        "model": model,
        "messages": messages,
        "temperature": 0.2,
        "max_tokens": 1500
    }
    if json_mode:
        payload["response_format"] = {"type": "json_object"}

    async with aiohttp.ClientSession() as session:
        async with session.post(GROQ_URL, json=payload, headers=headers, timeout=aiohttp.ClientTimeout(total=45)) as resp:
            if resp.status != 200:
                logger.error(f"Groq Error: {resp.status}")
                return None
            data = await resp.json()
            return data['choices'][0]['message']['content'].strip()

async def transcribe_voice(file_path: str) -> Optional[str]:
    headers = {"Authorization": f"Bearer {GROQ_API_KEY}"}
    form = aiohttp.FormData()
    form.add_field('file', open(file_path, 'rb'), filename='audio.ogg')
    form.add_field('model', WHISPER_MODEL)
    
    async with aiohttp.ClientSession() as session:
        async with session.post(WHISPER_URL, data=form, headers=headers, timeout=aiohttp.ClientTimeout(total=60)) as resp:
            if resp.status != 200: return None
            data = await resp.json()
            return data.get('text', '').strip()

# ═══ ЛОГИКА ОРКЕСТРАЦИИ ═══

async def process_request(text: str, user_id: int, username: str):
    db.ensure_user(user_id, username)
    db.save_history(user_id, "user", text)
    
    # 1. Получаем контекст
    history = db.get_history(user_id, limit=6)
    knowledge = db.get_knowledge(user_id)
    
    context_messages = history + [{"role": "system", "content": f"Контекст пользователя:\n{knowledge}"}]

    # 2. Роутинг (Orchestrator)
    router_msgs = context_messages + [{"role": "user", "content": f"Запрос: {text}"}]
    # Добавляем системный промпт роутера в начало
    router_msgs.insert(0, {"role": "system", "content": PROMPT_ORCHESTRATOR})
    
    router_response = await call_groq(router_msgs, model=MODEL_FAST, json_mode=True)
    if not router_response:
        return "❌ Ошибка маршрутизации."
    
    try:
        decision = json.loads(router_response.replace("```json", "").replace("```", ""))
        agent_name = decision.get("agent", "PERSONAL_ASSISTANT")
        has_deadline = decision.get("has_deadline", False)
        deadline_time = decision.get("deadline_time")
    except:
        agent_name = "PERSONAL_ASSISTANT"

    # 3. Выбор промпта агента
    system_prompt = AGENTS_PROMPTS.get(agent_name, AGENTS_PROMPTS["PERSONAL_ASSISTANT"])
    
    # 4. Выполнение задачи агентом
    agent_msgs = context_messages + [{"role": "user", "content": text}]
    agent_msgs.insert(0, {"role": "system", "content": system_prompt})
    
    result = await call_groq(agent_msgs, model=MODEL_PRO if agent_name in ["BUSINESS_COACH", "ANALYTICS_AGENT"] else MODEL_FAST)
    
    if not result:
        return "❌ Агент не ответил."

    # 5. Пост-обработка (Задачи и Обучение)
    final_response = result
    
    # Если есть дедлайн
    if has_deadline and deadline_time:
        db.add_task(user_id, f"Напоминание: {text}", deadline_time)
        final_response += "\n\n⏰ **Напоминание установлено!**"
    
    # Сохраняем ответ
    db.save_history(user_id, "assistant", final_response)
    
    return final_response

# ═══ TELEGRAM HANDLERS ═══
bot = Bot(token=TELEGRAM_TOKEN)
dp = Dispatcher()

@dp.message(Command("start"))
async def cmd_start(message: types.Message):
    await message.answer(
        "🚀 <b>Мультиагентная система активирована</b>\n\n"
        "Доступные роли:\n"
        "🎯 Business Coach (Продажи, стратегия)\n"
        "📋 Personal Assistant (Задачи, план)\n"
        "🔍 Search Agent (Факты, тренды)\n"
        "📊 Analytics Agent (Инсайты)\n"
        "🧠 Learning Agent (Адаптация)\n"
        "💻 Coder (Код)\n\n"
        "Просто напиши задачу. Я сам выберу нужного специалиста."
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

@dp.message(F.voice)
async def handle_voice(message: types.Message):
    status = await message.answer("🎧 Расшифровываю...")
    file = await bot.get_file(message.voice.file_id)
    path = f"voice_{message.message_id}.ogg"
    await bot.download_file(file.file_path, path)
    
    try:
        text = await transcribe_voice(path)
        if text:
            await status.edit_text(f"🎤 <i>{text}</i>\n\n⏳ Думаю...", parse_mode="HTML")
            response = await process_request(text, message.from_user.id, message.from_user.username)
            await status.edit_text(response, parse_mode="HTML")
        else:
            await status.edit_text("❌ Не расслышал.")
    finally:
        if os.path.exists(path): os.remove(path)

@dp.message()
async def handle_text(message: types.Message):
    if not message.text: return
    status = await message.answer("⏳ Оркестратор выбирает агента...")
    try:
        response = await process_request(message.text, message.from_user.id, message.from_user.username)
        await status.edit_text(response, parse_mode="HTML")
    except Exception as e:
        logger.error(e)
        await status.edit_text(f"❌ Ошибка: {str(e)}")

# ═══ ЗАПУСК ═══
async def main():
    await bot.session.close()
    logger.info("🚀 Multi-Agent System Started")
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
