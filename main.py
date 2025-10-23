import logging
import json
import uvicorn
import os
import base64
from io import BytesIO
from contextlib import asynccontextmanager
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, WebAppInfo, InputFile
from telegram.ext import (
    Application, CommandHandler, ContextTypes, ConversationHandler,
    MessageHandler, filters, CallbackQueryHandler
)
from sqlalchemy import create_engine, Column, Integer, BigInteger, String, Float, ForeignKey, Text
from sqlalchemy.orm import declarative_base, sessionmaker

# --- Logging Setup ---
logging.basicConfig(format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO)
logger = logging.getLogger(__name__)

# --- Configuration ---
BOT_TOKEN = "8486136204:AAFZkkxVFlBK1S5_RzrOlZ4ZZ6cDBcBjqVY"
BOT_USERNAME = "GTaskPHBot"
ADMIN_CHAT_ID = 7331257920
MINI_APP_URL = "https://gtask-fronted.vercel.app" # IMPORTANT: Make sure this is your Vercel URL
INVITE_REWARD = 77.0
MIN_WITHDRAWAL = 500.0

# --- Database Setup ---
Base = declarative_base()
class User(Base): __tablename__ = "users"; id = Column(BigInteger, primary_key=True, autoincrement=False); balance = Column(Float, default=0.0); referral_count = Column(Integer, default=0); successful_referrals = Column(Integer, default=0); tasks_completed = Column(Integer, default=0); referrer_id = Column(BigInteger, ForeignKey("users.id"), nullable=True)
class Task(Base): __tablename__ = "tasks"; id = Column(Integer, primary_key=True); description = Column(String); link = Column(String); reward = Column(Float)
class TaskSubmission(Base): __tablename__ = "task_submissions"; id = Column(Integer, primary_key=True); user_id = Column(BigInteger); task_id = Column(Integer); text_proof = Column(Text); photo_proof_base64 = Column(Text); status = Column(String, default="pending")
class Withdrawal(Base): __tablename__ = "withdrawals"; id = Column(Integer, primary_key=True); user_id = Column(BigInteger); amount = Column(Float); method = Column(String); details = Column(String); status = Column(String, default="pending")
class RedeemCode(Base): __tablename__ = "redeem_codes"; id = Column(Integer, primary_key=True); code = Column(String, unique=True); reward = Column(Float); uses_left = Column(Integer, default=1)
class SystemInfo(Base): __tablename__ = "system_info"; key = Column(String, primary_key=True); value = Column(String)
engine = create_engine("sqlite:///referrals.db"); Base.metadata.create_all(engine); Session = sessionmaker(bind=engine); db_session = Session()

# --- Conversation States ---
TASK_DESC, TASK_LINK, TASK_REWARD, \
REJECT_REASON, BROADCAST_MESSAGE, ANNOUNCEMENT_TEXT, \
NEW_CODE, NEW_CODE_REWARD, NEW_CODE_USES, \
SUBMIT_TASK_REJECT_REASON = range(10)

# --- Bot & API Lifespan Management ---
ptb_app = Application.builder().token(BOT_TOKEN).build()
@asynccontextmanager
async def lifespan(app: FastAPI): logger.info("Lifespan startup..."); await ptb_app.initialize(); await ptb_app.updater.start_polling(); await ptb_app.start(); logger.info("Telegram bot has started."); yield; logger.info("Lifespan shutdown..."); await ptb_app.updater.stop(); await ptb_app.stop(); await ptb_app.shutdown()
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
        announcement = db_session.query(SystemInfo).filter(SystemInfo.key == 'announcement').first()
        return { "balance": user.balance if user else 0.0, "referral_count": user.referral_count if user else 0, "successful_referrals": user.successful_referrals if user else 0, "announcement": announcement.value if announcement else "Welcome! No new announcements.", "tasks": [{"id": t.id, "description": t.description, "link": t.link, "reward": t.reward} for t in tasks], "withdrawals": [{"id": w.id, "amount": w.amount, "method": w.method, "status": w.status} for w in withdrawals] }
    except Exception as e: logger.error(f"Error in get_initial_data: {e}"); return {"error": "Internal server error"}

@app.post("/submit_task_proof")
async def submit_task_proof(request: Request):
    try:
        data = await request.json(); user_id = data.get('user_id'); task_id = data.get('task_id'); text = data.get('text'); photo_base64 = data.get('photo')
        submission = TaskSubmission(user_id=user_id, task_id=task_id, text_proof=text, photo_proof_base64=photo_base64)
        db_session.add(submission); db_session.commit()
        await ptb_app.bot.send_message(user_id, "✅ Your proof has been submitted for admin review!")
        return {"status": "success"}
    except Exception as e: logger.error(f"Error in submit_task_proof: {e}"); return {"error": "Internal server error"}

