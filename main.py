# /// script
# requires-python = "==3.11.*"
# dependencies = [
#   "codewords-client==0.4.6",
#   "fastapi==0.116.1",
#   "httpx==0.28.1"
# ]
# [tool.env-checker]
# env_vars = [
#   "PORT=8000",
#   "LOGLEVEL=INFO",
#   "CODEWORDS_API_KEY",
#   "CODEWORDS_RUNTIME_URI",
#   "TIMETRACKER_BOT_TOKEN",
#   "MINIAPP_URL",
#   "GOLOVA_API_KEY",
#   "GOLOVA_API_BASE=https://renteventservice.golova.io",
#   "DASHBOARD_LOGIN=admin",
#   "DASHBOARD_PASSWORD=admin123"
# ]
# ///

import os
import json
import math
import re
import secrets
import hmac
import hashlib
from datetime import datetime, timezone
import uuid
from typing import Literal, Optional

import httpx
from codewords_client import logger, run_service, redis_client
from fastapi import FastAPI, HTTPException, Request, Depends
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.middleware.cors import CORSMiddleware
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from pydantic import BaseModel, Field

_TOKEN: str | None = None
security = HTTPBasic(auto_error=False)


def get_token() -> str:
    global _TOKEN
    if _TOKEN is None:
        _TOKEN = os.environ.get("TIMETRACKER_BOT_TOKEN") or os.environ["TELEGRAM_BOT_TOKEN"]
    return _TOKEN


def check_dashboard_auth(credentials: HTTPBasicCredentials = Depends(security)):
    login = os.environ.get("DASHBOARD_LOGIN", "admin")
    password = os.environ.get("DASHBOARD_PASSWORD", "admin123")
    if not credentials:
        raise HTTPException(status_code=401, headers={"WWW-Authenticate": "Basic"})
    ok_user = secrets.compare_digest(credentials.username.encode(), login.encode())
    ok_pass = secrets.compare_digest(credentials.password.encode(), password.encode())
    if not (ok_user and ok_pass):
        raise HTTPException(status_code=401, headers={"WWW-Authenticate": "Basic"})
    return credentials


# ─────────────────────────────────
# Telegram helpers
# ─────────────────────────────────
async def tg(method: str, data: dict) -> dict:
    url = f"https://api.telegram.org/bot{get_token()}/{method}"
    async with httpx.AsyncClient(timeout=15) as c:
        r = await c.post(url, json=data)
        r.raise_for_status()
        return r.json()


async def send_msg(chat_id: int, text: str, reply_markup: dict | None = None):
    payload: dict = {"chat_id": chat_id, "text": text, "parse_mode": "HTML"}
    if reply_markup:
        payload["reply_markup"] = reply_markup
    await tg("sendMessage", payload)


async def send_inline_msg(chat_id: int, text: str, inline_markup: dict) -> int:
    r = await tg("sendMessage", {
        "chat_id": chat_id, "text": text, "parse_mode": "HTML", "reply_markup": inline_markup
    })
    return r.get("result", {}).get("message_id", 0)


async def edit_inline_markup(chat_id: int, message_id: int, inline_markup: dict):
    try:
        await tg("editMessageReplyMarkup", {
            "chat_id": chat_id, "message_id": message_id, "reply_markup": inline_markup
        })
    except Exception as e:
        logger.warning("Failed to edit inline markup", chat_id=chat_id, message_id=message_id, error=str(e))


# ─────────────────────────────────
# Keyboards
# ─────────────────────────────────
def shift_request_kb(req_id: str, spots_taken: int, total_spots: int) -> dict:
    remaining = total_spots - spots_taken
    if remaining <= 0:
        btn = "✅ Все места заняты"
    else:
        btn = f"✋ Откликнуться ({spots_taken}/{total_spots})"
    return {"inline_keyboard": [[{"text": btn, "callback_data": f"apply_shift:{req_id}"}]]}


def admin_confirm_kb(req_id: str, worker_id: int) -> dict:
    return {"inline_keyboard": [[
        {"text": "✅ Принять", "callback_data": f"confirm:{req_id}:{worker_id}"},
        {"text": "❌ Отклонить", "callback_data": f"reject:{req_id}:{worker_id}"}
    ]]}


def contact_kb() -> dict:
    return {"keyboard": [
        [{"text": "📱 Поделиться номером", "request_contact": True}],
    ], "resize_keyboard": True, "one_time_keyboard": True}


def main_menu_kb() -> dict:
    return {"keyboard": [
        [{"text": "🟢 Начать смену"}, {"text": "🔴 Завершить смену"}],
        [{"text": "📦 Склад: начать"}, {"text": "📦 Склад: завершить"}],
        [{"text": "💰 Зарплата"}, {"text": "👤 Мой кабинет"}],
        [{"text": "📊 Премии/Штрафы"}, {"text": "↩️ Отменить"}],
        [{"text": "📅 Моё расписание"}],
        [{"text": "💸 Записать расход"}],
    ], "resize_keyboard": True}


def location_kb(prompt: str) -> dict:
    return {"keyboard": [
        [{"text": prompt, "request_location": True}],
    ], "resize_keyboard": True, "one_time_keyboard": True}


def agreement_kb() -> dict:
    return {"keyboard": [
        [{"text": "✅ Согласен"}, {"text": "❌ Не согласен"}],
    ], "resize_keyboard": True, "one_time_keyboard": True}


WORKPLACE_POLICIES = (
    "📑 <b>ПРАВИЛА ВНУТРЕННЕГО ТРУДОВОГО РАСПОРЯДКА</b>\n"
    "─" * 30 + "\n\n"
    "<b>I. РАБОЧЕЕ ВРЕМЯ И ДИСЦИПЛИНА</b>\n"
    "1.1. Сотрудник обязан соблюдать установленный график работы.\n"
    "1.2. Начало и окончание рабочей смены фиксируются через систему учёта (данный бот).\n"
    "1.3. Сотрудник обязан выполнять распоряжения непосредственного руководителя.\n\n"
    "<b>II. МАТЕРИАЛЬНАЯ ОТВЕТСТВЕННОСТЬ</b>\n"
    "2.1. Сотрудник несёт полную материальную ответственность за вверенное имущество.\n"
    "2.2. О любых поломках необходимо незамедлительно уведомить руководителя.\n\n"
    "<b>III. БЕЗОПАСНОСТЬ И ОХРАНА ТРУДА</b>\n"
    "3.1. Соблюдать правила техники безопасности.\n"
    "3.2. Категорически запрещается находиться на рабочем месте в состоянии опьянения.\n\n"
    "<b>IV. УЧЁТ РАБОЧЕГО ВРЕМЕНИ</b>\n"
    "4.1. При начале и окончании смены отправлять геолокацию.\n"
    "4.2. Заработная плата рассчитывается на основании фактически отработанных часов.\n\n"
    "<b>V. ПРЕМИИ И ВЗЫСКАНИЯ</b>\n"
    "5.1. За добросовестное выполнение обязанностей могут быть назначены премии.\n"
    "5.2. За нарушение правил могут быть применены дисциплинарные взыскания.\n\n"
    "─" * 30 + "\n"
    "Подтвердите ознакомление с правилами:"
)

ADMIN_IDS = {742587575, 64408195}
MINIAPP_URL = os.environ.get('MINIAPP_URL', '')

ADMIN_IDS = {742587575, 64408195}
MINIAPP_URL = os.environ.get('MINIAPP_URL', '')
GOLOVA_API_KEY = os.environ.get('GOLOVA_API_KEY', '')
GOLOVA_API_BASE = os.environ.get('GOLOVA_API_BASE', 'https://renteventservice.golova.io')
GOLOVA_API_KEY = os.environ.get('GOLOVA_API_KEY', '')
GOLOVA_API_BASE = os.environ.get('GOLOVA_API_BASE', 'https://renteventservice.golova.io')
SQM_IDS = {904273869, 5955218562, 742587575}
DRIVER_IDS = {748721414, 742587575}
SQM_ONLY_IDS = {904273869, 5955218562}


def is_admin(tg_id: int) -> bool: return tg_id in ADMIN_IDS


async def golova_fetch_projects(days_ahead: int = 2) -> list:
    """Fetch projects from Golova CRM filtered to ±days_ahead days from today. Reads all pages."""
    if not GOLOVA_API_KEY:
        return []
    try:
        from datetime import timedelta
        now = datetime.now(timezone.utc)
        from_date = now.date()
        to_date = (now + timedelta(days=days_ahead)).date()
        headers = {"Authorization": f"Bearer {GOLOVA_API_KEY}", "Accept": "application/json"}
        result = []
        async with httpx.AsyncClient(timeout=15) as client:
            page = 1
            while True:
                r = await client.get(
                    f"{GOLOVA_API_BASE}/api/v1/projects",
                    headers=headers,
                    params={"page": page},
                )
                r.raise_for_status()
                data = r.json()
                projects = data.get("data", data) if isinstance(data, dict) else data
                if not projects:
                    break
                for p in projects:
                    start_raw = p.get("start_date_time", "")
                    if not start_raw or start_raw.startswith("0000"):
                        continue
                    try:
                        start_dt = datetime.fromisoformat(start_raw.replace(" ", "T"))
                        p_date = start_dt.date()
                        if from_date <= p_date <= to_date:
                            result.append({
                                "id": p["id"],
                                "name": p["name"],
                                "start_date_time": start_raw,
                                "end_date_time": p.get("end_date_time", ""),
                                "total": p.get("total", 0),
                                "venue": p.get("venue"),
                                "status": p.get("estimate", {}).get("status", {}).get("name", ""),
                            })
                    except (ValueError, TypeError):
                        pass
                meta = data.get("meta", {}) if isinstance(data, dict) else {}
                if page >= meta.get("last_page", 1):
                    break
                page += 1
        return result
    except Exception as e:
        logger.warning("Golova API error", error=str(e))
        return []


async def golova_fetch_month(month: int, year: int) -> list:
    """Fetch all Golova projects for a given month."""
    if not GOLOVA_API_KEY:
        return []
    try:
        headers = {"Authorization": f"Bearer {GOLOVA_API_KEY}", "Accept": "application/json"}
        async with httpx.AsyncClient(timeout=10) as client:
            # Fetch all pages
            all_projects = []
            page = 1
            while True:
                r = await client.get(f"{GOLOVA_API_BASE}/api/v1/projects",
                                     headers=headers, params={"page": page})
                r.raise_for_status()
                data = r.json()
                projects = data.get("data", []) if isinstance(data, dict) else data
                if not projects:
                    break
                for p in projects:
                    start_raw = p.get("start_date_time", "")
                    if not start_raw or start_raw.startswith("0000"):
                        continue
                    try:
                        start_dt = datetime.fromisoformat(start_raw.replace(" ", "T"))
                        if start_dt.month == month and start_dt.year == year:
                            all_projects.append({
                                "id": p["id"],
                                "name": p["name"],
                                "start_date_time": start_raw,
                                "end_date_time": p.get("end_date_time", ""),
                                "total": p.get("total", 0),
                                "venue": p.get("venue"),
                                "status": p.get("estimate", {}).get("status", {}).get("name", ""),
                                "status_color": p.get("estimate", {}).get("status", {}).get("color", ""),
                            })
                    except (ValueError, TypeError):
                        pass
                # Check if there are more pages
                meta = data.get("meta", {}) if isinstance(data, dict) else {}
                if page >= meta.get("last_page", 1):
                    break
                page += 1
            return all_projects
    except Exception as e:
        logger.warning("Golova API month error", error=str(e))
        return []


