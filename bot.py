from aiogram import Bot
from config import TG_TOKEN
from typing import Optional

# Инициализация бота Telegram
bot: Optional[Bot] = Bot(token=TG_TOKEN)
