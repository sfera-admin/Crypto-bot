# bot.py — финальный: Manual / Auto / Scalp / Levels + красивые карточки (no images)
import os
import time
import threading
import traceback
import math
import requests
import telebot
from telebot import apihelper
from binance.client import Client
import pandas as pd
import numpy as np
import ta
import schedule

# ---------------- Config ----------------
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN") or os.getenv("BOT_TOKEN") or os.getenv("BOT")
if not TELEGRAM_TOKEN:
    raise RuntimeError("TELEGRAM_TOKEN / BOT_TOKEN not set in env")

# increase Telegram session timeout to reduce ReadTimeouts
apihelper.SESSION_TIMEOUT = 60
bot = telebot.TeleBot(TELEGRAM_TOKEN)

# Binance public client (no API keys required for klines)
client = Client()

# only you (change if needed)
USER_CHAT_ID = 1217715528

# pairs and allowed timeframes
PAIRS = ["BTCUSDT","ETHUSDT","BNBUSDT","XRPUSDT","ADAUSDT","DOGEUSDT","SOLUSDT","MATICUSDT","DOTUSDT"]
TIMEFRAMES = {"5m":"5m","15m":"15m","1h":"1h","4h":"4h","1d":"1d"}

# state
state = {
    "mode": None,        # None / "manual" / "auto" / "scalp" / "levels"
    "pair": None,
    "timeframe": None,
    "scalp_enabled": False,
    "levels_enabled": False,
}

# weak signals control
SEND_WEAK_SIGNALS = False
WEAK_SEND_COOLDOWN = 60 * 60  # 1 hour per (pair,tf)
_last_weak_sent = {}  # {(pair,tf): timestamp}

# small helper to throttle debug prints in heavy loops
def now_ts():
    return int(time.time())

# ---------------- Safe send ----------------
def safe_send(chat_id, text, max_retries=3, delay=4):
    """Send message with retries on transient errors (timeout etc)."""
    for attempt in range(1, max_retries+1):
        try:
            bot.send_message(chat_id, text)
            return True
        except requests.exceptions.ReadTimeout:
            print(f"[safe_send] ReadTimeout attempt {attempt}, retry in {delay}s")
            time.sleep(delay)
        except Exception as e:
            print(f"[safe_send] Exception while sending (attempt {attempt}): {e}")
            time.sleep(delay)
    print("[safe_send] Failed to send message after retries.")
    return False

# ---------------- Data fetch ----------------
def fetch_klines(symbol, interval, limit=300):
    """Get klines from Binance public endpoint and return DataFrame or None."""
    try:
        klines = client.get_klines(symbol=symbol, interval=interval, limit=limit)
        df = pd.DataFrame(klines, columns=[
            "open_time","open","high","low","close","volume","close_time","quote_av","trades","tb_base_av","tb_quote_av","ignore"
        ])
        df = df.astype({"open": float, "high": float, "low": float, "close": float, "volume": float})
        return df
    except Exception as e:
        print(f"[fetch_klines] error {symbol} {interval}: {e}")
        return None

# ---------------- Indicators & helpers ----------------
def compute_indicators(df):
    d = df.copy()
    # EMA
    d["ema20"] = ta.trend.EMAIndicator(d["close"], window=20).ema_indicator()
    d["ema50"] = ta.trend.EMAIndicator(d["close"], window=50).ema_indicator()
    d["ema200"] = ta.trend.EMAIndicator(d["close"], window=200).ema_indicator()
    # RSI
    d["rsi"] = ta.momentum.RSIIndicator(d["close"], window=14).rsi()
    # MACD
    macd = ta.trend.MACD(d["close"], window_slow=26, window_fast=12, window_sign=9)
    d["macd"] = macd.macd()
    d["macd_signal"] = macd.macd_signal()
    # ATR and volume MA
    d["atr"] = ta.volatility.AverageTrueRange(d["high"], d["low"], d["close"], window=14).average_true_range()
    d["vol_ma20"] = d["volume"].rolling(20).mean()
    return d

