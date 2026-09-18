import asyncio
import base64
import json
import logging
import os
import random
import string
import threading
from datetime import datetime, timedelta, timezone
from http import HTTPStatus
from typing import Any, Dict, Optional

import httpx
from flask import Flask, jsonify
from telegram import (
    Bot,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    ReplyKeyboardMarkup,
    Update,
)
from telegram.constants import ChatMemberStatus
from telegram.ext import (
    Application,
    ApplicationBuilder,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

APP_NAME = "⚡STORM NET⚡"
BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
OWNER_ID = int(os.getenv("OWNER_ID", "0") or 0)
ADMIN_IDS = {
    int(x.strip()) for x in os.getenv("ADMIN_IDS", "").split(",")
    if x.strip().isdigit()
}
if OWNER_ID:
    ADMIN_IDS.add(OWNER_ID)

GITHUB_TOKEN = os.getenv("GITHUB_TOKEN", "").strip()
GITHUB_REPO = os.getenv("GITHUB_REPO", "").strip()  # owner/repo
GITHUB_BRANCH = os.getenv("GITHUB_BRANCH", "main").strip()
DATA_PATH = os.getenv("GITHUB_DATA_PATH", "data.json").strip()

logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    level=logging.INFO,
)
log = logging.getLogger(APP_NAME)

DEFAULT_DATA = {
    "users": {},
    "referrals": {},
    "transactions": [],
    "configurations": {},
    "categories": {},
    "tasks": {},
    "admins": {},
    "fraud_logs": [],
    "audit_logs": [],
    "redemptions": [],
    "redeem_codes": {},
    "code_usage": {},
    "collaborations": {},
    "settings": {
        "bot_name": APP_NAME,
        "referral_reward": 5,
        "max_referrals_per_day": 20,
        "fraud_threshold": 5,
        "fraud_block_hours": 24,
        "maintenance_mode": False,
        "auto_post_enabled": False,
        "auto_post_channel_id": "",
        "auto_post_discussion_id": "",
        "auto_post_to_channel": True,
        "auto_post_to_discussion": False,
        "auto_code_reward_points": 5,
        "auto_code_max_users": 100,
        "auto_code_expiry_hours": 24,
        "auto_code_interval_minutes": 360,
        "auto_code_daily_limit": 4,
        "auto_code_length": 8,
        "auto_code_prefix": "",
        "collaboration_enabled": False,
        "leaderboard_enabled": True,
    },
    "statistics": {},
}

db_lock = asyncio.Lock()
state = DEFAULT_DATA.copy()
admin_sessions: Dict[int, Dict[str, Any]] = {}
user_sessions: Dict[int, str] = {}
last_auto_run: Optional[datetime] = None


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def iso(dt: Optional[datetime]) -> Optional[str]:
    return dt.isoformat() if dt else None


