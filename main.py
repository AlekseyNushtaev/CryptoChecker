import asyncio
import logging

from aiogram import Dispatcher

import handlers
from balance_checker import periodic_balance_check
from bot import bot
from db.models import create_tables
from typing import NoReturn


logger: logging.Logger = logging.getLogger(__name__)
logging.basicConfig(
            level=logging.INFO,
            format="%(asctime)s - %(levelname)s - %(name)s - %(message)s"
        )


async def main() -> None:
    """
    Основная функция запуска бота

    Эта функция:
    1. Инициализирует таблицы в базе данных
    2. Настраивает логирование
    3. Регистрирует обработчики сообщений
    4. Запускает бота в режиме long-polling

    Шаги выполнения:
    1. Создание таблиц БД (если не существуют)
    2. Настройка уровня логирования (INFO)
    3. Инициализация диспетчера
    4. Регистрация роутеров (пользовательские и административные обработчики)
    5. Удаление ожидающих апдейтов
    6. Запуск опроса серверов Telegram

    Обработка ошибок:
        Ловит и логирует все исключения во время работы
    """
    try:
        # Инициализация таблиц в базе данных
        await create_tables()
        # Настройка базового логирования
        logger.info("Инициализация таблиц базы данных завершена")

        asyncio.create_task(periodic_balance_check())

        # Создание диспетчера для обработки событий
        dp: Dispatcher = Dispatcher()

        # Регистрация роутеров
        dp.include_router(handlers.router)  # обработчики
        logger.info("Роутеры успешно зарегистрированы")

        # Удаление вебхука для очистки ожидающих обновлений
        await bot.delete_webhook(drop_pending_updates=True)
        logger.info("Ожидающие обновления очищены")

        # Запуск бота в режиме long-polling
        logger.info("Запуск бота в режиме long-polling...")
        await bot.send_message(1012882762, 'Бот запущен!!!')
        await dp.start_polling(bot)


    except Exception as e:
        logger.exception(f"Критическая ошибка: {str(e)}")
        raise


def run_app() -> NoReturn:
    """
    Точка входа для запуска приложения

    Эта функция:
    1. Запускает асинхронную main функцию
    2. Обрабатывает KeyboardInterrupt (Ctrl+C) для корректного завершения
    3. Гарантирует закрытие ресурсов при завершении работы

    Особенности:
        - Использует asyncio.run для запуска асинхронного приложения
        - Перехватывает прерывание с клавиатуры для чистого выхода
        - Логирует сообщение о завершении работы
    """
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Работа бота принудительно завершена пользователем")
    finally:
        logger.info("Приложение завершило работу")


# Точка входа при запуске скрипта
if __name__ == '__main__':
    run_app()
