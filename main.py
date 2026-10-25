from __future__ import annotations

import asyncio
import logging
import os
import re
import tempfile
from pathlib import Path
from urllib.parse import urlsplit

import yt_dlp
from telegram import Update
from telegram.constants import ChatAction
from telegram.error import InvalidToken, TelegramError
from telegram.ext import Application, CommandHandler, ContextTypes, MessageHandler, filters

logger = logging.getLogger("telegram_video_bot")
logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
# httpx logs full request URLs at INFO level; Telegram Bot API URLs contain the
# bot token, so keep request URLs out of workflow logs.
logging.getLogger("httpx").setLevel(logging.CRITICAL)
logging.getLogger("httpcore").setLevel(logging.CRITICAL)
logging.getLogger("telegram").setLevel(logging.CRITICAL)

MAX_UPLOAD_BYTES = 48 * 1024 * 1024
URL_PATTERN = re.compile(r"https?://[^\s<>\"']+", re.IGNORECASE)
TRAILING_PUNCTUATION = ".,!?;:)]}"

ALLOWED_HOSTS = {
    "instagram.com",
    "www.instagram.com",
    "m.instagram.com",
    "tiktok.com",
    "www.tiktok.com",
    "vm.tiktok.com",
    "vt.tiktok.com",
    "youtube.com",
    "www.youtube.com",
    "m.youtube.com",
    "youtu.be",
}


class UnsupportedLinkError(ValueError):
    """The submitted URL is not one of the supported video link types."""


class VideoTooLargeError(ValueError):
    """The downloaded file exceeds Telegram's upload limit."""


class VideoFormatError(ValueError):
    """No compatible video file was produced."""


def get_supported_url(text: str) -> str:
    match = URL_PATTERN.search(text)
    if not match:
        raise UnsupportedLinkError("No link found.")

    url = match.group(0).rstrip(TRAILING_PUNCTUATION)
    try:
        parsed = urlsplit(url)
        host = (parsed.hostname or "").lower()
    except ValueError as error:
        raise UnsupportedLinkError("Invalid URL.") from error

    if parsed.scheme.lower() != "https" or host not in ALLOWED_HOSTS:
        raise UnsupportedLinkError("Unsupported URL.")

    if parsed.username or parsed.password or not parsed.path:
        raise UnsupportedLinkError("Invalid URL.")

    path = parsed.path.lower()
    if host.endswith("instagram.com") and not (
        path.startswith("/reel/") or path.startswith("/reels/")
    ):
        raise UnsupportedLinkError("Not an Instagram Reel URL.")

    if host.endswith("youtube.com") and not path.startswith("/shorts/"):
        raise UnsupportedLinkError("Not a YouTube Shorts URL.")

    return url


def download_video(url: str, download_dir: Path) -> Path:
    options = {
        "format": "best[ext=mp4]/best",
        "outtmpl": str(download_dir / "%(id)s.%(ext)s"),
        "noplaylist": True,
        "max_filesize": MAX_UPLOAD_BYTES,
        "quiet": True,
        "no_warnings": True,
        "retries": 2,
        "fragment_retries": 2,
        "socket_timeout": 30,
    }

    with yt_dlp.YoutubeDL(options) as downloader:
        downloader.download([url])

    files = [
        path
        for path in download_dir.iterdir()
        if path.is_file() and path.suffix.lower() in {".mp4", ".m4v", ".mov", ".webm", ".mkv"}
    ]
    if not files:
        raise VideoFormatError("No video file was downloaded.")

    video_path = max(files, key=lambda path: path.stat().st_size)
    if video_path.stat().st_size > MAX_UPLOAD_BYTES:
        raise VideoTooLargeError("The video is too large for Telegram.")

    return video_path


async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    del context
    if update.message:
        await update.message.reply_text(
            "Пришлите ссылку на Instagram Reel, TikTok или YouTube Shorts. "
            "Бот отправит видео в этот чат."
        )


async def handle_link(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.message
    if not message or not message.text:
        return

    try:
        url = get_supported_url(message.text)
    except UnsupportedLinkError:
        await message.reply_text(
            "Не удалось распознать ссылку. Отправьте ссылку на Instagram Reel, "
            "TikTok или YouTube Shorts."
        )
        return

    status = await message.reply_text("Скачиваю видео…")
    try:
        await context.bot.send_chat_action(
            chat_id=message.chat_id,
            action=ChatAction.UPLOAD_VIDEO,
        )
        with tempfile.TemporaryDirectory(prefix="telegram-video-") as temp_dir:
            video_path = await asyncio.to_thread(download_video, url, Path(temp_dir))
            await status.edit_text("Скачано. Отправляю видео…")
            with video_path.open("rb") as video_file:
                await context.bot.send_video(
                    chat_id=message.chat_id,
                    video=video_file,
                    supports_streaming=True,
                    read_timeout=120,
                    write_timeout=120,
                    connect_timeout=30,
                    pool_timeout=30,
                )
        try:
            await status.delete()
        except TelegramError:
            logger.debug("Could not remove the completed status message.")
    except VideoTooLargeError:
        await status.edit_text(
            "Это видео слишком большое для отправки в Telegram. "
            "Попробуйте ссылку на видео меньшего размера."
        )
    except VideoFormatError:
        await status.edit_text(
            "Не удалось получить совместимый видеофайл по этой ссылке. "
            "Попробуйте другую ссылку."
        )
    except yt_dlp.utils.DownloadError as error:
        if "max-filesize" in str(error).lower() or "max filesize" in str(error).lower():
            await status.edit_text(
                "Это видео слишком большое для отправки в Telegram. "
                "Попробуйте ссылку на видео меньшего размера."
            )
            return
        logger.info("yt-dlp could not download a submitted video.")
        await status.edit_text(
            "Не удалось скачать видео. Проверьте, что ссылка открыта и видео доступно "
            "без входа в аккаунт, затем попробуйте снова."
        )
    except (OSError, asyncio.TimeoutError, TelegramError):
        logger.warning("A temporary-file or Telegram transfer error occurred.")
        await status.edit_text(
            "Не получилось отправить видео. Попробуйте ещё раз позже или выберите "
            "видео меньшего размера."
        )
    except Exception as error:
        logger.error("Unexpected processing error (%s).", type(error).__name__)
        await status.edit_text(
            "Произошла непредвиденная ошибка при обработке видео. Попробуйте позже."
        )


def main() -> None:
    token = os.getenv("BOT_TOKEN")
    if not token:
        raise RuntimeError(
            "BOT_TOKEN is not set. Add it in Replit Secrets before starting the bot."
        )

    try:
        application = (
            Application.builder()
            .token(token)
            .read_timeout(30)
            .write_timeout(120)
            .connect_timeout(30)
            .pool_timeout(30)
            .build()
        )
        application.add_handler(CommandHandler("start", start_command))
        application.add_handler(
            MessageHandler(filters.TEXT & ~filters.COMMAND, handle_link)
        )

        logger.info("Telegram video bot is starting.")
        application.run_polling(allowed_updates=Update.ALL_TYPES)
    except InvalidToken:
        logger.error(
            "Telegram rejected BOT_TOKEN. Add a valid, newly issued token in Replit Secrets."
        )
    except Exception as error:
        logger.error("The bot stopped unexpectedly (%s).", type(error).__name__)


if __name__ == "__main__":
    main()
