import asyncio
import logging
import os
import re
from collections import defaultdict, deque
from threading import Lock
from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, filters
from telethon import TelegramClient, events

# Настройка логгирования
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# Конфигурация
BOT_TOKEN = "8067119413:AAFi1ltm2Igx0-36kv7Bkz-CJ1Bxpn7-i4k"
API_ID = 29899792
API_HASH = "d7cc22067d2840914fb39998023829c7"
TARGET_BOT = "@homoksjdfhbffb_bot"
SESSION_NAME = "proxy_bot_session"
RESPONSE_TIMEOUT = 40  # Таймаут ответа в секундах

WELCOME_MESSAGE = """
🔍 Введите запрос в одном из форматов:

⌕ Сидоров Иван Петрович 19.01.1975  
⌕ Сидоров Иван Петрович (1970-1975)  
⌕ Сидоров Иван Петрович (1975)  
⌕ Сидоров Иван Петрович  
⌕ Сидоров Иван  
⌕ Сидоров И П  
⌕ +79123456789  
⌕ name@mail.ru  
⌕ E777KX77 – номер авто (кириллицей)  
⌕ @name – никнейм (нужно добавить @ в начало)  
⌕ /id 137016 – Telegram ID  
⌕ /p 1234567890 – паспорт  
⌕ /s 12345678909 – СНИЛС  
⌕ /i 123456789091 – ИНН  
"""

