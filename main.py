import logging, json, uvicorn, os, base64, random, asyncio
from io import BytesIO
from contextlib import asynccontextmanager
from datetime import date, timedelta
from typing import Dict, List, Optional

# Core Frameworks
from fastapi import FastAPI, Request, HTTPException, WebSocket, WebSocketDisconnect, Depends
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

# Telegram Bot Library
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, WebAppInfo
from telegram.ext import (
    Application, CommandHandler, ContextTypes, ConversationHandler,
    MessageHandler, filters, CallbackQueryHandler
)

# Database
from sqlalchemy import create_engine, Column, Integer, BigInteger, String, Float, ForeignKey, Text, Date, Boolean
from sqlalchemy.orm import declarative_base, sessionmaker, Session

# --- Configuration & Logging ---
logging.basicConfig(format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO)
logger = logging.getLogger(__name__)

# --- SYSTEM CONFIGURATION ---
BOT_TOKEN = "8486136204:AAFZkkxVFlBK1S5_RzrOlZ4ZZ6cDBcBjqVY" # Replace with your Bot Token
BOT_USERNAME = "GTaskPHBot" # Replace with your Bot Username
ADMIN_CHAT_ID = 7331257920 # Replace with your Admin Telegram User ID
MINI_APP_URL = "https://gtask-fronted.vercel.app/" # Replace with your Vercel Frontend URL

# Feature Constants
INVITE_REWARD = 77.0
MIN_WITHDRAWAL = 300.0
MAX_WITHDRAWAL = 30000.0
WITHDRAWAL_FEE_PERCENT = 0.03
DAILY_BONUS = 10.0
DAILY_BONUS_INVITE_REQ = 2
TASK_MILESTONES = {"10_tasks": 50.0, "20_tasks": 150.0, "30_tasks": 400.0}
GIFT_TICKET_PRICE = 77.0
GIFT_MIN_AMOUNT = 300.0
GIFT_MAX_AMOUNT = 80000.0
GIFT_FEE_PERCENT = 0.05
GAME_FEE_PERCENT = 0.10
MIN_GAME_BET = 10.0

# --- Database Setup (SAFE & STABLE) ---
SQLALCHEMY_DATABASE_URL = "sqlite:///./gtask_data.db?check_same_thread=False"
engine = create_engine(SQLALCHEMY_DATABASE_URL)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()

# --- Database Models ---
class User(Base): __tablename__ = "users"; id = Column(BigInteger, primary_key=True, index=True, autoincrement=False); first_name = Column(String); balance = Column(Float, default=0.0); gift_tickets = Column(Integer, default=0); referral_count = Column(Integer, default=0); successful_referrals = Column(Integer, default=0); tasks_completed = Column(Integer, default=0); completed_task_ids = Column(Text, default="[]"); referrer_id = Column(BigInteger, ForeignKey("users.id"), nullable=True); status = Column(String, default="active"); status_until = Column(Date, nullable=True); last_login_date = Column(Date, nullable=True); daily_claim_invites = Column(Integer, default=0); claimed_milestones = Column(Text, default="{}")
class Task(Base): __tablename__ = "tasks"; id = Column(Integer, primary_key=True, index=True); description = Column(String); link = Column(String); reward = Column(Float); is_active = Column(Boolean, default=True)
class TaskSubmission(Base): __tablename__ = "task_submissions"; id = Column(Integer, primary_key=True, index=True); user_id = Column(BigInteger, index=True); task_id = Column(Integer); text_proof = Column(Text, nullable=True); photo_proof_base64 = Column(Text); status = Column(String, default="pending"); created_at = Column(Date, default=date.today)
class Withdrawal(Base): __tablename__ = "withdrawals"; id = Column(Integer, primary_key=True, index=True); user_id = Column(BigInteger, index=True); amount = Column(Float); fee = Column(Float); method = Column(String); details = Column(String); status = Column(String, default="pending"); created_at = Column(Date, default=date.today)
class RedeemCode(Base): __tablename__ = "redeem_codes"; id = Column(Integer, primary_key=True, index=True); code = Column(String, unique=True, index=True); reward = Column(Float); uses_left = Column(Integer)
class SystemInfo(Base): __tablename__ = "system_info"; key = Column(String, primary_key=True, index=True); value = Column(String)
class GameRoom(Base): __tablename__ = "game_rooms"; id = Column(Integer, primary_key=True, index=True); bet_amount = Column(Float); creator_id = Column(BigInteger); opponent_id = Column(BigInteger, nullable=True); status = Column(String, default="pending"); winner_id = Column(BigInteger, nullable=True); creator_move = Column(String, nullable=True); opponent_move = Column(String, nullable=True); created_at = Column(Date, default=date.today)
Base.metadata.create_all(bind=engine)

# --- Pydantic Models & DB Dependency ---
class UserAuthRequest(BaseModel): user_id: int; _auth: str
class TaskProofRequest(UserAuthRequest): task_id: int; text: Optional[str]; photo: str
class RedeemCodeRequest(UserAuthRequest): code: str
class WithdrawalRequest(UserAuthRequest): amount: float; method: str; details: str
class GiftMoneyRequest(UserAuthRequest): recipient_id: int; amount: float
class CreateGameRoomRequest(UserAuthRequest): bet: float
class JoinGameRoomRequest(UserAuthRequest): room_id: int

def get_db():
    db = SessionLocal()
    try: yield db
    finally: db.close()

