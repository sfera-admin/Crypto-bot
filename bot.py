import telebot
from binance.client import Client
import pandas as pd
import numpy as np
import ta
import os
import schedule
import time
import threading

# ========= НАСТРОЙКИ =========
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
bot = telebot.TeleBot(TELEGRAM_TOKEN)

# Binance клиент без ключей (только публичные данные)
client = Client()

# Твой chat_id
USER_CHAT_ID = 1217715528  

# Доступные пары и таймфреймы
PAIRS = ["BTCUSDT", "ETHUSDT", "BNBUSDT", "XRPUSDT", "ADAUSDT", "DOGEUSDT", "SOLUSDT", "DOTUSDT", "MATICUSDT"]
TIMEFRAMES = {"15m": "15m", "1h": "1h", "4h": "4h", "1d": "1d"}

# Сохраняем выбор пользователя
user_settings = {"pair": None, "timeframe": None}


# ========= ФУНКЦИИ =========
def fetch_klines(symbol, interval, limit=200):
    """Получаем свечи с Binance"""
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


def advanced_analysis(df):
    """Возвращает индикаторы и сигнал с фильтрацией шумов"""
    df["ema20"] = ta.trend.EMAIndicator(df["close"], window=20).ema_indicator()
    df["ema50"] = ta.trend.EMAIndicator(df["close"], window=50).ema_indicator()
    df["ema200"] = ta.trend.EMAIndicator(df["close"], window=200).ema_indicator()
    df["rsi"] = ta.momentum.RSIIndicator(df["close"], window=14).rsi()
    macd = ta.trend.MACD(df["close"])
    df["macd"] = macd.macd()
    df["signal"] = macd.macd_signal()

    latest = df.iloc[-1]
    price = latest["close"]

    text = (
        f"📊 {user_settings['pair']} ({user_settings['timeframe']})\n"
        f"Цена: {price:.4f}\n\n"
        f"EMA20: {latest['ema20']:.4f} | EMA50: {latest['ema50']:.4f} | EMA200: {latest['ema200']:.4f}\n"
        f"RSI: {latest['rsi']:.2f} | MACD: {latest['macd']:.4f} | Signal: {latest['signal']:.4f}\n"
    )

    # ===== ФИЛЬТРАЦИЯ =====
    if latest["rsi"] < 30 and latest["ema20"] > latest["ema50"] > latest["ema200"] and latest["macd"] > latest["signal"]:
        signal = "✅ BUY (сильный бычий сигнал)"
    elif latest["rsi"] > 70 and latest["ema20"] < latest["ema50"] < latest["ema200"] and latest["macd"] < latest["signal"]:
        signal = "❌ SELL (сильный медвежий сигнал)"
    else:
        signal = "⚖️ HOLD (сигнала нет)"

    text += f"\n➡️ Сигнал: {signal}"
    return signal, text


def check_signals():
    """Фоновая проверка сигналов"""
    pair = user_settings["pair"]
    timeframe = user_settings["timeframe"]

    if not pair or not timeframe:
        return

    df = fetch_klines(pair, timeframe)
    if df is not None:
        signal, text = advanced_analysis(df)
        if "BUY" in signal or "SELL" in signal:
            bot.send_message(USER_CHAT_ID, text)


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

    bot.send_message(message.chat.id, "👋 Мир вам дорогие друзья!\nВыберите торговую пару:", reply_markup=pair_keyboard())


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

    # Сразу отправляем анализ текущей ситуации
    df = fetch_klines(user_settings["pair"], user_settings["timeframe"])
    if df is not None:
        signal, text = advanced_analysis(df)
        bot.send_message(USER_CHAT_ID, text)
    else:
        bot.send_message(USER_CHAT_ID, "❌ Ошибка при загрузке данных")

    # Настраиваем расписание для сигналов
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
