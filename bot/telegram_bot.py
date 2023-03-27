import logging
import os

import asyncio

import telegram
from telegram import constants
from telegram import Update, InlineQueryResultArticle, InputTextMessageContent, BotCommand
from telegram.error import RetryAfter, TimedOut
from telegram.ext import ApplicationBuilder, ContextTypes, CommandHandler, MessageHandler, \
    filters, InlineQueryHandler, Application

from pydub import AudioSegment
from openai_helper import OpenAIHelper
from usage_tracker import UsageTracker


class ChatGPTTelegramBot:
    """
    Class representing a ChatGPT Telegram Bot.
    """

    def __init__(self, config: dict, openai: OpenAIHelper):
        """
        Initializes the bot with the given configuration and GPT bot object.
        :param config: A dictionary containing the bot configuration
        :param openai: OpenAIHelper object
        """
        self.config = config
        self.openai = openai
        self.commands = [
            BotCommand(command='help', description='Show help message'),
            BotCommand(command='reset', description='Reset the conversation. Optionally pass high-level instructions '
                                                    '(e.g. /reset You are a helpful assistant)'),
            BotCommand(command='image', description='Generate image from prompt (e.g. /image cat)'),
            BotCommand(command='stats', description='Get your current usage statistics')
        ]
        self.disallowed_message = "Sorry, you are not allowed to use this bot. You can check out the source code at " \
                                  "https://github.com/n3d1117/chatgpt-telegram-bot"
        self.budget_limit_message = "Sorry, you have reached your monthly usage limit."
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

        logging.info(f'User {update.message.from_user.name} requested their usage statistics')
        
        user_id = update.message.from_user.id
        if user_id not in self.usage:
            self.usage[user_id] = UsageTracker(user_id, update.message.from_user.name)

        tokens_today, tokens_month = self.usage[user_id].get_current_token_usage()
        images_today, images_month = self.usage[user_id].get_current_image_count()
        transcribe_durations = self.usage[user_id].get_current_transcription_duration()
        cost_today, cost_month = self.usage[user_id].get_current_cost()
        
        chat_id = update.effective_chat.id
        chat_messages, chat_token_length = self.openai.get_conversation_stats(chat_id)
        budget = await self.get_remaining_budget(update)

        text_current_conversation = f"*Current conversation:*\n"+\
                     f"{chat_messages} chat messages in history.\n"+\
                     f"{chat_token_length} chat tokens in history.\n"+\
                     f"----------------------------\n"
        text_today = f"*Usage today:*\n"+\
                     f"{tokens_today} chat tokens used.\n"+\
                     f"{images_today} images generated.\n"+\
                     f"{transcribe_durations[0]} minutes and {transcribe_durations[1]} seconds transcribed.\n"+\
                     f"💰 For a total amount of ${cost_today:.2f}\n"+\
                     f"----------------------------\n"
        text_month = f"*Usage this month:*\n"+\
                     f"{tokens_month} chat tokens used.\n"+\
                     f"{images_month} images generated.\n"+\
                     f"{transcribe_durations[2]} minutes and {transcribe_durations[3]} seconds transcribed.\n"+\
                     f"💰 For a total amount of ${cost_month:.2f}"
        # text_budget filled with conditional content
        text_budget = "\n\n"
        if budget < float('inf'):
            text_budget += f"You have a remaining budget of ${budget:.2f} this month.\n"
        # add OpenAI account information for admin request
        if self.is_admin(update):
            grant_balance = self.openai.get_grant_balance()
            if grant_balance > 0.0:
                text_budget += f"Your remaining OpenAI grant balance is ${grant_balance:.2f}.\n"
            text_budget += f"Your OpenAI account was billed ${self.openai.get_billing_current_month():.2f} this month."
        
        usage_text = text_current_conversation + text_today + text_month + text_budget
        await update.message.reply_text(usage_text, parse_mode=constants.ParseMode.MARKDOWN)

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
        reset_content = update.message.text.replace('/reset', '').strip()
        self.openai.reset_chat_history(chat_id=chat_id, content=reset_content)
        await context.bot.send_message(chat_id=chat_id, text='Done!')

    async def image(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """
        Generates an image for the given prompt using DALL·E APIs
        """
        if not await self.is_allowed(update):
            logging.warning(f'User {update.message.from_user.name} is not allowed to generate images')
            await self.send_disallowed_message(update, context)
            return

        if not await self.is_within_budget(update):
            logging.warning(f'User {update.message.from_user.name} reached their usage limit')
            await self.send_budget_reached_message(update, context)
            return
        
        chat_id = update.effective_chat.id
        image_query = update.message.text.replace('/image', '').strip()
        if image_query == '':
            await context.bot.send_message(chat_id=chat_id, text='Please provide a prompt! (e.g. /image cat)')
            return

        logging.info(f'New image generation request received from user {update.message.from_user.name}')

        await context.bot.send_chat_action(chat_id=chat_id, action=constants.ChatAction.UPLOAD_PHOTO)
        try:
            image_url, image_size = await self.openai.generate_image(prompt=image_query)
            await context.bot.send_photo(
                chat_id=chat_id,
                reply_to_message_id=update.message.message_id,
                photo=image_url
            )
            # add image request to users usage tracker
            user_id = update.message.from_user.id
            self.usage[user_id].add_image_request(image_size, self.config['image_prices'])
            # add guest chat request to guest usage tracker
            if str(user_id) not in self.config['allowed_user_ids'].split(',') and 'guests' in self.usage:
                self.usage["guests"].add_image_request(image_size, self.config['image_prices'])

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
        
        if not await self.is_within_budget(update):
            logging.warning(f'User {update.message.from_user.name} reached their usage limit')
            await self.send_budget_reached_message(update, context)
            return

        if self.is_group_chat(update) and self.config['ignore_group_transcriptions']:
            logging.info(f'Transcription coming from group chat, ignoring...')
            return

        chat_id = update.effective_chat.id
        await context.bot.send_chat_action(chat_id=chat_id, action=constants.ChatAction.TYPING)
        filename = update.message.effective_attachment.file_unique_id
        filename_mp3 = f'{filename}.mp3'
        try:
            media_file = await context.bot.get_file(update.message.effective_attachment.file_id)
            await media_file.download_to_drive(filename)
        except Exception as e:
            logging.exception(e)
            await context.bot.send_message(
                chat_id=chat_id,
                reply_to_message_id=update.message.message_id,
                text=f'Failed to download audio file: {str(e)}. Make sure the file is not too large. (max 20MB)'
            )
            return

        # detect and extract audio from the attachment with pydub
        try:
            audio_track = AudioSegment.from_file(filename)
            audio_track.export(filename_mp3, format="mp3")
            logging.info(f'New transcribe request received from user {update.message.from_user.name}')

        except Exception as e:
            logging.exception(e)
            await context.bot.send_message(
                chat_id=update.effective_chat.id,
                reply_to_message_id=update.message.message_id,
                text='Unsupported file type'
            )
            if os.path.exists(filename):
                os.remove(filename)
            return

        filename_mp3 = f'{filename}.mp3'

        user_id = update.message.from_user.id
        if user_id not in self.usage:
            self.usage[user_id] = UsageTracker(user_id, update.message.from_user.name)

        # send decoded audio to openai
        try:

            # Transcribe the audio file
            transcript = await self.openai.transcribe(filename_mp3)

            # add transcription seconds to usage tracker
            transcription_price = self.config['transcription_price']
            self.usage[user_id].add_transcription_seconds(audio_track.duration_seconds, transcription_price)

            # add guest chat request to guest usage tracker
            allowed_user_ids = self.config['allowed_user_ids'].split(',')
            if str(user_id) not in allowed_user_ids and 'guests' in self.usage:
                self.usage["guests"].add_transcription_seconds(audio_track.duration_seconds, transcription_price)

            if self.config['voice_reply_transcript']:

                # Split into chunks of 4096 characters (Telegram's message limit)
                transcript_output = f'_Transcript:_\n"{transcript}"'
                chunks = self.split_into_chunks(transcript_output)

                for index, transcript_chunk in enumerate(chunks):
                    await context.bot.send_message(
                        chat_id=chat_id,
                        reply_to_message_id=update.message.message_id if index == 0 else None,
                        text=transcript_chunk,
                        parse_mode=constants.ParseMode.MARKDOWN
                    )
            else:
                # Get the response of the transcript
                response, total_tokens = await self.openai.get_chat_response(chat_id=chat_id, query=transcript)

                # add chat request to users usage tracker
                self.usage[user_id].add_chat_tokens(total_tokens, self.config['token_price'])
                # add guest chat request to guest usage tracker
                if str(user_id) not in allowed_user_ids and 'guests' in self.usage:
                    self.usage["guests"].add_chat_tokens(total_tokens, self.config['token_price'])

                # Split into chunks of 4096 characters (Telegram's message limit)
                transcript_output = f'_Transcript:_\n"{transcript}"\n\n_Answer:_\n{response}'
                chunks = self.split_into_chunks(transcript_output)

                for index, transcript_chunk in enumerate(chunks):
                    await context.bot.send_message(
                        chat_id=chat_id,
                        reply_to_message_id=update.message.message_id if index == 0 else None,
                        text=transcript_chunk,
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

        if not await self.is_within_budget(update):
            logging.warning(f'User {update.message.from_user.name} reached their usage limit')
            await self.send_budget_reached_message(update, context)
            return
        
        logging.info(f'New message received from user {update.message.from_user.name}')
        chat_id = update.effective_chat.id
        user_id = update.message.from_user.id
        prompt = update.message.text

        if self.is_group_chat(update):
            trigger_keyword = self.config['group_trigger_keyword']
            if prompt.startswith(trigger_keyword):
                prompt = prompt[len(trigger_keyword):].strip()
            else:
                if update.message.reply_to_message and update.message.reply_to_message.from_user.id == context.bot.id:
                    logging.info('Message is a reply to the bot, allowing...')
                else:
                    logging.warning('Message does not start with trigger keyword, ignoring...')
                    return

        await context.bot.send_chat_action(chat_id=chat_id, action=constants.ChatAction.TYPING)

        try:
            if self.config['stream']:
                is_group_chat = self.is_group_chat(update)

                stream_response = self.openai.get_chat_response_stream(chat_id=chat_id, query=prompt)
                i = 0
                prev = ''
                sent_message = None
                backoff = 0
                chunk = 0

                async for content, tokens in stream_response:
                    if len(content.strip()) == 0:
                        continue

                    chunks = self.split_into_chunks(content)
                    if len(chunks) > 1:
                        content = chunks[-1]
                        if chunk != len(chunks) - 1:
                            chunk += 1
                            try:
                                await self.edit_message_with_retry(context, chat_id, sent_message.message_id, chunks[-2])
                            except:
                                pass
                            try:
                                sent_message = await context.bot.send_message(
                                    chat_id=sent_message.chat_id,
                                    text=content if len(content) > 0 else "..."
                                )
                            except:
                                pass
                            continue

                    if is_group_chat:
                        # group chats have stricter flood limits
                        cutoff = 180 if len(content) > 1000 else 120 if len(content) > 200 else 90 if len(content) > 50 else 50
                    else:
                        cutoff = 90 if len(content) > 1000 else 45 if len(content) > 200 else 25 if len(content) > 50 else 15

                    cutoff += backoff

                    if i == 0:
                        try:
                            if sent_message is not None:
                                await context.bot.delete_message(chat_id=sent_message.chat_id,
                                                                 message_id=sent_message.message_id)
                            sent_message = await context.bot.send_message(
                                chat_id=chat_id,
                                reply_to_message_id=update.message.message_id,
                                text=content
                            )
                        except:
                            continue

                    elif abs(len(content) - len(prev)) > cutoff or tokens != 'not_finished':
                        prev = content

                        try:
                            await self.edit_message_with_retry(context, chat_id, sent_message.message_id, content)

                        except RetryAfter as e:
                            backoff += 5
                            await asyncio.sleep(e.retry_after)
                            continue

                        except TimedOut:
                            backoff += 5
                            await asyncio.sleep(0.5)
                            continue

                        except Exception:
                            backoff += 5
                            continue

                        await asyncio.sleep(0.01)

                    i += 1
                    if tokens != 'not_finished':
                        total_tokens = int(tokens)

            else:
                response, total_tokens = await self.openai.get_chat_response(chat_id=chat_id, query=prompt)

                # Split into chunks of 4096 characters (Telegram's message limit)
                chunks = self.split_into_chunks(response)

                for index, chunk in enumerate(chunks):
                    await context.bot.send_message(
                        chat_id=chat_id,
                        reply_to_message_id=update.message.message_id if index == 0 else None,
                        text=chunk,
                        parse_mode=constants.ParseMode.MARKDOWN
                    )

            try:
                # add chat request to users usage tracker
                self.usage[user_id].add_chat_tokens(total_tokens, self.config['token_price'])
                # add guest chat request to guest usage tracker
                allowed_user_ids = self.config['allowed_user_ids'].split(',')
                if str(user_id) not in allowed_user_ids and 'guests' in self.usage:
                    self.usage["guests"].add_chat_tokens(total_tokens, self.config['token_price'])
            except:
                pass

        except Exception as e:
            logging.exception(e)
            await context.bot.send_message(
                chat_id=chat_id,
                reply_to_message_id=update.message.message_id,
                text=f'Failed to get response: {str(e)}'
            )

    async def inline_query(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """
        Handle the inline query. This is run when you type: @botusername <query>
        """
        query = update.inline_query.query

        if query == '':
            return

        results = [
            InlineQueryResultArticle(
                id=query,
                title='Ask ChatGPT',
                input_message_content=InputTextMessageContent(query),
                description=query,
                thumb_url='https://user-images.githubusercontent.com/11541888/223106202-7576ff11-2c8e-408d-94ea-b02a7a32149a.png'
            )
        ]

        await update.inline_query.answer(results)

    async def edit_message_with_retry(self, context: ContextTypes.DEFAULT_TYPE, chat_id: int, message_id: int, text: str):
        """
        Edit a message with retry logic in case of failure (e.g. broken markdown)
        :param context: The context to use
        :param chat_id: The chat id to edit the message in
        :param message_id: The message id to edit
        :param text: The text to edit the message with
        :return: None
        """
        try:
            await context.bot.edit_message_text(
                chat_id=chat_id,
                message_id=message_id,
                text=text,
                parse_mode=constants.ParseMode.MARKDOWN
            )
        except telegram.error.BadRequest as e:
            if str(e).startswith("Message is not modified"):
                return
            try:
                await context.bot.edit_message_text(
                    chat_id=chat_id,
                    message_id=message_id,
                    text=text
                )
            except Exception as e:
                logging.warning(f'Failed to edit message: {str(e)}')
                raise e

        except Exception as e:
            logging.warning(str(e))
            raise e

    async def send_disallowed_message(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """
        Sends the disallowed message to the user.
        """
        await context.bot.send_message(
            chat_id=update.effective_chat.id,
            text=self.disallowed_message,
            disable_web_page_preview=True
        )

    async def send_budget_reached_message(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """
        Sends the budget reached message to the user.
        """
        await context.bot.send_message(
            chat_id=update.effective_chat.id,
            text=self.budget_limit_message
        )

    async def error_handler(self, update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
        """
        Handles errors in the telegram-python-bot library.
        """
        logging.error(f'Exception while handling an update: {context.error}')

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
        
        if self.is_admin(update):
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

    def is_admin(self, update: Update) -> bool:
        """
        Checks if the user is the admin of the bot.
        The first user in the user list is the admin.
        """
        if self.config['admin_user_ids'] == '-':
            logging.info('No admin user defined.')
            return False

        admin_user_ids = self.config['admin_user_ids'].split(',')

        # Check if user is in the admin user list
        if str(update.message.from_user.id) in admin_user_ids:
            return True

        return False

    async def get_remaining_budget(self, update: Update) -> float:
        user_id = update.message.from_user.id
        if user_id not in self.usage:
            self.usage[user_id] = UsageTracker(user_id, update.message.from_user.name)

        if self.is_admin(update):
            return float('inf')

        if self.config['monthly_user_budgets'] == '*':
            return float('inf')

        allowed_user_ids = self.config['allowed_user_ids'].split(',')
        if str(user_id) in allowed_user_ids:
            # find budget for allowed user
            user_index = allowed_user_ids.index(str(user_id))
            user_budgets = self.config['monthly_user_budgets'].split(',')
            # check if user is included in budgets list
            if len(user_budgets) <= user_index:
                logging.warning(f'No budget set for user: {update.message.from_user.name} ({user_id}).')
                return 0.0
            user_budget = float(user_budgets[user_index])
            cost_month = self.usage[user_id].get_current_cost()[1]
            remaining_budget = user_budget - cost_month
            return remaining_budget
        else:
            return 0.0

    async def is_within_budget(self, update: Update) -> bool:
        """
        Checks if the user reached their monthly usage limit.
        Initializes UsageTracker for user and guest when needed.
        """
        user_id = update.message.from_user.id
        if user_id not in self.usage:
            self.usage[user_id] = UsageTracker(user_id, update.message.from_user.name)

        if self.is_admin(update):
            return True

        if self.config['monthly_user_budgets'] == '*':
            return True

        allowed_user_ids = self.config['allowed_user_ids'].split(',')
        if str(user_id) in allowed_user_ids:
            # find budget for allowed user
            user_index = allowed_user_ids.index(str(user_id))
            user_budgets = self.config['monthly_user_budgets'].split(',')
            # check if user is included in budgets list
            if len(user_budgets) <= user_index:
                logging.warning(f'No budget set for user: {update.message.from_user.name} ({user_id}).')
                return False
            user_budget = float(user_budgets[user_index])
            cost_month = self.usage[user_id].get_current_cost()[1]
            # Check if allowed user is within budget
            return user_budget > cost_month

        # Check if group member is within budget
        if self.is_group_chat(update):
            for user in allowed_user_ids:
                if await self.is_user_in_group(update, user):
                    if 'guests' not in self.usage:
                        self.usage['guests'] = UsageTracker('guests', 'all guest users in group chats')
                    if self.config['monthly_guest_budget'] >= self.usage['guests'].get_current_cost()[1]:
                        return True
                    logging.warning('Monthly guest budget for group chats used up.')
                    return False
            logging.info(f'Group chat messages from user {update.message.from_user.name} are not allowed')
        return False

    def split_into_chunks(self, text: str, chunk_size: int = 4096) -> list[str]:
        """
        Splits a string into chunks of a given size.
        """
        return [text[i:i + chunk_size] for i in range(0, len(text), chunk_size)]

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
            .concurrent_updates(True) \
            .build()

        application.add_handler(CommandHandler('reset', self.reset))
        application.add_handler(CommandHandler('help', self.help))
        application.add_handler(CommandHandler('image', self.image))
        application.add_handler(CommandHandler('start', self.help))
        application.add_handler(CommandHandler('stats', self.stats))
        application.add_handler(MessageHandler(
            filters.AUDIO | filters.VOICE | filters.Document.AUDIO |
            filters.VIDEO | filters.VIDEO_NOTE | filters.Document.VIDEO,
            self.transcribe))
        application.add_handler(MessageHandler(filters.TEXT & (~filters.COMMAND), self.prompt))
        application.add_handler(InlineQueryHandler(self.inline_query, chat_types=[
            constants.ChatType.GROUP, constants.ChatType.SUPERGROUP
        ]))

        application.add_error_handler(self.error_handler)

        application.run_polling()