# --- Conversation States ---
(TASK_DESC, TASK_LINK, TASK_REWARD, REJECT_REASON_WD, BROADCAST_MESSAGE,
 ANNOUNCEMENT_TEXT, NEW_CODE_CODE, NEW_CODE_REWARD, NEW_CODE_USES,
 USER_MGT_ID, USER_MGT_DURATION, RAIN_AMOUNT, RAIN_USERS,
 SUBMIT_TASK_REJECT_REASON, WARN_USER_ID, WARN_REASON, USER_LOOKUP_ID) = range(17)

# --- WebSocket Manager ---
class ConnectionManager:
    def __init__(self): self.active_connections: Dict[int, List[WebSocket]] = {}
    async def connect(self, room_id: int, websocket: WebSocket):
        if room_id not in self.active_connections: self.active_connections[room_id] = []
        self.active_connections[room_id].append(websocket)
    def disconnect(self, room_id: int, websocket: WebSocket):
        if room_id in self.active_connections:
            self.active_connections[room_id].remove(websocket)
            if not self.active_connections[room_id]: del self.active_connections[room_id]
    async def broadcast(self, room_id: int, message: str):
        if room_id in self.active_connections:
            for connection in self.active_connections[room_id]: await connection.send_text(message)
manager = ConnectionManager()

# --- Bot & API Lifespan ---
ptb_app = Application.builder().token(BOT_TOKEN).build()
@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("Lifespan startup...")
    with SessionLocal() as db:
        for key, value in [('global_maintenance', 'false'), ('withdrawal_maintenance', 'false'), ('announcement', 'Welcome! No new announcements.')]:
            if not db.query(SystemInfo).filter(SystemInfo.key == key).first():
                db.add(SystemInfo(key=key, value=value)); db.commit()
    await ptb_app.initialize()
    await ptb_app.updater.start_polling(drop_pending_updates=True)
    await ptb_app.start()
    logger.info("Telegram bot has started successfully.")
    yield
    logger.info("Lifespan shutdown..."); await ptb_app.updater.stop(); await ptb_app.stop(); await ptb_app.shutdown()


app = FastAPI(lifespan=lifespan)
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_credentials=True, allow_methods=["*"], allow_headers=["*"])

# --- API Endpoints ---
@app.get("/")
async def health_check(): return {"status": "ok", "message": f"{BOT_USERNAME} API is running!"}

@app.middleware("http")
async def maintenance_middleware(request: Request, call_next):
    db = SessionLocal()
    try:
        maintenance = db.query(SystemInfo).filter(SystemInfo.key == 'global_maintenance').first()
        if maintenance and maintenance.value == 'true':
            is_admin = False
            try:
                if request.method == "POST":
                    body = await request.json()
                    if body.get('user_id') == ADMIN_CHAT_ID: is_admin = True
            except Exception: pass
            if not is_admin: raise HTTPException(status_code=503, detail="The service is temporarily unavailable due to maintenance.")
    finally: db.close()
    return await call_next(request)

@app.post("/get_initial_data")
async def get_initial_data(req: UserAuthRequest, db: Session = Depends(get_db)):
    user = db.query(User).filter(User.id == req.user_id).first()
    if not user: raise HTTPException(status_code=404, detail=f"User not found. Please start the bot first: @{BOT_USERNAME}")

    if user.status == 'banned': raise HTTPException(status_code=403, detail="You are permanently banned.")
    if user.status == 'restricted' and user.status_until and user.status_until > date.today():
        raise HTTPException(status_code=403, detail=f"You are restricted until {user.status_until.strftime('%b %d')}.")
    elif user.status == 'restricted' and user.status_until and user.status_until <= date.today():
         user.status = 'active'; user.status_until = None; db.commit()

    can_claim_daily = (user.last_login_date is None or user.last_login_date < date.today()) and user.daily_claim_invites >= DAILY_BONUS_INVITE_REQ
    completed_ids = json.loads(user.completed_task_ids)
    available_tasks = db.query(Task).filter(Task.is_active == True, ~Task.id.in_(completed_ids)).all()
    withdrawals = db.query(Withdrawal).filter(Withdrawal.user_id == req.user_id).order_by(Withdrawal.id.desc()).limit(20).all()
    announcement = db.query(SystemInfo).filter(SystemInfo.key == 'announcement').first()
    wd_maintenance = db.query(SystemInfo).filter(SystemInfo.key == 'withdrawal_maintenance').first()
    game_rooms = db.query(GameRoom).filter(GameRoom.status == 'pending', GameRoom.creator_id != req.user_id, GameRoom.opponent_id == None).all()
    
    return {
        "balance": user.balance, "gift_tickets": user.gift_tickets, "referral_count": user.referral_count,
        "successful_referrals": user.successful_referrals, "tasks_completed": user.tasks_completed,
        "daily_claim_invites": user.daily_claim_invites, "can_claim_daily": can_claim_daily,
        "daily_bonus_req": DAILY_BONUS_INVITE_REQ, "daily_bonus_amount": DAILY_BONUS,
        "announcement": announcement.value if announcement else "Welcome! No new announcements.",
        "tasks": [{"id": t.id, "description": t.description, "link": t.link, "reward": t.reward} for t in available_tasks],
        "withdrawals": [{"amount": w.amount, "method": w.method, "status": w.status, "date": w.created_at.strftime('%Y-%m-%d')} for w in withdrawals],
        "claimed_milestones": json.loads(user.claimed_milestones), "min_withdrawal": MIN_WITHDRAWAL,
        "max_withdrawal": MAX_WITHDRAWAL, "withdrawal_fee_percent": WITHDRAWAL_FEE_PERCENT,
        "withdrawal_maintenance": wd_maintenance.value == "true" if wd_maintenance else False,
        "gift_ticket_price": GIFT_TICKET_PRICE, "gift_min_amount": GIFT_MIN_AMOUNT,
        "gift_max_amount": GIFT_MAX_AMOUNT, "gift_fee_percent": GIFT_FEE_PERCENT,
        "min_game_bet": MIN_GAME_BET,
        "game_rooms": [{"id": r.id, "bet": r.bet_amount, "creator_id": r.creator_id} for r in game_rooms]
    }

