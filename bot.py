# bot.py
import os
import logging
import asyncio
import pandas as pd
import numpy as np
import ccxt
import ta

from telegram import ReplyKeyboardMarkup, ReplyKeyboardRemove, Update
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    ContextTypes,
    filters,
)

# ---------- ЛОГИ ----------
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# ---------- ПЕРЕМЕННЫЕ ОКРУЖЕНИЯ ----------
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")  # обязателен
# BINANCE ключи не обязательны, ccxt может брать публичные данные
BINANCE_API_KEY = os.getenv("BINANCE_API_KEY", None)
BINANCE_API_SECRET = os.getenv("BINANCE_API_SECRET", None)

if not TELEGRAM_TOKEN:
    raise RuntimeError("TELEGRAM_TOKEN env var is missing")

# ---------- ИНИЦИАЛИЗАЦИЯ BINANCE (ccxt) ----------
binance = ccxt.binance({
    "enableRateLimit": True,
    **({"apiKey": BINANCE_API_KEY, "secret": BINANCE_API_SECRET} if BINANCE_API_KEY else {})
})

# ---------- Настройки пар и TF ----------
PAIRS = ["BTC/USDT", "ETH/USDT", "XRP/USDT", "MATIC/USDT", "ADA/USDT", "DOGE/USDT", "SOL/USDT", "TRX/USDT", "SUI/USDT"]
TIMEFRAMES = ["15m", "30m", "1h", "4h", "1d"]

# Хранилище выбора пользователей (в памяти)
user_settings = {}  # {chat_id: {"pair": "BTC/USDT", "tf":"1h"}}


# ---------- Утилиты: загрузка данных и индикаторы ----------
def fetch_ohlcv(symbol: str, timeframe: str, limit: int = 200):
    """
    Возвращает DataFrame с колонками time, open, high, low, close, volume
    symbol в формате 'BTC/USDT'
    """
    # ccxt требует символ без изменений: 'BTC/USDT'
    ohlcv = binance.fetch_ohlcv(symbol, timeframe=timeframe, limit=limit)
    df = pd.DataFrame(ohlcv, columns=["time", "open", "high", "low", "close", "volume"])
    df["close"] = df["close"].astype(float)
    df["volume"] = df["volume"].astype(float)
    return df


def calculate_indicators(df: pd.DataFrame):
    # EMA20, EMA50, EMA200
    df["EMA20"] = df["close"].ewm(span=20, adjust=False).mean()
    df["EMA50"] = df["close"].ewm(span=50, adjust=False).mean()
    df["EMA200"] = df["close"].ewm(span=200, adjust=False).mean()

    # RSI 14
    delta = df["close"].diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    avg_gain = gain.rolling(14).mean()
    avg_loss = loss.rolling(14).mean()
    rs = avg_gain / avg_loss
    df["RSI"] = 100 - (100 / (1 + rs))

    # MACD (EMA12 - EMA26) and signal
    ema12 = df["close"].ewm(span=12, adjust=False).mean()
    ema26 = df["close"].ewm(span=26, adjust=False).mean()
    df["MACD"] = ema12 - ema26
    df["MACD_SIGNAL"] = df["MACD"].ewm(span=9, adjust=False).mean()

    return df


def decide_signal(df: pd.DataFrame):
    last = df.iloc[-1]
    ema20 = last["EMA20"]
    ema50 = last["EMA50"]
    rsi = last["RSI"]
    macd = last["MACD"]
    macd_signal = last["MACD_SIGNAL"]

    # Простая логика — можно менять/улучшать
    if ema20 > ema50 and rsi > 55 and macd > macd_signal:
        return "✅ BUY"
    if ema20 < ema50 and rsi < 45 and macd < macd_signal:
        return "❌ SELL"
    return "⏸ HOLD"


# ---------- Формирование клавиатур ----------
def keyboard_pairs():
    keys = [[p] for p in PAIRS]
    return ReplyKeyboardMarkup(keys, one_time_keyboard=True, resize_keyboard=True)


def keyboard_timeframes():
    keys = [[t] for t in TIMEFRAMES]
    return ReplyKeyboardMarkup(keys, one_time_keyboard=True, resize_keyboard=True)


def keyboard_after_signal():
    return ReplyKeyboardMarkup([["🔄 Обновить сигнал", "📌 Сменить пару"]], resize_keyboard=True)


# ---------- Хэндлеры ----------

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    user_settings[chat_id] = {"pair": None, "tf": None}
    await update.message.reply_text("Ассаламу алейкум! 👋\nВыберите торговую пару:", reply_markup=keyboard_pairs())


