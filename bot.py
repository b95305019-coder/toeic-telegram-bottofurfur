import os
import json
import re
import logging
import time
import threading
import urllib.request
from http.server import HTTPServer, BaseHTTPRequestHandler
from telegram import Update, ReplyKeyboardMarkup, ReplyKeyboardRemove
from telegram.ext import (
    Application, CommandHandler, MessageHandler,
    filters, ContextTypes, ConversationHandler
)
import gspread
from google.oauth2.service_account import Credentials

# ── 設定 ──────────────────────────────────────────
BOT_TOKEN = os.environ.get("BOT_TOKEN")
CHAT_ID = int(os.environ.get("CHAT_ID", "7672912526"))
SHEETS_ID = "1XopniplcnUMrojQ8AAemBLUp_WRIXIrAN2G-Kfr5vu8"
SHEET_NAME = "工作表1"

SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets.readonly",
    "https://www.googleapis.com/auth/drive.readonly"
]

ANSWERING = 1
# ─────────────────────────────────────────────────

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


def get_sheets_client():
    import base64
    creds_json = os.environ.get("GOOGLE_CREDENTIALS")
    if not creds_json:
        raise ValueError("GOOGLE_CREDENTIALS 環境變數未設定")
    creds_str = creds_json.strip()
    creds_dict = None
    try:
        decoded = base64.b64decode(creds_str).decode("utf-8")
        creds_dict = json.loads(decoded)
    except Exception:
        pass
    if creds_dict is None:
        try:
            creds_dict = json.loads(creds_str)
        except Exception:
            pass
    if creds_dict and "private_key" in creds_dict:
        creds_dict["private_key"] = creds_dict["private_key"].replace("\\n", "\n")
    if creds_dict is None:
        raise ValueError("無法解析 GOOGLE_CREDENTIALS")
    creds = Credentials.from_service_account_info(creds_dict, scopes=SCOPES)
    return gspread.authorize(creds)


def parse_json_data(raw):
    if not raw:
        return None
    try:
        clean = re.sub(r"```json|```", "", raw).strip()
        return json.loads(clean)
    except Exception as e:
        logger.warning(f"JSON 解析失敗: {e}")
        return None


def get_latest_articles(count=4):
    import traceback
    try:
        client = get_sheets_client()
    except Exception as e:
        raise Exception(f"get_sheets_client failed: {type(e).__name__}: {e}\n{traceback.format_exc()}")
    try:
        sheet = client.open_by_url(
            f"https://docs.google.com/spreadsheets/d/{SHEETS_ID}/edit"
        ).worksheet(SHEET_NAME)
    except Exception as e:
        raise Exception(f"open_by_url failed: {type(e).__name__}: {e}\n{traceback.format_exc()}")
    try:
        rows = sheet.get_all_records()
    except Exception as e:
        raise Exception(f"get_all_records failed: {type(e).__name__}: {e}\n{traceback.format_exc()}")

    valid = [r for r in rows if r.get("JSON_Data", "").strip()]
    latest = valid[-count:] if len(valid) >= count else valid
    latest.reverse()

    articles = []
    for row in latest:
        parsed = parse_json_data(row["JSON_Data"])
        if parsed:
            articles.append({
                "title": parsed.get("title", ""),
                "contentEn": parsed.get("contentEn", ""),
                "questions": parsed.get("questions", []),
                "date": row.get("Date", ""),
            })
    return articles


def format_question(q_idx, q, total):
    letters = ["A", "B", "C", "D"]
    lines = [f"📝 *第 {q_idx+1} 題（共 {total} 題）*\n"]
    lines.append(f"{q['q']}\n")
    for i, opt in enumerate(q["options"]):
        lines.append(f"({letters[i]}) {opt}")
    return "\n".join(lines)


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "👋 哈囉！我是你的多益練習 Bot！\n\n"
        "指令列表：\n"
        "📚 /quiz - 開始今日測驗\n"
        "📰 /articles - 查看今日文章列表\n"
        "❓ /help - 說明"
    )


async def articles_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("⏳ 載入文章中...")
    try:
        articles = get_latest_articles(4)
        if not articles:
            await update.message.reply_text("目前還沒有文章，請稍後再試！")
            return
        lines = ["📰 *今日文章列表*\n"]
        for i, a in enumerate(articles, 1):
            lines.append(f"{i}. {a['title']}")
        lines.append("\n輸入 /quiz 開始測驗！")
        await update.message.reply_text("\n".join(lines), parse_mode="Markdown")
    except Exception as e:
        logger.error(f"articles_command error: {e}")
        await update.message.reply_text(f"❌ 載入失敗：{e}")


