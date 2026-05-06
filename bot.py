import os
import asyncio
import aiohttp
import json
import logging
import re
from datetime import datetime
from typing import Optional, List, Dict, Any
from enum import Enum

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
    raise ValueError("❌ ОШИБКА: Проверьте переменные окружения (TELEGRAM, GROQ, SUPABASE)")

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
MAX_TOKENS = 1500
RETRIES = 2

# ═══ ТИПЫ АГЕНТОВ ═══
class AgentType(str, Enum):
    CODE = "code"
    COACH = "coach"
    ASSISTANT = "assistant"
    WRITER = "writer"
    ANALYST = "analyst"
    TRANSLATOR = "translator"

# ═══ ПРОМПТЫ ═══
ROUTER_PROMPT = """Ты — Orchestrator мультиагентного ИИ.
Определи тип запроса строго из списка: CODE, COACH, ASSISTANT, WRITER, ANALYST, TRANSLATOR.

Примеры:
- "напиши скрипт на python" → CODE
- "как поднять продажи?" → COACH
- "добавь задачу купить молоко" → ASSISTANT
- "напиши пост для блога" → WRITER
- "проанализируй данные продаж за месяц" → ANALYST
- "переведи на английский" → TRANSLATOR
- "сочини сказку" → WRITER
- "объясни баг в коде" → CODE

Верни ТОЛЬКО JSON: {"type": "<тип>", "summary": "краткая суть запроса"}"""

AGENT_PROMPTS = {
    AgentType.CODE: "Ты Senior Developer. Пиши чистый, эффективный код с комментариями. Используй ```python ... ``` для блоков кода.",
    AgentType.COACH: "Ты жёсткий бизнес-тренер. Только факты, метрики, конкретные шаги. Используй жирный шрифт и списки.",
    AgentType.ASSISTANT: "Ты личный ассистент. Работай с задачами пользователя из БД. Никогда не выдумывай задачи. Если просят добавить задачу — подтверди сохранение. Форматируй ответ маркированными списками.",
    AgentType.WRITER: "Ты креативный писатель. Сочиняй рассказы, посты, стихи. Используй образный язык, но оставайся понятным.",
    AgentType.ANALYST: "Ты аналитик данных. Интерпретируй информацию, находи закономерности, давай прогнозы. Ссылайся на логику и цифры.",
    AgentType.TRANSLATOR: "Ты профессиональный переводчик. Переводи точно, сохраняя смысл и стиль. Если язык не указан, переведи на английский."
}

# ═══ УТИЛИТЫ ═══
def safe_html(text: str) -> str:
    """Экранирует HTML-теги, разрешая только базовые."""
    if not text:
        return ""
    # Сначала заменяем запрещённые теги на сущности
    text = text.replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')
    # Возвращаем обратно разрешённые
    for tag in ['b', 'i', 'u', 's', 'code', 'pre', 'a']:
        text = text.replace(f'&lt;{tag}&gt;', f'<{tag}>')
        text = text.replace(f'&lt;/{tag}&gt;', f'</{tag}>')
    return text

def extract_json(text: str) -> dict:
    """Извлекает JSON объект из ответа модели."""
    # Убираем маркеры ```json ... ```
    cleaned = re.sub(r'```json\s*|\s*```', '', text).strip()
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError:
        # Пытаемся найти первый { ... }
        match = re.search(r'\{.*\}', cleaned, re.DOTALL)
        if match:
            return json.loads(match.group())
        raise

# ═══ БАЗА ДАННЫХ ═══
class Database:
    @staticmethod
    def ensure_user(tg_id: int, username: str, full_name: str = None):
        try:
            data, _ = supabase.table("users").select("id").eq("telegram_id", tg_id).execute()
            if not data or len(data) == 0:
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
            return True
        except Exception as e:
            logger.error(f"DB Task Error: {e}")
            return False

    @staticmethod
    def get_tasks(tg_id: int) -> List[Dict]:
        try:
            response = supabase.table("tasks").select("*").eq("telegram_id", tg_id).eq("status", "pending").order("created_at", desc=True).execute()
            return response.data or []
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
            if not response.data:
                return []
            clean_history = [{"role": item['role'], "content": item['message']} for item in sorted(response.data, key=lambda x: x['created_at'])]
            return clean_history
        except Exception as e:
            logger.error(f"DB History Get Error: {e}")
            return []