def parse_dt(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except Exception:
        return None


def deep_copy_default() -> Dict[str, Any]:
    return json.loads(json.dumps(DEFAULT_DATA))


def normalize_data(raw: Any) -> Dict[str, Any]:
    data = deep_copy_default()
    if isinstance(raw, dict):
        for k, v in raw.items():
            data[k] = v
    for key in DEFAULT_DATA:
        if key not in data:
            data[key] = json.loads(json.dumps(DEFAULT_DATA[key]))
    return data


async def github_request(
    client: httpx.AsyncClient, method: str, url: str, **kwargs
) -> httpx.Response:
    headers = kwargs.pop("headers", {})
    headers.update({
        "Authorization": f"Bearer {GITHUB_TOKEN}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "storm-net-bot",
    })
    return await client.request(method, url, headers=headers, **kwargs)


async def load_data() -> None:
    global state
    if not (GITHUB_TOKEN and GITHUB_REPO):
        log.warning("GITHUB_TOKEN/GITHUB_REPO not configured; using local in-memory defaults.")
        state = deep_copy_default()
        return

    url = f"https://api.github.com/repos/{GITHUB_REPO}/contents/{DATA_PATH}"
    async with httpx.AsyncClient(timeout=25) as client:
        r = await github_request(client, "GET", url, params={"ref": GITHUB_BRANCH})
        if r.status_code == 404:
            state = deep_copy_default()
            await save_data("Initialize STORM NET database")
            return
        r.raise_for_status()
        payload = r.json()
        content = base64.b64decode(payload["content"]).decode("utf-8")
        state = normalize_data(json.loads(content))
    log.info("Loaded persistent database from GitHub.")


async def save_data(commit_message: str = "Update STORM NET database") -> bool:
    if not (GITHUB_TOKEN and GITHUB_REPO):
        return True

    raw = json.dumps(state, ensure_ascii=False, indent=2, sort_keys=True).encode()
    encoded = base64.b64encode(raw).decode()
    url = f"https://api.github.com/repos/{GITHUB_REPO}/contents/{DATA_PATH}"

    async with httpx.AsyncClient(timeout=25) as client:
        for attempt in range(4):
            get = await github_request(
                client, "GET", url, params={"ref": GITHUB_BRANCH}
            )
            sha = get.json().get("sha") if get.status_code == 200 else None
            body = {
                "message": commit_message,
                "content": encoded,
                "branch": GITHUB_BRANCH,
            }
            if sha:
                body["sha"] = sha
            put = await github_request(client, "PUT", url, json=body)
            if put.status_code in (200, 201):
                return True
            if put.status_code in (409, 422):
                await asyncio.sleep(0.7 * (attempt + 1))
                continue
            log.error("GitHub save failed: %s %s", put.status_code, put.text[:500])
            return False
    return False


async def persist(message: str) -> None:
    async with db_lock:
        await save_data(message)


def is_admin(user_id: int) -> bool:
    return user_id in ADMIN_IDS or user_id == OWNER_ID


def admin_role(user_id: int) -> str:
    if user_id == OWNER_ID:
        return "Super Admin"
    record = state["admins"].get(str(user_id), {})
    return record.get("role", "Admin" if user_id in ADMIN_IDS else "User")


def main_menu() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        [
            ["💰 My Points", "👥 Referrals"],
            ["🎟️ Redeem Code", "📦 My Redemptions"],
            ["📋 Tasks", "🏆 Leaderboard"],
            ["ℹ️ Help"],
        ],
        resize_keyboard=True,
    )


def admin_menu() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        [
            ["📊 Statistics", "👤 User Management"],
            ["🎟️ Redeem Codes", "🤖 Auto Post"],
            ["🤝 Collaborations", "🛡️ Anti-Cheat"],
            ["📢 Broadcast", "⚙️ Settings"],
            ["⬅️ User Menu"],
        ],
        resize_keyboard=True,
    )


def cancel_menu() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup([["❌ Cancel"]], resize_keyboard=True)


def generate_code(length: int = 8, prefix: str = "") -> str:
    alphabet = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
    n = max(4, int(length))
    raw = "".join(random.choice(alphabet) for _ in range(n))
    if len(raw) >= 4:
        raw = raw[:4] + "-" + raw[4:]
    return f"{prefix}{raw}"


def user_record(user) -> Dict[str, Any]:
    uid = str(user.id)
    if uid not in state["users"]:
        state["users"][uid] = {
            "user_id": user.id,
            "username": user.username or "",
            "first_name": user.first_name or "",
            "points": 0,
            "earned_points": 0,
            "spent_points": 0,
            "referred_by": None,
            "referral_status": None,
            "fraud_attempts": 0,
            "blocked_until": None,
            "status": "active",
            "created_at": iso(utcnow()),
            "last_activity": iso(utcnow()),
        }
    else:
        state["users"][uid]["username"] = user.username or state["users"][uid].get("username", "")
        state["users"][uid]["first_name"] = user.first_name or state["users"][uid].get("first_name", "")
        state["users"][uid]["last_activity"] = iso(utcnow())
    return state["users"][uid]


def blocked(record: Dict[str, Any]) -> bool:
    until = parse_dt(record.get("blocked_until"))
    if until and until > utcnow():
        return True
    if until:
        record["blocked_until"] = None
        record["fraud_attempts"] = 0
        record["status"] = "active"
    return False


async def register_user(update: Update) -> Dict[str, Any]:
    rec = user_record(update.effective_user)
    await persist("Register/update user")
    return rec


def referral_link(bot_username: str, user_id: int) -> str:
    return f"https://t.me/{bot_username}?start=ref_{user_id}"


