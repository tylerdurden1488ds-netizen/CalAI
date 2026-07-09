#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Telegram calorie counter bot (single-file).
Requirements:
- Python 3.12+
- aiogram 3.x, aiosqlite, pillow, google-genai

Environment variables:
- API_TOKEN
- GEMINI_API_KEY

DB:
- calories.db (created automatically next to this file)

Usage:
- /start to register and (if needed) set daily goal
- Send text = food description
- Send photo = food photo
- /stats to view today's totals
"""

import os
import re
import io
import math
import logging
import asyncio
import datetime
import aiosqlite
from typing import Optional

from PIL import Image

# aiogram 3.x imports
from aiogram import Bot, Dispatcher, Router
from aiogram.types import Message, ReplyKeyboardMarkup, KeyboardButton
from aiogram.filters import Command, Text
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import StatesGroup, State
from aiogram.fsm.storage.memory import MemoryStorage

# google-genai library (user requested). We import as google_genai and adapt to common variants.
try:
    import google_genai as genai  # noqa: E402
except Exception:
    genai = None

# Logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

API_TOKEN = os.getenv("8908197730:AAGRp2jRBJCHJM3BPVy6eTTUCCk2j7lIc7g")
GEMINI_API_KEY = os.getenv("AQ.Ab8RN6K6_nsdNprUG8KUGRj8egS9dzEPxmZtT5RjmRCIVAP6tw")
if not API_TOKEN:
    logger.error("API_TOKEN environment variable is not set.")
    raise SystemExit("Set API_TOKEN environment variable.")
if not GEMINI_API_KEY:
    logger.error("GEMINI_API_KEY environment variable is not set.")
    raise SystemExit("Set GEMINI_API_KEY environment variable.")

DB_PATH = os.path.join(os.path.dirname(__file__), "calories.db")

# FSM for setting daily goal
class GoalState(StatesGroup):
    waiting_for_goal = State()

# Create router
router = Router()

# Keyboard
main_kb = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton(text="🍽 Добавить еду"), KeyboardButton(text="📊 Статистика")]
    ],
    resize_keyboard=True,
)

# DB helpers
async def init_db():
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """
            CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY,
                daily_goal INTEGER
            )
            """
        )
        await db.execute(
            """
            CREATE TABLE IF NOT EXISTS daily_stats (
                user_id INTEGER,
                date TEXT,
                calories REAL DEFAULT 0,
                protein REAL DEFAULT 0,
                fat REAL DEFAULT 0,
                carbs REAL DEFAULT 0,
                PRIMARY KEY (user_id, date)
            )
            """
        )
        await db.commit()
    logger.info("Initialized DB at %s", DB_PATH)

async def ensure_user(user_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT OR IGNORE INTO users(user_id, daily_goal) VALUES(?, NULL)",
            (user_id,),
        )
        await db.commit()

async def get_daily_goal(user_id: int) -> Optional[int]:
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("SELECT daily_goal FROM users WHERE user_id = ?", (user_id,))
        row = await cur.fetchone()
        return None if (not row) else row[0]

async def set_daily_goal(user_id: int, goal: int):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("INSERT OR REPLACE INTO users(user_id, daily_goal) VALUES(?, ?)", (user_id, goal))
        await db.commit()

async def add_daily_stats(user_id: int, date: str, calories: float, protein: float, fat: float, carbs: float):
    async with aiosqlite.connect(DB_PATH) as db:
        # Try to insert, if exists update by adding
        await db.execute(
            """
            INSERT INTO daily_stats(user_id, date, calories, protein, fat, carbs)
            VALUES(?, ?, ?, ?, ?, ?)
            ON CONFLICT(user_id, date) DO UPDATE SET
                calories = daily_stats.calories + excluded.calories,
                protein = daily_stats.protein + excluded.protein,
                fat = daily_stats.fat + excluded.fat,
                carbs = daily_stats.carbs + excluded.carbs
            """,
            (user_id, date, calories, protein, fat, carbs),
        )
        await db.commit()

async def get_today_stats(user_id: int, date: str):
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            "SELECT calories, protein, fat, carbs FROM daily_stats WHERE user_id = ? AND date = ?",
            (user_id, date),
        )
        row = await cur.fetchone()
        if not row:
            return {"calories": 0, "protein": 0, "fat": 0, "carbs": 0}
        return {"calories": row[0] or 0, "protein": row[1] or 0, "fat": row[2] or 0, "carbs": row[3] or 0}

# Regex for parsing final line
RESULT_RE = re.compile(
    r"\[CALORIES\s*:\s*(?P<cal>[\d.]+)\s*,\s*PROTEIN\s*:\s*(?P<protein>[\d.]+)\s*,\s*FAT\s*:\s*(?P<fat>[\d.]+)\s*,\s*CARBS\s*:\s*(?P<carbs>[\d.]+)\s*\]\s*$",
    re.IGNORECASE,
)

# System prompt in Russian per specification
SYSTEM_PROMPT = (
    "Ты — эксперт-нутрициолог.\n"
    "Проанализируй описание или фотографию еды.\n"
    "Определи блюдо.\n"
    "Оцени примерный вес.\n"
    "Рассчитай:\n\n"
    "- калории\n"
    "- белки\n"
    "- жиры\n"
    "- углеводы\n\n"
    "Ответ оформи красиво с эмодзи.\n\n"
    "В самом конце ОБЯЗАТЕЛЬНО добавь строку:\n"
    "[CALORIES:123, PROTEIN:10, FAT:5, CARBS:15]\n"
    "где числа реальные (целые или дробные)."
)

# Helper: call google-genai (tries to adapt to common variants of library API)
async def call_gemini(prompt_text: str, image_bytes: Optional[bytes] = None, timeout: int = 30) -> str:
    """
    Attempts to call google-genai library in a flexible way.
    Returns the text output. Raises informative exceptions on failure.
    """
    if genai is None:
        raise RuntimeError("google-genai library is not available (import failed).")

    user_prompt = f"{prompt_text}"
    full_prompt = SYSTEM_PROMPT + "\n\n" + user_prompt

    # Try several common patterns for python google-genai libs
    last_err = None

    # Pattern A: genai.Client with async generate(...)
    try:
        Client = getattr(genai, "Client", None)
        if Client:
            client = Client(api_key=GEMINI_API_KEY)
            gen_method = getattr(client, "generate", None) or getattr(client, "generate_text", None) or getattr(client, "generate_content", None)
            if gen_method:
                # prepare inputs for image if supported
                if image_bytes:
                    # try to pass base64 image if accepted
                    import base64
                    b64 = base64.b64encode(image_bytes).decode()
                    inputs = {"text": full_prompt, "image_base64": b64}
                    # try async call
                    try:
                        result = gen_method(model="gemini", inputs=inputs)
                        # if coroutine, await
                        if asyncio.iscoroutine(result):
                            result = await asyncio.wait_for(result, timeout=timeout)
                    except TypeError:
                        # maybe method signature is (input=..., model=...)
                        result = gen_method(input=full_prompt, image=b64, model="gemini")
                        if asyncio.iscoroutine(result):
                            result = await asyncio.wait_for(result, timeout=timeout)
                else:
                    result = gen_method(input=full_prompt, model="gemini")
                    if asyncio.iscoroutine(result):
                        result = await asyncio.wait_for(result, timeout=timeout)
                # Try to extract text
                if result is None:
                    raise RuntimeError("Нет ответа от модели (Client.generate вернул None)")
                text = None
                # common accessors
                for key in ("text", "output_text", "content", "response", "output"):
                    if hasattr(result, key):
                        text_val = getattr(result, key)
                        if isinstance(text_val, str):
                            text = text_val
                            break
                        # if nested
                        try:
                            text = str(text_val)
                            break
                        except Exception:
                            pass
                if text is None:
                    # dict-like
                    try:
                        if isinstance(result, dict):
                            # try common fields
                            for k in ("output", "text", "content"):
                                if k in result:
                                    text = result[k]
                                    break
                    except Exception:
                        pass
                if text is None:
                    text = str(result)
                return text
    except Exception as e:
        logger.exception("Pattern A (Client.generate) failed.")
        last_err = e

    # Pattern B: genai.configure + GenerativeModel / generate_content (older examples)
    try:
        configure = getattr(genai, "configure", None)
        if configure:
            try:
                configure(api_key=GEMINI_API_KEY)
            except Exception:
                # some variants use configure({'api_key': ...})
                try:
                    configure({"api_key": GEMINI_API_KEY})
                except Exception:
                    pass
        GenModel = getattr(genai, "GenerativeModel", None)
        if GenModel:
            model_name = "gemini-pro-vision" if image_bytes else "gemini-pro"
            model = GenModel(model_name)
            # The generate_content examples accept a list of parts (text + PIL.Image)
            parts = [full_prompt]
            if image_bytes:
                try:
                    img = Image.open(io.BytesIO(image_bytes))
                    parts.append(img)
                except Exception:
                    # send raw bytes if PIL open fails
                    parts.append(image_bytes)
            gen = getattr(model, "generate_content", None) or getattr(model, "generate", None)
            if gen:
                result = gen(parts)
                # await if coroutine
                if asyncio.iscoroutine(result):
                    result = await asyncio.wait_for(result, timeout=timeout)
                # Try extract text
                text = None
                for attr in ("text", "output_text", "content"):
                    if hasattr(result, attr):
                        text = getattr(result, attr)
                        break
                if text is None:
                    # dict-like
                    if isinstance(result, dict):
                        text = result.get("output") or result.get("text") or result.get("content") or str(result)
                if text is None:
                    text = str(result)
                return text
    except Exception as e:
        logger.exception("Pattern B (GenerativeModel) failed.")
        last_err = e

    # Pattern C: genai.generate top-level function
    try:
        gen_func = getattr(genai, "generate", None) or getattr(genai, "generate_text", None)
        if gen_func:
            if image_bytes:
                # try base64
                import base64
                b64 = base64.b64encode(image_bytes).decode()
                result = gen_func(prompt=full_prompt, image=b64, model="gemini")
            else:
                result = gen_func(prompt=full_prompt, model="gemini")
            if asyncio.iscoroutine(result):
                result = await asyncio.wait_for(result, timeout=timeout)
            # extract
            text = None
            if isinstance(result, dict):
                text = result.get("text") or result.get("output_text") or result.get("content")
            if text is None and hasattr(result, "text"):
                text = getattr(result, "text")
            if text is None:
                text = str(result)
            return text
    except Exception as e:
        logger.exception("Pattern C (top-level generate) failed.")
        last_err = e

    # If we get here, we failed to call successfully
    raise RuntimeError(f"Не удалось вызвать google-genai библиотеку корректно. Последняя ошибка: {last_err}")

# Helper: parse model's response for the required bracketed line
def parse_result_text(response_text: str):
    """
    Возвращает кортеж (cal, protein, fat, carbs) как float если строка найдена,
    иначе None.
    """
    if not response_text:
        return None
    match = RESULT_RE.search(response_text.strip())
    if not match:
        return None
    try:
        cal = float(match.group("cal"))
        protein = float(match.group("protein"))
        fat = float(match.group("fat"))
        carbs = float(match.group("carbs"))
        return (cal, protein, fat, carbs)
    except Exception:
        return None

# Utils: pretty formatting of the response (we will show the whole model text)
def make_stats_text(today_stats: dict, daily_goal: Optional[int]):
    cal = today_stats["calories"]
    protein = today_stats["protein"]
    fat = today_stats["fat"]
    carbs = today_stats["carbs"]

    goal = daily_goal or 0
    percent = 0
    if goal and goal > 0:
        percent = int(min(100, math.floor((cal / goal) * 100)))
    else:
        percent = 0

    filled = int(percent / 10)
    bar = "█" * filled + "░" * (10 - filled)

    text = (
        f"🥗 <b>Статистика за сегодня</b>\n\n"
        f"🔥 <b>Калории:</b>\n"
        f"{int(cal)} / {int(goal) if goal else '—'}\n\n"
        f"📈 <b>Прогресс:</b>\n"
        f"{bar} {percent}%\n\n"
        f"🍗 <b>Белки:</b>\n"
        f"{int(protein)} г\n\n"
        f"🥑 <b>Жиры:</b>\n"
        f"{int(fat)} г\n\n"
        f"🍞 <b>Углеводы:</b>\n"
        f"{int(carbs)} г\n"
    )
    return text

# Handlers

@router.message(Command(commands=["start"]))
async def cmd_start(message: Message, state: FSMContext):
    user_id = message.from_user.id
    await ensure_user(user_id)
    goal = await get_daily_goal(user_id)
    if not goal:
        await message.answer(
            "👋 Привет! Я помогу считать ваши калории.\n\n"
            "Пожалуйста, введите вашу дневную норму калорий (целое число, например 2200):",
            reply_markup=types.ReplyKeyboardRemove(),
        )
        await state.set_state(GoalState.waiting_for_goal)
        return
    # else send welcome and keyboard
    await message.answer(
        f"👋 Привет! Я сохранил вашу дневную цель: <b>{goal} ккал</b>.\n\n"
        "Отправьте описание блюда или фото — я проанализирую и добавлю в статистику.",
        reply_markup=main_kb,
    )

@router.message(GoalState.waiting_for_goal)
async def process_goal(message: Message, state: FSMContext):
    user_id = message.from_user.id
    text = (message.text or "").strip()
    if not text:
        await message.answer("Пожалуйста, пришлите число (например: 2200).")
        return
    try:
        goal = int(float(text))
        if goal <= 0:
            raise ValueError()
    except Exception:
        await message.answer("Неправильный формат. Введите положительное целое число, например: 2000")
        return
    await set_daily_goal(user_id, goal)
    await state.clear()
    await message.answer(
        f"✅ Сохранено! Ваша дневная норма: <b>{goal} ккал</b>.",
        reply_markup=main_kb,
    )

@router.message(Command(commands=["stats"]))
async def cmd_stats(message: Message):
    user_id = message.from_user.id
    today = datetime.date.today().isoformat()
    stats = await get_today_stats(user_id, today)
    goal = await get_daily_goal(user_id)
    text = make_stats_text(stats, goal)
    await message.answer(text, parse_mode="HTML", reply_markup=main_kb)

@router.message(Text(text="📊 Статистика"))
async def kb_stats(message: Message):
    await cmd_stats(message)

@router.message(Text(text="🍽 Добавить еду"))
async def kb_add_food(message: Message):
    await message.answer("📩 Пришлите описание блюда или фотографию.", reply_markup=types.ReplyKeyboardRemove())

# Photo handler
@router.message()
async def catch_all(message: Message, state: FSMContext):
    """
    This handler processes:
    - Photos (message.photo is not empty) -> treat as photo of food
    - Text messages (when not in FSM) -> treat as text description of food or commands handled earlier
    """
    user_id = message.from_user.id
    # If in FSM waiting for goal, that is handled above
    # If there is a photo, treat as photo-food
    if message.photo:
        # download largest photo into bytes
        try:
            bio = io.BytesIO()
            await message.photo[-1].download(destination=bio)
            bio.seek(0)
            image_bytes = bio.read()
        except Exception as e:
            logger.exception("Failed to download photo")
            await message.answer("Не удалось скачать фото. Попробуйте ещё раз.")
            return

        await message.answer("🔎 Анализирую фотографию... Пожалуйста, подождите.")
        try:
            response_text = await call_gemini(prompt_text="(фото) " + (message.caption or ""), image_bytes=image_bytes)
        except Exception as e:
            logger.exception("Gemini error for image")
            await message.answer("⚠️ Произошла ошибка при обращении к модели. Попробуйте позже.")
            return

        parsed = parse_result_text(response_text)
        if not parsed:
            # send friendly message from specification
            await message.answer(
                "😕 Модель не смогла определить КБЖУ из ответа.\n"
                "Попробуйте прислать более подробное описание или другое фото.",
                reply_markup=main_kb,
            )
            # also send model's raw text for debugging
            await message.answer(f"<b>Ответ модели:</b>\n{response_text}", parse_mode="HTML")
            return

        cal, prot, fat, carbs = parsed
        today = datetime.date.today().isoformat()
        await add_daily_stats(user_id, today, cal, prot, fat, carbs)

        # reply with model's formatted answer and summary
        await message.answer(
            f"🍽 <b>Добавлено в статистику</b>\n\n"
            f"{response_text}\n\n"
            f"✅ <b>Сумма за сегодня обновлена.</b>",
            parse_mode="HTML",
            reply_markup=main_kb,
        )
        return

    # Otherwise, if it's text (not a command), treat as food description unless FSM handled
    if message.text and not message.text.startswith("/"):
        # skip if we are setting a goal (handled earlier)
        if (await state.get_state()) == GoalState.waiting_for_goal.state:
            # Should be handled by FSM handler; just return to let that handler process
            return

        await message.answer("🔎 Анализирую описание... Пожалуйста, подождите.")
        try:
            response_text = await call_gemini(prompt_text=message.text)
        except Exception as e:
            logger.exception("Gemini error for text")
            await message.answer("⚠️ Произошла ошибка при обращении к модели. Попробуйте позже.", reply_markup=main_kb)
            return

        parsed = parse_result_text(response_text)
        if not parsed:
            await message.answer(
                "😕 Модель вернула ответ, но я не нашёл строку с КБЖУ в нужном формате.\n"
                "Пожалуйста, попросите модель уточнить или опишите блюдо подробнее.",
                reply_markup=main_kb,
            )
            await message.answer(f"<b>Ответ модели:</b>\n{response_text}", parse_mode="HTML")
            return

        cal, prot, fat, carbs = parsed
        today = datetime.date.today().isoformat()
        await add_daily_stats(user_id, today, cal, prot, fat, carbs)

        await message.answer(
            f"🍽 <b>Добавлено в статистику</b>\n\n"
            f"{response_text}\n\n"
            f"✅ <b>Сумма за сегодня обновлена.</b>",
            parse_mode="HTML",
            reply_markup=main_kb,
        )
        return

    # No content recognized
    await message.answer("Я ожидаю описание блюда или фотографию. Используйте клавиатуру.", reply_markup=main_kb)

# Main entrypoint
async def main():
    await init_db()
    bot = Bot(token=API_TOKEN, parse_mode="HTML")
    storage = MemoryStorage()
    dp = Dispatcher(storage=storage)
    dp.include_router(router)
    logger.info("Bot started. Polling...")
    try:
        await dp.start_polling(bot)
    finally:
        await bot.session.close()

if __name__ == "__main__":
    asyncio.run(main())