db = Database()

# ═══ AI ИНТЕГРАЦИЯ (с сессией и повтором) ═══
class GroqClient:
    def __init__(self, api_key: str):
        self.api_key = api_key
        self.session: Optional[aiohttp.ClientSession] = None

    async def start(self):
        if not self.session:
            self.session = aiohttp.ClientSession()

    async def close(self):
        if self.session:
            await self.session.close()

    async def _post(self, url, json_data=None, data=None, headers_extra=None, timeout=30):
        headers = {"Authorization": f"Bearer {self.api_key}"}
        if headers_extra:
            headers.update(headers_extra)
        for attempt in range(RETRIES + 1):
            try:
                async with self.session.post(url, json=json_data, data=data, headers=headers, timeout=aiohttp.ClientTimeout(total=timeout)) as resp:
                    if resp.status == 200:
                        return await resp.json()
                    else:
                        body = await resp.text()
                        logger.error(f"Groq HTTP {resp.status}: {body}")
                        if attempt == RETRIES:
                            return None
            except asyncio.TimeoutError:
                logger.error(f"Request timeout (attempt {attempt+1})")
                if attempt == RETRIES:
                    return None
            await asyncio.sleep(2 ** attempt)  # exponential backoff
        return None

    async def chat_completion(self, messages: List[Dict], system_prompt: str, response_format: Optional[str] = None) -> Optional[str]:
        full_messages = [{"role": "system", "content": system_prompt}] + messages
        payload = {
            "model": TEXT_MODEL,
            "messages": full_messages,
            "temperature": 0.2,
            "max_tokens": MAX_TOKENS
        }
        if response_format == "json":
            payload["response_format"] = {"type": "json_object"}
        data = await self._post(GROQ_URL, json_data=payload)
        if data:
            return data['choices'][0]['message']['content'].strip()
        return None

    async def transcribe_audio(self, audio_path: str) -> Optional[str]:
        form = aiohttp.FormData()
        form.add_field('file', open(audio_path, 'rb'), filename='audio.ogg')
        form.add_field('model', WHISPER_MODEL)
        data = await self._post(WHISPER_URL, data=form, timeout=60)
        if data:
            return data.get('text', '').strip()
        return None

groq = GroqClient(GROQ_API_KEY)

# ═══ ЛОГИКА ПАЙПЛАЙНА ═══
async def send_or_edit(bot: Bot, chat_id: int, message_id: int, text: str, parse_mode: str = "HTML"):
    """Отправляет или редактирует сообщение, при ошибке форматирования шлёт как plain text."""
    safe = safe_html(text)
    try:
        await bot.edit_message_text(chat_id=chat_id, message_id=message_id, text=safe, parse_mode=parse_mode)
    except Exception as e:
        logger.warning(f"Parse error: {e}. Sending as plain text.")
        try:
            await bot.edit_message_text(chat_id=chat_id, message_id=message_id, text=text, parse_mode=None)
        except Exception as e2:
            logger.error(f"Final send error: {e2}")