async def handle_referral(update: Update, ref_id: int) -> str:
    uid = update.effective_user.id
    if uid == ref_id:
        state["fraud_logs"].append({
            "user_id": uid, "type": "self_referral", "created_at": iso(utcnow())
        })
        rec = state["users"][str(uid)]
        rec["fraud_attempts"] += 1
        if rec["fraud_attempts"] >= int(state["settings"]["fraud_threshold"]):
            rec["blocked_until"] = iso(utcnow() + timedelta(hours=int(state["settings"]["fraud_block_hours"])))
            rec["status"] = "temporary_block"
        await persist("Referral fraud attempt")
        return "⚠️ Self-referral is not allowed."

    invited = state["users"][str(uid)]
    existing = state["referrals"].get(str(uid))
    if existing:
        # Re-opening a link is not fraud by itself. The stored relationship prevents another reward.
        await persist("Repeated referral link checked")
        return "ℹ️ Your referral has already been recorded. It cannot be rewarded again."

    inviter = state["users"].get(str(ref_id))
    if not inviter:
        return "ℹ️ The referral owner is not registered."

    invited["referred_by"] = ref_id
    invited["referral_status"] = "qualified"
    reward = int(state["settings"].get("referral_reward", 5))
    state["referrals"][str(uid)] = {
        "inviter_id": ref_id,
        "status": "qualified",
        "reward": reward,
        "rewarded": True,
        "created_at": iso(utcnow()),
    }
    inviter["points"] += reward
    inviter["earned_points"] += reward
    state["transactions"].append({
        "user_id": ref_id,
        "type": "referral_reward",
        "amount": reward,
        "target_user_id": uid,
        "created_at": iso(utcnow()),
    })
    invited["fraud_attempts"] = 0
    await persist("Referral reward")
    return f"✅ Referral recorded.\n\n🎁 {reward} points were added to your inviter."


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.effective_user:
        return
    rec = await register_user(update)
    if blocked(rec):
        await update.message.reply_text(
            "🛡️ Your account is temporarily blocked for anti-cheat protection.\n"
            f"⏳ Until: {rec['blocked_until']}",
            reply_markup=main_menu(),
        )
        return

    payload = context.args[0] if context.args else ""
    referral_message = ""
    if payload.startswith("ref_"):
        try:
            ref_id = int(payload[4:])
            referral_message = "\n\n" + await handle_referral(update, ref_id)
        except ValueError:
            referral_message = "\n\n⚠️ Invalid referral link."

    text = (
        f"⚡ Welcome to {APP_NAME}!\n\n"
        "Use the buttons below to manage your points, referrals, tasks and redeem codes."
        + referral_message
    )
    await update.message.reply_text(text, reply_markup=main_menu())


async def show_points(update: Update) -> None:
    rec = state["users"][str(update.effective_user.id)]
    await update.message.reply_text(
        f"💰 Your Points\n\n"
        f"Balance: {rec.get('points', 0)}\n"
        f"Earned: {rec.get('earned_points', 0)}\n"
        f"Spent: {rec.get('spent_points', 0)}",
        reply_markup=main_menu(),
    )


