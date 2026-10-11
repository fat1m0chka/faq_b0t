import os
import re
import json
import random
import logging
from datetime import datetime, timedelta, timezone

import aiohttp
from dotenv import load_dotenv

from aiogram import Bot, Dispatcher, F, types
from aiogram.filters import CommandStart, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    CallbackQuery, FSInputFile, InlineKeyboardButton,
    InlineKeyboardMarkup, KeyboardButton, ReplyKeyboardMarkup, WebAppInfo
)
from sqlalchemy import select, delete

# Импортируем базу данных из нашего соседнего модуля
from database.models import AsyncSessionMaker, Participant, Contest

# Загружаем переменные из .env
load_dotenv()
BOT_TOKEN = os.getenv("BOT_TOKEN")

if not BOT_TOKEN:
    raise ValueError("❌ ОШИБКА: BOT_TOKEN не найден в файле .env!")

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher(storage=MemoryStorage())

# =====================================================================
# 1. КОНФИГУРАЦИЯ И СИСТЕМНЫЕ НАСТРОЙКИ
# =====================================================================
CHANNEL_ID = "@test_channellk" 
MY_TELEGRAM_ID = 5390254050
GREYDER_ADMIN_ID = 7508100064
KOMMUNIST_ADMIN_ID = 222222222

ALL_ADMIN_IDS = {MY_TELEGRAM_ID, GREYDER_ADMIN_ID, KOMMUNIST_ADMIN_ID}
BRANCH_RECIPIENTS = {
    "loc_greyder": {GREYDER_ADMIN_ID, MY_TELEGRAM_ID},
    "loc_kommunist": {KOMMUNIST_ADMIN_ID, MY_TELEGRAM_ID},
}

# ⚠️ СЮДА ВСТАВЬ ССЫЛКУ ОТ СВОЕГО ХОСТИНГА (добавь /map в конце)
WEBAPP_URL = "https://ТВОЙ_ДОМЕН_НА_BOTHOST.ru/map"

USERS_DB = set(ALL_ADMIN_IDS)
PENDING_ORDERS = {}
CONFIG_FILE = "bot_config.json"
SMARTSHELL_URL = "https://billing.smartshell.gg/api/graphql"

def is_admin(user: types.User) -> bool:
    return user.id in ALL_ADMIN_IDS

def load_config() -> dict:
    if not os.path.exists(CONFIG_FILE):
        defaults = {
            "smartshell": {
                "loc_greyder": {"company_id": 3489, "login": "79968370695", "password": "йфцыув321"},
                "loc_kommunist": {"company_id": 0, "login": "", "password": ""}
            },
            "promos": "🔥 <b>Акции и Бонусная программа сети:</b>\n\n🎁 <b>При регистрации:</b> 2 часа бесплатно!\n💳 <b>Кешбэк 10%:</b> при пополнении от 1000 рублей!",
            "clubs": {
                "loc_greyder": {
                    "name": "📍 Грейдерная, 1 (2 этаж)",
                    "phone": "+7 960 856 06 14",
                    "admin_tg": "@NextGenAst",
                    "price_photo": "price_greyder.jpg",
                    "info_caption": "🏢 <b>Филиал: ул. Грейдерная, 1</b>\n💻 VIP-зона: RTX 3060 Ti | 165Hz\n🔥 MVP-зона: RTX 3070 Ti | 240Hz",
                },
                "loc_kommunist": {
                    "name": "📍 Коммунистическая, 7",
                    "phone": "89064560613",
                    "admin_tg": "@genesisvrast",
                    "price_photo": "price_kommunist.jpg",
                    "info_caption": "🏢 <b>Филиал: ул. Коммунистическая, 7</b>\n🔹 Standard: RTX 5060 Ti | 180-240Hz\n👑 VIP: RTX 5060 Ti | 32 GB RAM | 280-360Hz",
                }
            }
        }
        with open(CONFIG_FILE, "w", encoding="utf-8") as f:
            json.dump(defaults, f, ensure_ascii=False, indent=4)
        return defaults
    with open(CONFIG_FILE, "r", encoding="utf-8") as f:
        return json.load(f)

def save_config(data: dict):
    with open(CONFIG_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=4)

APP_CONFIG = load_config()

async def is_subscribed(channel: str, user_id: int) -> bool:
    if not channel or channel.lower() == "нет": return True
    try:
        member = await bot.get_chat_member(chat_id=channel, user_id=user_id)
        return member.status in ["member", "administrator", "creator"]
    except Exception:
        return False

# =====================================================================
# 2. SMARTSHELL API
# =====================================================================
def normalize_alias(name: str) -> str:
    s = str(name).strip().upper().replace("№", "").replace(" ", "")
    if "PS5" in s: match = re.search(r"\d+", s); return f"PS5_{match.group()}" if match else "PS5"
    if "VR" in s: match = re.search(r"\d+", s); return f"VR_{match.group()}" if match else "VR"
    match = re.search(r"\d+", s)
    return match.group() if match else s

