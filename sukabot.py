import asyncio
import random
import time
import aiosqlite

from aiogram import Bot, Dispatcher, F
from aiogram.filters import CommandStart
from aiogram.types import Message, InlineKeyboardMarkup, InlineKeyboardButton, CallbackQuery
from aiogram.client.default import DefaultBotProperties

TOKEN = "8882192556:AAF3oDo4sabJSHr1-E-0YxzNjzSPp8rsTO0"

ADMIN_IDS = [
    5966445013,
    5274203328
]

TINKY = "⚡"  # кастомный прем эмодзи (ID: 5469663696387087636)
TINKY_EMOJI = f'<tg-emoji emoji-id="5469663696387087636">{TINKY}</tg-emoji>'

# ВАЖНО: на Railway этот путь должен указывать на примонтированный Volume,
# иначе база будет обнуляться при каждом деплое.
# Пример после настройки Volume в Railway (Mount Path: /data):
# DB_PATH = "/data/glw_coins.db"
DB_PATH = "glw_coins.db"

WORK_COOLDOWN = 7200  # 2 часа

DUEL_ACCEPT_TIMEOUT = 120   # 2 минуты на принятие вызова
DUEL_BETTING_WINDOW = 60    # 1 минута на ставки зрителей
DUEL_SHOT_TIMEOUT = 60      # таймаут на выстрел (чтобы дуэль не висела вечно)

# ── СЛОТЫ (с казик) ──────────────────────────────────────────────────────────
SLOT_SYMBOLS = ["🍒", "🍋", "🍇", "🍉", "⭐", "💎", "7️⃣"]
SLOT_WEIGHTS = [30, 25, 20, 15, 6, 3, 1]  # чем реже символ, тем больше выплата

# множитель для 3 одинаковых символов (включает возврат ставки)
SLOT_PAYOUTS_TRIPLE = {
    "🍒": 2,
    "🍋": 3,
    "🍇": 4,
    "🍉": 5,
    "⭐": 8,
    "💎": 15,
    "7️⃣": 50,
}
SLOT_PAYOUT_PAIR = 1.2  # множитель за любую пару из трёх (утешительный приз)

bot = Bot(token=TOKEN, default=DefaultBotProperties(parse_mode="HTML"))
dp = Dispatcher()

# ── ГЛОБАЛЬНОЕ СОСТОЯНИЕ ДУЭЛЕЙ (in-memory, сбрасывается при рестарте) ───────
# chat_id -> dict с данными активной дуэли
duels: dict[int, dict] = {}


# ── ВСПОМОГАТЕЛЬНОЕ ──────────────────────────────────────────────────────────

def fmt(n: int) -> str:
    """Форматирует число с разделителями разрядов: 100000 -> 100,000"""
    return f"{n:,}"


def display_name(user) -> str:
    username = getattr(user, "username", None)
    if username:
        return f"@{username}"
    first_name = getattr(user, "first_name", None)
    return first_name or f"ID {getattr(user, 'id', '?')}"


def display_name_notag(user) -> str:
    """Имя для списков/топов — никогда не создаёт кликабельный тег-упоминание.
    Не использует @username (это создаёт notify-ссылку в Telegram)."""
    first_name = getattr(user, "first_name", None)
    if first_name:
        return first_name
    username = getattr(user, "username", None)
    if username:
        return username
    return f"ID {getattr(user, 'id', '?')}"


# ── БАЗА ДАННЫХ ──────────────────────────────────────────────────────────────

