import html
import re
from datetime import date, datetime, timedelta

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


def stats_custom_inline_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="📅 Кастомная статистика",
                    callback_data="stats:custom",
                )
            ]
        ]
    )


def _day_boundary_datetime(d: date, *, end_of_day: bool) -> datetime:
    """Как в show_stats: полуночь календарного дня в UTC, сдвиг −3 ч."""
    base = datetime(d.year, d.month, d.day, 0, 0, 0)
    if end_of_day:
        base = base + timedelta(days=1)
    return base - timedelta(hours=3)


def parse_date_ddmmyy(text: str) -> tuple[date | None, str | None]:
    """
    Формат ДД.ММ.ГГ или ДД.ММ.ГГГГ (цифры, разделитель точка).
    Возвращает (date, None) или (None, сообщение об ошибке).
    """
    raw = (text or "").strip()
    if not raw:
        return None, "Введите дату, например 15.03.25"
    if not re.fullmatch(r"\d{1,2}\.\d{1,2}\.\d{2,4}", raw):
        return None, "Используйте формат ДД.ММ.ГГ или ДД.ММ.ГГГГ, только цифры и точки."

    parts = raw.split(".")
    d_s, m_s, y_s = parts[0], parts[1], parts[2]
    try:
        dd = int(d_s)
        mm = int(m_s)
        yy = int(y_s)
    except ValueError:
        return None, "Некорректные числа в дате."

    if len(y_s) == 2:
        yyyy = 2000 + yy
    elif len(y_s) == 4:
        yyyy = yy
    else:
        return None, "Год укажите двумя (25) или четырьмя (2025) цифрами."

    try:
        parsed = date(yyyy, mm, dd)
    except ValueError:
        return None, "Такой даты не существует. Проверьте день, месяц и год."

    return parsed, None


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
    custom_stats_start = State()
    custom_stats_end = State()


async def custom_range_inflows_by_wallet(
    start_d: date, end_d: date
) -> tuple[float, list[tuple[Wallet, float]], float]:
    """
    Поступления (CryptoFlow.price > 0) за [start_d, end_d] включительно.
    Возвращает (общая сумма, список (кошелёк, сумма) в порядке списка кошельков,
    сумма по wallet_id без записи в таблице wallet — редкий случай).
    """
    start_dt = _day_boundary_datetime(start_d, end_of_day=False)
    end_excl = _day_boundary_datetime(end_d, end_of_day=True)
    async with Session() as session:
        result = await session.execute(
            select(CryptoFlow.wallet_id, func.sum(CryptoFlow.price)).where(
                CryptoFlow.price > 0,
                CryptoFlow.time_created >= start_dt,
                CryptoFlow.time_created < end_excl,
            ).group_by(CryptoFlow.wallet_id)
        )
        by_id: dict[int, float] = {
            wid: float(s or 0) for wid, s in result.all()
        }

    total = sum(by_id.values())
    wallets = await load_wallets_sorted()
    known_ids = {w.id for w in wallets}
    orphan_total = sum(amt for wid, amt in by_id.items() if wid not in known_ids)

    rows: list[tuple[Wallet, float]] = [
        (w, by_id.get(w.id, 0.0)) for w in wallets
    ]
    return total, rows, orphan_total


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


def _fmt_dd_mm_yy(d: date) -> str:
    return f"{d.day:02d}.{d.month:02d}.{d.year % 100:02d}"


@router.message(F.text == "📊 Статистика", F.from_user.id.in_(ADMIN_IDS))
async def show_stats(message: Message, state: FSMContext):
    await state.clear()
    async with Session() as session:
        now = datetime.utcnow()
        today_start = now.replace(hour=0, minute=0, second=0, microsecond=0) - timedelta(hours=3)
        week_start = now - timedelta(days=now.weekday())
        week_start = week_start.replace(hour=0, minute=0, second=0, microsecond=0) - timedelta(hours=3)
        month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0) - timedelta(hours=3)

        first_of_month = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        if first_of_month.month == 1:
            prev_cal = datetime(first_of_month.year - 1, 12, 1, 0, 0, 0)
        else:
            prev_cal = datetime(first_of_month.year, first_of_month.month - 1, 1, 0, 0, 0)
        prev_month_start = prev_cal - timedelta(hours=3)

        q_day = session.execute(
            select(func.sum(CryptoFlow.price)).where(
                CryptoFlow.price > 0,
                CryptoFlow.time_created >= today_start,
            )
        )
        q_week = session.execute(
            select(func.sum(CryptoFlow.price)).where(
                CryptoFlow.price > 0,
                CryptoFlow.time_created >= week_start,
            )
        )
        q_month = session.execute(
            select(func.sum(CryptoFlow.price)).where(
                CryptoFlow.price > 0,
                CryptoFlow.time_created >= month_start,
            )
        )
        q_prev_month = session.execute(
            select(func.sum(CryptoFlow.price)).where(
                CryptoFlow.price > 0,
                CryptoFlow.time_created >= prev_month_start,
                CryptoFlow.time_created < month_start,
            )
        )
        q_all = session.execute(
            select(func.sum(CryptoFlow.price)).where(CryptoFlow.price > 0)
        )

        rows = [
            ("сутки", q_day),
            ("неделю", q_week),
            ("месяц", q_month),
            ("прошлый месяц", q_prev_month),
            ("все время", q_all),
        ]

        stats_text = "📊 Статистика поступлений:\n\n"
        for period, query in rows:
            result = await query
            total = result.scalar() or 0
            stats_text += f"За {period}: {total:.2f} $\n"

    await message.answer(
        stats_text,
        reply_markup=stats_custom_inline_keyboard(),
    )


