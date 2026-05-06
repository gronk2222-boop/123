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
from aiogram.types import (
    ReplyKeyboardMarkup, KeyboardButton,
    InlineKeyboardMarkup, InlineKeyboardButton
)
from supabase import create_client, Client

# ═══ НАСТРОЙКА ЛОГИРОВАНИЯ ═══
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# ═══ КОНФИГУРАЦИЯ (переменные окружения на Railway) ═══
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
    if not text:
        return ""
    text = text.replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')
    for tag in ['b', 'i', 'u', 's', 'code', 'pre', 'a']:
        text = text.replace(f'&lt;{tag}&gt;', f'<{tag}>')
        text = text.replace(f'&lt;/{tag}&gt;', f'</{tag}>')
    return text

def extract_json(text: str) -> dict:
    cleaned = re.sub(r'```json\s*|\s*```', '', text).strip()
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError:
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

# ═══ AI ИНТЕГРАЦИЯ ═══
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
            await asyncio.sleep(2 ** attempt)
        return None

    async def chat_completion(self, messages, system_prompt, response_format=None):
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

    async def transcribe_audio(self, audio_path):
        form = aiohttp.FormData()
        form.add_field('file', open(audio_path, 'rb'), filename='audio.ogg')
        form.add_field('model', WHISPER_MODEL)
        data = await self._post(WHISPER_URL, data=form, timeout=60)
        if data:
            return data.get('text', '').strip()
        return None

groq = GroqClient(GROQ_API_KEY)

# ═══ КЛАВИАТУРЫ ═══
def get_main_keyboard():
    buttons = [
        [KeyboardButton(text="📋 Мои задачи"), KeyboardButton(text="🗑 Очистить задачи")],
        [KeyboardButton(text="🤖 Сменить агента"), KeyboardButton(text="❓ Помощь")]
    ]
    return ReplyKeyboardMarkup(
        keyboard=buttons,
        resize_keyboard=True,
        input_field_placeholder="Выберите действие или напишите запрос..."
    )

def get_agents_inline_keyboard():
    agents = list(AgentType)
    buttons = []
    row = []
    for agent in agents:
        emoji = {
            AgentType.CODE: "🧑‍💻",
            AgentType.COACH: "📈",
            AgentType.ASSISTANT: "🤖",
            AgentType.WRITER: "✍️",
            AgentType.ANALYST: "📊",
            AgentType.TRANSLATOR: "🌐"
        }.get(agent, "🤖")
        row.append(InlineKeyboardButton(
            text=f"{emoji} {agent.value.capitalize()}",
            callback_data=f"set_agent:{agent.value}"
        ))
        if len(row) == 3:
            buttons.append(row)
            row = []
    if row:
        buttons.append(row)
    return InlineKeyboardMarkup(inline_keyboard=buttons)

def get_tasks_inline_keyboard():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📋 Мои задачи", callback_data="show_tasks"),
         InlineKeyboardButton(text="🗑 Очистить", callback_data="clear_tasks")]
    ])

# ═══ СОСТОЯНИЕ ПОЛЬЗОВАТЕЛЕЙ ═══
user_modes: Dict[int, AgentType] = {}

# ═══ ЛОГИКА ПАЙПЛАЙНА ═══
async def send_or_edit(bot: Bot, chat_id: int, message_id: int, text: str, reply_markup=None, parse_mode="HTML"):
    safe = safe_html(text)
    try:
        await bot.edit_message_text(
            chat_id=chat_id, message_id=message_id,
            text=safe, parse_mode=parse_mode, reply_markup=reply_markup
        )
    except Exception as e:
        logger.warning(f"Parse error: {e}. Sending as plain text.")
        try:
            await bot.edit_message_text(
                chat_id=chat_id, message_id=message_id,
                text=text, reply_markup=reply_markup
            )
        except Exception as e2:
            logger.error(f"Final send error: {e2}")

