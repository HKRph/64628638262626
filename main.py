import logging, json, uvicorn, os, base64, random
from io import BytesIO
from contextlib import asynccontextmanager
from datetime import date, timedelta
from typing import Optional
from fastapi import FastAPI, Request, HTTPException, status
from fastapi.middleware.cors import CORSMiddleware
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, WebAppInfo, InputFile
from telegram.ext import (
    Application, CommandHandler, ContextTypes, ConversationHandler,
    MessageHandler, filters, CallbackQueryHandler
)
from sqlalchemy import create_engine, Column, Integer, BigInteger, String, Float, ForeignKey, Text, Date
from sqlalchemy.orm import declarative_base, sessionmaker

# --- Configuration ---
BOT_TOKEN = "8486136204:AAFZkkxVFlBK1S5_RzrOlZ4ZZ6cDBcBjqVY"
BOT_USERNAME = "GTaskPHBot"
ADMIN_CHAT_ID = 7331257920 # IMPORTANT: Replace with your actual Admin Telegram User ID
MINI_APP_URL = "https://gtask-fronted.vercel.app/" # YOUR FRONTEND URL
INVITE_REWARD = 77.0 # Reward for a successful referral (when referred user completes first task)
MIN_WITHDRAWAL = 300.0 # Minimum amount a user can withdraw
MAX_WITHDRAWAL = 30000.0 # Maximum amount a user can withdraw
WITHDRAWAL_FEE_PERCENT = 0.03 # 3% fee
DAILY_BONUS = 10.0 # Amount for daily login bonus
DAILY_BONUS_INVITE_REQ = 2 # Number of new invites required since last claim for daily bonus
TASK_MILESTONES = {"10_tasks": 50.0, "20_tasks": 150.0, "30_tasks": 400.0} # Bonus rewards for completing tasks
GIFT_TICKET_PRICE = 77.0
GIFT_MIN_AMOUNT = 300.0
GIFT_MAX_AMOUNT = 80000.0
GIFT_FEE_PERCENT = 0.05 # 5% fee

# --- Database Setup ---
logging.basicConfig(format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO)
logger = logging.getLogger(__name__)

Base = declarative_base()
class User(Base):
    __tablename__ = "users"
    id = Column(BigInteger, primary_key=True, autoincrement=False)
    balance = Column(Float, default=0.0)
    gift_tickets = Column(Integer, default=0)
    referral_count = Column(Integer, default=0) # Total referred users
    successful_referrals = Column(Integer, default=0) # Referred users who completed at least one task
    tasks_completed = Column(Integer, default=0) # Total tasks completed by user
    completed_task_ids = Column(Text, default="[]") # JSON list of task IDs completed by this user
    referrer_id = Column(BigInteger, ForeignKey("users.id"), nullable=True)
    status = Column(String, default="active") # active, banned, restricted
    status_until = Column(Date, nullable=True) # For temporary restrictions
    last_login_date = Column(Date, nullable=True) # For daily bonus claim
    daily_claim_invites = Column(Integer, default=0) # Invites since last daily claim
    claimed_milestones = Column(Text, default="{}") # JSON string for task milestones claimed

class Task(Base):
    __tablename__ = "tasks"
    id = Column(Integer, primary_key=True)
    description = Column(String)
    link = Column(String)
    reward = Column(Float)

class TaskSubmission(Base):
    __tablename__ = "task_submissions"
    id = Column(Integer, primary_key=True)
    user_id = Column(BigInteger)
    task_id = Column(Integer)
    text_proof = Column(Text)
    photo_proof_base64 = Column(Text)
    status = Column(String, default="pending") # pending, approved, rejected
    created_at = Column(Date, default=date.today())

class Withdrawal(Base):
    __tablename__ = "withdrawals"
    id = Column(Integer, primary_key=True)
    user_id = Column(BigInteger)
    amount = Column(Float)
    fee = Column(Float, default=0.0)
    method = Column(String)
    details = Column(String)
    status = Column(String, default="pending") # pending, approved, rejected
    created_at = Column(Date, default=date.today())

class RedeemCode(Base):
    __tablename__ = "redeem_codes"
    id = Column(Integer, primary_key=True)
    code = Column(String, unique=True)
    reward = Column(Float)
    uses_left = Column(Integer, default=1) # -1 for unlimited

class SystemInfo(Base):
    __tablename__ = "system_info"
    key = Column(String, primary_key=True)
    value = Column(String)

engine = create_engine("sqlite:///gtask_data.db") # Using a specific DB name for clarity
Base.metadata.create_all(engine); Session = sessionmaker(bind=engine); db_session = Session()

# --- Conversation States (FIXED AND EXPLICITLY DEFINED) ---
STATE_TASK_DESC = 0
STATE_TASK_LINK = 1
STATE_TASK_REWARD = 2
STATE_REJECT_WD_REASON = 3
STATE_BROADCAST_MESSAGE = 4
STATE_ANNOUNCEMENT_TEXT = 5
STATE_NEW_CODE_CODE = 6
STATE_NEW_CODE_REWARD = 7
STATE_NEW_CODE_USES = 8
STATE_USER_MGT_ID = 9
STATE_USER_MGT_ACTION = 10
STATE_USER_MGT_DURATION = 11
STATE_RAIN_AMOUNT = 12
STATE_RAIN_USERS = 13
STATE_REJECT_SUB_REASON = 14
STATE_DELETE_TASK = 15
STATE_DELETE_CODE = 16
STATE_WARN_USER_ID = 17
STATE_WARN_REASON = 18

# Assigning to readable names for use in ConversationHandler
TASK_DESC, TASK_LINK, TASK_REWARD, REJECT_REASON_WD, BROADCAST_MESSAGE, ANNOUNCEMENT_TEXT, NEW_CODE_CODE, NEW_CODE_REWARD, NEW_CODE_USES, USER_MGT_ID, USER_MGT_ACTION, USER_MGT_DURATION, RAIN_AMOUNT, RAIN_USERS, SUBMIT_TASK_REJECT_REASON, DELETE_TASK, DELETE_CODE, WARN_USER_ID, WARN_REASON = range(19)


