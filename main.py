import logging
import os
from collections import defaultdict
from dotenv import load_dotenv

import gspread
from oauth2client.service_account import ServiceAccountCredentials
import datetime

from aiogram import Bot, Dispatcher, Router, F
from aiogram.types import Message
from aiogram.types import ReplyKeyboardMarkup, KeyboardButton
from aiogram.enums import ContentType
from aiogram.client.session.aiohttp import AiohttpSession
from openai import OpenAI
from aiogram.filters import Command

# === CONFIGURATION ===
load_dotenv()
AUTHORIZED_USER_IDS = [int(id.strip()) for id in os.getenv("AUTHORIZED_USER_IDS", "").split(",") if id.strip().isdigit()]
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
ASSISTANT_ID = os.getenv("ASSISTANT_ID")

# === LOGGING ===
logger = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO)

# === INITIALIZATION ===
session = AiohttpSession()
bot = Bot(token=TELEGRAM_TOKEN, session=session)
dp = Dispatcher()
router = Router()

openai_client = OpenAI(api_key=OPENAI_API_KEY)

# === MAIN MENU KEYBOARD ===
main_keyboard = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton(text="🍽 Блюдо"), KeyboardButton(text="🩺 Симптом")]
    ],
    resize_keyboard=True,
    one_time_keyboard=False
)

# === STATE MANAGEMENT ===
user_state = defaultdict(bool)

# === GOOGLE SHEETS CLIENT ===
def get_google_sheet_client():
    scope = ["https://spreadsheets.google.com/feeds", "https://www.googleapis.com/auth/drive"]
    creds = ServiceAccountCredentials.from_json_keyfile_name("google_credentials.json", scope)
    client = gspread.authorize(creds)
    return client

def write_to_sheet(data_row: list):
    try:
        client = get_google_sheet_client()
        sheet = client.open("Nutribot").worksheet("Nutrition")
        sheet.append_row(data_row)
    except Exception as e:
        logger.error(f"Failed to write to Nutrition sheet: {e}")

def write_symptom_to_sheet(data_row: list):
    try:
        client = get_google_sheet_client()
        sheet = client.open("Nutribot").worksheet("Symptoms")
        sheet.append_row(data_row)
    except Exception as e:
        logger.error(f"Failed to write to Symptoms sheet: {e}")

def is_authorized(message: Message) -> bool:
    return message.from_user.id in AUTHORIZED_USER_IDS

# === COMMAND /start ===
@router.message(Command("start"))
async def cmd_start(message: Message):
    if not is_authorized(message):
        await message.answer("⛔️ У тебя нет доступа к этому боту.")
        return
    await message.answer("Привет! Выбери действие:", reply_markup=main_keyboard)

# === COMMAND /dish ===
@router.message(Command("dish"))
async def cmd_dish(message: Message):
    if not is_authorized(message):
        await message.answer("⛔️ У тебя нет доступа к этому боту.")
        return
    user_state[message.from_user.id] = True
    await message.answer("Отправь мне блюдо.")

# === COMMAND /symptom ===
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton, CallbackQuery

@router.message(Command("symptom"))
async def ask_symptom(message: Message):
    if not is_authorized(message):
        await message.answer("⛔️ У тебя нет доступа к этому боту.")
        return
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="💩 Стул", callback_data="symptom:Стул")],
        [InlineKeyboardButton(text="🌬️ Метеоризм", callback_data="symptom:Метеоризм")],
        [InlineKeyboardButton(text="⚡ Боль", callback_data="symptom:Боль")],
        [InlineKeyboardButton(text="🌫️ Мозговой туман", callback_data="symptom:Мозговой туман")]
    ])
    await message.answer("Выбери симптом:", reply_markup=keyboard)

