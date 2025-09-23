# bot.py
import os
import time
import threading
import schedule
import logging

import pandas as pd
import numpy as np
import ta
from binance.client import Client
from binance.exceptions import BinanceAPIException
import telebot
from telebot import types

# ---------- ЛОГИ ----------
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# ---------- ПЕРЕМЕННЫЕ (из Railway / .env) ----------
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
BINANCE_API_KEY = os.getenv("BINANCE_API_KEY", "")
BINANCE_API_SECRET = os.getenv("BINANCE_API_SECRET", "")

if not TELEGRAM_TOKEN:
    raise RuntimeError("TELEGRAM_TOKEN env var is missing")

# ---------- Инициализация клиентов ----------
bot = telebot.TeleBot(TELEGRAM_TOKEN, threaded=True)
# Можно передать ключи или оставить пустыми (публичные данные)
client = Client(BINANCE_API_KEY, BINANCE_API_SECRET)

# ---------- Настройки (можешь менять) ----------
PAIRS = ["BTCUSDT","ETHUSDT","XRPUSDT","MATICUSDT","ADAUSDT","DOGEUSDT","SOLUSDT","TRXUSDT","SUIUSDT"]
TIMEFRAMES = ["15m","30m","1h","4h","1d"]

CHECK_INTERVAL_MINUTES = 15  # автопроверка каждые 15 минут
RESEND_COOLDOWN_SECONDS = 60 * 60  # не слать один и тот же сигнал чаще, чем 60 минут

# Пороговые значения для фильтра качества сигнала
RSI_BUY_THRESH = 30
RSI_SELL_THRESH = 70
VOL_MULTIPLIER = 1.2  # объём > средний * VOL_MULTIPLIER
TP_PCT = 0.03  # тейк-профит (3%)
SL_PCT = 0.015  # стоп-лосс (1.5%)

# ---------- Хранилище состояний (в памяти) ----------
# user_settings: chat_id -> {"pair": str or None, "tf": str, "monitor_all": bool}
user_settings = {}
# sent_cache: (chat_id, pair, tf, signal_type) -> last_sent_timestamp
sent_cache = {}

# ---------- Утилиты ----------
def get_klines(symbol: str, interval: str, limit: int = 200):
    """Получаем свечи (public) через python-binance. Возвращаем DataFrame с колонками time,o,h,l,c,v"""
    try:
        klines = client.get_klines(symbol=symbol, interval=interval, limit=limit)
        df = pd.DataFrame(klines, columns=["time","o","h","l","c","v","ct","qav","n","tbbav","tbqav","ignore"])
        df["c"] = df["c"].astype(float)
        df["o"] = df["o"].astype(float)
        df["h"] = df["h"].astype(float)
        df["l"] = df["l"].astype(float)
        df["v"] = df["v"].astype(float)
        return df
    except BinanceAPIException as e:
        logger.exception("Binance API exception")
        raise
    except Exception as e:
        logger.exception("get_klines error")
        raise

def analyze_df(df: pd.DataFrame):
    """Вычисляет индикаторы и возвращает словарь с последним значением + сигнал или None"""
    if df is None or df.empty or len(df) < 50:
        return {"error": "Not enough data"}

    # EMA
    df["ema_fast"] = df["c"].ewm(span=9, adjust=False).mean()
    df["ema_slow"] = df["c"].ewm(span=21, adjust=False).mean()
    df["ema200"] = df["c"].ewm(span=200, adjust=False).mean()

    # RSI
    delta = df["c"].diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    avg_gain = gain.rolling(14).mean()
    avg_loss = loss.rolling(14).mean()
    rs = avg_gain / avg_loss
    df["rsi"] = 100 - (100 / (1 + rs))

    # MACD
    ema12 = df["c"].ewm(span=12, adjust=False).mean()
    ema26 = df["c"].ewm(span=26, adjust=False).mean()
    df["macd"] = ema12 - ema26
    df["macd_signal"] = df["macd"].ewm(span=9, adjust=False).mean()

    # Volume
    avg_vol = df["v"].tail(50).mean() if len(df) >= 50 else df["v"].mean()
    last = df.iloc[-1]

    res = {
        "price": float(last["c"]),
        "ema_fast": float(last["ema_fast"]),
        "ema_slow": float(last["ema_slow"]),
        "ema200": float(last["ema200"]),
        "rsi": float(last["rsi"]) if not np.isnan(last["rsi"]) else None,
        "macd": float(last["macd"]),
        "macd_signal": float(last["macd_signal"]),
        "last_vol": float(last["v"]),
        "avg_vol": float(avg_vol)
    }

    # Сигнальная логика (жесткая: все условия подтверждают)
    signal = None
    # BUY условие
    if (res["ema_fast"] > res["ema_slow"]
        and (res["rsi"] is not None and res["rsi"] <= RSI_BUY_THRESH)
        and res["macd"] > res["macd_signal"]
        and res["last_vol"] > max(1e-9, res["avg_vol"] * VOL_MULTIPLIER)):
        signal = "BUY"
    # SELL условие
    elif (res["ema_fast"] < res["ema_slow"]
          and (res["rsi"] is not None and res["rsi"] >= RSI_SELL_THRESH)
          and res["macd"] < res["macd_signal"]
          and res["last_vol"] > max(1e-9, res["avg_vol"] * VOL_MULTIPLIER)):
        signal = "SELL"

    res["signal"] = signal
    # TP/SL
    if signal == "BUY":
        res["tp"] = res["price"] * (1 + TP_PCT)
        res["sl"] = res["price"] * (1 - SL_PCT)
    elif signal == "SELL":
        res["tp"] = res["price"] * (1 - TP_PCT)
        res["sl"] = res["price"] * (1 + SL_PCT)
    else:
        res["tp"] = None
        res["sl"] = None

    return res