async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Универсальный обработчик: сначала ловим выбор пары, затем выбор TF,
    также обрабатываем кнопки обновления и смены пары.
    """
    chat_id = update.effective_chat.id
    text = (update.message.text or "").strip()

    # Обработать кнопки действий
    if text == "🔄 Обновить сигнал":
        await refresh_signal(update, context)
        return
    if text == "📌 Сменить пару":
        user_settings[chat_id] = {"pair": None, "tf": None}
        await update.message.reply_text("Выберите торговую пару:", reply_markup=keyboard_pairs())
        return

    # Если пара не выбрана — считаем, что ввод пользователя это пара
    settings = user_settings.get(chat_id, {"pair": None, "tf": None})
    if not settings.get("pair"):
        # Поддерживаем вводы в виде "BTCUSDT" или "BTC/USDT"
        candidate = text.upper().replace(" ", "")
        if "/" not in candidate and len(candidate) >= 6:
            # переводим в формат ccxt 'BTC/USDT'
            if candidate.endswith("USDT"):
                candidate = candidate[:-4] + "/USDT"
        if candidate not in PAIRS:
            await update.message.reply_text("❌ Такой пары нет в списке. Выберите одну из кнопок.", reply_markup=keyboard_pairs())
            return
        # сохранили пару и предложили ТФ
        user_settings[chat_id]["pair"] = candidate
        await update.message.reply_text(f"✅ Пара выбрана: {candidate}\nТеперь выберите таймфрейм:", reply_markup=keyboard_timeframes())
        return

    # Если пара есть, но tf не выбран — считаем ввод как TF
    if settings.get("pair") and not settings.get("tf"):
        tf = text
        if tf not in TIMEFRAMES:
            await update.message.reply_text("❌ Такой таймфрейм не поддерживается. Выберите из списка.", reply_markup=keyboard_timeframes())
            return
        user_settings[chat_id]["tf"] = tf
        await update.message.reply_text(f"📊 Загружаю данные для {settings['pair']} ({tf}) ...", reply_markup=ReplyKeyboardRemove())
        await send_signal_for_user(chat_id, update, context)
        return

    # Если пара и tf уже установлены и ввели свободный текст — предложим варианты
    await update.message.reply_text("Для обновления сигнала используйте кнопку «🔄 Обновить сигнал» или «📌 Сменить пару».",
                                    reply_markup=keyboard_after_signal())


async def send_signal_for_user(chat_id: int, update: Update, context: ContextTypes.DEFAULT_TYPE):
    settings = user_settings.get(chat_id)
    if not settings or not settings.get("pair") or not settings.get("tf"):
        await context.bot.send_message(chat_id, "⚠️ Сначала выберите пару и таймфрейм через /start", reply_markup=keyboard_pairs())
        return

    pair = settings["pair"]
    tf = settings["tf"]

    try:
        df = fetch_ohlcv(pair, tf, limit=200)
        df = calculate_indicators(df)
        signal = decide_signal(df)
        last_price = df["close"].iloc[-1]

        text = (
            f"📊 {pair} ({tf})\n"
            f"Цена: {last_price:.4f}\n\n"
            f"EMA20: {df['EMA20'].iloc[-1]:.4f} | EMA50: {df['EMA50'].iloc[-1]:.4f} | EMA200: {df['EMA200'].iloc[-1]:.4f}\n"
            f"RSI: {df['RSI'].iloc[-1]:.2f} | MACD: {df['MACD'].iloc[-1]:.6f} | Signal: {df['MACD_SIGNAL'].iloc[-1]:.6f}\n\n"
            f"➡️ Сигнал: {signal}"
        )
        await context.bot.send_message(chat_id, text, reply_markup=keyboard_after_signal())
    except Exception as e:
        logger.exception("Ошибка при получении/анализе данных")
        await context.bot.send_message(chat_id, f"⚠️ Ошибка при получении данных: {e}")


async def refresh_signal(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    settings = user_settings.get(chat_id)
    if not settings or not settings.get("pair") or not settings.get("tf"):
        await update.message.reply_text("⚠️ Сначала выберите пару и таймфрейм через /start", reply_markup=keyboard_pairs())
        return
    await update.message.reply_text(f"🔄 Обновляю сигнал для {settings['pair']} ({settings['tf']}) ...")
    await send_signal_for_user(chat_id, update, context)


# ---------- MAIN ----------
def main():
    application = Application.builder().token(TELEGRAM_TOKEN).build()

    application.add_handler(CommandHandler("start", cmd_start))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))

    logger.info("Bot started")
    application.run_polling()


if __name__ == "__main__":
    main()