def detect_macd_cross(df):
    if len(df) < 2: return None
    p, l = df.iloc[-2], df.iloc[-1]
    if p["macd"] < p["macd_signal"] and l["macd"] > l["macd_signal"]: return "up"
    if p["macd"] > p["macd_signal"] and l["macd"] < l["macd_signal"]: return "down"
    return None

def detect_ema20_50_cross(df):
    if len(df) < 2: return None
    p, l = df.iloc[-2], df.iloc[-1]
    if p["ema20"] <= p["ema50"] and l["ema20"] > l["ema50"]: return "up"
    if p["ema20"] >= p["ema50"] and l["ema20"] < l["ema50"]: return "down"
    return None

# ---------------- Levels (support/resistance) ----------------
def find_local_extrema(series, order=3):
    """Simple local minima/maxima finder. order=bars to each side."""
    minima = []
    maxima = []
    n = len(series)
    for i in range(order, n-order):
        window = series[i-order:i+order+1]
        val = series[i]
        if val == window.min():
            minima.append((i, val))
        if val == window.max():
            maxima.append((i, val))
    return minima, maxima

def cluster_levels(values, threshold=0.005):
    """
    Cluster similar levels (values list) into representative levels.
    threshold — relative distance (e.g., 0.005 = 0.5%).
    """
    if not values:
        return []
    vals = sorted(values)
    clusters = []
    current = [vals[0]]
    for v in vals[1:]:
        if abs(v - current[-1]) / current[-1] <= threshold:
            current.append(v)
        else:
            clusters.append(current)
            current = [v]
    clusters.append(current)
    centers = [sum(c)/len(c) for c in clusters]
    return centers

def get_levels_from_df(df, order=4, cluster_threshold=0.006):
    """
    Return (supports, resistances) — lists of levels (floats).
    order: window for local extrema
    cluster_threshold: clustering threshold (relative)
    """
    closes = df["close"].values
    minima, maxima = find_local_extrema(closes, order=order)
    min_vals = [v for i,v in minima]
    max_vals = [v for i,v in maxima]
    supports = cluster_levels(min_vals, threshold=cluster_threshold)
    resistances = cluster_levels(max_vals, threshold=cluster_threshold)
    # sort supports ascending, resistances descending
    supports.sort()
    resistances.sort(reverse=True)
    return supports, resistances

