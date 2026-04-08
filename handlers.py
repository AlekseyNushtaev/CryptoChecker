import html
from datetime import datetime, timedelta

from aiogram import Router, F
from aiogram.types import (
    Message,
    ReplyKeyboardMarkup,
    KeyboardButton,
    CallbackQuery,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
)
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.exceptions import TelegramBadRequest
from sqlalchemy import select, delete, func
from sqlalchemy.exc import IntegrityError

from db.models import Session, Wallet, Balance, User, CryptoFlow
from config import ADMIN_IDS, USER_PASS

router = Router()

TOKEN_ORDER = ("btc", "eth", "ton", "tron")


def get_admin_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="💼 Кошельки"), KeyboardButton(text="📊 Статистика")],
        ],
        resize_keyboard=True,
    )


def _shorten_address(addr: str, left: int = 10, right: int = 8) -> str:
    if len(addr) <= left + right + 1:
        return addr
    return f"{addr[:left]}…{addr[-right:]}"


def _wallet_sort_key(w: Wallet) -> tuple:
    t = w.token.lower()
    try:
        idx = TOKEN_ORDER.index(t)
    except ValueError:
        idx = len(TOKEN_ORDER)
    return (idx, w.id)


async def load_wallets_sorted():
    async with Session() as session:
        result = await session.execute(select(Wallet))
        wallets = list(result.scalars().all())
    wallets.sort(key=_wallet_sort_key)
    return wallets


def format_wallets_caption(wallets: list[Wallet]) -> str:
    lines = ["💼 <b>Кошельки</b>", ""]
    if not wallets:
        lines.append("<i>Пока нет кошельков.</i>")
        return "\n".join(lines)
    for i, w in enumerate(wallets, start=1):
        token_u = w.token.upper()
        lines.append(
            f'{i}. <b>{token_u}</b> <code>{html.escape(w.address)}</code>'
        )
    return "\n".join(lines)


def build_wallets_inline_keyboard(wallets) -> InlineKeyboardMarkup:
    rows = []
    for i, w in enumerate(wallets, start=1):
        label = f"{i}. {w.token} {_shorten_address(w.address)}"
        if len(label) > 64:
            label = label[:61] + "..."
        rows.append(
            [
                InlineKeyboardButton(text=label, callback_data=f"w:i:{w.id}"),
                InlineKeyboardButton(text="Удалить", callback_data=f"w:del:{w.id}"),
            ]
        )
    rows.append([InlineKeyboardButton(text="➕ Добавить кошелек", callback_data="w:add")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


async def build_wallets_message_payload() -> tuple[str, InlineKeyboardMarkup]:
    wallets = await load_wallets_sorted()
    text = format_wallets_caption(wallets)
    kb = build_wallets_inline_keyboard(wallets)
    return text, kb


async def delete_wallet_cascade(wallet_id: int) -> bool:
    async with Session() as session:
        w = await session.get(Wallet, wallet_id)
        if not w:
            return False
        await session.execute(delete(CryptoFlow).where(CryptoFlow.wallet_id == wallet_id))
        await session.execute(delete(Balance).where(Balance.wallet_id == wallet_id))
        await session.execute(delete(Wallet).where(Wallet.id == wallet_id))
        await session.commit()
    return True


async def try_edit_wallets_panel(bot, chat_id: int, message_id: int) -> bool:
    text, kb = await build_wallets_message_payload()
    try:
        await bot.edit_message_text(
            chat_id=chat_id,
            message_id=message_id,
            text=text,
            reply_markup=kb,
            parse_mode="HTML",
        )
        return True
    except TelegramBadRequest as e:
        if "message is not modified" in str(e).lower():
            return True
        return False


class AdminStates(StatesGroup):
    waiting_for_wallet_address = State()


@router.message(Command("start"))
async def start_command(message: Message, state: FSMContext):
    await state.clear()

    if message.from_user.id in ADMIN_IDS:
        await message.answer(
            "Добро пожаловать, администратор!",
            reply_markup=get_admin_keyboard(),
        )
    else:
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
                    CryptoFlow.time_created >= today_start,
                )
            ),
            "неделю": session.execute(
                select(func.sum(CryptoFlow.price))
                .where(
                    CryptoFlow.price > 0,
                    CryptoFlow.time_created >= week_start,
                )
            ),
            "месяц": session.execute(
                select(func.sum(CryptoFlow.price))
                .where(
                    CryptoFlow.price > 0,
                    CryptoFlow.time_created >= month_start,
                )
            ),
            "все время": session.execute(
                select(func.sum(CryptoFlow.price)).where(CryptoFlow.price > 0)
            ),
        }

        stats_text = "📊 Статистика поступлений:\n\n"
        for period, query in queries.items():
            result = await query
            total = result.scalar() or 0
            stats_text += f"За {period}: {total:.2f} $\n"

        await message.answer(stats_text, reply_markup=get_admin_keyboard())