class SmartShellAPI:
    def __init__(self, loc_key: str):
        self.loc_key = loc_key
        self.token = None
        self.token_expire_time = None
        self.hosts_cache = {}

    async def ensure_auth(self, session: aiohttp.ClientSession) -> bool:
        creds = APP_CONFIG["smartshell"].get(self.loc_key, {})
        cid, login, password = creds.get("company_id"), creds.get("login"), creds.get("password")
        if not cid or not login or not password: return False
        if self.token and self.token_expire_time and datetime.now(timezone.utc) < self.token_expire_time: return True
        mutation = """mutation Login($input: LoginInput!) { login(input: $input) { access_token } }"""
        variables = {"input": {"login": str(login), "password": str(password), "company_id": int(cid)}}
        try:
            async with session.post(SMARTSHELL_URL, json={"query": mutation, "variables": variables}, headers={"Content-Type": "application/json", "User-Agent": f"company_id={cid}"}, timeout=6) as resp:
                data = await resp.json()
                if "errors" in data or not data.get("data", {}).get("login"): return False
                self.token = data["data"]["login"]["access_token"]
                self.token_expire_time = datetime.now(timezone.utc) + timedelta(hours=10)
                return True
        except Exception: return False

    async def get_hosts_status(self) -> dict:
        status_map = {}
        cid = APP_CONFIG["smartshell"].get(self.loc_key, {}).get("company_id")
        if not cid: return status_map
        query = """query GetHosts { hosts { id alias client_sessions { id status time_left } } }"""
        try:
            async with aiohttp.ClientSession() as session:
                if not await self.ensure_auth(session): return status_map
                async with session.post(SMARTSHELL_URL, json={"query": query}, headers={"Content-Type": "application/json", "User-Agent": f"company_id={cid}", "Authorization": f"Bearer {self.token}"}, timeout=5) as resp:
                    data = await resp.json()
                    hosts = data.get("data", {}).get("hosts", []) or []
                    for h in hosts:
                        alias_key = normalize_alias(h.get("alias", ""))
                        self.hosts_cache[alias_key] = h.get("id")
                        sessions = h.get("client_sessions", []) or []
                        active = [s for s in sessions if s.get("time_left") and s.get("time_left") > 0]
                        if active:
                            end_dt = datetime.now() + timedelta(seconds=active[0]["time_left"])
                            status_map[alias_key] = f"до ~{end_dt.strftime('%H:%M')}"
                        else: status_map[alias_key] = None
        except Exception: pass
        return status_map

    async def create_booking_api(self, places_list: list, client_name: str, client_phone: str, time_info: str) -> tuple[bool, str]:
        cid = APP_CONFIG["smartshell"].get(self.loc_key, {}).get("company_id")
        if not cid: return False, "SmartShell не привязан."
        host_ids = []
        for p in places_list:
            key = normalize_alias(p)
            hid = self.hosts_cache.get(key)
            if not hid:
                await self.get_hosts_status()
                hid = self.hosts_cache.get(key)
            if hid: host_ids.append(int(hid))

        if not host_ids: return False, "ПК не найдены."
        now = datetime.now()
        dt_from = datetime.combine(now.date() + timedelta(days=1), datetime.min.time()) + timedelta(minutes=5)
        dt_to = dt_from + timedelta(hours=2)
        mutation = """mutation CreateBooking($input: BookingInput!) { createBooking(input: $input) { id status } }"""
        async with aiohttp.ClientSession() as session:
            if not await self.ensure_auth(session): return False, "Ошибка авторизации."
            for attempt in range(2):
                variables = {"input": {"hosts": host_ids, "from": dt_from.strftime("%Y-%m-%dT%H:%M:%S"), "to": dt_to.strftime("%Y-%m-%dT%H:%M:%S"), "comment": f"ТГ: {client_name} | {client_phone} | {time_info}"}}
                try:
                    async with session.post(SMARTSHELL_URL, json={"query": mutation, "variables": variables}, headers={"Content-Type": "application/json", "User-Agent": f"company_id={cid}", "Authorization": f"Bearer {self.token}"}, timeout=8) as resp:
                        res = await resp.json()
                        if "errors" in res:
                            err_msg = res["errors"][0].get("message", "Ошибка API")
                            match = re.search(r"after\s+(\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2}:\d{2})", err_msg)
                            if match and attempt == 0:
                                dt_from = datetime.strptime(match.group(1), "%Y-%m-%d %H:%M:%S") + timedelta(minutes=5)
                                dt_to = dt_from + timedelta(hours=2)
                                continue
                            return False, f"❌ Отказ: {err_msg}"
                        return True, f"✅ Бронь создана: #{res['data']['createBooking']['id']}"
                except Exception as err: return False, f"❌ Исключение: {err}"
        return False, "Не удалось согласовать время."

CLIENTS = {"loc_greyder": SmartShellAPI("loc_greyder"), "loc_kommunist": SmartShellAPI("loc_kommunist")}