class ProxyBot:
    def __init__(self):
        self.user_requests = {}
        self.request_queue = deque()
        self.pending_requests = defaultdict(list)
        self.client = None
        self.app = None
        self.lock = Lock()
        self.request_counter = 0

    def extract_key(self, text):
        text = text.strip()
        digits = re.sub(r"\D", "", text)

        if re.match(r"^(7|8)?9\d{9}$", digits):
            return '7' + digits[-10:]

        if re.match(r"^\d{10,12}$", digits):
            return digits

        if text.startswith("/i "):
            return text.split("/i ")[1].strip()

        if re.match(r"^[А-Яа-яA-Za-z]\d{3}[А-Яа-яA-Za-z]{2}\d{2,3}$", text.replace(" ", "")):
            return text.replace(" ", "").lower()

        fio_key = re.sub(r"[().\-]", "", text)
        fio_key = re.sub(r"\s+", "_", fio_key.strip()).lower()
        return fio_key

    async def start(self, update: Update, context):
        await update.message.reply_text(WELCOME_MESSAGE)

    async def handle_message(self, update: Update, context):
        user_id = update.message.chat_id
        user_message = update.message.text

        with self.lock:
            if user_id in self.user_requests:
                self.user_requests[user_id]["task"].cancel()

            timeout_task = asyncio.create_task(self.handle_response_timeout(user_id))
            request_key = self.extract_key(user_message)
            request_id = f"{user_id}_{self.request_counter}"
            self.request_counter += 1

            self.user_requests[user_id] = {
                "request": user_message,
                "task": timeout_task,
                "timestamp": asyncio.get_event_loop().time(),
                "request_id": request_id,
                "request_key": request_key
            }
            
            self.request_queue.append(user_id)
            self.pending_requests[request_key].append({
                "user_id": user_id,
                "original_request": user_message,
                "request_id": request_id
            })

        try:
            await self.client.send_message(TARGET_BOT, user_message)
            await update.message.reply_text("⌛ Запрос принят. Ожидайте ответ...")
        except Exception as e:
            logger.error(f"Ошибка отправки: {e}")
            await update.message.reply_text("⚠️ Ошибка при отправке запроса")
            self.cancel_timeout_task(user_id)
            with self.lock:
                try:
                    self.request_queue.remove(user_id)
                    self.remove_pending_request(user_id)
                except ValueError:
                    pass

    def remove_pending_request(self, user_id):
        with self.lock:
            keys_to_remove = []
            for key, requests in self.pending_requests.items():
                self.pending_requests[key] = [
                    r for r in requests if r["user_id"] != user_id
                ]
                if not self.pending_requests[key]:
                    keys_to_remove.append(key)
            
            for key in keys_to_remove:
                del self.pending_requests[key]

    async def handle_response_timeout(self, user_id):
        await asyncio.sleep(RESPONSE_TIMEOUT)

        with self.lock:
            if user_id in self.user_requests:
                try:
                    await self.app.bot.send_message(
                        chat_id=user_id,
                        text="👐 Ничего не найдено, попробуйте изменить запрос"
                    )
                except Exception as e:
                    logger.error(f"Ошибка отправки таймаута {user_id}: {e}")
                finally:
                    self.cancel_timeout_task(user_id)
                    try:
                        self.request_queue.remove(user_id)
                        self.remove_pending_request(user_id)
                    except ValueError:
                        pass

    def cancel_timeout_task(self, user_id):
        with self.lock:
            if user_id in self.user_requests:
                if "task" in self.user_requests[user_id]:
                    self.user_requests[user_id]["task"].cancel()
                del self.user_requests[user_id]

    async def handle_target_response(self, event):
        try:
            if "👇 Слишком много результатов" in event.message.text:
                with self.lock:
                    for user_id in list(self.request_queue):
                        try:
                            await self.app.bot.send_message(
                                chat_id=user_id,
                                text=event.message.text
                            )
                        except Exception as e:
                            logger.error(f"Ошибка отправки сообщения {user_id}: {e}")
                return

            if event.message.file:
                file_path = await event.message.download_media()
                filename = os.path.basename(file_path).lower()

                with self.lock:
                    for request_key, requests in list(self.pending_requests.items()):
                        key_variants = {
                            request_key,
                            request_key.replace("_", ""),
                            request_key.replace("_", " "),
                            request_key.replace("_", "-"),
                            " ".join(request_key.split("_")),
                            "-".join(request_key.split("_"))
                        }

                        for variant in key_variants:
                            if variant in filename:
                                best_match = None
                                max_score = 0
                                
                                for req in requests:
                                    score = sum(
                                        1 for word in req["original_request"].lower().split() 
                                        if word in filename
                                    )
                                    
                                    if score > max_score or (score == max_score and len(req["original_request"]) > len(best_match["original_request"])):
                                        max_score = score
                                        best_match = req
                                
                                if best_match:
                                    try:
                                        await self.app.bot.send_document(
                                            chat_id=best_match["user_id"],
                                            document=open(file_path, 'rb'),
                                            caption=f"📄 Результат по запросу:\n{best_match['original_request']}"
                                        )
                                        self.cancel_timeout_task(best_match["user_id"])
                                        self.remove_pending_request(best_match["user_id"])
                                        try:
                                            self.request_queue.remove(best_match["user_id"])
                                        except ValueError:
                                            pass
                                    except Exception as e:
                                        logger.error(f"Ошибка отправки файла {best_match['user_id']}: {e}")
                                    finally:
                                        if os.path.exists(file_path):
                                            os.remove(file_path)
                                break

            elif event.message.text:
                with self.lock:
                    for user_id, data in list(self.user_requests.items()):
                        if (data["request"].lower() == event.message.text.lower() or 
                            data["request"] in event.message.text):
                            try:
                                await self.app.bot.send_message(
                                    chat_id=user_id,
                                    text=event.message.text
                                )
                                self.cancel_timeout_task(user_id)
                                self.remove_pending_request(user_id)
                                try:
                                    self.request_queue.remove(user_id)
                                except ValueError:
                                    pass
                            except Exception as e:
                                logger.error(f"Ошибка отправки ответа {user_id}: {e}")
                            return

        except Exception as e:
            logger.error(f"Ошибка обработки ответа: {e}")

    async def cleanup_old_requests(self):
        while True:
            await asyncio.sleep(300)
            current_time = asyncio.get_event_loop().time()
            with self.lock:
                for user_id, data in list(self.user_requests.items()):
                    if current_time - data['timestamp'] > 1800:
                        self.cancel_timeout_task(user_id)
                        try:
                            self.request_queue.remove(user_id)
                            self.remove_pending_request(user_id)
                        except ValueError:
                            pass

    async def run(self):
        self.client = TelegramClient(SESSION_NAME, API_ID, API_HASH)
        await self.client.start()

        self.client.add_event_handler(
            self.handle_target_response,
            events.NewMessage(chats=TARGET_BOT)
        )

        self.app = Application.builder().token(BOT_TOKEN).build()
        self.app.add_handler(CommandHandler("start", self.start))
        self.app.add_handler(MessageHandler(filters.TEXT, self.handle_message))

        await self.app.initialize()
        await self.app.start()

        asyncio.create_task(self.cleanup_old_requests())

        try:
            await self.app.updater.start_polling()
            await self.client.run_until_disconnected()
        finally:
            await self.app.stop()
            await self.client.disconnect()

if __name__ == "__main__":
    bot = ProxyBot()
    try:
        asyncio.run(bot.run())
    except KeyboardInterrupt:
        logger.info("Бот остановлен")
    except Exception as e:
        logger.critical(f"Фатальная ошибка: {e}")