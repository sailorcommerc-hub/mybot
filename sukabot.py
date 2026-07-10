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

bot = Bot(token=TOKEN, default=DefaultBotProperties(parse_mode="HTML"))
dp = Dispatcher()


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


# ── КЛАВИАТУРЫ ───────────────────────────────────────────────────────────────

def welcome_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📌 Команды", callback_data="show_commands")],
        [InlineKeyboardButton(text=f"💼 Заработать", callback_data="do_work")],
        [InlineKeyboardButton(text=f"💎 Мой счёт", callback_data="my_balance")],
    ])


def commands_text() -> str:
    return (
        f"📌 <b>Команды бота:</b>\n\n"
        f"• <b>счёт</b> — посмотреть свой баланс\n"
        f"• <b>счёт</b> (реплай) — баланс другого игрока\n"
        f"• <b>ворк</b> — заработать тинки (КД 2 часа)\n"
        f"• <b>передать [сумма]</b> (реплай) — перевести тинки другому игроку\n"
        f"• <b>топ богачей</b> — рейтинг игроков\n"
        f"• <b>коммандс</b> — список команд\n\n"
        f"<i>Админ-команды (реплай):</i>\n"
        f"• <b>начислить [сумма]</b>\n"
        f"• <b>списать [сумма]</b>"
    )


# ── ХЭНДЛЕРЫ ─────────────────────────────────────────────────────────────────

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
    await call.message.answer(f"💎 Твой счёт: <b>{bal}</b> тинки {TINKY_EMOJI}")


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
        f"💼 Ты заработал <b>+{earn}</b> тинки {TINKY_EMOJI}\n"
        f"💎 Счёт: <b>{bal}</b> тинки"
    )


# ── ТЕКСТОВЫЙ РОУТЕР ─────────────────────────────────────────────────────────

@dp.message(F.text)
async def router(message: Message):
    text = message.text.lower().strip()
    uid = message.from_user.id

    # КОММАНДС
    if text == "коммандс":
        return await message.answer(commands_text())

    # СЧЁТ
    if text in ("счёт", "счет"):
        await ensure_user(uid)
        bal = await get_balance(uid)
        return await message.answer(f"💎 Твой счёт: <b>{bal}</b> тинки {TINKY_EMOJI}")

    if (text.startswith("счёт") or text.startswith("счет")) and message.reply_to_message:
        target = message.reply_to_message.from_user
        await ensure_user(target.id)
        bal = await get_balance(target.id)
        name = f"@{target.username}" if target.username else target.first_name
        return await message.answer(f"💎 Счёт {name}: <b>{bal}</b> тинки {TINKY_EMOJI}")

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
            f"💼 Ты заработал <b>+{earn}</b> тинки {TINKY_EMOJI}\n"
            f"💎 Счёт: <b>{bal}</b> тинки"
        )

    # ПЕРЕДАТЬ (перевод тинки другому игроку)
    if text.startswith("передать"):
        if not message.reply_to_message:
            return await message.answer("❌ Ответь на сообщение игрока, которому хочешь передать тинки")

        parts = text.split()
        if len(parts) < 2:
            return await message.answer("❌ Укажи сумму: передать [сумма]")

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
        target_name = f"@{target.username}" if target.username else target.first_name

        return await message.answer(
            f"✅ Ты передал <b>{amount}</b> тинки игроку {target_name} {TINKY_EMOJI}\n"
            f"💎 Твой счёт: <b>{sender_bal}</b>"
        )

    # ТОП БОГАЧЕЙ
    if text == "топ богачей":
        async with aiosqlite.connect(DB_PATH) as db:
            cursor = await db.execute(
                "SELECT user_id, balance FROM users ORDER BY balance DESC LIMIT 10"
            )
            rows = await cursor.fetchall()

        if not rows:
            return await message.answer("❌ Нет данных")

        medals = {1: "🥇", 2: "🥈", 3: "🥉"}
        lines = [f"🏆 <b>Топ богачей:</b>\n"]

        for i, (row_uid, bal) in enumerate(rows, 1):
            try:
                user = await bot.get_chat(row_uid)
                name = f"@{user.username}" if user.username else user.first_name
            except Exception:
                name = f"ID {row_uid}"

            prefix = medals.get(i, f"{i}.")
            lines.append(f"{prefix} {name} — {bal} тинки {TINKY_EMOJI}")

        return await message.answer("\n".join(lines))

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
        name = f"@{target.username}" if target.username else target.first_name

        return await message.answer(
            f"✅ <b>{name}</b>: +{amount} тинки {TINKY_EMOJI}\n"
            f"💎 Баланс: <b>{bal}</b>"
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
        name = f"@{target.username}" if target.username else target.first_name

        return await message.answer(
            f"💸 <b>{name}</b>: -{amount} тинки {TINKY_EMOJI}\n"
            f"💎 Баланс: <b>{new_bal}</b>"
        )


# ── ЗАПУСК ───────────────────────────────────────────────────────────────────

async def main():
    await create_db()
    print("BOT STARTED")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())