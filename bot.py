"""
bot.py — Bovonto Inventory Telegram Bot
Salesman weekly closing stock entry via Telegram conversation.

Optimized flow:
  /start → salesman → week → ONE fetch for all month+week rows (cached)
  → distributor 1 → filter from cache → enter closing
  → next distributor → complete
"""

import os
import logging
from dotenv import load_dotenv

from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    ReplyKeyboardMarkup,
    KeyboardButton,
)
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    MessageHandler,
    ContextTypes,
    ConversationHandler,
    filters,
)

import sheets

load_dotenv()

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")

# ──────────────────────────────────────────────
#  Conversation states
# ──────────────────────────────────────────────
(
    SELECT_SALESMAN,
    SELECT_WEEK,
    SHOW_DISTRIBUTOR,
    ENTER_CLOSING,
    CONFIRM_SUBMIT,
) = range(5)

WEEKS = ["1st Week", "2nd Week", "3rd Week", "4th Week"]

START_KEYBOARD = ReplyKeyboardMarkup(
    [[KeyboardButton("🚀 Start")]],
    resize_keyboard=True,
)


def make_keyboard(options: list[str], cols: int = 2) -> InlineKeyboardMarkup:
    buttons = [InlineKeyboardButton(o, callback_data=o) for o in options]
    rows    = [buttons[i : i + cols] for i in range(0, len(buttons), cols)]
    return InlineKeyboardMarkup(rows)


# ──────────────────────────────────────────────
#  /start
# ──────────────────────────────────────────────

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data.clear()
    await update.message.reply_text("⏳ Loading salesman list...")
    try:
        salesmen = sheets.get_salesmen()
    except Exception as e:
        logger.error("Failed to load salesmen: %s", e)
        await update.message.reply_text(
            "❌ Could not connect to Google Sheets. Try again later.",
            reply_markup=START_KEYBOARD,
        )
        return ConversationHandler.END

    if not salesmen:
        await update.message.reply_text(
            "❌ No salesmen found in the sheet.", reply_markup=START_KEYBOARD
        )
        return ConversationHandler.END

    await update.message.reply_text(
        "👋 Welcome to *Bovonto Inventory Bot*\n\nSelect your name:",
        parse_mode="Markdown",
        reply_markup=make_keyboard(salesmen, cols=2),
    )
    return SELECT_SALESMAN


# ──────────────────────────────────────────────
#  Salesman selected
# ──────────────────────────────────────────────