# ---------------- Core analysis (BUY/SELL/HOLD + strength) ----------------
def analyze_df_for_pair(df, pair_display=None, tf_display=None, scalp_mode=False, levels_mode=False):
    """
    returns (label, strength, report_text, details_dict)
    label: "BUY"/"SELL"/"HOLD"
    strength: 0..3 -> map later to stars
    details_dict: useful info (levels if computed)
    """
    try:
        df = compute_indicators(df)
        if len(df) < 30:
            return "HOLD", 0, f"Недостаточно данных для анализа (len={len(df)})", {}

        last = df.iloc[-1]
        prev = df.iloc[-2] if len(df) >= 2 else last

        price = last["close"]
        ema20 = last["ema20"]; ema50 = last["ema50"]; ema200 = last["ema200"]
        rsi = last["rsi"]; macd = last["macd"]; macd_sig = last["macd_signal"]
        atr = last["atr"]; vol = last["volume"]; vol_ma = last["vol_ma20"]

        trend_up = (ema20 > ema50) and (ema50 > ema200)
        trend_down = (ema20 < ema50) and (ema50 < ema200)

        macd_cross = detect_macd_cross(df)
        ema_cross = detect_ema20_50_cross(df)

        vol_ok = (vol_ma is not None) and (not np.isnan(vol_ma)) and (vol > vol_ma)
        price_above_ema20 = price > ema20
        price_below_ema20 = price < ema20

        # RSI thresholds (we keep moderately strict for strong)
        rsi_buyish = rsi < 45
        rsi_strong_buy = rsi < 35
        rsi_sellish = rsi > 55
        rsi_strong_sell = rsi > 65

        buy_score = 0.0
        sell_score = 0.0

        # Trend weight
        if trend_up: buy_score += 1.0
        if trend_down: sell_score += 1.0

        # EMA cross weight
        if ema_cross == "up": buy_score += 1.0
        if ema_cross == "down": sell_score += 1.0

        # MACD cross weight
        if macd_cross == "up": buy_score += 1.0
        if macd_cross == "down": sell_score += 1.0

        # RSI weight
        if rsi_strong_buy: buy_score += 1.0
        elif rsi_buyish: buy_score += 0.5
        if rsi_strong_sell: sell_score += 1.0
        elif rsi_sellish: sell_score += 0.5

        # Volume & price confirmation
        if vol_ok and price_above_ema20: buy_score += 0.7
        if vol_ok and price_below_ema20: sell_score += 0.7

        # Map scores to strength 0..3
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

        # compute levels optionally
        levels = {}
        if levels_mode:
            supports, resistances = get_levels_from_df(df, order=4, cluster_threshold=0.006)
            levels["supports"] = supports
            levels["resistances"] = resistances

            # extra logic: if price near support/resistance, may increase strength
            # near = within atr*0.7 relative or within 0.5% absolute approx
            def near_level(level):
                # use absolute or relative threshold based on ATR
                if math.isnan(atr) or atr == 0:
                    rel = abs(price - level) / level
                    return rel < 0.004  # 0.4%
                else:
                    return abs(price - level) <= 0.7 * atr
            # if price near top resistance and label SELL, bump strength if possible
            for r in levels["resistances"][:3]:
                if near_level(r) and label == "SELL":
                    strength = max(strength, min(3, strength+1))
            for s in levels["supports"][:3]:
                if near_level(s) and label == "BUY":
                    strength = max(strength, min(3, strength+1))

        # Prepare report text (card)
        stars_map = {0: "—", 1: "⭐", 2: "⭐⭐", 3: "⭐⭐⭐"}
        strength_name = {0: "HOLD", 1: "Weak", 2: "Medium", 3: "Strong"}[strength]
        emoji_label = "🟢 BUY" if label == "BUY" else ("🔴 SELL" if label == "SELL" else "⚪ HOLD")

        report_lines = []
        report_lines.append("━━━━━━━━━━━━━━━━━━━━━━━━")
        report_lines.append(f"📊 Пара: {pair_display or state.get('pair','?')}")
        report_lines.append(f"⏱ Таймфрейм: {tf_display or state.get('timeframe','?')}")
        report_lines.append("")
        report_lines.append(f"{emoji_label}  ({strength_name})")
        report_lines.append(f"💵 Цена: {price:.6f}")
        report_lines.append("")
        # include top 1 support/resistance if present
        if levels_mode and levels.get("supports"):
            report_lines.append(f"📈 Поддержка: {levels['supports'][0]:.6f}")
        if levels_mode and levels.get("resistances"):
            report_lines.append(f"📉 Сопротивление: {levels['resistances'][0]:.6f}")
        report_lines.append("")
        report_lines.append("🔎 Индикаторы:")
        report_lines.append(f"• EMA20: {ema20:.6f} | EMA50: {ema50:.6f} | EMA200: {ema200:.6f}")
        rsi_status = f"RSI = {rsi:.2f}"
        report_lines.append(f"• {rsi_status} | MACD = {macd:.6f} | Signal = {macd_sig:.6f}")
        report_lines.append(f"• ATR = {atr:.6f} | Vol = {vol:.4f} | VolMA20 = {vol_ma:.4f}")
        report_lines.append("")
        # star rating 1..5 (map strength 0..3 -> 1..5 scale)
        # 0->1,1->3,2->4,3->5
        if strength == 0:
            stars = "⚪ No signal"
            score_val = "0/5"
        else:
            map_to = {1: "★★★ (3/5)", 2: "★★★★ (4/5)", 3: "★★★★★ (5/5)"}
            stars = map_to[strength]
            score_val = map_to[strength].split("(")[1].strip(")")
        report_lines.append(f"⭐ Оценка сигнала: {stars}")
        report_lines.append("")
        report_lines.append(f"Детали: trend_up={trend_up}, ema_cross={ema_cross}, macd_cross={macd_cross}, vol_ok={vol_ok}")
        report_lines.append("━━━━━━━━━━━━━━━━━━━━━━━━")
        report = "\n".join(report_lines)

        details = {"price": price, "ema20": ema20, "ema50": ema50, "ema200": ema200,
                   "rsi": rsi, "macd": macd, "macd_signal": macd_sig, "atr": atr, "vol": vol}
        if levels_mode:
            details["levels"] = levels
        return label, strength, report, details
    except Exception as e:
        print("analyze_df_for_pair error:", e)
        traceback.print_exc()
        return "HOLD", 0, f"Ошибка анализа: {e}", {}