def verify_tg_init_data(init_data: str) -> dict | None:
    """Verify Telegram WebApp initData HMAC. Returns user dict or None."""
    try:
        from urllib.parse import unquote
        params: dict = {}
        for part in unquote(init_data).split("&"):
            if "=" in part:
                k, v = part.split("=", 1)
                params[k] = v
        received_hash = params.pop("hash", None)
        if not received_hash:
            return None
        data_check_string = "\n".join(f"{k}={v}" for k, v in sorted(params.items()))
        token = get_token()
        secret_key = hmac.new(b"WebAppData", token.encode(), hashlib.sha256).digest()
        computed_hash = hmac.new(secret_key, data_check_string.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(computed_hash, received_hash):
            return None
        return json.loads(params.get("user", "{}"))
    except Exception:
        return None
def is_sqm_worker(tg_id: int) -> bool: return tg_id in SQM_IDS
def is_driver(tg_id: int) -> bool: return tg_id in DRIVER_IDS


def admin_menu_kb() -> dict:
    mini_row = [[{"text": "📊 Панель", "web_app": {"url": MINIAPP_URL}}]] if MINIAPP_URL else []
    return {"keyboard": mini_row + [
        [{"text": "🟢 Начать смену"}, {"text": "🔴 Завершить смену"}],
        [{"text": "💰 Зарплата"}, {"text": "👤 Мой кабинет"}],
        [{"text": "📊 Премии/Штрафы"}, {"text": "↩️ Отменить"}],
        [{"text": "📅 Моё расписание"}],
        # Быстрые админ-действия
        [{"text": "📨 Рассылка"}, {"text": "👥 Набор на смену"}],
        [{"text": "📩 Личное сообщение"}, {"text": "🎖 Начислить"}],
    ], "resize_keyboard": True}


def sqm_menu_kb(tg_id: int) -> dict:
    if tg_id in SQM_ONLY_IDS:
        base = [
            [{"text": "📦 Склад: начать"}, {"text": "📦 Склад: завершить"}],
            [{"text": "🏗 Монтаж"}, {"text": "🏚 Демонтаж"}],
            [{"text": "💰 Зарплата"}, {"text": "👤 Мой кабинет"}],
            [{"text": "📊 Премии/Штрафы"}, {"text": "↩️ Отменить"}],
            [{"text": "📅 Моё расписание"}],
        ]
    else:
        base = [
            [{"text": "🟢 Начать смену"}, {"text": "🔴 Завершить смену"}],
            [{"text": "📦 Склад: начать"}, {"text": "📦 Склад: завершить"}],
            [{"text": "🏗 Монтаж"}, {"text": "🏚 Демонтаж"}],
            [{"text": "💰 Зарплата"}, {"text": "👤 Мой кабинет"}],
            [{"text": "📊 Премии/Штрафы"}, {"text": "↩️ Отменить"}],
            [{"text": "📅 Моё расписание"}],
        ]
    if is_driver(tg_id):
        base.append([{"text": "🚗 Водитель: начать"}, {"text": "🚗 Водитель: завершить"}])
    if is_admin(tg_id):
        base.append([{"text": "📨 Рассылка"}, {"text": "👥 Набор на смену"}])
        base.append([{"text": "📩 Личное сообщение"}, {"text": "🎖 Начислить"}])
        base.append([{"text": "🛠 Сотрудники"}, {"text": "📋 Отчёт"}, {"text": "⚙️ Изм. ставку"}])
        base.append([{"text": "💳 Аванс"}, {"text": "📅 Расписание"}, {"text": "🛠 Уволить"}])
    return {"keyboard": base, "resize_keyboard": True}


def get_kb(tg_id: int) -> dict:
    if is_sqm_worker(tg_id) or is_driver(tg_id):
        return sqm_menu_kb(tg_id)
    if is_admin(tg_id):
        return admin_menu_kb()
    return main_menu_kb()


# ─────────────────────────────────
# Redis helpers
# ─────────────────────────────────
async def get_employee(redis, ns, tg_id):
    raw = await redis.get(f"{ns}:employee:{tg_id}")
    return json.loads(raw) if raw else None

async def save_employee(redis, ns, tg_id, data):
    await redis.set(f"{ns}:employee:{tg_id}", json.dumps(data, ensure_ascii=False))
    await redis.sadd(f"{ns}:employees_index", str(tg_id))

async def get_session(redis, ns, tg_id):
    raw = await redis.get(f"{ns}:session:{tg_id}")
    return json.loads(raw) if raw else None

async def save_session(redis, ns, tg_id, data):
    key = f"{ns}:session:{tg_id}"
    if data is None: await redis.delete(key)
    else: await redis.set(key, json.dumps(data, ensure_ascii=False))

async def get_shifts(redis, ns, tg_id):
    raw = await redis.get(f"{ns}:shifts:{tg_id}")
    return json.loads(raw) if raw else []

async def save_shifts(redis, ns, tg_id, shifts):
    await redis.set(f"{ns}:shifts:{tg_id}", json.dumps(shifts, ensure_ascii=False))

async def get_bonuses(redis, ns, tg_id):
    raw = await redis.get(f"{ns}:bonuses:{tg_id}")
    return json.loads(raw) if raw else []

async def save_bonuses(redis, ns, tg_id, bonuses):
    await redis.set(f"{ns}:bonuses:{tg_id}", json.dumps(bonuses, ensure_ascii=False))

async def get_driver_shifts(redis, ns, tg_id):
    raw = await redis.get(f"{ns}:driver_shifts:{tg_id}")
    return json.loads(raw) if raw else []

async def save_driver_shifts(redis, ns, tg_id, shifts):
    await redis.set(f"{ns}:driver_shifts:{tg_id}", json.dumps(shifts, ensure_ascii=False))

async def get_schedule(redis, ns, tg_id):
    raw = await redis.get(f"{ns}:schedule:{tg_id}")
    return json.loads(raw) if raw else []

async def save_schedule(redis, ns, tg_id, schedule):
    await redis.set(f"{ns}:schedule:{tg_id}", json.dumps(schedule, ensure_ascii=False))

async def get_advances(redis, ns, tg_id):
    raw = await redis.get(f"{ns}:advances:{tg_id}")
    return json.loads(raw) if raw else []


async def get_expenses(redis, ns, tg_id):
    raw = await redis.get(f"{ns}:expenses:{tg_id}")
    return json.loads(raw) if raw else []


async def save_expenses(redis, ns, tg_id, expenses):
    await redis.set(f"{ns}:expenses:{tg_id}", json.dumps(expenses, ensure_ascii=False))

async def save_advances(redis, ns, tg_id, advances):
    await redis.set(f"{ns}:advances:{tg_id}", json.dumps(advances, ensure_ascii=False))

async def get_sqm_jobs(redis, ns, tg_id):
    raw = await redis.get(f"{ns}:sqm_jobs:{tg_id}")
    return json.loads(raw) if raw else []

async def save_sqm_jobs(redis, ns, tg_id, jobs):
    await redis.set(f"{ns}:sqm_jobs:{tg_id}", json.dumps(jobs, ensure_ascii=False))

async def get_all_employee_ids(redis, ns):
    members = await redis.smembers(f"{ns}:employees_index")
    return [m if isinstance(m, str) else m.decode() for m in members]

async def get_shift_request(redis, ns, req_id):
    raw = await redis.get(f"{ns}:shift_req:{req_id}")
    return json.loads(raw) if raw else None

async def save_shift_request(redis, ns, req_id, data):
    key = f"{ns}:shift_req:{req_id}"
    if data is None: await redis.delete(key)
    else: await redis.set(key, json.dumps(data, ensure_ascii=False), ex=86400 * 7)

async def get_projects(redis, ns):
    raw = await redis.get(f"{ns}:projects")
    return json.loads(raw) if raw else []

async def save_projects(redis, ns, projects):
    await redis.set(f"{ns}:projects", json.dumps(projects, ensure_ascii=False))

async def get_project_templates(redis, ns):
    raw = await redis.get(f"{ns}:project_templates")
    return json.loads(raw) if raw else []

async def save_project_templates(redis, ns, templates):
    await redis.set(f"{ns}:project_templates", json.dumps(templates, ensure_ascii=False))

def get_active_projects(projects: list) -> list:
    today = datetime.now(timezone.utc).date().isoformat()
    return [p for p in projects if p.get("status") == "active" and p.get("date_start", "9999") <= today <= p.get("date_end", "0000")]


# ─────────────────────────────────
# Registration flow
# ─────────────────────────────────
async def handle_registration(chat_id, tg_id, text, msg, session, redis, ns):
    """Multi-step registration: ФИО → телефон → ставки → фото → правила"""

    # Шаг 1 — ФИО
    if session and session.get("state") == "awaiting_name":
        if len(text) < 3:
            await send_msg(chat_id, "❌ Введите полное ФИО (минимум 3 символа):")
            return True
        await save_session(redis, ns, tg_id, {"state": "awaiting_phone", "name": text})
        await send_msg(chat_id, f"✅ <b>{text}</b>\n\nТеперь поделитесь вашим <b>номером телефона</b>:", contact_kb())
        return True

    # Шаг 2 — телефон
    if session and session.get("state") == "awaiting_phone":
        contact = msg.get("contact") if msg else None
        phone = None
        if contact:
            phone = contact.get("phone_number", "")
        elif text and any(c.isdigit() for c in text):
            phone = text.strip()
        if not phone:
            await send_msg(chat_id, "📱 Нажмите кнопку <b>«Поделиться номером»</b> или введите номер вручную:", contact_kb())
            return True
        await save_session(redis, ns, tg_id, {**session, "state": "awaiting_rate", "phone": phone})
        await send_msg(chat_id, f"✅ Телефон: <b>{phone}</b>\n\nВведите вашу <b>почасовую ставку</b> (руб/час):")
        return True

    # Шаг 3 — почасовая ставка
    if session and session.get("state") == "awaiting_rate":
        try:
            rate = float(text.replace(",", "."))
            if rate <= 0: raise ValueError
        except ValueError:
            await send_msg(chat_id, "❌ Введите корректное число (например: 500):")
            return True
        await save_session(redis, ns, tg_id, {**session, "state": "awaiting_warehouse_rate", "rate": rate})
        await send_msg(chat_id, f"✅ Ставка: <b>{rate} руб/час</b>\n\nВведите <b>складскую ставку</b> (руб/час):")
        return True

    # Шаг 4 — складская ставка
    if session and session.get("state") == "awaiting_warehouse_rate":
        try:
            wrate = float(text.replace(",", "."))
            if wrate <= 0: raise ValueError
        except ValueError:
            await send_msg(chat_id, "❌ Введите корректное число:")
            return True
        # Если sqm-работник — спрашиваем ставки за м²
        if tg_id in SQM_IDS:
            await save_session(redis, ns, tg_id, {**session, "state": "awaiting_sqm_tent_rate", "warehouse_rate": wrate})
            await send_msg(chat_id, f"✅ Складская: <b>{wrate} руб/час</b>\n\n🏗 Введите <b>ставку за монтаж шатров</b> (руб/м² — полная, монтаж = полставки):")
        else:
            await save_session(redis, ns, tg_id, {**session, "state": "awaiting_photo", "warehouse_rate": wrate})
            await send_msg(chat_id, f"✅ Складская: <b>{wrate} руб/час</b>\n\n📸 Отправьте вашу <b>фотографию</b> для профиля:")
        return True

    # Шаг 5а — ставка монтаж шатров (только sqm-работники)
    if session and session.get("state") == "awaiting_sqm_tent_rate":
        try:
            sqm_tent = float(text.replace(",", "."))
            if sqm_tent <= 0: raise ValueError
        except ValueError:
            await send_msg(chat_id, "❌ Введите корректное число (например: 70):")
            return True
        await save_session(redis, ns, tg_id, {**session, "state": "awaiting_sqm_floor_rate", "sqm_rate_tent": sqm_tent})
        await send_msg(chat_id, f"✅ Монтаж шатров: <b>{sqm_tent} руб/м²</b>\n\n🏠 Введите <b>ставку за монтаж полов</b> (руб/м²):")
        return True

    # Шаг 5б — ставка монтаж полов
    if session and session.get("state") == "awaiting_sqm_floor_rate":
        try:
            sqm_floor = float(text.replace(",", "."))
            if sqm_floor <= 0: raise ValueError
        except ValueError:
            await send_msg(chat_id, "❌ Введите корректное число:")
            return True
        await save_session(redis, ns, tg_id, {**session, "state": "awaiting_photo", "sqm_rate_floor": sqm_floor})
        await send_msg(chat_id, f"✅ Монтаж полов: <b>{sqm_floor} руб/м²</b>\n\n📸 Отправьте вашу <b>фотографию</b> для профиля:")
        return True

    # Шаг 6 — фото
    if session and session.get("state") == "awaiting_photo":
        photo = msg.get("photo") if msg else None
        photo_id = None
        if photo:
            photo_id = photo[-1]["file_id"]  # берём наибольшее разрешение
        if not photo_id:
            await send_msg(chat_id, "📸 Пожалуйста, отправьте именно <b>фотографию</b> (не файл):")
            return True
        await save_session(redis, ns, tg_id, {**session, "state": "awaiting_agreement", "photo_id": photo_id})
        await send_msg(chat_id, WORKPLACE_POLICIES, agreement_kb())
        return True

    # Шаг 7 — соглашение
    if session and session.get("state") == "awaiting_agreement":
        if "Согласен" in text:
            employee = {
                "name": session["name"],
                "phone": session.get("phone", ""),
                "photo_id": session.get("photo_id", ""),
                "hourly_rate": session["rate"],
                "warehouse_rate": session.get("warehouse_rate", session["rate"]),
                "sqm_rate_tent": session.get("sqm_rate_tent", 0),
                "sqm_rate_floor": session.get("sqm_rate_floor", 0),
                "registered_at": datetime.now(timezone.utc).isoformat(),
                "tg_id": tg_id,
                "policies_accepted": True,
                "policies_accepted_at": datetime.now(timezone.utc).isoformat(),
            }
            await save_employee(redis, ns, tg_id, employee)
            await save_session(redis, ns, tg_id, None)
            await send_msg(chat_id,
                f"🎉 <b>Регистрация завершена!</b>\n\n"
                f"👤 {employee['name']}\n"
                f"📱 {employee['phone']}\n"
                f"💵 Ставка: {session['rate']} руб/час\n"
                f"✅ Правила приняты\n\n"
                f"Используйте кнопки ниже для работы.",
                get_kb(tg_id))
            return True
        elif "Не согласен" in text:
            await save_session(redis, ns, tg_id, None)
            await send_msg(chat_id, "❌ Вы отклонили правила. Регистрация отменена.\n\nДля повторной попытки отправьте /start")
            return True
        else:
            await send_msg(chat_id, "Нажмите <b>✅ Согласен</b> или <b>❌ Не согласен</b>", agreement_kb())
            return True

    return False


# ─────────────────────────────────
# SQM flow — монтаж / демонтаж
# ─────────────────────────────────
async def handle_sqm_flow(chat_id, tg_id, text, session, emp, redis, ns) -> bool:
    if not is_sqm_worker(tg_id) and not is_driver(tg_id):
        return False

    # Кнопка "Монтаж"
    if "Монтаж" in text and not (session and session.get("state", "").startswith("sqm_")):
        logger.info("STEPLOG START sqm_mount")
        projects = await get_projects(redis, ns)
        active = get_active_projects(projects)
        await save_session(redis, ns, tg_id, {"state": "sqm_mount_type"})
        await send_msg(chat_id, "🏗 <b>Монтаж</b>\n\nЧто монтируем?",
            {"keyboard": [[{"text": "🏕 Шатры"}, {"text": "🏠 Полы"}], [{"text": "❌ Отмена"}]], "resize_keyboard": True, "one_time_keyboard": True})
        return True

    # Кнопка "Демонтаж"
    if "Демонтаж" in text and not (session and session.get("state", "").startswith("sqm_")):
        logger.info("STEPLOG START sqm_dismount")
        await save_session(redis, ns, tg_id, {"state": "sqm_dismount_type"})
        await send_msg(chat_id, "🏚 <b>Демонтаж</b>\n\nЧто демонтируем?",
            {"keyboard": [[{"text": "🏕 Шатры"}, {"text": "🏠 Полы"}], [{"text": "❌ Отмена"}]], "resize_keyboard": True, "one_time_keyboard": True})
        return True

    # Старые кнопки монтаж шатров/полов для обратной совместимости
    if "Монтаж шатров" in text and not (session and session.get("state", "").startswith("sqm_")):
        await save_session(redis, ns, tg_id, {"state": "sqm_volume", "work_type": "tent", "is_dismount": False})
        sqm_rate = emp.get("sqm_rate_tent", 0) / 2
        await send_msg(chat_id, f"🏕 <b>Монтаж шатров</b>\n💵 Ставка: <b>{sqm_rate} руб/м²</b>\n\nВведите <b>объём</b> (м²):")
        return True

    if "Монтаж полов" in text and not (session and session.get("state", "").startswith("sqm_")):
        await save_session(redis, ns, tg_id, {"state": "sqm_volume", "work_type": "floor", "is_dismount": False})
        sqm_rate = emp.get("sqm_rate_floor", 0) / 2
        await send_msg(chat_id, f"🏠 <b>Монтаж полов</b>\n💵 Ставка: <b>{sqm_rate} руб/м²</b>\n\nВведите <b>объём</b> (м²):")
        return True

    if not session:
        return False

    state = session.get("state", "")

    # Выбор типа монтажа
    if state == "sqm_mount_type":
        if "Отмена" in text:
            await save_session(redis, ns, tg_id, None)
            await send_msg(chat_id, "Отменено.", get_kb(tg_id))
            return True
        work_type = "tent" if "Шатр" in text else "floor" if "Пол" in text else None
        if not work_type:
            await send_msg(chat_id, "Выберите: Шатры или Полы.")
            return True
        sqm_rate = (emp.get("sqm_rate_tent", 0) if work_type == "tent" else emp.get("sqm_rate_floor", 0)) / 2
        label = "шатров" if work_type == "tent" else "полов"
        # Предлагаем выбрать проект
        projects = await get_projects(redis, ns)
        active = get_active_projects(projects)
        await save_session(redis, ns, tg_id, {**session, "state": "sqm_mount_project", "work_type": work_type, "sqm_rate": sqm_rate, "is_dismount": False})
        if active:
            btns = [[{"text": p["name"]}] for p in active]
            btns.append([{"text": "📦 Без проекта"}])
            await send_msg(chat_id, f"🏗 Монтаж {label}\n💵 Ставка: <b>{sqm_rate} руб/м²</b>\n\n📋 Выберите проект:", {"keyboard": btns, "resize_keyboard": True, "one_time_keyboard": True})
        else:
            await save_session(redis, ns, tg_id, {**session, "state": "sqm_volume", "work_type": work_type, "sqm_rate": sqm_rate, "is_dismount": False, "project": None})
            await send_msg(chat_id, f"🏗 Монтаж {label}\n💵 Ставка: <b>{sqm_rate} руб/м²</b>\n\nВведите <b>объём</b> (м²):")
        return True

    # Выбор типа демонтажа
    if state == "sqm_dismount_type":
        if "Отмена" in text:
            await save_session(redis, ns, tg_id, None)
            await send_msg(chat_id, "Отменено.", get_kb(tg_id))
            return True
        work_type = "tent" if "Шатр" in text else "floor" if "Пол" in text else None
        if not work_type:
            await send_msg(chat_id, "Выберите: Шатры или Полы.")
            return True
        sqm_rate = (emp.get("sqm_rate_tent", 0) if work_type == "tent" else emp.get("sqm_rate_floor", 0)) / 2
        label = "шатров" if work_type == "tent" else "полов"
        projects = await get_projects(redis, ns)
        active = get_active_projects(projects)
        await save_session(redis, ns, tg_id, {**session, "state": "sqm_mount_project", "work_type": work_type, "sqm_rate": sqm_rate, "is_dismount": True})
        if active:
            btns = [[{"text": p["name"]}] for p in active]
            btns.append([{"text": "📦 Без проекта"}])
            await send_msg(chat_id, f"🏚 Демонтаж {label}\n💵 Ставка: <b>{sqm_rate} руб/м²</b>\n\n📋 Выберите проект:", {"keyboard": btns, "resize_keyboard": True, "one_time_keyboard": True})
        else:
            await save_session(redis, ns, tg_id, {**session, "state": "sqm_volume", "work_type": work_type, "sqm_rate": sqm_rate, "is_dismount": True, "project": None})
            await send_msg(chat_id, f"🏚 Демонтаж {label}\n💵 Ставка: <b>{sqm_rate} руб/м²</b>\n\nВведите <b>объём</b> (м²):")
        return True

    # Выбор проекта
    if state == "sqm_mount_project":
        project_name = None if "Без проекта" in text else text.strip()
        await save_session(redis, ns, tg_id, {**session, "state": "sqm_volume", "project": project_name})
        is_d = session.get("is_dismount", False)
        action = "Демонтаж" if is_d else "Монтаж"
        await send_msg(chat_id, f"{'🏚' if is_d else '🏗'} {action}\n{'📋 ' + project_name if project_name else '📦 Без проекта'}\n\nВведите <b>объём</b> (м²):")
        return True

    # Ввод объёма
    if state == "sqm_volume":
        if "Отмена" in text:
            await save_session(redis, ns, tg_id, None)
            await send_msg(chat_id, "Отменено.", get_kb(tg_id))
            return True
        try:
            volume = float(text.replace(",", "."))
            if volume <= 0: raise ValueError
        except ValueError:
            await send_msg(chat_id, "❌ Введите корректное число (например: 25):")
            return True
        sqm_rate = session.get("sqm_rate", 0)
        if not sqm_rate:
            work_type = session.get("work_type", "tent")
            full_rate = emp.get("sqm_rate_tent", 0) if work_type == "tent" else emp.get("sqm_rate_floor", 0)
            sqm_rate = full_rate / 2
        work_type = session.get("work_type", "tent")
        is_dismount = session.get("is_dismount", False)
        project = session.get("project")
        earned = round(sqm_rate * volume, 2)
        action = "Демонтаж" if is_dismount else "Монтаж"
        type_label = "шатров" if work_type == "tent" else "полов"
        icon = "🏚" if is_dismount else ("🏕" if work_type == "tent" else "🏠")
        job = {
            "type": work_type,
            "is_dismount": is_dismount,
            "rate_per_sqm": sqm_rate,
            "volume_sqm": volume,
            "earned": earned,
            "project": project,
            "date": datetime.now(timezone.utc).isoformat()
        }
        jobs = await get_sqm_jobs(redis, ns, tg_id)
        jobs.append(job)
        await save_sqm_jobs(redis, ns, tg_id, jobs)
        await save_session(redis, ns, tg_id, None)
        proj_line = f"\n📋 Проект: <b>{project}</b>" if project else ""
        await send_msg(chat_id,
            f"{icon} <b>{action} {type_label} — записано!</b>\n\n"
            f"📐 Объём: <b>{volume} м²</b>\n"
            f"💵 Ставка: <b>{sqm_rate} руб/м²</b>\n"
            f"💰 Заработано: <b>{earned} руб</b>{proj_line}",
            get_kb(tg_id))
        return True

    # Водитель
    if state == "awaiting_driver_rate":
        try:
            d_rate = float(text.replace(",", "."))
            if d_rate <= 0: raise ValueError
        except ValueError:
            await send_msg(chat_id, "❌ Введите корректное число:")
            return True
        await save_session(redis, ns, tg_id, {"state": "on_driver_shift", "start_time": datetime.now(timezone.utc).isoformat(), "driver_rate": d_rate})
        await send_msg(chat_id, f"🚗 <b>Водительская смена начата!</b>\n💵 Ставка: {d_rate} руб/ч", get_kb(tg_id))
        return True

    return False


# ─────────────────────────────────
# Geolocation
# ─────────────────────────────────
def haversine_distance(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Расстояние между двумя точками в метрах."""
    R = 6371000
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlambda = math.radians(lon2 - lon1)
    a = math.sin(dphi/2)**2 + math.cos(phi1)*math.cos(phi2)*math.sin(dlambda/2)**2
    return R * 2 * math.atan2(math.sqrt(a), math.sqrt(1-a))


async def get_geofence(redis, ns) -> dict | None:
    """Получить настройки геофенса (координаты объекта + радиус)."""
    raw = await redis.get(f"{ns}:geofence")
    return json.loads(raw) if raw else None


async def save_geofence(redis, ns, data: dict):
    await redis.set(f"{ns}:geofence", json.dumps(data, ensure_ascii=False))


async def handle_location(chat_id, tg_id, loc, session, emp, redis, ns):
    logger.info("STEPLOG START geolocation")
    lat, lon = loc["latitude"], loc["longitude"]
    now = datetime.now(timezone.utc).isoformat()

    if session.get("state") == "awaiting_warehouse_start_loc":
        await save_session(redis, ns, tg_id, {"state": "on_warehouse_shift", "start_time": now, "start_lat": lat, "start_lon": lon})
        await send_msg(chat_id, f"📦 <b>Складская смена начата!</b>\n📍 {lat:.5f}, {lon:.5f}", get_kb(tg_id))
        return True

    if session.get("state") == "awaiting_warehouse_end_loc":
        start_time = datetime.fromisoformat(session["start_time"])
        hours = round((datetime.fromisoformat(now) - start_time).total_seconds() / 3600, 2)
        wrate = emp.get("warehouse_rate", emp["hourly_rate"])
        earned = round(hours * wrate, 2)
        shift = {"start_time": session["start_time"], "end_time": now, "start_lat": session["start_lat"], "start_lon": session["start_lon"], "end_lat": lat, "end_lon": lon, "hours": hours, "earned": earned, "type": "warehouse"}
        shifts = await get_shifts(redis, ns, tg_id)
        shifts.append(shift)
        await save_shifts(redis, ns, tg_id, shifts)
        await save_session(redis, ns, tg_id, None)
        await send_msg(chat_id, f"📦 <b>Складская смена завершена!</b>\n\n🕒 {hours} ч\n💰 {earned} руб ({wrate} руб/ч)", get_kb(tg_id))
        return True

    if session.get("state") == "awaiting_start_location":
        # Геофенс — проверка расстояния до объекта
        geofence = await get_geofence(redis, ns)
        geofence_warn = ""
        if geofence and geofence.get("lat") and geofence.get("lon"):
            dist = haversine_distance(lat, lon, geofence["lat"], geofence["lon"])
            radius = geofence.get("radius", 300)
            if dist > radius:
                geofence_warn = f"\n⚠️ <b>Внимание:</b> вы в {int(dist)} м от объекта (допустимо {radius} м)"
                # Уведомить администраторов
                for admin_id in ADMIN_IDS:
                    try:
                        await send_msg(admin_id,
                            f"⚠️ <b>Геофенс нарушен!</b>\n\n"
                            f"👤 {emp['name']}\n"
                            f"📍 Расстояние от объекта: <b>{int(dist)} м</b> (допустимо {radius} м)\n"
                            f"🕒 {datetime.fromisoformat(now).strftime('%H:%M %d.%m.%Y')}")
                    except Exception:
                        pass

        project = session.get("project")
        new_session = {"state": "on_shift", "start_time": now, "start_lat": lat, "start_lon": lon}
        if project: new_session["project"] = project
        await save_session(redis, ns, tg_id, new_session)
        proj_line = f"\n📋 Проект: <b>{project}</b>" if project else ""
        await send_msg(chat_id,
            f"✅ <b>Смена начата!</b>\n"
            f"📍 {lat:.5f}, {lon:.5f}\n"
            f"🕒 {datetime.fromisoformat(now).strftime('%H:%M %d.%m.%Y')}{proj_line}{geofence_warn}\n\n"
            f"Хорошего рабочего дня!",
            get_kb(tg_id))
        return True

    if session.get("state") == "awaiting_end_location":
        start_time = datetime.fromisoformat(session["start_time"])
        hours = round((datetime.fromisoformat(now) - start_time).total_seconds() / 3600, 2)
        earned = round(hours * emp["hourly_rate"], 2)
        shift = {"start_time": session["start_time"], "end_time": now, "start_lat": session["start_lat"], "start_lon": session["start_lon"], "end_lat": lat, "end_lon": lon, "hours": hours, "earned": earned, "project": session.get("project")}
        shifts = await get_shifts(redis, ns, tg_id)
        shifts.append(shift)
        await save_shifts(redis, ns, tg_id, shifts)
        await save_session(redis, ns, tg_id, None)
        await send_msg(chat_id,
            f"🛑 <b>Смена завершена!</b>\n\n"
            f"🕒 Отработано: <b>{hours} ч</b>\n"
            f"💰 Заработано: <b>{earned} руб</b>\n"
            f"📍 {lat:.5f}, {lon:.5f}",
            get_kb(tg_id))
        return True
    return False


# ─────────────────────────────────
# Menu commands
# ─────────────────────────────────
async def handle_command(chat_id, tg_id, text, session, emp, redis, ns):
    if "Начать смену" in text:
        if session and session.get("state") == "on_shift":
            await send_msg(chat_id, "⚠️ У вас уже открыта смена!")
            return True
        # Предлагаем выбрать проект (Golova ±2 дня + локальные)
        local_projects = await get_projects(redis, ns)
        active_local = get_active_projects(local_projects)
        golova_projs = await golova_fetch_projects(days_ahead=2) if GOLOVA_API_KEY else []

        combined = []
        for gp in golova_projs:
            date_str = gp["start_date_time"][:10] if gp.get("start_date_time") else ""
            combined.append({"name": f"🎪 {gp['name']} ({date_str})", "golova_id": gp["id"]})
        for lp in active_local:
            combined.append({"name": lp["name"]})

        if combined:
            btns = [[{"text": p["name"]}] for p in combined]
            btns.append([{"text": "📦 Без проекта"}])
            await save_session(redis, ns, tg_id, {"state": "awaiting_shift_project"})
            await send_msg(chat_id, "📋 <b>Выберите проект для смены:</b>", {"keyboard": btns, "resize_keyboard": True, "one_time_keyboard": True})
        else:
            await save_session(redis, ns, tg_id, {"state": "awaiting_start_location"})
            await send_msg(chat_id, "📍 Отправьте вашу <b>геолокацию</b> для начала смены:", location_kb("📍 Отправить геолокацию"))
        return True

    if session and session.get("state") == "awaiting_shift_project":
        project = None if "Без проекта" in text else text.strip()
        await save_session(redis, ns, tg_id, {"state": "awaiting_start_location", "project": project})
        await send_msg(chat_id, "📍 Отправьте вашу <b>геолокацию</b> для начала смены:", location_kb("📍 Отправить геолокацию"))
        return True

    if "Завершить смену" in text:
        if not session or session.get("state") != "on_shift":
            await send_msg(chat_id, "⚠️ У вас нет открытой смены.", get_kb(tg_id))
            return True
        await save_session(redis, ns, tg_id, {**session, "state": "awaiting_end_location"})
        await send_msg(chat_id, "📍 Отправьте вашу <b>геолокацию</b> для завершения смены:", location_kb("📍 Отправить геолокацию"))
        return True

    if "Водитель: начать" in text and is_driver(tg_id):
        if session and session.get("state") == "on_driver_shift":
            await send_msg(chat_id, "⚠️ Водительская смена уже открыта!")
            return True
        await save_session(redis, ns, tg_id, {"state": "awaiting_driver_rate"})
        await send_msg(chat_id, "🚗 <b>Водитель</b>\n\nВведите <b>ставку</b> (руб/час):")
        return True

    if "Водитель: завершить" in text and is_driver(tg_id):
        if not session or session.get("state") != "on_driver_shift":
            await send_msg(chat_id, "⚠️ Нет открытой водительской смены.", get_kb(tg_id))
            return True
        now_dt = datetime.now(timezone.utc)
        raw_h = (now_dt - datetime.fromisoformat(session["start_time"])).total_seconds() / 3600
        rounded_h = max(1, math.ceil(raw_h))
        d_rate = session["driver_rate"]
        earned = round(rounded_h * d_rate, 2)
        shift = {"start_time": session["start_time"], "end_time": now_dt.isoformat(), "raw_hours": round(raw_h, 2), "hours": rounded_h, "rate": d_rate, "earned": earned, "date": now_dt.isoformat()}
        d_shifts = await get_driver_shifts(redis, ns, tg_id)
        d_shifts.append(shift)
        await save_driver_shifts(redis, ns, tg_id, d_shifts)
        await save_session(redis, ns, tg_id, None)
        await send_msg(chat_id, f"🚗 <b>Водительская смена завершена!</b>\n\n🕒 {round(raw_h,2)} ч → <b>{rounded_h} ч</b>\n💰 <b>{earned} руб</b>", get_kb(tg_id))
        return True

    if "Склад: начать" in text:
        if session and session.get("state") == "on_warehouse_shift":
            await send_msg(chat_id, "⚠️ Складская смена уже открыта!")
            return True
        await save_session(redis, ns, tg_id, {"state": "awaiting_warehouse_start_loc"})
        await send_msg(chat_id, "📦 Отправьте <b>геолокацию</b> для начала складской смены:", location_kb("📍 Отправить геолокацию"))
        return True

    if "Склад: завершить" in text:
        if not session or session.get("state") != "on_warehouse_shift":
            await send_msg(chat_id, "⚠️ Нет открытой складской смены.", get_kb(tg_id))
            return True
        await save_session(redis, ns, tg_id, {**session, "state": "awaiting_warehouse_end_loc"})
        await send_msg(chat_id, "📦 Отправьте <b>геолокацию</b> для завершения складской смены:", location_kb("📍 Отправить геолокацию"))
        return True

    if "Моё расписание" in text:
        schedule = await get_schedule(redis, ns, tg_id)
        today = datetime.now(timezone.utc).date()
        upcoming = sorted([s for s in schedule if datetime.fromisoformat(s["date"]).date() >= today], key=lambda x: x["date"])
        if not upcoming:
            await send_msg(chat_id, "📅 <b>Назначенных смен нет.</b>", get_kb(tg_id))
            return True
        lines = "📅 <b>Моё расписание</b>\n\n"
        for s in upcoming[:10]:
            dt = datetime.fromisoformat(s["date"]).strftime("%d.%m.%Y")
            lines += f"🕒 <b>{dt}</b> — {s.get('time', '-')}\n"
        await send_msg(chat_id, lines, get_kb(tg_id))
        return True

    if "Зарплата" in text:
        await cmd_salary(chat_id, tg_id, emp, redis, ns)
        return True

    if "Записать расход" in text:
        shift_data = None
        expense_project = ""
        if session and session.get("state") in ("on_shift", "on_warehouse_shift"):
            shift_data = dict(session)
            expense_project = session.get("project", "")
        await save_session(redis, ns, tg_id, {
            "state": "awaiting_expense_desc",
            "shift_data": shift_data,
            "expense_project": expense_project,
        })
        await send_msg(chat_id,
            "📝 <b>Опишите расход:</b>\n\nНапример: Бензин, Перчатки, Питание",
            {"keyboard": [[{"text": "❌ Отмена"}]], "resize_keyboard": True, "one_time_keyboard": True}
        )
        return True

    if session and session.get("state") == "awaiting_expense_desc":
        if "Отмена" in text or "❌" in text:
            await save_session(redis, ns, tg_id, session.get("shift_data"))
            await send_msg(chat_id, "❌ Отменено.", get_kb(tg_id))
            return True
        await save_session(redis, ns, tg_id, {**session, "state": "awaiting_expense_amount", "expense_desc": text})
        await send_msg(chat_id,
            f"💰 <b>Сумма расхода (руб):</b>\n\nОписание: <b>{text}</b>",
            {"keyboard": [[{"text": "❌ Отмена"}]], "resize_keyboard": True, "one_time_keyboard": True}
        )
        return True

    if session and session.get("state") == "awaiting_expense_amount":
        if "Отмена" in text or "❌" in text:
            await save_session(redis, ns, tg_id, session.get("shift_data"))
            await send_msg(chat_id, "❌ Отменено.", get_kb(tg_id))
            return True
        try:
            amount = float(text.replace(",", ".").replace(" ", ""))
            if amount <= 0:
                raise ValueError
        except ValueError:
            await send_msg(chat_id, "⚠️ Введите сумму цифрой (например: 500 или 1500.50):")
            return True
        await save_session(redis, ns, tg_id, {**session, "state": "awaiting_expense_photo", "expense_amount": amount})
        desc = session.get("expense_desc", "")
        proj_line = f"\n📋 Проект: <b>{session.get('expense_project')}</b>" if session.get("expense_project") else ""
        await send_msg(chat_id,
            f"📸 <b>Сфотографируйте чек или скриншот:</b>\n\n"
            f"📝 {desc}\n▶️ {amount} руб{proj_line}\n\n"
            f"Отправьте <b>фотографию</b> (не файл!):",
            {"keyboard": [[{"text": "❌ Отмена"}]], "resize_keyboard": True, "one_time_keyboard": True}
        )
        return True

    if "Отменить" in text:
        await cmd_undo(chat_id, tg_id, redis, ns)
        return True

    if "Мой кабинет" in text:
        await cmd_profile(chat_id, tg_id, emp, session, redis, ns)
        return True

    if "Премии" in text or "Штрафы" in text:
        await cmd_bonuses(chat_id, tg_id, redis, ns)
        return True

    return False


async def cmd_salary(chat_id, tg_id, emp, redis, ns):
    logger.info("STEPLOG START salary")
    now = datetime.now(timezone.utc)
    shifts = await get_shifts(redis, ns, tg_id)
    month_shifts = [s for s in shifts if datetime.fromisoformat(s["start_time"]).month == now.month and datetime.fromisoformat(s["start_time"]).year == now.year]
    total_hours = sum(s["hours"] for s in month_shifts)
    base_salary = round(total_hours * emp["hourly_rate"], 2)
    bonuses = await get_bonuses(redis, ns, tg_id)
    month_bonuses = [b for b in bonuses if datetime.fromisoformat(b["date"]).month == now.month and datetime.fromisoformat(b["date"]).year == now.year]
    total_bonuses = sum(b["amount"] for b in month_bonuses if b["type"] == "bonus")
    total_penalties = sum(b["amount"] for b in month_bonuses if b["type"] == "penalty")
    sqm_jobs = await get_sqm_jobs(redis, ns, tg_id)
    month_sqm = [j for j in sqm_jobs if datetime.fromisoformat(j["date"]).month == now.month and datetime.fromisoformat(j["date"]).year == now.year]
    sqm_total = round(sum(j["earned"] for j in month_sqm), 2)
    d_shifts_all = await get_driver_shifts(redis, ns, tg_id)
    month_drv = [d for d in d_shifts_all if datetime.fromisoformat(d["date"]).month == now.month and datetime.fromisoformat(d["date"]).year == now.year]
    driver_total = round(sum(d["earned"] for d in month_drv), 2)
    advances_all = await get_advances(redis, ns, tg_id)
    month_adv = round(sum(a["amount"] for a in advances_all if datetime.fromisoformat(a["date"]).month == now.month and datetime.fromisoformat(a["date"]).year == now.year), 2)
    total = round(base_salary + sqm_total + driver_total + total_bonuses - total_penalties - month_adv, 2)
    sqm_line = f"📐 Монтаж/Демонтаж: <b>{sqm_total} руб</b>\n" if sqm_total > 0 else ""
    drv_line = f"🚗 Водитель: <b>{driver_total} руб</b>\n" if driver_total > 0 else ""
    adv_line = f"💳 Аванс: <b>-{month_adv} руб</b>\n" if month_adv > 0 else ""
    await send_msg(chat_id,
        f"💰 <b>Зарплата за {now.strftime('%B %Y')}</b>\n\n"
        f"🕒 Часы: <b>{round(total_hours, 2)} ч</b>\n"
        f"💵 Ставка: {emp['hourly_rate']} руб/час\n"
        f"💲 Базовая: <b>{base_salary} руб</b>\n"
        f"{sqm_line}{drv_line}{adv_line}"
        f"🎁 Премии: <b>+{total_bonuses} руб</b>\n"
        f"⚠️ Штрафы: <b>-{total_penalties} руб</b>\n\n"
        f"💰 <b>Итого: {total} руб</b>",
        get_kb(tg_id))


async def cmd_profile(chat_id, tg_id, emp, session, redis, ns):
    logger.info("STEPLOG START profile")
    shifts = await get_shifts(redis, ns, tg_id)
    recent = shifts[-5:] if shifts else []
    status = "🟢 На смене" if (session and session.get("state") == "on_shift") else "⚪ Не на смене"
    shifts_text = ""
    for s in reversed(recent):
        st = datetime.fromisoformat(s["start_time"]).strftime("%d.%m %H:%M")
        shifts_text += f"  • {st} — {s['hours']} ч — {s['earned']} руб\n"
    if not shifts_text:
        shifts_text = "  Нет смен\n"
    sqm_line = ""
    if emp.get("sqm_rate_tent"):
        sqm_line = f"🏕 Монтаж шатров: {emp['sqm_rate_tent']/2} руб/м²\n🏠 Монтаж полов: {emp.get('sqm_rate_floor',0)/2} руб/м²\n"
    tg_link = f'<a href="tg://user?id={tg_id}">написать в ЛС</a>'
    await send_msg(chat_id,
        f"👤 <b>Личный кабинет</b>\n\n"
        f"📋 ФИО: <b>{emp['name']}</b>\n"
        f"📱 Тел: {emp.get('phone', '—')}\n"
        f"💵 Ставка: {emp['hourly_rate']} руб/час\n"
        f"{sqm_line}"
        f"📊 Статус: {status}\n"
        f"🕒 Всего смен: {len(shifts)}\n"
        f"🔗 {tg_link}\n\n"
        f"📅 <b>Последние смены:</b>\n{shifts_text}",
        get_kb(tg_id))


async def cmd_undo(chat_id, tg_id, redis, ns):
    logger.info("STEPLOG START undo")
    session = await get_session(redis, ns, tg_id)
    if session:
        state = session.get("state", "")
        if state in ("on_shift", "on_warehouse_shift", "on_driver_shift"):
            await save_session(redis, ns, tg_id, None)
            await send_msg(chat_id, "↩️ <b>Отменено:</b> смена сброшена", get_kb(tg_id))
            return
        if state.startswith("awaiting_") or state.startswith("sqm_") or state.startswith("admin_"):
            await save_session(redis, ns, tg_id, None)
            await send_msg(chat_id, "↩️ <b>Отменено:</b> текущая операция отменена", get_kb(tg_id))
            return
        await save_session(redis, ns, tg_id, None)
        await send_msg(chat_id, "↩️ <b>Отменено:</b> сессия сброшена", get_kb(tg_id))
        return
    candidates = []
    shifts = await get_shifts(redis, ns, tg_id)
    if shifts:
        last = shifts[-1]
        candidates.append((last.get("end_time") or last.get("start_time", ""), "shift", f"Смена: {last.get('hours',0)} ч, {last.get('earned',0)} руб"))
    sqm = await get_sqm_jobs(redis, ns, tg_id)
    if sqm:
        last = sqm[-1]
        action = "Демонтаж" if last.get("is_dismount") else "Монтаж"
        label = "шатров" if last.get("type") == "tent" else "полов"
        candidates.append((last.get("date", ""), "sqm", f"{action} {label}: {last.get('volume_sqm',0)} м², {last.get('earned',0)} руб"))
    d_shifts = await get_driver_shifts(redis, ns, tg_id)
    if d_shifts:
        last = d_shifts[-1]
        candidates.append((last.get("date", ""), "driver", f"Водитель: {last.get('hours',0)} ч, {last.get('earned',0)} руб"))
    if not candidates:
        await send_msg(chat_id, "❌ Нет действий для отмены.", get_kb(tg_id))
        return
    candidates.sort(key=lambda x: x[0], reverse=True)
    latest_type, latest_desc = candidates[0][1], candidates[0][2]
    if latest_type == "shift": shifts.pop(); await save_shifts(redis, ns, tg_id, shifts)
    elif latest_type == "sqm": sqm.pop(); await save_sqm_jobs(redis, ns, tg_id, sqm)
    elif latest_type == "driver": d_shifts.pop(); await save_driver_shifts(redis, ns, tg_id, d_shifts)
    await send_msg(chat_id, f"↩️ <b>Отменено:</b>\n{latest_desc}", get_kb(tg_id))


async def cmd_bonuses(chat_id, tg_id, redis, ns):
    logger.info("STEPLOG START bonuses")
    bonuses = await get_bonuses(redis, ns, tg_id)
    if not bonuses:
        await send_msg(chat_id, "📊 У вас пока нет премий и штрафов.", get_kb(tg_id))
        return
    lines = ""
    for b in reversed(bonuses[-10:]):
        icon = "🎁" if b["type"] == "bonus" else "⚠️"
        sign = "+" if b["type"] == "bonus" else "-"
        dt = datetime.fromisoformat(b["date"]).strftime("%d.%m.%Y")
        lines += f"  {icon} {dt}: <b>{sign}{b['amount']} руб</b> — {b['reason']}\n"
    await send_msg(chat_id, f"📊 <b>Премии и штрафы</b>\n\n{lines}", get_kb(tg_id))


# ─────────────────────────────────
# Admin commands
# ─────────────────────────────────
async def handle_admin_cmd(chat_id, tg_id, text, session, redis, ns) -> bool:
    if not is_admin(tg_id):
        return False

    # Отчёт
    if "Отчёт" in text and not (session and session.get("state", "").startswith("admin_")):
        logger.info("STEPLOG START admin_report")
        now = datetime.now(timezone.utc)
        ids = await get_all_employee_ids(redis, ns)
        if not ids:
            await send_msg(chat_id, "📋 Нет сотрудников.", admin_menu_kb())
            return True
        total_payout = 0
        lines = f"📋 <b>Ведомость за {now.strftime('%B %Y')}</b>\n" + "─" * 28 + "\n"
        for eid in ids:
            emp = await get_employee(redis, ns, int(eid))
            if not emp: continue
            shifts = await get_shifts(redis, ns, int(eid))
            ms = [s for s in shifts if datetime.fromisoformat(s["start_time"]).month == now.month and datetime.fromisoformat(s["start_time"]).year == now.year]
            total_h = sum(s["hours"] for s in ms)
            base = round(total_h * emp["hourly_rate"], 2)
            sqm_j = await get_sqm_jobs(redis, ns, int(eid))
            sqm_earn = round(sum(j["earned"] for j in sqm_j if datetime.fromisoformat(j["date"]).month == now.month and datetime.fromisoformat(j["date"]).year == now.year), 2)
            d_sh = await get_driver_shifts(redis, ns, int(eid))
            drv_earn = round(sum(d["earned"] for d in d_sh if datetime.fromisoformat(d["date"]).month == now.month and datetime.fromisoformat(d["date"]).year == now.year), 2)
            bn = await get_bonuses(redis, ns, int(eid))
            mbn = [b for b in bn if datetime.fromisoformat(b["date"]).month == now.month and datetime.fromisoformat(b["date"]).year == now.year]
            bonus_s = sum(b["amount"] for b in mbn if b["type"] == "bonus")
            penalty_s = sum(b["amount"] for b in mbn if b["type"] == "penalty")
            adv = await get_advances(redis, ns, int(eid))
            adv_s = round(sum(a["amount"] for a in adv if datetime.fromisoformat(a["date"]).month == now.month and datetime.fromisoformat(a["date"]).year == now.year), 2)
            total_emp = round(base + sqm_earn + drv_earn + bonus_s - penalty_s - adv_s, 2)
            total_payout += total_emp
            extras = []
            if sqm_earn > 0: extras.append(f"📐{sqm_earn}р")
            if drv_earn > 0: extras.append(f"🚗{drv_earn}р")
            if bonus_s > 0: extras.append(f"🎁+{bonus_s}р")
            if penalty_s > 0: extras.append(f"⚠️-{penalty_s}р")
            if adv_s > 0: extras.append(f"💳-{adv_s}р")
            extra_str = "  " + " ".join(extras) if extras else ""
            lines += f"• <b>{emp['name']}</b>\n   {round(total_h,1)}ч × {emp['hourly_rate']}р = {base}р{extra_str}\n   💰 <b>{total_emp} руб</b>\n"
        lines += "─" * 28 + f"\n💳 <b>ИТОГО К ВЫПЛАТЕ: {total_payout} руб</b>"
        await send_msg(chat_id, lines, admin_menu_kb())
        return True

    # Изменить ставку
    if "Изм. ставку" in text and not (session and session.get("state", "").startswith("admin_")):
        ids = await get_all_employee_ids(redis, ns)
        if not ids:
            await send_msg(chat_id, "Нет сотрудников.", admin_menu_kb())
            return True
        btns = []
        for eid in ids:
            emp = await get_employee(redis, ns, int(eid))
            if emp: btns.append([{"text": f"{emp['name']} (ID: {eid})"}])
        btns.append([{"text": "❌ Отмена"}])
        await save_session(redis, ns, tg_id, {"state": "admin_edit_rate_pick"})
        await send_msg(chat_id, "⚙️ <b>Изменить ставку</b>\n\nВыберите сотрудника:", {"keyboard": btns, "resize_keyboard": True, "one_time_keyboard": True})
        return True

    if session and session.get("state") == "admin_edit_rate_pick":
        if "Отмена" in text:
            await save_session(redis, ns, tg_id, None)
            await send_msg(chat_id, "Отменено.", admin_menu_kb())
            return True
        m = re.search(r"ID:\s*(\d+)", text)
        if not m:
            await send_msg(chat_id, "Выберите из списка.")
            return True
        tid = int(m.group(1))
        emp = await get_employee(redis, ns, tid)
        if not emp:
            await send_msg(chat_id, "Не найден.", admin_menu_kb())
            return True
        await save_session(redis, ns, tg_id, {"state": "admin_edit_rate_type", "tid": tid, "tname": emp["name"]})
        await send_msg(chat_id,
            f"⚙️ <b>{emp['name']}</b>\n"
            f"💵 Почасовая: {emp['hourly_rate']} руб/ч\n"
            f"📦 Склад: {emp.get('warehouse_rate','—')} руб/ч\n"
            f"🏕 Монтаж шатров: {emp.get('sqm_rate_tent',0)} руб/м²\n"
            f"🏠 Монтаж полов: {emp.get('sqm_rate_floor',0)} руб/м²\n\nЧто изменить?",
            {"keyboard": [[{"text": "💵 Почасовую"}, {"text": "📦 Складскую"}], [{"text": "🏕 Монтаж шатров"}, {"text": "🏠 Монтаж полов"}], [{"text": "❌ Отмена"}]], "resize_keyboard": True, "one_time_keyboard": True})
        return True

    if session and session.get("state") == "admin_edit_rate_type":
        if "Отмена" in text:
            await save_session(redis, ns, tg_id, None)
            await send_msg(chat_id, "Отменено.", admin_menu_kb())
            return True
        if "Почасовую" in text: field, label = "hourly_rate", "почасовую"
        elif "Складскую" in text: field, label = "warehouse_rate", "складскую"
        elif "Монтаж шатров" in text: field, label = "sqm_rate_tent", "монтаж шатров (полная ставка)"
        elif "Монтаж полов" in text: field, label = "sqm_rate_floor", "монтаж полов (полная ставка)"
        else:
            await send_msg(chat_id, "Выберите из списка.")
            return True
        await save_session(redis, ns, tg_id, {**session, "state": "admin_edit_rate_value", "field": field, "label": label})
        await send_msg(chat_id, f"Введите новую <b>{label}</b> ставку:")
        return True

    if session and session.get("state") == "admin_edit_rate_value":
        if "Отмена" in text:
            await save_session(redis, ns, tg_id, None)
            await send_msg(chat_id, "Отменено.", admin_menu_kb())
            return True
        try:
            new_rate = float(text.replace(",", "."))
            if new_rate < 0: raise ValueError
        except ValueError:
            await send_msg(chat_id, "❌ Введите корректное число:")
            return True
        tid = session["tid"]; field = session["field"]; label = session["label"]; tname = session["tname"]
        emp = await get_employee(redis, ns, tid)
        if not emp:
            await save_session(redis, ns, tg_id, None)
            await send_msg(chat_id, "❌ Сотрудник не найден.", admin_menu_kb())
            return True
        old_rate = emp.get(field, 0)
        emp[field] = new_rate
        await save_employee(redis, ns, tid, emp)
        await save_session(redis, ns, tg_id, None)
        await send_msg(chat_id, f"✅ <b>{tname}</b>\n{label}: {old_rate} → <b>{new_rate}</b>", admin_menu_kb())
        try: await send_msg(tid, f"⚙️ Ваша ставка <b>{label}</b> изменена: {old_rate} → <b>{new_rate}</b>", get_kb(tid))
        except Exception: pass
        return True

    # Личное сообщение
    if "Личное сообщение" in text and not (session and session.get("state", "").startswith("admin_")):
        emp_ids = [eid for eid in await get_all_employee_ids(redis, ns) if int(eid) != tg_id]
        if not emp_ids:
            await send_msg(chat_id, "Нет сотрудников.", admin_menu_kb())
            return True
        lines_list = []
        for eid in emp_ids:
            e = await get_employee(redis, ns, int(eid))
            if e: lines_list.append(f"• {e['name']}")
        await save_session(redis, ns, tg_id, {"state": "admin_personal_pick"})
        await send_msg(chat_id, f"📩 <b>Личное сообщение</b>\n\nВведите имя сотрудника:\n\n" + "\n".join(lines_list), {"keyboard": [[{"text": "❌ Отмена"}]], "resize_keyboard": True, "one_time_keyboard": True})
        return True

    if session and session.get("state") == "admin_personal_pick":
        if "Отмена" in text:
            await save_session(redis, ns, tg_id, None)
            await send_msg(chat_id, "Отменено.", admin_menu_kb())
            return True
        found_emp, found_id = None, None
        for eid in await get_all_employee_ids(redis, ns):
            e = await get_employee(redis, ns, int(eid))
            if e and text.strip().lower() in e["name"].lower():
                found_emp, found_id = e, int(eid)
                break
        if not found_emp:
            await send_msg(chat_id, "❌ Не найден:", {"keyboard": [[{"text": "❌ Отмена"}]], "resize_keyboard": True})
            return True
        await save_session(redis, ns, tg_id, {"state": "admin_personal_msg", "tid": found_id, "tname": found_emp["name"]})
        await send_msg(chat_id, f"📩 Для <b>{found_emp['name']}</b>\n\nВведите текст:", {"keyboard": [[{"text": "❌ Отмена"}]], "resize_keyboard": True, "one_time_keyboard": True})
        return True

    if session and session.get("state") == "admin_personal_msg":
        if "Отмена" in text:
            await save_session(redis, ns, tg_id, None)
            await send_msg(chat_id, "Отменено.", admin_menu_kb())
            return True
        worker_id, worker_name = session["tid"], session["tname"]
        try:
            await send_msg(worker_id, f"📩 <b>Сообщение от руководства:</b>\n\n{text}")
            await save_session(redis, ns, tg_id, None)
            await send_msg(chat_id, f"✅ Отправлено <b>{worker_name}</b>", admin_menu_kb())
        except Exception as ex:
            await save_session(redis, ns, tg_id, None)
            await send_msg(chat_id, f"❌ Не удалось: {ex}", admin_menu_kb())
        return True

    # Рассылка
    if "Рассылка" in text and not (session and session.get("state", "").startswith("admin_")):
        count = len(await get_all_employee_ids(redis, ns))
        if count == 0:
            await send_msg(chat_id, "Нет сотрудников.", admin_menu_kb())
            return True
        await save_session(redis, ns, tg_id, {"state": "admin_broadcast"})
        await send_msg(chat_id, f"📨 <b>Рассылка</b> ({count} чел.)\n\nВведите текст:", {"keyboard": [[{"text": "❌ Отмена"}]], "resize_keyboard": True, "one_time_keyboard": True})
        return True

    if session and session.get("state") == "admin_broadcast":
        if "Отмена" in text:
            await save_session(redis, ns, tg_id, None)
            await send_msg(chat_id, "Отменено.", admin_menu_kb())
            return True
        ids = await get_all_employee_ids(redis, ns)
        sent, failed = 0, 0
        for eid in ids:
            if int(eid) == tg_id: continue
            try:
                await send_msg(int(eid), f"📨 <b>Сообщение от руководства:</b>\n\n{text}")
                sent += 1
            except Exception: failed += 1
        await save_session(redis, ns, tg_id, None)
        status = f"✅ Отправлено: {sent}"
        if failed: status += f"\n❌ Не доставлено: {failed}"
        await send_msg(chat_id, f"📨 <b>Рассылка завершена</b>\n\n{status}", admin_menu_kb())
        return True

    # Набор на смену
    if "Набор на смену" in text and not (session and session.get("state", "").startswith("admin_")):
        emp_count = len(await get_all_employee_ids(redis, ns))
        if emp_count == 0:
            await send_msg(chat_id, "Нет сотрудников.", admin_menu_kb())
            return True
        await save_session(redis, ns, tg_id, {"state": "admin_shift_req_desc"})
        await send_msg(chat_id, "👥 <b>Набор на смену</b>\n\nВведите описание:\n<i>25.06, склад, 09:00-18:00</i>", {"keyboard": [[{"text": "❌ Отмена"}]], "resize_keyboard": True, "one_time_keyboard": True})
        return True

    if session and session.get("state") == "admin_shift_req_desc":
        if "Отмена" in text:
            await save_session(redis, ns, tg_id, None)
            await send_msg(chat_id, "Отменено.", admin_menu_kb())
            return True
        await save_session(redis, ns, tg_id, {"state": "admin_shift_req_count", "desc": text})
        await send_msg(chat_id, f"👥 Смена: <b>{text}</b>\n\nСколько сотрудников нужно?", {"keyboard": [[{"text": "❌ Отмена"}]], "resize_keyboard": True, "one_time_keyboard": True})
        return True

    if session and session.get("state") == "admin_shift_req_count":
        if "Отмена" in text:
            await save_session(redis, ns, tg_id, None)
            await send_msg(chat_id, "Отменено.", admin_menu_kb())
            return True
        try:
            count = int(text.strip())
            if count < 1 or count > 100: raise ValueError()
        except ValueError:
            await send_msg(chat_id, "❌ Введите число 1-100:")
            return True
        desc = session["desc"]
        req_id = str(uuid.uuid4())[:8]
        req_data = {"req_id": req_id, "description": desc, "count": count, "applicants": [], "confirmed": [], "message_ids": {}, "admin_id": tg_id}
        await save_shift_request(redis, ns, req_id, req_data)
        emp_ids = await get_all_employee_ids(redis, ns)
        sent = 0
        kb = shift_request_kb(req_id, 0, count)
        for eid in emp_ids:
            wid = int(eid)
            if wid == tg_id: continue
            try:
                mid = await send_inline_msg(wid, f"👥 <b>Требуются сотрудники!</b>\n\n📋 {desc}\n\n👥 Мест: {count}\n\nНажмите кнопку чтобы откликнуться:", kb)
                req_data["message_ids"][str(wid)] = mid
                sent += 1
            except Exception: pass
        await save_shift_request(redis, ns, req_id, req_data)
        await save_session(redis, ns, tg_id, None)
        await send_msg(chat_id, f"✅ <b>Набор опубликован!</b>\n\n📋 {desc}\n👥 Мест: {count}\n📨 Отправлено: {sent}\n🆔 ID: {req_id}", admin_menu_kb())
        return True

    # Расписание (назначить смену нескольким)
    if "Расписание" in text and not (session and session.get("state", "").startswith("admin_")):
        ids = await get_all_employee_ids(redis, ns)
        if not ids:
            await send_msg(chat_id, "Нет сотрудников.", admin_menu_kb())
            return True
        btns = []
        for eid in ids:
            emp = await get_employee(redis, ns, int(eid))
            if emp: btns.append([{"text": f"{emp['name']} (ID: {eid})"}])
        btns.append([{"text": "👥 Всем сразу"}])
        btns.append([{"text": "❌ Отмена"}])
        await save_session(redis, ns, tg_id, {"state": "admin_sched_pick"})
        await send_msg(chat_id, "📅 <b>Назначить смену</b>\n\nВыберите сотрудника или «Всем сразу»:", {"keyboard": btns, "resize_keyboard": True, "one_time_keyboard": True})
        return True

    if session and session.get("state") == "admin_sched_pick":
        if "Отмена" in text:
            await save_session(redis, ns, tg_id, None)
            await send_msg(chat_id, "Отменено.", admin_menu_kb())
            return True
        if "Всем сразу" in text:
            all_ids = await get_all_employee_ids(redis, ns)
            await save_session(redis, ns, tg_id, {"state": "admin_sched_date", "tid": "all", "tname": "Всем сотрудникам", "all_ids": all_ids})
        else:
            m = re.search(r"ID:\s*(\d+)", text)
            if not m:
                await send_msg(chat_id, "Выберите из списка.")
                return True
            tid = int(m.group(1))
            emp2 = await get_employee(redis, ns, tid)
            if not emp2:
                await send_msg(chat_id, "Не найден.", admin_menu_kb())
                return True
            await save_session(redis, ns, tg_id, {"state": "admin_sched_date", "tid": tid, "tname": emp2["name"]})
        await send_msg(chat_id, f"📅 <b>{session.get('tname','')}</b>\n\nВведите <b>дату</b> (ДД.ММ.ГГГГ):\n<code>16.06.2026</code>")
        return True

    if session and session.get("state") == "admin_sched_date":
        if "Отмена" in text:
            await save_session(redis, ns, tg_id, None)
            await send_msg(chat_id, "Отменено.", admin_menu_kb())
            return True
        m = re.match(r"(\d{1,2})\.(\d{1,2})\.(\d{4})", text.strip())
        if not m:
            await send_msg(chat_id, "❌ Формат: ДД.ММ.ГГГГ")
            return True
        try:
            from datetime import date as _date
            d, mo, y = int(m.group(1)), int(m.group(2)), int(m.group(3))
            _date(y, mo, d)
            date_iso = f"{y:04d}-{mo:02d}-{d:02d}"
        except ValueError:
            await send_msg(chat_id, "❌ Неверная дата.")
            return True
        await save_session(redis, ns, tg_id, {**session, "state": "admin_sched_time", "date": date_iso})
        await send_msg(chat_id, f"✅ {text.strip()}\n\nВведите <b>время</b>:\n<code>09:00-18:00</code>")
        return True

    if session and session.get("state") == "admin_sched_time":
        if "Отмена" in text:
            await save_session(redis, ns, tg_id, None)
            await send_msg(chat_id, "Отменено.", admin_menu_kb())
            return True
        date_iso = session["date"]
        tname = session["tname"]
        dt_nice = datetime.fromisoformat(date_iso).strftime("%d.%m.%Y")
        if session.get("tid") == "all":
            # Назначаем всем
            all_ids = session.get("all_ids", [])
            for eid in all_ids:
                schedule = await get_schedule(redis, ns, int(eid))
                schedule.append({"date": date_iso, "time": text.strip(), "assigned_at": datetime.now(timezone.utc).isoformat()})
                await save_schedule(redis, ns, int(eid), schedule)
                try: await send_msg(int(eid), f"📅 <b>Новая смена!</b>\n📅 <b>{dt_nice}</b> — {text.strip()}", get_kb(int(eid)))
                except Exception: pass
            await save_session(redis, ns, tg_id, None)
            await send_msg(chat_id, f"✅ <b>Смена назначена всем</b>\n\n📅 {dt_nice}\n🕒 {text.strip()}", admin_menu_kb())
        else:
            tid = session["tid"]
            schedule = await get_schedule(redis, ns, tid)
            schedule.append({"date": date_iso, "time": text.strip(), "assigned_at": datetime.now(timezone.utc).isoformat()})
            await save_schedule(redis, ns, tid, schedule)
            await save_session(redis, ns, tg_id, None)
            await send_msg(chat_id, f"✅ <b>Смена назначена</b>\n\n👤 {tname}\n📅 {dt_nice}\n🕒 {text.strip()}", admin_menu_kb())
            try: await send_msg(tid, f"📅 <b>Новая смена!</b>\n📅 <b>{dt_nice}</b> — {text.strip()}", get_kb(tid))
            except Exception: pass
        return True

    # Аванс
    if "Аванс" in text and not (session and session.get("state", "").startswith("admin_")):
        ids = await get_all_employee_ids(redis, ns)
        if not ids:
            await send_msg(chat_id, "Нет сотрудников.", admin_menu_kb())
            return True
        btns = []
        for eid in ids:
            emp = await get_employee(redis, ns, int(eid))
            if emp: btns.append([{"text": f"{emp['name']} (ID: {eid})"}])
        btns.append([{"text": "❌ Отмена"}])
        await save_session(redis, ns, tg_id, {"state": "admin_advance_pick"})
        await send_msg(chat_id, "💳 <b>Выдача аванса</b>\n\nВыберите сотрудника:", {"keyboard": btns, "resize_keyboard": True, "one_time_keyboard": True})
        return True

    if session and session.get("state") == "admin_advance_pick":
        if "Отмена" in text:
            await save_session(redis, ns, tg_id, None)
            await send_msg(chat_id, "Отменено.", admin_menu_kb())
            return True
        m = re.search(r"ID:\s*(\d+)", text)
        if not m:
            await send_msg(chat_id, "Выберите из списка.")
            return True
        tid = int(m.group(1))
        emp = await get_employee(redis, ns, tid)
        if not emp:
            await send_msg(chat_id, "Не найден.", admin_menu_kb())
            return True
        advances = await get_advances(redis, ns, tid)
        total_adv = sum(a["amount"] for a in advances)
        await save_session(redis, ns, tg_id, {"state": "admin_advance_amount", "tid": tid, "tname": emp["name"]})
        await send_msg(chat_id, f"💳 <b>{emp['name']}</b>\nВсего авансов: {total_adv} руб\n\nВведите <b>сумму</b> (руб):")
        return True

    if session and session.get("state") == "admin_advance_amount":
        if "Отмена" in text:
            await save_session(redis, ns, tg_id, None)
            await send_msg(chat_id, "Отменено.", admin_menu_kb())
            return True
        try:
            amount = float(text.replace(",", "."))
            if amount <= 0: raise ValueError
        except ValueError:
            await send_msg(chat_id, "❌ Введите корректное число:")
            return True
        tid = session["tid"]; tname = session["tname"]
        advances = await get_advances(redis, ns, tid)
        advances.append({"amount": amount, "date": datetime.now(timezone.utc).isoformat()})
        await save_advances(redis, ns, tid, advances)
        await save_session(redis, ns, tg_id, None)
        await send_msg(chat_id, f"✅ <b>Аванс выдан</b>\n\n👤 {tname}\n💳 {amount} руб", admin_menu_kb())
        try: await send_msg(tid, f"💳 <b>Вам выдан аванс: {amount} руб</b>\nСумма будет вычтена из зарплаты.", get_kb(tid))
        except Exception: pass
        return True

    # Список сотрудников
    if "Сотрудники" in text and not (session and session.get("state", "").startswith("admin_")):
        ids = await get_all_employee_ids(redis, ns)
        if not ids:
            await send_msg(chat_id, "🛠 <b>Нет зарегистрированных сотрудников.</b>", admin_menu_kb())
            return True
        lines = ""
        for eid in ids:
            emp = await get_employee(redis, ns, int(eid))
            if not emp: continue
            ses = await get_session(redis, ns, int(eid))
            st = "🟢" if (ses and ses.get("state") == "on_shift") else "⚪"
            shifts = await get_shifts(redis, ns, int(eid))
            th = sum(s["hours"] for s in shifts)
            tg_link = f'<a href="tg://user?id={eid}">{emp["name"]}</a>'
            lines += f"{st} {tg_link} (ID: {eid})\n    {emp['hourly_rate']} руб/ч · {len(shifts)} смен · {round(th,1)} ч\n    📱 {emp.get('phone','—')}\n\n"
        await send_msg(chat_id, f"🛠 <b>Список сотрудников</b>\n\n{lines}", admin_menu_kb())
        return True

    # Начислить премию/штраф
    if "Начислить" in text and not (session and session.get("state", "").startswith("admin_")):
        ids = await get_all_employee_ids(redis, ns)
        if not ids:
            await send_msg(chat_id, "Нет сотрудников.", admin_menu_kb())
            return True
        btns = []
        for eid in ids:
            emp = await get_employee(redis, ns, int(eid))
            if emp: btns.append([{"text": f"{emp['name']} (ID: {eid})"}])
        btns.append([{"text": "❌ Отмена"}])
        await save_session(redis, ns, tg_id, {"state": "admin_pick_emp"})
        await send_msg(chat_id, "🛠 <b>Выберите сотрудника:</b>", {"keyboard": btns, "resize_keyboard": True, "one_time_keyboard": True})
        return True

    # Уволить
    if "Уволить" in text and not (session and session.get("state", "").startswith("admin_")):
        ids = await get_all_employee_ids(redis, ns)
        if not ids:
            await send_msg(chat_id, "Нет сотрудников.", admin_menu_kb())
            return True
        btns = []
        for eid in ids:
            emp = await get_employee(redis, ns, int(eid))
            if emp: btns.append([{"text": f"{emp['name']} (ID: {eid})"}])
        btns.append([{"text": "❌ Отмена"}])
        await save_session(redis, ns, tg_id, {"state": "admin_fire_pick"})
        await send_msg(chat_id, "🛠 <b>Выберите сотрудника для увольнения:</b>", {"keyboard": btns, "resize_keyboard": True, "one_time_keyboard": True})
        return True

    # Multi-step admin
    if session and session.get("state", "").startswith("admin_"):
        state = session["state"]
        if "Отмена" in text:
            await save_session(redis, ns, tg_id, None)
            await send_msg(chat_id, "Отменено.", admin_menu_kb())
            return True

        if state == "admin_fire_pick":
            m = re.search(r"ID:\s*(\d+)", text)
            if not m:
                await send_msg(chat_id, "Выберите из списка.")
                return True
            tid = int(m.group(1))
            emp = await get_employee(redis, ns, tid)
            if not emp:
                await send_msg(chat_id, "Не найден.", admin_menu_kb())
                return True
            await save_session(redis, ns, tg_id, {"state": "admin_fire_confirm", "tid": tid, "tname": emp["name"]})
            await send_msg(chat_id, f"⚠️ Уволить <b>{emp['name']}</b>?", {"keyboard": [[{"text": "✅ Да, уволить"}], [{"text": "❌ Отмена"}]], "resize_keyboard": True, "one_time_keyboard": True})
            return True

        if state == "admin_fire_confirm":
            if "Да" in text:
                tid = session["tid"]; tname = session["tname"]
                await redis.srem(f"{ns}:employees_index", str(tid))
                await redis.delete(f"{ns}:employee:{tid}")
                await redis.delete(f"{ns}:session:{tid}")
                await save_session(redis, ns, tg_id, None)
                await send_msg(chat_id, f"✅ <b>{tname}</b> уволен.", admin_menu_kb())
                try: await send_msg(tid, "❌ Ваш аккаунт деактивирован. Обратитесь к руководителю.")
                except Exception: pass
                return True
            await save_session(redis, ns, tg_id, None)
            await send_msg(chat_id, "Отменено.", admin_menu_kb())
            return True

        if state == "admin_pick_emp":
            m = re.search(r"ID:\s*(\d+)", text)
            if not m:
                await send_msg(chat_id, "Выберите из списка.")
                return True
            tid = int(m.group(1))
            emp = await get_employee(redis, ns, tid)
            if not emp:
                await send_msg(chat_id, "Не найден.", admin_menu_kb())
                return True
            await save_session(redis, ns, tg_id, {"state": "admin_pick_type", "tid": tid, "tname": emp["name"]})
            await send_msg(chat_id, f"<b>{emp['name']}</b>\nВыберите тип:", {"keyboard": [[{"text": "🎁 Премия"}], [{"text": "⚠️ Штраф"}], [{"text": "❌ Отмена"}]], "resize_keyboard": True, "one_time_keyboard": True})
            return True

        if state == "admin_pick_type":
            bt = "bonus" if "Премия" in text else "penalty" if "Штраф" in text else None
            if not bt:
                await send_msg(chat_id, "Выберите: Премия или Штраф.")
                return True
            await save_session(redis, ns, tg_id, {**session, "state": "admin_amount", "bt": bt})
            await send_msg(chat_id, f"Введите <b>сумму</b> ({'премии' if bt == 'bonus' else 'штрафа'}) в рублях:")
            return True

        if state == "admin_amount":
            try:
                amt = float(text.replace(",", "."))
                if amt <= 0: raise ValueError
            except ValueError:
                await send_msg(chat_id, "Введите корректное число:")
                return True
            await save_session(redis, ns, tg_id, {**session, "state": "admin_reason", "amt": amt})
            await send_msg(chat_id, "Введите <b>причину</b>:")
            return True

        if state == "admin_reason":
            tid = session["tid"]; bt = session["bt"]; amt = session["amt"]; tname = session["tname"]
            bonuses = await get_bonuses(redis, ns, tid)
            entry = {"amount": amt, "reason": text, "type": bt, "date": datetime.now(timezone.utc).isoformat()}
            bonuses.append(entry)
            await save_bonuses(redis, ns, tid, bonuses)
            await save_session(redis, ns, tg_id, None)
            label = "Премия" if bt == "bonus" else "Штраф"
            icon = "🎁" if bt == "bonus" else "⚠️"
            await send_msg(chat_id, f"✅ <b>{label} начислен!</b>\n\n👤 {tname}\n💰 {amt} руб\n📝 {text}", admin_menu_kb())
            try: await send_msg(tid, f"{icon} <b>{label}</b>\n💰 {amt} руб\n📝 {text}", get_kb(tid))
            except Exception: logger.warning("Could not notify employee", tg_id=tid)
            return True

    return False


# ─────────────────────────────────
# Callback query
# ─────────────────────────────────
async def handle_callback_query(callback: dict):
    cq_id = callback["id"]
    tg_id = callback["from"]["id"]
    cq_msg = callback.get("message", {})
    chat_id = cq_msg.get("chat", {}).get("id", tg_id)
    message_id = cq_msg.get("message_id", 0)
    data = callback.get("data", "")

    async with redis_client() as (redis, ns):
        emp = await get_employee(redis, ns, tg_id)

        if data.startswith("apply_shift:"):
            req_id = data.split(":", 1)[1]
            req = await get_shift_request(redis, ns, req_id)
            if not req:
                await tg("answerCallbackQuery", {"callback_query_id": cq_id, "text": "❌ Заявка устарела."})
                return
            applicants = req.get("applicants", [])
            confirmed = req.get("confirmed", [])
            total = req["count"]
            worker_name = emp["name"] if emp else f"ID:{tg_id}"
            if tg_id in [a["id"] for a in applicants]:
                await tg("answerCallbackQuery", {"callback_query_id": cq_id, "text": "⏳ Вы уже откликнулись!"})
                return
            if len(confirmed) >= total:
                await tg("answerCallbackQuery", {"callback_query_id": cq_id, "text": "❌ Все места заняты."})
                return
            applicants.append({"id": tg_id, "name": worker_name})
            req["applicants"] = applicants
            await save_shift_request(redis, ns, req_id, req)
            for admin_id in ADMIN_IDS:
                try:
                    await tg("sendMessage", {"chat_id": admin_id, "text": f"👋 <b>Новый отклик</b>\n\n📋 {req['description']}\n👤 {worker_name}\n\nПодтвердить?", "parse_mode": "HTML", "reply_markup": admin_confirm_kb(req_id, tg_id)})
                except Exception: pass
            await tg("answerCallbackQuery", {"callback_query_id": cq_id, "text": "✅ Отклик отправлен!"})
            await send_msg(tg_id, f"✅ <b>Вы откликнулись!</b>\n\n📋 {req['description']}\n\nОжидайте подтверждения.")

        elif data.startswith("confirm:"):
            if not is_admin(tg_id):
                await tg("answerCallbackQuery", {"callback_query_id": cq_id, "text": "❌ Нет доступа."})
                return
            parts = data.split(":")
            req_id, worker_id = parts[1], int(parts[2])
            req = await get_shift_request(redis, ns, req_id)
            if not req:
                await tg("answerCallbackQuery", {"callback_query_id": cq_id, "text": "❌ Заявка не найдена."})
                return
            confirmed = req.get("confirmed", [])
            total = req["count"]
            applicants = req.get("applicants", [])
            worker_name = next((a["name"] for a in applicants if a["id"] == worker_id), f"ID:{worker_id}")
            if worker_id in confirmed:
                await tg("answerCallbackQuery", {"callback_query_id": cq_id, "text": "Уже подтверждён."})
                return
            if len(confirmed) >= total:
                await tg("answerCallbackQuery", {"callback_query_id": cq_id, "text": "❌ Все места заняты."})
                return
            confirmed.append(worker_id)
            req["confirmed"] = confirmed
            await save_shift_request(redis, ns, req_id, req)
            spots_taken = len(confirmed)
            try:
                await tg("editMessageText", {"chat_id": chat_id, "message_id": message_id, "text": f"✅ <b>Принят:</b> {worker_name}\n📋 {req['description']}\n👥 {spots_taken}/{total}", "parse_mode": "HTML"})
            except Exception: pass
            await send_msg(worker_id, f"✅ <b>Вы приняты на смену!</b>\n\n📋 {req['description']}")
            new_kb = shift_request_kb(req_id, spots_taken, total)
            for uid_str, mid in req.get("message_ids", {}).items():
                try: await edit_inline_markup(int(uid_str), mid, new_kb)
                except Exception: pass
            await tg("answerCallbackQuery", {"callback_query_id": cq_id, "text": f"✅ {worker_name} принят!"})

        elif data.startswith("reject:"):
            if not is_admin(tg_id):
                await tg("answerCallbackQuery", {"callback_query_id": cq_id, "text": "❌ Нет доступа."})
                return
            parts = data.split(":")
            req_id, worker_id = parts[1], int(parts[2])
            req = await get_shift_request(redis, ns, req_id)
            if not req:
                await tg("answerCallbackQuery", {"callback_query_id": cq_id, "text": "❌ Заявка не найдена."})
                return
            applicants = req.get("applicants", [])
            worker_name = next((a["name"] for a in applicants if a["id"] == worker_id), f"ID:{worker_id}")
            req["applicants"] = [a for a in applicants if a["id"] != worker_id]
            await save_shift_request(redis, ns, req_id, req)
            try:
                await tg("editMessageText", {"chat_id": chat_id, "message_id": message_id, "text": f"❌ <b>Отклонён:</b> {worker_name}\n📋 {req['description']}", "parse_mode": "HTML"})
            except Exception: pass
            await send_msg(worker_id, f"❌ <b>Ваш отклик отклонён.</b>\n\n📋 {req['description']}")
            await tg("answerCallbackQuery", {"callback_query_id": cq_id, "text": f"❌ {worker_name} отклонён."})

        else:
            await tg("answerCallbackQuery", {"callback_query_id": cq_id, "text": ""})


# ─────────────────────────────────
# Main dispatcher
# ─────────────────────────────────
async def handle_update(update: dict):
    logger.info("STEPLOG START parse")
    callback = update.get("callback_query")
    if callback:
        await handle_callback_query(callback)
        return
    msg = update.get("message")
    if not msg:
        return

    chat_id = msg["chat"]["id"]
    tg_id = msg["from"]["id"]
    text = msg.get("text", "").strip()
    loc = msg.get("location")

    async with redis_client() as (redis, ns):
        session = await get_session(redis, ns, tg_id)
        emp = await get_employee(redis, ns, tg_id)

        if is_admin(tg_id) and await handle_admin_cmd(chat_id, tg_id, text, session, redis, ns):
            return

        if text == "/start" or (not emp and not session):
            await save_session(redis, ns, tg_id, {"state": "awaiting_name"})
            await send_msg(chat_id, "👋 <b>Добро пожаловать!</b>\n\nДля регистрации введите ваше <b>ФИО</b>:")
            return

        if await handle_registration(chat_id, tg_id, text, msg, session, redis, ns):
            return

        if not emp:
            await save_session(redis, ns, tg_id, {"state": "awaiting_name"})
            await send_msg(chat_id, "Вы не зарегистрированы. Введите ваше <b>ФИО</b>:")
            return

        if is_sqm_worker(tg_id) or is_driver(tg_id):
            if await handle_sqm_flow(chat_id, tg_id, text, session, emp, redis, ns):
                return

        if loc and session:
            if await handle_location(chat_id, tg_id, loc, session, emp, redis, ns):
                return

        # ── Expense receipt photo ─────────────────────────────────
        photo_msg = msg.get("photo")
        if photo_msg and session and session.get("state") == "awaiting_expense_photo":
            photo_id = photo_msg[-1]["file_id"]
            desc = session.get("expense_desc", "Расход")
            amount = float(session.get("expense_amount", 0))
            expense_project = session.get("expense_project", "")
            expense_entry = {
                "id": str(uuid.uuid4()),
                "date": datetime.now(timezone.utc).isoformat(),
                "description": desc,
                "amount": amount,
                "photo_file_id": photo_id,
                "project": expense_project,
            }
            expenses = await get_expenses(redis, ns, tg_id)
            expenses.append(expense_entry)
            await save_expenses(redis, ns, tg_id, expenses)
            # Restore previous shift session
            shift_data = session.get("shift_data")
            await save_session(redis, ns, tg_id, shift_data)
            proj_line = f"\n📋 Проект: <b>{expense_project}</b>" if expense_project else ""
            await send_msg(
                chat_id,
                f"✅ <b>Расход сохранён!</b>\n\n"
                f"📝 {desc}\n💰 {amount} руб{proj_line}\n\n"
                f"Чек прикреплён 📎",
                get_kb(tg_id)
            )
            for admin_id in ADMIN_IDS:
                try:
                    await tg("sendPhoto", {
                        "chat_id": admin_id,
                        "photo": photo_id,
                        "caption": (
                            f"💸 <b>Расход от {emp['name']}</b>\n"
                            f"📝 {desc}\n💰 {amount} руб{proj_line}"
                        ),
                        "parse_mode": "HTML",
                    })
                except Exception:
                    pass
            return

        if text:
            if await handle_command(chat_id, tg_id, text, session, emp, redis, ns):
                return

        await send_msg(chat_id, "Используйте кнопки ниже ⬇️", get_kb(tg_id))


# ─────────────────────────────────
# FastAPI App
# ─────────────────────────────────
app = FastAPI(title="TimeTracker Bot", version="2.0.0")

app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_credentials=True, allow_methods=["*"], allow_headers=["*"])


@app.get("/dashboard")
async def dashboard(credentials: HTTPBasicCredentials = Depends(check_dashboard_auth)):
    return FileResponse("dashboard.html")


@app.get("/golova/projects")
async def get_golova_projects(
    request: Request,
    days_ahead: int = 2,
    month: int = 0,
    year: int = 0,
):
    """Fetch Golova projects. Admin-only: requires X-TG-Init-Data header or Basic Auth."""
    # Check admin via Telegram initData (Mini App) OR Basic Auth (dashboard)
    tg_init_data = request.headers.get("X-TG-Init-Data", "")
    if tg_init_data:
        caller = verify_tg_init_data(tg_init_data)
        if not caller or not is_admin(caller.get("id", 0)):
            raise HTTPException(status_code=403, detail="Admin only")
    else:
        # Fallback: check Basic Auth (same as dashboard)
        auth_header = request.headers.get("Authorization", "")
        if auth_header.startswith("Basic "):
            import base64
            try:
                decoded = base64.b64decode(auth_header[6:]).decode()
                login, password = decoded.split(":", 1)
                dash_login = os.environ.get("DASHBOARD_LOGIN", "admin")
                dash_pass  = os.environ.get("DASHBOARD_PASSWORD", "admin123")
                if not (secrets.compare_digest(login, dash_login) and
                        secrets.compare_digest(password, dash_pass)):
                    raise HTTPException(status_code=403, detail="Admin only")
            except Exception:
                raise HTTPException(status_code=403, detail="Admin only")
        else:
            raise HTTPException(status_code=403, detail="Admin only: send X-TG-Init-Data or Basic Auth")
    if month and year:
        projects = await golova_fetch_month(month, year)
    else:
        projects = await golova_fetch_projects(days_ahead)
    # Strip financial data — only admins use this endpoint, but be explicit
    return {"projects": projects, "count": len(projects)}


@app.get("/miniapp")
async def serve_miniapp():
    """Serve Telegram Mini App HTML (no Basic Auth — auth handled client-side via initData)."""
    return FileResponse("miniapp.html")


@app.post("/miniapp/verify-admin")
async def verify_miniapp_admin(request: Request):
    """Verify Telegram initData server-side and confirm admin status."""
    body = await request.json()
    init_data = body.get("initData", "")
    if not init_data:
        raise HTTPException(status_code=400, detail="initData required")
    user = verify_tg_init_data(init_data)
    if not user:
        raise HTTPException(status_code=401, detail="Invalid initData")
    return {"ok": True, "is_admin": is_admin(user.get("id", 0)), "user": user}


class SetWebhookRequest(BaseModel):
    webhook_url: str


class SetWebhookResponse(BaseModel):
    success: bool
    bot_configured: bool
    message: str


@app.post("/", response_model=SetWebhookResponse)
async def set_telegram_webhook(request: SetWebhookRequest):
    token = get_token()
    async with httpx.AsyncClient(timeout=15) as c:
        r = await c.post(f"https://api.telegram.org/bot{token}/setWebhook", json={"url": request.webhook_url})
        r.raise_for_status()
        data = r.json()
    return SetWebhookResponse(success=data.get("ok", False), bot_configured=True, message=data.get("description", "Webhook set"))


@app.post("/webhook")
async def telegram_webhook(request: Request):
    raw = await request.json()
    update = raw["payload"] if ("payload" in raw and "defaultInputs" in raw) else raw
    logger.info("Telegram update received", update_id=update.get("update_id"))
    await handle_update(update)
    return {"ok": True}


@app.post("/send-weekly-summary")
async def send_weekly_summary_endpoint():
    now = datetime.now(timezone.utc)
    sent, failed = 0, 0
    async with redis_client() as (redis, ns):
        ids = await get_all_employee_ids(redis, ns)
        for eid in ids:
            try:
                tg_id = int(eid)
                emp = await get_employee(redis, ns, tg_id)
                if not emp: continue
                shifts = await get_shifts(redis, ns, tg_id)
                ms = [s for s in shifts if datetime.fromisoformat(s["start_time"]).month == now.month and datetime.fromisoformat(s["start_time"]).year == now.year]
                total_h = sum(s["hours"] for s in ms)
                base = round(total_h * emp["hourly_rate"], 2)
                sqm_j = await get_sqm_jobs(redis, ns, tg_id)
                sqm_earn = round(sum(j["earned"] for j in sqm_j if datetime.fromisoformat(j["date"]).month == now.month and datetime.fromisoformat(j["date"]).year == now.year), 2)
                d_sh = await get_driver_shifts(redis, ns, tg_id)
                drv_earn = round(sum(d["earned"] for d in d_sh if datetime.fromisoformat(d["date"]).month == now.month and datetime.fromisoformat(d["date"]).year == now.year), 2)
                bn = await get_bonuses(redis, ns, tg_id)
                mbn = [b for b in bn if datetime.fromisoformat(b["date"]).month == now.month and datetime.fromisoformat(b["date"]).year == now.year]
                bonus_s = sum(b["amount"] for b in mbn if b["type"] == "bonus")
                penalty_s = sum(b["amount"] for b in mbn if b["type"] == "penalty")
                adv_all = await get_advances(redis, ns, tg_id)
                adv_s = round(sum(a["amount"] for a in adv_all if datetime.fromisoformat(a["date"]).month == now.month and datetime.fromisoformat(a["date"]).year == now.year), 2)
                total = round(base + sqm_earn + drv_earn + bonus_s - penalty_s - adv_s, 2)
                lines = [f"📅 <b>Сводка за {now.strftime('%B %Y')}</b>", f"🕒 Часы: <b>{round(total_h,2)} ч</b> ({len(ms)} смен)", f"💲 Базовая: {base} руб"]
                if sqm_earn > 0: lines.append(f"📐 Монтаж: {sqm_earn} руб")
                if drv_earn > 0: lines.append(f"🚗 Водитель: {drv_earn} руб")
                if bonus_s > 0: lines.append(f"🎁 Премии: +{bonus_s} руб")
                if penalty_s > 0: lines.append(f"⚠️ Штрафы: -{penalty_s} руб")
                if adv_s > 0: lines.append(f"💳 Аванс: -{adv_s} руб")
                lines.append(f"\n💰 <b>Накоплено: {total} руб</b>")
                await send_msg(tg_id, "\n".join(lines), get_kb(tg_id))
                sent += 1
            except Exception as e:
                logger.warning("Could not send summary", tg_id=eid, error=str(e))
                failed += 1
    return {"sent": sent, "failed": failed}


@app.api_route("/employees", methods=["GET", "POST"])
async def list_employees():
    async with redis_client() as (redis, ns):
        ids = await get_all_employee_ids(redis, ns)
        employees = []
        for eid in ids:
            emp = await get_employee(redis, ns, int(eid))
            if emp:
                session = await get_session(redis, ns, int(eid))
                on_shift = bool(session and session.get("state") == "on_shift")
                shifts = await get_shifts(redis, ns, int(eid))
                total_hours = sum(s["hours"] for s in shifts)
                employees.append({**emp, "on_shift": on_shift, "total_shifts": len(shifts), "total_hours": round(total_hours, 2)})
    return {"employees": employees}


@app.api_route("/employees/{tg_id}/shifts", methods=["GET", "POST"])
async def get_employee_shifts(tg_id: int):
    async with redis_client() as (redis, ns):
        emp = await get_employee(redis, ns, tg_id)
        shifts = await get_shifts(redis, ns, tg_id)
    return {"employee": emp, "shifts": shifts}


@app.api_route("/employees/{tg_id}/salary", methods=["GET", "POST"])
async def get_employee_salary(tg_id: int, month: Optional[int] = None, year: Optional[int] = None):
    now = datetime.now(timezone.utc)
    m = month or now.month
    y = year or now.year
    async with redis_client() as (redis, ns):
        emp = await get_employee(redis, ns, tg_id)
        if not emp: raise HTTPException(status_code=404, detail="Employee not found")
        shifts = await get_shifts(redis, ns, tg_id)
        bonuses = await get_bonuses(redis, ns, tg_id)
        advances = await get_advances(redis, ns, tg_id)
    month_shifts = [s for s in shifts if datetime.fromisoformat(s["start_time"]).month == m and datetime.fromisoformat(s["start_time"]).year == y]
    total_hours = sum(s["hours"] for s in month_shifts)
    base = round(total_hours * emp["hourly_rate"], 2)
    month_bonuses = [b for b in bonuses if datetime.fromisoformat(b["date"]).month == m and datetime.fromisoformat(b["date"]).year == y]
    bonus_sum = sum(b["amount"] for b in month_bonuses if b["type"] == "bonus")
    penalty_sum = sum(b["amount"] for b in month_bonuses if b["type"] == "penalty")
    adv_sum = round(sum(a["amount"] for a in advances if datetime.fromisoformat(a["date"]).month == m and datetime.fromisoformat(a["date"]).year == y), 2)
    total = round(base + bonus_sum - penalty_sum - adv_sum, 2)
    return {"employee": emp["name"], "tg_id": tg_id, "hourly_rate": emp["hourly_rate"], "month": m, "year": y, "hours": round(total_hours, 2), "base_salary": base, "bonuses": bonus_sum, "penalties": penalty_sum, "advances": adv_sum, "total": total, "shifts_count": len(month_shifts)}


class BonusRequest(BaseModel):
    amount: float = Field(..., gt=0)
    reason: str
    bonus_type: Literal["bonus", "penalty"]


@app.get("/employees/{tg_id}/expenses")
async def get_employee_expenses_api(tg_id: int):
    """Get all expenses for an employee (admin)."""
    async with redis_client() as (redis, ns):
        emp = await get_employee(redis, ns, tg_id)
        if not emp:
            raise HTTPException(status_code=404, detail="Employee not found")
        expenses = await get_expenses(redis, ns, tg_id)
    return {
        "employee": emp["name"],
        "tg_id": tg_id,
        "expenses": expenses,
        "total_amount": round(sum(e["amount"] for e in expenses), 2),
    }


@app.get("/expenses")
async def list_all_expenses_api():
    """List all expenses from all employees (admin)."""
    async with redis_client() as (redis, ns):
        ids = await get_all_employee_ids(redis, ns)
        result = []
        for eid in ids:
            tid = int(eid)
            emp = await get_employee(redis, ns, tid)
            if not emp:
                continue
            for e in await get_expenses(redis, ns, tid):
                result.append({**e, "employee_name": emp["name"], "tg_id": tid})
    result.sort(key=lambda x: x.get("date", ""), reverse=True)
    return {
        "expenses": result,
        "total_count": len(result),
        "total_amount": round(sum(e["amount"] for e in result), 2),
    }


@app.post("/employees/{tg_id}/bonus")
async def add_bonus(tg_id: int, req: BonusRequest, request: Request):
    # Server-side admin check when called from Mini App
    tg_init_data = request.headers.get("X-TG-Init-Data", "")
    if tg_init_data:
        caller = verify_tg_init_data(tg_init_data)
        if not caller or not is_admin(caller.get("id", 0)):
            raise HTTPException(status_code=403, detail="Admin only")
    async with redis_client() as (redis, ns):
        emp = await get_employee(redis, ns, tg_id)
        if not emp: raise HTTPException(status_code=404, detail="Employee not found")
        bonuses = await get_bonuses(redis, ns, tg_id)
        entry = {"amount": req.amount, "reason": req.reason, "type": req.bonus_type, "date": datetime.now(timezone.utc).isoformat()}
        bonuses.append(entry)
        await save_bonuses(redis, ns, tg_id, bonuses)
    icon = "🎁" if req.bonus_type == "bonus" else "⚠️"
    label = "Премия" if req.bonus_type == "bonus" else "Штраф"
    try:
        await send_msg(tg_id, f"{icon} <b>{label}</b>\n💰 {req.amount} руб\n📝 {req.reason}", get_kb(tg_id))
    except Exception:
        logger.warning("Could not notify employee", tg_id=tg_id)
    return {"success": True, "entry": entry}


# Проекты API
class ProjectRequest(BaseModel):
    name: str
    date_start: str
    date_end: str
    location: Optional[str] = ""
    status: str = "active"


@app.get("/projects")
async def get_projects_api():
    async with redis_client() as (redis, ns):
        projects = await get_projects(redis, ns)
    return {"projects": projects}


@app.post("/projects")
async def create_project(req: ProjectRequest, request: Request):
    tg_init_data = request.headers.get("X-TG-Init-Data", "")
    user = verify_tg_init_data(tg_init_data)
    if not user or user.get("id") not in ADMIN_IDS:
        raise HTTPException(status_code=403, detail="Admin access required")
    async with redis_client() as (redis, ns):
        projects = await get_projects(redis, ns)
        project = {"id": str(uuid.uuid4())[:8], "name": req.name, "date_start": req.date_start, "date_end": req.date_end, "location": req.location, "status": req.status, "created_at": datetime.now(timezone.utc).isoformat()}
        projects.append(project)
        await save_projects(redis, ns, projects)
    return {"success": True, "project": project}


@app.put("/projects/{project_id}/status")
async def update_project_status(project_id: str, status: str, request: Request):
    tg_init_data = request.headers.get("X-TG-Init-Data", "")
    user = verify_tg_init_data(tg_init_data)
    if not user or user.get("id") not in ADMIN_IDS:
        raise HTTPException(status_code=403, detail="Admin access required")
    async with redis_client() as (redis, ns):
        projects = await get_projects(redis, ns)
        for p in projects:
            if p["id"] == project_id:
                p["status"] = status
                break
        await save_projects(redis, ns, projects)
    return {"success": True}


# ── Шаблоны проектов ────────────────
class ProjectTemplateMember(BaseModel):
    role: str  # описание роли, например "Монтажник шатров"
    count: int = 1  # сколько человек на этой роли


class ProjectTemplateRequest(BaseModel):
    name: str  # название шаблона, например "Свадьба стандарт"
    default_location: Optional[str] = ""
    default_duration_days: int = 1  # сколько дней обычно длится монтаж+мероприятие
    members: list[ProjectTemplateMember] = []
    notes: Optional[str] = ""


@app.get("/project-templates")
async def get_project_templates_api():
    async with redis_client() as (redis, ns):
        templates = await get_project_templates(redis, ns)
    return {"templates": templates}


@app.post("/project-templates")
async def create_project_template(req: ProjectTemplateRequest, request: Request):
    tg_init_data = request.headers.get("X-TG-Init-Data", "")
    user = verify_tg_init_data(tg_init_data)
    if not user or user.get("id") not in ADMIN_IDS:
        raise HTTPException(status_code=403, detail="Admin access required")
    async with redis_client() as (redis, ns):
        templates = await get_project_templates(redis, ns)
        template = {
            "id": str(uuid.uuid4())[:8],
            "name": req.name,
            "default_location": req.default_location,
            "default_duration_days": req.default_duration_days,
            "members": [m.dict() for m in req.members],
            "notes": req.notes,
            "created_at": datetime.now(timezone.utc).isoformat()
        }
        templates.append(template)
        await save_project_templates(redis, ns, templates)
    return {"success": True, "template": template}


@app.delete("/project-templates/{template_id}")
async def delete_project_template(template_id: str, request: Request):
    tg_init_data = request.headers.get("X-TG-Init-Data", "")
    user = verify_tg_init_data(tg_init_data)
    if not user or user.get("id") not in ADMIN_IDS:
        raise HTTPException(status_code=403, detail="Admin access required")
    async with redis_client() as (redis, ns):
        templates = await get_project_templates(redis, ns)
        templates = [t for t in templates if t["id"] != template_id]
        await save_project_templates(redis, ns, templates)
    return {"success": True}


class ProjectFromTemplateRequest(BaseModel):
    template_id: str
    name: str  # конкретное название проекта, например "Свадьба Ивановых"
    date_start: str
    date_end: Optional[str] = None  # если не указано — считается из default_duration_days
    location: Optional[str] = None  # если не указано — берётся из шаблона


@app.post("/projects/from-template")
async def create_project_from_template(req: ProjectFromTemplateRequest, request: Request):
    tg_init_data = request.headers.get("X-TG-Init-Data", "")
    user = verify_tg_init_data(tg_init_data)
    if not user or user.get("id") not in ADMIN_IDS:
        raise HTTPException(status_code=403, detail="Admin access required")
    async with redis_client() as (redis, ns):
        templates = await get_project_templates(redis, ns)
        template = next((t for t in templates if t["id"] == req.template_id), None)
        if not template:
            raise HTTPException(status_code=404, detail="Template not found")

        date_end = req.date_end
        if not date_end:
            from datetime import timedelta
            start_dt = datetime.fromisoformat(req.date_start)
            end_dt = start_dt + timedelta(days=template.get("default_duration_days", 1) - 1)
            date_end = end_dt.date().isoformat()

        projects = await get_projects(redis, ns)
        project = {
            "id": str(uuid.uuid4())[:8],
            "name": req.name,
            "date_start": req.date_start,
            "date_end": date_end,
            "location": req.location if req.location is not None else template.get("default_location", ""),
            "status": "active",
            "template_id": req.template_id,
            "planned_members": template.get("members", []),
            "created_at": datetime.now(timezone.utc).isoformat()
        }
        projects.append(project)
        await save_projects(redis, ns, projects)

    return {"success": True, "project": project}


# ── Прогноз ФОТ на месяц ────────────
@app.get("/payroll-forecast")
async def payroll_forecast():
    """
    Прогноз ФОТ на текущий месяц.
    Считает факт по сегодня + проецирует средний дневной темп на оставшиеся дни,
    плюс учитывает уже назначенные смены в расписании.
    """
    now = datetime.now(timezone.utc)
    today = now.date()
    days_in_month = (datetime(now.year, now.month % 12 + 1, 1) - datetime(now.year, now.month, 1)).days if now.month < 12 else 31
    days_passed = today.day
    days_remaining = days_in_month - days_passed

    total_fact = 0.0
    total_scheduled_shifts = 0
    employee_breakdown = []

    async with redis_client() as (redis, ns):
        ids = await get_all_employee_ids(redis, ns)
        for eid in ids:
            tg_id = int(eid)
            emp = await get_employee(redis, ns, tg_id)
            if not emp: continue

            shifts = await get_shifts(redis, ns, tg_id)
            month_shifts = [s for s in shifts if datetime.fromisoformat(s["start_time"]).month == now.month and datetime.fromisoformat(s["start_time"]).year == now.year]
            total_h = sum(s["hours"] for s in month_shifts)
            base = round(total_h * emp["hourly_rate"], 2)

            sqm_j = await get_sqm_jobs(redis, ns, tg_id)
            sqm_earn = round(sum(j["earned"] for j in sqm_j if datetime.fromisoformat(j["date"]).month == now.month and datetime.fromisoformat(j["date"]).year == now.year), 2)

            d_sh = await get_driver_shifts(redis, ns, tg_id)
            drv_earn = round(sum(d["earned"] for d in d_sh if datetime.fromisoformat(d["date"]).month == now.month and datetime.fromisoformat(d["date"]).year == now.year), 2)

            bn = await get_bonuses(redis, ns, tg_id)
            mbn = [b for b in bn if datetime.fromisoformat(b["date"]).month == now.month and datetime.fromisoformat(b["date"]).year == now.year]
            bonus_s = sum(b["amount"] for b in mbn if b["type"] == "bonus")
            penalty_s = sum(b["amount"] for b in mbn if b["type"] == "penalty")

            emp_fact = round(base + sqm_earn + drv_earn + bonus_s - penalty_s, 2)
            total_fact += emp_fact

            # Считаем сколько смен назначено в расписании на оставшиеся дни
            schedule = await get_schedule(redis, ns, tg_id)
            future_scheduled = [s for s in schedule if today.isoformat() < s.get("date", "") <= (today.replace(day=days_in_month)).isoformat()]
            total_scheduled_shifts += len(future_scheduled)

            # Прогноз для сотрудника: средний дневной заработок * (дни прошедшие + назначенные смены)
            daily_avg = emp_fact / days_passed if days_passed > 0 else 0
            emp_forecast = round(emp_fact + daily_avg * len(future_scheduled), 2)

            employee_breakdown.append({
                "name": emp["name"],
                "tg_id": tg_id,
                "fact": emp_fact,
                "scheduled_shifts": len(future_scheduled),
                "forecast": emp_forecast
            })

        total_forecast = sum(e["forecast"] for e in employee_breakdown)
        daily_avg_total = total_fact / days_passed if days_passed > 0 else 0
        simple_projection = round(daily_avg_total * days_in_month, 2)

    return {
        "month": now.month,
        "year": now.year,
        "days_passed": days_passed,
        "days_remaining": days_remaining,
        "days_in_month": days_in_month,
        "total_fact": round(total_fact, 2),
        "total_forecast": round(total_forecast, 2),
        "simple_projection": simple_projection,
        "scheduled_shifts_remaining": total_scheduled_shifts,
        "employee_breakdown": sorted(employee_breakdown, key=lambda x: x["forecast"], reverse=True)
    }


# ── Геофенс ───────────────────────
class GeofenceRequest(BaseModel):
    lat: float
    lon: float
    radius: int = 300
    name: Optional[str] = ""


@app.get("/geofence")
async def get_geofence_api():
    async with redis_client() as (redis, ns):
        gf = await get_geofence(redis, ns)
    return {"geofence": gf}


@app.post("/geofence")
async def set_geofence_api(req: GeofenceRequest, request: Request):
    tg_init_data = request.headers.get("X-TG-Init-Data", "")
    user = verify_tg_init_data(tg_init_data)
    if not user or user.get("id") not in ADMIN_IDS:
        raise HTTPException(status_code=403, detail="Admin access required")
    async with redis_client() as (redis, ns):
        await save_geofence(redis, ns, {"lat": req.lat, "lon": req.lon, "radius": req.radius, "name": req.name})
    return {"success": True}


# ── Проверка невыходов ─────────────
@app.post("/check-no-shows")
async def check_no_shows():
    """
    Вызывать по расписанию (например каждые 15 минут через Railway cron).
    Проверяет у кого назначена смена сегодня но не открыта — шлёт уведомление админу.
    """
    now = datetime.now(timezone.utc)
    today = now.date().isoformat()
    current_time = now.strftime("%H:%M")
    no_shows = []

    async with redis_client() as (redis, ns):
        ids = await get_all_employee_ids(redis, ns)
        for eid in ids:
            tg_id = int(eid)
            emp = await get_employee(redis, ns, tg_id)
            if not emp: continue
            schedule = await get_schedule(redis, ns, tg_id)
            session = await get_session(redis, ns, tg_id)
            on_shift = bool(session and session.get("state") in ("on_shift", "on_warehouse_shift", "on_driver_shift"))

            for s in schedule:
                if s.get("date") != today: continue
                # Парсим время начала из строки типа "09:00-18:00" или "09:00"
                time_str = s.get("time", "")
                start_time_str = time_str.split("-")[0].strip() if "-" in time_str else time_str.strip()
                if not start_time_str or ":" not in start_time_str: continue

                # Проверяем что время уже прошло (+ 15 минут grace period)
                try:
                    sh, sm = map(int, start_time_str.split(":"))
                    scheduled_minutes = sh * 60 + sm
                    current_minutes = now.hour * 60 + now.minute
                    if current_minutes < scheduled_minutes + 15: continue  # ещё не опоздал
                except Exception:
                    continue

                if not on_shift:
                    no_shows.append({"tg_id": tg_id, "name": emp["name"], "scheduled": start_time_str, "late_min": current_minutes - scheduled_minutes})

        if no_shows:
            lines = "⚠️ <b>Не вышли на смену:</b>\n\n"
            for ns_item in no_shows:
                lines += f"👤 {ns_item['name']} — смена в {ns_item['scheduled']}, опоздание <b>{ns_item['late_min']} мин</b>\n"
            for admin_id in ADMIN_IDS:
                try:
                    await send_msg(admin_id, lines)
                except Exception:
                    pass

    return {"no_shows": no_shows, "checked_at": now.isoformat()}


# ── Длинные смены ──────────────────
@app.post("/check-long-shifts")
async def check_long_shifts():
    """Уведомить админа если смена открыта больше 12 часов."""
    now = datetime.now(timezone.utc)
    long_shifts = []

    async with redis_client() as (redis, ns):
        ids = await get_all_employee_ids(redis, ns)
        for eid in ids:
            tg_id = int(eid)
            emp = await get_employee(redis, ns, tg_id)
            if not emp: continue
            session = await get_session(redis, ns, tg_id)
            if not session or session.get("state") not in ("on_shift", "on_warehouse_shift"): continue
            start_time = session.get("start_time")
            if not start_time: continue
            hours = (now - datetime.fromisoformat(start_time)).total_seconds() / 3600
            if hours >= 12:
                long_shifts.append({"tg_id": tg_id, "name": emp["name"], "hours": round(hours, 1)})

        if long_shifts:
            lines = "⏰ <b>Долгие смены (12+ часов):</b>\n\n"
            for ls in long_shifts:
                lines += f"👤 {ls['name']} — <b>{ls['hours']} ч</b>\n"
            for admin_id in ADMIN_IDS:
                try:
                    await send_msg(admin_id, lines)
                except Exception:
                    pass

    return {"long_shifts": long_shifts}


# ── Итоговый отчёт по проекту ───────
class ProjectReportRequest(BaseModel):
    project_name: str
    ratings: Optional[dict] = {}  # {tg_id: {"avg": 4.5, "count": 3}} — передаётся с фронта из localStorage


@app.post("/projects/report")
async def project_report(req: ProjectReportRequest):
    """
    Собирает полный отчёт по проекту: кто работал, сколько часов/м²/денег,
    были ли опоздания (из расписания), какой рейтинг (передаётся с фронта).
    """
    project_name = req.project_name
    workers = {}  # tg_id -> агрегированные данные

    async with redis_client() as (redis, ns):
        projects = await get_projects(redis, ns)
        project = next((p for p in projects if p["name"] == project_name), None)

        ids = await get_all_employee_ids(redis, ns)
        for eid in ids:
            tg_id = int(eid)
            emp = await get_employee(redis, ns, tg_id)
            if not emp: continue

            entry = {"name": emp["name"], "tg_id": tg_id, "hours": 0.0, "shift_earned": 0.0, "sqm_volume": 0.0, "sqm_earned": 0.0, "shifts_count": 0, "sqm_jobs_count": 0}
            touched = False

            shifts = await get_shifts(redis, ns, tg_id)
            for s in shifts:
                if s.get("project") == project_name:
                    entry["hours"] += s.get("hours", 0)
                    entry["shift_earned"] += s.get("earned", 0)
                    entry["shifts_count"] += 1
                    touched = True

            sqm_jobs = await get_sqm_jobs(redis, ns, tg_id)
            for j in sqm_jobs:
                if j.get("project") == project_name:
                    entry["sqm_volume"] += j.get("volume_sqm", 0)
                    entry["sqm_earned"] += j.get("earned", 0)
                    entry["sqm_jobs_count"] += 1
                    touched = True

            # Опоздания — смотрим расписание этого сотрудника на даты проекта
            late_count = 0
            if project:
                schedule = await get_schedule(redis, ns, tg_id)
                date_start = project.get("date_start", "")
                date_end = project.get("date_end", "")
                relevant_sched = [s for s in schedule if date_start <= s.get("date", "") <= date_end]
                for sched_item in relevant_sched:
                    # Проверяем была ли смена начата в этот день
                    sched_date = sched_item.get("date", "")
                    day_shifts = [s for s in shifts if s.get("start_time", "").startswith(sched_date)]
                    if not day_shifts:
                        late_count += 1  # назначена смена но не вышел вообще
                        touched = True
            entry["late_count"] = late_count

            if touched:
                entry["total_earned"] = round(entry["shift_earned"] + entry["sqm_earned"], 2)
                rating = req.ratings.get(str(tg_id)) if req.ratings else None
                entry["rating"] = rating
                workers[tg_id] = entry

    workers_list = sorted(workers.values(), key=lambda x: x["total_earned"], reverse=True)
    total_payout = sum(w["total_earned"] for w in workers_list)
    total_hours = sum(w["hours"] for w in workers_list)
    total_sqm = sum(w["sqm_volume"] for w in workers_list)
    total_late = sum(w["late_count"] for w in workers_list)

    return {
        "project_name": project_name,
        "project": project,
        "workers": workers_list,
        "total_payout": round(total_payout, 2),
        "total_hours": round(total_hours, 2),
        "total_sqm": round(total_sqm, 2),
        "total_late_count": total_late,
        "workers_count": len(workers_list)
    }


@app.post("/projects/report/send-telegram")
async def send_project_report_telegram(req: ProjectReportRequest):
    """Отправляет итоговый отчёт по проекту администраторам в Telegram."""
    report = await project_report(req)

    lines = f"📋 <b>Отчёт по проекту «{report['project_name']}»</b>\n" + "─" * 28 + "\n\n"
    lines += f"👥 Сотрудников: <b>{report['workers_count']}</b>\n"
    lines += f"🕒 Часов: <b>{report['total_hours']}</b>\n"
    if report['total_sqm'] > 0:
        lines += f"📐 Монтаж/демонтаж: <b>{report['total_sqm']} м²</b>\n"
    lines += f"💰 ФОТ: <b>{report['total_payout']} руб</b>\n"
    if report['total_late_count'] > 0:
        lines += f"⚠️ Опозданий/невыходов: <b>{report['total_late_count']}</b>\n"
    lines += "\n<b>Состав:</b>\n"

    for w in report['workers']:
        rating_str = ""
        if w.get("rating"):
            rating_str = f" ⭐{w['rating'].get('avg', '—')}"
        late_str = f" ⚠️{w['late_count']}" if w['late_count'] > 0 else ""
        lines += f"• {w['name']} — {w['total_earned']} руб{rating_str}{late_str}\n"

    sent = 0
    for admin_id in ADMIN_IDS:
        try:
            await send_msg(admin_id, lines)
            sent += 1
        except Exception:
            pass

    return {"sent": sent, "report": report}


if __name__ == "__main__":
    run_service(app)
