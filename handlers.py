from datetime import datetime, timedelta
from io import BytesIO

from aiogram import Router, F
from aiogram.types import Message, BufferedInputFile, ReplyKeyboardMarkup, KeyboardButton, ReplyKeyboardRemove
from aiogram.filters import Command, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from openpyxl.styles import Font
from openpyxl.workbook import Workbook
from sqlalchemy import select, delete, func
from sqlalchemy.exc import IntegrityError

from db.models import Session, Wallet, Balance, User, CryptoFlow
from config import ADMIN_IDS, USER_PASS

router = Router()


# Клавиатура для администраторов
def get_admin_keyboard():
    keyboard = ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="📥 Добавить кошелек"), KeyboardButton(text="🗑️ Удалить кошелек")],
            [KeyboardButton(text="📊 Статистика")]
        ],
        resize_keyboard=True
    )
    return keyboard


# Состояния FSM
class AdminStates(StatesGroup):
    waiting_for_wallet_address = State()
    waiting_for_wallet_remove = State()


@router.message(Command("start"))
async def start_command(message: Message, state: FSMContext):
    # Сброс любых активных состояний
    await state.clear()

    if message.from_user.id in ADMIN_IDS:
        # Администраторы получают специальную клавиатуру
        await message.answer(
            "Добро пожаловать, администратор!",
            reply_markup=get_admin_keyboard()
        )
    else:
        # Обычные пользователи
        async with Session() as session:
            result = await session.execute(
                select(User).where(User.user_id == message.from_user.id)
            )
            user = result.scalar_one_or_none()

            if not user:
                user = User(user_id=message.from_user.id)
                session.add(user)
                await session.commit()

            if user.is_active:
                await message.answer("Вы уже активированы!")
            else:
                await message.answer("Введите пароль для активации:")


@router.message(F.text == "📊 Статистика", F.from_user.id.in_(ADMIN_IDS))
async def show_stats(message: Message):
    async with Session() as session:
        now = datetime.utcnow()
        today_start = now.replace(hour=0, minute=0, second=0, microsecond=0) - timedelta(hours=3)
        week_start = now - timedelta(days=now.weekday())
        week_start = week_start.replace(hour=0, minute=0, second=0, microsecond=0) - timedelta(hours=3)
        month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0) - timedelta(hours=3)

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

        stats_text = "📊 Статистика поступлений:\n\n"
        for period, query in queries.items():
            result = await query
            total = result.scalar() or 0
            stats_text += f"За {period}: {total:.2f} $\n"

        await message.answer(stats_text, reply_markup=get_admin_keyboard())


@router.message(F.text == "📥 Добавить кошелек", F.from_user.id.in_(ADMIN_IDS))
async def add_wallet_start(message: Message, state: FSMContext):
    await message.answer(
        "Введите адрес кошелька и тип через пробел (например: 1A1zP1eP5QGefi2DMPTfTL5SLmv7DivfNa btc):",
        reply_markup=ReplyKeyboardRemove()
    )
    await state.set_state(AdminStates.waiting_for_wallet_address)


@router.message(F.text == "🗑️ Удалить кошелек", F.from_user.id.in_(ADMIN_IDS))
async def remove_wallet_start(message: Message, state: FSMContext):
    await message.answer(
        "Введите адрес кошелька и тип через пробел для удаления (например: 1A1zP1eP5QGefi2DMPTfTL5SLmv7DivfNa btc):",
        reply_markup=ReplyKeyboardRemove()
    )
    await state.set_state(AdminStates.waiting_for_wallet_remove)


@router.message(AdminStates.waiting_for_wallet_address, F.from_user.id.in_(ADMIN_IDS))
async def add_wallet_finish(message: Message, state: FSMContext):
    try:
        address, token = message.text.split()
        token = token.lower()

        if token not in ['btc', 'eth', 'ton', 'tron']:
            await message.answer("Неверный токен. Допустимые: btc, eth, ton, tron")
            await state.clear()
            return

        async with Session() as session:
            wallet = Wallet(address=address, token=token)
            session.add(wallet)
            await session.commit()
            await message.answer(f"Кошелек {address} ({token}) добавлен", reply_markup=get_admin_keyboard())

    except ValueError:
        await message.answer("Неверный формат. Используйте: <address> <token>")
    except IntegrityError:
        await message.answer("Этот кошелек уже существует в базе", reply_markup=get_admin_keyboard())
    finally:
        await state.clear()


@router.message(AdminStates.waiting_for_wallet_remove, F.from_user.id.in_(ADMIN_IDS))
async def remove_wallet_finish(message: Message, state: FSMContext):
    try:
        address, token = message.text.split()
        token = token.lower()

        async with Session() as session:
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

            result = await session.execute(
                delete(Wallet).where(
                    Wallet.address == address,
                    Wallet.token == token
                )
            )

            await session.commit()

            if result.rowcount > 0:
                await message.answer(f"Кошелек {address} ({token}) удален", reply_markup=get_admin_keyboard())
            else:
                await message.answer("Кошелек не найден", reply_markup=get_admin_keyboard())

    except ValueError:
        await message.answer("Неверный формат. Используйте: <address> <token>")
    finally:
        await state.clear()


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