@app.post("/redeem_code")
async def redeem_code(request: Request):
    try:
        data = await request.json(); user_id = data.get('user_id'); code_str = data.get('code')
        code = db_session.query(RedeemCode).filter(RedeemCode.code == code_str).first()
        if code and code.uses_left > 0:
            user = db_session.query(User).filter(User.id == user_id).first(); user.balance += code.reward; code.uses_left -= 1; db_session.commit()
            return {"status": "success", "amount_rewarded": code.reward}
        else: return {"status": "error", "message": "Invalid or expired code."}
    except Exception as e: logger.error(f"Error in redeem_code: {e}"); return {"error": "Internal server error"}

# --- Telegram Bot Handlers ---
async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if context.args:
        try:
            referrer_id = int(context.args[0])
            if referrer_id != user.id and not db_session.query(User).filter(User.id == user.id).first():
                db_session.add(User(id=user.id, referrer_id=referrer_id))
                referrer = db_session.query(User).filter(User.id == referrer_id).first()
                if referrer: referrer.referral_count += 1
                db_session.commit()
        except (ValueError, IndexError): pass
    if not db_session.query(User).filter(User.id == user.id).first(): db_session.add(User(id=user.id)); db_session.commit()
    caption = (f"🚀 **Greetings, {user.first_name}!**\n\nWelcome to **{BOT_USERNAME}**, your portal to earning real rewards. Embark on quests (tasks), recruit allies (referrals), and claim your treasure.\n\nYour adventure begins now. Launch the dashboard to get started!")
    keyboard = [[InlineKeyboardButton("📱 Launch Dashboard", web_app=WebAppInfo(url=MINI_APP_URL))]]
    await update.message.reply_text(caption, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode='Markdown')

async def admin_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_CHAT_ID: return
    keyboard = [ [InlineKeyboardButton("📊 User Stats", callback_data="admin_stats"), InlineKeyboardButton("📢 Broadcast", callback_data="admin_broadcast")], [InlineKeyboardButton("📝 Manage Tasks", callback_data="admin_manage_tasks")], [InlineKeyboardButton("📜 Set Announcement", callback_data="admin_set_announcement")], [InlineKeyboardButton("🔑 Manage Codes", callback_data="admin_manage_codes")], [InlineKeyboardButton("🧐 Pending Submissions", callback_data="admin_pending_submissions")] ]
    await update.message.reply_text("👑 **Ultimate Admin Dashboard**", reply_markup=InlineKeyboardMarkup(keyboard), parse_mode='Markdown')
async def admin_stats(update: Update, context: ContextTypes.DEFAULT_TYPE): query = update.callback_query; await query.answer(); total_users = db_session.query(User).count(); total_balance = sum(u.balance for u in db_session.query(User).all()); pending_withdrawals = db_session.query(Withdrawal).filter(Withdrawal.status == 'pending').count(); await query.message.reply_text(f"**Bot Stats:**\n\n- Users: {total_users}\n- Balance in Circulation: ₱{total_balance:.2f}\n- Pending Withdrawals: {pending_withdrawals}", parse_mode='Markdown')

# --- Admin Conversations ---
async def broadcast_start(update: Update, context: ContextTypes.DEFAULT_TYPE): await update.callback_query.answer(); await update.callback_query.message.reply_text("Send the message to broadcast."); return BROADCAST_MESSAGE
async def broadcast_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    users = db_session.query(User).all()
    for user in users:
        try: await context.bot.copy_message(chat_id=user.id, from_chat_id=update.effective_chat.id, message_id=update.message.message_id)
        except Exception as e: logger.error(f"Failed to broadcast to {user.id}: {e}")
    await update.message.reply_text(f"Broadcast sent to {len(users)} users."); return ConversationHandler.END

async def announcement_start(update: Update, context: ContextTypes.DEFAULT_TYPE): await update.callback_query.answer(); await update.callback_query.message.reply_text("Enter new announcement text or /clear."); return ANNOUNCEMENT_TEXT
async def set_announcement_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    announcement = db_session.query(SystemInfo).filter(SystemInfo.key == 'announcement').first()
    if not announcement: announcement = SystemInfo(key='announcement')
    if update.message.text.lower() == '/clear': db_session.delete(announcement); await update.message.reply_text("Announcement cleared.")
    else: announcement.value = update.message.text; db_session.add(announcement); await update.message.reply_text("Announcement set.")
    db_session.commit(); return ConversationHandler.END