async def handle_agent(agent: AgentType, summary: str, original_text: str, messages: List[Dict], message: types.Message, bot: Bot, status_msg_id: int, tg_id: int):
    status_texts = {
        AgentType.CODE: "👨‍💻 Кодер пишет...",
        AgentType.COACH: "📈 Тренер анализирует...",
        AgentType.ASSISTANT: "🤖 Ассистент думает...",
        AgentType.WRITER: "✍️ Писатель творит...",
        AgentType.ANALYST: "📊 Аналитик изучает...",
        AgentType.TRANSLATOR: "🌐 Переводчик работает..."
    }
    await bot.edit_message_text(chat_id=message.chat.id, message_id=status_msg_id, text=f"⏳ {status_texts.get(agent, 'Думаю...')}")

    context = messages + [{"role": "user", "content": original_text}]

    # Особый контекст для ассистента (задачи)
    if agent == AgentType.ASSISTANT:
        tasks = db.get_tasks(tg_id)
        if tasks:
            task_list = "\n".join([f"- {t['content']}" for t in tasks])
            context.append({"role": "system", "content": f"Активные задачи пользователя:\n{task_list}"})
        else:
            context.append({"role": "system", "content": "У пользователя нет активных задач. Не выдумывай их."})

    result = await groq.chat_completion(context, AGENT_PROMPTS[agent], response_format=None)
    if not result:
        await send_or_edit(bot, message.chat.id, status_msg_id, "❌ Ошибка генерации ответа.")
        return

    # Сохраняем в историю
    db.add_history(tg_id, "assistant", result, agent.value)

    # Форматирование ответа в зависимости от агента
    prefix_map = {
        AgentType.CODE: f"💻 <b>Код:</b>\n<pre><code>{result}</code></pre>",
        AgentType.COACH: f"🔥 <b>Вердикт тренера:</b>\n\n{result}",
        AgentType.ASSISTANT: f"🤖 <b>Ответ:</b>\n\n{result}",
        AgentType.WRITER: f"✨ <b>Творческий результат:</b>\n\n{result}",
        AgentType.ANALYST: f"📈 <b>Аналитика:</b>\n\n{result}",
        AgentType.TRANSLATOR: f"🌐 <b>Перевод:</b>\n\n{result}"
    }
    final_text = prefix_map.get(agent, result)

    # Если ассистент и создание задачи (детектируем по намерению из summary)
    if agent == AgentType.ASSISTANT and any(kw in summary.lower() for kw in ["добавить задачу", "новая задача", "запланировать", "напомнить"]):
        success = db.add_task(tg_id, original_text)
        final_text += "\n\n✅ <i>Задача сохранена в облачную базу Supabase.</i>" if success else "\n\n⚠️ <i>Не удалось сохранить задачу.</i>"

    await send_or_edit(bot, message.chat.id, status_msg_id, final_text)

async def run_pipeline(text: str, message: types.Message, bot: Bot, status_msg_id: int, force_agent: Optional[AgentType] = None):
    tg_id = message.from_user.id
    db.ensure_user(tg_id, message.from_user.username, message.from_user.full_name)
    db.add_history(tg_id, "user", text, "user")

    # Если задан принудительный агент, используем его
    if force_agent:
        await handle_agent(force_agent, "forced", text, [], message, bot, status_msg_id, tg_id)
        return

    # Иначе маршрутизируем
    history = db.get_recent_history(tg_id, limit=4)
    router_input = history + [{"role": "user", "content": text}]
    try:
        router_json = await groq.chat_completion(router_input, ROUTER_PROMPT, response_format="json")
        if not router_json:
            raise Exception("Роутер не ответил")
        decision = extract_json(router_json)
        task_type = decision.get("type", "ASSISTANT").lower()
        summary = decision.get("summary", text[:100])
        # Проверяем валидность
        if task_type not in [a.value for a in AgentType]:
            task_type = AgentType.ASSISTANT.value
        agent = AgentType(task_type)
        logger.info(f"🔀 Маршрут: {agent.value} | {summary}")
        await handle_agent(agent, summary, text, history, message, bot, status_msg_id, tg_id)
    except Exception as e:
        logger.error(f"Pipeline error: {e}")
        await send_or_edit(bot, message.chat.id, status_msg_id, f"❌ Сбой маршрутизации: {str(e)}")

# ═══ ХРАНЕНИЕ РЕЖИМОВ ПОЛЬЗОВАТЕЛЕЙ ═══
user_modes: Dict[int, AgentType] = {}

