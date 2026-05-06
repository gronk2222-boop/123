import os
import asyncio
import aiohttp
import json
import logging
from datetime import datetime
from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import Command
from dotenv import load_dotenv

# ═══ НАСТРОЙКА ЛОГИРОВАНИЯ ═══
# Уровень INFO покажет основные этапы работы, ERROR — проблемы
logging.basicConfig(
    level=logging.INFO, 
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

load_dotenv()

# ═══ КОНФИГУРАЦИЯ И КЛЮЧИ ═══
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
GROQ_API_KEY = os.getenv("GROQ_API_KEY")

# Автоматическая очистка ключей от пробелов и переносов строк
if TELEGRAM_TOKEN:
    TELEGRAM_TOKEN = TELEGRAM_TOKEN.strip()
if GROQ_API_KEY:
    GROQ_API_KEY = GROQ_API_KEY.strip()

if not TELEGRAM_TOKEN or not GROQ_API_KEY:
    logger.error("❌ КРИТИЧЕСКАЯ ОШИБКА: Отсутствуют TELEGRAM_TOKEN или GROQ_API_KEY")
    raise ValueError("Не найдены переменные окружения TELEGRAM_TOKEN или GROQ_API_KEY")

# Настройки API Groq
GROQ_URL = "https://api.groq.com/openai/v1/chat/completions"
WHISPER_URL = "https://api.groq.com/openai/v1/audio/transcriptions"
TEXT_MODEL = "llama-3.1-8b-instant"
WHISPER_MODEL = "whisper-large-v3-turbo"

# Файл для хранения задач (База данных)
TASKS_FILE = "tasks.txt"

# ═══ ПРОМПТЫ АГЕНТОВ ═══

ROUTER_PROMPT = """
Ты — Диспетчер задач. Проанализируй запрос пользователя.
1. Если запрос про код, программирование, баги, скрипты → тип "CODE".
2. Если запрос про текст, планы, идеи, перевод, общение → тип "ASSISTANT".
3. Если пользователь просит запомнить, добавить в список, напомнить → is_task: true.

Ответь ТОЛЬКО в формате JSON (без markdown):
{"type": "CODE" или "ASSISTANT", "summary": "суть в 5 словах", "is_task": true/false}
"""

CODER_PROMPT = """
Ты Senior Python Developer. Твоя задача — писать идеальный код.
- Пиши только код и краткие технические комментарии.
- Не пиши вступлений типа "Конечно, вот ваш код".
- Код должен быть готов к запуску.
"""

ASSISTANT_PROMPT = """
Ты Личный Ассистент. Твоя задача — помогать пользователю эффективно.
- Отвечай структурно (списки, жирный шрифт для главного).
- Будь краток, но давай полные ответы.
- Стиль: профессиональный, дружелюбный.
"""

# ═══ УТИЛИТЫ (БАЗА ДАННЫХ И API) ═══

def init_tasks_file():
    """Создает файл задач, если он не существует"""
    if not os.path.exists(TASKS_FILE):
        with open(TASKS_FILE, "w", encoding="utf-8") as f:
            pass
        logger.info(f"Файл {TASKS_FILE} создан.")

def save_task_to_db(user_id, task_text):
    """Сохраняет задачу в локальный файл"""
    try:
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M")
        entry = f"[{timestamp}] User {user_id}: {task_text}\n"
        with open(TASKS_FILE, "a", encoding="utf-8") as f:
            f.write(entry)
        logger.info(f"✅ Задача сохранена: {task_text[:30]}...")
        return True
    except Exception as e:
        logger.error(f"Ошибка записи в БД: {e}")
        return False

def get_tasks_from_db():
    """Читает задачи из файла"""
    if not os.path.exists(TASKS_FILE):
        return "📭 Список задач пуст."
    with open(TASKS_FILE, "r", encoding="utf-8") as f:
        content = f.read()
    return content if content.strip() else "📭 Список задач пуст."

def clear_tasks_db():
    """Очищает файл задач"""
    try:
        with open(TASKS_FILE, "w", encoding="utf-8") as f:
            pass
        logger.info("🗑️ База задач очищена.")
        return "🗑️ Все задачи удалены."
    except Exception as e:
        logger.error(f"Ошибка очистки БД: {e}")
        return "❌ Ошибка при очистке."

async def call_groq_text(system_prompt, user_content, response_format=None):
    """Вызов LLM для текста (Llama 3)"""
    headers = {
        "Authorization": f"Bearer {GROQ_API_KEY}",
        "Content-Type": "application/json"
    }
    payload = {
        "model": TEXT_MODEL,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_content}
        ],
        "temperature": 0.2,
        "max_tokens": 1500
    }
    if response_format == "json":
        payload["response_format"] = {"type": "json_object"}

    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(GROQ_URL, json=payload, headers=headers, timeout=aiohttp.ClientTimeout(total=30)) as resp:
                if resp.status != 200:
                    error_text = await resp.text()
                    logger.error(f"Groq Text Error {resp.status}: {error_text}")
                    return None
                data = await resp.json()
                return data['choices'][0]['message']['content'].strip()
    except Exception as e:
        logger.error(f"Network error (Text): {e}")
        return None