# =====================================================================
# 3. FSM СОСТОЯНИЯ
# =====================================================================
class BookingStates(StatesGroup): client_name, date_time, duration, phone = State(), State(), State(), State()
class TokenStates(StatesGroup): user, amount = State(), State()
class ProfileStates(StatesGroup): location, login, password = State(), State(), State()
class FeedbackStates(StatesGroup): loc, text = State(), State()
class ContestCreateStates(StatesGroup): title, description, channel, photo = State(), State(), State(), State()
class PromoEditStates(StatesGroup): text = State()

# =====================================================================
# 4. КЛАВИАТУРЫ
# =====================================================================
async def get_menu_kb(user: types.User) -> ReplyKeyboardMarkup:
    has_contest = False
    async with AsyncSessionMaker() as session:
        res = await session.execute(select(Contest).where(Contest.is_active == True))
        if res.scalar_one_or_none():
            has_contest = True
            
    kb = [
        # Интеграция WebApp для бронирования
        [KeyboardButton(text="🗺 Открыть карту зала", web_app=WebAppInfo(url=WEBAPP_URL))],
        [KeyboardButton(text="👤 Профиль")],
        [KeyboardButton(text="💻 Прайс"), KeyboardButton(text="🎁 Акции")],
        [KeyboardButton(text="🎮 Игры"), KeyboardButton(text="☎️ Контакты")]
    ]
    if has_contest: kb[1].append(KeyboardButton(text="🎉 Розыгрыш"))
    if is_admin(user): kb.append([KeyboardButton(text="🛡 Админ-панель")])
    return ReplyKeyboardMarkup(keyboard=kb, resize_keyboard=True)

def get_admin_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(keyboard=[
        [KeyboardButton(text="📝 Редактор Акций")],
        [KeyboardButton(text="🪙 Выдать токены"), KeyboardButton(text="👑 Управление розыгрышем")],
        [KeyboardButton(text="🔄 Вернуться в меню")]
    ], resize_keyboard=True)

def get_cancel_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(keyboard=[[KeyboardButton(text="❌ Отменить")]], resize_keyboard=True)

def get_location_select_kb(prefix: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text=APP_CONFIG["clubs"][k]["name"], callback_data=f"{prefix}_{k}")] for k in APP_CONFIG["clubs"]])

# =====================================================================
# 5. БАЗОВЫЕ КОМАНДЫ И МЕНЮ
# =====================================================================
@dp.message(CommandStart(), StateFilter("*"))
@dp.message(F.text == "🔄 Вернуться в меню", StateFilter("*"))
@dp.message(F.text == "❌ Отменить", StateFilter("*"))
async def cmd_start_or_back(message: types.Message, state: FSMContext):
    await state.clear()
    USERS_DB.add(message.from_user.id)
    await message.answer("👋 <b>Добро пожаловать в сеть клубов NextGen!</b>\nВыберите раздел меню:", reply_markup=await get_menu_kb(message.from_user), parse_mode="HTML")

@dp.message(F.text == "🛡 Админ-панель", StateFilter("*"))
async def open_admin_panel(message: types.Message):
    if not is_admin(message.from_user): return
    await message.answer("🛡 <b>Панель администратора</b>", reply_markup=get_admin_keyboard(), parse_mode="HTML")

@dp.callback_query(F.data == "cancel_action", StateFilter("*"))
async def cancel_action_cb(callback: CallbackQuery, state: FSMContext):
    await state.clear()
    await callback.message.delete()
    await callback.message.answer("Действие отменено.", reply_markup=await get_menu_kb(callback.from_user))
    await callback.answer()

@dp.message(F.text == "💻 Прайс", StateFilter("*"))
async def choose_location_info(message: types.Message, state: FSMContext):
    await state.clear()
    await message.answer("Выберите филиал:", reply_markup=get_location_select_kb("info"))

@dp.callback_query(F.data.startswith("info_"), StateFilter("*"))
async def send_club_info(callback: CallbackQuery):
    club_key = callback.data.replace("info_", "")
    club = APP_CONFIG["clubs"].get(club_key)
    if not club: return
    await callback.message.delete()
    try: await callback.message.answer_photo(photo=FSInputFile(club["price_photo"]), caption=club["info_caption"], parse_mode="HTML")
    except Exception: await callback.message.answer(club["info_caption"], parse_mode="HTML")
    await callback.answer()

@dp.message(F.text == "☎️ Контакты", StateFilter("*"))
async def send_contacts(message: types.Message, state: FSMContext):
    await state.clear()
    text = "🏢 <b>Наши филиалы:</b>\n\n"
    for _, club in APP_CONFIG["clubs"].items():
        text += f"<b>{club['name']}</b>\n🕒 Режим: 24/7\n📞 Телефон: <code>{club['phone']}</code>\n💬 TG админа: {club['admin_tg']}\n\n"
    await message.answer(text, parse_mode="HTML")

@dp.message(F.text == "🎁 Акции", StateFilter("*"))
async def send_bonuses(message: types.Message, state: FSMContext):
    await state.clear()
    await message.answer(APP_CONFIG["promos"], parse_mode="HTML")

