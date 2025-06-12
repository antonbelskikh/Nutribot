import logging
import os
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

# === LOAD ENV ===
load_dotenv()
AUTHORIZED_USER_IDS = int(os.getenv("AUTHORIZED_USER_IDS"))
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
ASSISTANT_ID = os.getenv("ASSISTANT_ID")

# === LOGGING ===
logging.basicConfig(level=logging.INFO)

# === INIT OBJECTS ===
session = AiohttpSession()
bot = Bot(token=TELEGRAM_TOKEN, session=session)
dp = Dispatcher()
router = Router()

# === MAIN MENU KEYBOARD ===
main_keyboard = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton(text="🍽 Блюдо"), KeyboardButton(text="🩺 Симптом")]
    ],
    resize_keyboard=True,
    one_time_keyboard=False
)
openai_client = OpenAI(api_key=OPENAI_API_KEY)

def write_to_sheet(data_row: list):
    scope = ["https://spreadsheets.google.com/feeds", "https://www.googleapis.com/auth/drive"]
    creds = ServiceAccountCredentials.from_json_keyfile_name("google_credentials.json", scope)
    client = gspread.authorize(creds)
    sheet = client.open("Nutribot").worksheet("Nutrition")
    sheet.append_row(data_row)

# === SYMPTOM SHEET WRITER ===
def write_symptom_to_sheet(data_row: list):
    scope = ["https://spreadsheets.google.com/feeds", "https://www.googleapis.com/auth/drive"]
    creds = ServiceAccountCredentials.from_json_keyfile_name("google_credentials.json", scope)
    client = gspread.authorize(creds)
    sheet = client.open("Nutribot").worksheet("Symptoms")
    sheet.append_row(data_row)

def is_authorized(message: Message) -> bool:
    return message.from_user.id == AUTHORIZED_USER_IDS


# === COMMAND /dish ===
@router.message(Command("dish"))
async def cmd_dish(message: Message):
    if not is_authorized(message):
        await message.answer("⛔️ У тебя нет доступа к этому боту.")
        return
    # Use per-user state via context or a global dictionary (simplest for now)
    if not hasattr(cmd_dish, "user_state"):
        cmd_dish.user_state = {}
    cmd_dish.user_state[message.from_user.id] = True
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
        [InlineKeyboardButton(text="⚡ Боль", callback_data="symptom:Боль")]
    ])
    await message.answer("Выбери симптом:", reply_markup=keyboard)


# === UNIVERSAL MESSAGE HANDLER ===
@router.message()
async def universal_handler(message: Message, **kwargs):
    if not is_authorized(message):
        await message.answer("⛔️ У тебя нет доступа к этому боту.")
        return
    # Use the same per-user state as in cmd_dish
    if not hasattr(cmd_dish, "user_state"):
        cmd_dish.user_state = {}
    if not cmd_dish.user_state.get(message.from_user.id):
        return  # Ignore messages if not awaiting dish input

    # Reset the flag at the end of processing
    cmd_dish.user_state[message.from_user.id] = False

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
        reply = messages.data[0].content[0].text.value

        lines = reply.split("\n")
        fields = {"dish": "", "ingredients": "", "fodmap": "", "calories": "", "carbs": "", "proteins": "", "fats": ""}
        for line in lines:
            for key in fields:
                if line.lower().startswith(f"{key}:"):
                    fields[key] = line.split(":", 1)[1].strip()

        timestamp = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        write_to_sheet([
            timestamp,
            fields["dish"],
            fields["ingredients"],
            fields["fodmap"],
            fields["calories"],
            fields["carbs"],
            fields["proteins"],
            fields["fats"]
        ])

        await message.answer(reply)

    elif message.content_type == ContentType.PHOTO:
        import io

        await message.answer("⏳ Загружаю фото...")

        # Получаем файл от Telegram
        photo = message.photo[-1]
        tg_file = await bot.get_file(photo.file_id)
        file_path = tg_file.file_path
        file_content = await bot.download_file(file_path)
        file_bytes = io.BytesIO(file_content.read())

        # Загружаем в OpenAI с purpose vision
        openai_file = openai_client.files.create(
            file=("image.jpg", file_bytes),
            purpose="vision"
        )

        # Создаём thread и отправляем изображение как image_file
        thread = openai_client.beta.threads.create()
        openai_client.beta.threads.messages.create(
            thread_id=thread.id,
            role="user",
            content=[
                {"type": "text", "text": "На этом изображении еда. Назови ингредиенты, уровень FODMAP и калорийность."},
                {"type": "image_file", "image_file": {"file_id": openai_file.id}}
            ]
        )

        # Запускаем ассистента
        openai_client.beta.threads.runs.create_and_poll(
            thread_id=thread.id,
            assistant_id=ASSISTANT_ID
        )

        # Получаем ответ
        messages = openai_client.beta.threads.messages.list(thread_id=thread.id)
        reply = messages.data[0].content[0].text.value

        lines = reply.split("\n")
        fields = {"dish": "", "ingredients": "", "fodmap": "", "calories": "", "carbs": "", "proteins": "", "fats": ""}
        for line in lines:
            for key in fields:
                if line.lower().startswith(f"{key}:"):
                    fields[key] = line.split(":", 1)[1].strip()

        timestamp = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        write_to_sheet([
            timestamp,
            fields["dish"],
            fields["ingredients"],
            fields["fodmap"],
            fields["calories"],
            fields["carbs"],
            fields["proteins"],
            fields["fats"]
        ])

        await message.answer(reply)

    else:
        await message.answer("Отправь мне блюдо.")


# === SYMPTOM REGISTRATION HANDLERS ===
@router.callback_query(F.data.startswith("symptom:"))
async def ask_severity(callback: CallbackQuery):
    if callback.from_user.id not in AUTHORIZED_USER_IDS:
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
    if callback.from_user.id not in AUTHORIZED_USER_IDS:
        await callback.answer("⛔️ Нет доступа", show_alert=True)
        return
    _, symptom, severity = callback.data.split(":")
    timestamp = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    write_symptom_to_sheet([timestamp, symptom, severity])
    await callback.message.edit_text(f"✅ Симптом '{symptom}' ({severity}) записан.")

# === MAIN ===
async def main():
    dp.include_router(router)
    await dp.start_polling(bot)

if __name__ == "__main__":
    import asyncio
    asyncio.run(main())
@router.message(Command("start"))
async def cmd_start(message: Message):
    if not is_authorized(message):
        await message.answer("⛔️ У тебя нет доступа к этому боту.")
        return
    await message.answer("Привет! Выбери действие:", reply_markup=main_keyboard)


# === MAIN MENU BUTTON HANDLERS ===