async def handle_agent(agent: AgentType, summary: str, original_text: str, messages: List[Dict],
                       message: types.Message, bot: Bot, status_msg_id: int, tg_id: int):
    status_texts = {
        AgentType.CODE: "👨‍💻 Кодер пишет...",
        AgentType.COACH: "📈 Тренер анализирует...",
        AgentType.ASSISTANT: "🤖 Ассистент думает...",
        AgentType.WRITER: "✍️ Писатель творит...",
        AgentType.ANALYST: "📊 Аналитик изучает...",
        AgentType.TRANSLATOR: "🌐 Переводчик работает..."
    }
    await bot.edit_message_text(
        chat_id=message.chat.id, message_id=status_msg_id,
        text=f"⏳ {status_texts.get(agent, 'Думаю...')}"
    )

    context = messages + [{"role": "user", "content": original_text}]
    if agent == AgentType.ASSISTANT:
        tasks = db.get_tasks(tg_id)
        if tasks:
            task_list = "\n".join([f"- {t['content']}" for t in tasks])
            context.append({"role": "system", "content": f"Активные задачи пользователя:\n{task_list}"})
        else:
            context.append({"role": "system", "content": "У пользователя нет активных задач. Не выдумывай их."})

    result = await groq.chat_completion(context, AGENT_PROMPTS[agent])
    if not result:
        await send_or_edit(bot, message.chat.id, status_msg_id, "❌ Ошибка генерации ответа.")
        return

    db.add_history(tg_id, "assistant", result, agent.value)

    prefix_map = {
        AgentType.CODE: f"💻 <b>Код:</b>\n<pre><code>{result}</code></pre>",
        AgentType.COACH: f"🔥 <b>Вердикт тренера:</b>\n\n{result}",
        AgentType.ASSISTANT: f"🤖 <b>Ответ:</b>\n\n{result}",
        AgentType.WRITER: f"✨ <b>Творческий результат:</b>\n\n{result}",
        AgentType.ANALYST: f"📈 <b>Аналитика:</b>\n\n{result}",
        AgentType.TRANSLATOR: f"🌐 <b>Перевод:</b>\n\n{result}"
    }
    final_text = prefix_map.get(agent, result)

    reply_markup = get_tasks_inline_keyboard() if agent == AgentType.ASSISTANT else None

    if agent == AgentType.ASSISTANT and any(kw in summary.lower() for kw in ["добавить задачу", "новая задача", "запланировать", "напомнить"]):
        success = db.add_task(tg_id, original_text)
        final_text += "\n\n✅ <i>Задача сохранена.</i>" if success else "\n\n⚠️ <i>Не удалось сохранить задачу.</i>"

    await send_or_edit(bot, message.chat.id, status_msg_id, final_text, reply_markup=reply_markup)

async def run_pipeline(text: str, message: types.Message, bot: Bot, status_msg_id: int, force_agent: Optional[AgentType] = None):
    tg_id = message.from_user.id
    db.ensure_user(tg_id, message.from_user.username, message.from_user.full_name)
    db.add_history(tg_id, "user", text, "user")

    if force_agent:
        await handle_agent(force_agent, "forced", text, [], message, bot, status_msg_id, tg_id)
        return

    history = db.get_recent_history(tg_id, limit=4)
    router_input = history + [{"role": "user", "content": text}]
    try:
        router_json = await groq.chat_completion(router_input, ROUTER_PROMPT, response_format="json")
        if not router_json:
            raise Exception("Роутер не ответил")
        decision = extract_json(router_json)
        task_type = decision.get("type", "ASSISTANT").lower()
        summary = decision.get("summary", text[:100])
        if task_type not in [a.value for a in AgentType]:
            task_type = AgentType.ASSISTANT.value
        agent = AgentType(task_type)
        logger.info(f"🔀 Маршрут: {agent.value} | {summary}")
        await handle_agent(agent, summary, text, history, message, bot, status_msg_id, tg_id)
    except Exception as e:
        logger.error(f"Pipeline error: {e}")
        await send_or_edit(bot, message.chat.id, status_msg_id, f"❌ Сбой маршрутизации: {str(e)}")

# ═══ ИНИЦИАЛИЗАЦИЯ BOT И DISPATCHER (ДОЛЖНЫ БЫТЬ ДО ДЕКОРАТОРОВ!) ═══
bot = Bot(token=TELEGRAM_TOKEN)
dp = Dispatcher()

# ═══ ОБРАБОТЧИКИ КОМАНД ═══
@dp.message(Command("start"))
async def cmd_start(message: types.Message):
    await message.answer(
        "🚀 <b>Мультиагентный ИИ v2.0</b>\n\n"
        "Я – твой универсальный помощник с 6 агентами.\n"
        "Используй кнопки внизу или введи /menu.\n\n"
        "Выбери агента прямо сейчас:",
        reply_markup=get_agents_inline_keyboard(),
        parse_mode="HTML"
    )
    await message.answer("Или пользуйся постоянными кнопками 👇", reply_markup=get_main_keyboard())

@dp.message(Command("menu"))
async def cmd_menu(message: types.Message):
    await message.answer("Главное меню:", reply_markup=get_main_keyboard())

@dp.message(Command("tasks"))
async def cmd_tasks(message: types.Message):
    tasks = db.get_tasks(message.from_user.id)
    if not tasks:
        await message.answer("📭 В базе задач нет.")
        return
    text = "📋 <b>Ваши задачи:</b>\n\n" + "\n".join([f"{i}. {t['content']}" for i, t in enumerate(tasks, 1)])
    await message.answer(text, parse_mode="HTML", reply_markup=get_tasks_inline_keyboard())