@dp.message(F.text == "🎮 Игры", StateFilter("*"))
async def send_games(message: types.Message, state: FSMContext):
    await state.clear()
    kb = ReplyKeyboardMarkup(keyboard=[[KeyboardButton(text="💡 Предложить игру")], [KeyboardButton(text="🔄 Вернуться в меню")]], resize_keyboard=True)
    await message.answer("💬 <b>Популярные игры:</b>\n• CS2 / Dota 2 / Fortnite\n• Apex / PUBG / Rust / EFT", reply_markup=kb, parse_mode="HTML")

@dp.message(F.text == "💡 Предложить игру", StateFilter("*"))
async def start_game_request(message: types.Message, state: FSMContext):
    await state.set_state(FeedbackStates.loc)
    await message.answer("Выберите филиал:", reply_markup=get_location_select_kb("fbloc"))

@dp.callback_query(F.data.startswith("fbloc_"), FeedbackStates.loc)
async def process_feedback_loc(callback: CallbackQuery, state: FSMContext):
    loc_key = callback.data.replace("fbloc_", "")
    await state.update_data(loc_key=loc_key, location=APP_CONFIG["clubs"][loc_key]["name"])
    await state.set_state(FeedbackStates.text)
    await callback.message.delete()
    await callback.message.answer(f"Клуб: <b>{APP_CONFIG['clubs'][loc_key]['name']}</b>\nНапишите название игры:", reply_markup=get_cancel_keyboard(), parse_mode="HTML")
    await callback.answer()

@dp.message(FeedbackStates.text)
async def process_feedback(message: types.Message, state: FSMContext):
    data = await state.get_data()
    loc_key = data.get("loc_key", "loc_greyder")
    await state.clear()
    card = f"🎮 <b>ЗАПРОС НА ИГРУ</b>\n🏢 <b>Филиал:</b> {data.get('location', '')}\n👤 <b>От:</b> {message.from_user.full_name}\n📝 <b>Игра:</b> <i>{message.text}</i>"
    for adm in BRANCH_RECIPIENTS.get(loc_key, {MY_TELEGRAM_ID}):
        try: await bot.send_message(chat_id=adm, text=card, parse_mode="HTML")
        except Exception: pass
    await message.answer("Спасибо! Передали заявку админам 🙌", reply_markup=await get_menu_kb(message.from_user))


# =====================================================================
# 6. БРОНИРОВАНИЕ ЧЕРЕЗ WEB APP КАРТУ
# =====================================================================
@dp.message(F.web_app_data)
async def web_app_data_handler(message: types.Message, state: FSMContext):
    """Ловит данные от нажатия кнопки в WebApp карте"""
    try:
        data = json.loads(message.web_app_data.data)
        if data.get("action") == "book_pcs":
            pcs = data.get("pcs", [])
            loc_key = data.get("location")
            location_name = APP_CONFIG["clubs"].get(loc_key, {}).get("name", loc_key)
            
            await state.update_data(loc_key=loc_key, location=location_name, raw_places=pcs)
            await state.set_state(BookingStates.client_name)
            
            places_str = ", ".join([f"ПК №{p}" if "PS5" not in p and "VR" not in p else p for p in pcs])
            await state.update_data(pc_number=places_str)
            
            await message.answer(
                f"✅ Вы выбрали: <b>{places_str}</b>\n\n👤 <b>На какое имя оформить бронь?</b>", 
                reply_markup=get_cancel_keyboard(), 
                parse_mode="HTML"
            )
    except Exception as e:
        logging.error(f"WebApp Data Error: {e}")

@dp.message(BookingStates.client_name)
async def booking_client_name(message: types.Message, state: FSMContext):
    await state.update_data(client_name=message.text.strip())
    await state.set_state(BookingStates.date_time)
    await message.answer("🕒 <b>К какому времени подтянетесь?</b> (например: <i>К 20:30</i>):", parse_mode="HTML")

@dp.message(BookingStates.date_time)
async def booking_datetime(message: types.Message, state: FSMContext):
    await state.update_data(date_time=message.text.strip())
    await state.set_state(BookingStates.duration)
    await message.answer("⏳ <b>На сколько часов садитесь?</b> (например: <i>3 часа / Пакет Ночь</i>):", parse_mode="HTML")

@dp.message(BookingStates.duration)
async def booking_duration(message: types.Message, state: FSMContext):
    await state.update_data(duration=message.text.strip())
    await state.set_state(BookingStates.phone)
    await message.answer("📞 <b>Оставьте номер телефона для связи:</b>", parse_mode="HTML")