async def create_db():
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            CREATE TABLE IF NOT EXISTS users (
                user_id   INTEGER PRIMARY KEY,
                balance   INTEGER DEFAULT 0,
                last_earn INTEGER DEFAULT 0
            )
        """)
        await db.commit()


async def ensure_user(user_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT OR IGNORE INTO users (user_id, balance, last_earn) VALUES (?, 0, 0)",
            (user_id,)
        )
        await db.commit()


async def get_balance(user_id: int) -> int:
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            "SELECT balance FROM users WHERE user_id = ?", (user_id,)
        )
        row = await cursor.fetchone()
        return row[0] if row else 0


async def change_balance(user_id: int, amount: int):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE users SET balance = balance + ? WHERE user_id = ?",
            (amount, user_id)
        )
        await db.commit()


async def get_last_earn(user_id: int) -> int:
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            "SELECT last_earn FROM users WHERE user_id = ?", (user_id,)
        )
        row = await cursor.fetchone()
        return row[0] if row else 0


async def update_last_earn(user_id: int, t: int):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE users SET last_earn = ? WHERE user_id = ?",
            (t, user_id)
        )
        await db.commit()


async def transfer_balance(from_id: int, to_id: int, amount: int) -> bool:
    """Атомарно переводит amount тинки от from_id к to_id.
    Возвращает True при успехе, False если не хватает средств."""
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            "SELECT balance FROM users WHERE user_id = ?", (from_id,)
        )
        row = await cursor.fetchone()
        current = row[0] if row else 0

        if current < amount:
            return False

        await db.execute(
            "UPDATE users SET balance = balance - ? WHERE user_id = ?",
            (amount, from_id)
        )
        await db.execute(
            "UPDATE users SET balance = balance + ? WHERE user_id = ?",
            (amount, to_id)
        )
        await db.commit()
        return True


async def reset_all_balances():
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("UPDATE users SET balance = 0")
        await db.commit()


async def reset_all_cooldowns():
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("UPDATE users SET last_earn = 0")
        await db.commit()


# ── КЛАВИАТУРЫ ───────────────────────────────────────────────────────────────

def welcome_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📌 Команды", callback_data="show_commands")],
        [InlineKeyboardButton(text=f"💼 Заработать", callback_data="do_work")],
        [InlineKeyboardButton(text=f"💎 Мой счёт", callback_data="my_balance")],
    ])


def coin_keyboard(owner_id: int, amount: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="🦅 Орёл", callback_data=f"coin:{owner_id}:{amount}:orel"),
            InlineKeyboardButton(text="🪙 Решка", callback_data=f"coin:{owner_id}:{amount}:reshka"),
        ]
    ])


def duel_accept_keyboard(chat_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="✅ Принять вызов", callback_data=f"duel_accept:{chat_id}"),
            InlineKeyboardButton(text="❌ Отклонить", callback_data=f"duel_decline:{chat_id}"),
        ]
    ])


def reset_all_confirm_keyboard(admin_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="✅ Да, обнулить всё", callback_data=f"reset_all_confirm:{admin_id}"),
            InlineKeyboardButton(text="❌ Отмена", callback_data=f"reset_all_cancel:{admin_id}"),
        ]
    ])


def reset_cd_confirm_keyboard(admin_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="✅ Да, обнулить кд", callback_data=f"reset_cd_confirm:{admin_id}"),
            InlineKeyboardButton(text="❌ Отмена", callback_data=f"reset_cd_cancel:{admin_id}"),
        ]
    ])


def commands_text() -> str:
    return (
        f"📌 <b>Команды бота:</b>\n\n"
        f"• <b>счёт</b> — посмотреть свой баланс\n"
        f"• <b>счёт</b> (реплай) — баланс другого игрока\n"
        f"• <b>ворк</b> — заработать тинки (КД 2 часа)\n"
        f"• <b>дать [сумма]</b> (реплай) — перевести тинки другому игроку\n"
        f"• <b>топ богачей</b> — рейтинг игроков\n"
        f"• <b>коммандс</b> — список команд\n\n"
        f"<i>Казино:</i>\n"
        f"• <b>м казик [сумма]</b> — монетка орёл/решка (x2)\n"
        f"• <b>с казик [сумма]</b> — слоты 🎰 (3 одинаковых — джекпот)\n"
        f"• <b>батл [сумма] @юзер</b> (или реплаем) — русская рулетка, стреляешь в себя (1/6)\n"
        f"• <b>ставка [а/б] [сумма]</b> — ставка на исход дуэли (только во время сбора ставок)\n\n"
        f"<i>Админ-команды (реплай):</i>\n"
        f"• <b>начислить [сумма]</b>\n"
        f"• <b>списать [сумма]</b>\n\n"
        f"<i>Админ (без реплая):</i>\n"
        f"• <b>обнулить всех</b> — списать тинки у всех игроков сразу\n"
        f"• <b>обнулить кд</b> — сбросить кулдаун команды «ворк» у всех игроков"
    )


# ── ХЭНДЛЕРЫ СТАРТА / КНОПОК ГЛАВНОГО МЕНЮ ───────────────────────────────────

@dp.message(CommandStart())
async def cmd_start(message: Message):
    await ensure_user(message.from_user.id)
    name = message.from_user.first_name

    await message.answer(
        f"👋 Привет, <b>{name}</b>!\n\n"
        f"Добро пожаловать в <b>GLW-ТИНКИ</b> {TINKY_EMOJI}\n\n"
        f"Здесь ты можешь зарабатывать тинки, соревноваться с друзьями "
        f"и попасть в топ богачей!\n\n"
        f"Нажми кнопку ниже, чтобы узнать, что умеет бот 👇",
        reply_markup=welcome_keyboard()
    )


@dp.callback_query(F.data == "show_commands")
async def cb_commands(call: CallbackQuery):
    await call.answer()
    await call.message.answer(commands_text())


@dp.callback_query(F.data == "my_balance")
async def cb_balance(call: CallbackQuery):
    await call.answer()
    uid = call.from_user.id
    await ensure_user(uid)
    bal = await get_balance(uid)
    await call.message.answer(f"💎 Твой счёт: <b>{fmt(bal)}</b> тинки {TINKY_EMOJI}")


@dp.callback_query(F.data == "do_work")
async def cb_work(call: CallbackQuery):
    await call.answer()
    uid = call.from_user.id
    await ensure_user(uid)

    now = int(time.time())
    last = await get_last_earn(uid)
    left = WORK_COOLDOWN - (now - last)

    if left > 0:
        h, remainder = divmod(left, 3600)
        m, s = divmod(remainder, 60)
        time_str = f"{h}ч {m}м {s}с" if h else f"{m}м {s}с"
        return await call.message.answer(f"⏳ Подожди ещё <b>{time_str}</b>")

    earn = random.randint(1, 25)
    await change_balance(uid, earn)
    await update_last_earn(uid, now)
    bal = await get_balance(uid)

    await call.message.answer(
        f"💼 Ты заработал <b>+{fmt(earn)}</b> тинки {TINKY_EMOJI}\n"
        f"💎 Счёт: <b>{fmt(bal)}</b> тинки"
    )


# ── КАЗИНО: МОНЕТКА (м казик [сумма]) ───────────────────────────────────────

@dp.callback_query(F.data.startswith("coin:"))
async def cb_coin(call: CallbackQuery):
    _, owner_id_str, amount_str, choice = call.data.split(":")
    owner_id = int(owner_id_str)
    amount = int(amount_str)

    if call.from_user.id != owner_id:
        return await call.answer("❌ Это не твоя ставка!", show_alert=True)

    await call.answer()

    bal = await get_balance(owner_id)
    if bal < amount:
        return await call.message.edit_text("❌ Недостаточно тинки на счету — ставка отменена")

    result = random.choice(["orel", "reshka"])
    win = (result == choice)

    await change_balance(owner_id, amount if win else -amount)
    new_bal = await get_balance(owner_id)

    result_text = "Орёл 🦅" if result == "orel" else "Решка 🪙"
    choice_text = "Орёл 🦅" if choice == "orel" else "Решка 🪙"

    if win:
        text = (
            f"🎉 Выпало: <b>{result_text}</b>\n"
            f"Ты выбрал: <b>{choice_text}</b> — победа!\n"
            f"💰 +{fmt(amount)} тинки {TINKY_EMOJI}\n"
            f"💎 Счёт: <b>{fmt(new_bal)}</b>"
        )
    else:
        text = (
            f"😢 Выпало: <b>{result_text}</b>\n"
            f"Ты выбрал: <b>{choice_text}</b> — проигрыш\n"
            f"💸 -{fmt(amount)} тинки {TINKY_EMOJI}\n"
            f"💎 Счёт: <b>{fmt(new_bal)}</b>"
        )

    await call.message.edit_text(text)


# ── ДУЭЛИ (батл / ставка / стрелять) ────────────────────────────────────────

async def duel_accept_timeout_task(chat_id: int):
    await asyncio.sleep(DUEL_ACCEPT_TIMEOUT)
    duel = duels.get(chat_id)
    if duel and duel["status"] == "pending":
        del duels[chat_id]
        await bot.send_message(
            chat_id,
            f"⏳ Вызов от {duel['a_name']} на дуэль истёк — {duel['b_name']} не ответил вовремя."
        )


async def duel_betting_timeout_task(chat_id: int):
    await asyncio.sleep(DUEL_BETTING_WINDOW)
    duel = duels.get(chat_id)
    if duel and duel["status"] == "betting":
        duel["status"] = "fighting"
        await bot.send_message(
            chat_id,
            f"🔒 Ставки закрыты!\n\n"
            f"⚔️ Дуэль начинается: {duel['a_name']} 🆚 {duel['b_name']}\n"
            f"🔫 Это русская рулетка: каждый стреляет <b>сам в себя</b>, шанс словить пулю — 1 из 6.\n"
            f"Первым крутит барабан {duel['a_name']} — напиши <b>стрелять</b>"
        )


async def duel_shot_timeout_task(chat_id: int, shooter_id: int):
    await asyncio.sleep(DUEL_SHOT_TIMEOUT)
    duel = duels.get(chat_id)
    if duel and duel["status"] == "fighting" and duel["turn"] == shooter_id:
        # игрок не выстрелил вовремя — засчитываем ему поражение
        winner_id = duel["b"] if shooter_id == duel["a"] else duel["a"]
        await finish_duel(chat_id, winner_id, timeout=True)


async def finish_duel(chat_id: int, winner_id: int, timeout: bool = False):
    duel = duels.get(chat_id)
    if not duel:
        return

    loser_id = duel["b"] if winner_id == duel["a"] else duel["a"]
    winner_name = duel["a_name"] if winner_id == duel["a"] else duel["b_name"]
    loser_name = duel["b_name"] if winner_id == duel["a"] else duel["a_name"]

    prize = duel["amount"] * 2
    await change_balance(winner_id, prize)

    lines = []
    if timeout:
        lines.append(f"⏳ {loser_name} не выстрелил вовремя и проиграл по таймауту!")
    lines.append(f"🏆 Победитель дуэли: <b>{winner_name}</b>")
    lines.append(f"💰 Приз: <b>{fmt(prize)}</b> тинки {TINKY_EMOJI}")

    # расчёт ставок зрителей
    winner_side = "a" if winner_id == duel["a"] else "b"
    loser_side = "b" if winner_side == "a" else "a"

    winner_pool = sum(b["amount"] for b in duel["bets"].values() if b["side"] == winner_side)
    loser_pool = sum(b["amount"] for b in duel["bets"].values() if b["side"] == loser_side)

    if winner_pool > 0 and loser_pool > 0:
        lines.append("\n💸 <b>Выплаты зрителям:</b>")
        for bettor_id, bet in duel["bets"].items():
            if bet["side"] == winner_side:
                share = bet["amount"] + int(bet["amount"] * (loser_pool / winner_pool))
                await change_balance(bettor_id, share)
                lines.append(f"• {bet['name']}: +{fmt(share)} {TINKY_EMOJI}")
    elif winner_pool > 0 and loser_pool == 0:
        # ставившие на победителя просто получают свои деньги назад (некого делить)
        for bettor_id, bet in duel["bets"].items():
            if bet["side"] == winner_side:
                await change_balance(bettor_id, bet["amount"])
        lines.append("\n💸 Ставок на проигравшего не было — ставки на победителя возвращены")

    await bot.send_message(chat_id, "\n".join(lines))
    del duels[chat_id]


@dp.callback_query(F.data.startswith("duel_accept:"))
async def cb_duel_accept(call: CallbackQuery):
    chat_id = int(call.data.split(":")[1])
    duel = duels.get(chat_id)

    if not duel or duel["status"] != "pending":
        return await call.answer("❌ Этот вызов больше не активен", show_alert=True)

    if call.from_user.id != duel["b"]:
        return await call.answer("❌ Этот вызов не тебе", show_alert=True)

    await ensure_user(duel["b"])
    bal_a = await get_balance(duel["a"])
    bal_b = await get_balance(duel["b"])
    amount = duel["amount"]

    if bal_a < amount:
        del duels[chat_id]
        await call.answer()
        return await call.message.edit_text(f"❌ У {duel['a_name']} недостаточно тинки — дуэль отменена")

    if bal_b < amount:
        del duels[chat_id]
        await call.answer()
        return await call.message.edit_text(f"❌ У тебя недостаточно тинки для этой ставки — дуэль отменена")

    await change_balance(duel["a"], -amount)
    await change_balance(duel["b"], -amount)

    duel["status"] = "betting"
    duel["turn"] = duel["a"]

    await call.answer()
    await call.message.edit_text(
        f"✅ Вызов принят! {duel['a_name']} 🆚 {duel['b_name']}\n"
        f"💰 Банк дуэли: <b>{fmt(amount * 2)}</b> тинки {TINKY_EMOJI}\n\n"
        f"🎲 Открыты ставки для зрителей на <b>{DUEL_BETTING_WINDOW} сек.</b>\n"
        f"Команда: <b>ставка а [сумма]</b> — за {duel['a_name']}\n"
        f"Команда: <b>ставка б [сумма]</b> — за {duel['b_name']}"
    )
    asyncio.create_task(duel_betting_timeout_task(chat_id))


@dp.callback_query(F.data.startswith("duel_decline:"))
async def cb_duel_decline(call: CallbackQuery):
    chat_id = int(call.data.split(":")[1])
    duel = duels.get(chat_id)

    if not duel or duel["status"] != "pending":
        return await call.answer("❌ Этот вызов больше не активен", show_alert=True)

    if call.from_user.id != duel["b"]:
        return await call.answer("❌ Этот вызов не тебе", show_alert=True)

    del duels[chat_id]
    await call.answer()
    await call.message.edit_text(f"❌ {duel['b_name']} отклонил вызов от {duel['a_name']}")


@dp.callback_query(F.data.startswith("reset_all_confirm:"))
async def cb_reset_all_confirm(call: CallbackQuery):
    admin_id = int(call.data.split(":")[1])
    if call.from_user.id != admin_id:
        return await call.answer("❌ Это не твоё подтверждение", show_alert=True)

    await reset_all_balances()
    await call.answer()
    await call.message.edit_text("💥 Балансы всех игроков обнулены (0 тинки у всех)")


@dp.callback_query(F.data.startswith("reset_all_cancel:"))
async def cb_reset_all_cancel(call: CallbackQuery):
    admin_id = int(call.data.split(":")[1])
    if call.from_user.id != admin_id:
        return await call.answer("❌ Это не твоё подтверждение", show_alert=True)

    await call.answer()
    await call.message.edit_text("❌ Обнуление отменено")


@dp.callback_query(F.data.startswith("reset_cd_confirm:"))
async def cb_reset_cd_confirm(call: CallbackQuery):
    admin_id = int(call.data.split(":")[1])
    if call.from_user.id != admin_id:
        return await call.answer("❌ Это не твоё подтверждение", show_alert=True)

    await reset_all_cooldowns()
    await call.answer()
    await call.message.edit_text("⏱️ Кулдаун «ворк» сброшен у всех игроков — можно снова зарабатывать")


@dp.callback_query(F.data.startswith("reset_cd_cancel:"))
async def cb_reset_cd_cancel(call: CallbackQuery):
    admin_id = int(call.data.split(":")[1])
    if call.from_user.id != admin_id:
        return await call.answer("❌ Это не твоё подтверждение", show_alert=True)

    await call.answer()
    await call.message.edit_text("❌ Сброс кулдауна отменён")


async def resolve_target_user(message: Message):
    """Определяет цель дуэли: через реплай или через @username в тексте."""
    if message.reply_to_message:
        return message.reply_to_message.from_user

    if message.entities:
        for ent in message.entities:
            if ent.type == "text_mention":
                return ent.user
            if ent.type == "mention":
                username = message.text[ent.offset + 1: ent.offset + ent.length]
                try:
                    chat = await bot.get_chat(f"@{username}")
                    return chat
                except Exception:
                    return None
    return None


# ── ТЕКСТОВЫЙ РОУТЕР ─────────────────────────────────────────────────────────

@dp.message(F.text)
async def router(message: Message):
    raw_parts = message.text.strip().split()
    text = message.text.lower().strip()
    parts = text.split()
    uid = message.from_user.id
    chat_id = message.chat.id

    # КОММАНДС
    if text == "коммандс":
        return await message.answer(commands_text())

    # СЧЁТ
    if text in ("счёт", "счет"):
        await ensure_user(uid)
        bal = await get_balance(uid)
        return await message.answer(f"💎 Твой счёт: <b>{fmt(bal)}</b> тинки {TINKY_EMOJI}")

    if (text.startswith("счёт") or text.startswith("счет")) and message.reply_to_message:
        target = message.reply_to_message.from_user
        await ensure_user(target.id)
        bal = await get_balance(target.id)
        name = display_name(target)
        return await message.answer(f"💎 Счёт {name}: <b>{fmt(bal)}</b> тинки {TINKY_EMOJI}")

    # ВОРК
    if text == "ворк":
        await ensure_user(uid)
        now = int(time.time())
        last = await get_last_earn(uid)
        left = WORK_COOLDOWN - (now - last)

        if left > 0:
            h, remainder = divmod(left, 3600)
            m, s = divmod(remainder, 60)
            time_str = f"{h}ч {m}м {s}с" if h else f"{m}м {s}с"
            return await message.answer(f"⏳ Подожди ещё <b>{time_str}</b>")

        earn = random.randint(1, 25)
        await change_balance(uid, earn)
        await update_last_earn(uid, now)
        bal = await get_balance(uid)

        return await message.answer(
            f"💼 Ты заработал <b>+{fmt(earn)}</b> тинки {TINKY_EMOJI}\n"
            f"💎 Счёт: <b>{fmt(bal)}</b> тинки"
        )

    # КАЗИНО: М КАЗИК (монетка)
    if len(parts) >= 3 and parts[0] == "м" and parts[1] == "казик":
        try:
            amount = int(parts[2])
        except ValueError:
            return await message.answer("❌ Сумма должна быть числом: м казик [сумма]")

        if amount <= 0:
            return await message.answer("❌ Сумма должна быть больше нуля")

        await ensure_user(uid)
        bal = await get_balance(uid)
        if bal < amount:
            return await message.answer("❌ Недостаточно тинки на счету")

        return await message.answer(
            f"🎰 Ставка: <b>{fmt(amount)}</b> тинки {TINKY_EMOJI}\n"
            f"Выбери сторону монеты:",
            reply_markup=coin_keyboard(uid, amount)
        )

    # КАЗИНО: С КАЗИК (слоты)
    if len(parts) >= 3 and parts[0] == "с" and parts[1] == "казик":
        try:
            amount = int(parts[2])
        except ValueError:
            return await message.answer("❌ Сумма должна быть числом: с казик [сумма]")

        if amount <= 0:
            return await message.answer("❌ Сумма должна быть больше нуля")

        await ensure_user(uid)
        bal = await get_balance(uid)
        if bal < amount:
            return await message.answer("❌ Недостаточно тинки на счету")

        # сразу списываем ставку — дальше либо возвращаем с приплатой, либо нет
        await change_balance(uid, -amount)

        spin_msg = await message.answer(f"🎰 [ ❔ | ❔ | ❔ ]\nКрутим барабаны...")

        reels = random.choices(SLOT_SYMBOLS, weights=SLOT_WEIGHTS, k=3)

        # немного "анимации" для драйва
        for frame in range(2):
            await asyncio.sleep(0.6)
            teaser = [random.choice(SLOT_SYMBOLS) for _ in range(3)]
            teaser[frame % 3] = reels[frame % 3]
            await spin_msg.edit_text(f"🎰 [ {' | '.join(teaser)} ]\nКрутим барабаны...")

        await asyncio.sleep(0.6)

        counts = {}
        for s in reels:
            counts[s] = counts.get(s, 0) + 1

        if 3 in counts.values():
            symbol = reels[0]
            multiplier = SLOT_PAYOUTS_TRIPLE[symbol]
            payout = amount * multiplier
            await change_balance(uid, payout)
            net = payout - amount
            result_line = (
                f"🎉 ТРИ ОДИНАКОВЫХ {symbol}{symbol}{symbol}! Джекпот x{multiplier}\n"
                f"💰 Выигрыш: +{fmt(net)} тинки {TINKY_EMOJI}"
            )
        elif 2 in counts.values():
            payout = int(amount * SLOT_PAYOUT_PAIR)
            await change_balance(uid, payout)
            net = payout - amount
            if net > 0:
                result_line = f"🙂 Пара совпала! Небольшой выигрыш: +{fmt(net)} тинки {TINKY_EMOJI}"
            elif net == 0:
                result_line = "😐 Пара совпала, но выигрыш вышел в ноль — ставка вернулась"
            else:
                result_line = f"😐 Пара совпала, но этого мало: {fmt(net)} тинки {TINKY_EMOJI}"
        else:
            net = -amount
            result_line = f"😢 Ничего не совпало. Проигрыш: {fmt(net)} тинки {TINKY_EMOJI}"

        new_bal = await get_balance(uid)
        await spin_msg.edit_text(
            f"🎰 [ {' | '.join(reels)} ]\n\n"
            f"{result_line}\n"
            f"💎 Счёт: <b>{fmt(new_bal)}</b>"
        )
        return

    # ДУЭЛЬ: БАТЛ [сумма] @юзер / реплай
    if parts and parts[0] == "батл":
        if chat_id in duels:
            return await message.answer("❌ В этом чате уже есть активная дуэль")

        if len(raw_parts) < 2:
            return await message.answer("❌ Формат: батл [сумма] @юзер (или реплаем на сообщение)")

        try:
            amount = int(raw_parts[1])
        except ValueError:
            return await message.answer("❌ Сумма должна быть числом")

        if amount <= 0:
            return await message.answer("❌ Сумма должна быть больше нуля")

        target = await resolve_target_user(message)
        if not target:
            return await message.answer("❌ Не могу найти игрока для дуэли — ответь на его сообщение или укажи @username")

        if target.id == uid:
            return await message.answer("❌ Нельзя вызвать самого себя")

        if getattr(target, "is_bot", False):
            return await message.answer("❌ Нельзя вызвать бота")

        await ensure_user(uid)
        await ensure_user(target.id)

        bal = await get_balance(uid)
        if bal < amount:
            return await message.answer("❌ У тебя недостаточно тинки для такой ставки")

        challenger_name = display_name(message.from_user)
        target_name = display_name(target)

        duels[chat_id] = {
            "a": uid,
            "b": target.id,
            "a_name": challenger_name,
            "b_name": target_name,
            "amount": amount,
            "status": "pending",
            "turn": None,
            "bets": {},
        }

        asyncio.create_task(duel_accept_timeout_task(chat_id))

        return await message.answer(
            f"⚔️ {challenger_name} вызывает {target_name} на дуэль!\n"
            f"💰 Ставка: <b>{fmt(amount)}</b> тинки с каждого {TINKY_EMOJI}\n"
            f"🏆 Победитель получит: <b>{fmt(amount * 2)}</b> тинки\n\n"
            f"⏳ На принятие вызова {DUEL_ACCEPT_TIMEOUT // 60} минуты",
            reply_markup=duel_accept_keyboard(chat_id)
        )

    # ДУЭЛЬ: СТАВКА (только во время фазы betting)
    if len(parts) >= 3 and parts[0] == "ставка":
        duel = duels.get(chat_id)
        if not duel or duel["status"] != "betting":
            return await message.answer("❌ Сейчас нет открытых ставок на дуэль")

        side_raw = parts[1]
        if side_raw in ("а", "a", "1"):
            side = "a"
        elif side_raw in ("б", "b", "2"):
            side = "b"
        else:
            return await message.answer("❌ Укажи сторону: ставка а [сумма] или ставка б [сумма]")

        if uid in (duel["a"], duel["b"]):
            return await message.answer("❌ Участники дуэли не могут делать ставки на неё")

        try:
            bet_amount = int(parts[2])
        except ValueError:
            return await message.answer("❌ Сумма должна быть числом")

        if bet_amount <= 0:
            return await message.answer("❌ Сумма должна быть больше нуля")

        await ensure_user(uid)
        bal = await get_balance(uid)
        if bal < bet_amount:
            return await message.answer("❌ Недостаточно тинки на счету")

        await change_balance(uid, -bet_amount)

        if uid in duel["bets"]:
            # если уже ставил — добавляем к текущей ставке (сторону не меняем)
            duel["bets"][uid]["amount"] += bet_amount
        else:
            duel["bets"][uid] = {
                "side": side,
                "amount": bet_amount,
                "name": display_name(message.from_user),
            }

        side_name = duel["a_name"] if side == "a" else duel["b_name"]
        return await message.answer(
            f"✅ Ставка принята: <b>{fmt(bet_amount)}</b> тинки за {side_name} {TINKY_EMOJI}"
        )

    # ДУЭЛЬ: СТРЕЛЯТЬ
    if text == "стрелять":
        duel = duels.get(chat_id)
        if not duel or duel["status"] != "fighting":
            return  # молча игнорируем вне контекста дуэли

        if uid != duel["turn"]:
            return await message.answer("❌ Сейчас не твой ход")

        roll = random.randint(1, 6)
        shooter_name = duel["a_name"] if uid == duel["a"] else duel["b_name"]

        if roll == 1:
            winner_id = duel["b"] if uid == duel["a"] else duel["a"]
            await message.answer(
                f"💥 БАХ! {shooter_name} выстрелил себе в висок и выбывает из дуэли..."
            )
            return await finish_duel(chat_id, winner_id)

        other_id = duel["b"] if uid == duel["a"] else duel["a"]
        other_name = duel["b_name"] if uid == duel["a"] else duel["a_name"]
        duel["turn"] = other_id

        asyncio.create_task(duel_shot_timeout_task(chat_id, other_id))

        return await message.answer(
            f"🔫 Клик! Осечка — {shooter_name} выстрелил в себя и выжил.\n"
            f"Ход переходит к {other_name} — напиши <b>стрелять</b>"
        )

    # ДАТЬ (перевод тинки другому игроку)
    if parts and parts[0] == "дать":
        if not message.reply_to_message:
            return await message.answer("❌ Ответь на сообщение игрока, которому хочешь передать тинки")

        if len(parts) < 2:
            return await message.answer("❌ Укажи сумму: дать [сумма]")

        try:
            amount = int(parts[1])
        except ValueError:
            return await message.answer("❌ Сумма должна быть числом")

        if amount <= 0:
            return await message.answer("❌ Сумма должна быть больше нуля")

        target = message.reply_to_message.from_user

        if target.id == uid:
            return await message.answer("❌ Нельзя передать тинки самому себе")

        if target.is_bot:
            return await message.answer("❌ Нельзя передать тинки боту")

        await ensure_user(uid)
        await ensure_user(target.id)

        success = await transfer_balance(uid, target.id, amount)

        if not success:
            return await message.answer("❌ Недостаточно тинки на счету")

        sender_bal = await get_balance(uid)
        target_name = display_name(target)

        return await message.answer(
            f"✅ Ты передал <b>{fmt(amount)}</b> тинки игроку {target_name} {TINKY_EMOJI}\n"
            f"💎 Твой счёт: <b>{fmt(sender_bal)}</b>"
        )

    # ТОП БОГАЧЕЙ
    if text == "топ богачей":
        async with aiosqlite.connect(DB_PATH) as db:
            cursor = await db.execute(
                "SELECT user_id, balance FROM users ORDER BY balance DESC LIMIT 50"
            )
            rows = await cursor.fetchall()

        if not rows:
            return await message.answer("❌ Нет данных")

        medals = {1: "🥇", 2: "🥈", 3: "🥉"}
        lines = [f"🏆 <b>Топ богачей:</b>\n"]

        for i, (row_uid, bal) in enumerate(rows, 1):
            try:
                user = await bot.get_chat(row_uid)
                name = display_name_notag(user)
            except Exception:
                name = f"ID {row_uid}"

            prefix = medals.get(i, f"{i}.")
            lines.append(f"{prefix} {name} — {fmt(bal)} тинки {TINKY_EMOJI}")

        return await message.answer("\n".join(lines))

    # ОБНУЛИТЬ ВСЕХ (админ, требует подтверждения)
    if text == "обнулить всех":
        if uid not in ADMIN_IDS:
            return
        return await message.answer(
            "⚠️ Ты уверен, что хочешь списать <b>все тинки у всех игроков</b> без исключения?\n"
            "Это действие необратимо.",
            reply_markup=reset_all_confirm_keyboard(uid)
        )

    # ОБНУЛИТЬ КД (админ, требует подтверждения)
    if text == "обнулить кд":
        if uid not in ADMIN_IDS:
            return
        return await message.answer(
            "⚠️ Сбросить кулдаун команды «ворк» у <b>всех игроков</b>?\n"
            "После этого все смогут сразу же снова заработать тинки.",
            reply_markup=reset_cd_confirm_keyboard(uid)
        )

    # НАЧИСЛИТЬ (админ)
    if text.startswith("начислить"):
        if uid not in ADMIN_IDS or not message.reply_to_message:
            return
        try:
            amount = int(text.split()[1])
        except (ValueError, IndexError):
            return await message.answer("❌ Укажи сумму: начислить [сумма]")

        target = message.reply_to_message.from_user
        await ensure_user(target.id)
        await change_balance(target.id, amount)
        bal = await get_balance(target.id)
        name = display_name(target)

        return await message.answer(
            f"✅ <b>{name}</b>: +{fmt(amount)} тинки {TINKY_EMOJI}\n"
            f"💎 Баланс: <b>{fmt(bal)}</b>"
        )

    # СПИСАТЬ (админ)
    if text.startswith("списать"):
        if uid not in ADMIN_IDS or not message.reply_to_message:
            return
        try:
            amount = int(text.split()[1])
        except (ValueError, IndexError):
            return await message.answer("❌ Укажи сумму: списать [сумма]")

        target = message.reply_to_message.from_user
        await ensure_user(target.id)
        bal = await get_balance(target.id)

        if bal < amount:
            return await message.answer("❌ Недостаточно тинки")

        await change_balance(target.id, -amount)
        new_bal = await get_balance(target.id)
        name = display_name(target)

        return await message.answer(
            f"💸 <b>{name}</b>: -{fmt(amount)} тинки {TINKY_EMOJI}\n"
            f"💎 Баланс: <b>{fmt(new_bal)}</b>"
        )


# ── ЗАПУСК ───────────────────────────────────────────────────────────────────

async def main():
    await create_db()
    print("BOT STARTED")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