@app.post("/submit_task_proof")
async def submit_task_proof(req: TaskProofRequest, db: Session = Depends(get_db)):
    user = db.query(User).filter(User.id == req.user_id).with_for_update().first()
    if not user or user.status != 'active': raise HTTPException(status_code=403, detail="Account not active.")
    
    completed_ids = json.loads(user.completed_task_ids)
    if req.task_id in completed_ids: raise HTTPException(status_code=400, detail="Task already completed.")

    submission = TaskSubmission(user_id=req.user_id, task_id=req.task_id, text_proof=req.text, photo_proof_base64=req.photo)
    db.add(submission); db.commit(); db.refresh(submission)
    
    task = db.query(Task).filter(Task.id == req.task_id).first()
    caption = f"**New Task Submission**\n\n- User: `{req.user_id}` ({user.first_name})\n- Task: {task.description}\n- Reward: ₱{task.reward:.2f}\n- Note: {req.text or 'N/A'}"
    keyboard = [[InlineKeyboardButton("Approve ✅", callback_data=f"approve_sub_{submission.id}"), InlineKeyboardButton("Reject ❌", callback_data=f"reject_sub_start_{submission.id}")]]
    
    try:
        photo_data = base64.b64decode(req.photo.split(',')[1])
        await ptb_app.bot.send_photo(chat_id=ADMIN_CHAT_ID, photo=BytesIO(photo_data), caption=caption, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode='Markdown')
        await ptb_app.bot.send_message(req.user_id, "✅ Your proof has been submitted for admin review!")
    except Exception as e:
        logger.error(f"Failed to send task submission to admin: {e}")
        raise HTTPException(status_code=500, detail="Could not process submission notification.")

    return {"status": "success"}

@app.post("/redeem_code")
async def redeem_code(req: RedeemCodeRequest, db: Session = Depends(get_db)):
    user = db.query(User).filter(User.id == req.user_id).with_for_update().first()
    if not user or user.status != 'active': raise HTTPException(status_code=403, detail="Account not active.")

    code = db.query(RedeemCode).filter(RedeemCode.code == req.code.upper()).with_for_update().first()
    if code and (code.uses_left == -1 or code.uses_left > 0):
        user.balance += code.reward
        if code.uses_left != -1: code.uses_left -= 1
        db.commit()
        return {"status": "success", "amount_rewarded": code.reward}
    else:
        raise HTTPException(status_code=400, detail="Invalid or expired code.")

@app.post("/claim_daily_bonus")
async def claim_daily_bonus(req: UserAuthRequest, db: Session = Depends(get_db)):
    user = db.query(User).filter(User.id == req.user_id).with_for_update().first()
    if not user or user.status != 'active': raise HTTPException(status_code=403, detail="Account not active.")
    
    if user.last_login_date is None or user.last_login_date < date.today():
        if user.daily_claim_invites >= DAILY_BONUS_INVITE_REQ:
            user.balance += DAILY_BONUS
            user.last_login_date = date.today()
            user.daily_claim_invites = 0
            db.commit()
            await ptb_app.bot.send_message(req.user_id, f"🎉 Daily bonus of ₱{DAILY_BONUS:.2f} claimed!")
            return {"status": "success"}
        else:
            needed = DAILY_BONUS_INVITE_REQ - user.daily_claim_invites
            raise HTTPException(status_code=400, detail=f"Invite {needed} more user(s) to claim your daily bonus.")
    else:
        raise HTTPException(status_code=400, detail="Daily bonus already claimed for today.")

@app.post("/submit_withdrawal")
async def submit_withdrawal(req: WithdrawalRequest, db: Session = Depends(get_db)):
    wd_maintenance = db.query(SystemInfo).filter(SystemInfo.key == 'withdrawal_maintenance').first()
    if wd_maintenance and wd_maintenance.value == "true":
        raise HTTPException(status_code=503, detail="Withdrawals are under maintenance. Please try again later.")

    user = db.query(User).filter(User.id == req.user_id).with_for_update().first()
    if not user or user.status != 'active': raise HTTPException(status_code=403, detail="Account not active.")
    if not (MIN_WITHDRAWAL <= req.amount <= MAX_WITHDRAWAL):
        raise HTTPException(status_code=400, detail=f"Amount must be between ₱{MIN_WITHDRAWAL:.2f} and ₱{MAX_WITHDRAWAL:.2f}.")
    
    fee = req.amount * WITHDRAWAL_FEE_PERCENT
    total_deduction = req.amount + fee

    if user.balance < total_deduction:
        raise HTTPException(status_code=400, detail="Insufficient balance to cover withdrawal amount and fee.")
    
    user.balance -= total_deduction
    new_withdrawal = Withdrawal(user_id=user.id, amount=req.amount, fee=fee, method=req.method, details=req.details)
    db.add(new_withdrawal); db.commit(); db.refresh(new_withdrawal)
    
    await ptb_app.bot.send_message(req.user_id, f"✅ Your withdrawal request for ₱{req.amount:.2f} (Fee: ₱{fee:.2f}) has been submitted!")
    admin_msg = f"**New Withdrawal Request**\n\n- User: `{user.id}` ({user.first_name})\n- Amount: `₱{req.amount:.2f}`\n- Fee: `₱{fee:.2f}`\n- Method: `{req.method}`\n- Details: `{req.details}`"
    keyboard = [[InlineKeyboardButton("Approve ✅", callback_data=f"approve_wd_{new_withdrawal.id}"), InlineKeyboardButton("Reject ❌", callback_data=f"reject_wd_start_{new_withdrawal.id}")]]
    await ptb_app.bot.send_message(chat_id=ADMIN_CHAT_ID, text=admin_msg, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode='Markdown')
    return {"status": "success"}