# ---------------- Weak throttling ----------------
def should_send_weak(pair, tf):
    key = (pair, tf)
    last = _last_weak_sent.get(key)
    if last is None:
        _last_weak_sent[key] = time.time()
        return True
    if time.time() - last > WEAK_SEND_COOLDOWN:
        _last_weak_sent[key] = time.time()
        return True
    return False

# ---------------- Tasks: selected/auto/scalp/levels ----------------
def first_analysis_selected():
    pair = state.get("pair"); tf = state.get("timeframe")
    if not pair or not tf:
        return
    df = fetch_klines(pair, tf, limit=300)
    if df is None:
        safe_send(USER_CHAT_ID, f"❌ Не удалось загрузить данные для {pair} [{tf}]")
        return
    label, strength, report, details = analyze_df_for_pair(df, pair, tf, levels_mode=state.get("levels_enabled", False))
    safe_send(USER_CHAT_ID, report)

def periodic_selected_check():
    pair = state.get("pair"); tf = state.get("timeframe")
    if not pair or not tf:
        return
    df = fetch_klines(pair, tf, limit=300)
    if df is None:
        print(f"[periodic_selected_check] no data {pair} {tf}")
        return
    label, strength, report, details = analyze_df_for_pair(df, pair, tf, levels_mode=state.get("levels_enabled", False))
    if label in ("BUY","SELL"):
        if strength >= 2:
            safe_send(USER_CHAT_ID, "(Selected) " + report)
        elif strength == 1 and SEND_WEAK_SIGNALS and should_send_weak(pair, tf):
            safe_send(USER_CHAT_ID, "(Selected) ⚠ Weak:\n" + report)
        else:
            print(f"[selected {pair} {tf}] weak suppressed or HOLD")
    else:
        print(f"[selected {pair} {tf}] {label} (strength {strength})")

def background_auto_scan():
    scan_tfs = ["15m","1h","4h"]
    for pair in PAIRS:
        for tf in scan_tfs:
            df = fetch_klines(pair, tf, limit=300)
            if df is None:
                continue
            label, strength, report, details = analyze_df_for_pair(df, pair, tf, levels_mode=False)
            if label in ("BUY","SELL"):
                if strength >= 2:
                    safe_send(USER_CHAT_ID, "🔔 AUTO:\n" + report)
                elif strength == 1 and SEND_WEAK_SIGNALS and should_send_weak(pair, tf):
                    safe_send(USER_CHAT_ID, "🔔 AUTO ⚠ Weak:\n" + report)
                else:
                    print(f"[auto] {pair} {tf} -> weak suppressed")
            else:
                print(f"[auto] {pair} {tf} -> HOLD")