async def manage_tasks(update: Update, context: ContextTypes.DEFAULT_TYPE): query = update.callback_query; await query.answer(); keyboard = [[InlineKeyboardButton("➕ Add New Task", callback_data="add_task_start")], [InlineKeyboardButton("🗑️ Remove Task", callback_data="remove_task_list")]]; await query.message.edit_text("Manage tasks:", reply_markup=InlineKeyboardMarkup(keyboard))
async def add_task_start(update: Update, context: ContextTypes.DEFAULT_TYPE): await update.callback_query.answer(); await update.callback_query.message.reply_text("Enter task description:"); return TASK_DESC
async def get_task_description(update: Update, context: ContextTypes.DEFAULT_TYPE): context.user_data['task_desc'] = update.message.text; await update.message.reply_text("Send the link:"); return TASK_LINK
async def get_task_link(update: Update, context: ContextTypes.DEFAULT_TYPE): context.user_data['task_link'] = update.message.text; await update.message.reply_text("Enter the reward amount:"); return TASK_REWARD
async def get_task_reward(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try: reward = float(update.message.text); db_session.add(Task(description=context.user_data['task_desc'], link=context.user_data['task_link'], reward=reward)); db_session.commit(); await update.message.reply_text("✅ Task added!"); return ConversationHandler.END
    except ValueError: await update.message.reply_text("Invalid amount."); return TASK_REWARD

async def review_submissions(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query; await query.answer()
    submission = db_session.query(TaskSubmission).filter(TaskSubmission.status == 'pending').first()
    if not submission: await query.message.edit_text("No pending submissions."); return
    user = db_session.query(User).filter(User.id == submission.user_id).first()
    task = db_session.query(Task).filter(Task.id == submission.task_id).first()
    caption = f"**Submission Review**\n\n- User: {user.id}\n- Task: {task.description}\n- Note: {submission.text_proof}"
    keyboard = [[InlineKeyboardButton("Approve ✅", callback_data=f"approve_sub_{submission.id}"), InlineKeyboardButton("Reject ❌", callback_data=f"reject_sub_{submission.id}")]]
    photo_data = base64.b64decode(submission.photo_proof_base64.split(',')[1])
    await query.message.reply_photo(photo=BytesIO(photo_data), caption=caption, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode='Markdown')

async def approve_submission(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query; await query.answer()
    sub_id = int(query.data.split("_")[2]); submission = db_session.query(TaskSubmission).filter(TaskSubmission.id == sub_id).first()
    if not submission or submission.status != 'pending': await query.edit_message_caption("Already processed."); return
    submission.status = 'approved'
    user = db_session.query(User).filter(User.id == submission.user_id).first()
    task = db_session.query(Task).filter(Task.id == submission.task_id).first()
    user.balance += task.reward
    if user.tasks_completed == 0 and user.referrer_id:
        referrer = db_session.query(User).filter(User.id == user.referrer_id).first()
        if referrer: referrer.balance += INVITE_REWARD; referrer.successful_referrals += 1
    user.tasks_completed += 1
    db_session.commit()
    await query.edit_message_caption(caption=f"{query.message.caption}\n\n**Status: APPROVED**", parse_mode='Markdown')
    await ptb_app.bot.send_message(chat_id=user.id, text=f"🎉 Your submission for '{task.description}' was approved! You earned ₱{task.reward:.2f}.")

# Add all other handlers here...

ptb_app.add_handler(CommandHandler("start", start_command))
ptb_app.add_handler(CommandHandler("admin", admin_command))
ptb_app.add_handler(CallbackQueryHandler(admin_stats, pattern="^admin_stats$"))
ptb_app.add_handler(CallbackQueryHandler(manage_tasks, pattern="^admin_manage_tasks$"))
ptb_app.add_handler(CallbackQueryHandler(review_submissions, pattern="^admin_pending_submissions$"))
ptb_app.add_handler(CallbackQueryHandler(approve_submission, pattern=r"^approve_sub_\d+$"))
# ... conversations
add_task_conv = ConversationHandler(entry_points=[CallbackQueryHandler(add_task_start, pattern="^add_task_start$")], states={TASK_DESC: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_task_description)], TASK_LINK: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_task_link)], TASK_REWARD: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_task_reward)]}, fallbacks=[])
broadcast_conv = ConversationHandler(entry_points=[CallbackQueryHandler(broadcast_start, pattern="^admin_broadcast$")], states={BROADCAST_MESSAGE: [MessageHandler(filters.ALL & ~filters.COMMAND, broadcast_message)]}, fallbacks=[])
announcement_conv = ConversationHandler(entry_points=[CallbackQueryHandler(announcement_start, pattern="^admin_set_announcement$")], states={ANNOUNCEMENT_TEXT: [MessageHandler(filters.TEXT & ~filters.COMMAND, set_announcement_text)]}, fallbacks=[])
ptb_app.add_handler(add_task_conv)
ptb_app.add_handler(broadcast_conv)
ptb_app.add_handler(announcement_conv)

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8000))
    uvicorn.run("main:app", host="0.0.0.0", port=port, reload=False)