async def show_referrals(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    uid = update.effective_user.id
    count = sum(1 for x in state["referrals"].values() if x.get("inviter_id") == uid and x.get("rewarded"))
    me = await context.bot.get_me()
    link = referral_link(me.username, uid)
    await update.message.reply_text(
        f"👥 Referrals\n\nSuccessful referrals: {count}\nReward per referral: "
        f"{state['settings'].get('referral_reward', 5)} points\n\n"
        f"🔗 Your link:\n{link}",
        reply_markup=main_menu(),
    )


async def redeem_code(update: Update, code: str) -> None:
    uid = update.effective_user.id
    rec = state["users"][str(uid)]
    key = code.strip().upper()
    item = state["redeem_codes"].get(key)
    if not item or item.get("status") != "active":
        await update.message.reply_text("❌ Code not found or inactive.", reply_markup=main_menu())
        return
    exp = parse_dt(item.get("expires_at"))
    if exp and exp <= utcnow():
        item["status"] = "expired"
        await persist("Expire redeem code")
        await update.message.reply_text("⏳ This redeem code has expired.", reply_markup=main_menu())
        return
    if uid in item.get("used_by", []):
        await update.message.reply_text("ℹ️ You already redeemed this code.", reply_markup=main_menu())
        return
    if int(item.get("used_count", 0)) >= int(item.get("max_users", 0)):
        item["status"] = "limit_reached"
        await persist("Redeem code limit reached")
        await update.message.reply_text("👥 This code has reached its user limit.", reply_markup=main_menu())
        return

    reward = int(item.get("reward_points", 0))
    item.setdefault("used_by", []).append(uid)
    item["used_count"] = int(item.get("used_count", 0)) + 1
    rec["points"] += reward
    rec["earned_points"] += reward
    state["code_usage"].setdefault(key, []).append({
        "user_id": uid, "reward": reward, "created_at": iso(utcnow())
    })
    state["transactions"].append({
        "user_id": uid, "type": "redeem_code", "amount": reward,
        "code": key, "created_at": iso(utcnow())
    })
    await persist("Redeem code used")
    await update.message.reply_text(
        f"🎉 Redeem successful!\n\n🎟️ {key}\n💰 +{reward} points\n"
        f"💳 Balance: {rec['points']}",
        reply_markup=main_menu(),
    )


def collaboration_targets() -> list:
    if not state["settings"].get("collaboration_enabled"):
        return []
    return [
        x for x in state["collaborations"].values()
        if x.get("status") == "active"
    ]


async def post_code(bot: Bot, code: str, item: Dict[str, Any], targets=None) -> Dict[str, Any]:
    text = (
        f"⚡ {APP_NAME}\n\n"
        "🎟️ NEW REDEEM CODE\n\n"
        f"🔑 Code: `{code}`\n"
        f"💰 Reward: {item['reward_points']} points\n"
        f"👥 Limit: {item['max_users']} users\n"
        f"⏳ Expires: {item.get('expires_at') or 'No expiry'}\n\n"
        "🎁 Redeem it in the bot."
    )
    posted = []
    errors = []
    destinations = []

    if state["settings"].get("auto_post_to_channel"):
        cid = state["settings"].get("auto_post_channel_id")
        if cid:
            destinations.append(("channel", cid))

    if state["settings"].get("auto_post_to_discussion"):
        did = state["settings"].get("auto_post_discussion_id")
        if did:
            destinations.append(("discussion", did))

    for partner in (targets if targets is not None else collaboration_targets()):
        for typ, key in [("partner_channel", partner.get("channel_id")), ("partner_discussion", partner.get("discussion_id"))]:
            if key:
                destinations.append((typ, key))

    for typ, chat_id in destinations:
        try:
            msg = await bot.send_message(chat_id=chat_id, text=text, parse_mode="Markdown")
            posted.append({"type": typ, "chat_id": str(chat_id), "message_id": msg.message_id})
        except Exception as exc:
            errors.append({"type": typ, "chat_id": str(chat_id), "error": str(exc)[:300]})
    item["posts"] = posted
    item["post_errors"] = errors
    return {"posted": posted, "errors": errors}


async def create_manual_code(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    admin_sessions[update.effective_user.id] = {"step": "code"}
    await update.message.reply_text("🎟️ Enter the redeem code:", reply_markup=cancel_menu())


async def create_code_step(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    uid = update.effective_user.id
    session = admin_sessions.get(uid)
    if not session or session.get("action") != "create_code":
        return False
    text = update.message.text.strip()
    if text == "❌ Cancel":
        admin_sessions.pop(uid, None)
        await update.message.reply_text("Cancelled.", reply_markup=admin_menu())
        return True

    step = session.get("step")
    if step == "code":
        if "|" in text or len(text) < 3 or len(text) > 40:
            await update.message.reply_text("❌ Invalid code. Enter a simple code without `|`.")
            return True
        session["code"] = text.upper()
        session["step"] = "reward"
        await update.message.reply_text("💰 Enter points reward:")
    elif step == "reward":
        if not text.isdigit() or int(text) <= 0:
            await update.message.reply_text("❌ Enter a positive whole number.")
            return True
        session["reward"] = int(text)
        session["step"] = "expiry"
        await update.message.reply_text("⏳ Enter expiration (example: 24h, 7d, or 0 for no expiry):")
    elif step == "expiry":
        if text == "0":
            session["expiry_hours"] = 0
        else:
            try:
                if text.lower().endswith("h"):
                    hours = float(text[:-1])
                elif text.lower().endswith("d"):
                    hours = float(text[:-1]) * 24
                else:
                    raise ValueError
                if hours <= 0 or hours > 8760:
                    raise ValueError
                session["expiry_hours"] = hours
            except ValueError:
                await update.message.reply_text("❌ Use 24h, 7d, or 0.")
                return True
        session["step"] = "max_users"
        await update.message.reply_text("👥 Enter maximum users:")
    elif step == "max_users":
        if not text.isdigit() or int(text) <= 0:
            await update.message.reply_text("❌ Enter a positive whole number.")
            return True
        session["max_users"] = int(text)
        session["step"] = "auto_post"
        await update.message.reply_text(
            "📢 Auto-post this code?\nChoose one:",
            reply_markup=ReplyKeyboardMarkup([["✅ Yes", "❌ No"], ["❌ Cancel"]], resize_keyboard=True),
        )
    elif step == "auto_post":
        if text not in ("✅ Yes", "❌ No"):
            await update.message.reply_text("Choose ✅ Yes or ❌ No.")
            return True
        session["auto_post"] = text == "✅ Yes"
        code = session["code"]
        if code in state["redeem_codes"]:
            await update.message.reply_text("❌ That code already exists.", reply_markup=admin_menu())
            admin_sessions.pop(uid, None)
            return True
        hours = session["expiry_hours"]
        exp = iso(utcnow() + timedelta(hours=hours)) if hours else None
        item = {
            "code": code,
            "reward_points": session["reward"],
            "max_users": session["max_users"],
            "used_count": 0,
            "used_by": [],
            "expires_at": exp,
            "status": "active",
            "created_by": uid,
            "created_at": iso(utcnow()),
            "auto_post": session["auto_post"],
            "posts": [],
            "post_errors": [],
        }
        state["redeem_codes"][code] = item
        await persist("Admin created redeem code")
        result = {"posted": [], "errors": []}
        if session["auto_post"] and state["settings"].get("auto_post_enabled"):
            result = await post_code(context.bot, code, item)
            await persist("Post admin-created redeem code")
        admin_sessions.pop(uid, None)
        await update.message.reply_text(
            f"✅ Redeem code created!\n\n"
            f"🎟️ {code}\n💰 {item['reward_points']} points\n"
            f"👥 {item['max_users']} users\n⏳ {item['expires_at'] or 'No expiry'}\n"
            f"📢 Posted: {len(result['posted'])} destination(s)",
            reply_markup=admin_menu(),
        )
    return True


async def auto_generate_and_post(context: ContextTypes.DEFAULT_TYPE) -> None:
    settings = state["settings"]
    if not settings.get("auto_post_enabled"):
        return
    now = utcnow()
    today = now.date().isoformat()
    count = int(state["statistics"].get(f"auto_codes_{today}", 0))
    if count >= int(settings.get("auto_code_daily_limit", 4)):
        return

    # Enforce interval using the most recent auto code.
    latest = None
    for item in state["redeem_codes"].values():
        if item.get("auto_generated"):
            dt = parse_dt(item.get("created_at"))
            if dt and (latest is None or dt > latest):
                latest = dt
    if latest and now - latest < timedelta(minutes=int(settings.get("auto_code_interval_minutes", 360))):
        return

    code = generate_code(
        int(settings.get("auto_code_length", 8)),
        str(settings.get("auto_code_prefix", "")),
    )
    while code in state["redeem_codes"]:
        code = generate_code(int(settings.get("auto_code_length", 8)), str(settings.get("auto_code_prefix", "")))

    hours = int(settings.get("auto_code_expiry_hours", 24))
    item = {
        "code": code,
        "reward_points": int(settings.get("auto_code_reward_points", 5)),
        "max_users": int(settings.get("auto_code_max_users", 100)),
        "used_count": 0,
        "used_by": [],
        "expires_at": iso(now + timedelta(hours=hours)) if hours else None,
        "status": "active",
        "created_by": OWNER_ID or 0,
        "created_at": iso(now),
        "auto_post": True,
        "auto_generated": True,
        "posts": [],
        "post_errors": [],
    }
    state["redeem_codes"][code] = item
    state["statistics"][f"auto_codes_{today}"] = count + 1
    await persist("Generate automatic redeem code")
    await post_code(context.bot, code, item)
    await persist("Post automatic redeem code")


async def auto_loop(application: Application) -> None:
    while True:
        try:
            await auto_generate_and_post(application.job_queue.application if application.job_queue else application)  # type: ignore
        except Exception:
            log.exception("Auto-code loop error")
        await asyncio.sleep(60)


async def admin_panel(update: Update) -> None:
    await update.message.reply_text(
        f"👑 {APP_NAME} Admin Panel\nRole: {admin_role(update.effective_user.id)}",
        reply_markup=admin_menu(),
    )


async def stats(update: Update) -> None:
    users = len(state["users"])
    points = sum(int(x.get("points", 0)) for x in state["users"].values())
    codes = len(state["redeem_codes"])
    redemptions = len(state["transactions"])
    await update.message.reply_text(
        f"📊 Statistics\n\nUsers: {users}\nActive points: {points}\n"
        f"Redeem codes: {codes}\nTransactions: {redemptions}",
        reply_markup=admin_menu(),
    )


async def anti_cheat_panel(update: Update) -> None:
    s = state["settings"]
    await update.message.reply_text(
        "🛡️ Anti-Cheat\n\n"
        f"Threshold: {s['fraud_threshold']} consecutive suspicious attempts\n"
        f"Temporary block: {s['fraud_block_hours']} hours\n"
        "Permanent ban: ❌ Disabled",
        reply_markup=admin_menu(),
    )


async def redeem_admin_panel(update: Update) -> None:
    await update.message.reply_text(
        "🎟️ Redeem Codes\n\n"
        "Choose an action:",
        reply_markup=ReplyKeyboardMarkup(
            [["➕ Create Code", "📋 Code List"], ["🗑️ Disable Code", "⬅️ Admin Menu"]],
            resize_keyboard=True,
        ),
    )


async def auto_post_panel(update: Update) -> None:
    s = state["settings"]
    await update.message.reply_text(
        "🤖 Auto Post\n\n"
        f"Status: {'🟢 ON' if s['auto_post_enabled'] else '🔴 OFF'}\n"
        f"Channel: {s.get('auto_post_channel_id') or 'Not set'}\n"
        f"Discussion: {s.get('auto_post_discussion_id') or 'Not set'}\n"
        f"To channel: {'ON' if s.get('auto_post_to_channel') else 'OFF'}\n"
        f"To discussion: {'ON' if s.get('auto_post_to_discussion') else 'OFF'}\n\n"
        "Use Settings to change these values.",
        reply_markup=admin_menu(),
    )


async def code_list(update: Update) -> None:
    items = list(state["redeem_codes"].values())
    if not items:
        text = "🎟️ No redeem codes yet."
    else:
        lines = ["🎟️ Redeem Codes"]
        for x in items[-15:]:
            lines.append(
                f"\n🔑 {x['code']}\n💰 {x['reward_points']} | "
                f"👥 {x['used_count']}/{x['max_users']} | "
                f"Status: {x['status']}"
            )
        text = "".join(lines)
    await update.message.reply_text(text, reply_markup=admin_menu())


async def collaborations_panel(update: Update) -> None:
    s = state["settings"]
    if not s.get("collaboration_enabled"):
        await update.message.reply_text(
            "🤝 Collaboration is currently OFF.\n\nEnable it from Settings when you want to work with partner channel owners.",
            reply_markup=admin_menu(),
        )
        return
    await update.message.reply_text(
        "🤝 Collaborations\n\n"
        "Use the buttons to add or manage partner channels.",
        reply_markup=ReplyKeyboardMarkup(
            [["➕ Add Partner", "📋 Partners"], ["🟢 Enable All", "🔴 Disable All"], ["⬅️ Admin Menu"]],
            resize_keyboard=True,
        ),
    )


async def add_partner_start(update: Update) -> None:
    admin_sessions[update.effective_user.id] = {"action": "add_partner", "step": "name"}
    await update.message.reply_text("🤝 Enter partner/channel name:", reply_markup=cancel_menu())


async def add_partner_step(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    uid = update.effective_user.id
    session = admin_sessions.get(uid)
    if not session or session.get("action") != "add_partner":
        return False
    text = update.message.text.strip()
    if text == "❌ Cancel":
        admin_sessions.pop(uid, None)
        await update.message.reply_text("Cancelled.", reply_markup=admin_menu())
        return True
    step = session["step"]
    if step == "name":
        session["name"] = text
        session["step"] = "channel"
        await update.message.reply_text("📢 Enter partner channel @username or chat ID:")
    elif step == "channel":
        session["channel_id"] = text
        session["step"] = "discussion"
        await update.message.reply_text("💬 Enter discussion group chat ID, or type `skip`:")
    elif step == "discussion":
        session["discussion_id"] = "" if text.lower() == "skip" else text
        session["step"] = "owner"
        await update.message.reply_text("👤 Enter partner owner's Telegram User ID:")
    elif step == "owner":
        if not text.isdigit():
            await update.message.reply_text("❌ Telegram User ID must be a number.")
            return True
        partner_id = str(len(state["collaborations"]) + 1)
        state["collaborations"][partner_id] = {
            "id": partner_id,
            "name": session["name"],
            "channel_id": session["channel_id"],
            "discussion_id": session["discussion_id"],
            "owner_id": int(text),
            "status": "active",
            "created_by": uid,
            "created_at": iso(utcnow()),
            "codes_posted": 0,
        }
        await persist("Add collaboration partner")
        admin_sessions.pop(uid, None)
        await update.message.reply_text("✅ Collaboration partner added.", reply_markup=admin_menu())
    return True


async def settings_panel(update: Update) -> None:
    s = state["settings"]
    await update.message.reply_text(
        "⚙️ Settings\n\n"
        f"Collaboration: {'ON' if s.get('collaboration_enabled') else 'OFF'}\n"
        f"Auto Post: {'ON' if s.get('auto_post_enabled') else 'OFF'}\n"
        f"Referral reward: {s.get('referral_reward')} points\n"
        f"Fraud threshold: {s.get('fraud_threshold')}\n"
        f"Temporary block: {s.get('fraud_block_hours')}h\n\n"
        "Use dedicated admin workflows to change settings.",
        reply_markup=admin_menu(),
    )


async def tasks_panel(update: Update) -> None:
    enabled = [x for x in state["tasks"].values() if x.get("enabled", True)]
    if not enabled:
        await update.message.reply_text("📋 No active tasks right now.", reply_markup=main_menu())
        return
    lines = ["📋 Tasks"]
    for t in enabled[:20]:
        lines.append(f"\n• {t.get('title','Task')} — {t.get('reward',0)} points")
    await update.message.reply_text("".join(lines), reply_markup=main_menu())


async def leaderboard(update: Update) -> None:
    if not state["settings"].get("leaderboard_enabled", True):
        await update.message.reply_text("🏆 Leaderboard is disabled.", reply_markup=main_menu())
        return
    rows = sorted(state["users"].values(), key=lambda x: int(x.get("earned_points", 0)), reverse=True)[:10]
    lines = ["🏆 Leaderboard"]
    for i, x in enumerate(rows, 1):
        name = x.get("username") or x.get("first_name") or str(x.get("user_id"))
        lines.append(f"{i}. {name} — {x.get('earned_points',0)}")
    await update.message.reply_text("\n".join(lines), reply_markup=main_menu())


async def help_panel(update: Update) -> None:
    await update.message.reply_text(
        f"ℹ️ {APP_NAME}\n\n"
        "Use the buttons to navigate.\n"
        "Redeem codes are one-time per user.\n"
        "Referral rewards are protected by Telegram User ID.\n"
        "Anti-cheat blocks are temporary only; there are no permanent bans.",
        reply_markup=main_menu(),
    )


async def user_redemptions(update: Update) -> None:
    uid = update.effective_user.id
    rows = [x for x in state["transactions"] if x.get("user_id") == uid and x.get("type") == "redeem_code"]
    if not rows:
        await update.message.reply_text("📦 No redemptions yet.", reply_markup=main_menu())
        return
    lines = ["📦 My Redemptions"]
    for x in rows[-15:]:
        lines.append(f"\n🎟️ {x.get('code')}  +{x.get('amount')} points")
    await update.message.reply_text("".join(lines), reply_markup=main_menu())


async def broadcast_start(update: Update) -> None:
    admin_sessions[update.effective_user.id] = {"action": "broadcast", "step": "message"}
    await update.message.reply_text("📢 Send the broadcast message:", reply_markup=cancel_menu())


async def broadcast_step(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    uid = update.effective_user.id
    session = admin_sessions.get(uid)
    if not session or session.get("action") != "broadcast":
        return False
    if update.message.text == "❌ Cancel":
        admin_sessions.pop(uid, None)
        await update.message.reply_text("Cancelled.", reply_markup=admin_menu())
        return True
    sent = failed = 0
    for user_id in list(state["users"].keys()):
        try:
            await context.bot.send_message(int(user_id), update.message.text)
            sent += 1
        except Exception:
            failed += 1
        await asyncio.sleep(0.05)
    state["audit_logs"].append({
        "admin_id": uid, "action": "broadcast", "sent": sent,
        "failed": failed, "created_at": iso(utcnow())
    })
    admin_sessions.pop(uid, None)
    await persist("Admin broadcast")
    await update.message.reply_text(
        f"📢 Broadcast complete.\nSent: {sent}\nFailed: {failed}",
        reply_markup=admin_menu(),
    )
    return True


async def generic_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message or not update.effective_user:
        return
    uid = update.effective_user.id
    rec = await register_user(update)
    if blocked(rec):
        await update.message.reply_text("🛡️ You are temporarily blocked for 24 hours.", reply_markup=main_menu())
        return

    if is_admin(uid):
        if await create_code_step(update, context):
            return
        if await add_partner_step(update, context):
            return
        if await broadcast_step(update, context):
            return

    text = update.message.text.strip()
    if text == "💰 My Points":
        await show_points(update)
    elif text == "👥 Referrals":
        await show_referrals(update, context)
    elif text == "🎟️ Redeem Code":
        user_sessions[uid] = "redeem"
        await update.message.reply_text("🎟️ Enter your redeem code:", reply_markup=cancel_menu())
    elif user_sessions.get(uid) == "redeem":
        if text == "❌ Cancel":
            user_sessions.pop(uid, None)
            await update.message.reply_text("Cancelled.", reply_markup=main_menu())
        else:
            user_sessions.pop(uid, None)
            await redeem_code(update, text)
    elif text == "📦 My Redemptions":
        await user_redemptions(update)
    elif text == "📋 Tasks":
        await tasks_panel(update)
    elif text == "🏆 Leaderboard":
        await leaderboard(update)
    elif text == "ℹ️ Help":
        await help_panel(update)
    elif is_admin(uid) and text == "📊 Statistics":
        await stats(update)
    elif is_admin(uid) and text == "👤 User Management":
        await update.message.reply_text("👤 User Management: search/manage workflows can be added from the admin tools.", reply_markup=admin_menu())
    elif is_admin(uid) and text == "🎟️ Redeem Codes":
        await redeem_admin_panel(update)
    elif is_admin(uid) and text == "➕ Create Code":
        admin_sessions[uid] = {"action": "create_code", "step": "code"}
        await update.message.reply_text("🎟️ Enter the redeem code:", reply_markup=cancel_menu())
    elif is_admin(uid) and text == "📋 Code List":
        await code_list(update)
    elif is_admin(uid) and text == "🤖 Auto Post":
        await auto_post_panel(update)
    elif is_admin(uid) and text == "🤝 Collaborations":
        await collaborations_panel(update)
    elif is_admin(uid) and text == "➕ Add Partner":
        await add_partner_start(update)
    elif is_admin(uid) and text == "📋 Partners":
        rows = list(state["collaborations"].values())
        if not rows:
            await update.message.reply_text("No partners.", reply_markup=admin_menu())
        else:
            await update.message.reply_text(
                "\n".join(f"🤝 {x['name']} | {x['status']} | {x['channel_id']}" for x in rows),
                reply_markup=admin_menu(),
            )
    elif is_admin(uid) and text == "🟢 Enable All":
        for x in state["collaborations"].values():
            x["status"] = "active"
        await persist("Enable collaboration partners")
        await update.message.reply_text("🟢 All collaboration partners enabled.", reply_markup=admin_menu())
    elif is_admin(uid) and text == "🔴 Disable All":
        for x in state["collaborations"].values():
            x["status"] = "disabled"
        await persist("Disable collaboration partners")
        await update.message.reply_text("🔴 All collaboration partners disabled.", reply_markup=admin_menu())
    elif is_admin(uid) and text == "🛡️ Anti-Cheat":
        await anti_cheat_panel(update)
    elif is_admin(uid) and text == "📢 Broadcast":
        await broadcast_start(update)
    elif is_admin(uid) and text == "⚙️ Settings":
        await settings_panel(update)
    elif is_admin(uid) and text in ("⬅️ User Menu", "⬅️ Admin Menu"):
        await update.message.reply_text("Menu:", reply_markup=admin_menu() if text == "⬅️ Admin Menu" else main_menu())
    else:
        # Plain text can be used as a redeem code when user is in redeem mode only.
        await update.message.reply_text("Use the buttons below.", reply_markup=admin_menu() if is_admin(uid) else main_menu())


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    log.exception("Unhandled bot error", exc_info=context.error)


# Render health server
flask_app = Flask(__name__)


@flask_app.get("/")
def health():
    return jsonify({"service": APP_NAME, "status": "ok"}), HTTPStatus.OK


@flask_app.get("/health")
def health2():
    return jsonify({"status": "healthy"}), HTTPStatus.OK


def run_web() -> None:
    port = int(os.getenv("PORT", "10000"))
    flask_app.run(host="0.0.0.0", port=port, use_reloader=False)


async def post_init(application: Application) -> None:
    await load_data()
    # The auto scheduler is intentionally implemented as an asyncio task,
    # avoiding reliance on PTB's optional JobQueue package.
    application.bot_data["auto_task"] = asyncio.create_task(auto_loop(application))


async def post_shutdown(application: Application) -> None:
    task = application.bot_data.get("auto_task")
    if task:
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass


def main() -> None:
    if not BOT_TOKEN:
        raise RuntimeError("BOT_TOKEN environment variable is required.")
    threading.Thread(target=run_web, daemon=True).start()

    app = (
        ApplicationBuilder()
        .token(BOT_TOKEN)
        .post_init(post_init)
        .post_shutdown(post_shutdown)
        .build()
    )
    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, generic_text))
    app.add_error_handler(error_handler)
    log.info("%s starting...", APP_NAME)
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