async def call_groq_whisper(audio_path):
    """Вызов Whisper для транскрибации голоса"""
    headers = {
        "Authorization": f"Bearer {GROQ_API_KEY}"
    }
    
    # Подготовка multipart/form-data вручную для надежности
    form_data = aiohttp.FormData()
    try:
        with open(audio_path, 'rb') as f:
            form_data.add_field('file', f, filename='audio.ogg', content_type='audio/ogg')
        form_data.add_field('model', WHISPER_MODEL)

        async with aiohttp.ClientSession() as session:
            async with session.post(WHISPER_URL, data=form_data, headers=headers, timeout=aiohttp.ClientTimeout(total=60)) as resp:
                if resp.status != 200:
                    error_text = await resp.text()
                    logger.error(f"Groq Whisper Error {resp.status}: {error_text}")
                    return None
                data = await resp.json()
                text = data.get('text', '').strip()
                logger.info(f"🎤 Расшифровка: {text}")
                return text
    except Exception as e:
        logger.error(f"Network error (Whisper): {e}")
        return None

# ═══ ЛОГИКА ПАЙПЛАЙНА ═══

async def process_code_task(summary, original_text, message, bot, status_msg_id):
    """Обработка задач кодером"""
    await bot.edit_message_text(
        chat_id=message.chat.id, 
        message_id=status_msg_id, 
        text="⏳ 👨‍💻 Кодер пишет решение..."
    )
    
    context = f"Задача пользователя: {original_text}\nКраткая суть: {summary}"
    result = await call_groq_text(CODER_PROMPT, context)
    
    if result:
        final_text = f"💻 <b>Решение от Кодера:</b>\n\n<code>{result}</code>"
        await bot.edit_message_text(
            chat_id=message.chat.id, 
            message_id=status_msg_id, 
            text=final_text, 
            parse_mode="HTML"
        )
    else:
        await bot.edit_message_text(
            chat_id=message.chat.id, 
            message_id=status_msg_id, 
            text="❌ Ошибка: Кодер не смог сгенерировать ответ."
        )

async def process_assistant_task(summary, original_text, message, bot, status_msg_id, is_task_request=False):
    """Обработка задач ассистентом"""
    await bot.edit_message_text(
        chat_id=message.chat.id, 
        message_id=status_msg_id, 
        text="⏳ 🤖 Ассистент готовит ответ..."
    )
    
    context = f"Запрос пользователя: {original_text}\nКраткая суть: {summary}"
    result = await call_groq_text(ASSISTANT_PROMPT, context)
    
    if result:
        extra_msg = ""
        # Если нужно сохранить задачу
        if is_task_request:
            if save_task_to_db(message.from_user.id, original_text):
                extra_msg = "\n\n<i>✅ Задача добавлена в ваш список (/tasks).</i>"
            else:
                extra_msg = "\n\n<i>⚠️ Не удалось сохранить задачу.</i>"
        
        final_text = f"🤖 <b>Ответ Ассистента:</b>\n\n{result}{extra_msg}"
        await bot.edit_message_text(
            chat_id=message.chat.id, 
            message_id=status_msg_id, 
            text=final_text, 
            parse_mode="HTML"
        )
    else:
        await bot.edit_message_text(
            chat_id=message.chat.id, 
            message_id=status_msg_id, 
            text="❌ Ошибка: Ассистент не смог сгенерировать ответ."
        )

async def run_pipeline(text_content, message, bot, status_msg_id):
    """Главный диспетчер: Анализ -> Маршрутизация -> Выполнение"""
    try:
        # 1. Роутер определяет тип задачи
        router_response = await call_groq_text(ROUTER_PROMPT, text_content, response_format="json")
        
        if not router_response:
            raise Exception("Роутер не ответил (API ошибка)")
        
        # Очистка ответа от возможных артефактов markdown
        clean_json = router_response.replace("```json", "").replace("```", "").strip()
        
        try:
            decision = json.loads(clean_json)
        except json.JSONDecodeError:
            logger.warning(f"Неверный JSON от роутера: {clean_json}. Используем fallback.")
            decision = {"type": "ASSISTANT", "summary": "", "is_task": False}
        
        task_type = decision.get("type", "ASSISTANT")
        summary = decision.get("summary", text_content[:50])
        is_task = decision.get("is_task", False)
        
        logger.info(f"🔀 Роутинг: Тип={task_type}, Задача={is_task}, Суть={summary}")

        # 2. Делегирование
        if task_type == "CODE":
            await process_code_task(summary, text_content, message, bot, status_msg_id)
        else:
            await process_assistant_task(summary, text_content, message, bot, status_msg_id, is_task_request=is_task)
            
    except Exception as e:
        logger.error(f"❌ Критический сбой пайплайна: {e}")
        await bot.edit_message_text(
            chat_id=message.chat.id, 
            message_id=status_msg_id, 
            text=f"❌ Внутренняя ошибка системы: {str(e)}"
        )

