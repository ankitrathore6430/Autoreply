import os
import sys
import asyncio
import logging
import time
from datetime import datetime, timedelta, timezone
from telethon import TelegramClient, events
from telethon.tl.types import PeerUser
from flask import Flask, jsonify
from waitress import serve

# --- Setup logging ---
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
)
logger = logging.getLogger("tg-auto-reply")

# --- Configuration ---
# WARNING: Hardcoding keys is insecure and not recommended.
API_ID = 1778606
API_HASH = "d2bdbdd125a7e1d83fdc27c51f3791c4"
BOT_TOKEN = "8470442409:AAGUwWjdcBDQwRVzRebyste_tV_S6WUxxE0"
ADMIN_IDS_STR = "745211839".split(',')
ADMIN_IDS = {int(admin_id) for admin_id in ADMIN_IDS_STR if admin_id}
SESSION_NAME = "bot.session"

# --- Bot settings ---
AUTO_REPLY_TEXT = (
    "Hey! I'm currently away and using an auto-reply. "
    "I'll get back to you as soon as I can.\n\n"
    "— This is an automated message."
)
AUTO_REPLY_MEDIA_INFO = None
REPLY_COOLDOWN_S = 1
BROADCAST_DELAY_S = 30
last_replied: dict[int, float] = {}

# File paths and in-memory sets for different user lists
AUTO_REPLY_USERS_FILE = "auto-reply.txt"
ALL_USERS_FILE = "users.txt"
auto_replied_users: set[int] = set()
all_fetched_users: set[int] = set()

# Global state for auto-reply, bulk messaging, and fetching
AUTO_REPLY_ENABLED = True
is_bulk_messaging = False
is_fetching_users = False

# --- Flask Web Server Setup ---
app = Flask(__name__)

@app.route('/')
def home():
    return jsonify({
        "status": "ok",
        "message": "Bot is running.",
        "uptime": str(datetime.now() - bot_start_time) if 'bot_start_time' in globals() else "N/A"
    }), 200

async def run_webserver():
    loop = asyncio.get_running_loop()
    await loop.run_in_executor(
        None,
        lambda: serve(app, host='0.0.0.0', port=5000)
    )

def human(dt: float) -> str:
    return datetime.fromtimestamp(dt).strftime("%Y-%m-%d %H:%M:%S")

def load_user_list(filename: str, user_set: set[int]) -> None:
    """Loads user IDs from a specified file into a specified set."""
    if os.path.exists(filename):
        with open(filename, "r") as f:
            for line in f:
                try:
                    user_id = int(line.strip())
                    user_set.add(user_id)
                except ValueError:
                    continue
    logger.info(f"Loaded {len(user_set)} users from {filename}.")

def save_user_id(user_id: int, filename: str, user_set: set[int]) -> None:
    """Saves a single user ID to the specified file if it's not already there."""
    if user_id not in user_set:
        with open(filename, "a") as f:
            f.write(f"{user_id}\n")
        user_set.add(user_id)
        logger.info(f"Saved new user_id {user_id} to file {filename}.")

