import logging
import os

from dotenv import load_dotenv
from asyncChatGPT.asyncChatGPT import Chatbot as ChatGPT3Bot
from telegram_bot import ChatGPT3TelegramBot


def main():
    load_dotenv()

    logging.basicConfig(
        format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
        level=logging.INFO
    )

    chatgpt_config = {
        'email': os.environ['OPENAI_EMAIL'],
        'password': os.environ['OPENAI_PASSWORD'],
    }
    telegram_config = {
        'token': os.environ['TELEGRAM_BOT_TOKEN'],
        'allowed_user_ids': os.environ['ALLOWED_TELEGRAM_USER_IDS'].split(',')
    }

    gpt3_bot = ChatGPT3Bot(config=chatgpt_config, debug=True)
    telegram_bot = ChatGPT3TelegramBot(config=telegram_config, gpt3_bot=gpt3_bot)
    telegram_bot.run()


if __name__ == '__main__':
    main()