# ═══ TELEGRAM HANDLERS ═══
bot = Bot(token=TELEGRAM_TOKEN)
dp = Dispatcher()

@dp.message(Command("start"))
async def cmd_start(message: types.Message):
    await message.answer(
        "👋 <b>Мультиагентный Ассистент активирован!</b>\n\n"
        "Я умею:\n"
        "🔹 <b>Писать код</b> (Python, JS, SQL...)\n"
        "🔹 <b>Планировать</b> (тексты, стратегии, идеи)\n"
        "🔹 <b>Слушать голос</b> (отправь голосовое сообщение)\n"
        "🔹 <b>Запоминать задачи</b> (скажи: 'добавь в план...')\n\n"
        "Команды:\n"
        "/tasks — показать список задач\n"
        "/clear — очистить список"
    )

@dp.message(Command("tasks"))
async def cmd_tasks(message: types.Message):
    tasks = get_tasks_from_db()
    await message.answer(f"📋 <b>Ваши задачи:</b>\n\n<code>{tasks}</code>", parse_mode="HTML")

@dp.message(Command("clear"))
async def cmd_clear(message: types.Message):
    msg = clear_tasks_db()
    await message.answer(msg)

@dp.message(F.voice)
async def handle_voice(message: types.Message):
    status_msg = await message.answer("⏳ 🎧 Слушаю и расшифровываю...")
    
    file_id = message.voice.file_id
    file = await bot.get_file(file_id)
    
    # Уникальное имя файла, чтобы не было конфликтов
    file_path = f"voice_{message.message_id}_{message.from_user.id}.ogg"
    
    try:
        # Скачивание файла
        await bot.download_file(file.file_path, file_path)
        logger.info(f"📥 Голосовое сообщение загружено: {file_path}")
        
        # Транскрибация
        text = await call_groq_whisper(file_path)
        
        if text:
            await bot.edit_message_text(
                chat_id=message.chat.id, 
                message_id=status_msg.message_id,
                text=f"🎤 <b>Вы сказали:</b>\n<i>{text}</i>\n\n⏳ Обрабатываю...", 
                parse_mode="HTML"
            )
            # Запуск основного пайплайна с текстом
            await run_pipeline(text, message, bot, status_msg.message_id)
        else:
            await bot.edit_message_text(
                chat_id=message.chat.id, 
                message_id=status_msg.message_id, 
                text="❌ Не удалось распознать голос. Попробуйте еще раз."
            )
    except Exception as e:
        logger.error(f"Ошибка обработки голоса: {e}")
        await bot.edit_message_text(
            chat_id=message.chat.id, 
            message_id=status_msg.message_id, 
            text=f"❌ Ошибка обработки аудио: {str(e)}"
        )
    finally:
        # Гарантированное удаление временного файла
        if os.path.exists(file_path):
            try:
                os.remove(file_path)
                logger.debug(f"🗑️ Временный файл удален: {file_path}")
            except Exception as e:
                logger.warning(f"Не удалось удалить файл {file_path}: {e}")

@dp.message()
async def handle_text(message: types.Message):
    if not message.text:
        return
    
    # Игнорируем команды, обработанные другими хендлерами (хотя фильтр Command уже отработал)
    if message.text.startswith('/'):
        return

    status_msg = await message.answer("⏳ 🧠 Диспетчер анализирует...")
    await run_pipeline(message.text, message, bot, status_msg.message_id)

# ═══ ЗАПУСК ═══
async def main():
    # Инициализация БД
    init_tasks_file()
    
    # Принудительный сброс сессии для избежания конфликтов
    await bot.session.close()
    
    logger.info("🚀 ЗАПУСК БОТА (v2.0 Stable)")
    logger.info(f"Модель текста: {TEXT_MODEL}")
    logger.info(f"Модель голоса: {WHISPER_MODEL}")
    
    try:
        await dp.start_polling(bot)
    except KeyboardInterrupt:
        logger.info("Бот остановлен пользователем")
    finally:
        await bot.session.close()
        logger.info("Сессия бота закрыта")

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except Exception as e:
        logger.critical(f"Фатальная ошибка при запуске: {e}")