@app.post("/buy_ticket")
async def buy_ticket(req: UserAuthRequest, db: Session = Depends(get_db)):
    user = db.query(User).filter(User.id == req.user_id).with_for_update().first()
    if not user or user.status != 'active': raise HTTPException(status_code=403, detail="Account not active.")
    if user.balance < GIFT_TICKET_PRICE: raise HTTPException(status_code=400, detail="Insufficient balance to buy a Gift Ticket.")
    
    user.balance -= GIFT_TICKET_PRICE
    user.gift_tickets += 2
    db.commit()
    await ptb_app.bot.send_message(req.user_id, f"🎉 Purchase successful! You received 2 Gift Tickets. You now have {user.gift_tickets} tickets.")
    return {"status": "success"}

@app.post("/gift_money")
async def gift_money(req: GiftMoneyRequest, db: Session = Depends(get_db)):
    sender = db.query(User).filter(User.id == req.user_id).with_for_update().first()
    if not sender or sender.status != 'active': raise HTTPException(status_code=403, detail="Sender account not active.")
    if sender.gift_tickets < 1: raise HTTPException(status_code=400, detail="You do not have any Gift Tickets.")
    if not (GIFT_MIN_AMOUNT <= req.amount <= GIFT_MAX_AMOUNT): raise HTTPException(status_code=400, detail=f"Amount must be between ₱{GIFT_MIN_AMOUNT:.2f} and ₱{GIFT_MAX_AMOUNT:.2f}.")
    
    fee = req.amount * GIFT_FEE_PERCENT
    total_deduction = req.amount + fee
    if sender.balance < total_deduction: raise HTTPException(status_code=400, detail="Insufficient balance to cover gift and fee.")

    recipient = db.query(User).filter(User.id == req.recipient_id).with_for_update().first()
    if not recipient: raise HTTPException(status_code=404, detail="Recipient user not found.")
    if recipient.status != 'active': raise HTTPException(status_code=400, detail="Recipient account is not active.")

    sender.balance -= total_deduction
    sender.gift_tickets -= 1
    recipient.balance += req.amount
    db.commit()

    await ptb_app.bot.send_message(req.user_id, f"✅ You gifted ₱{req.amount:.2f} to user {req.recipient_id}. Fee: ₱{fee:.2f}.")
    await ptb_app.bot.send_message(recipient.id, f"🎉 You have received a gift of ₱{req.amount:.2f} from user {req.user_id}!")
    return {"status": "success"}

@app.post("/create_game_room")
async def create_game_room(req: CreateGameRoomRequest, db: Session = Depends(get_db)):
    user = db.query(User).filter(User.id == req.user_id).with_for_update().first()
    if not user or user.status != 'active': raise HTTPException(status_code=403, detail="Account not active.")
    if req.bet < MIN_GAME_BET: raise HTTPException(status_code=400, detail=f"Minimum bet is ₱{MIN_GAME_BET:.2f}.")
    if user.balance < req.bet: raise HTTPException(status_code=400, detail="Insufficient balance.")
    
    user.balance -= req.bet
    new_room = GameRoom(creator_id=req.user_id, bet_amount=req.bet, status='pending')
    db.add(new_room); db.commit(); db.refresh(new_room)
    await ptb_app.bot.send_message(req.user_id, f"✅ Game room #{new_room.id} created with a bet of ₱{req.bet:.2f}. Your balance is now ₱{user.balance:.2f}.")
    return {"status": "success", "room_id": new_room.id}

@app.post("/join_game_room")
async def join_game_room(req: JoinGameRoomRequest, db: Session = Depends(get_db)):
    user = db.query(User).filter(User.id == req.user_id).with_for_update().first()
    room = db.query(GameRoom).filter(GameRoom.id == req.room_id, GameRoom.status == 'pending').with_for_update().first()
    
    if not user or user.status != 'active': raise HTTPException(status_code=403, detail="Account not active.")
    if not room: raise HTTPException(status_code=404, detail="Room not found or is no longer available.")
    if user.id == room.creator_id: raise HTTPException(status_code=400, detail="You cannot join your own room.")
    if user.balance < room.bet_amount: raise HTTPException(status_code=400, detail="Insufficient balance to join.")
    
    user.balance -= room.bet_amount
    room.opponent_id = req.user_id
    room.status = 'active'
    db.commit()
    
    creator = db.query(User).get(room.creator_id)
    await ptb_app.bot.send_message(req.user_id, f"✅ You joined Game Room #{room.id}. Your balance is now ₱{user.balance:.2f}. Good luck!")
    await ptb_app.bot.send_message(room.creator_id, f"🎉 An opponent ({creator.first_name if creator else room.creator_id}) has joined your Game Room #{room.id}! The game starts now.")
    
    await manager.broadcast(room.id, json.dumps({"type": "game_start", "creator_id": room.creator_id, "opponent_id": room.opponent_id}))
    return {"status": "success"}