def format_signal_message(pair, tf, info):
    if "error" in info:
        return f"⚠️ Ошибка: {info['error']}"
    price = info["price"]
    s = info["signal"]
    if not s:
        return None
    tp = info["tp"]
    sl = info["sl"]
    return (f"🔔 <b>{s} — {pair} ({tf})</b>\n"
            f"Цена: {price:.8f}\n"
            f"EMA9: {info['ema_fast']:.6f} | EMA21: {info['ema_slow']:.6f} | EMA200: {info['ema200']:.6f}\n"
            f"RSI: {info['rsi']:.2f} | MACD: {info['macd']:.6f}\n"
            f"Объём: {info['last_vol']:.2f} (avg {info['avg_vol']:.2f})\n\n"
            f"🎯 TP: {tp:.8f}\n"
            f"🛑 SL: {sl:.8f}")

def can_send(chat_id, pair, tf, signal):
    key = (chat_id, pair, tf, signal)
    now = time.time()
    last = sent_cache.get(key, 0)
    if now - last > RESEND_COOLDOWN_SECONDS:
        sent_cache[key] = now
        return True
    return False

# ---------- Клавиатуры ----------
def kb_pairs():
    markup = types.ReplyKeyboardMarkup(resize_keyboard=True, row_width=3)
    for p in PAIRS:
        markup.add(types.KeyboardButton(p))
    markup.add(types.KeyboardButton("📊 Проверить сигналы"), types.KeyboardButton("⏱ Сменить таймфрейм"))
    markup.add(types.KeyboardButton("🌐 Вкл. мониторинг всех пар"), types.KeyboardButton("🛑 Выкл. мониторинг всех пар"))
    return markup

def kb_timeframes():
    markup = types.ReplyKeyboardMarkup(resize_keyboard=True, row_width=3)
    for t in TIMEFRAMES:
        markup.add(types.KeyboardButton(t))
    return markup

def kb_after_signal():
    markup = types.ReplyKeyboardMarkup(resize_keyboard=True, row_width=2)
    markup.add(types.KeyboardButton("🔄 Обновить сигнал"), types.KeyboardButton("📌 Сменить пару"))
    markup.add(types.KeyboardButton("⏱ Сменить таймфрейм"))
    markup.add(types.KeyboardButton("🌐 Вкл. мониторинг всех пар"), types.KeyboardButton("🛑 Выкл. мониторинг всех пар"))
    return markup

# ---------- Хэндлеры ----------
@bot.message_handler(commands=['start'])
def handle_start(message):
    chat_id = message.chat.id
    # Инициализация настроек
    user_settings[chat_id] = {"pair": None, "tf": "1h", "monitor_all": False}
    bot.send_message(chat_id, "Ассаламу алейкум! Выберите торговую пару:", reply_markup=kb_pairs())

@bot.message_handler(func=lambda m: m.text == "📌 Сменить пару")
def handle_change_pair(message):
    bot.send_message(message.chat.id, "Выберите новую пару:", reply_markup=kb_pairs())

@bot.message_handler(func=lambda m: m.text == "⏱ Сменить таймфрейм")
def handle_change_tf(message):
    bot.send_message(message.chat.id, "Выберите таймфрейм:", reply_markup=kb_timeframes())

@bot.message_handler(func=lambda m: m.text == "🔄 Обновить сигнал")
def handle_refresh(message):
    chat_id = message.chat.id
    settings = user_settings.get(chat_id)
    if not settings or not settings.get("pair"):
        bot.send_message(chat_id, "Сначала выберите пару через /start или кнопку.", reply_markup=kb_pairs())
        return
    pair = settings["pair"]
    tf = settings["tf"]
    bot.send_message(chat_id, f"🔎 Обновляю сигнал для {pair} ({tf}) ...")
    try:
        df = get_klines(pair, tf, limit=200)
        info = analyze_df(df)
        text = format_signal_message(pair, tf, info)
        if text:
            bot.send_message(chat_id, text, parse_mode="HTML", reply_markup=kb_after_signal())
        else:
            bot.send_message(chat_id, f"⏸ Нет сигнала для {pair} ({tf}).", reply_markup=kb_after_signal())
    except Exception as e:
        bot.send_message(chat_id, f"⚠️ Ошибка при обновлении: {e}")

