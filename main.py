import asyncio
import logging

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties

from handlers import user_commands
from handlers.user_commands import init_db, periodic_subscription_check, check_hwid_changes
from config_reader import config

# Настройка логирования
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger(__name__)


async def main():
    bot = Bot(
        config.bot_token.get_secret_value(),
        default=DefaultBotProperties(parse_mode='HTML')
    )
    dp = Dispatcher()
    await init_db()
    logger.info("База данных (aiosqlite) подключена.")

    dp.include_routers(
        user_commands.router
    )

    await bot.delete_webhook(drop_pending_updates=True)

    # FIX: запускаем фоновую проверку подписок на канал
    asyncio.create_task(periodic_subscription_check(bot))
    logger.info("Фоновая задача проверки подписок запущена.")

    # Мониторинг новых HWID-устройств
    asyncio.create_task(check_hwid_changes(bot))
    logger.info("Фоновая задача мониторинга HWID запущена.")

    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