def background_scalp_scan():
    if not state.get("scalp_enabled"):
        return
    pair = state.get("pair")
    if not pair:
        return
    df = fetch_klines(pair, "5m", limit=200)
    if df is None:
        return
    label, strength, report, details = analyze_df_for_pair(df, pair, "5m", scalp_mode=True)
    if label in ("BUY","SELL"):
        if strength >= 2:
            safe_send(USER_CHAT_ID, "⚡ SCALP:\n" + report)
        elif strength == 1 and SEND_WEAK_SIGNALS and should_send_weak(pair, "5m"):
            safe_send(USER_CHAT_ID, "⚡ SCALP ⚠ Weak:\n" + report)
        else:
            print(f"[scalp] {pair} 5m -> weak suppressed")
    else:
        print(f"[scalp] {pair} 5m -> HOLD")

def background_levels_monitor():
    """Monitor levels for selected pair+tf, send when price near support/resistance"""
    if not state.get("levels_enabled"):
        return
    pair = state.get("pair")
    tf = state.get("timeframe")
    if not pair or not tf:
        return
    df = fetch_klines(pair, tf, limit=300)
    if df is None:
        return
    supports, resistances = get_levels_from_df(df, order=4, cluster_threshold=0.006)
    if not supports and not resistances:
        return
    price = df.iloc[-1]["close"]
    atr = ta.volatility.AverageTrueRange(df["high"], df["low"], df["close"], window=14).average_true_range().iloc[-1]
    # thresholds: near if within 0.7*ATR or within 0.4% if atr nan
    def near(lvl):
        if not (isinstance(atr, float) and not math.isnan(atr) and atr>0):
            return abs(price - lvl)/lvl < 0.004
        return abs(price - lvl) <= 0.7*atr
    # check supports (buy)
    for s in supports[:3]:
        if near(s):
            # build report including levels
            label, strength, report, details = analyze_df_for_pair(df, pair, tf, levels_mode=True)
            # If label is HOLD but price touches support -> we can send special BUY-from-level message
            if label == "BUY" or (label == "HOLD" and SEND_WEAK_SIGNALS):
                safe_send(USER_CHAT_ID, "🎯 LEVELS (support) detected:\n" + report)
            else:
                print(f"[levels] support near but label {label}")
            return
    # check resistances (sell)
    for r in resistances[:3]:
        if near(r):
            label, strength, report, details = analyze_df_for_pair(df, pair, tf, levels_mode=True)
            if label == "SELL" or (label == "HOLD" and SEND_WEAK_SIGNALS):
                safe_send(USER_CHAT_ID, "🎯 LEVELS (resistance) detected:\n" + report)
            else:
                print(f"[levels] resistance near but label {label}")
            return

# ---------------- Telegram UI ----------------
def main_menu_kb():
    kb = telebot.types.ReplyKeyboardMarkup(resize_keyboard=True)
    kb.add("🧭 Manual (ручной)")
    kb.add("🤖 Auto (авто)")
    kb.add("⚡ Scalp (скальпинг 5m)")
    kb.add("🎯 Levels (уровни)")
    kb.add("🧰 Toggle Weak (вкл/выкл weak)")
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
    # reset selection
    state["mode"] = None; state["pair"] = None; state["timeframe"] = None
    state["scalp_enabled"] = False; state["levels_enabled"] = False
    safe_send(m.chat.id, "👋 Мир вам дорогие друзья!\nВыберите режим работы бота:")
    bot.send_message(m.chat.id, "Режимы:", reply_markup=main_menu_kb())

@bot.message_handler(func=lambda m: m.text == "🧭 Manual (ручной)")
def choose_manual(m):
    if m.chat.id != USER_CHAT_ID: return
    state["mode"] = "manual"; state["scalp_enabled"] = False; state["levels_enabled"] = False
    safe_send(m.chat.id, "Ручной режим включён. Выберите торговую пару:")
    bot.send_message(m.chat.id, "Пара:", reply_markup=pair_kb())