# --- Bot & API Lifespan ---
ptb_app = Application.builder().token(BOT_TOKEN).build()

@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("Lifespan startup...")
    await ptb_app.initialize()
    await ptb_app.updater.start_polling(drop_pending_updates=True) # Drop old updates on startup
    await ptb_app.start()
    logger.info("Telegram bot has started successfully.")
    yield
    logger.info("Lifespan shutdown..."); await ptb_app.updater.stop(); await ptb_app.stop(); await ptb_app.shutdown()

app = FastAPI(lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"], # Allow all origins for simplicity
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# --- API Endpoints ---
@app.get("/")
async def health_check():
    return {"status": "ok", "message": f"{BOT_USERNAME} API is running!"}

@app.post("/get_initial_data")
async def get_initial_data(request: Request):
    try:
        data = await request.json(); user_id = data.get('user_id')
        if not user_id: raise HTTPException(status_code=400, detail="user_id not provided")
        
        user = db_session.query(User).filter(User.id == user_id).first()
        if not user: user = User(id=user_id); db_session.add(user); db_session.commit()
        
        # Check user status for Mini App access
        if user.status == 'banned': raise HTTPException(status_code=403, detail="You are permanently banned.")
        if user.status == 'restricted' and user.status_until and user.status_until > date.today():
            raise HTTPException(status_code=403, detail=f"You are restricted until {user.status_until.strftime('%b %d')}.")
        elif user.status == 'restricted' and user.status_until and user.status_until <= date.today():
             user.status = 'active'; user.status_until = None; db_session.commit() # Lift restriction

        can_claim_daily = (user.last_login_date is None or user.last_login_date < date.today()) and user.daily_claim_invites >= DAILY_BONUS_INVITE_REQ
        
        tasks = db_session.query(Task).all()
        # Filter tasks already completed by the user
        completed_task_ids_list = json.loads(user.completed_task_ids) if user.completed_task_ids else []
        available_tasks = [t for t in tasks if t.id not in completed_task_ids_list]

        withdrawals = db_session.query(Withdrawal).filter(Withdrawal.user_id == user_id).order_by(Withdrawal.created_at.desc()).all()
        announcement = db_session.query(SystemInfo).filter(SystemInfo.key == 'announcement').first()
        withdrawal_maintenance = db_session.query(SystemInfo).filter(SystemInfo.key == 'withdrawal_maintenance').first()
        
        return {
            "balance": user.balance,
            "gift_tickets": user.gift_tickets,
            "referral_count": user.referral_count,
            "successful_referrals": user.successful_referrals,
            "tasks_completed": user.tasks_completed,
            "daily_claim_invites": user.daily_claim_invites,
            "can_claim_daily": can_claim_daily,
            "daily_bonus_req": DAILY_BONUS_INVITE_REQ,
            "announcement": announcement.value if announcement else "Welcome! No new announcements.",
            "tasks": [{"id": t.id, "description": t.description, "link": t.link, "reward": t.reward} for t in available_tasks],
            "withdrawals": [{"id": w.id, "amount": w.amount, "fee": w.fee, "status": w.status, "date": w.created_at.strftime('%Y-%m-%d')} for w in withdrawals],
            "claimed_milestones": json.loads(user.claimed_milestones) if user.claimed_milestones else {},
            "min_withdrawal": MIN_WITHDRAWAL,
            "max_withdrawal": MAX_WITHDRAWAL,
            "withdrawal_fee_percent": WITHDRAWAL_FEE_PERCENT,
            "withdrawal_maintenance": withdrawal_maintenance.value == "true" if withdrawal_maintenance else False,
            "gift_ticket_price": GIFT_TICKET_PRICE,
            "gift_min_amount": GIFT_MIN_AMOUNT,
            "gift_max_amount": GIFT_MAX_AMOUNT,
            "gift_fee_percent": GIFT_FEE_PERCENT
        }
    except HTTPException as he:
        logger.warning(f"API Error for user {user_id}: {he.detail}")
        raise he
    except Exception as e:
        logger.error(f"API Error in get_initial_data for user {user_id}: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Internal server error")

@app.post("/submit_task_proof")
async def submit_task_proof(request: Request):
    try:
        data = await request.json(); user_id = data.get('user_id'); task_id = data.get('task_id'); text = data.get('text'); photo_base64 = data.get('photo')
        
        user_db = db_session.query(User).filter(User.id == user_id).first()
        if not user_db or user_db.status != 'active': raise HTTPException(status_code=403, detail="Account not active.")
        
        completed_task_ids_list = json.loads(user_db.completed_task_ids) if user_db.completed_task_ids else []
        if task_id in completed_task_ids_list:
            raise HTTPException(status_code=400, detail="Task already completed.")

        submission = TaskSubmission(user_id=user_id, task_id=task_id, text_proof=text, photo_proof_base64=photo_base64, created_at=date.today())
        db_session.add(submission); db_session.commit()
        await ptb_app.bot.send_message(user_id, "✅ Your proof has been submitted for admin review!")
        
        task = db_session.query(Task).filter(Task.id == task_id).first()
        caption = f"**New Task Submission for Review**\n\n- User ID: `{user_id}`\n- Task: {task.description}\n- Reward: ₱{task.reward:.2f}\n- Note: {text}"
        keyboard = [[InlineKeyboardButton("Approve ✅", callback_data=f"approve_sub_{submission.id}"), InlineKeyboardButton("Reject ❌", callback_data=f"reject_sub_start_{submission.id}")]]
        
        photo_data = base64.b64decode(photo_base64.split(',')[1])
        await ptb_app.bot.send_photo(chat_id=ADMIN_CHAT_ID, photo=BytesIO(photo_data), caption=caption, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode='Markdown')

        return {"status": "success"}
    except HTTPException as he: raise he
    except Exception as e: logger.error(f"Error in submit_task_proof: {e}", exc_info=True); raise HTTPException(status_code=500, detail="Internal server error")

@app.post("/redeem_code")
async def redeem_code(request: Request):
    try:
        data = await request.json(); user_id = data.get('user_id'); code_str = data.get('code').upper()
        
        user_db = db_session.query(User).filter(User.id == user_id).first()
        if not user_db or user_db.status != 'active': raise HTTPException(status_code=403, detail="Account not active.")

        code = db_session.query(RedeemCode).filter(RedeemCode.code == code_str).first()
        if code and (code.uses_left == -1 or code.uses_left > 0):
            user_db.balance += code.reward
            if code.uses_left != -1: code.uses_left -= 1
            db_session.commit()
            return {"status": "success", "amount_rewarded": code.reward}
        else:
            raise HTTPException(status_code=400, detail="Invalid or expired code.")
    except HTTPException as he: raise he
    except Exception as e: logger.error(f"Error in redeem_code: {e}", exc_info=True); raise HTTPException(status_code=500, detail="Internal server error")

@app.post("/claim_daily_bonus")
async def claim_daily_bonus(request: Request):
    try:
        data = await request.json(); user_id = data.get('user_id')
        user = db_session.query(User).filter(User.id == user_id).first()
        if not user or user.status != 'active': raise HTTPException(status_code=403, detail="Account not active.")

        if user and (user.last_login_date is None or user.last_login_date < date.today()):
            if user.daily_claim_invites >= DAILY_BONUS_INVITE_REQ:
                user.balance += DAILY_BONUS
                user.last_login_date = date.today()
                user.daily_claim_invites = 0 # Reset invites after claiming
                db_session.commit()
                await ptb_app.bot.send_message(user_id, f"🎉 Daily bonus of ₱{DAILY_BONUS:.2f} claimed! Come back tomorrow!")
                return {"status": "success"}
            else:
                raise HTTPException(status_code=400, detail=f"Invite {DAILY_BONUS_INVITE_REQ - user.daily_claim_invites} more users to claim your daily bonus.")
        else:
            raise HTTPException(status_code=400, detail="Daily bonus already claimed or not yet available.")
    except HTTPException as he: raise he
    except Exception as e: logger.error(f"API Error in claim_daily_bonus: {e}", exc_info=True); raise HTTPException(status_code=500, detail="Internal server error")

@app.post("/submit_withdrawal")
async def submit_withdrawal(request: Request):
    try:
        data = await request.json(); user_id = data.get('user_id'); amount = float(data.get('amount')); method = data.get('method'); details = data.get('details')
        
        withdrawal_maintenance = db_session.query(SystemInfo).filter(SystemInfo.key == 'withdrawal_maintenance').first()
        if withdrawal_maintenance and withdrawal_maintenance.value == "true":
            raise HTTPException(status_code=403, detail="Withdrawals are currently under maintenance. Please try again later.")

        user = db_session.query(User).filter(User.id == user_id).first()
        if not user or user.status != 'active': raise HTTPException(status_code=403, detail="Account not active.")
        if not (MIN_WITHDRAWAL <= amount <= MAX_WITHDRAWAL):
            raise HTTPException(status_code=400, detail=f"Amount must be between ₱{MIN_WITHDRAWAL:.2f} and ₱{MAX_WITHDRAWAL:.2f}.")
        
        fee = amount * WITHDRAWAL_FEE_PERCENT
        total_deduction = amount + fee

        if user.balance < total_deduction:
            raise HTTPException(status_code=400, detail="Insufficient balance to cover withdrawal amount and fee.")
            
        new_withdrawal = Withdrawal(user_id=user.id, amount=amount, fee=fee, method=method, details=details, created_at=date.today())
        db_session.add(new_withdrawal); user.balance -= total_deduction; db_session.commit()
        await ptb_app.bot.send_message(user_id, f"✅ Your withdrawal request for ₱{amount:.2f} (Fee: ₱{fee:.2f}) has been submitted! Our team will review it shortly.")
        admin_message = f"**New Withdrawal Request**\n\n- User ID: `{user.id}`\n- Amount: `₱{amount:.2f}`\n- Fee: `₱{fee:.2f}`\n- Method: `{method}`\n- Details: `{details}`\n\n**Action: /admin**"
        keyboard = [[InlineKeyboardButton("Approve ✅", callback_data=f"approve_wd_{new_withdrawal.id}"), InlineKeyboardButton("Reject ❌", callback_data=f"reject_wd_start_{new_withdrawal.id}")]]
        await ptb_app.bot.send_message(chat_id=ADMIN_CHAT_ID, text=admin_message, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode='Markdown')
        return {"status": "success"}
    except HTTPException as he: raise he
    except Exception as e: logger.error(f"Error in submit_withdrawal: {e}", exc_info=True); raise HTTPException(status_code=500, detail="Internal server error")

@app.post("/buy_ticket")
async def buy_ticket(request: Request):
    try:
        data = await request.json(); user_id = data.get('user_id')
        user = db_session.query(User).filter(User.id == user_id).first()
        if not user or user.status != 'active': raise HTTPException(status_code=403, detail="Account not active.")
        if user.balance < GIFT_TICKET_PRICE: raise HTTPException(status_code=400, detail="Insufficient balance to buy a Gift Ticket.")
        user.balance -= GIFT_TICKET_PRICE; user.gift_tickets += 2; db_session.commit()
        await ptb_app.bot.send_message(user_id, f"🎉 You have successfully bought a Gift Ticket and received one for free! You now have {user.gift_tickets} tickets.")
        return {"status": "success"}
    except HTTPException as he: raise he
    except Exception as e: logger.error(f"API Error in buy_ticket: {e}", exc_info=True); raise HTTPException(status_code=500, detail="Internal server error")

@app.post("/gift_money")
async def gift_money(request: Request):
    try:
        data = await request.json(); user_id = data.get('user_id'); recipient_id = int(data.get('recipient_id')); amount = float(data.get('amount'))
        
        sender = db_session.query(User).filter(User.id == user_id).first()
        if not sender or sender.status != 'active': raise HTTPException(status_code=403, detail="Account not active.")
        if sender.gift_tickets < 1: raise HTTPException(status_code=400, detail="You do not have any Gift Tickets.")
        if not (GIFT_MIN_AMOUNT <= amount <= GIFT_MAX_AMOUNT): raise HTTPException(status_code=400, detail=f"Amount must be between ₱{GIFT_MIN_AMOUNT:.2f} and ₱{GIFT_MAX_AMOUNT:.2f}.")
        
        fee = amount * GIFT_FEE_PERCENT
        total_deduction = amount + fee
        if sender.balance < total_deduction: raise HTTPException(status_code=400, detail="Insufficient balance to cover gift amount and fee.")

        recipient = db_session.query(User).filter(User.id == recipient_id).first()
        if not recipient: raise HTTPException(status_code=404, detail="Recipient user not found.")
        if recipient.status != 'active': raise HTTPException(status_code=400, detail="Recipient account is not active.")

        sender.balance -= total_deduction; sender.gift_tickets -= 1
        recipient.balance += amount
        db_session.commit()

        await ptb_app.bot.send_message(user_id, f"✅ You have successfully gifted ₱{amount:.2f} to user {recipient_id}. A fee of ₱{fee:.2f} was applied.")
        await ptb_app.bot.send_message(recipient_id, f"🎉 You have received a gift of ₱{amount:.2f} from user {user_id}!")
        return {"status": "success"}
    except HTTPException as he: raise he
    except Exception as e: logger.error(f"API Error in gift_money: {e}", exc_info=True); raise HTTPException(status_code=500, detail="Internal server error")

# --- Telegram Handlers ---
async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    user_db = db_session.query(User).filter(User.id == user.id).first()
    if context.args:
        try:
            referrer_id = int(context.args[0])
            if referrer_id != user.id and (not user_db or user_db.status == 'active'):
                if not user_db: user_db = User(id=user.id); db_session.add(user_db)
                user_db.referrer_id = referrer_id
                referrer = db_session.query(User).filter(User.id == referrer_id).first()
                if referrer:
                    referrer.referral_count += 1
                    referrer.daily_claim_invites += 1 # Increment invites for daily claim
                    await context.bot.send_message(chat_id=referrer.id, text=f"🎉 {user.first_name} has joined using your link! Get them to complete a task for a reward!")
                db_session.commit()
        except (ValueError, IndexError): pass
    if not user_db: user_db = User(id=user.id); db_session.add(user_db); db_session.commit()
    
    if user_db.status == 'banned': caption = "🚫 You are permanently banned from this bot."; keyboard = []
    elif user_db.status == 'restricted' and user_db.status_until and user_db.status_until > date.today():
        caption = f"⚠️ Your account is restricted until {user_db.status_until.strftime('%b %d')}."; keyboard = []
    else:
        caption = (f"🚀 **Greetings, {user.first_name}!**\n\nWelcome to **{BOT_USERNAME}**, your portal to earning real rewards. Embark on quests (tasks), recruit allies (referrals), and claim your treasure.\n\nYour adventure begins now. Launch the dashboard to get started!")
        keyboard = [[InlineKeyboardButton("📱 Launch Dashboard", web_app=WebAppInfo(url=MINI_APP_URL))]]
    await update.message.reply_text(caption, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode='Markdown')

# --- Admin Panel Handlers ---
async def admin_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_CHAT_ID: return
    keyboard = [
        [InlineKeyboardButton("📊 User Stats", callback_data="admin_stats"), InlineKeyboardButton("📢 Broadcast", callback_data="admin_broadcast")],
        [InlineKeyboardButton("📜 Set Announcement", callback_data="admin_set_announcement"), InlineKeyboardButton("📝 Manage Tasks", callback_data="admin_manage_tasks")],
        [InlineKeyboardButton("🔑 Manage Codes", callback_data="admin_manage_codes"), InlineKeyboardButton("🔨 User Management", callback_data="admin_user_mgt")],
        [InlineKeyboardButton("🌧️ Rain Prize", callback_data="admin_rain"), InlineKeyboardButton("🧐 Review Submissions", callback_data="admin_pending_submissions")],
        [InlineKeyboardButton("⚙️ Maintenance Mode", callback_data="admin_maintenance")],
        [InlineKeyboardButton("⚠️ Warn User", callback_data="admin_warn_user")]
    ]
    await update.message.reply_text("👑 **Ultimate Admin Dashboard**", reply_markup=InlineKeyboardMarkup(keyboard), parse_mode='Markdown')

async def admin_main_menu_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query; await query.answer()
    await admin_command(query, context) # Re-send the admin dashboard

async def admin_stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query; await query.answer()
    total_users = db_session.query(User).count()
    active_users = db_session.query(User).filter(User.status == 'active').count()
    banned_users = db_session.query(User).filter(User.status == 'banned').count()
    restricted_users = db_session.query(User).filter(User.status == 'restricted').count()
    total_balance = sum(u.balance for u in db_session.query(User).all())
    pending_withdrawals = db_session.query(Withdrawal).filter(Withdrawal.status == 'pending').count()
    pending_submissions = db_session.query(TaskSubmission).filter(TaskSubmission.status == 'pending').count()

    await query.message.reply_text(
        f"**📊 Bot Statistics:**\n\n"
        f"👥 Total Users: {total_users}\n"
        f"🟢 Active Users: {active_users}\n"
        f"⛔ Banned Users: {banned_users}\n"
        f"🚧 Restricted Users: {restricted_users}\n"
        f"💰 Total Balance in Circulation: ₱{total_balance:.2f}\n"
        f"💸 Pending Withdrawals: {pending_withdrawals}\n"
        f"📝 Pending Task Submissions: {pending_submissions}",
        parse_mode='Markdown'
    )

# --- Broadcast Conversation ---
async def broadcast_start(update: Update, context: ContextTypes.DEFAULT_TYPE): await update.callback_query.answer(); await update.callback_query.message.reply_text("Send the message you want to broadcast to all active users."); return BROADCAST_MESSAGE
async def broadcast_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    active_users = db_session.query(User).filter(User.status == 'active').all()
    sent_count = 0
    for user in active_users:
        try: await context.bot.copy_message(chat_id=user.id, from_chat_id=update.effective_chat.id, message_id=update.message.message_id); sent_count += 1
        except Exception as e: logger.error(f"Failed to broadcast to {user.id}: {e}")
    await update.message.reply_text(f"Broadcast sent to {sent_count}/{len(active_users)} active users."); return ConversationHandler.END

# --- Announcement Conversation ---
async def announcement_start(update: Update, context: ContextTypes.DEFAULT_TYPE): await update.callback_query.answer(); await update.callback_query.message.reply_text("Enter new announcement text (or send /clear to remove)."); return ANNOUNCEMENT_TEXT
async def set_announcement_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    announcement = db_session.query(SystemInfo).filter(SystemInfo.key == 'announcement').first()
    if not announcement: announcement = SystemInfo(key='announcement')
    if update.message.text.lower() == '/clear': db_session.delete(announcement); await update.message.reply_text("Announcement cleared.")
    else: announcement.value = update.message.text; db_session.add(announcement); await update.message.reply_text("Announcement set.")
    db_session.commit(); return ConversationHandler.END

# --- Manage Tasks ---
async def manage_tasks(update: Update, context: ContextTypes.DEFAULT_TYPE): query = update.callback_query; await query.answer(); keyboard = [[InlineKeyboardButton("➕ Add New Task", callback_data="add_task_start")], [InlineKeyboardButton("🗑️ Remove Task", callback_data="remove_task_list")]]; await query.message.edit_text("Manage tasks:", reply_markup=InlineKeyboardMarkup(keyboard))
async def add_task_start(update: Update, context: ContextTypes.DEFAULT_TYPE): await update.callback_query.answer(); await update.callback_query.message.reply_text("Enter task description:"); return TASK_DESC
async def get_task_description(update: Update, context: ContextTypes.DEFAULT_TYPE): context.user_data['task_desc'] = update.message.text; await update.message.reply_text("Send the link (e.g., https://example.com):"); return TASK_LINK
async def get_task_link(update: Update, context: ContextTypes.DEFAULT_TYPE):
    link = update.message.text
    if not link.startswith('http://') and not link.startswith('https://'): link = 'https://' + link # Ensure absolute link
    context.user_data['task_link'] = link
    await update.message.reply_text("Enter the reward amount (e.g., 50.00):"); return TASK_REWARD
async def get_task_reward(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try: reward = float(update.message.text); db_session.add(Task(description=context.user_data['task_desc'], link=context.user_data['task_link'], reward=reward)); db_session.commit(); await update.message.reply_text("✅ Task added!"); return ConversationHandler.END
    except ValueError: await update.message.reply_text("Invalid amount."); return TASK_REWARD

async def remove_task_list(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query; await query.answer(); tasks = db_session.query(Task).all()
    if not tasks: await query.message.edit_text("No tasks to remove."); return
    keyboard = [[InlineKeyboardButton(f"❌ {task.description[:40]} (₱{task.reward:.2f})", callback_data=f"delete_task_{task.id}")] for task in tasks]
    await query.message.edit_text("Select a task to remove:", reply_markup=InlineKeyboardMarkup(keyboard));
async def delete_task_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query; task_id = int(query.data.split("_")[2]); task = db_session.query(Task).filter(Task.id == task_id).first()
    if task: db_session.delete(task); db_session.commit(); await query.answer("Task removed!", show_alert=True); await remove_task_list(update, context)
    else: await query.answer("Task not found.", show_alert=True)

# --- Manage Codes ---
async def manage_codes(update: Update, context: ContextTypes.DEFAULT_TYPE): query = update.callback_query; await query.answer(); keyboard = [[InlineKeyboardButton("➕ Add New Code", callback_data="add_code_start")], [InlineKeyboardButton("🗑️ Remove Code", callback_data="remove_code_list")]]; await query.message.edit_text("Manage redeem codes:", reply_markup=InlineKeyboardMarkup(keyboard))
async def add_code_start(update: Update, context: ContextTypes.DEFAULT_TYPE): await update.callback_query.answer(); await update.callback_query.message.reply_text("Enter new redeem code:"); return NEW_CODE_CODE
async def get_new_code_code(update: Update, context: ContextTypes.DEFAULT_TYPE): context.user_data['new_code_code'] = update.message.text.upper(); await update.message.reply_text("Enter reward amount:"); return NEW_CODE_REWARD
async def get_new_code_reward(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try: context.user_data['new_code_reward'] = float(update.message.text); await update.message.reply_text("Enter uses left (-1 for unlimited):"); return NEW_CODE_USES
    except ValueError: await update.message.reply_text("Invalid amount."); return NEW_CODE_REWARD
async def get_new_code_uses(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        uses = int(update.message.text)
        code = RedeemCode(code=context.user_data['new_code_code'], reward=context.user_data['new_code_reward'], uses_left=uses)
        db_session.add(code); db_session.commit(); await update.message.reply_text("✅ Code added!"); return ConversationHandler.END
    except ValueError: await update.message.reply_text("Invalid uses."); return NEW_CODE_USES

async def remove_code_list(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query; await query.answer(); codes = db_session.query(RedeemCode).all()
    if not codes: await query.message.edit_text("No codes to remove."); return
    keyboard = [[InlineKeyboardButton(f"❌ {code.code} (₱{code.reward:.2f}, uses: {code.uses_left})", callback_data=f"delete_code_{code.id}")] for code in codes]
    await query.message.edit_text("Select a code to remove:", reply_markup=InlineKeyboardMarkup(keyboard));
async def delete_code_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query; code_id = int(query.data.split("_")[2]); code = db_session.query(RedeemCode).filter(RedeemCode.id == code_id).first()
    if code: db_session.delete(code); db_session.commit(); await query.answer("Code removed!", show_alert=True); await remove_code_list(update, context)
    else: await query.answer("Code not found.", show_alert=True)

# --- User Management ---
async def user_mgt_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query; await query.answer()
    keyboard = [[InlineKeyboardButton("Ban 🔨", callback_data="user_mgt_ban")], [InlineKeyboardButton("Unban 🔓", callback_data="user_mgt_unban")], [InlineKeyboardButton("Restrict Temp ⏳", callback_data="user_mgt_restrict")]]
    await query.message.edit_text("User Management:", reply_markup=InlineKeyboardMarkup(keyboard))
async def user_mgt_action_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query; context.user_data['mgt_action'] = query.data.split('_')[-1]
    await query.message.edit_text(f"Send the User ID to {context.user_data['mgt_action']}:"); return USER_MGT_ID
async def user_mgt_id_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try: user_id = int(update.message.text); context.user_data['mgt_user_id'] = user_id
    except ValueError: await update.message.reply_text("Invalid User ID."); return USER_MGT_ID
    if context.user_data['mgt_action'] == 'restrict': await update.message.reply_text("Enter duration in days:"); return USER_MGT_DURATION
    user = db_session.query(User).filter(User.id == user_id).first()
    if not user: await update.message.reply_text("User not found."); return ConversationHandler.END
    if context.user_data['mgt_action'] == 'ban': user.status = 'banned'; user.status_until = None; await ptb_app.bot.send_message(user_id, "⚠️ Your account has been permanently banned."); await update.message.reply_text("User banned.");
    elif context.user_data['mgt_action'] == 'unban': user.status = 'active'; user.status_until = None; await ptb_app.bot.send_message(user_id, "✅ Your account has been unbanned."); await update.message.reply_text("User unbanned.");
    db_session.commit(); return ConversationHandler.END
async def user_mgt_duration_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try: duration = int(update.message.text); user_id = context.user_data['mgt_user_id']
    except ValueError: await update.message.reply_text("Invalid duration."); return USER_MGT_DURATION
    user = db_session.query(User).filter(User.id == user_id).first()
    if not user: await update.message.reply_text("User not found."); return ConversationHandler.END
    user.status = 'restricted'; user.status_until = date.today() + timedelta(days=duration)
    db_session.commit()
    await ptb_app.bot.send_message(user_id, f"⚠️ Your account has been temporarily restricted for {duration} days."); await update.message.reply_text(f"User restricted for {duration} days.");
    return ConversationHandler.END

# --- Rain Prize ---
async def rain_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query; await query.answer(); await query.message.reply_text("Send the total amount to distribute (e.g., 500):"); return RAIN_AMOUNT
async def rain_amount_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try: amount = float(update.message.text); context.user_data['rain_amount'] = amount
    except ValueError: await update.message.reply_text("Invalid amount."); return RAIN_AMOUNT
    await update.message.reply_text("Send the number of users to share the prize pool with (e.g., 10):"); return RAIN_USERS
async def rain_users_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try: num_users = int(update.message.text); amount = context.user_data['rain_amount']
    except ValueError: await update.message.reply_text("Invalid number."); return RAIN_USERS
    eligible_users = db_session.query(User).filter(User.status == 'active').all()
    if len(eligible_users) < num_users: await update.message.reply_text(f"Only {len(eligible_users)} eligible users found."); return ConversationHandler.END
    winners = random.sample(eligible_users, num_users)
    prize_per_user = amount / num_users
    for user in winners:
        user.balance += prize_per_user
        await ptb_app.bot.send_message(user.id, f"🎉 You were in the Rain Prize! You won ₱{prize_per_user:.2f}!")
    db_session.commit()
    await update.message.reply_text(f"Rain Prize complete. ₱{amount:.2f} distributed to {num_users} users."); return ConversationHandler.END

# --- Review Submissions ---
async def review_submissions(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query; await query.answer()
    submission = db_session.query(TaskSubmission).filter(TaskSubmission.status == 'pending').first()
    if not submission: await query.message.edit_text("No pending submissions."); return
    user = db_session.query(User).filter(User.id == submission.user_id).first()
    task = db_session.query(Task).filter(Task.id == submission.task_id).first()
    caption = f"**Submission Review**\n\n- User: {user.id}\n- Task: {task.description}\n- Reward: ₱{task.reward:.2f}\n- Note: {submission.text_proof}"
    keyboard = [[InlineKeyboardButton("Approve ✅", callback_data=f"approve_sub_{submission.id}"), InlineKeyboardButton("Reject ❌", callback_data=f"reject_sub_start_{submission.id}")]]
    photo_data = base64.b64decode(submission.photo_proof_base64.split(',')[1])
    await query.message.reply_photo(photo=BytesIO(photo_data), caption=caption, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode='Markdown')

async def approve_submission(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query; await query.answer()
    sub_id = int(query.data.split("_")[2]); submission = db_session.query(TaskSubmission).filter(TaskSubmission.id == sub_id).first()
    if not submission or submission.status != 'pending': await query.edit_message_caption("Already processed."); return
    submission.status = 'approved'
    user = db_session.query(User).filter(User.id == submission.user_id).first()
    task = db_session.query(Task).filter(Task.id == submission.task_id).first()
    
    completed_task_ids_list = json.loads(user.completed_task_ids) if user.completed_task_ids else []
    if task.id not in completed_task_ids_list:
        user.balance += task.reward
        user.tasks_completed += 1
        completed_task_ids_list.append(task.id)
        user.completed_task_ids = json.dumps(completed_task_ids_list)

        claimed_milestones = json.loads(user.claimed_milestones) if user.claimed_milestones else {}
        for milestone_str, reward_amount in TASK_MILESTONES.items():
            milestone = int(milestone_str.split('_')[0])
            if (user.tasks_completed) == milestone and claimed_milestones.get(milestone_str) is None:
                user.balance += reward_amount
                claimed_milestones[milestone_str] = True
                user.claimed_milestones = json.dumps(claimed_milestones) # Update claimed milestones
                await ptb_app.bot.send_message(user.id, f"🎉 Milestone Reached! You completed {milestone} tasks and earned a bonus of ₱{reward_amount:.2f}!")

        if user.tasks_completed == 1 and user.referrer_id: # First task completed triggers successful referral
            referrer = db_session.query(User).filter(User.id == user.referrer_id).first()
            if referrer: referrer.balance += INVITE_REWARD; referrer.successful_referrals += 1
            await ptb_app.bot.send_message(user.referrer_id, f"🎉 Your referred ally {user.id} completed their first task! You earned ₱{INVITE_REWARD:.2f}!")
    
    db_session.commit()
    await query.edit_message_caption(caption=f"{query.message.caption.text}\n\n**Status: APPROVED**", parse_mode='Markdown')
    await ptb_app.bot.send_message(chat_id=user.id, text=f"🎉 Your submission for '{task.description}' was approved! You earned ₱{task.reward:.2f}.")
    await review_submissions(update, context) # Show next pending submission


async def reject_submission_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data['submission_id_to_reject'] = int(update.callback_query.data.split("_")[3])
    await update.callback_query.answer(); await update.callback_query.message.reply_text("Please provide a brief reason for rejecting this submission (or send /skip)."); return SUBMIT_TASK_REJECT_REASON

async def get_submission_rejection_reason(update: Update, context: ContextTypes.DEFAULT_TYPE):
    sub_id = context.user_data['submission_id_to_reject']; submission = db_session.query(TaskSubmission).filter(TaskSubmission.id == sub_id).first()
    if not submission or submission.status != 'pending': await update.message.reply_text("Already processed."); return ConversationHandler.END
    reason = "No reason provided." if update.message.text.lower() == '/skip' else update.message.text
    submission.status = 'rejected'; db_session.commit()
    await update.message.reply_text(f"❌ Submission #{sub_id} has been rejected.")
    task = db_session.query(Task).filter(Task.id == submission.task_id).first()
    await ptb_app.bot.send_message(chat_id=submission.user_id, text=f"⚠️ Your submission for '{task.description}' was rejected.\n\n**Admin's Remark:** {reason}", parse_mode='Markdown')
    await review_submissions(update, context) # Show next pending submission
    return ConversationHandler.END

# --- Withdrawal Handlers ---
async def approve_withdrawal(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query; await query.answer()
    wd_id = int(query.data.split("_")[2]); withdrawal = db_session.query(Withdrawal).filter(Withdrawal.id == wd_id).first()
    if not withdrawal or withdrawal.status != "pending": await query.edit_message_text("Request already processed."); return
    withdrawal.status = "approved"; db_session.commit(); await query.edit_message_text(f"✅ Request #{wd_id} approved.")
    await ptb_app.bot.send_message(chat_id=withdrawal.user_id, text=f"🎉 Good news! Your withdrawal of ₱{withdrawal.amount:.2f} has been approved and sent.")

async def reject_withdrawal_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data['withdrawal_id_to_reject'] = int(update.callback_query.data.split("_")[3])
    await update.callback_query.answer(); await update.callback_query.message.reply_text("Please provide a brief reason for rejecting this withdrawal (or send /skip)."); return REJECT_REASON_WD

async def get_withdrawal_rejection_reason(update: Update, context: ContextTypes.DEFAULT_TYPE):
    wd_id = context.user_data['withdrawal_id_to_reject']; withdrawal = db_session.query(Withdrawal).filter(Withdrawal.id == wd_id).first()
    if not withdrawal or withdrawal.status != 'pending': await update.message.reply_text("Already processed."); return ConversationHandler.END
    reason = "No reason provided." if update.message.text.lower() == '/skip' else update.message.text
    user = db_session.query(User).filter(User.id == withdrawal.user_id).first(); user.balance += withdrawal.amount; withdrawal.status = "rejected"; db_session.commit()
    await update.message.reply_text(f"❌ Request #{wd_id} has been rejected.")
    await ptb_app.bot.send_message(chat_id=user.id, text=f"⚠️ Your withdrawal of ₱{withdrawal.amount:.2f} was rejected and the amount returned to your balance.\n\n**Admin's Remark:** {reason}", parse_mode='Markdown')
    return ConversationHandler.END

# --- Maintenance Mode Toggle ---
async def maintenance_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query; await query.answer()
    maintenance_info = db_session.query(SystemInfo).filter(SystemInfo.key == 'withdrawal_maintenance').first()
    current_status_text = "ENABLED ✅" if maintenance_info and maintenance_info.value == "true" else "DISABLED ❌"
    toggle_button_text = f"Turn {'OFF' if current_status_text.startswith('ENABLED') else 'ON'}"
    keyboard = [[InlineKeyboardButton(toggle_button_text, callback_data="toggle_maintenance")]]
    await query.message.edit_text(f"Withdrawal Maintenance is currently {current_status_text}", reply_markup=InlineKeyboardMarkup(keyboard))
    
async def toggle_maintenance_mode(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query; await query.answer()
    maintenance_info = db_session.query(SystemInfo).filter(SystemInfo.key == 'withdrawal_maintenance').first()
    if not maintenance_info: maintenance_info = SystemInfo(key='withdrawal_maintenance', value='false')

    new_status = "true" if maintenance_info.value == "false" else "false"
    maintenance_info.value = new_status
    db_session.add(maintenance_info); db_session.commit()

    status_text = "ENABLED ✅" if new_status == "true" else "DISABLED ❌"
    await query.message.edit_text(f"Withdrawal Maintenance is now {status_text}", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton(f"Toggle {'OFF' if new_status=='true' else 'ON'}", callback_data="toggle_maintenance")]]))


# --- Warn User Conversation ---
async def warn_user_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query; await query.answer(); await query.message.reply_text("Send the User ID to warn:"); return WARN_USER_ID
async def get_warn_user_id(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try: context.user_data['warn_user_id'] = int(update.message.text)
    except ValueError: await update.message.reply_text("Invalid User ID."); return WARN_USER_ID
    await update.message.reply_text("Send the warning message/reason:"); return WARN_REASON
async def send_warn_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = context.user_data['warn_user_id']; reason = update.message.text
    user_db = db_session.query(User).filter(User.id == user_id).first()
    if user_db:
        await ptb_app.bot.send_message(user_id, f"⚠️ **Warning from Admin:** {reason}", parse_mode='Markdown')
        await update.message.reply_text(f"Warning sent to user {user_id}.")
    else:
        await update.message.reply_text(f"User {user_id} not found.")
    return ConversationHandler.END


# --- Add Handlers to the PTB Application ---
ptb_app.add_handler(CommandHandler("start", start_command))
ptb_app.add_handler(CommandHandler("admin", admin_command))

# Conversations
ptb_app.add_handler(ConversationHandler(entry_points=[CallbackQueryHandler(broadcast_start, pattern="^admin_broadcast$")], states={BROADCAST_MESSAGE: [MessageHandler(filters.ALL & ~filters.COMMAND, broadcast_message)]}, fallbacks=[], per_user=True))
ptb_app.add_handler(ConversationHandler(entry_points=[CallbackQueryHandler(announcement_start, pattern="^admin_set_announcement$")], states={ANNOUNCEMENT_TEXT: [MessageHandler(filters.TEXT & ~filters.COMMAND, set_announcement_text)]}, fallbacks=[], per_user=True))
ptb_app.add_handler(ConversationHandler(entry_points=[CallbackQueryHandler(add_task_start, pattern="^add_task_start$")], states={TASK_DESC: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_task_description)], TASK_LINK: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_task_link)], TASK_REWARD: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_task_reward)]}, fallbacks=[], per_user=True))
ptb_app.add_handler(ConversationHandler(entry_points=[CallbackQueryHandler(add_code_start, pattern="^add_code_start$")], states={NEW_CODE_CODE: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_new_code_code)], NEW_CODE_REWARD: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_new_code_reward)], NEW_CODE_USES: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_new_code_uses)]}, fallbacks=[], per_user=True))
ptb_app.add_handler(ConversationHandler(entry_points=[CallbackQueryHandler(user_mgt_start, pattern="^admin_user_mgt$")], states={USER_MGT_ID: [MessageHandler(filters.TEXT & ~filters.COMMAND, user_mgt_id_input)], USER_MGT_DURATION: [MessageHandler(filters.TEXT & ~filters.COMMAND, user_mgt_duration_input)]}, fallbacks=[], per_user=True))
ptb_app.add_handler(ConversationHandler(entry_points=[CallbackQueryHandler(rain_start, pattern="^admin_rain$")], states={RAIN_AMOUNT: [MessageHandler(filters.TEXT & ~filters.COMMAND, rain_amount_input)], RAIN_USERS: [MessageHandler(filters.TEXT & ~filters.COMMAND, rain_users_input)]}, fallbacks=[], per_user=True))
ptb_app.add_handler(ConversationHandler(entry_points=[CallbackQueryHandler(reject_withdrawal_start, pattern=r"^reject_wd_start_\d+$")], states={REJECT_REASON_WD: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_withdrawal_rejection_reason)]}, fallbacks=[], per_user=True))
ptb_app.add_handler(ConversationHandler(entry_points=[CallbackQueryHandler(reject_submission_start, pattern=r"^reject_sub_start_\d+$")], states={SUBMIT_TASK_REJECT_REASON: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_submission_rejection_reason)]}, fallbacks=[], per_user=True))
ptb_app.add_handler(ConversationHandler(entry_points=[CallbackQueryHandler(warn_user_start, pattern="^admin_warn_user$")], states={WARN_USER_ID: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_warn_user_id)], WARN_REASON: [MessageHandler(filters.TEXT & ~filters.COMMAND, send_warn_message)]}, fallbacks=[], per_user=True))


