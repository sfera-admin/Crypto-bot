# bot.py — Telegram TA-bot: manual / auto / scalp (ready to paste)
import os
import time
import threading
import traceback
import requests
import telebot
from telebot import apihelper
from binance.client import Client
import pandas as pd
import numpy as np
import ta
import schedule

# ---------------- CONFIG ----------------
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN") or os.getenv("BOT_TOKEN") or os.getenv("BOT")
if not TELEGRAM_TOKEN:
    raise RuntimeError("TELEGRAM_TOKEN / BOT_TOKEN not set in env")

# make Telegram requests less likely to timeout
apihelper.SESSION_TIMEOUT = 60
bot = telebot.TeleBot(TELEGRAM_TOKEN)

# Binance public client (no keys required for public klines)
client = Client()

# your chat id (only you will receive signals)
USER_CHAT_ID = 1217715528

# pairs/timeframes
PAIRS = ["BTCUSDT","ETHUSDT","BNBUSDT","XRPUSDT","ADAUSDT","DOGEUSDT","SOLUSDT","MATICUSDT","DOTUSDT"]
TIMEFRAMES = {"5m":"5m","15m":"15m","1h":"1h","4h":"4h","1d":"1d"}

# state
state = {
    "mode": None,            # None / "manual" / "auto" / "scalp"
    "pair": None,
    "timeframe": None,
    "scalp_enabled": False,  # whether scalp scanning is active for selected pair
}

# ---------------- safe send ----------------
def safe_send(chat_id, text, max_retries=3, delay=4):
    for attempt in range(1, max_retries+1):
        try:
            bot.send_message(chat_id, text)
            return True
        except requests.exceptions.ReadTimeout:
            print(f"[safe_send] ReadTimeout attempt {attempt}, retry in {delay}s")
            time.sleep(delay)
        except Exception as e:
            print(f"[safe_send] Exception sending message: {e} (attempt {attempt})")
            time.sleep(delay)
    print("[safe_send] failed to send message after retries")
    return False

# ---------------- data fetch ----------------
def fetch_klines(symbol, interval, limit=300):
    try:
        klines = client.get_klines(symbol=symbol, interval=interval, limit=limit)
        df = pd.DataFrame(klines, columns=[
            "open_time","open","high","low","close","volume","close_time","quote_av","trades","tb_base_av","tb_quote_av","ignore"
        ])
        df = df.astype({"open":float,"high":float,"low":float,"close":float,"volume":float})
        return df
    except Exception as e:
        print(f"[fetch_klines] error {symbol} {interval}: {e}")
        return None

# ---------------- indicators & helpers ----------------
def compute_indicators(df):
    d = df.copy()
    d["ema20"] = ta.trend.EMAIndicator(d["close"], window=20).ema_indicator()
    d["ema50"] = ta.trend.EMAIndicator(d["close"], window=50).ema_indicator()
    d["ema200"] = ta.trend.EMAIndicator(d["close"], window=200).ema_indicator()
    d["rsi"] = ta.momentum.RSIIndicator(d["close"], window=14).rsi()
    macd = ta.trend.MACD(d["close"], window_slow=26, window_fast=12, window_sign=9)
    d["macd"] = macd.macd()
    d["macd_signal"] = macd.macd_signal()
    d["atr"] = ta.volatility.AverageTrueRange(d["high"], d["low"], d["close"], window=14).average_true_range()
    d["vol_ma20"] = d["volume"].rolling(20).mean()
    return d

def detect_macd_cross(df):
    if len(df) < 2: return None
    p = df.iloc[-2]; l = df.iloc[-1]
    if p["macd"] < p["macd_signal"] and l["macd"] > l["macd_signal"]: return "up"
    if p["macd"] > p["macd_signal"] and l["macd"] < l["macd_signal"]: return "down"
    return None

def detect_ema20_50_cross(df):
    if len(df) < 2: return None
    p = df.iloc[-2]; l = df.iloc[-1]
    if p["ema20"] <= p["ema50"] and l["ema20"] > l["ema50"]: return "up"
    if p["ema20"] >= p["ema50"] and l["ema20"] < l["ema50"]: return "down"
    return None

