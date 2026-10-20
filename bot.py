"""HoopQuiz - a basketball quiz bot. Needs only a Telegram bot token."""
import json
import logging
import os
import random
import sqlite3
import unicodedata
from pathlib import Path

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Poll, Update
from telegram.constants import ChatType
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    PollAnswerHandler,
    filters,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

BASE = Path(__file__).parent
QUESTIONS = json.loads((BASE / "questions.json").read_text(encoding="utf-8"))
PLAYERS = json.loads((BASE / "players.json").read_text(encoding="utf-8"))
POINTS = {"rookie": 1, "pro": 2, "legend": 3}
# Set DB_PATH (e.g. /data/hoopquiz.db) if you attach a persistent volume
DB_PATH = Path(os.environ.get("DB_PATH", BASE / "hoopquiz.db"))


# ---------- storage ----------
def db():
    return sqlite3.connect(DB_PATH)


def init_db():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    with db() as con:
        con.execute(
            """CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY,
                name TEXT,
                points INTEGER DEFAULT 0,
                correct INTEGER DEFAULT 0,
                wrong INTEGER DEFAULT 0,
                streak INTEGER DEFAULT 0,
                best_streak INTEGER DEFAULT 0
            )"""
        )


def record_result(user_id, name, points, is_correct):
    """Save a result and return (total_points, current_streak)."""
    with db() as con:
        con.execute(
            "INSERT OR IGNORE INTO users (user_id, name) VALUES (?, ?)", (user_id, name)
        )
        if is_correct:
            con.execute(
                """UPDATE users SET name=?, points=points+?, correct=correct+1,
                   streak=streak+1, best_streak=MAX(best_streak, streak+1)
                   WHERE user_id=?""",
                (name, points, user_id),
            )
        else:
            con.execute(
                "UPDATE users SET name=?, wrong=wrong+1, streak=0 WHERE user_id=?",
                (name, user_id),
            )
        return con.execute(
            "SELECT points, streak FROM users WHERE user_id=?", (user_id,)
        ).fetchone()


# ---------- helpers ----------
def normalize(text: str) -> str:
    text = unicodedata.normalize("NFKD", text.lower())
    text = "".join(c for c in text if not unicodedata.combining(c))
    return " ".join(text.split())


def next_keyboard(data: str, label: str):
    return InlineKeyboardMarkup([[InlineKeyboardButton(label, callback_data=data)]])


# ---------- basic commands ----------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🏀 Welcome to HoopQuiz, the basketball quiz bot!\n\n"
        "/quiz - random question (or /quiz rookie|pro|legend)\n"
        "/guess - guess the player from clues\n"
        "/top - leaderboard\n"
        "/me - your stats\n"
        "/help - show this message"
    )


# ---------- quiz ----------
async def send_quiz(chat_id, context: ContextTypes.DEFAULT_TYPE, difficulty=None):
    pool = [q for q in QUESTIONS if not difficulty or q["difficulty"] == difficulty]
    q = random.choice(pool)
    options = q["options"][:]
    correct_text = options[q["answer"]]
    random.shuffle(options)
    correct_id = options.index(correct_text)

    msg = await context.bot.send_poll(
        chat_id=chat_id,
        question=f"[{q['difficulty'].title()}] {q['question']}"[:300],
        options=options,
        type=Poll.QUIZ,
        correct_option_id=correct_id,
        explanation=q.get("explanation", "")[:200] or None,
        is_anonymous=False,  # needed so we receive who answered
    )
    context.bot_data.setdefault("polls", {})[msg.poll.id] = {
        "correct": correct_id,
        "points": POINTS[q["difficulty"]],
        "chat_id": chat_id,
        "private": msg.chat.type == ChatType.PRIVATE,
    }


async def quiz(update: Update, context: ContextTypes.DEFAULT_TYPE):
    difficulty = context.args[0].lower() if context.args else None
    if difficulty and difficulty not in POINTS:
        await update.message.reply_text("Choose: rookie, pro or legend.")
        return
    await send_quiz(update.effective_chat.id, context, difficulty)


async def on_poll_answer(update: Update, context: ContextTypes.DEFAULT_TYPE):
    ans = update.poll_answer
    info = context.bot_data.get("polls", {}).get(ans.poll_id)
    if not info or not ans.option_ids:
        return
    is_correct = ans.option_ids[0] == info["correct"]
    total, streak = record_result(
        ans.user.id, ans.user.full_name, info["points"], is_correct
    )
    if info["private"]:
        text = (
            f"✅ Correct! +{info['points']} pts | Total: {total} | Streak: 🔥{streak}"
            if is_correct
            else f"❌ Wrong. Streak reset. Total: {total}"
        )
        await context.bot.send_message(
            info["chat_id"], text, reply_markup=next_keyboard("quiz_next", "Next question ➡️")
        )


