import logging
import json
import uvicorn
import os
from contextlib import asynccontextmanager
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, WebAppInfo
from telegram.ext import (
    Application, CommandHandler, ContextTypes, ConversationHandler,
    MessageHandler, filters, CallbackQueryHandler
)
from sqlalchemy import create_engine, Column, Integer, BigInteger, String, Float, ForeignKey
from sqlalchemy.orm import declarative_base, sessionmaker

# --- Logging Setup ---
logging.basicConfig(format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO)
logger = logging.getLogger(__name__)

# --- Configuration ---
BOT_TOKEN = "8486136204:AAFZkkxVFlBK1S5_RzrOlZ4ZZ6cDBcBjqVY"
BOT_USERNAME = "GTaskPHBot"
ADMIN_CHAT_ID = 7331257920
MINI_APP_URL = "https://gtask-fronted.vercel.app" # IMPORTANT: We will replace this in the final step
INVITE_REWARD = 10.0
MIN_WITHDRAWAL = 500.0

# --- Database Setup ---
Base = declarative_base()
class User(Base): __tablename__ = "users"; id = Column(BigInteger, primary_key=True, autoincrement=False); balance = Column(Float, default=0.0); referral_count = Column(Integer, default=0); referrer_id = Column(BigInteger, ForeignKey("users.id"), nullable=True)
class Task(Base): __tablename__ = "tasks"; id = Column(Integer, primary_key=True); description = Column(String); link = Column(String); reward = Column(Float); photo_file_id = Column(String, nullable=True)
class Withdrawal(Base): __tablename__ = "withdrawals"; id = Column(Integer, primary_key=True); user_id = Column(BigInteger); amount = Column(Float); method = Column(String); details = Column(String); status = Column(String, default="pending")
engine = create_engine("sqlite:///referrals.db"); Base.metadata.create_all(engine); Session = sessionmaker(bind=engine); db_session = Session()

# --- Conversation States ---
TASK_DESC, TASK_LINK, TASK_REWARD, TASK_PHOTO = range(4)
REJECT_REASON = range(4, 5)

# --- Bot & API Lifespan Management ---
ptb_app = Application.builder().token(BOT_TOKEN).build()

@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("Lifespan startup...")
    await ptb_app.initialize()
    await ptb_app.updater.start_polling()
    await ptb_app.start()
    logger.info("Telegram bot has started successfully.")
    yield
    logger.info("Lifespan shutdown..."); await ptb_app.updater.stop(); await ptb_app.stop(); await ptb_app.shutdown()

app = FastAPI(lifespan=lifespan)
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_credentials=True, allow_methods=["*"], allow_headers=["*"])

# --- API Endpoints ---
@app.get("/")
async def health_check(): return {"status": "ok", "bot": BOT_USERNAME}
@app.post("/get_initial_data")
async def get_initial_data(request: Request):
    try:
        data = await request.json(); user_id = data.get('user_id')
        if not user_id: return {"error": "user_id not provided"}
        user = db_session.query(User).filter(User.id == user_id).first()
        tasks = db_session.query(Task).all()
        withdrawals = db_session.query(Withdrawal).filter(Withdrawal.user_id == user_id).order_by(Withdrawal.id.desc()).all()
        return {"balance": user.balance if user else 0.0, "tasks": [{"id": t.id, "description": t.description, "link": t.link, "reward": t.reward} for t in tasks], "withdrawals": [{"id": w.id, "amount": w.amount, "method": w.method, "status": w.status} for w in withdrawals]}
    except Exception as e: logger.error(f"Error in get_initial_data: {e}"); return {"error": "Internal server error"}