@router.message(F.text == "💼 Кошельки", F.from_user.id.in_(ADMIN_IDS))
async def show_wallets_panel(message: Message, state: FSMContext):
    await state.clear()
    text, kb = await build_wallets_message_payload()
    await message.answer(text, reply_markup=kb, parse_mode="HTML")


@router.callback_query(F.data == "stats:custom", F.from_user.id.in_(ADMIN_IDS))
async def stats_custom_callback(callback: CallbackQuery, state: FSMContext):
    await state.set_state(AdminStates.custom_stats_start)
    await callback.answer()
    await callback.message.answer(
        "Введите дату <b>начала</b> периода в формате ДД.ММ.ГГ (например 15.03.25):",
        parse_mode="HTML",
        reply_markup=get_admin_keyboard(),
    )


@router.message(AdminStates.custom_stats_start, F.from_user.id.in_(ADMIN_IDS))
async def stats_custom_start_date(message: Message, state: FSMContext):
    parsed, err = parse_date_ddmmyy(message.text)
    if err:
        await message.answer(
            f"{err}\n\nПример: 01.04.25",
            reply_markup=get_admin_keyboard(),
        )
        return
    await state.update_data(custom_stats_start=parsed.isoformat())
    await state.set_state(AdminStates.custom_stats_end)
    await message.answer(
        "Введите дату <b>конца</b> периода в формате ДД.ММ.ГГ (включительно):",
        parse_mode="HTML",
        reply_markup=get_admin_keyboard(),
    )


@router.message(AdminStates.custom_stats_end, F.from_user.id.in_(ADMIN_IDS))
async def stats_custom_end_date(message: Message, state: FSMContext):
    data = await state.get_data()
    start_raw = data.get("custom_stats_start")
    if not start_raw:
        await state.clear()
        await message.answer("Сессия сброшена. Нажмите «Кастомная статистика» снова.")
        return

    start_d = date.fromisoformat(start_raw)
    end_d, err = parse_date_ddmmyy(message.text)
    if err:
        await message.answer(
            f"{err}\n\nПример: 30.04.25",
            reply_markup=get_admin_keyboard(),
        )
        return
    if end_d < start_d:
        await message.answer(
            "Дата конца не может быть раньше даты начала. Введите дату конца ещё раз:",
            reply_markup=get_admin_keyboard(),
        )
        return

    total, per_wallet, orphan_total = await custom_range_inflows_by_wallet(start_d, end_d)
    a, b = _fmt_dd_mm_yy(start_d), _fmt_dd_mm_yy(end_d)
    msg_lines = [
        f"Поступлений в период {a}-{b} включительно — {total:.2f} $",
        "",
        "<b>По кошелькам:</b>",
    ]
    if not per_wallet:
        msg_lines.append("<i>Нет кошельков в базе.</i>")
    else:
        for w, amt in per_wallet:
            coin = w.token.upper()
            addr_short = html.escape(_shorten_address(w.address))
            msg_lines.append(
                f"• <b>{coin}</b> <code>{addr_short}</code> — {amt:.2f} $"
            )
    if orphan_total > 0:
        msg_lines.append(
            f"• <i>Прочие (нет в списке кошельков)</i> — {orphan_total:.2f} $"
        )
    await message.answer(
        "\n".join(msg_lines),
        parse_mode="HTML",
        reply_markup=get_admin_keyboard(),
    )
    await state.clear()


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