# ---------- guess the player ----------
def clue_keyboard():
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("💡 Next clue", callback_data="guess_clue"),
                InlineKeyboardButton("🏳️ Give up", callback_data="guess_giveup"),
            ]
        ]
    )


async def start_guess(chat_id, context: ContextTypes.DEFAULT_TYPE):
    player = random.choice(PLAYERS)
    context.user_data["guess"] = {"player": player, "shown": 1}
    await context.bot.send_message(
        chat_id,
        f"🕵️ Who is this player? (3 pts for clue 1, 2 for clue 2, 1 for clue 3)\n\n"
        f"Clue 1: {player['clues'][0]}\n\nType your guess!",
        reply_markup=clue_keyboard(),
    )


async def guess(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await start_guess(update.effective_chat.id, context)


async def on_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    game = context.user_data.get("guess")
    if not game:
        return
    player = game["player"]
    answer = normalize(update.message.text)
    accepted = {normalize(a) for a in player["answers"]}
    user = update.effective_user
    if answer in accepted:
        points = len(player["clues"]) - game["shown"] + 1
        total, streak = record_result(user.id, user.full_name, points, True)
        context.user_data.pop("guess")
        await update.message.reply_text(
            f"✅ Yes, it's {player['name']}! +{points} pts | Total: {total} | Streak: 🔥{streak}",
            reply_markup=next_keyboard("guess_next", "Another player ➡️"),
        )
    else:
        await update.message.reply_text("❌ Not quite, try again or tap 💡 Next clue.")


# ---------- buttons ----------
async def on_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    chat_id = query.message.chat_id
    data = query.data
    user = query.from_user

    if data == "quiz_next":
        await send_quiz(chat_id, context)
    elif data == "guess_next":
        await start_guess(chat_id, context)
    elif data in ("guess_clue", "guess_giveup"):
        game = context.user_data.get("guess")
        if not game:
            await query.message.reply_text("No active game. Use /guess to start one.")
            return
        player = game["player"]
        if data == "guess_giveup" or game["shown"] >= len(player["clues"]):
            context.user_data.pop("guess")
            record_result(user.id, user.full_name, 0, False)
            await query.message.reply_text(
                f"It was {player['name']}! Streak reset.",
                reply_markup=next_keyboard("guess_next", "Another player ➡️"),
            )
        else:
            game["shown"] += 1
            n = game["shown"]
            await query.message.reply_text(
                f"Clue {n}: {player['clues'][n - 1]}", reply_markup=clue_keyboard()
            )


# ---------- stats ----------
async def top(update: Update, context: ContextTypes.DEFAULT_TYPE):
    with db() as con:
        rows = con.execute(
            "SELECT name, points, best_streak FROM users ORDER BY points DESC LIMIT 10"
        ).fetchall()
    if not rows:
        await update.message.reply_text("No scores yet. Try /quiz!")
        return
    medals = ["🥇", "🥈", "🥉"]
    lines = [
        f"{medals[i] if i < 3 else f'{i + 1}.'} {name} - {pts} pts (best streak {bs})"
        for i, (name, pts, bs) in enumerate(rows)
    ]
    await update.message.reply_text("🏆 Leaderboard\n\n" + "\n".join(lines))


async def me(update: Update, context: ContextTypes.DEFAULT_TYPE):
    with db() as con:
        row = con.execute(
            "SELECT points, correct, wrong, streak, best_streak FROM users WHERE user_id=?",
            (update.effective_user.id,),
        ).fetchone()
    if not row:
        await update.message.reply_text("You have no stats yet. Try /quiz!")
        return
    pts, correct, wrong, streak, best = row
    await update.message.reply_text(
        f"📊 {update.effective_user.first_name}\n"
        f"Points: {pts}\nCorrect: {correct} | Wrong: {wrong}\n"
        f"Streak: {streak} | Best: {best}"
    )


def main():
    token = os.environ.get("BOT_TOKEN")
    if not token:
        raise SystemExit("Set the BOT_TOKEN environment variable first.")
    init_db()
    app = Application.builder().token(token).build()
    app.add_handler(CommandHandler(["start", "help"], start))
    app.add_handler(CommandHandler("quiz", quiz))
    app.add_handler(CommandHandler("guess", guess))
    app.add_handler(CommandHandler("top", top))
    app.add_handler(CommandHandler("me", me))
    app.add_handler(PollAnswerHandler(on_poll_answer))
    app.add_handler(CallbackQueryHandler(on_button))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))
    logging.info("HoopQuiz is running...")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
