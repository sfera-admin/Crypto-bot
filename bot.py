import os
import telebot

BOT_TOKEN = os.getenv("BOT_TOKEN")
if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN env var is missing")

bot = telebot.TeleBot(BOT_TOKEN, parse_mode="HTML")

@bot.message_handler(commands=['start'])
def start(m):
    bot.reply_to(m, "Привет 👋 Бот запущен. Напиши /price")

@bot.message_handler(commands=['price'])
def price(m):
    bot.reply_to(m, "Тест: бот отвечает — всё ок ✅")

if __name__ == "__main__":
    print("Bot polling...")
    bot.infinity_polling()