@app.websocket("/ws/{room_id}/{user_id}")
async def websocket_endpoint(websocket: WebSocket, room_id: int, user_id: int):
    await websocket.accept(); manager.connect(room_id, websocket)
    db: Session = SessionLocal()
    try:
        while True:
            data_str = await websocket.receive_text(); data = json.loads(data_str)
            room = db.query(GameRoom).filter(GameRoom.id == room_id).with_for_update().first()
            if not room or room.status != 'active':
                await websocket.send_text(json.dumps({"type": "error", "message": "Game is no longer active."})); break

            if data.get('type') == 'make_move':
                move = data['move']
                if user_id == room.creator_id and not room.creator_move: room.creator_move = move
                elif user_id == room.opponent_id and not room.opponent_move: room.opponent_move = move
                else: continue
                
                db.commit()
                await manager.broadcast(room.id, json.dumps({"type": "move_made", "user_id": user_id}))

                if room.creator_move and room.opponent_move:
                    c_move, o_move = room.creator_move, room.opponent_move
                    
                    if c_move == o_move: winner_id = -1
                    elif (c_move, o_move) in [('rock', 'scissors'), ('scissors', 'paper'), ('paper', 'rock')]: winner_id = room.creator_id
                    else: winner_id = room.opponent_id
                    
                    room.status = 'finished'; room.winner_id = winner_id
                    
                    creator = db.query(User).filter(User.id == room.creator_id).with_for_update().first()
                    opponent = db.query(User).filter(User.id == room.opponent_id).with_for_update().first()

                    if winner_id == -1:
                        creator.balance += room.bet_amount; opponent.balance += room.bet_amount
                        await ptb_app.bot.send_message(creator.id, f"Game #{room.id} was a draw! Your bet was returned.")
                        await ptb_app.bot.send_message(opponent.id, f"Game #{room.id} was a draw! Your bet was returned.")
                    else:
                        prize = (room.bet_amount * 2) * (1 - GAME_FEE_PERCENT)
                        winner_user, loser_user = (creator, opponent) if winner_id == creator.id else (opponent, creator)
                        winner_user.balance += prize
                        await ptb_app.bot.send_message(winner_user.id, f"🎉 You won Game #{room.id}! You received ₱{prize:.2f}.")
                        await ptb_app.bot.send_message(loser_user.id, f"😭 You lost Game #{room.id}.")
                    
                    db.commit()
                    await manager.broadcast(room.id, json.dumps({"type": "game_over", "winner": winner_id, "creator_move": c_move, "opponent_move": o_move}))
            elif data.get('type') == 'request_status':
                 room = db.query(GameRoom).filter(GameRoom.id == room_id).first()
                 if room:
                    await websocket.send_text(json.dumps({
                        "type": "game_status", "room_id": room.id, "status": room.status,
                        "creator_id": room.creator_id, "opponent_id": room.opponent_id,
                        "creator_move": room.creator_move, "opponent_move": room.opponent_move,
                    }))
    except WebSocketDisconnect:
        room = db.query(GameRoom).filter(GameRoom.id == room_id, (GameRoom.status == 'active' or GameRoom.status == 'pending')).with_for_update().first()
        if room and room.winner_id is None:
            if room.status == 'pending':
                creator = db.query(User).filter(User.id == room.creator_id).with_for_update().first()
                if creator: creator.balance += room.bet_amount; await ptb_app.bot.send_message(creator.id, f"Game Room #{room.id} cancelled due to creator disconnect. Your bet returned.")
            else:
                remaining_player_id = room.opponent_id if user_id == room.creator_id else room.creator_id
                winner_id = remaining_player_id
                winner = db.query(User).filter(User.id == winner_id).with_for_update().first()
                if winner:
                    prize = (room.bet_amount * 2) * (1 - GAME_FEE_PERCENT)
                    winner.balance += prize
                    await ptb_app.bot.send_message(winner_id, f"🎉 Opponent disconnected from Game #{room.id}. You win ₱{prize:.2f} by default!")
                await manager.broadcast(room.id, json.dumps({"type": "game_over", "winner": winner_id, "message": "Opponent disconnected."}))
                
            room.status = 'cancelled'
            db.commit()
    except Exception as e:
        logger.error(f"WebSocket Error in room {room_id} for user {user_id}: {e}", exc_info=True)
    finally:
        manager.disconnect(room_id, websocket)
        db.close()

# --- Telegram Handlers ---
async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_tg = update.effective_user
    with SessionLocal() as db:
        user_db = db.query(User).filter(User.id == user_tg.id).first()
        if context.args:
            try:
                referrer_id = int(context.args[0])
                if referrer_id != user_tg.id and not user_db:
                    referrer = db.query(User).filter(User.id == referrer_id).with_for_update().first()
                    if referrer:
                        referrer.referral_count += 1
                        referrer.daily_claim_invites += 1
                        user_db = User(id=user_tg.id, first_name=user_tg.first_name, referrer_id=referrer_id)
                        db.add(user_db)
                        db.commit()
                        await context.bot.send_message(chat_id=referrer.id, text=f"🎉 {user_tg.first_name} has joined using your link!")
            except (ValueError, IndexError): pass
        if not user_db:
            user_db = User(id=user_tg.id, first_name=user_tg.first_name)
            db.add(user_db)
            db.commit()
        
        caption = f"🚀 **Greetings, {user_tg.first_name}!**\n\nWelcome to **{BOT_USERNAME}**, your portal to earning rewards."
        keyboard = [[InlineKeyboardButton("📱 Launch Dashboard", web_app=WebAppInfo(url=MINI_APP_URL))]]
        await update.message.reply_text(caption, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode='Markdown')