@bot.message_handler(func=lambda m: m.text == "🤖 Auto (авто)")
def choose_auto(m):
    if m.chat.id != USER_CHAT_ID: return
    state["mode"] = "auto"; state["pair"] = None; state["timeframe"] = None
    state["scalp_enabled"] = False; state["levels_enabled"] = False
    safe_send(m.chat.id, "Авто-режим включён. Буду сканировать пары (15m/1h/4h) и присылать medium/strong (и weak по настройке).")
    schedule.clear('auto_scan'); schedule.every(5).minutes.do(background_auto_scan).tag('auto_scan')

@bot.message_handler(func=lambda m: m.text == "⚡ Scalp (скальпинг 5m)")
def choose_scalp(m):
    if m.chat.id != USER_CHAT_ID: return
    state["mode"] = "scalp"
    safe_send(m.chat.id, "Скальпинг выбран. Выберите пару для скальпинга (5m):")
    bot.send_message(m.chat.id, "Пара для скальпа:", reply_markup=pair_kb())

@bot.message_handler(func=lambda m: m.text == "🎯 Levels (уровни)")
def choose_levels(m):
    if m.chat.id != USER_CHAT_ID: return
    state["mode"] = "levels"
    safe_send(m.chat.id, "Режим уровней выбран. Выберите пару, затем таймфрейм:")
    bot.send_message(m.chat.id, "Пара:", reply_markup=pair_kb())

@bot.message_handler(func=lambda m: m.text == "🛑 Stop (выключить автоскан/скальп)")
def stop_scans(m):
    if m.chat.id != USER_CHAT_ID: return
    state["mode"] = None; state["pair"] = None; state["timeframe"] = None
    state["scalp_enabled"] = False; state["levels_enabled"] = False
    schedule.clear('auto_scan'); schedule.clear('selected'); schedule.clear('scalp'); schedule.clear('levels')
    safe_send(m.chat.id, "Автосканы, скальп и уровни отключены. Возвращаю меню.")
    bot.send_message(m.chat.id, "Главное меню:", reply_markup=main_menu_kb())

@bot.message_handler(func=lambda m: m.text == "🧰 Toggle Weak (вкл/выкл weak)")
def toggle_weak(m):
    if m.chat.id != USER_CHAT_ID: return
    global SEND_WEAK_SIGNALS
    SEND_WEAK_SIGNALS = not SEND_WEAK_SIGNALS
    safe_send(m.chat.id, f"Weak signals now {'ENABLED' if SEND_WEAK_SIGNALS else 'DISABLED'}")

@bot.message_handler(func=lambda m: m.text in PAIRS)
def handle_pair_choice(m):
    if m.chat.id != USER_CHAT_ID: return
    pair = m.text
    mode = state.get("mode")
    if mode == "manual":
        state["pair"] = pair
        safe_send(m.chat.id, f"Пара {pair} выбрана. Выберите таймфрейм:")
        bot.send_message(m.chat.id, "ТФ:", reply_markup=tf_kb())
    elif mode == "scalp":
        state["pair"] = pair
        state["scalp_enabled"] = True
        safe_send(m.chat.id, f"Скальпинг включён для {pair} (5m). Бот будет анализировать 5m.")
        schedule.clear('scalp'); schedule.every(1).minutes.do(background_scalp_scan).tag('scalp')
        # immediate scalp analysis
        df = fetch_klines(pair, "5m", limit=200)
        if df is not None:
            lab,strg,rep,det = analyze_df_for_pair(df, pair, "5m", scalp_mode=True)
            safe_send(m.chat.id, "Первый SCALP-анализ:\n" + rep)
        else:
            safe_send(m.chat.id, f"Не удалось загрузить 5m данные для {pair}")
    elif mode == "levels":
        state["pair"] = pair
        safe_send(m.chat.id, f"Пара {pair} выбрана для уровней. Выберите таймфрейм:")
        bot.send_message(m.chat.id, "ТФ:", reply_markup=tf_kb())
    else:
        safe_send(m.chat.id, "Сначала выберите режим: Manual / Auto / Scalp / Levels")
        bot.send_message(m.chat.id, "Режимы:", reply_markup=main_menu_kb())

