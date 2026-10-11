import asyncio
import logging
from uvicorn import Server, Config

from database.models import init_db
from api.server import app
# Импортируем бота (создадим этот файл следующим шагом)
# from bot.handlers import dp, bot 

async def main():
    logging.basicConfig(level=logging.INFO)
    await init_db()
    
    # Настройки FastAPI сервера
    config = Config(app=app, host="0.0.0.0", port=8000, log_level="info")
    server = Server(config)
    
    logging.info("🚀 Запуск FastAPI сервера и Telegram бота...")
    
    # Запускаем параллельно сервер и бота (раскомментируй строку с ботом позже)
    await asyncio.gather(
        server.serve(),
        # dp.start_polling(bot)
    )

if __name__ == "__main__":
    asyncio.run(main())