# ---------------- core analysis ----------------
def analyze_df_for_pair(df, pair_display=None, tf_display=None, scalp_mode=False):
    """
    returns: (label, strength, report_text)
    label in {"BUY","SELL","HOLD"}
    strength: 0..3  (0=HOLD,1=Weak,2=Medium,3=Strong)
    """
    try:
        df = compute_indicators(df)
        if len(df) < 30:
            return "HOLD",0,f"Недостаточно данных (len={len(df)})"

        last = df.iloc[-1]
        prev = df.iloc[-2] if len(df)>=2 else last

        price = last["close"]
        ema20 = last["ema20"]; ema50 = last["ema50"]; ema200 = last["ema200"]
        rsi = last["rsi"]; macd = last["macd"]; macd_sig = last["macd_signal"]
        atr = last["atr"]; vol = last["volume"]; vol_ma = last["vol_ma20"]

        # trend flags
        trend_up = (ema20 > ema50) and (ema50 > ema200)
        trend_down = (ema20 < ema50) and (ema50 < ema200)

        macd_cross = detect_macd_cross(df)       # 'up'/'down'/None
        ema_cross = detect_ema20_50_cross(df)    # 'up'/'down'/None

        vol_ok = (vol_ma is not None) and (not np.isnan(vol_ma)) and (vol > vol_ma)
        price_above_ema20 = price > ema20
        price_below_ema20 = price < ema20

        # RSI thresholds: чуть более мягкие, чтобы не полностью молчал
        rsi_buyish = rsi < 45
        rsi_strong_buy = rsi < 35
        rsi_sellish = rsi > 55
        rsi_strong_sell = rsi > 65

        buy_score = 0.0
        sell_score = 0.0

        # Trend weight
        if trend_up: buy_score += 1.0
        if trend_down: sell_score += 1.0

        # EMA cross
        if ema_cross == "up": buy_score += 1.0
        if ema_cross == "down": sell_score += 1.0

        # MACD cross
        if macd_cross == "up": buy_score += 1.0
        if macd_cross == "down": sell_score += 1.0

        # RSI
        if rsi_strong_buy: buy_score += 1.0
        elif rsi_buyish: buy_score += 0.5

        if rsi_strong_sell: sell_score += 1.0
        elif rsi_sellish: sell_score += 0.5

        # Volume + price confirmation
        if vol_ok and price_above_ema20: buy_score += 0.7
        if vol_ok and price_below_ema20: sell_score += 0.7

        # Additional scalp-mode relax: accept smaller confirmations if scalp_mode True
        if scalp_mode:
            # make scalp slightly more sensitive: reduce required thresholds implicitly by boosting scores from EMA cross / macd
            # (we already evaluate with smaller window, but this tweak increases chance of scalp signals)
            pass  # current scoring already allows scalp via different schedule; keep as-is for clarity

        # Convert to strength 0..3
        def to_strength(score):
            if score >= 4: return 3
            if score >= 2: return 2
            if score > 0: return 1
            return 0

        b_str = to_strength(buy_score)
        s_str = to_strength(sell_score)

        if b_str > s_str and b_str > 0:
            label = "BUY"; strength = b_str
        elif s_str > b_str and s_str > 0:
            label = "SELL"; strength = s_str
        else:
            label = "HOLD"; strength = 0

        strmap = {0:"HOLD",1:"Weak",2:"Medium",3:"Strong"}
        report = (
            f"📊 {pair_display or state.get('pair','?')} ({tf_display or state.get('timeframe','?')})\n"
            f"Цена: {price:.6f}\n\n"
            f"EMA20: {ema20:.6f} | EMA50: {ema50:.6f} | EMA200: {ema200:.6f}\n"
            f"RSI: {rsi:.2f} | MACD: {macd:.6f} | Signal: {macd_sig:.6f}\n"
            f"ATR: {atr:.6f} | Vol: {vol:.4f} | VolMA20: {vol_ma:.4f}\n\n"
            f"➡️ Сигнал: {label} ({strmap[strength]})\n"
            f"Детали: trend_up={trend_up}, ema_cross={ema_cross}, macd_cross={macd_cross}, vol_ok={vol_ok}"
        )
        return label, strength, report
    except Exception as e:
        print("analyze_df_for_pair error", e)
        traceback.print_exc()
        return "HOLD",0,f"Ошибка анализа: {e}"

# ---------------- tasks ----------------
def send_first_analysis_for_selected():
    pair = state.get("pair"); tf = state.get("timeframe")
    if not pair or not tf:
        return
    df = fetch_klines(pair, tf, limit=300)
    if df is None:
        safe_send(USER_CHAT_ID, f"❌ Не удалось загрузить данные для {pair} [{tf}]")
        return
    label,strength,report = analyze_df_for_pair(df, pair, tf)
    # always send first analysis so user sees bot works
    safe_send(USER_CHAT_ID, report)

def periodic_selected_check():
    pair = state.get("pair"); tf = state.get("timeframe")
    if not pair or not tf:
        return
    df = fetch_klines(pair, tf, limit=300)
    if df is None:
        print(f"[periodic_selected_check] no data {pair} {tf}")
        return
    label,strength,report = analyze_df_for_pair(df, pair, tf)
    # only send medium/strong signals to avoid spam
    if label in ("BUY","SELL") and strength >= 2:
        safe_send(USER_CHAT_ID, "(selected) " + report)
    else:
        print(f"[selected {pair} {tf}] label={label} strength={strength}")

