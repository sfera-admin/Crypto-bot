import telebot
from telebot import types
import os
import ccxt
import pandas as pd
import ta

# ========================
# 🔑 Переменные окружения
# ========================
BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
ADMIN_ID = os.getenv("TELEGRAM_ADMIN_ID")

bot = telebot.TeleBot(BOT_TOKEN)

# ========================
# ⚡️ Подключение к Binance
# ========================
binance = ccxt.binance()

# Доступные пары и таймфреймы
PAIRS = ["BTC/USDT", "ETH/USDT", "XRP/USDT", "MATIC/USDT", "ADA/USDT", "DOGE/USDT", "SOL/USDT", "TRX/USDT", "SUI/USDT"]
TIMEFRAMES = ["15m", "30m", "1h", "4h", "1d"]

# Память выбора пользователей
user_choice = {}

# ========================
# 📊 Функция анализа рынка
# ========================
def analyze_symbol(symbol, timeframe):
    try:
        ohlcv = binance.fetch_ohlcv(symbol, timeframe, limit=200)
        df = pd.DataFrame(ohlcv, columns=['time', 'open', 'high', 'low', 'close', 'volume'])

        # Индикаторы
        df['EMA20'] = ta.trend.EMAIndicator(df['close'], window=20).ema_indicator()
        df['EMA50'] = ta.trend.EMAIndicator(df['close'], window=50).ema_indicator()
        df['EMA200'] = ta.trend.EMAIndicator(df['close'], window=200).ema_indicator()
        df['RSI'] = ta.momentum.RSIIndicator(df['close'], window=14).rsi()
        df['MACD'] = ta.trend.MACD(df['close']).macd()

        last = df.iloc[-1]
        price = last['close']

        # Логика сигналов
        if last['EMA20'] > last['EMA50'] and last['RSI'] > 55 and last['MACD'] > 0:
            signal = "BUY ✅"
        elif last['EMA20'] < last['EMA50'] and last['RSI'] < 45 and last['MACD'] < 0:
            signal = "SELL ❌"
        else:
            signal = "HOLD ⏸"

        return f"""
📊 {symbol} ({timeframe})
Цена: {price:.2f}
➡️ Сигнал: {signal}

EMA20: {last['EMA20']:.2f} | EMA50: {last['EMA50']:.2f} | EMA200: {last['EMA200']:.2f}
RSI: {last['RSI']:.2f} | MACD: {last['MACD']:.2f}
Объём: {last['volume']:.2f}
"""
    except Exception as e:
        return f"⚠️ Ошибка анализа: {str(e)}"

# ========================
# 🟢 Стартовое меню
# ========================
@bot.message_handler(commands=['start'])
def start(message):
    markup = types.ReplyKeyboardMarkup(resize_keyboard=True, row_width=2)
    for pair in PAIRS:
        markup.add(types.KeyboardButton(pair))
    bot.send_message(message.chat.id, "👋 Привет! Выбери торговую пару:", reply_markup=markup)

# ========================
# 📌 Выбор пары
# ========================
@bot.message_handler(func=lambda message: message.text in PAIRS)
def choose_pair(message):
    user_choice[message.chat.id] = {"pair": message.text}
    markup = types.ReplyKeyboardMarkup(resize_keyboard=True, row_width=3)
    for tf in TIMEFRAMES:
        markup.add(types.KeyboardButton(tf))
    markup.add("📌 Сменить пару")
    bot.send_message(message.chat.id, f"✅ Пара {message.text} выбрана.\nТеперь выбери таймфрейм:", reply_markup=markup)

# ========================
# ⏱ Выбор таймфрейма
# ========================
@bot.message_handler(func=lambda message: message.text in TIMEFRAMES)
def choose_timeframe(message):
    chat_id = message.chat.id
    if chat_id not in user_choice or "pair" not in user_choice[chat_id]:
        bot.send_message(chat_id, "⚠️ Сначала выбери пару через /start")
        return

    user_choice[chat_id]["timeframe"] = message.text
    pair = user_choice[chat_id]["pair"]
    timeframe = message.text

    text = analyze_symbol(pair, timeframe)

    markup = types.ReplyKeyboardMarkup(resize_keyboard=True, row_width=2)
    markup.add("🔄 Обновить сигнал", "📌 Сменить пару")

    bot.send_message(chat_id, text, reply_markup=markup)

# ========================
# 🔄 Обновление сигнала
# ========================
@bot.message_handler(func=lambda message: message.text == "🔄 Обновить сигнал")
def refresh_signal(message):
    chat_id = message.chat.id
    if chat_id not in user_choice or "timeframe" not in user_choice[chat_id]:
        bot.send_message(chat_id, "⚠️ Сначала выбери пару и таймфрейм через /start")
        return

    pair = user_choice[chat_id]["pair"]
    timeframe = user_choice[chat_id]["timeframe"]

    text = analyze_symbol(pair, timeframe)

    markup = types.ReplyKeyboardMarkup(resize_keyboard=True, row_width=2)
    markup.add("🔄 Обновить сигнал", "📌 Сменить пару")

    bot.send_message(chat_id, text, reply_markup=markup)

# ========================
# 📌 Сменить пару
# ========================
@bot.message_handler(func=lambda message: message.text == "📌 Сменить пару")
def change_pair(message):
    markup = types.ReplyKeyboardMarkup(resize_keyboard=True, row_width=2)
    for pair in PAIRS:
        markup.add(types.KeyboardButton(pair))
    bot.send_message(message.chat.id, "🔄 Выбери новую торговую пару:", reply_markup=markup)

# ========================
# 🚀 Запуск
# ========================
if __name__ == "__main__":
    print("🤖 Bot started...")
    bot.infinity_polling()
