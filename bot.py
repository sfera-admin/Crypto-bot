import telebot
import pandas as pd
import numpy as np
import ta
from binance.client import Client
import schedule
import time
import threading
import os

# ========= НАСТРОЙКИ =========
API_KEY = os.getenv("BINANCE_API_KEY")
API_SECRET = os.getenv("BINANCE_API_SECRET")
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")

bot = telebot.TeleBot(TELEGRAM_TOKEN)
client = Client(API_KEY, API_SECRET)

# Твой chat_id
USER_CHAT_ID = 1217715528  

# Доступные пары и таймфреймы
PAIRS = ["BTCUSDT", "ETHUSDT", "BNBUSDT", "XRPUSDT", "ADAUSDT", "DOGEUSDT", "SOLUSDT", "MATICUSDT", "DOTUSDT"]
TIMEFRAMES = {"15m": "15m", "1h": "1h", "4h": "4h", "1d": "1d"}

# Сохраняем выбор пользователя
user_settings = {"pair": None, "timeframe": None}


# ========= ФУНКЦИИ =========
def fetch_klines(symbol, interval, limit=100):
    try:
        klines = client.get_klines(symbol=symbol, interval=interval, limit=limit)
        df = pd.DataFrame(klines, columns=[
            "timestamp", "open", "high", "low", "close", "volume",
            "close_time", "quote_av", "trades", "tb_base_av", "tb_quote_av", "ignore"
        ])
        df["close"] = df["close"].astype(float)
        df["open"] = df["open"].astype(float)
        df["high"] = df["high"].astype(float)
        df["low"] = df["low"].astype(float)
        return df
    except Exception as e:
        print(f"Ошибка при получении данных: {e}")
        return None


def generate_signal(df):
    df["rsi"] = ta.momentum.RSIIndicator(df["close"], window=14).rsi()
    df["ema50"] = ta.trend.EMAIndicator(df["close"], window=50).ema_indicator()
    df["ema200"] = ta.trend.EMAIndicator(df["close"], window=200).ema_indicator()

    latest = df.iloc[-1]
    signal = None

    if latest["rsi"] < 30 and latest["ema50"] > latest["ema200"]:
        signal = "📈 Покупка (RSI перепродан, бычий тренд)"
    elif latest["rsi"] > 70 and latest["ema50"] < latest["ema200"]:
        signal = "📉 Продажа (RSI перекуплен, медвежий тренд)"

    return signal


def check_signals():
    pair = user_settings["pair"]
    timeframe = user_settings["timeframe"]

    if not pair or not timeframe:
        return

    df = fetch_klines(pair, timeframe)
    if df is not None:
        signal = generate_signal(df)
        if signal:
            bot.send_message(USER_CHAT_ID, f"Сигнал для {pair} [{timeframe}]:\n{signal}")


def run_schedule():
    while True:
        schedule.run_pending()
        time.sleep(5)


# ========= TELEGRAM ИНТЕРФЕЙС =========
@bot.message_handler(commands=["start"])
def start(message):
    if message.chat.id != USER_CHAT_ID:
        bot.send_message(message.chat.id, "⛔ Доступ запрещён")
        return

    bot.send_message(message.chat.id, "👋 Привет! Выбери торговую пару:", reply_markup=pair_keyboard())


def pair_keyboard():
    keyboard = telebot.types.ReplyKeyboardMarkup(resize_keyboard=True)
    for pair in PAIRS:
        keyboard.add(pair)
    return keyboard


def timeframe_keyboard():
    keyboard = telebot.types.ReplyKeyboardMarkup(resize_keyboard=True)
    for tf in TIMEFRAMES.keys():
        keyboard.add(tf)
    keyboard.add("🔄 Сменить пару")
    return keyboard


@bot.message_handler(func=lambda message: message.text in PAIRS)
def set_pair(message):
    if message.chat.id != USER_CHAT_ID:
        return

    user_settings["pair"] = message.text
    bot.send_message(message.chat.id, f"Пара {message.text} выбрана ✅\nТеперь выбери таймфрейм:", reply_markup=timeframe_keyboard())


@bot.message_handler(func=lambda message: message.text in TIMEFRAMES.keys())
def set_timeframe(message):
    if message.chat.id != USER_CHAT_ID:
        return

    user_settings["timeframe"] = message.text
    bot.send_message(message.chat.id, f"Таймфрейм {message.text} выбран ✅\nБуду присылать сигналы по {user_settings['pair']} [{user_settings['timeframe']}]")

    schedule.clear()
    if message.text == "15m":
        schedule.every(15).minutes.do(check_signals)
    elif message.text == "1h":
        schedule.every().hour.do(check_signals)
    elif message.text == "4h":
        schedule.every(4).hours.do(check_signals)
    elif message.text == "1d":
        schedule.every().day.do(check_signals)


@bot.message_handler(func=lambda message: message.text == "🔄 Сменить пару")
def change_pair(message):
    bot.send_message(message.chat.id, "Выбери новую пару:", reply_markup=pair_keyboard())


# ========= ЗАПУСК =========
threading.Thread(target=run_schedule, daemon=True).start()
bot.polling(non_stop=True)
