from aiogram import Bot
from config import TG_TOKEN, SIGNAL
from typing import Optional

# Инициализация бота Telegram
bot: Optional[Bot] = Bot(token=TG_TOKEN)


async def notify_signal(text: str) -> None:
    if SIGNAL is not None:
        await bot.send_message(SIGNAL, text)