# ═══ HANDLERS ═══
bot = Bot(token=TELEGRAM_TOKEN)
dp = Dispatcher()

@dp.message(Command("start"))
async def cmd_start(message: types.Message):
    await message.answer(
        "🚀 <b>Мультиагентный ИИ v2.0</b>\n\n"
        "Агенты: Кодер, Тренер, Ассистент, Писатель, Аналитик, Переводчик.\n"
        "Память: Supabase Cloud.\n\n"
        "Команды:\n"
        "/tasks — мои задачи\n"
        "/clear — очистить задачи\n"
        "/mode [тип] — принудительный агент (code,coach,assistant,writer,analyst,translator)\n"
        "/resetmode — сброс режима"
    )

@dp.message(Command("tasks"))
async def cmd_tasks(message: types.Message):
    tasks = db.get_tasks(message.from_user.id)
    if not tasks:
        await message.answer("📭 В базе задач нет.")
        return
    text = "📋 <b>Ваши задачи:</b>\n\n" + "\n".join([f"{i}. {t['content']}" for i, t in enumerate(tasks, 1)])
    await message.answer(text, parse_mode="HTML")

@dp.message(Command("clear"))
async def cmd_clear(message: types.Message):
    db.clear_tasks(message.from_user.id)
    await message.answer("🗑️ Все задачи архивированы.")

@dp.message(Command("mode"))
async def cmd_mode(message: types.Message):
    try:
        _, agent_str = message.text.split(maxsplit=1)
        agent_str = agent_str.strip().lower()
        if agent_str not in [a.value for a in AgentType]:
            await message.answer(f"❌ Неизвестный агент. Доступны: {', '.join([a.value for a in AgentType])}")
            return
        user_modes[message.from_user.id] = AgentType(agent_str)
        await message.answer(f"✅ Агент принудительно установлен: <b>{agent_str}</b>. Следующее сообщение будет обработано им.")
    except ValueError:
        await message.answer("Укажите тип: /mode code, /mode coach и т.д.")

@dp.message(Command("resetmode"))
async def cmd_resetmode(message: types.Message):
    user_modes.pop(message.from_user.id, None)
    await message.answer("🔁 Режим сброшен. Теперь ИИ сам выбирает агента.")

@dp.message(F.voice)
async def handle_voice(message: types.Message):
    status_msg = await message.answer("⏳ 🎧 Слушаю...")
    file_id = message.voice.file_id
    file = await bot.get_file(file_id)
    file_path = f"voice_{message.message_id}.ogg"
    try:
        await bot.download_file(file.file_path, file_path)
        text = await groq.transcribe_audio(file_path)
        if text:
            await bot.edit_message_text(
                chat_id=message.chat.id, message_id=status_msg.message_id,
                text=f"🎤 <b>Вы сказали:</b> <i>{text}</i>\n\n⏳ Думаю...", parse_mode="HTML"
            )
            mode = user_modes.get(message.from_user.id)
            await run_pipeline(text, message, bot, status_msg.message_id, force_agent=mode)
        else:
            await bot.edit_message_text(chat_id=message.chat.id, message_id=status_msg.message_id, text="❌ Не расслышал.")
    finally:
        if os.path.exists(file_path):
            os.remove(file_path)

@dp.message()
async def handle_text(message: types.Message):
    if not message.text:
        return
    status_msg = await message.answer("⏳ 🧠 Думаю...")
    mode = user_modes.get(message.from_user.id)
    await run_pipeline(message.text, message, bot, status_msg.message_id, force_agent=mode)

# ═══ ЗАПУСК ═══
async def main():
    await groq.start()  # инициализация HTTP сессии
    logger.info("🚀 Бот запущен с 6 агентами и Supabase Cloud!")
    try:
        await dp.start_polling(bot)
    finally:
        await groq.close()
        await bot.session.close()

if __name__ == "__main__":
    asyncio.run(main())