@dp.message(Command("clear"))
async def cmd_clear(message: types.Message):
    db.clear_tasks(message.from_user.id)
    await message.answer("🗑️ Все задачи архивированы.", reply_markup=get_main_keyboard())

@dp.message(Command("mode"))
async def cmd_mode(message: types.Message):
    try:
        _, agent_str = message.text.split(maxsplit=1)
        agent_str = agent_str.strip().lower()
        if not any(agent_str == a.value for a in AgentType):
            raise ValueError
        agent = AgentType(agent_str)
        user_modes[message.from_user.id] = agent
        await message.answer(f"✅ Агент установлен: <b>{agent.value}</b>. Следующее сообщение будет обработано им.",
                             parse_mode="HTML")
    except ValueError:
        await message.answer("Выбери агента:", reply_markup=get_agents_inline_keyboard())

@dp.message(Command("resetmode"))
async def cmd_resetmode(message: types.Message):
    user_modes.pop(message.from_user.id, None)
    await message.answer("🔁 Режим сброшен. ИИ снова выбирает агента автоматически.", reply_markup=get_main_keyboard())

# ═══ ОБРАБОТЧИКИ КНОПОК REPLY КЛАВИАТУРЫ ═══
@dp.message(F.text.in_(["📋 Мои задачи"]))
async def show_tasks_from_button(message: types.Message):
    await cmd_tasks(message)

@dp.message(F.text.in_(["🗑 Очистить задачи"]))
async def clear_tasks_from_button(message: types.Message):
    await cmd_clear(message)

@dp.message(F.text.in_(["🤖 Сменить агента"]))
async def change_agent_from_button(message: types.Message):
    await message.answer("Выбери агента:", reply_markup=get_agents_inline_keyboard())

@dp.message(F.text.in_(["❓ Помощь"]))
async def help_from_button(message: types.Message):
    await message.answer(
        "🤖 <b>Как пользоваться:</b>\n"
        "• Напиши любой запрос – ИИ сам подберет нужного агента.\n"
        "• Используй кнопки для быстрого доступа.\n"
        "• Команда /mode <i>тип</i> принудительно включает агента.\n\n"
        f"Доступные агенты: {', '.join([a.value for a in AgentType])}",
        parse_mode="HTML"
    )

# ═══ ГОЛОСОВЫЕ СООБЩЕНИЯ ═══
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

# ═══ ТЕКСТОВЫЕ СООБЩЕНИЯ ═══
@dp.message()
async def handle_text(message: types.Message):
    if not message.text:
        return
    status_msg = await message.answer("⏳ 🧠 Думаю...")
    mode = user_modes.get(message.from_user.id)
    await run_pipeline(message.text, message, bot, status_msg.message_id, force_agent=mode)

# ═══ CALLBACK-ОБРАБОТЧИКИ ═══
@dp.callback_query(F.data.startswith("set_agent:"))
async def on_set_agent_callback(callback: types.CallbackQuery):
    agent_value = callback.data.split(":")[1]
    agent = AgentType(agent_value)
    user_modes[callback.from_user.id] = agent
    await callback.answer(f"✅ Агент {agent.value} активирован!", show_alert=False)
    await callback.message.edit_text(
        f"✅ Выбран агент: <b>{agent.value.capitalize()}</b>\n\n"
        "Теперь введи запрос, и бот будет отвечать через этого агента.",
        parse_mode="HTML",
        reply_markup=None
    )

@dp.callback_query(F.data == "show_tasks")
async def on_show_tasks_callback(callback: types.CallbackQuery):
    tasks = db.get_tasks(callback.from_user.id)
    if not tasks:
        await callback.answer("Задач нет", show_alert=False)
        await callback.message.edit_text("📭 В базе задач нет.", reply_markup=None)
        return
    text = "📋 <b>Ваши задачи:</b>\n\n" + "\n".join([f"{i}. {t['content']}" for i, t in enumerate(tasks, 1)])
    await callback.message.edit_text(text, parse_mode="HTML", reply_markup=get_tasks_inline_keyboard())
    await callback.answer()

@dp.callback_query(F.data == "clear_tasks")
async def on_clear_tasks_callback(callback: types.CallbackQuery):
    db.clear_tasks(callback.from_user.id)
    await callback.answer("Задачи очищены ✅", show_alert=False)
    await callback.message.edit_text("🗑️ Все задачи архивированы.", reply_markup=None)

# ═══ ЗАПУСК ═══
async def main():
    await groq.start()
    logger.info("🚀 Бот запущен с клавиатурами и 6 агентами!")
    try:
        await dp.start_polling(bot)
    finally:
        await groq.close()
        await bot.session.close()

if __name__ == "__main__":
    asyncio.run(main())