@bot.message_handler(func=lambda m: m.text == "📊 Проверить сигналы")
def handle_manual_check_all(message):
    chat_id = message.chat.id
    settings = user_settings.get(chat_id, {"tf":"1h"})
    tf = settings.get("tf", "1h")
    bot.send_message(chat_id, f"🔎 Ручная проверка всех пар на {tf} ...")
    for pair in PAIRS:
        try:
            df = get_klines(pair, tf, limit=200)
            info = analyze_df(df)
            text = format_signal_message(pair, tf, info)
            if text:
                bot.send_message(chat_id, text, parse_mode="HTML")
            time.sleep(0.5)  # пауза, чтобы не ударить лимиты
        except Exception as e:
            logger.exception("manual check error")
    bot.send_message(chat_id, "✅ Ручная проверка завершена.", reply_markup=kb_after_signal())

@bot.message_handler(func=lambda m: m.text == "🌐 Вкл. мониторинг всех пар")
def handle_enable_global(message):
    chat_id = message.chat.id
    settings = user_settings.setdefault(chat_id, {"pair": None, "tf": "1h", "monitor_all": False})
    settings["monitor_all"] = True
    bot.send_message(chat_id, "✅ Мониторинг всех пар включён. Бот будет присылать сигналы для любых пар.", reply_markup=kb_after_signal())

@bot.message_handler(func=lambda m: m.text == "🛑 Выкл. мониторинг всех пар")
def handle_disable_global(message):
    chat_id = message.chat.id
    settings = user_settings.setdefault(chat_id, {"pair": None, "tf": "1h", "monitor_all": False})
    settings["monitor_all"] = False
    bot.send_message(chat_id, "✅ Мониторинг всех пар выключен. Бот будет присылать сигналы только по выбранной паре.", reply_markup=kb_after_signal())

@bot.message_handler(func=lambda m: m.text in TIMEFRAMES)
def handle_set_tf(message):
    chat_id = message.chat.id
    tf = message.text
    settings = user_settings.setdefault(chat_id, {"pair": None, "tf": "1h", "monitor_all": False})
    settings["tf"] = tf
    bot.send_message(chat_id, f"✅ Таймфрейм установлен: {tf}", reply_markup=kb_after_signal())

@bot.message_handler(func=lambda m: m.text in PAIRS)
def handle_set_pair(message):
    chat_id = message.chat.id
    pair = message.text.strip().upper()
    settings = user_settings.setdefault(chat_id, {"pair": None, "tf": "1h", "monitor_all": False})
    settings["pair"] = pair
    bot.send_message(chat_id, f"✅ Пара установлена: {pair}\nТеперь выбери таймфрейм:", reply_markup=kb_timeframes())

@bot.message_handler(func=lambda m: True)
def handle_unknown(message):
    bot.send_message(message.chat.id, "Не понял команду. Нажмите /start, чтобы начать.", reply_markup=kb_pairs())

# ---------- Автопроверка (scheduler) ----------
def auto_check_for_user(chat_id, settings):
    """Проверяет все пары (если monitor_all) или только выбранную и отправляет сигналы."""
    tf = settings.get("tf", "1h")
    monitor_all = settings.get("monitor_all", False)
    pairs_to_check = PAIRS if monitor_all else ([settings.get("pair")] if settings.get("pair") else [])
    if not pairs_to_check:
        return

    for pair in pairs_to_check:
        if not pair:
            continue
        try:
            df = get_klines(pair, tf, limit=200)
            info = analyze_df(df)
            text = format_signal_message(pair, tf, info)
            if text and info.get("signal"):
                # проверка cooldown
                if can_send(chat_id, pair, tf, info["signal"]):
                    bot.send_message(chat_id, text, parse_mode="HTML")
            time.sleep(0.6)  # небольшая пауза между запросами
        except Exception as e:
            logger.exception("auto_check_for_user error")

def auto_check_all_users():
    logger.info("Scheduler: запускаю автопроверку для всех пользователей")
    for chat_id, settings in list(user_settings.items()):
        try:
            auto_check_for_user(chat_id, settings)
        except Exception:
            logger.exception("Ошибка в auto_check_all_users")

def run_scheduler():
    schedule.every(CHECK_INTERVAL_MINUTES).minutes.do(auto_check_all_users)
    # При старте можно сразу выполнить одну проверку (опционально)
    time.sleep(5)
    auto_check_all_users()
    while True:
        schedule.run_pending()
        time.sleep(1)

# ---------- Запуск ----------
if __name__ == "__main__":
    logger.info("Запуск бота и планировщика...")
    # старт планировщика в отдельном потоке
    t = threading.Thread(target=run_scheduler, daemon=True)
    t.start()
    # старт бота (пуллинг)
    bot.infinity_polling(timeout=60, long_polling_timeout=60)