async def admin_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_CHAT_ID: return
    keyboard = [
        [InlineKeyboardButton("📊 User Stats", callback_data="admin_stats"), InlineKeyboardButton("📢 Broadcast", callback_data="admin_broadcast")],
        [InlineKeyboardButton("📜 Set Announcement", callback_data="admin_set_announcement"), InlineKeyboardButton("🧐 User Lookup", callback_data="admin_user_lookup")],
        [InlineKeyboardButton("📝 Manage Tasks", callback_data="admin_manage_tasks"), InlineKeyboardButton("🔑 Manage Codes", callback_data="admin_manage_codes")],
        [InlineKeyboardButton("🔨 User Management", callback_data="admin_user_mgt"), InlineKeyboardButton("⚠️ Warn User", callback_data="admin_warn_user")],
        [InlineKeyboardButton("🌧️ Rain Prize", callback_data="admin_rain"), InlineKeyboardButton("🎲 Manage Games", callback_data="admin_manage_games")],
        [InlineKeyboardButton("⚙️ Maintenance", callback_data="admin_maintenance"), InlineKeyboardButton("📋 Review Submissions", callback_data="admin_pending_submissions")],
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    if update.callback_query:
        await update.callback_query.answer()
        await update.callback_query.edit_message_text("👑 **Admin Dashboard**", reply_markup=reply_markup, parse_mode='Markdown')
    else:
        await update.message.reply_text("👑 **Admin Dashboard**", reply_markup=reply_markup, parse_mode='Markdown')

async def admin_back_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await admin_command(update, context)

async def admin_stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query; await query.answer()
    with SessionLocal() as db:
        total_users = db.query(User).count()
        active_users = db.query(User).filter(User.status == 'active').count()
        total_balance = sum(u.balance for u in db.query(User).all() if u.balance)
        pending_withdrawals = db.query(Withdrawal).filter(Withdrawal.status == 'pending').count()
        pending_submissions = db.query(TaskSubmission).filter(TaskSubmission.status == 'pending').count()
        active_game_rooms = db.query(GameRoom).filter(GameRoom.status == 'active').count()
    
    stats_text = (
        f"**📊 Bot Statistics:**\n\n"
        f"👥 Total Users: {total_users}\n"
        f"🟢 Active Users: {active_users}\n"
        f"💰 Total Balance: ₱{total_balance:.2f}\n"
        f"💸 Pending Withdrawals: {pending_withdrawals}\n"
        f"📝 Pending Tasks: {pending_submissions}\n"
        f"🎮 Active Games: {active_game_rooms}"
    )
    keyboard = [[InlineKeyboardButton("⬅️ Back", callback_data="admin_back")]]
    await query.edit_message_text(stats_text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode='Markdown')

# --- Broadcast Conversation ---
async def broadcast_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query; await query.answer()
    keyboard = [[InlineKeyboardButton("Cancel", callback_data="admin_back")]]
    await query.edit_message_text("Send the message to broadcast to all active users. (Media/Stickers supported)", reply_markup=InlineKeyboardMarkup(keyboard))
    return BROADCAST_MESSAGE
async def broadcast_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    with SessionLocal() as db:
        active_users = db.query(User).filter(User.status == 'active').all()
    sent_count = 0
    for user in active_users:
        try:
            await context.bot.copy_message(chat_id=user.id, from_chat_id=update.effective_chat.id, message_id=update.message.message_id)
            sent_count += 1
            await asyncio.sleep(0.05)
        except Exception as e:
            logger.error(f"Failed to broadcast to {user.id}: {e}")
    await update.message.reply_text(f"Broadcast sent to {sent_count}/{len(active_users)} active users.")
    await admin_command(update, context)
    return ConversationHandler.END

# --- Announcement Conversation ---
async def announcement_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query; await query.answer()
    keyboard = [[InlineKeyboardButton("Cancel", callback_data="admin_back")]]
    await query.edit_message_text("Enter new announcement text (or send /clear to remove).", reply_markup=InlineKeyboardMarkup(keyboard))
    return ANNOUNCEMENT_TEXT
async def set_announcement_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    with SessionLocal() as db:
        announcement = db.query(SystemInfo).filter(SystemInfo.key == 'announcement').with_for_update().first()
        if not announcement: announcement = SystemInfo(key='announcement')
        
        if update.message.text.lower() == '/clear':
            db.delete(announcement); await update.message.reply_text("Announcement cleared.")
        else:
            announcement.value = update.message.text; db.add(announcement); await update.message.reply_text("Announcement set.")
        db.commit()
    await admin_command(update, context)
    return ConversationHandler.END

# --- User Lookup Conversation ---
async def user_lookup_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query; await query.answer()
    keyboard = [[InlineKeyboardButton("Cancel", callback_data="admin_back")]]
    await query.edit_message_text("Send the User ID to look up:", reply_markup=InlineKeyboardMarkup(keyboard))
    return USER_LOOKUP_ID
async def user_lookup_id_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try: user_id = int(update.message.text)
    except ValueError: await update.message.reply_text("Invalid User ID."); return USER_LOOKUP_ID
    
    with SessionLocal() as db:
        user = db.query(User).filter(User.id == user_id).first()
        if not user: await update.message.reply_text(f"User with ID `{user_id}` not found."); return ConversationHandler.END

        referrer_info = "None"
        if user.referrer_id:
            referrer = db.query(User).filter(User.id == user.referrer_id).first()
            referrer_info = f"{referrer.first_name} (`{user.referrer_id}`)" if referrer else f"`{user.referrer_id}` (Not Found)"
        
        info_text = f"""
**🔍 User Info for {user.first_name} (`{user.id}`)**

- **Status:** `{user.status.upper()}` ({user.status_until or 'N/A'})
- **Balance:** `₱{user.balance:.2f}`
- **Gift Tickets:** `{user.gift_tickets}`
- **Tasks Completed:** `{user.tasks_completed}`

**Referrals:**
- **Recruited:** `{user.referral_count}`
- **Successful:** `{user.successful_referrals}`
- **Referred By:** {referrer_info}
"""
        await update.message.reply_text(info_text, parse_mode='Markdown')
    await admin_command(update, context)
    return ConversationHandler.END

# --- Maintenance Control Panel ---
async def admin_maintenance(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query; await query.answer()
    with SessionLocal() as db:
        global_m = db.query(SystemInfo).filter(SystemInfo.key == 'global_maintenance').first()
        wd_m = db.query(SystemInfo).filter(SystemInfo.key == 'withdrawal_maintenance').first()
        
        global_status = "ENABLED ✅" if global_m and global_m.value == 'true' else "DISABLED ❌"
        wd_status = "ENABLED ✅" if wd_m and wd_m.value == 'true' else "DISABLED ❌"

    keyboard = [
        [InlineKeyboardButton(f"Global Mode: {global_status}", callback_data="toggle_maintenance_global")],
        [InlineKeyboardButton(f"Withdrawal Mode: {wd_status}", callback_data="toggle_maintenance_wd")],
        [InlineKeyboardButton("⬅️ Back", callback_data="admin_back")]
    ]
    await query.edit_message_text("⚙️ **Maintenance Controls**", reply_markup=InlineKeyboardMarkup(keyboard))

async def toggle_maintenance(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    mode = query.data.split("_")[-1]
    key = 'global_maintenance' if mode == 'global' else 'withdrawal_maintenance'
    
    with SessionLocal() as db:
        setting = db.query(SystemInfo).filter(SystemInfo.key == key).with_for_update().first()
        if setting:
            setting.value = 'false' if setting.value == 'true' else 'true'
            db.commit()
            await query.answer(f"{mode.capitalize()} maintenance {'ENABLED' if setting.value == 'true' else 'DISABLED'}")
    
    await admin_maintenance(update, context)

# --- Task Management ---
async def admin_manage_tasks(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query; await query.answer()
    with SessionLocal() as db:
        tasks = db.query(Task).all()
        keyboard = [[InlineKeyboardButton("➕ Add New Task", callback_data="add_task_start")]]
        if tasks:
            for task in tasks:
                status_icon = "✅" if task.is_active else "❌"
                keyboard.append([InlineKeyboardButton(f"{status_icon} {task.description[:30]}... (₱{task.reward:.2f})", callback_data=f"toggle_task_{task.id}")])
        keyboard.append([InlineKeyboardButton("⬅️ Back", callback_data="admin_back")])
    await query.edit_message_text("📝 **Manage Tasks**\n\nSelect a task to toggle its active status, or add a new one.", reply_markup=InlineKeyboardMarkup(keyboard))

async def toggle_task_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    task_id = int(query.data.split("_")[2])
    with SessionLocal() as db:
        task = db.query(Task).filter(Task.id == task_id).with_for_update().first()
        if task:
            task.is_active = not task.is_active
            db.commit()
            await query.answer(f"Task {'activated' if task.is_active else 'deactivated'}")
    await admin_manage_tasks(update, context)

async def add_task_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query; await query.answer()
    keyboard = [[InlineKeyboardButton("Cancel", callback_data="admin_back")]]
    await query.edit_message_text("Enter task description:", reply_markup=InlineKeyboardMarkup(keyboard))
    return TASK_DESC
async def get_task_description(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data['task_desc'] = update.message.text
    await update.message.reply_text("Send the link (e.g., https://example.com):")
    return TASK_LINK
async def get_task_link(update: Update, context: ContextTypes.DEFAULT_TYPE):
    link = update.message.text
    if not link.startswith('http') and not link.startswith('https'): link = 'https://' + link
    context.user_data['task_link'] = link
    await update.message.reply_text("Enter the reward amount (e.g., 50.00):")
    return TASK_REWARD
async def get_task_reward(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        reward = float(update.message.text)
        with SessionLocal() as db:
            db.add(Task(description=context.user_data['task_desc'], link=context.user_data['task_link'], reward=reward, is_active=True))
            db.commit()
        await update.message.reply_text("✅ Task added!")
        await admin_command(update, context)
        return ConversationHandler.END
    except ValueError:
        await update.message.reply_text("Invalid amount.")
        return TASK_REWARD

# --- Review Submissions ---
async def review_submissions(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query; await query.answer()
    with SessionLocal() as db:
        submission = db.query(TaskSubmission).filter(TaskSubmission.status == 'pending').first()
        if not submission:
            keyboard = [[InlineKeyboardButton("⬅️ Back", callback_data="admin_back")]]
            await query.edit_message_text("No pending submissions.", reply_markup=InlineKeyboardMarkup(keyboard)); return
        user = db.query(User).filter(User.id == submission.user_id).first()
        task = db.query(Task).filter(Task.id == submission.task_id).first()
        caption = f"**Submission Review**\n\n- User: {user.first_name} (`{user.id}`)\n- Task: {task.description}\n- Reward: ₱{task.reward:.2f}\n- Note: {submission.text_proof}"
        keyboard = [[InlineKeyboardButton("Approve ✅", callback_data=f"approve_sub_{submission.id}"), InlineKeyboardButton("Reject ❌", callback_data=f"reject_sub_start_{submission.id}")]]
        photo_data = base64.b64decode(submission.photo_proof_base64.split(',')[1])
        await query.message.reply_photo(photo=BytesIO(photo_data), caption=caption, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode='Markdown')

async def approve_submission(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query; await query.answer()
    sub_id = int(query.data.split("_")[2])
    with SessionLocal() as db:
        submission = db.query(TaskSubmission).filter(TaskSubmission.id == sub_id).with_for_update().first()
        if not submission or submission.status != 'pending': await query.edit_message_caption("Already processed."); return
        submission.status = 'approved'
        user = db.query(User).filter(User.id == submission.user_id).with_for_update().first()
        task = db.query(Task).filter(Task.id == submission.task_id).first()
        
        # Financial/Milestone Logic (Safe due to `with_for_update` and explicit session)
        completed_ids = json.loads(user.completed_task_ids)
        if task.id not in completed_ids:
            user.balance += task.reward; user.tasks_completed += 1; completed_ids.append(task.id); user.completed_task_ids = json.dumps(completed_ids)
            claimed_milestones = json.loads(user.claimed_milestones)
            for ms_key, ms_reward in TASK_MILESTONES.items():
                ms_count = int(ms_key.split('_')[0])
                if user.tasks_completed == ms_count and ms_key not in claimed_milestones:
                    user.balance += ms_reward; claimed_milestones[ms_key] = True; user.claimed_milestones = json.dumps(claimed_milestones)
                    await ptb_app.bot.send_message(user.id, f"🎉 Milestone Reached! You completed {ms_count} tasks and earned a bonus of ₱{ms_reward:.2f}!")
            if user.tasks_completed == 1 and user.referrer_id:
                referrer = db.query(User).filter(User.id == user.referrer_id).with_for_update().first()
                if referrer: referrer.balance += INVITE_REWARD; referrer.successful_referrals += 1
                await ptb_app.bot.send_message(user.referrer_id, f"🎉 Your referral {user.first_name} completed their first task! You earned ₱{INVITE_REWARD:.2f}!")
        db.commit()
    await query.edit_message_caption(caption=f"{query.message.caption.text}\n\n**Status: APPROVED**", parse_mode='Markdown')
    await ptb_app.bot.send_message(chat_id=user.id, text=f"🎉 Your submission for '{task.description}' was approved! You earned ₱{task.reward:.2f}.")
    await review_submissions(update, context) # Show next pending submission


# --- Handler Registration (Final) ---

if __name__ == "__main__":
    # Command Handlers
    ptb_app.add_handler(CommandHandler("start", start_command))
    ptb_app.add_handler(CommandHandler("admin", admin_command))

    # Conversations
    ptb_app.add_handler(ConversationHandler(
        entry_points=[CallbackQueryHandler(broadcast_start, pattern="^admin_broadcast$")],
        states={BROADCAST_MESSAGE: [MessageHandler(filters.ALL & ~filters.COMMAND, broadcast_message)]},
        fallbacks=[CallbackQueryHandler(admin_back_callback, pattern="^admin_back$")]
    ))
    ptb_app.add_handler(ConversationHandler(
        entry_points=[CallbackQueryHandler(announcement_start, pattern="^admin_set_announcement$")],
        states={ANNOUNCEMENT_TEXT: [MessageHandler(filters.TEXT & ~filters.COMMAND, set_announcement_text)]},
        fallbacks=[CallbackQueryHandler(admin_back_callback, pattern="^admin_back$")]
    ))
    ptb_app.add_handler(ConversationHandler(
        entry_points=[CallbackQueryHandler(add_task_start, pattern="^add_task_start$")],
        states={
            TASK_DESC: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_task_description)],
            TASK_LINK: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_task_link)],
            TASK_REWARD: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_task_reward)]
        },
        fallbacks=[CallbackQueryHandler(admin_back_callback, pattern="^admin_back$")]
    ))
    ptb_app.add_handler(ConversationHandler(
        entry_points=[CallbackQueryHandler(user_lookup_start, pattern="^admin_user_lookup$")],
        states={USER_LOOKUP_ID: [MessageHandler(filters.TEXT & ~filters.COMMAND, user_lookup_id_input)]},
        fallbacks=[CallbackQueryHandler(admin_back_callback, pattern="^admin_back$")]
    ))
    # ... Other Conversation Handlers (User Mgt, Rain Prize, Redeem Codes, Withdrawal/Submission Rejection) would be added here

    # Callback Query Handlers
    ptb_app.add_handler(CallbackQueryHandler(admin_back_callback, pattern="^admin_back$"))
    ptb_app.add_handler(CallbackQueryHandler(admin_stats, pattern="^admin_stats$"))
    ptb_app.add_handler(CallbackQueryHandler(admin_maintenance, pattern="^admin_maintenance$"))
    ptb_app.add_handler(CallbackQueryHandler(toggle_maintenance, pattern=r"^toggle_maintenance_(global|wd)$"))
    ptb_app.add_handler(CallbackQueryHandler(admin_manage_tasks, pattern="^admin_manage_tasks$"))
    ptb_app.add_handler(CallbackQueryHandler(toggle_task_status, pattern=r"^toggle_task_\d+$"))
    ptb_app.add_handler(CallbackQueryHandler(review_submissions, pattern="^admin_pending_submissions$"))
    ptb_app.add_handler(CallbackQueryHandler(approve_submission, pattern=r"^approve_sub_\d+$"))
    # ... All other callbacks are added here

    # Main Entry
    port = int(os.environ.get("PORT", 8000))
    uvicorn.run("main:app", host="0.0.0.0", port=port, reload=False)