async def send_article_content(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """傳送文章內容供閱讀"""
    articles = context.user_data["articles"]
    idx = context.user_data["current_article_idx"]
    article = articles[idx]

    # 傳送文章標題
    await update.message.reply_text(
        f"📰 *文章 {idx+1}/{len(articles)}*\n\n*{article['title']}*",
        parse_mode="Markdown",
        reply_markup=ReplyKeyboardRemove()
    )

    # 傳送文章內容（分段避免超過 4096 字元限制）
    content_en = article.get("contentEn", "")
    if content_en:
        chunks = [content_en[i:i+3000] for i in range(0, len(content_en), 3000)]
        for chunk in chunks:
            await update.message.reply_text(chunk)
    else:
        await update.message.reply_text("（無文章內容）")

    # 計算這篇文章有幾題
    q_count = sum(
        1 for q in context.user_data["questions"]
        if q["article_title"] == article["title"]
    )

    keyboard = [["✅ 開始答題"]]
    reply_markup = ReplyKeyboardMarkup(keyboard, one_time_keyboard=True, resize_keyboard=True)
    await update.message.reply_text(
        f"📖 閱讀完畢後，按下方按鈕開始回答這篇的 *{q_count} 題*！",
        parse_mode="Markdown",
        reply_markup=reply_markup
    )


async def quiz_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("⏳ 載入題目中...")
    try:
        articles = get_latest_articles(4)
        if not articles:
            await update.message.reply_text("目前還沒有文章，請稍後再試！")
            return ConversationHandler.END

        all_questions = []
        for a in articles:
            for q in a.get("questions", []):
                all_questions.append({
                    "article_title": a["title"],
                    "q": q["q"],
                    "options": q["options"],
                    "ans": q["ans"],
                })

        if not all_questions:
            await update.message.reply_text("找不到題目，請稍後再試！")
            return ConversationHandler.END

        # 儲存狀態
        context.user_data["articles"] = articles
        context.user_data["questions"] = all_questions
        context.user_data["current"] = 0
        context.user_data["correct"] = 0
        context.user_data["total"] = len(all_questions)
        context.user_data["current_article_idx"] = 0

        await update.message.reply_text(
            f"🎯 今日共 *{len(all_questions)} 題*，來自 {len(articles)} 篇文章！\n\n"
            "流程：先閱讀文章 → 按「✅ 開始答題」→ 回答題目\n"
            "輸入 /stop 可以中途結束。",
            parse_mode="Markdown"
        )

        # 先顯示第一篇文章
        await send_article_content(update, context)
        return ANSWERING

    except Exception as e:
        import traceback
        logger.error(f"quiz_start error: {e}\n{traceback.format_exc()}")
        await update.message.reply_text(f"❌ 載入失敗：{type(e).__name__}: {e}")
        return ConversationHandler.END


async def send_question(update: Update, context: ContextTypes.DEFAULT_TYPE):
    questions = context.user_data["questions"]
    current = context.user_data["current"]
    total = context.user_data["total"]
    q = questions[current]
    msg = format_question(current, q, total)
    keyboard = [["A", "B"], ["C", "D"]]
    reply_markup = ReplyKeyboardMarkup(keyboard, one_time_keyboard=True, resize_keyboard=True)
    await update.message.reply_text(msg, parse_mode="Markdown", reply_markup=reply_markup)


async def handle_answer(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_input = update.message.text.strip()
    letters = ["A", "B", "C", "D"]

    # 處理「開始答題」按鈕
    if user_input == "✅ 開始答題":
        await send_question(update, context)
        return ANSWERING

    user_input = user_input.upper()
    if user_input not in letters:
        await update.message.reply_text("請輸入 A、B、C 或 D！")
        return ANSWERING

    questions = context.user_data["questions"]
    current = context.user_data["current"]
    q = questions[current]
    correct_idx = q["ans"]
    user_idx = letters.index(user_input)

    if user_idx == correct_idx:
        context.user_data["correct"] += 1
        feedback = "✅ *正確！*"
    else:
        correct_letter = letters[correct_idx]
        feedback = f"❌ *錯誤！*\n正確答案是 ({correct_letter}) {q['options'][correct_idx]}"

    await update.message.reply_text(feedback, parse_mode="Markdown")

    context.user_data["current"] += 1
    next_idx = context.user_data["current"]

    if next_idx >= context.user_data["total"]:
        return await quiz_end(update, context)

    # 檢查是否換到新文章
    prev_title = questions[next_idx - 1]["article_title"]
    next_title = questions[next_idx]["article_title"]

    if prev_title != next_title:
        context.user_data["current_article_idx"] += 1
        await send_article_content(update, context)
    else:
        await send_question(update, context)

    return ANSWERING


async def quiz_end(update: Update, context: ContextTypes.DEFAULT_TYPE):
    correct = context.user_data["correct"]
    total = context.user_data["total"]
    pct = correct / total if total > 0 else 0

    if pct == 1.0:
        emoji, msg = "🎉", "滿分！太厲害了！"
    elif pct >= 0.75:
        emoji, msg = "🌟", "表現很好！繼續保持！"
    elif pct >= 0.5:
        emoji, msg = "👍", "不錯喔！再接再厲！"
    else:
        emoji, msg = "💪", "繼續加油！明天再挑戰！"

    await update.message.reply_text(
        f"{emoji} *測驗完成！*\n\n"
        f"答對：{correct} / {total} 題\n"
        f"正確率：{pct*100:.0f}%\n\n"
        f"{msg}\n\n"
        f"明天見！輸入 /quiz 可以再次練習。",
        parse_mode="Markdown",
        reply_markup=ReplyKeyboardRemove()
    )
    return ConversationHandler.END


async def stop_quiz(update: Update, context: ContextTypes.DEFAULT_TYPE):
    correct = context.user_data.get("correct", 0)
    current = context.user_data.get("current", 0)
    await update.message.reply_text(
        f"已停止測驗。\n答對 {correct} / {current} 題。\n輸入 /quiz 可重新開始！",
        reply_markup=ReplyKeyboardRemove()
    )
    return ConversationHandler.END


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "📖 *使用說明*\n\n"
        "/quiz - 開始今日測驗（先閱讀文章再答題）\n"
        "/articles - 查看今日文章標題\n"
        "/stop - 中途停止測驗\n"
        "/help - 顯示此說明\n\n"
        "每天早上 6 點會自動更新新文章！",
        parse_mode="Markdown"
    )


# Bot 啟動時間，用來判斷是否健康
_bot_start_time = None
_bot_alive = False


def start_health_server():
    """啟動 HTTP 健康檢查伺服器 + 定時 self-ping，避免 Render spin down"""
    class HealthHandler(BaseHTTPRequestHandler):
        def do_GET(self):
            if _bot_alive:
                self.send_response(200)
                self.end_headers()
                self.wfile.write(b"OK")
            else:
                self.send_response(503)
                self.end_headers()
                self.wfile.write(b"Bot not running")

        def do_HEAD(self):
            # UptimeRobot 用 HEAD 請求檢查，必須支援
            if _bot_alive:
                self.send_response(200)
                self.end_headers()
            else:
                self.send_response(503)
                self.end_headers()

        def log_message(self, format, *args):
            pass  # 靜音 log

    port = int(os.environ.get("PORT", 8080))
    server = HTTPServer(("0.0.0.0", port), HealthHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    logger.info(f"健康檢查伺服器啟動於 port {port}")

    # Self-ping：每 4 分鐘 ping 自己，防止 Render 因閒置 spin down
    render_url = os.environ.get("RENDER_EXTERNAL_URL", "")
    if render_url:
        def self_ping():
            while True:
                time.sleep(240)  # 4 分鐘
                try:
                    urllib.request.urlopen(f"{render_url}/", timeout=10)
                    logger.info("Self-ping OK")
                except Exception as e:
                    logger.warning(f"Self-ping 失敗（可忽略）: {e}")
        ping_thread = threading.Thread(target=self_ping, daemon=True)
        ping_thread.start()
        logger.info(f"Self-ping 啟動，目標：{render_url}")
    else:
        logger.warning("RENDER_EXTERNAL_URL 未設定，Self-ping 未啟動")


def clear_old_connections():
    try:
        url = f"https://api.telegram.org/bot{BOT_TOKEN}/deleteWebhook?drop_pending_updates=true"
        urllib.request.urlopen(url, timeout=10)
        logger.info("舊連線已清除")
    except Exception as e:
        logger.warning(f"清除連線時發生錯誤（可忽略）: {e}")


def main():
    global _bot_alive
    start_health_server()
    clear_old_connections()
    time.sleep(3)

    app = (
        Application.builder()
        .token(BOT_TOKEN)
        .connect_timeout(30)
        .read_timeout(30)
        .write_timeout(30)
        .build()
    )

    conv_handler = ConversationHandler(
        entry_points=[CommandHandler("quiz", quiz_start)],
        states={
            ANSWERING: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, handle_answer),
                CommandHandler("stop", stop_quiz),
            ],
        },
        fallbacks=[CommandHandler("stop", stop_quiz)],
    )

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(CommandHandler("articles", articles_command))
    app.add_handler(conv_handler)

    logger.info("Bot 啟動中...")
    _bot_alive = True
    app.run_polling(
        drop_pending_updates=True,
        allowed_updates=["message"],
    )


if __name__ == "__main__":
    main()