@dp.message(BookingStates.phone)
async def booking_phone(message: types.Message, state: FSMContext):
    await state.update_data(phone=message.text.strip())
    data = await state.get_data()
    loc_key = data["loc_key"]
    await state.clear()
    user = message.from_user

    PENDING_ORDERS[user.id] = {
        "loc_key": loc_key, 
        "places": data.get("raw_places", []), 
        "client_name": data["client_name"], 
        "phone": data["phone"], 
        "duration": data["duration"], 
        "time": data["date_time"]
    }

    await message.answer("🚀 <b>Заявка улетела админу!</b> Сейчас подтвердим и заброним место.", reply_markup=await get_menu_kb(user), parse_mode="HTML")

    admin_kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="✅ Принять в SmartShell", callback_data=f"adm_ok_{user.id}"), InlineKeyboardButton(text="❌ Отклонить", callback_data=f"adm_no_{user.id}")]])
    admin_card = (f"🚨 <b>НОВАЯ ЗАЯВКА ЧЕРЕЗ КАРТУ</b>\n🏢 {data['location']}\n🎯 Выбрано: <b>{data['pc_number']}</b>\n👤 <b>{data['client_name']}</b> | 📞 <code>{data['phone']}</code>\n🕒 {data['date_time']} | ⏳ {data['duration']}")

    for adm in BRANCH_RECIPIENTS.get(loc_key, {MY_TELEGRAM_ID}):
        try: await bot.send_message(chat_id=adm, text=admin_card, reply_markup=admin_kb, parse_mode="HTML")
        except Exception: pass

@dp.callback_query(F.data.startswith("adm_ok_"), StateFilter("*"))
async def admin_approve(callback: CallbackQuery):
    if not is_admin(callback.from_user): return
    client_id = int(callback.data.split("_")[2])
    order = PENDING_ORDERS.get(client_id)
    ss_status_text = ""

    if order:
        client = CLIENTS.get(order["loc_key"])
        if client:
            ok, log_res = await client.create_booking_api(order["places"], order["client_name"], order["phone"], f"{order['time']} ({order['duration']})")
            ss_status_text = f"\n\n<b>SmartShell:</b>\n{log_res}"

    await callback.message.edit_text(f"{callback.message.text}\n\n🟢 <b>ПРИНЯТА ({callback.from_user.first_name})</b>{ss_status_text}", parse_mode="HTML")
    try: await bot.send_message(chat_id=client_id, text="🎉 <b>Ваша бронь подтверждена!</b> Ждем в клубе!", parse_mode="HTML")
    except Exception: pass

@dp.callback_query(F.data.startswith("adm_no_"), StateFilter("*"))
async def admin_reject(callback: CallbackQuery):
    if not is_admin(callback.from_user): return
    client_id = int(callback.data.split("_")[2])
    PENDING_ORDERS.pop(client_id, None)
    await callback.message.edit_text(f"{callback.message.text}\n\n🔴 <b>ОТКЛОНЕНА ({callback.from_user.first_name})</b>", parse_mode="HTML")
    try: await bot.send_message(chat_id=client_id, text="😔 К сожалению, на выбранное время все места заняты.", parse_mode="HTML")
    except Exception: pass


# =====================================================================
# 7. ПРОФИЛЬ И РОЗЫГРЫШИ (Логика из предыдущей версии)
# =====================================================================
async def fetch_smartshell_profile(login: str, password: str, cid: int) -> dict | None:
    headers = {"Content-Type": "application/json", "User-Agent": f"company_id={cid}"}
    auth_mutation = """mutation Login($input: LoginInput!) { login(input: $input) { access_token } }"""
    me_query = """query { me { nickname phone deposit bonus group_discount } }"""
    async with aiohttp.ClientSession() as session:
        try:
            resp = await session.post(SMARTSHELL_URL, json={"query": auth_mutation, "variables": {"input": {"login": login, "password": password, "company_id": cid}}}, headers=headers, timeout=5)
            data = await resp.json()
            if "errors" in data or not data.get("data", {}).get("login"): return None
            token = data["data"]["login"]["access_token"]
            headers["Authorization"] = f"Bearer {token}"
            resp2 = await session.post(SMARTSHELL_URL, json={"query": me_query}, headers=headers, timeout=5)
            prof_data = await resp2.json()
            return prof_data.get("data", {}).get("me")
        except Exception: return None

@dp.message(F.text == "👤 Профиль", StateFilter("*"))
async def show_profile(message: types.Message, state: FSMContext):
    await state.clear()
    user_id = message.from_user.id
    async with AsyncSessionMaker() as session:
        result = await session.execute(select(Participant).where(Participant.user_id == user_id))
        user = result.scalar_one_or_none()
        
    if not user or not user.ss_login or not user.ss_password or not user.ss_cid:
        await state.set_state(ProfileStates.location)
        await message.answer("💳 <b>Поиск клубной карты</b>\n\nПодскажите, в каком филиале нашей сети вы чаще всего бываете? 🏠", reply_markup=get_location_select_kb("profloc"), parse_mode="HTML")
        return

    msg = await message.answer("⏳ <i>Подключаемся к серверу клуба...</i>", parse_mode="HTML")
    profile = await fetch_smartshell_profile(user.ss_login, user.ss_password, user.ss_cid)
    
    if not profile:
        await msg.edit_text("❌ Ошибка авторизации. Возможно, пароль был изменен.")
        await msg.edit_reply_markup(reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🔄 Привязать заново", callback_data="relink_profile")]]))
        return

    nickname = profile.get('nickname') or 'Гость'
    deposit = profile.get('deposit')
    bonus = profile.get('bonus')
    discount = profile.get('group_discount')
    
    deposit = deposit if deposit is not None else 0
    bonus = bonus if bonus is not None else 0
    discount = discount if discount is not None else 0
    
    card = (f"👤 <b>Профиль:</b> {nickname}\n📞 <b>Телефон:</b> <code>{profile.get('phone', '')}</code>\n\n💰 <b>Баланс:</b> {deposit} ₽\n"
            f"🎁 <b>Бонусы:</b> {bonus} баллов\n📉 <b>Скидка аккаунта:</b> {discount}%\n\n💎 <b>Токены розыгрыша:</b> {user.tokens} шт.")
    await msg.edit_text(card, reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="❌ Отвязать аккаунт", callback_data="unlink_profile")]]), parse_mode="HTML")

