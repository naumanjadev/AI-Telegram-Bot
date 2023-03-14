import logging
import os

from telegram import constants
from telegram import Update, InlineQueryResultArticle, InputTextMessageContent, BotCommand
from telegram.ext import ApplicationBuilder, ContextTypes, CommandHandler, MessageHandler, \
    filters, InlineQueryHandler, Application

from pydub import AudioSegment
from openai_helper import OpenAIHelper
from usage_tracker import UsageTracker


class ChatGPT3TelegramBot:
    """
    Class representing a Chat-GPT3 Telegram Bot.
    """

    def __init__(self, config: dict, openai: OpenAIHelper):
        """
        Initializes the bot with the given configuration and GPT-3 bot object.
        :param config: A dictionary containing the bot configuration
        :param openai: OpenAIHelper object
        """
        self.config = config
        self.openai = openai
        self.commands = [
            BotCommand(command='help', description='Show this help message'),
            BotCommand(command='reset', description='Reset the conversation'),
            BotCommand(command='image', description='Generate image from prompt (e.g. /image cat)'),
            BotCommand(command='stats', description='Get your current usage statistics')
        ]
        self.disallowed_message = "Sorry, you are not allowed to use this bot. You can check out the source code at " \
                                  "https://github.com/n3d1117/chatgpt-telegram-bot"
        self.usage = {}

    async def help(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """
        Shows the help menu.
        """
        commands = [f'/{command.command} - {command.description}' for command in self.commands]
        help_text = 'I\'m a ChatGPT bot, talk to me!' + \
                    '\n\n' + \
                    '\n'.join(commands) + \
                    '\n\n' + \
                    'Send me a voice message or file and I\'ll transcribe it for you!' + \
                    '\n\n' + \
                    "Open source at https://github.com/n3d1117/chatgpt-telegram-bot"
        await update.message.reply_text(help_text, disable_web_page_preview=True)


    async def stats(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """
        Returns token usage statistics for current day and month.
        """
        if not await self.is_allowed(update):
            logging.warning(f'User {update.message.from_user.name} is not allowed to request their usage statistics')
            await self.send_disallowed_message(update, context)
            return

        logging.info(f'User {update.message.from_user.name} requested their token usage statistics')
        
        user_id = update.message.from_user.id
        if user_id not in self.usage:
            self.usage[user_id] = UsageTracker(user_id, update.message.from_user.name)

        tokens_today, tokens_month = self.usage[user_id].get_token_usage()
        images_today, images_month = self.usage[user_id].get_image_count()
        transcribe_durations = self.usage[user_id].get_transcription_duration()
        cost_today, cost_month = self.usage[user_id].get_current_cost()
        
        usage_text = f"Today:\n"+\
                     f"{tokens_today} chat tokens used.\n"+\
                     f"{images_today} images generated.\n"+\
                     f"{transcribe_durations[0]} minutes and {transcribe_durations[1]} seconds transcribed.\n"+\
                     f"💰 For a total amount of ${cost_today}.\n"+\
                     f"\n----------------------------\n\n"+\
                     f"This month:\n"+\
                     f"{tokens_month} chat tokens used.\n"+\
                     f"{images_month} images generated.\n"+\
                     f"{transcribe_durations[2]} minutes and {transcribe_durations[3]} seconds transcribed.\n"+\
                     f"💰 For a total amount of ${cost_month}."
        await update.message.reply_text(usage_text)

    async def reset(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """
        Resets the conversation.
        """
        if not await self.is_allowed(update):
            logging.warning(f'User {update.message.from_user.name} is not allowed to reset the conversation')
            await self.send_disallowed_message(update, context)
            return

        logging.info(f'Resetting the conversation for user {update.message.from_user.name}...')

        chat_id = update.effective_chat.id
        self.openai.reset_chat_history(chat_id=chat_id)
        await context.bot.send_message(chat_id=chat_id, text='Done!')

    async def image(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """
        Generates an image for the given prompt using DALL·E APIs
        """
        if not await self.is_allowed(update):
            logging.warning(f'User {update.message.from_user.name} is not allowed to generate images')
            await self.send_disallowed_message(update, context)
            return

        chat_id = update.effective_chat.id
        image_query = update.message.text.replace('/image', '').strip()
        if image_query == '':
            await context.bot.send_message(chat_id=chat_id, text='Please provide a prompt! (e.g. /image cat)')
            return

        logging.info(f'New image generation request received from user {update.message.from_user.name}')

        await context.bot.send_chat_action(chat_id=chat_id, action=constants.ChatAction.UPLOAD_PHOTO)
        try:
            image_url, image_size = self.openai.generate_image(prompt=image_query)
            await context.bot.send_photo(
                chat_id=chat_id,
                reply_to_message_id=update.message.message_id,
                photo=image_url
            )
            # add image request to usage tracker
            user_id = update.message.from_user.id
            if user_id not in self.usage:
                self.usage[user_id] = UsageTracker(user_id, update.message.from_user.name)
            self.usage[user_id].add_image_request(image_size, self.config['image_prices'])

        except Exception as e:
            logging.exception(e)
            await context.bot.send_message(
                chat_id=chat_id,
                reply_to_message_id=update.message.message_id,
                text=f'Failed to generate image: {str(e)}'
            )

    async def transcribe(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """
        Transcribe audio messages.
        """
        if not await self.is_allowed(update):
            logging.warning(f'User {update.message.from_user.name} is not allowed to transcribe audio messages')
            await self.send_disallowed_message(update, context)
            return

        logging.info(f'New transcribe request received from user {update.message.from_user.name}')

        chat_id = update.effective_chat.id
        await context.bot.send_chat_action(chat_id=chat_id, action=constants.ChatAction.TYPING)

        if update.message.voice:
            filename = update.message.voice.file_unique_id
        elif update.message.audio:
            filename = update.message.audio.file_unique_id
        elif update.message.video:
            filename = update.message.video.file_unique_id
        else:
            await context.bot.send_message(
                chat_id=update.effective_chat.id,
                reply_to_message_id=update.message.message_id,
                text='Unsupported file type'
            )
            return

        filename_mp3 = f'{filename}.mp3'

        user_id = update.message.from_user.id
        if user_id not in self.usage:
            self.usage[user_id] = UsageTracker(user_id, update.message.from_user.name)

        try:
            if update.message.voice:
                media_file = await context.bot.get_file(update.message.voice.file_id)

            elif update.message.audio:
                media_file = await context.bot.get_file(update.message.audio.file_id)

            elif update.message.video:
                media_file = await context.bot.get_file(update.message.video.file_id)

            await media_file.download_to_drive(filename)
            
            audio_track = AudioSegment.from_file(filename)
            audio_track.export(filename_mp3, format="mp3")

            # Transcribe the audio file
            transcript = self.openai.transcribe(filename_mp3)
            # add transcription seconds to usage tracker
            self.usage[user_id].add_transcription_seconds(audio_track.duration_seconds, self.config['transcription_price'])
            if self.config['voice_reply_transcript']:
                # Send the transcript
                await context.bot.send_message(
                    chat_id=chat_id,
                    reply_to_message_id=update.message.message_id,
                    text=f'_Transcript:_\n"{transcript}"',
                    parse_mode=constants.ParseMode.MARKDOWN
                )
            else:
                # Send the response of the transcript
                response, total_tokens = self.openai.get_chat_response(chat_id=chat_id, query=transcript)
                # add chat request to usage tracker
                self.usage[user_id].add_chat_tokens(total_tokens, self.config['token_price'])
                await context.bot.send_message(
                    chat_id=chat_id,
                    reply_to_message_id=update.message.message_id,
                    text=f'_Transcript:_\n"{transcript}"\n\n_Answer:_\n{response}',
                    parse_mode=constants.ParseMode.MARKDOWN
                )
        except Exception as e:
            logging.exception(e)
            await context.bot.send_message(
                chat_id=chat_id,
                reply_to_message_id=update.message.message_id,
                text=f'Failed to transcribe text: {str(e)}'
            )
        finally:
            # Cleanup files
            if os.path.exists(filename_mp3):
                os.remove(filename_mp3)
            if os.path.exists(filename):
                os.remove(filename)

    async def prompt(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """
        React to incoming messages and respond accordingly.
        """
        if not await self.is_allowed(update):
            logging.warning(f'User {update.message.from_user.name} is not allowed to use the bot')
            await self.send_disallowed_message(update, context)
            return

        logging.info(f'New message received from user {update.message.from_user.name}')
        chat_id = update.effective_chat.id
        prompt = update.message.text

        if self.is_group_chat(update):
            trigger_keyword = self.config['group_trigger_keyword']
            if prompt.startswith(trigger_keyword):
                prompt = prompt[len(trigger_keyword):].strip()
            else:
                logging.warning(f'Message does not start with trigger keyword, ignoring...')
                return

        await context.bot.send_chat_action(chat_id=chat_id, action=constants.ChatAction.TYPING)
        response, total_tokens = self.openai.get_chat_response(chat_id=chat_id, query=prompt)

        # add chat request to usage tracker
        user_id = update.message.from_user.id
        if user_id not in self.usage:
            self.usage[user_id] = UsageTracker(user_id, update.message.from_user.name)
        self.usage[user_id].add_chat_tokens(total_tokens, self.config['token_price'])

        await context.bot.send_message(
            chat_id=chat_id,
            reply_to_message_id=update.message.message_id,
            text=response,
            parse_mode=constants.ParseMode.MARKDOWN
        )

    async def inline_query(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """
        Handle the inline query. This is run when you type: @botusername <query>
        """
        query = update.inline_query.query

        if query == "":
            return

        results = [
            InlineQueryResultArticle(
                id=query,
                title="Ask ChatGPT",
                input_message_content=InputTextMessageContent(query),
                description=query,
                thumb_url='https://user-images.githubusercontent.com/11541888/223106202-7576ff11-2c8e-408d-94ea-b02a7a32149a.png'
            )
        ]

        await update.inline_query.answer(results)

    async def send_disallowed_message(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """
        Sends the disallowed message to the user.
        """
        await context.bot.send_message(
            chat_id=update.effective_chat.id,
            text=self.disallowed_message,
            disable_web_page_preview=True
        )

    async def error_handler(self, update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
        """
        Handles errors in the telegram-python-bot library.
        """
        logging.debug(f'Exception while handling an update: {context.error}')

    def is_group_chat(self, update: Update) -> bool:
        """
        Checks if the message was sent from a group chat
        """
        return update.effective_chat.type in [
            constants.ChatType.GROUP,
            constants.ChatType.SUPERGROUP
        ]

    async def is_user_in_group(self, update: Update, user_id: int) -> bool:
        """
        Checks if user_id is a member of the group
        """
        member = await update.effective_chat.get_member(user_id)
        return member.status in [
            constants.ChatMemberStatus.OWNER,
            constants.ChatMemberStatus.ADMINISTRATOR,
            constants.ChatMemberStatus.MEMBER
        ]

    async def is_allowed(self, update: Update) -> bool:
        """
        Checks if the user is allowed to use the bot.
        """
        if self.config['allowed_user_ids'] == '*':
            return True

        allowed_user_ids = self.config['allowed_user_ids'].split(',')

        # Check if user is allowed
        if str(update.message.from_user.id) in allowed_user_ids:
            return True

        # Check if it's a group a chat with at least one authorized member
        if self.is_group_chat(update):
            for user in allowed_user_ids:
                if await self.is_user_in_group(update, user):
                    logging.info(f'{user} is a member. Allowing group chat message...')
                    return True
            logging.info(f'Group chat messages from user {update.message.from_user.name} are not allowed')

        return False

    async def post_init(self, application: Application) -> None:
        """
        Post initialization hook for the bot.
        """
        await application.bot.set_my_commands(self.commands)

    def run(self):
        """
        Runs the bot indefinitely until the user presses Ctrl+C
        """
        application = ApplicationBuilder() \
            .token(self.config['token']) \
            .proxy_url(self.config['proxy']) \
            .get_updates_proxy_url(self.config['proxy']) \
            .post_init(self.post_init) \
            .build()

        application.add_handler(CommandHandler('reset', self.reset))
        application.add_handler(CommandHandler('help', self.help))
        application.add_handler(CommandHandler('image', self.image))
        application.add_handler(CommandHandler('start', self.help))
        application.add_handler(CommandHandler('stats', self.stats))
        application.add_handler(MessageHandler(filters.VOICE | filters.AUDIO | filters.VIDEO, self.transcribe))
        application.add_handler(MessageHandler(filters.TEXT & (~filters.COMMAND), self.prompt))
        application.add_handler(InlineQueryHandler(self.inline_query, chat_types=[
            constants.ChatType.GROUP, constants.ChatType.SUPERGROUP
        ]))

        application.add_error_handler(self.error_handler)

        application.run_polling()
