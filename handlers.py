from datetime import datetime, timedelta
from io import BytesIO

from aiogram import Router, F
from aiogram.types import Message, BufferedInputFile
from aiogram.filters import Command
from openpyxl.styles import Font
from openpyxl.workbook import Workbook
from sqlalchemy import select, delete, func
from sqlalchemy.exc import IntegrityError

from db.models import Session, Wallet, Balance, User, CryptoFlow
from config import ADMIN_IDS, USER_PASS

router = Router()


@router.message(Command("start"), ~F.from_user.id.in_(ADMIN_IDS))
async def start_command(message: Message):
    async with Session() as session:
        # Проверяем существует ли пользователь
        result = await session.execute(
            select(User).where(User.user_id == message.from_user.id)
        )
        user = result.scalar_one_or_none()

        if not user:
            # Создаем нового пользователя
            user = User(user_id=message.from_user.id)
            session.add(user)
            await session.commit()

        if user.is_active:
            await message.answer("Вы уже активированы!")
        else:
            await message.answer("Введите пароль для активации:")


@router.message(F.text, ~F.from_user.id.in_(ADMIN_IDS))
async def handle_password(message: Message):
    async with Session() as session:
        result = await session.execute(
            select(User).where(User.user_id == message.from_user.id)
        )
        user = result.scalar_one_or_none()

        if user and not user.is_active:
            if message.text == USER_PASS:
                user.is_active = True
                await session.commit()
                await message.answer("Пароль верный! Вы активированы.")
            else:
                await message.answer("Неверный пароль. Попробуйте еще раз.")


@router.message(Command("add"), F.from_user.id.in_(ADMIN_IDS))
async def add_wallet(message: Message):
    try:
        _, address, token = message.text.split()
        token = token.lower()

        if token not in ['btc', 'eth', 'ton', 'tron']:
            await message.answer("Неверный токен. Допустимые: btc, eth, ton, tron")
            return

        async with Session() as session:
            wallet = Wallet(address=address, token=token)
            session.add(wallet)
            await session.commit()
            await message.answer(f"Кошелек {address} ({token}) добавлен")

    except ValueError:
        await message.answer("Неверный формат команды. Используйте: /add <address> <token>")
    except IntegrityError:
        await message.answer("Этот кошелек уже существует в базе")


@router.message(Command("remove"), F.from_user.id.in_(ADMIN_IDS))
async def remove_wallet(message: Message):
    try:
        _, address, token = message.text.split()
        token = token.lower()

        async with Session() as session:
            # Удаляем балансы кошелька
            await session.execute(
                delete(Balance).where(
                    Balance.wallet_id.in_(
                        select(Wallet.id).where(
                            Wallet.address == address,
                            Wallet.token == token
                        )
                    )
                )
            )

            # Удаляем сам кошелек
            result = await session.execute(
                delete(Wallet).where(
                    Wallet.address == address,
                    Wallet.token == token
                )
            )

            await session.commit()

            if result.rowcount > 0:
                await message.answer(f"Кошелек {address} ({token}) удален")
            else:
                await message.answer("Кошелек не найден")

    except ValueError:
        await message.answer("Неверный формат команды. Используйте: /remove <address> <token>")


@router.message(Command("export"), F.from_user.id.in_(ADMIN_IDS))
async def export_data(message: Message):
    async with Session() as session:
        # Получаем все уникальные временные метки
        time_query = await session.execute(
            select(Balance.time_check)
            .distinct()
            .order_by(Balance.time_check.desc())
        )
        all_timestamps = [row[0] for row in time_query]

        # Получаем все кошельки, отсортированные по типу токена
        wallets_query = await session.execute(
            select(Wallet)
            .order_by(Wallet.token, Wallet.address)
        )
        wallets = wallets_query.scalars().all()

        # Создаем Excel-файл
        wb = Workbook()
        ws = wb.active
        ws.title = "Балансы"

        # Заголовки
        headers = ["Время", "Общий баланс (USD)"]
        for wallet in wallets:
            headers.append(f"{wallet.address} ({wallet.token})")

        for col, header in enumerate(headers, 1):
            ws.cell(row=1, column=col, value=header).font = Font(bold=True)

        # Заполняем данные
        row_num = 2
        for timestamp in all_timestamps:
            # Получаем общий баланс для этого времени
            total_balance = await session.execute(
                select(func.sum(Balance.price))
                .where(Balance.time_check == timestamp)
            )
            total = total_balance.scalar() or 0.0

            # Записываем время и общий баланс
            ws.cell(row=row_num, column=1, value=timestamp)
            ws.cell(row=row_num, column=2, value=total)

            # Для каждого кошелька получаем баланс на этот момент времени
            for col_num, wallet in enumerate(wallets, 3):
                balance_query = await session.execute(
                    select(Balance.amount)
                    .where(
                        Balance.wallet_id == wallet.id,
                        Balance.time_check == timestamp
                    )
                )
                balance = balance_query.scalar_one_or_none()
                if balance is not None:
                    ws.cell(row=row_num, column=col_num, value=balance)

            row_num += 1

        # Сохраняем в буфер
        buffer = BytesIO()
        wb.save(buffer)
        buffer.seek(0)
        file_data = buffer.getvalue()
        buffer.close()

    # Создаем BufferedInputFile для отправки
    excel_file = BufferedInputFile(
        file=file_data,
        filename="balances_export.xlsx"
    )

    # Отправляем файл
    await message.answer_document(
        document=excel_file,
        caption="Экспорт данных о балансах"
    )


@router.message(Command("stats"), F.from_user.id.in_(ADMIN_IDS))
async def show_stats(message: Message):
    async with Session() as session:
        now = datetime.utcnow()

        # Временные интервалы
        today_start = now.replace(hour=0, minute=0, second=0, microsecond=0) - timedelta(hours=3)
        # Начало недели (понедельник)
        week_start = now - timedelta(days=now.weekday())
        week_start = week_start.replace(hour=0, minute=0, second=0, microsecond=0) - timedelta(hours=3)

        # Начало месяца
        month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0) - timedelta(hours=3)

        # Запросы для разных периодов
        queries = {
            "сутки": session.execute(
                select(func.sum(CryptoFlow.price))
                .where(
                    CryptoFlow.price > 0,
                    CryptoFlow.time_created >= today_start
                )
            ),
            "неделю": session.execute(
                select(func.sum(CryptoFlow.price))
                .where(
                    CryptoFlow.price > 0,
                    CryptoFlow.time_created >= week_start
                )
            ),
            "месяц": session.execute(
                select(func.sum(CryptoFlow.price))
                .where(
                    CryptoFlow.price > 0,
                    CryptoFlow.time_created >= month_start
                )
            ),
            "все время": session.execute(
                select(func.sum(CryptoFlow.price))
                .where(CryptoFlow.price > 0)
            )
        }

        # Получаем результаты
        stats_text = "📊 Статистика поступлений:\n\n"
        for period, query in queries.items():
            result = await query
            total = result.scalar() or 0
            stats_text += f"За {period}: {total:.2f} $\n"

        await message.answer(stats_text)