@router.message(F.text == "💼 Кошельки", F.from_user.id.in_(ADMIN_IDS))
async def show_wallets_panel(message: Message, state: FSMContext):
    await state.clear()
    text, kb = await build_wallets_message_payload()
    await message.answer(text, reply_markup=kb, parse_mode="HTML")


@router.callback_query(F.data.startswith("w:i:"), F.from_user.id.in_(ADMIN_IDS))
async def wallet_info_callback(callback: CallbackQuery):
    try:
        wid = int(callback.data.split(":")[2])
    except (IndexError, ValueError):
        await callback.answer("Ошибка данных", show_alert=True)
        return
    async with Session() as session:
        w = await session.get(Wallet, wid)
    if w:
        await callback.answer(w.address[:200], show_alert=True)
    else:
        await callback.answer("Кошелек не найден", show_alert=True)


@router.callback_query(F.data.startswith("w:del:"), F.from_user.id.in_(ADMIN_IDS))
async def wallet_delete_callback(callback: CallbackQuery):
    try:
        wid = int(callback.data.split(":")[2])
    except (IndexError, ValueError):
        await callback.answer("Ошибка данных", show_alert=True)
        return
    ok = await delete_wallet_cascade(wid)
    if not ok:
        await callback.answer("Уже удален или не найден", show_alert=True)
        return
    await callback.answer("Удалено")
    edited = await try_edit_wallets_panel(
        callback.bot,
        callback.message.chat.id,
        callback.message.message_id,
    )
    if not edited:
        text, kb = await build_wallets_message_payload()
        await callback.message.answer(text, reply_markup=kb, parse_mode="HTML")


@router.callback_query(F.data == "w:add", F.from_user.id.in_(ADMIN_IDS))
async def add_wallet_callback(callback: CallbackQuery, state: FSMContext):
    await state.update_data(
        wallets_panel_chat_id=callback.message.chat.id,
        wallets_panel_message_id=callback.message.message_id,
    )
    await state.set_state(AdminStates.waiting_for_wallet_address)
    await callback.answer()
    await callback.message.answer(
        "Введите адрес кошелька и тип через пробел (например: 1A1zP1eP5QGefi2DMPTfTL5SLmv7DivfNa btc):",
        reply_markup=get_admin_keyboard(),
    )


@router.message(AdminStates.waiting_for_wallet_address, F.from_user.id.in_(ADMIN_IDS))
async def add_wallet_finish(message: Message, state: FSMContext):
    data = await state.get_data()
    panel_chat_id = data.get("wallets_panel_chat_id")
    panel_message_id = data.get("wallets_panel_message_id")

    try:
        address, token = message.text.split()
        token = token.lower()

        if token not in ["btc", "eth", "ton", "tron"]:
            await message.answer(
                "Неверный токен. Допустимые: btc, eth, ton, tron",
                reply_markup=get_admin_keyboard(),
            )
            return

        async with Session() as session:
            wallet = Wallet(address=address, token=token)
            session.add(wallet)
            try:
                await session.commit()
            except IntegrityError:
                await session.rollback()
                await message.answer(
                    "Этот кошелек уже существует в базе",
                    reply_markup=get_admin_keyboard(),
                )
                return

        await message.answer("Кошелек добавлен.", reply_markup=get_admin_keyboard())

        if panel_chat_id and panel_message_id:
            ok = await try_edit_wallets_panel(
                message.bot,
                panel_chat_id,
                panel_message_id,
            )
            if not ok:
                text, kb = await build_wallets_message_payload()
                await message.answer(text, reply_markup=kb, parse_mode="HTML")

    except ValueError:
        await message.answer(
            "Неверный формат. Используйте: <address> <token>",
            reply_markup=get_admin_keyboard(),
        )
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