async def main() -> None:
    global bot_start_time
    bot_start_time = datetime.now()

    if not all([API_ID, API_HASH, BOT_TOKEN]):
        logger.error("API_ID, API_HASH, or BOT_TOKEN is missing in the configuration.")
        return

    client = TelegramClient(SESSION_NAME, API_ID, API_HASH)
    load_user_list(AUTO_REPLY_USERS_FILE, auto_replied_users)
    load_user_list(ALL_USERS_FILE, all_fetched_users)

    # --- INCOMING MESSAGE HANDLER (for auto-replies) ---
    @client.on(events.NewMessage(incoming=True))
    async def handler(event: events.NewMessage.Event) -> None:
        global AUTO_REPLY_ENABLED
        if not AUTO_REPLY_ENABLED:
            return

        if not event.is_private:
            return

        sender = await event.get_sender()
        if sender and sender.bot:
            return

        uid = event.sender_id
        now = time.time()
        
        # Save user ID to the auto-reply list
        save_user_id(uid, AUTO_REPLY_USERS_FILE, auto_replied_users)

        if now - last_replied.get(uid, 0) < REPLY_COOLDOWN_S:
            logger.debug("Cooldown active for user %s", uid)
            return

        try:
            if AUTO_REPLY_MEDIA_INFO:
                original_msg = await client.get_messages(
                    AUTO_REPLY_MEDIA_INFO['chat_id'],
                    ids=AUTO_REPLY_MEDIA_INFO['message_id']
                )
                if original_msg and original_msg.media:
                    await client.send_file(
                        event.chat_id,
                        original_msg.media,
                        caption=AUTO_REPLY_MEDIA_INFO.get('caption', ''),
                        parse_mode='md'
                    )
            elif AUTO_REPLY_TEXT:
                await event.reply(AUTO_REPLY_TEXT, parse_mode='md')
            
            last_replied[uid] = now
            logger.info("Auto-replied to user_id=%s", uid)
        except Exception as e:
            logger.exception("Failed to send auto-reply: %s", e)

    # --- COMMAND HANDLERS ---

    @client.on(events.NewMessage(pattern="/command"))
    async def command_handler(event: events.NewMessage.Event) -> None:
        if not event.is_private or event.sender_id not in ADMIN_IDS:
            return
        
        chat_id = event.chat_id
        try:
            await event.delete()
        except Exception as e:
            logger.warning(f"Could not delete command message: {e}")
        
        help_message = (
            "Hello! Here are the commands you can use:\n\n"
            "**✍️ Change Reply Message**\n"
            "`/setreply <your new message>`\n"
            "*(Or reply to a photo, video, or document with /setreply and an optional new caption)*\n\n"
            "**⏱️ Change Cooldown Time**\n"
            "`/setcooldown <time_in_seconds>`\n"
            "*Example:* `/setcooldown 600` (for 10 minutes)\n\n"
            "**📢 Bulk Message**\n"
            "`/bulkmsg <your message>` - Sends a message to ALL users (both lists).\n"
            "`/bulkmsg bot <message>` - Sends a message to users in `auto-reply.txt`.\n"
            "`/bulkmsg all <message>` - Sends a message to users in `users.txt`.\n"
            "`/bulkmsg stop` - Stops an ongoing bulk message.\n"
            "*(Or reply to a photo/video/document with /bulkmsg and an optional new caption)*\n\n"
            "**⏱️ Change Bulk Message Delay**\n"
            "`/setbulkmsgdelay <time_in_seconds>`\n"
            "*Example:* `/setbulkmsgdelay 30`\n\n"
            "**📥 Fetch Users**\n"
            "`/fetchusers` - Fetches all users into `users.txt`.\n"
            "`/fetchusers bot` - Fetches only users the bot has auto-replied to into `auto-reply.txt`.\n"
            "`/fetchusers last <number>h` - Fetches users active in the last X hours into `users.txt`.\n"
            "`/fetchusers last <number>d` - Fetches users active in the last X days into `users.txt`.\n"
            "`/fetchusers last <number>m` - Fetches users active in the last X months into `users.txt`.\n"
            "`/fetchusers stop` - Stops an ongoing fetch.\n\n"
            "**🗑️ Remove Saved Users**\n"
            "`/removefetchusers` - Deletes the `users.txt` file.\n"
            "`/removeautoreplyusers` - Deletes the `auto-reply.txt` file.\n\n"
            "**⏺️ Auto-reply Control**\n"
            "`/stopreply` - Stops the bot's auto-reply function.\n"
            "`/restartreply` - Restarts the bot's auto-reply function."
        )
        await client.send_message(chat_id, help_message, parse_mode='md')

    @client.on(events.NewMessage(pattern=r"(?s)/setreply(?: |$)(.*)"))
    async def set_reply_handler(event: events.NewMessage.Event) -> None:
        if not event.is_private or event.sender_id not in ADMIN_IDS:
            return
        
        chat_id = event.chat_id
        try:
            await event.delete()
        except Exception as e:
            logger.warning(f"Could not delete command message: {e}")

        global AUTO_REPLY_TEXT, AUTO_REPLY_MEDIA_INFO
        
        replied_msg = await event.get_reply_message()
        new_caption = event.pattern_match.group(1).strip()

        if replied_msg and replied_msg.media:
            final_caption = new_caption if new_caption else replied_msg.text

            AUTO_REPLY_MEDIA_INFO = {
                'chat_id': replied_msg.chat_id,
                'message_id': replied_msg.id,
                'caption': final_caption
            }
            AUTO_REPLY_TEXT = None
            logger.info("Auto-reply updated to a media message.")
            await client.send_message(chat_id, "✅ **Auto-reply updated to the specified media and caption.**", parse_mode='md')
        
        elif new_caption:
            AUTO_REPLY_TEXT = new_caption
            AUTO_REPLY_MEDIA_INFO = None
            logger.info("Auto-reply message changed to: %s", new_caption)
            await client.send_message(chat_id, f"✅ **Auto-reply message updated to:**\n\n{new_caption}", parse_mode='md')
        else:
            await client.send_message(chat_id, 
                "⚠️ **Usage:**\n"
                "• `/setreply <your new text message>`\n"
                "• Or, reply to a photo/video/document with `/setreply` and an optional new caption.",
                parse_mode='md'
            )

    @client.on(events.NewMessage(pattern=r"/setcooldown(?: |$)(.*)"))
    async def set_cooldown_handler(event: events.NewMessage.Event) -> None:
        if not event.is_private or event.sender_id not in ADMIN_IDS:
            return

        chat_id = event.chat_id
        try:
            await event.delete()
        except Exception as e:
            logger.warning(f"Could not delete command message: {e}")

        global REPLY_COOLDOWN_S
        input_text = event.pattern_match.group(1).strip()

        if input_text and input_text.isdigit():
            new_cooldown_seconds = int(input_text)
            REPLY_COOLDOWN_S = new_cooldown_seconds
            logger.info(f"Cooldown time changed to {new_cooldown_seconds} seconds.")
            await client.send_message(chat_id, f"✅ **Cooldown time updated to:** {new_cooldown_seconds} seconds.", parse_mode='md')
        else:
            current_cooldown = REPLY_COOLDOWN_S
            await client.send_message(chat_id,
                f"⚠️ **Usage:** `/setcooldown <time_in_seconds>`\n\n"
                f"Example: `/setcooldown 600` (for 10 minutes)\n"
                f"The current cooldown is {current_cooldown} seconds.",
                parse_mode='md'
            )

    @client.on(events.NewMessage(pattern=r"(?s)/bulkmsg(?: |$)(.*)"))
    async def bulk_message_handler(event: events.NewMessage.Event) -> None:
        if not event.is_private or event.sender_id not in ADMIN_IDS:
            return

        chat_id = event.chat_id
        try:
            await event.delete()
        except Exception as e:
            logger.warning(f"Could not delete command message: {e}")

        global is_bulk_messaging
        replied_msg = await event.get_reply_message()
        command_text = event.pattern_match.group(1).strip()
        users_to_message = []
        message_to_send = ""
        
        # --- Command parsing logic ---
        parts = command_text.split(None, 1)
        subcommand = parts[0].lower() if parts else ""
        
        if subcommand == "stop":
            if is_bulk_messaging:
                is_bulk_messaging = False
                await client.send_message(chat_id, "⏸️ **Bulk message will be stopped after the current message is sent.**", parse_mode='md')
                logger.info("Bulk message stop command received.")
            else:
                await client.send_message(chat_id, "⚠️ **No active bulk message to stop.**", parse_mode='md')
            return

        if is_bulk_messaging:
            await client.send_message(chat_id, "⚠️ **A bulk message is already in progress.** Please wait for it to finish or send `/bulkmsg stop` to stop it.", parse_mode='md')
            return

        if not subcommand and command_text:
            # Default case: /bulkmsg <message> -> Send to all users
            message_to_send = command_text
            users_to_message = list(all_fetched_users.union(auto_replied_users))
            await client.send_message(chat_id, f"📢 **Sending bulk message to {len(users_to_message)} users from both lists...**", parse_mode='md')
        elif subcommand == "bot":
            # /bulkmsg bot <message> -> Send to auto-reply users
            message_to_send = parts[1] if len(parts) > 1 else ""
            users_to_message = list(auto_replied_users.copy())
            await client.send_message(chat_id, f"📢 **Sending bulk message to {len(users_to_message)} users from `auto-reply.txt`...**", parse_mode='md')
        elif subcommand == "all":
            # /bulkmsg all <message> -> Send to all fetched users
            message_to_send = parts[1] if len(parts) > 1 else ""
            users_to_message = list(all_fetched_users.copy())
            await client.send_message(chat_id, f"📢 **Sending bulk message to {len(users_to_message)} users from `users.txt`...**", parse_mode='md')
        else:
            await client.send_message(chat_id, "⚠️ **Invalid bulk message command format.**", parse_mode='md')
            return
        
        if not users_to_message:
            await client.send_message(chat_id, "⚠️ **No users to bulk message to.**", parse_mode='md')
            return

        if not message_to_send and not replied_msg:
             await client.send_message(chat_id, "⚠️ **Usage:** Provide a message or reply to a media file.", parse_mode='md')
             return

        is_bulk_messaging = True
        
        success_count = 0
        fail_count = 0

        for user_id in users_to_message:
            if not is_bulk_messaging:
                logger.info("Bulk message was stopped by admin.")
                break
            try:
                if replied_msg and replied_msg.media:
                    await client.send_file(
                        user_id,
                        replied_msg.media,
                        caption=message_to_send if message_to_send else replied_msg.text,
                        parse_mode='md'
                    )
                else:
                    await client.send_message(user_id, message_to_send, parse_mode='md')
                success_count += 1
                await asyncio.sleep(BROADCAST_DELAY_S)
            except Exception as e:
                logger.error(f"Failed to bulk message to user {user_id}: {e}")
                fail_count += 1
        
        is_bulk_messaging = False
        status_message = f"✅ **Bulk message complete.**\n\n" if success_count + fail_count == len(users_to_message) else f"❌ **Bulk message stopped.**\n\n"
        await client.send_message(chat_id, status_message +
                           f"**Success:** {success_count}\n"
                           f"**Failed:** {fail_count}", parse_mode='md')

    @client.on(events.NewMessage(pattern=r"/setbulkmsgdelay(?: |$)(.*)"))
    async def set_bulk_message_delay_handler(event: events.NewMessage.Event) -> None:
        if not event.is_private or event.sender_id not in ADMIN_IDS:
            return

        chat_id = event.chat_id
        try:
            await event.delete()
        except Exception as e:
            logger.warning(f"Could not delete command message: {e}")

        global BROADCAST_DELAY_S
        input_text = event.pattern_match.group(1).strip()

        if input_text and input_text.isdigit():
            new_delay_seconds = int(input_text)
            BROADCAST_DELAY_S = new_delay_seconds
            logger.info(f"Bulk message delay time changed to {new_delay_seconds} seconds.")
            await client.send_message(chat_id, f"✅ **Bulk message delay updated to:** {new_delay_seconds} seconds.", parse_mode='md')
        else:
            current_delay = BROADCAST_DELAY_S
            await client.send_message(chat_id,
                f"⚠️ **Usage:** `/setbulkmsgdelay <time_in_seconds>`\n\n"
                f"Example: `/setbulkmsgdelay 30`\n"
                f"The current bulk message delay is {current_delay} seconds.",
                parse_mode='md'
            )

    @client.on(events.NewMessage(pattern=r"(?s)/fetchusers(?: |$)(.*)"))
    async def fetchusers_handler(event: events.NewMessage.Event) -> None:
        if not event.is_private or event.sender_id not in ADMIN_IDS:
            return

        chat_id = event.chat_id
        try:
            await event.delete()
        except Exception as e:
            logger.warning(f"Could not delete command message: {e}")

        global is_fetching_users
        command_text = event.pattern_match.group(1).strip().lower()

        if command_text == "stop":
            if is_fetching_users:
                is_fetching_users = False
                await client.send_message(chat_id, "⏸️ **User fetching will stop after the current dialog is processed.**", parse_mode='md')
                logger.info("User fetching stop command received.")
            else:
                await client.send_message(chat_id, "⚠️ **No active user fetching process to stop.**", parse_mode='md')
            return

        if is_fetching_users:
            await client.send_message(chat_id, "⚠️ **A user fetching process is already in progress.** Please wait for it to finish or send `/fetchusers stop`.", parse_mode='md')
            return

        cutoff_time = None
        target_file = ALL_USERS_FILE
        target_set = all_fetched_users
        info_message = "🔄 **Starting to fetch all user IDs into `users.txt`...** This might take some time."
        
        if command_text == "bot":
            target_file = AUTO_REPLY_USERS_FILE
            target_set = auto_replied_users
            info_message = "🔄 **Fetching only users that have received an auto-reply from this bot into `auto-reply.txt`...**"
        
        elif command_text:
            parts = command_text.split()
            if len(parts) == 2 and parts[0] == "last":
                value_str = parts[1][:-1]
                unit = parts[1][-1]
                if value_str.isdigit():
                    value = int(value_str)
                    if unit == 'h':
                        cutoff_time = datetime.now(timezone.utc) - timedelta(hours=value)
                        info_message = f"🔄 **Starting to fetch user IDs active in the last {value} hours into `users.txt`...**"
                    elif unit == 'd':
                        cutoff_time = datetime.now(timezone.utc) - timedelta(days=value)
                        info_message = f"🔄 **Starting to fetch user IDs active in the last {value} days into `users.txt`...**"
                    elif unit == 'm':
                        # A month is approximated as 30 days
                        cutoff_time = datetime.now(timezone.utc) - timedelta(days=value * 30)
                        info_message = f"🔄 **Starting to fetch user IDs active in the last {value} months into `users.txt`...**"
                    else:
                        await client.send_message(chat_id, "⚠️ **Invalid time unit.** Use 'h' for hours, 'd' for days, or 'm' for months. Example: `/fetchusers last 24h`", parse_mode='md')
                        return
                else:
                    await client.send_message(chat_id, "⚠️ **Invalid time value.** Example: `/fetchusers last 24h`", parse_mode='md')
                    return
            else:
                await client.send_message(chat_id,
                    "⚠️ **Invalid command format.** Use `/fetchusers` to fetch all, `/fetchusers last <time>` to filter, or `/fetchusers stop`.",
                    parse_mode='md'
                )
                return

        await client.send_message(chat_id, info_message, parse_mode='md')
        
        is_fetching_users = True
        fetched_count = 0
        target_set.clear()

        async for dialog in client.iter_dialogs():
            if not is_fetching_users:
                logger.info("User fetching was stopped by admin.")
                break
            
            # Apply filter only if cutoff_time is set
            if dialog.is_user and (cutoff_time is None or dialog.date > cutoff_time):
                user_id = dialog.entity.id
                if user_id not in target_set:
                    save_user_id(user_id, target_file, target_set)
                    fetched_count += 1
                    
        is_fetching_users = False
        
        status_message = f"✅ **User fetching complete.**\n" if not is_fetching_users else f"❌ **User fetching stopped.**\n"
        await client.send_message(chat_id, status_message +
                           f"Found and saved **{fetched_count}** new user IDs.\n"
                           f"Total users stored: **{len(target_set)}**.", parse_mode='md')

    @client.on(events.NewMessage(pattern="/removefetchusers"))
    async def remove_fetchusers_handler(event: events.NewMessage.Event) -> None:
        if not event.is_private or event.sender_id not in ADMIN_IDS:
            return
        
        chat_id = event.chat_id
        try:
            await event.delete()
        except Exception as e:
            logger.warning(f"Could not delete command message: {e}")
            
        if os.path.exists(ALL_USERS_FILE):
            os.remove(ALL_USERS_FILE)
            all_fetched_users.clear()
            logger.info(f"{ALL_USERS_FILE} file removed and in-memory user list cleared.")
            await client.send_message(chat_id, f"✅ **All saved user IDs have been removed from `{ALL_USERS_FILE}`.**", parse_mode='md')
        else:
            await client.send_message(chat_id, f"⚠️ **No saved user IDs file (`{ALL_USERS_FILE}`) found to remove.**", parse_mode='md')

    @client.on(events.NewMessage(pattern="/removeautoreplyusers"))
    async def remove_autoreply_users_handler(event: events.NewMessage.Event) -> None:
        if not event.is_private or event.sender_id not in ADMIN_IDS:
            return
        
        chat_id = event.chat_id
        try:
            await event.delete()
        except Exception as e:
            logger.warning(f"Could not delete command message: {e}")
            
        if os.path.exists(AUTO_REPLY_USERS_FILE):
            os.remove(AUTO_REPLY_USERS_FILE)
            auto_replied_users.clear()
            logger.info(f"{AUTO_REPLY_USERS_FILE} file removed and in-memory user list cleared.")
            await client.send_message(chat_id, f"✅ **All saved user IDs have been removed from `{AUTO_REPLY_USERS_FILE}`.**", parse_mode='md')
        else:
            await client.send_message(chat_id, f"⚠️ **No saved user IDs file (`{AUTO_REPLY_USERS_FILE}`) found to remove.**", parse_mode='md')
    
    @client.on(events.NewMessage(pattern="/stopreply"))
    async def stopreply_handler(event: events.NewMessage.Event) -> None:
        if not event.is_private or event.sender_id not in ADMIN_IDS:
            return
        
        chat_id = event.chat_id
        try:
            await event.delete()
        except Exception as e:
            logger.warning(f"Could not delete command message: {e}")
            
        global AUTO_REPLY_ENABLED
        AUTO_REPLY_ENABLED = False
        await client.send_message(chat_id, "⏸️ **Auto-reply has been stopped.**", parse_mode='md')
        logger.info("Auto-reply stopped by admin.")

    @client.on(events.NewMessage(pattern="/restartreply"))
    async def restartreply_handler(event: events.NewMessage.Event) -> None:
        if not event.is_private or event.sender_id not in ADMIN_IDS:
            return
        
        chat_id = event.chat_id
        try:
            await event.delete()
        except Exception as e:
            logger.warning(f"Could not delete command message: {e}")
            
        global AUTO_REPLY_ENABLED
        AUTO_REPLY_ENABLED = True
        await client.send_message(chat_id, "▶️ **Auto-reply has been restarted.**", parse_mode='md')
        logger.info("Auto-reply restarted by admin.")

    logger.info("Starting bot...")
    await client.start(bot_token=BOT_TOKEN)

    me = await client.get_me()
    ADMIN_IDS.add(me.id)

    logger.info(f"Bot is active as '{me.first_name}'. ID: {me.id}")
    logger.info(f"Authorized Admins: {ADMIN_IDS}")
    logger.info("Send /command to see available commands.")

    await asyncio.gather(
        client.run_until_disconnected(),
        run_webserver()
    )

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Bot stopped by user.")