def background_auto_scan():
    # scans all pairs for key timeframes and sends medium/strong signals
    scan_tfs = ["15m","1h","4h"]  # auto scanning frames
    for pair in PAIRS:
        for tf in scan_tfs:
            df = fetch_klines(pair, tf, limit=300)
            if df is None:
                continue
            label,strength,report = analyze_df_for_pair(df, pair, tf)
            if label in ("BUY","SELL") and strength >= 2:
                safe_send(USER_CHAT_ID, "🔔 AUTO-SCAN signal:\n" + report)
            else:
                print(f"[auto_scan] {pair} {tf} -> {label}({strength})")

def background_scalp_scan():
    # scalp: only for selected pair, timeframe 5m
    if not state.get("scalp_enabled"): return
    pair = state.get("pair")
    if not pair: return
    df = fetch_klines(pair, "5m", limit=200)
    if df is None: return
    label,strength,report = analyze_df_for_pair(df, pair, "5m", scalp_mode=True)
    # scalp sends also medium/strong
    if label in ("BUY","SELL") and strength >= 2:
        safe_send(USER_CHAT_ID, "⚡ SCALP signal:\n" + report)
    else:
        print(f"[scalp] {pair} 5m -> {label}({strength})")

# ---------------- Telegram UI ----------------
def main_menu_kb():
    kb = telebot.types.ReplyKeyboardMarkup(resize_keyboard=True)
    kb.add("🧭 Manual (ручной)")
    kb.add("🤖 Auto (авто)")
    kb.add("⚡ Scalp (скальпинг 5m)")
    kb.add("🛑 Stop (выключить автоскан/скальп)")
    return kb

def pair_kb():
    kb = telebot.types.ReplyKeyboardMarkup(resize_keyboard=True)
    for p in PAIRS: kb.add(p)
    kb.add("🔙 Назад")
    return kb

def tf_kb():
    kb = telebot.types.ReplyKeyboardMarkup(resize_keyboard=True)
    kb.add("5m","15m","1h","4h","1d")
    kb.add("🔙 Назад")
    return kb

@bot.message_handler(commands=["start"])
def cmd_start(m):
    if m.chat.id != USER_CHAT_ID:
        bot.send_message(m.chat.id, "⛔ Доступ запрещён")
        return
    state["mode"] = None
    safe_send(m.chat.id, "👋 Мир вам дорогие друзья!\nВыберите режим работы бота:",)
    bot.send_message(m.chat.id, "Режимы:", reply_markup=main_menu_kb())

@bot.message_handler(func=lambda m: m.text == "🧭 Manual (ручной)")
def choose_manual(m):
    if m.chat.id != USER_CHAT_ID: return
    state["mode"] = "manual"
    safe_send(m.chat.id, "Выбран ручной режим. Выберите торговую пару:",)
    bot.send_message(m.chat.id, "Пара:", reply_markup=pair_kb())

@bot.message_handler(func=lambda m: m.text == "🤖 Auto (авто)")
def choose_auto(m):
    if m.chat.id != USER_CHAT_ID: return
    state["mode"] = "auto"
    state["pair"] = None; state["timeframe"] = None
    safe_send(m.chat.id, "Авто-режим включён. Бот сам будет сканировать все пары и присылать только качественные сигналы (medium/strong).",)
    # ensure scheduler tags: start auto scan every 5 minutes
    schedule.clear('auto_scan'); schedule.every(5).minutes.do(background_auto_scan).tag('auto_scan')

@bot.message_handler(func=lambda m: m.text == "⚡ Scalp (скальпинг 5m)")
def choose_scalp(m):
    if m.chat.id != USER_CHAT_ID: return
    state["mode"] = "scalp"
    # require user to choose pair first
    safe_send(m.chat.id, "Скальпинг выбран. Выберите пару для скальпинга (5m):",)
    bot.send_message(m.chat.id, "Пара для скальпа:", reply_markup=pair_kb())

@bot.message_handler(func=lambda m: m.text == "🛑 Stop (выключить автоскан/скальп)")
def stop_scans(m):
    if m.chat.id != USER_CHAT_ID: return
    # stop all background jobs
    state["mode"] = None
    state["scalp_enabled"] = False
    schedule.clear('auto_scan'); schedule.clear('selected'); schedule.clear('scalp')
    safe_send(m.chat.id, "Автосканы и скальп отключены. Возвращаю главное меню.",)
    bot.send_message(m.chat.id, "Главное меню:", reply_markup=main_menu_kb())