@dp.callback_query(F.data.startswith("profloc_"), ProfileStates.location)
async def profile_location_chosen(callback: CallbackQuery, state: FSMContext):
    loc_key = callback.data.replace("profloc_", "")
    cid = SMARTSHELL_DATA.get(loc_key, {}).get("company_id")
    if not cid: return await callback.answer("⚠️ Этот филиал еще не настроен администратором.", show_alert=True)
    await state.update_data(ss_cid=cid)
    await state.set_state(ProfileStates.login)
    await callback.message.delete()
    await callback.message.answer("Отлично! 🤝\nТеперь введите ваш <b>номер телефона</b> (логин от ПК):", reply_markup=get_cancel_keyboard(), parse_mode="HTML")

@dp.message(ProfileStates.login)
async def process_profile_login(message: types.Message, state: FSMContext):
    await state.update_data(ss_login=re.sub(r"\D", "", message.text))
    await state.set_state(ProfileStates.password)
    await message.answer("🔑 И последнее — введите ваш <b>пароль</b> от ПК:", parse_mode="HTML")

@dp.message(ProfileStates.password)
async def process_profile_password(message: types.Message, state: FSMContext):
    password = message.text.strip()
    data = await state.get_data()
    login, cid = data["ss_login"], data["ss_cid"]
    msg = await message.answer("⏳ <i>Проверяем данные...</i>", parse_mode="HTML")
    profile = await fetch_smartshell_profile(login, password, cid)
    
    if not profile: return await msg.edit_text("❌ Неверный логин или пароль. Попробуйте снова или нажмите «Отменить».")
        
    user_id = message.from_user.id
    async with AsyncSessionMaker() as db_session:
        result = await db_session.execute(select(Participant).where(Participant.user_id == user_id))
        user = result.scalar_one_or_none()
        if not user:
            user = Participant(user_id=user_id, username=message.from_user.username or "", full_name=message.from_user.full_name, phone=login, tokens=0)
            db_session.add(user)
        user.ss_login, user.ss_password, user.ss_cid, user.phone = login, password, cid, login
        await db_session.commit()
        
    await state.clear()
    try: await message.delete()
    except Exception: pass
    await msg.edit_text("✅ <b>Аккаунт успешно привязан!</b> Нажмите «👤 Профиль» в меню.", parse_mode="HTML")
    await msg.answer("Меню:", reply_markup=await get_menu_kb(message.from_user))

@dp.callback_query(F.data.in_(["unlink_profile", "relink_profile"]), StateFilter("*"))
async def unlink_profile_cb(callback: CallbackQuery, state: FSMContext):
    user_id = callback.from_user.id
    async with AsyncSessionMaker() as db_session:
        result = await db_session.execute(select(Participant).where(Participant.user_id == user_id))
        user = result.scalar_one_or_none()
        if user:
            user.ss_login, user.ss_password, user.ss_cid = None, None, None
            await db_session.commit()
    await callback.message.edit_text("🔗 Аккаунт отвязан. Ваши данные для входа удалены из бота.")
    if callback.data == "relink_profile":
        await state.set_state(ProfileStates.location)
        await callback.message.answer("💳 <b>Поиск клубной карты</b>\nПодскажите, в каком филиале нашей сети вы чаще всего бываете?", reply_markup=get_location_select_kb("profloc"), parse_mode="HTML")
    await callback.answer()