@app.post("/submit_withdrawal")
async def submit_withdrawal(request: Request):
    try:
        data = await request.json(); user_id = data.get('user_id'); amount = float(data.get('amount')); method = data.get('method'); details = data.get('details')
        user = db_session.query(User).filter(User.id == user_id).first()
        if not user or user.balance < amount or amount < MIN_WITHDRAWAL:
            await ptb_app.bot.send_message(user_id, "⚠️ Withdrawal request failed. Your balance may have changed or the amount was invalid."); return {"status": "error", "message": "Validation failed"}
        new_withdrawal = Withdrawal(user_id=user.id, amount=amount, method=method, details=details)
        db_session.add(new_withdrawal); user.balance -= amount; db_session.commit()
        await ptb_app.bot.send_message(user_id, "✅ Your withdrawal request has been submitted! Our team will review it shortly.")
        admin_message = f"**New Withdrawal Request**\n\n- User ID: `{user.id}`\n- Amount: `₱{amount:.2f}`\n- Method: `{method}`\n- Details: `{details}`"
        keyboard = [[InlineKeyboardButton("Approve ✅", callback_data=f"approve_{new_withdrawal.id}"), InlineKeyboardButton("Reject ❌", callback_data=f"reject_start_{new_withdrawal.id}")]]
        await ptb_app.bot.send_message(chat_id=ADMIN_CHAT_ID, text=admin_message, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode='Markdown')
        return {"status": "success"}
    except Exception as e: logger.error(f"Error in submit_withdrawal: {e}"); return {"error": "Internal server error"}

# --- Telegram Bot Handlers ---
async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if context.args:
        try:
            referrer_id = int(context.args[0])
            if referrer_id != user.id and not db_session.query(User).filter(User.id == user.id).first():
                db_session.add(User(id=user.id, referrer_id=referrer_id))
                referrer = db_session.query(User).filter(User.id == referrer_id).first()
                if referrer: referrer.referral_count += 1; referrer.balance += INVITE_REWARD; await context.bot.send_message(chat_id=referrer_id, text=f"🎉 A new user, {user.first_name}, joined using your link! You've earned ₱{INVITE_REWARD:.2f}.")
                db_session.commit()
        except (ValueError, IndexError): pass
    if not db_session.query(User).filter(User.id == user.id).first(): db_session.add(User(id=user.id)); db_session.commit()
    
    caption = (
        f"🚀 **Welcome to {BOT_USERNAME}, {user.first_name}!**\n\n"
        "Your journey to earning real rewards starts now. Complete tasks, invite friends, and manage everything from our seamless dashboard.\n\n"
        "Click the button below to launch the app and begin!"
    )
    keyboard = [[InlineKeyboardButton("📱 Open Dashboard", web_app=WebAppInfo(url=MINI_APP_URL))]]
    await update.message.reply_text(caption, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode='Markdown')

# --- Admin Panel Handlers ---
async def admin_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_CHAT_ID: return
    keyboard = [[InlineKeyboardButton("➕ Add Task", callback_data="add_task_start")], [InlineKeyboardButton("🗑️ Remove Task", callback_data="remove_task_list")]]
    await update.message.reply_text("👑 **Admin Dashboard**", reply_markup=InlineKeyboardMarkup(keyboard), parse_mode='Markdown')