@bot.message_handler(func=lambda m: m.text in PAIRS)
def handle_pair_choice(m):
    if m.chat.id != USER_CHAT_ID: return
    pair = m.text
    # if in manual mode -> ask timeframe
    if state.get("mode") == "manual":
        state["pair"] = pair
        safe_send(m.chat.id, f"Пара {pair} выбрана. Выберите таймфрейм:",)
        bot.send_message(m.chat.id, "ТФ:", reply_markup=tf_kb())
    elif state.get("mode") == "scalp":
        state["pair"] = pair
        # enable scalp scanning for this pair
        state["scalp_enabled"] = True
        safe_send(m.chat.id, f"Скальпинг включён для {pair} (5m). Бот будет анализировать 5m и присылать medium/strong сигналы.",)
        # start scalp schedule
        schedule.clear('scalp'); schedule.every(1).minutes.do(background_scalp_scan).tag('scalp')
        # optionally send immediate first scalp check
        try:
            df = fetch_klines(pair, "5m", limit=200)
            if df is not None:
                lab,strg,rep = analyze_df_for_pair(df, pair, "5m", scalp_mode=True)
                safe_send(m.chat.id, "Первый SCALP-анализ:\n" + rep)
            else:
                safe_send(m.chat.id, f"Не удалось загрузить 5m данные для {pair}")
        except Exception as e:
            print("scalp first check error",e)
    else:
        # if not in a mode, offer to choose mode
        safe_send(m.chat.id, "Сначала выбери режим (Manual / Auto / Scalp).",)
        bot.send_message(m.chat.id, "Режимы:", reply_markup=main_menu_kb())

@bot.message_handler(func=lambda m: m.text in ["5m","15m","1h","4h","1d"])
def handle_tf_choice(m):
    if m.chat.id != USER_CHAT_ID: return
    tf = m.text
    if state.get("mode") != "manual":
        safe_send(m.chat.id, "TF можно выбирать только в Manual режиме. Сначала выберите Manual.",)
        bot.send_message(m.chat.id, "Режимы:", reply_markup=main_menu_kb())
        return
    if not state.get("pair"):
        safe_send(m.chat.id, "Сначала выберите пару.",)
        bot.send_message(m.chat.id, "Пара:", reply_markup=pair_kb())
        return
    # set selection
    state["timeframe"] = tf
    safe_send(m.chat.id, f"Ручной режим: {state['pair']} [{tf}] выбран. Сейчас пришлю первый анализ...",)
    # immediate first analysis
    try:
        df = fetch_klines(state["pair"], tf, limit=300)
        if df is None:
            safe_send(m.chat.id, f"Ошибка загрузки данных для {state['pair']} [{tf}]")
            return
        lab,strg,rep = analyze_df_for_pair(df, state["pair"], tf)
        safe_send(m.chat.id, rep)
    except Exception as e:
        print("first manual analysis error", e)
        safe_send(m.chat.id, f"Ошибка при первом анализе: {e}")
        return
    # schedule periodic checks for selected pair (only medium/strong send)
    schedule.clear('selected')
    if tf == "5m":
        schedule.every(5).minutes.do(periodic_selected_check).tag('selected')
    elif tf == "15m":
        schedule.every(15).minutes.do(periodic_selected_check).tag('selected')
    elif tf == "1h":
        schedule.every().hour.do(periodic_selected_check).tag('selected')
    elif tf == "4h":
        schedule.every(4).hours.do(periodic_selected_check).tag('selected')
    elif tf == "1d":
        schedule.every().day.do(periodic_selected_check).tag('selected')

@bot.message_handler(func=lambda m: m.text == "🔙 Назад")
def handle_back(m):
    if m.chat.id != USER_CHAT_ID: return
    safe_send(m.chat.id, "Возврат в главное меню",)
    bot.send_message(m.chat.id, "Главное меню:", reply_markup=main_menu_kb())

# ---------------- schedule loop ----------------
def schedule_loop():
    # ensure auto-scan is set if user selected auto before restart
    while True:
        try:
            schedule.run_pending()
            time.sleep(1)
        except Exception as e:
            print("schedule_loop exception", e)
            time.sleep(3)

# ---------------- startup ----------------
def start_bot():
    # start schedule thread
    t = threading.Thread(target=schedule_loop, daemon=True)
    t.start()
    # start polling
    try:
        bot.polling(non_stop=True, timeout=60)
    except Exception as e:
        print("Polling error:", e)
        traceback.print_exc()

if __name__ == "__main__":
    print("Starting TA-bot with modes manual/auto/scalp...")
    start_bot()