@dp.message(F.text == "🎉 Розыгрыш", StateFilter("*"))
async def join_giveaway(message: types.Message, state: FSMContext):
    user_id = message.from_user.id
    async with AsyncSessionMaker() as session:
        res = await session.execute(select(Contest).where(Contest.is_active == True))
        contest = res.scalar_one_or_none()
        if not contest:
            await message.answer("❌ В данный момент нет активных розыгрышей.")
            return
            
        part_res = await session.execute(select(Participant).where(Participant.user_id == user_id))
        user = part_res.scalar_one_or_none()
        
        if not user or not user.ss_login:
            await message.answer(
                "⚠️ <b>Защита от абуза!</b>\n\n"
                "Участвовать в розыгрышах могут только реальные клиенты клуба.\n"
                "Пожалуйста, сначала привяжите свой аккаунт: нажмите кнопку <b>«👤 Профиль»</b>.",
                parse_mode="HTML"
            )
            return

        if contest.channel_id and not await is_subscribed(contest.channel_id, user_id):
            clean_channel = contest.channel_id.replace("@", "")
            kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="📢 Подписаться", url=f"https://t.me/{clean_channel}")], [InlineKeyboardButton(text="🔄 Я подписался", callback_data="check_sub")]])
            await message.answer(f"⚠️ <b>Условие участия!</b>\nДля участия необходимо подписаться на канал.", reply_markup=kb, parse_mode="HTML")
            return

        text = (
            f"🎁 <b>{contest.title}</b>\n\n"
            f"{contest.description}\n\n"
            f"😎 <b>Ты в игре! Аккаунт подтвержден.</b>\n"
            f"💎 Твои токены (билеты): <b>{user.tokens} шт.</b>\n"
            "<i>(1 токен = 1000₽ пополнения. Проси админа начислить при оплате)</i>"
        )
        if contest.photo_id: await message.answer_photo(photo=contest.photo_id, caption=text, parse_mode="HTML")
        else: await message.answer(text, parse_mode="HTML")

@dp.callback_query(F.data == "check_sub", StateFilter("*"))
async def check_sub_cb(callback: CallbackQuery):
    user_id = callback.from_user.id
    async with AsyncSessionMaker() as session:
        res = await session.execute(select(Contest).where(Contest.is_active == True))
        contest = res.scalar_one_or_none()
    
    if contest and await is_subscribed(contest.channel_id, user_id):
        await callback.message.delete()
        await callback.message.answer("✅ Подписка подтверждена. Нажми «🎉 Розыгрыш» еще раз.")
    else:
        await callback.answer("❌ Вы еще не подписались!", show_alert=True)

# =====================================================================
# 8. АДМИНКА (Акции, Выдача Токенов, Управление Конкурсом)
# =====================================================================
@dp.message(F.text == "📝 Редактор Акций", StateFilter("*"))
async def edit_promos(message: types.Message, state: FSMContext):
    if not is_admin(message.from_user): return
    await state.set_state(PromoEditStates.text)
    await message.answer(f"Текущий текст акций:\n\n{APP_CONFIG['promos']}\n\nОтправьте новый текст (поддерживается HTML-разметка):", reply_markup=get_cancel_keyboard(), parse_mode="HTML")

@dp.message(PromoEditStates.text)
async def save_promos(message: types.Message, state: FSMContext):
    APP_CONFIG["promos"] = message.text
    save_config(APP_CONFIG)
    await state.clear()
    await message.answer("✅ Акции успешно обновлены!", reply_markup=get_admin_keyboard())

@dp.message(F.text == "🪙 Выдать токены", StateFilter("*"))
async def start_tokens(message: types.Message, state: FSMContext):
    if not is_admin(message.from_user): return
    await state.set_state(TokenStates.user)
    await message.answer("Введите номер телефона пользователя:", reply_markup=get_cancel_keyboard())

@dp.message(TokenStates.user)
async def get_user_for_tokens(message: types.Message, state: FSMContext):
    phone = re.sub(r"\D", "", message.text)
    async with AsyncSessionMaker() as session:
        result = await session.execute(select(Participant).where(Participant.phone.like(f"%{phone}%")))
        user = result.scalars().first()

    if not user:
        return await message.answer("❌ Пользователь не найден. Попробуйте снова:")

    await state.update_data(db_user_id=user.id)
    await state.set_state(TokenStates.amount)
    await message.answer(f"👤 {user.full_name} | 📱 {user.phone}\n💰 Введите <b>сумму пополнения</b> (1 токен = 1000₽):", parse_mode="HTML")

@dp.message(TokenStates.amount)
async def get_token_amount(message: types.Message, state: FSMContext):
    if not message.text.isdigit(): return await message.answer("Введите только число.")
    amount = int(message.text)
    tokens_to_add = amount // 1000
    data = await state.get_data()

    if tokens_to_add <= 0:
        await state.clear()
        return await message.answer(f"⚠️ Сумма {amount}₽ мала. Токены от 1000₽.", reply_markup=get_admin_keyboard())

    async with AsyncSessionMaker() as session:
        user = await session.get(Participant, data["db_user_id"])
        if user:
            user.tokens += tokens_to_add
            total_tokens, phone, full_name, user_tg_id = user.tokens, user.phone, user.full_name, user.user_id
            await session.commit()

    await message.answer(f"✅ <b>Начислено {tokens_to_add} токенов</b>\n👤 {full_name}\n💎 Всего: {total_tokens}", reply_markup=get_admin_keyboard(), parse_mode="HTML")
    try: await bot.send_message(chat_id=user_tg_id, text=f"🎰 Вам начислено <b>{tokens_to_add}</b> токенов!\n💎 Всего билетов: <b>{total_tokens}</b>", parse_mode="HTML")
    except Exception: pass
    await state.clear()