@bot.message_handler(func=lambda m: m.text in ["5m","15m","1h","4h","1d"])
def handle_tf_choice(m):
    if m.chat.id != USER_CHAT_ID: return
    tf = m.text
    mode = state.get("mode")
    if mode == "manual":
        if not state.get("pair"):
            safe_send(m.chat.id, "Сначала выберите пару.")
            bot.send_message(m.chat.id, "Пара:", reply_markup=pair_kb())
            return
        state["timeframe"] = tf
        safe_send(m.chat.id, f"Manual: {state['pair']} [{tf}] выбран. Пришлю первый анализ...")
        df = fetch_klines(state["pair"], tf, limit=300)
        if df is None:
            safe_send(m.chat.id, f"Ошибка загрузки данных для {state['pair']} [{tf}]")
            return
        lab,strg,rep,det = analyze_df_for_pair(df, state["pair"], tf)
        safe_send(m.chat.id, rep)
        schedule.clear('selected')
        # schedule periodic selected checks
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
    elif mode == "levels":
        if not state.get("pair"):
            safe_send(m.chat.id, "Сначала выберите пару.")
            bot.send_message(m.chat.id, "Пара:", reply_markup=pair_kb())
            return
        state["timeframe"] = tf
        state["levels_enabled"] = True
        safe_send(m.chat.id, f"Levels: {state['pair']} [{tf}] выбран. Я буду отслеживать уровни и отправлять уведомления при касании.")
        schedule.clear('levels')
        schedule.every(2).minutes.do(background_levels_monitor).tag('levels')
        # immediate first levels analysis
        df = fetch_klines(state["pair"], tf, limit=300)
        if df is not None:
            supports, resistances = get_levels_from_df(df, order=4, cluster_threshold=0.006)
            text = "Первый анализ уровней:\n"
            if supports:
                text += f"Поддержки: {', '.join([f'{s:.6f}' for s in supports[:3]])}\n"
            if resistances:
                text += f"Сопротивления: {', '.join([f'{r:.6f}' for r in resistances[:3]])}\n"
            if not supports and not resistances:
                text += "Не найдено явных уровней (нужно больше исторических экстремумов).\n"
            safe_send(m.chat.id, text)
        else:
            safe_send(m.chat.id, f"Не удалось загрузить данные для {state['pair']} [{tf}]")
    else:
        safe_send(m.chat.id, "Выбор таймфрейма доступен только в Manual или Levels режиме.")
        bot.send_message(m.chat.id, "Главное меню:", reply_markup=main_menu_kb())

@bot.message_handler(func=lambda m: m.text == "🔙 Назад")
def handle_back(m):
    if m.chat.id != USER_CHAT_ID: return
    safe_send(m.chat.id, "Возврат в главное меню")
    bot.send_message(m.chat.id, "Главное меню:", reply_markup=main_menu_kb())

# ---------------- schedule loop ----------------
def schedule_loop():
    # ensure auto-scan tags work
    while True:
        try:
            schedule.run_pending()
            time.sleep(1)
        except Exception as e:
            print("schedule_loop error:", e)
            time.sleep(3)

# ---------------- start ----------------
def start_bot():
    t = threading.Thread(target=schedule_loop, daemon=True)
    t.start()
    try:
        bot.polling(non_stop=True, timeout=60)
    except Exception as e:
        print("Polling error:", e)
        traceback.print_exc()

if __name__ == "__main__":
    print("Starting TA-bot (Manual/Auto/Scalp/Levels) ...")
    start_bot()