# Callback Query Handlers (non-conversation specific)
ptb_app.add_handler(CallbackQueryHandler(admin_stats, pattern="^admin_stats$"))
ptb_app.add_handler(CallbackQueryHandler(manage_tasks, pattern="^admin_manage_tasks$"))
ptb_app.add_handler(CallbackQueryHandler(remove_task_list, pattern="^remove_task_list$"))
ptb_app.add_handler(CallbackQueryHandler(delete_task_callback, pattern=r"^delete_task_\d+$"))
ptb_app.add_handler(CallbackQueryHandler(manage_codes, pattern="^admin_manage_codes$"))
ptb_app.add_handler(CallbackQueryHandler(remove_code_list, pattern="^remove_code_list$"))
ptb_app.add_handler(CallbackQueryHandler(delete_code_callback, pattern=r"^delete_code_\d+$"))
ptb_app.add_handler(CallbackQueryHandler(user_mgt_action_callback, pattern=r"^user_mgt_(ban|unban|restrict)$"))
ptb_app.add_handler(CallbackQueryHandler(review_submissions, pattern="^admin_pending_submissions$"))
ptb_app.add_handler(CallbackQueryHandler(approve_submission, pattern=r"^approve_sub_\d+$"))
ptb_app.add_handler(CallbackQueryHandler(approve_withdrawal, pattern=r"^approve_wd_\d+$"))
ptb_app.add_handler(CallbackQueryHandler(maintenance_start, pattern="^admin_maintenance$"))
ptb_app.add_handler(CallbackQueryHandler(toggle_maintenance_mode, pattern="^toggle_maintenance$"))


# --- Main Entry ---
if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8000))
    uvicorn.run("main:app", host="0.0.0.0", port=port, reload=False)```