async def add_task_start(update: Update, context: ContextTypes.DEFAULT_TYPE): await update.callback_query.answer(); await update.callback_query.message.reply_text("Enter task description:"); return TASK_DESC
async def get_task_description(update: Update, context: ContextTypes.DEFAULT_TYPE): context.user_data['task_desc'] = update.message.text; await update.message.reply_text("Send the link:"); return TASK_LINK
async def get_task_link(update: Update, context: ContextTypes.DEFAULT_TYPE): context.user_data['task_link'] = update.message.text; await update.message.reply_text("Enter the reward amount:"); return TASK_REWARD
async def get_task_reward(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try: context.user_data['task_reward'] = float(update.message.text); await update.message.reply_text("Send a photo, or type 'skip'."); return TASK_PHOTO
    except ValueError: await update.message.reply_text("Invalid amount."); return TASK_REWARD
async def get_task_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    photo_file_id = update.message.photo[-1].file_id if update.message.photo else None
    if not photo_file_id and update.message.text.lower() != 'skip': await update.message.reply_text("Invalid input."); return TASK_PHOTO
    db_session.add(Task(description=context.user_data['task_desc'], link=context.user_data['task_link'], reward=context.user_data['task_reward'], photo_file_id=photo_file_id)); db_session.commit()
    await update.message.reply_text("✅ Task successfully added!"); return ConversationHandler.END
async def remove_task_list(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.callback_query.answer(); tasks = db_session.query(Task).all()
    if not tasks: await update.callback_query.message.edit_text("There are no tasks to remove."); return
    keyboard = [[InlineKeyboardButton(f"❌ {task.description[:30]}...", callback_data=f"delete_task_{task.id}")] for task in tasks]
    await update.callback_query.message.edit_text("Select a task to remove:", reply_markup=InlineKeyboardMarkup(keyboard))
async def delete_task(update: Update, context: ContextTypes.DEFAULT_TYPE):
    task_id = int(update.callback_query.data.split("_")[2]); task = db_session.query(Task).filter(Task.id == task_id).first()
    if task: db_session.delete(task); db_session.commit(); await update.callback_query.answer("Task removed!", show_alert=True); await remove_task_list(update, context)
    else: await update.callback_query.answer("Task not found.", show_alert=True)
async def approve_withdrawal(update: Update, context: ContextTypes.DEFAULT_TYPE):
    withdrawal_id = int(update.callback_query.data.split("_")[1]); withdrawal = db_session.query(Withdrawal).filter(Withdrawal.id == withdrawal_id).first()
    if not withdrawal or withdrawal.status != "pending": await update.callback_query.edit_message_text("Request already processed."); return
    withdrawal.status = "approved"; db_session.commit(); await update.callback_query.edit_message_text(f"✅ Request #{withdrawal_id} approved.")
    await ptb_app.bot.send_message(chat_id=withdrawal.user_id, text=f"🎉 Good news! Your withdrawal of ₱{withdrawal.amount:.2f} has been approved and sent.")
async def reject_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data['withdrawal_id_to_reject'] = int(update.callback_query.data.split("_")[2])
    await update.callback_query.answer(); await update.callback_query.message.reply_text("Please provide a brief reason for rejecting this withdrawal (or send /skip)."); return REJECT_REASON
async def get_rejection_reason(update: Update, context: ContextTypes.DEFAULT_TYPE):
    withdrawal_id = context.user_data['withdrawal_id_to_reject']; withdrawal = db_session.query(Withdrawal).filter(Withdrawal.id == withdrawal_id).first()
    if not withdrawal or withdrawal.status != "pending": await update.message.reply_text("Request already processed."); return ConversationHandler.END
    reason = "No reason provided." if update.message.text.lower() == '/skip' else update.message.text
    user = db_session.query(User).filter(User.id == withdrawal.user_id).first(); user.balance += withdrawal.amount; withdrawal.status = "rejected"; db_session.commit()
    await update.message.reply_text(f"❌ Request #{withdrawal_id} has been rejected.")
    await ptb_app.bot.send_message(chat_id=withdrawal.user_id, text=f"⚠️ Your withdrawal of ₱{withdrawal.amount:.2f} was rejected.\n\n**Admin's Remark:** {reason}", parse_mode='Markdown')
    return ConversationHandler.END

ptb_app.add_handler(CommandHandler("start", start_command))
ptb_app.add_handler(CommandHandler("admin", admin_command))
add_task_conv = ConversationHandler(entry_points=[CallbackQueryHandler(add_task_start, pattern="^add_task_start$")], states={TASK_DESC: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_task_description)], TASK_LINK: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_task_link)], TASK_REWARD: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_task_reward)], TASK_PHOTO: [MessageHandler(filters.PHOTO | filters.TEXT, get_task_photo)]}, fallbacks=[])
reject_conv = ConversationHandler(entry_points=[CallbackQueryHandler(reject_start, pattern=r"^reject_start_\d+$")], states={REJECT_REASON: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_rejection_reason)]}, fallbacks=[])
ptb_app.add_handler(add_task_conv); ptb_app.add_handler(reject_conv)
ptb_app.add_handler(CallbackQueryHandler(remove_task_list, pattern="^remove_task_list$")); ptb_app.add_handler(CallbackQueryHandler(delete_task, pattern=r"^delete_task_\d+$")); ptb_app.add_handler(CallbackQueryHandler(approve_withdrawal, pattern=r"^approve_\d+$"))

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8000))
    uvicorn.run("main:app", host="0.0.0.0", port=port, reload=True)
