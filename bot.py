import os
import json
import re
import logging
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
SHEETS_ID = "1XopniplcnUMrojQ8AAemBLUp_WRlXlrAN2G-Kfr5vu8"
SHEET_NAME = "工作表1"

# Google Sheets 認證
SCOPES = ["https://www.googleapis.com/auth/spreadsheets.readonly"]

# ConversationHandler 狀態
ANSWERING = 1
# ─────────────────────────────────────────────────

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


def get_sheets_client():
    """取得 Google Sheets 客戶端"""
    creds_json = os.environ.get("GOOGLE_CREDENTIALS")
    if not creds_json:
        raise ValueError("GOOGLE_CREDENTIALS 環境變數未設定")
    creds_dict = json.loads(creds_json)
    creds = Credentials.from_service_account_info(creds_dict, scopes=SCOPES)
    return gspread.authorize(creds)


def parse_json_data(raw):
    """解析 JSON_Data 欄位"""
    if not raw:
        return None
    try:
        clean = re.sub(r"```json|```", "", raw).strip()
        return json.loads(clean)
    except Exception as e:
        logger.warning(f"JSON 解析失敗: {e}")
        return None


def get_latest_articles(count=4):
    """從 Google Sheets 取得最新幾篇文章"""
    client = get_sheets_client()
    sheet = client.open_by_key(SHEETS_ID).worksheet(SHEET_NAME)
    rows = sheet.get_all_records()

    # 過濾有 JSON_Data 的行，取最新幾篇
    valid = [r for r in rows if r.get("JSON_Data", "").strip()]
    latest = valid[-count:] if len(valid) >= count else valid
    latest.reverse()  # 最新的排前面

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
    """格式化題目訊息"""
    letters = ["A", "B", "C", "D"]
    lines = [f"📝 *第 {q_idx+1} 題（共 {total} 題）*\n"]
    lines.append(f"{q['q']}\n")
    for i, opt in enumerate(q["options"]):
        lines.append(f"({letters[i]}) {opt}")
    return "\n".join(lines)


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """開始指令"""
    await update.message.reply_text(
        "👋 哈囉！我是你的多益練習 Bot！\n\n"
        "指令列表：\n"
        "📚 /quiz - 開始今日測驗\n"
        "📰 /articles - 查看今日文章列表\n"
        "❓ /help - 說明"
    )


async def articles_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """列出今日文章"""
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


async def quiz_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """開始測驗"""
    await update.message.reply_text("⏳ 載入題目中...")
    try:
        articles = get_latest_articles(4)
        if not articles:
            await update.message.reply_text("目前還沒有文章，請稍後再試！")
            return ConversationHandler.END

        # 收集所有題目
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
        context.user_data["questions"] = all_questions
        context.user_data["current"] = 0
        context.user_data["correct"] = 0
        context.user_data["total"] = len(all_questions)

        await update.message.reply_text(
            f"🎯 今日共 *{len(all_questions)} 題*，來自 {len(articles)} 篇文章！\n\n"
            "請用 A / B / C / D 回答每一題。\n"
            "輸入 /stop 可以中途結束。",
            parse_mode="Markdown"
        )

        # 傳送第一題
        await send_question(update, context)
        return ANSWERING

    except Exception as e:
        logger.error(f"quiz_start error: {e}")
        await update.message.reply_text(f"❌ 載入失敗：{e}")
        return ConversationHandler.END


async def send_question(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """傳送當前題目"""
    questions = context.user_data["questions"]
    current = context.user_data["current"]
    total = context.user_data["total"]
    q = questions[current]

    # 顯示文章標題（每篇第一題時顯示）
    msg = ""
    if current == 0 or questions[current]["article_title"] != questions[current-1]["article_title"]:
        msg += f"📰 *{q['article_title']}*\n\n"

    msg += format_question(current, q, total)

    keyboard = [["A", "B"], ["C", "D"]]
    reply_markup = ReplyKeyboardMarkup(keyboard, one_time_keyboard=True, resize_keyboard=True)
    await update.message.reply_text(msg, parse_mode="Markdown", reply_markup=reply_markup)


async def handle_answer(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """處理答案"""
    user_input = update.message.text.strip().upper()
    letters = ["A", "B", "C", "D"]

    if user_input not in letters:
        await update.message.reply_text("請輸入 A、B、C 或 D！")
        return ANSWERING

    questions = context.user_data["questions"]
    current = context.user_data["current"]
    q = questions[current]
    correct_idx = q["ans"]
    user_idx = letters.index(user_input)

    # 判斷對錯
    if user_idx == correct_idx:
        context.user_data["correct"] += 1
        feedback = f"✅ *正確！*"
    else:
        correct_letter = letters[correct_idx]
        feedback = f"❌ *錯誤！*\n正確答案是 ({correct_letter}) {q['options'][correct_idx]}"

    await update.message.reply_text(feedback, parse_mode="Markdown")

    # 下一題或結束
    context.user_data["current"] += 1
    if context.user_data["current"] >= context.user_data["total"]:
        return await quiz_end(update, context)
    else:
        await send_question(update, context)
        return ANSWERING


async def quiz_end(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """測驗結束"""
    correct = context.user_data["correct"]
    total = context.user_data["total"]
    pct = correct / total if total > 0 else 0

    if pct == 1.0:
        emoji = "🎉"
        msg = "滿分！太厲害了！"
    elif pct >= 0.75:
        emoji = "🌟"
        msg = "表現很好！繼續保持！"
    elif pct >= 0.5:
        emoji = "👍"
        msg = "不錯喔！再接再厲！"
    else:
        emoji = "💪"
        msg = "繼續加油！明天再挑戰！"

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
    """中途停止測驗"""
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
        "/quiz - 開始今日測驗（一題一題作答）\n"
        "/articles - 查看今日文章標題\n"
        "/stop - 中途停止測驗\n"
        "/help - 顯示此說明\n\n"
        "每天早上 6 點會自動更新新文章！",
        parse_mode="Markdown"
    )


def main():
    app = Application.builder().token(BOT_TOKEN).build()

    # 對答 ConversationHandler
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
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
