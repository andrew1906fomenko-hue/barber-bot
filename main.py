import json
import os
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Dict, List

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from telegram import KeyboardButton, ReplyKeyboardMarkup, Update, WebAppInfo
from telegram.ext import Application, CommandHandler, ContextTypes, MessageHandler, filters
from telegram.request import HTTPXRequest

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN")
ADMIN_CHAT_ID = int(os.getenv("ADMIN_CHAT_ID", "0"))
WEB_APP_URL = os.getenv("WEB_APP_URL", "http://localhost:8000")

DATA_FILE = Path("data/bookings.json")

if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN is required")

if not ADMIN_CHAT_ID:
    raise RuntimeError("ADMIN_CHAT_ID is required")

DATA_FILE.parent.mkdir(parents=True, exist_ok=True)

if not DATA_FILE.exists():
    DATA_FILE.write_text("[]", encoding="utf-8")


class BookingRequest(BaseModel):
    telegram_id: int
    telegram_username: str | None = None
    client_name: str = Field(min_length=2, max_length=40)
    phone: str = Field(min_length=6, max_length=20)
    service: str = Field(min_length=2, max_length=60)
    date: str
    time: str


def read_bookings() -> List[Dict]:
    return json.loads(DATA_FILE.read_text(encoding="utf-8"))


def write_bookings(bookings: List[Dict]):
    DATA_FILE.write_text(json.dumps(bookings, ensure_ascii=False, indent=2), encoding="utf-8")


def calendar_view(bookings: List[Dict]) -> str:
    today = date.today()
    days = [today + timedelta(days=i) for i in range(14)]

    weekday = ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс"]

    lines = ["📅 Календарь записей (14 дней):"]

    by_day: Dict[str, List[Dict]] = {}

    for b in bookings:
        by_day.setdefault(b["date"], []).append(b)

    for d in days:
        key = d.isoformat()
        items = sorted(by_day.get(key, []), key=lambda x: x["time"])

        lines.append(f"\n{weekday[d.weekday()]} {d.strftime('%d.%m.%Y')}")

        if not items:
            lines.append("  — свободно")
        else:
            for it in items:
                lines.append(f"  • {it['time']} — {it['client_name']} ({it['service']})")

    return "\n".join(lines)


# Telegram request with bigger timeout (fix for Python 3.14)
request = HTTPXRequest(connect_timeout=30, read_timeout=30)

telegram_app = (
    Application.builder()
    .token(BOT_TOKEN)
    .request(request)
    .build()
)

api = FastAPI(title="Barber Booking Web App")

api.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@api.on_event("startup")
async def startup():
    await telegram_app.initialize()
    await telegram_app.start()
    await telegram_app.updater.start_polling()


@api.on_event("shutdown")
async def shutdown():
    await telegram_app.stop()
    await telegram_app.shutdown()


async def start_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):

    if not update.message:
        return

    keyboard = ReplyKeyboardMarkup(
        [
            [KeyboardButton("🗓 Запись", web_app=WebAppInfo(url=WEB_APP_URL))],
            [KeyboardButton("💈 Услуги"), KeyboardButton("📍 Наш адрес")],
        ],
        resize_keyboard=True,
    )

    await update.message.reply_text(
        "Привет! Добро пожаловать в барбершоп ✂️",
        reply_markup=keyboard,
    )


async def text_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):

    if not update.message:
        return

    text = update.message.text

    if text == "💈 Услуги":

        await update.message.reply_text(
            "Наши услуги:\n"
            "• Мужская стрижка — 1500₽\n"
            "• Стрижка + борода — 2200₽\n"
            "• Оформление бороды — 1000₽"
        )

    elif text == "📍 Наш адрес":

        await update.message.reply_text(
            "Мы находимся: г. Москва, ул. Примерная, 10"
        )


async def admin_calendar(update: Update, context: ContextTypes.DEFAULT_TYPE):

    if not update.message:
        return

    if update.effective_user.id != ADMIN_CHAT_ID:
        await update.message.reply_text("Только для администратора")
        return

    bookings = read_bookings()

    await update.message.reply_text(calendar_view(bookings))


telegram_app.add_handler(CommandHandler("start", start_handler))
telegram_app.add_handler(CommandHandler("calendar", admin_calendar))
telegram_app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, text_handler))


@api.get("/")
def index():
    return FileResponse("web/index.html")


@api.get("/api/bookings")
def get_bookings():
    return {"items": read_bookings()}


@api.post("/api/book")
async def create_booking(payload: BookingRequest):

    try:
        booked_dt = datetime.strptime(
            f"{payload.date} {payload.time}",
            "%Y-%m-%d %H:%M"
        )
    except ValueError:
        raise HTTPException(status_code=400, detail="Неверная дата")

    if booked_dt.date() < date.today():
        raise HTTPException(status_code=400, detail="Дата в прошлом")

    bookings = read_bookings()

    for b in bookings:
        if b["date"] == payload.date and b["time"] == payload.time:
            raise HTTPException(status_code=409, detail="Время занято")

    item = payload.model_dump()
    item["created_at"] = datetime.now().isoformat()

    bookings.append(item)

    write_bookings(bookings)

    await telegram_app.bot.send_message(
        chat_id=ADMIN_CHAT_ID,
        text=f"Новая запись\n{payload.client_name}\n{payload.date} {payload.time}",
    )

    return {"ok": True}


api.mount("/web", StaticFiles(directory="web"), name="web")