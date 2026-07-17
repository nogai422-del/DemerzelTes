# Инициализация экземпляра Bot из переменных окружения.

from aiogram import Bot
from dotenv import load_dotenv
from env_config import require_env

load_dotenv()

TOKEN = require_env("BOT_TOKEN")

bot = Bot(token=TOKEN)