@dp.callback_query(F.data == "create_contest", StateFilter("*"))
async def create_contest_start(callback: CallbackQuery, state: FSMContext):
    if not is_admin(callback.from_user): return
    await state.set_state(ContestCreateStates.title)
    await callback.message.answer("📝 Введите <b>название</b> розыгрыша:", reply_markup=get_cancel_keyboard(), parse_mode="HTML")
    await callback.answer()

@dp.message(ContestCreateStates.title)
async def create_contest_title(message: types.Message, state: FSMContext):
    await state.update_data(title=message.text)
    await state.set_state(ContestCreateStates.description)
    await message.answer("📝 Введите <b>описание и условия</b> розыгрыша:", parse_mode="HTML")

@dp.message(ContestCreateStates.description)
async def create_contest_desc(message: types.Message, state: FSMContext):
    await state.update_data(description=message.text)
    await state.set_state(ContestCreateStates.channel)
    await message.answer("📢 Введите <b>ID или username канала</b> для проверки подписки.\nЕсли проверка не нужна, напишите <b>Нет</b>:", parse_mode="HTML")

@dp.message(ContestCreateStates.channel)
async def create_contest_channel(message: types.Message, state: FSMContext):
    channel = None if message.text.strip().lower() == "нет" else message.text.strip()
    await state.update_data(channel=channel)
    await state.set_state(ContestCreateStates.photo)
    await message.answer("📸 Отправьте <b>фото</b> для розыгрыша (или напишите <b>Нет</b>):", parse_mode="HTML")

@dp.message(ContestCreateStates.photo)
async def create_contest_photo(message: types.Message, state: FSMContext):
    data = await state.get_data()
    photo_id = message.photo[-1].file_id if message.photo else None
    async with AsyncSessionMaker() as session:
        await session.execute(Participant.__table__.update().values(tokens=0))
        new_contest = Contest(title=data["title"], description=data["description"], channel_id=data.get("channel"), photo_id=photo_id, is_active=True)
        session.add(new_contest)
        await session.commit()
    await state.clear()
    await message.answer("✅ <b>Розыгрыш запущен!</b> Кнопка появилась у клиентов.", reply_markup=await get_menu_kb(message.from_user), parse_mode="HTML")

@dp.message(F.text == "👑 Управление розыгрышем", StateFilter("*"))
async def giveaway_admin_panel(message: types.Message):
    if not is_admin(message.from_user): return
    async with AsyncSessionMaker() as session:
        result = await session.execute(select(Contest).where(Contest.is_active == True))
        active_contest = result.scalar_one_or_none()
        parts_res = await session.execute(select(Participant).where(Participant.ss_login.isnot(None)))
        participants = parts_res.scalars().all()
        count = len(participants)
        total_tokens = sum(p.tokens for p in participants)

    if active_contest:
        kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🎲 Выбрать победителя", callback_data="roll_winner")], [InlineKeyboardButton(text="🛑 Завершить конкурс", callback_data="end_contest")]])
        text = f"🎁 <b>Активный розыгрыш:</b> {active_contest.title}\n👥 Привязали профиль: <b>{count} чел.</b>\n💎 Выпущено токенов: <b>{total_tokens}</b>"
    else:
        kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="✨ Создать новый розыгрыш", callback_data="create_contest")]])
        text = "🎁 <b>Нет активных розыгрышей.</b> Кнопка скрыта от клиентов."
    await message.answer(text, reply_markup=kb, parse_mode="HTML")

@dp.callback_query(F.data == "end_contest", StateFilter("*"))
async def end_contest_cb(callback: CallbackQuery):
    if not is_admin(callback.from_user): return
    async with AsyncSessionMaker() as session:
        await session.execute(delete(Contest))
        await session.execute(Participant.__table__.update().values(tokens=0))
        await session.commit()
    await callback.message.edit_text("🛑 <b>Конкурс завершен.</b> Токены обнулены, кнопка скрыта.", parse_mode="HTML")
    await callback.message.answer("Меню обновлено.", reply_markup=await get_menu_kb(callback.from_user))
    await callback.answer()

@dp.callback_query(F.data == "roll_winner", StateFilter("*"))
async def roll_winner_cb(callback: CallbackQuery):
    if not is_admin(callback.from_user): return
    async with AsyncSessionMaker() as session:
        result = await session.execute(select(Participant).where(Participant.tokens > 0))
        participants = result.scalars().all()

    if not participants:
        await callback.answer("❌ Нет участников с токенами!", show_alert=True)
        return
    winner = random.choices(participants, weights=[p.tokens for p in participants], k=1)[0]
    user_tg = f"@{winner.username}" if winner.username else "нет никнейма"
    text = (f"🎉 <b>ПОБЕДИТЕЛЬ ОПРЕДЕЛЕН!</b> 🎉\n\n👤 Имя: <b>{winner.full_name}</b>\n📞 Тел: <code>{winner.phone}</code>\n💬 TG: {user_tg}\n💎 Шанс был по токенам: <b>{winner.tokens}</b> шт.")
    await callback.message.edit_text(text, parse_mode="HTML")