# === UNIVERSAL MESSAGE HANDLER ===
@router.message()
async def universal_handler(message: Message, **kwargs):
    if not is_authorized(message):
        await message.answer("⛔️ У тебя нет доступа к этому боту.")
        return
    if not user_state.get(message.from_user.id):
        return  # Ignore messages if not awaiting dish input

    user_state[message.from_user.id] = False

    if message.content_type == ContentType.TEXT:
        await message.answer("⏳ Анализирую текст...")
        thread = openai_client.beta.threads.create()
        openai_client.beta.threads.messages.create(
            thread_id=thread.id,
            role="user",
            content=f"Описание блюда: {message.text.strip()}. Определи ингредиенты, уровень FODMAP и калорийность."
        )
        openai_client.beta.threads.runs.create_and_poll(
            thread_id=thread.id,
            assistant_id=ASSISTANT_ID
        )
        messages = openai_client.beta.threads.messages.list(thread_id=thread.id)
        if not messages.data or not messages.data[0].content or not messages.data[0].content[0].text:
            await message.answer("Не удалось получить ответ от ассистента.")
            return
        reply = messages.data[0].content[0].text.value

        lines = reply.split("\n")
        fields = {"dish": "", "ingredients": "", "fodmap": "", "histamine": "", "calories": "", "carbs": "", "proteins": "", "fats": ""}
        for line in lines:
            line_strip = line.strip()
            if not line_strip:
                continue
            for key in fields:
                if line_strip.lower().startswith(f"{key}:"):
                    parts = line_strip.split(":", 1)
                    if len(parts) > 1:
                        fields[key] = parts[1].strip()

        timestamp = datetime.datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")
        user_info = message.from_user.username or message.from_user.full_name or str(message.from_user.id)
        try:
            write_to_sheet([
                timestamp,
                user_info,
                fields["dish"],
                fields["ingredients"],
                fields["fodmap"],
                fields["histamine"],
                fields["calories"],
                fields["carbs"],
                fields["proteins"],
                fields["fats"]
            ])
        except Exception as e:
            logger.error(f"Error writing dish data to sheet: {e}")

        await message.answer(reply)

    elif message.content_type == ContentType.PHOTO:
        import io

        await message.answer("⏳ Загружаю фото...")

        photo = message.photo[-1]
        tg_file = await bot.get_file(photo.file_id)
        file_path = tg_file.file_path
        file_content = await bot.download_file(file_path)
        file_bytes = io.BytesIO(file_content.read())

        openai_file = openai_client.files.create(
            file=("image.jpg", file_bytes),
            purpose="vision"
        )

        thread = openai_client.beta.threads.create()
        openai_client.beta.threads.messages.create(
            thread_id=thread.id,
            role="user",
            content=[
                {"type": "text", "text": "На этом изображении еда. Назови ингредиенты, уровень FODMAP и калорийность."},
                {"type": "image_file", "image_file": {"file_id": openai_file.id}}
            ]
        )

        openai_client.beta.threads.runs.create_and_poll(
            thread_id=thread.id,
            assistant_id=ASSISTANT_ID
        )

        messages = openai_client.beta.threads.messages.list(thread_id=thread.id)
        if not messages.data or not messages.data[0].content or not messages.data[0].content[0].text:
            await message.answer("Не удалось получить ответ от ассистента.")
            return
        reply = messages.data[0].content[0].text.value

        lines = reply.split("\n")
        fields = {"dish": "", "ingredients": "", "fodmap": "", "histamine": "", "calories": "", "carbs": "", "proteins": "", "fats": ""}
        for line in lines:
            line_strip = line.strip()
            if not line_strip:
                continue
            for key in fields:
                if line_strip.lower().startswith(f"{key}:"):
                    parts = line_strip.split(":", 1)
                    if len(parts) > 1:
                        fields[key] = parts[1].strip()

        timestamp = datetime.datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")
        user_info = message.from_user.username or message.from_user.full_name or str(message.from_user.id)
        try:
            write_to_sheet([
                timestamp,
                user_info,
                fields["dish"],
                fields["ingredients"],
                fields["fodmap"],
                fields["histamine"],
                fields["calories"],
                fields["carbs"],
                fields["proteins"],
                fields["fats"]
            ])
        except Exception as e:
            logger.error(f"Error writing dish data to sheet: {e}")

        await message.answer(reply)

    else:
        await message.answer("Отправь мне блюдо.")

# === SYMPTOM REGISTRATION HANDLERS ===
@router.callback_query(F.data.startswith("symptom:"))
async def ask_severity(callback: CallbackQuery):
    if callback.from_user.id not in (AUTHORIZED_USER_IDS if isinstance(AUTHORIZED_USER_IDS, list) else [AUTHORIZED_USER_IDS]):
        await callback.answer("⛔️ Нет доступа", show_alert=True)
        return
    symptom = callback.data.split(":")[1]
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🟢 Хорошо", callback_data=f"severity:{symptom}:Хорошо")],
        [InlineKeyboardButton(text="🟡 Нормально", callback_data=f"severity:{symptom}:Нормально")],
        [InlineKeyboardButton(text="🔴 Плохо", callback_data=f"severity:{symptom}:Плохо")]
    ])
    await callback.message.edit_text(f"Оцени уровень выраженности симптома: {symptom}", reply_markup=keyboard)

@router.callback_query(F.data.startswith("severity:"))
async def save_symptom(callback: CallbackQuery):
    if callback.from_user.id not in (AUTHORIZED_USER_IDS if isinstance(AUTHORIZED_USER_IDS, list) else [AUTHORIZED_USER_IDS]):
        await callback.answer("⛔️ Нет доступа", show_alert=True)
        return
    _, symptom, severity = callback.data.split(":")
    timestamp = datetime.datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")
    user_info = callback.from_user.username or callback.from_user.full_name or str(callback.from_user.id)
    try:
        write_symptom_to_sheet([timestamp, user_info, symptom, severity])
    except Exception as e:
        logger.error(f"Error writing symptom data to sheet: {e}")
    await callback.message.edit_text(f"✅ Симптом '{symptom}' ({severity}) записан.")

# === MAIN ===
async def main():
    dp.include_router(router)
    await dp.start_polling(bot)

if __name__ == "__main__":
    import asyncio
    asyncio.run(main())