async def salesman_selected(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()

    salesman = query.data
    month    = sheets.get_current_month()
    context.user_data["salesman"] = salesman
    context.user_data["month"]    = month

    await query.edit_message_text(
        f"✅ Salesman: *{salesman}*\n📅 Month: *{month}*\n\nSelect the week:",
        parse_mode="Markdown",
        reply_markup=make_keyboard(WEEKS, cols=2),
    )
    return SELECT_WEEK


# ──────────────────────────────────────────────
#  Week selected — ONE fetch, cache all rows
# ──────────────────────────────────────────────

async def week_selected(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()

    week     = query.data
    salesman = context.user_data["salesman"]
    month    = context.user_data["month"]
    context.user_data["week"] = week

    await query.edit_message_text(
        f"⏳ Loading data for *{week}* — *{month}*...\n_(this may take a moment)_",
        parse_mode="Markdown",
    )

    # ── Single fetch for ALL rows of this month+week ──────────────
    try:
        all_rows = sheets.fetch_month_week_rows(month, week)
    except Exception as e:
        logger.error("Failed to fetch master rows: %s", e)
        await query.edit_message_text("❌ Could not load data. Please try again.")
        return ConversationHandler.END

    if not all_rows:
        await query.edit_message_text(
            f"❌ No data found in Master sheet for *{week}* — *{month}*.\n"
            f"Run *Build Master Sheet* in Google Sheets first.",
            parse_mode="Markdown",
        )
        return ConversationHandler.END

    # ── Load distributors ─────────────────────────────────────────
    try:
        distributors = sheets.get_distributors_for_salesman(salesman)
    except Exception as e:
        logger.error("Failed to load distributors: %s", e)
        await query.edit_message_text("❌ Could not load distributors.")
        return ConversationHandler.END

    if not distributors:
        await query.edit_message_text(
            f"❌ No distributors assigned to *{salesman}*.", parse_mode="Markdown"
        )
        return ConversationHandler.END

    # Cache everything — no more API calls until final submit
    context.user_data["cached_rows"]    = all_rows        # ← cached here
    context.user_data["distributors"]   = distributors
    context.user_data["dist_index"]     = 0
    context.user_data["pending_updates"] = []
    context.user_data["product_index"]  = 0

    return await show_current_distributor(query, context)


# ──────────────────────────────────────────────
#  Show distributor (filter from cache — no API)
# ──────────────────────────────────────────────

async def show_current_distributor(query_or_msg, context: ContextTypes.DEFAULT_TYPE) -> int:
    data         = context.user_data
    distributors = data["distributors"]
    dist_index   = data["dist_index"]

    if dist_index >= len(distributors):
        return await finalize(query_or_msg, context)

    distributor = distributors[dist_index]
    data["current_distributor"] = distributor

    # Filter from cache — zero API calls
    rows = sheets.filter_rows_for_distributor(
        data["cached_rows"],
        data["salesman"],
        distributor,
    )

    if not rows:
        text = (
            f"⚠️ No products in Master for *{distributor}* — *{data['week']}*.\n"
            f"Skipping..."
        )
        if hasattr(query_or_msg, "edit_message_text"):
            await query_or_msg.edit_message_text(text, parse_mode="Markdown")
        else:
            await query_or_msg.reply_text(text, parse_mode="Markdown")

        data["dist_index"] += 1
        return await show_current_distributor(query_or_msg, context)

    data["current_products"] = rows
    data["product_index"]    = 0

    total  = len(distributors)
    header = (
        f"📦 *Distributor {dist_index + 1}/{total}:* {distributor}\n"
        f"📅 {data['week']} — {data['month']}\n"
        f"{'─' * 30}\n"
        f"Enter closing stock for each product.\n"
    )
    if hasattr(query_or_msg, "edit_message_text"):
        await query_or_msg.edit_message_text(header, parse_mode="Markdown")
    else:
        await query_or_msg.reply_text(header, parse_mode="Markdown")

    return await ask_next_product(query_or_msg, context)


# ──────────────────────────────────────────────
#  Ask product one by one
# ──────────────────────────────────────────────

async def ask_next_product(query_or_msg, context: ContextTypes.DEFAULT_TYPE) -> int:
    data  = context.user_data
    rows  = data["current_products"]
    p_idx = data["product_index"]

    if p_idx >= len(rows):
        return await next_distributor(query_or_msg, context)

    row      = rows[p_idx]
    existing = f"  (current: {row['closing_stock']})" if row["closing_stock"] else ""
    text = (
        f"🛒 *{row['product']}*"
        + (f" _{row['category']}_" if row["category"] else "")
        + f"\n  Opening: *{row['opening_stock'] or '0'}*"
        + f"\n  Receipt: *{row['receipt'] or '0'}*"
        + existing
        + f"\n\n➡️ Enter closing stock:"
    )
    skip_keyboard = InlineKeyboardMarkup([[
        InlineKeyboardButton("⏭️ Skip", callback_data="skip_product")
    ]])

    if hasattr(query_or_msg, "edit_message_text"):
        await query_or_msg.edit_message_text(text, parse_mode="Markdown", reply_markup=skip_keyboard)
    else:
        await query_or_msg.reply_text(text, parse_mode="Markdown", reply_markup=skip_keyboard)

    return ENTER_CLOSING


# ──────────────────────────────────────────────
#  Receive closing stock input
# ──────────────────────────────────────────────

async def closing_entered(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    text = update.message.text.strip()
    try:
        value = float(text)
        if value < 0:
            raise ValueError
    except ValueError:
        await update.message.reply_text("⚠️ Enter a valid number (e.g. 24 or 12.5):")
        return ENTER_CLOSING

    data  = context.user_data
    row   = data["current_products"][data["product_index"]]
    disp  = int(value) if value == int(value) else value

    data["pending_updates"].append({
        "row_index":     row["row_index"],
        "closing_stock": str(disp),
        "product":       row["product"],
        "distributor":   row["distributor"],
    })

    await update.message.reply_text(
        f"✅ *{row['product']}* → Closing: *{disp}*", parse_mode="Markdown"
    )

    data["product_index"] += 1
    return await ask_next_product(update.message, context)


async def skip_product_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    data = context.user_data
    row  = data["current_products"][data["product_index"]]
    data["product_index"] += 1
    # Acknowledge skip then move to next product (edit current message)
    return await ask_next_product(query, context)


# ──────────────────────────────────────────────
#  Next distributor
# ──────────────────────────────────────────────

async def next_distributor(query_or_msg, context: ContextTypes.DEFAULT_TYPE) -> int:
    data = context.user_data
    data["dist_index"] += 1
    distributors = data["distributors"]
    dist_index   = data["dist_index"]

    if dist_index >= len(distributors):
        return await finalize(query_or_msg, context)

    next_dist = distributors[dist_index]
    keyboard  = InlineKeyboardMarkup([
        [InlineKeyboardButton(f"➡️ Continue → {next_dist}", callback_data="next_dist")],
        [InlineKeyboardButton("✅ Submit & Stop here", callback_data="submit_stop")],
    ])
    text = f"✅ Done with this distributor!\n\nWhat next?"

    if hasattr(query_or_msg, "edit_message_text"):
        await query_or_msg.edit_message_text(text, parse_mode="Markdown", reply_markup=keyboard)
    else:
        await query_or_msg.reply_text(text, parse_mode="Markdown", reply_markup=keyboard)

    return SHOW_DISTRIBUTOR


async def continue_to_next_distributor(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    return await show_current_distributor(query, context)


async def submit_stop(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    return await finalize(query, context)


# ──────────────────────────────────────────────
#  Finalize & confirm
# ──────────────────────────────────────────────

async def finalize(query_or_msg, context: ContextTypes.DEFAULT_TYPE) -> int:
    data    = context.user_data
    updates = data.get("pending_updates", [])

    if not updates:
        text = "⚠️ No values entered. Nothing to save."
        if hasattr(query_or_msg, "edit_message_text"):
            await query_or_msg.edit_message_text(text)
        else:
            await query_or_msg.reply_text(text)
        return ConversationHandler.END

    lines = [
        f"📋 *Summary — {data['week']} | {data['month']}*",
        f"👤 *{data['salesman']}*\n",
    ]
    current_dist = None
    for u in updates:
        if u["distributor"] != current_dist:
            current_dist = u["distributor"]
            lines.append(f"🏪 *{current_dist}*")
        lines.append(f"  • {u['product']}: {u['closing_stock']}")

    lines.append(f"\n*{len(updates)} product(s) to update.*")
    lines.append("Confirm to save to Google Sheets?")

    keyboard = InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ Confirm & Submit", callback_data="submit"),
        InlineKeyboardButton("❌ Cancel",           callback_data="cancel"),
    ]])

    if hasattr(query_or_msg, "edit_message_text"):
        await query_or_msg.edit_message_text(
            "\n".join(lines), parse_mode="Markdown", reply_markup=keyboard
        )
    else:
        await query_or_msg.reply_text(
            "\n".join(lines), parse_mode="Markdown", reply_markup=keyboard
        )

    return CONFIRM_SUBMIT


async def confirm_submit(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()

    if query.data == "cancel":
        await query.edit_message_text("❌ Cancelled. Use /start to begin again.")
        return ConversationHandler.END

    await query.edit_message_text("⏳ Saving to Google Sheets...")

    data    = context.user_data
    updates = data.get("pending_updates", [])

    try:
        sheets.batch_write_closing_stocks(updates)
    except Exception as e:
        logger.error("Write failed: %s", e)
        await query.edit_message_text(
            f"❌ Error saving:\n`{e}`\n\nContact admin.", parse_mode="Markdown"
        )
        return ConversationHandler.END

    await query.edit_message_text(
        f"🎉 *Done!* {len(updates)} value(s) saved.\n\n"
        f"Week: *{data['week']}* | Month: *{data['month']}*\n"
        f"Salesman: *{data['salesman']}*\n\n"
        f"Tap below for next entry.",
        parse_mode="Markdown",
    )
    await query.message.reply_text("👇", reply_markup=START_KEYBOARD)
    context.user_data.clear()
    return ConversationHandler.END


# ──────────────────────────────────────────────
#  Fallbacks
# ──────────────────────────────────────────────

async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data.clear()
    await update.message.reply_text("❌ Cancelled.", reply_markup=START_KEYBOARD)
    return ConversationHandler.END


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    logger.error("Unhandled error: %s", context.error, exc_info=context.error)


# ──────────────────────────────────────────────
#  Main
# ──────────────────────────────────────────────

def main() -> None:
    if not BOT_TOKEN:
        raise ValueError("TELEGRAM_BOT_TOKEN is not set in .env")

    app = Application.builder().token(BOT_TOKEN).build()

    conv = ConversationHandler(
        entry_points=[
            CommandHandler("start", start),
            MessageHandler(filters.Regex("^🚀 Start$"), start),
        ],
        states={
            SELECT_SALESMAN:  [CallbackQueryHandler(salesman_selected)],
            SELECT_WEEK:      [CallbackQueryHandler(week_selected)],
            SHOW_DISTRIBUTOR: [
                CallbackQueryHandler(continue_to_next_distributor, pattern="^next_dist$"),
                CallbackQueryHandler(submit_stop, pattern="^submit_stop$"),
            ],
            ENTER_CLOSING: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, closing_entered),
                CallbackQueryHandler(skip_product_callback, pattern="^skip_product$"),
            ],
            CONFIRM_SUBMIT: [CallbackQueryHandler(confirm_submit, pattern="^(submit|cancel)$")],
        },
        fallbacks=[
            CommandHandler("cancel", cancel),
            CommandHandler("start", start),
        ],
        allow_reentry=True,
    )

    app.add_handler(conv)
    app.add_error_handler(error_handler)

    logger.info("🚀 Bovonto Inventory Bot started.")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()