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

USER_CHAT_ID = 1217715528

PAIRS = ["BTCUSDT","ETHUSDT","BNBUSDT","XRPUSDT","ADAUSDT","DOGEUSDT","SOLUSDT","MATICUSDT","DOTUSDT"]
TIMEFRAMES = {"15m":"15m","1h":"1h","4h":"4h","1d":"1d"}

user_settings = {"pair": None, "timeframe": None}

# ========= ФУНКЦИИ =========
def fetch_klines(symbol, interval, limit=200):
    try:
        klines = client.get_klines(symbol=symbol, interval=interval, limit=limit)
        df = pd.DataFrame(klines, columns=[
            "timestamp","open","high","low","close","volume",
            "close_time","quote_av","trades","tb_base_av","tb_quote_av","ignore"
        ])
        for col in ["close","open","high","low","volume"]:
            df[col] = df[col].astype(float)
        return df
    except Exception as e:
        print(f"Ошибка при получении данных: {e}")
        return None

def advanced_analysis(df):
    """Возвращает индикаторы и сигнал с фильтром ложных пробоев"""
    # Индикаторы
    df["ema20"] = ta.trend.EMAIndicator(df["close"], window=20).ema_indicator()
    df["ema50"] = ta.trend.EMAIndicator(df["close"], window=50).ema_indicator()
    df["ema200"] = ta.trend.EMAIndicator(df["close"], window=200).ema_indicator()
    df["rsi"] = ta.momentum.RSIIndicator(df["close"], window=14).rsi()
    macd = ta.trend.MACD(df["close"])
    df["macd"] = macd.macd()
    df["macd_signal"] = macd.macd_signal()
    df["atr"] = ta.volatility.AverageTrueRange(df["high"], df["low"], df["close"], window=14).average_true_range()
    df["vol_ma"] = df["volume"].rolling(20).mean()

    latest = df.iloc[-1]
    previous = df.iloc[-2]

    # Определяем тренд
    trend = "sideway"
    if latest["ema20"] > latest["ema50"] > latest["ema200"]:
        trend = "uptrend"
    elif latest["ema20"] < latest["ema50"] < latest["ema200"]:
        trend = "downtrend"

    # Фильтр обманного пробоя: цена не должна сильно отскакивать от EMA50/EMA200
    breakout_filter = abs(latest["close"] - latest["ema50"]) < 1.5 * latest["atr"] and abs(latest["close"] - latest["ema200"]) < 2.5 * latest["atr"]

    # Фильтр объема: текущий объем выше среднего за 20 свечей
    volume_filter = latest["volume"] > latest["vol_ma"]

    # Сигналы
    signal = "HOLD"
    if trend == "uptrend" and latest["rsi"] < 35 and latest["macd"] > latest["macd_signal"] and breakout_filter and volume_filter:
        signal = "BUY"
    elif trend == "downtrend" and latest["rsi"] > 65 and latest["macd"] < latest["macd_signal"] and breakout_filter and volume_filter:
        signal = "SELL"

    return {
        "price": latest["close"],
        "ema20": latest["ema20"],
        "ema50": latest["ema50"],
        "ema200": latest["ema200"],
        "rsi": latest["rsi"],
        "macd": latest["macd"],
        "macd_signal": latest["macd_signal"],
        "atr": latest["atr"],
        "signal": signal
    }

def check_all_pairs():
    """Проверяет все пары на качественные сигналы"""
    for pair in PAIRS:
        for tf in TIMEFRAMES.values():
            df = fetch_klines(pair, tf)
            if df is None or len(df) < 50:
                continue
            analysis = advanced_analysis(df)
            if analysis["signal"] != "HOLD":
                bot.send_message(USER_CHAT_ID,
                    f"📊 {pair} [{tf}]\n"
                    f"Цена: {analysis['price']:.4f}\n"
                    f"EMA20: {analysis['ema20']:.4f} | EMA50: {analysis['ema50']:.4f} | EMA200: {analysis['ema200']:.4f}\n"
                    f"RSI: {analysis['rsi']:.2f} | MACD: {analysis['macd']:.4f} | Signal: {analysis['macd_signal']:.4f}\n"
                    f"ATR: {analysis['atr']:.4f}\n"
                    f"➡️ Сигнал: {analysis['signal']}"
                )

def run_schedule():
    while True:
        schedule.run_pending()
        time.sleep(5)

# ========= TELEGRAM =========
@bot.message_handler(commands=["start"])
def start(message):
    if message.chat.id != USER_CHAT_ID:
        bot.send_message(message.chat.id, "⛔ Доступ запрещён")
        return
    bot.send_message(message.chat.id, "Мир вам дорогие друзья!👋 Выберите торговую пару:", reply_markup=pair_keyboard())

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
    keyboard.add("🔄 Сменить таймфрейм")
    return keyboard

@bot.message_handler(func=lambda message: message.text in PAIRS)
def set_pair(message):
    user_settings["pair"] = message.text
    bot.send_message(message.chat.id, f"Пара {message.text} выбрана ✅\nТеперь выбери таймфрейм:", reply_markup=timeframe_keyboard())

@bot.message_handler(func=lambda message: message.text in TIMEFRAMES.keys())
def set_timeframe(message):
    user_settings["timeframe"] = message.text
    bot.send_message(message.chat.id, f"Таймфрейм {message.text} выбран ✅")
    schedule.clear()
    if message.text == "15m":
        schedule.every(15).minutes.do(check_all_pairs)
    elif message.text == "1h":
        schedule.every().hour.do(check_all_pairs)
    elif message.text == "4h":
        schedule.every(4).hours.do(check_all_pairs)
    elif message.text == "1d":
        schedule.every().day.do(check_all_pairs)

@bot.message_handler(func=lambda message: message.text == "🔄 Сменить пару")
def change_pair(message):
    bot.send_message(message.chat.id, "Выбери новую пару:", reply_markup=pair_keyboard())

@bot.message_handler(func=lambda message: message.text == "🔄 Сменить таймфрейм")
def change_timeframe(message):
    bot.send_message(message.chat.id, "Выбери новый таймфрейм:", reply_markup=timeframe_keyboard())

# ========= ЗАПУСК =========
threading.Thread(target=run_schedule, daemon=True).start()
bot.polling(non_stop=True)
