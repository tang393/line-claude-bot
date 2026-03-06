#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
LINE Claude Bot - Aaron 的全知全能私人助理
工具：網路搜尋、Gmail 收發、提醒事項、圖片/語音/影片分析
"""

import os
import json
import hashlib
import hmac
import base64
import asyncio
import tempfile
import smtplib
import imaplib
import email as email_lib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from email.header import decode_header
from pathlib import Path
from datetime import datetime
from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import JSONResponse
import anthropic
import httpx
import uvicorn

app = FastAPI()

# ── 環境變數 ──────────────────────────────────────────
ANTHROPIC_API_KEY        = os.environ.get("ANTHROPIC_API_KEY", "")
LINE_CHANNEL_SECRET      = os.environ.get("LINE_CHANNEL_SECRET", "")
LINE_CHANNEL_ACCESS_TOKEN= os.environ.get("LINE_CHANNEL_ACCESS_TOKEN", "")
BRAVE_API_KEY            = os.environ.get("BRAVE_API_KEY", "")
GROQ_API_KEY             = os.environ.get("GROQ_API_KEY", "")
GMAIL_ADDRESS            = os.environ.get("GMAIL_ADDRESS", "")
GMAIL_APP_PASSWORD       = os.environ.get("GMAIL_APP_PASSWORD", "")
SYNC_SECRET              = os.environ.get("PASSWORD", "")

MEMORY_PATH  = Path.home() / ".claude/projects/-Users-user/memory/MEMORY.md"
LINE_LOG_PATH= Path.home() / ".claude/projects/-Users-user/memory/line-conversations.md"
MAX_HISTORY  = 20

# ── 狀態 ──────────────────────────────────────────────
conversation_history: dict[str, list] = {}
daily_log: list = []
known_user_ids: list = []   # 存已知的 LINE user_id，供主動推播用
reminders: list = []        # [{text, time_hint, created}]
anthropic_client = anthropic.AsyncAnthropic(api_key=ANTHROPIC_API_KEY)

# ── 記憶 ──────────────────────────────────────────────

def get_memory() -> str:
    content = os.environ.get("MEMORY_CONTENT", "")
    if not content:
        try:
            content = MEMORY_PATH.read_text(encoding="utf-8")
        except Exception:
            content = "（無背景資料）"
    if LINE_LOG_PATH.exists():
        log = LINE_LOG_PATH.read_text(encoding="utf-8").strip()
        if log:
            content += f"\n\n# 近期 LINE 對話重點\n{log}"
    if reminders:
        reminder_text = "\n".join([f"・{r['text']} （設定於 {r['created']}）" for r in reminders])
        content += f"\n\n# 待辦提醒\n{reminder_text}"
    return content

def save_to_memory(content: str) -> bool:
    try:
        LINE_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        ts = datetime.now().strftime("%Y-%m-%d %H:%M")
        with open(LINE_LOG_PATH, "a", encoding="utf-8") as f:
            f.write(f"\n## {ts}\n{content}\n")
        return True
    except Exception:
        return False

# ── System Prompt（8層架構）─────────────────────────

def build_system_prompt() -> str:
    memory = get_memory()
    today  = datetime.now().strftime("%Y-%m-%d %A")
    gmail_status = "已連接（可收發信）" if GMAIL_ADDRESS else "未設定"

    return f"""# LAYER 1 — 身份與範疇
你是 Aaron（湯凱賀）的私人全知全能助理，透過 LINE 24/7 待命。
你的核心職責：商業決策支援、越南診所管理、賀寶芙團隊、日常事務處理。

# LAYER 2 — 用戶背景
{memory}

# LAYER 3 — 工具使用規則
你擁有以下工具，必須主動使用，不要等 Aaron 要求：

web_search：
・用於：最新市場數據、競品、法規、新聞、任何你不確定的即時資訊
・禁止：查你已知的常識性問題

send_email：
・狀態：{gmail_status}
・用於：代 Aaron 起草並發送正式信件
・發送前必須先念給 Aaron 確認，獲得許可再送出
・如果 Aaron 說「直接發」，不需確認

read_emails：
・狀態：{gmail_status}
・用於：讀取 Aaron 最新郵件，主動彙報重要內容
・預設讀最新 5 封

set_reminder：
・用於：Aaron 說「記住」「提醒我」「待辦」時，立即存入提醒清單

# LAYER 4 — 記憶規則
・Aaron 說「記住 [XXX]」→ 立即用 set_reminder 存入，同時確認「已記住：[XXX]」
・重要決策、承諾、數字：主動在回覆後加一行「已記入：[重點]」
・不要依賴對話記憶，重要事項一定要用工具存儲

# LAYER 5 — 工作流程
收到任務 → 判斷需要哪些工具 → 先執行工具取得資訊 → 整合後直接給出答案/成品
・草稿類（合約、信件、文案）：直接給完整版，不給半成品讓 Aaron 自己填
・研究類：先搜尋，再給有數據支撐的結論
・待辦類：執行完回報結果，不是回報「你需要做 XXX」

# LAYER 6 — 溝通風格
・繁體中文，像朋友發訊息，不是報告書
・絕對禁止：* ** # ` 等 Markdown 符號（LINE 顯示亂碼）
・條列用「・」，不用「-」「*」
・不說「當然可以」「很好的問題」「我理解您的需求」
・長的才長，短的就短，不硬撐字數
・給建議時直接說結論，原因放後面

# LAYER 7 — 自主決定 vs 先請示
自主決定（直接做）：研究分析、起草文件、搜尋資訊、記錄事項、提供建議
先請示再做：發送郵件、重大財務建議、對外聯繫、刪除或不可逆操作

# LAYER 8 — 當前環境
今天：{today}
Gmail：{gmail_status}
語音辨識：{'已啟用' if GROQ_API_KEY else '未啟用'}"""


# ── 工具實作 ──────────────────────────────────────────

TOOLS = [
    {
        "name": "web_search",
        "description": "搜尋網路最新資訊：市場數據、競品、法規、新聞、股價等即時內容。",
        "input_schema": {"type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"]}
    },
    {
        "name": "send_email",
        "description": "代 Aaron 發送 Gmail 郵件。發送前需先向 Aaron 確認內容，除非 Aaron 說直接發。",
        "input_schema": {
            "type": "object",
            "properties": {
                "to": {"type": "string", "description": "收件人信箱"},
                "subject": {"type": "string", "description": "主旨"},
                "body": {"type": "string", "description": "信件內容"},
                "confirmed": {"type": "boolean", "description": "Aaron 是否已確認要發送"}
            },
            "required": ["to", "subject", "body", "confirmed"]
        }
    },
    {
        "name": "read_emails",
        "description": "讀取 Aaron 的 Gmail 最新郵件，彙報重要內容。",
        "input_schema": {
            "type": "object",
            "properties": {"count": {"type": "integer", "description": "讀幾封，預設5", "default": 5}},
            "required": []
        }
    },
    {
        "name": "set_reminder",
        "description": "存入提醒/待辦事項。Aaron 說「記住」「提醒我」「待辦」時使用。",
        "input_schema": {
            "type": "object",
            "properties": {
                "text": {"type": "string", "description": "提醒內容"},
                "time_hint": {"type": "string", "description": "時間提示（選填），如「明天」「下週一」「開幕前」"}
            },
            "required": ["text"]
        }
    },
    {
        "name": "list_reminders",
        "description": "列出所有待辦提醒事項。",
        "input_schema": {"type": "object", "properties": {}, "required": []}
    }
]


async def brave_search(query: str) -> str:
    if not BRAVE_API_KEY:
        return "（搜尋功能未設定）"
    url = "https://api.search.brave.com/res/v1/web/search"
    headers = {"Accept": "application/json", "X-Subscription-Token": BRAVE_API_KEY}
    async with httpx.AsyncClient() as client:
        resp = await client.get(url, headers=headers, params={"q": query, "count": 5}, timeout=10)
        if resp.status_code != 200:
            return f"搜尋失敗（{resp.status_code}）"
        results = resp.json().get("web", {}).get("results", [])
        if not results:
            return "沒找到相關結果"
        return "\n\n".join([f"・{r.get('title','')}\n  {r.get('description','')}\n  {r.get('url','')}" for r in results[:5]])


def do_send_email(to: str, subject: str, body: str, confirmed: bool) -> str:
    if not confirmed:
        return f"等待確認。信件草稿：\n收件人：{to}\n主旨：{subject}\n\n{body}\n\n請回覆「確認發送」或「不用了」"
    if not GMAIL_ADDRESS or not GMAIL_APP_PASSWORD:
        return "Gmail 未設定，請先設定 GMAIL_ADDRESS 和 GMAIL_APP_PASSWORD 環境變數"
    try:
        msg = MIMEMultipart()
        msg["From"] = GMAIL_ADDRESS
        msg["To"] = to
        msg["Subject"] = subject
        msg.attach(MIMEText(body, "plain", "utf-8"))
        with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
            server.login(GMAIL_ADDRESS, GMAIL_APP_PASSWORD)
            server.sendmail(GMAIL_ADDRESS, to, msg.as_string())
        return f"信件已發送給 {to}"
    except Exception as e:
        return f"發送失敗：{str(e)}"


def do_read_emails(count: int = 5) -> str:
    if not GMAIL_ADDRESS or not GMAIL_APP_PASSWORD:
        return "Gmail 未設定，請先設定 GMAIL_ADDRESS 和 GMAIL_APP_PASSWORD 環境變數"
    try:
        mail = imaplib.IMAP4_SSL("imap.gmail.com")
        mail.login(GMAIL_ADDRESS, GMAIL_APP_PASSWORD)
        mail.select("INBOX")
        _, data = mail.search(None, "ALL")
        ids = data[0].split()[-count:]
        results = []
        for num in reversed(ids):
            _, msg_data = mail.fetch(num, "(RFC822)")
            msg = email_lib.message_from_bytes(msg_data[0][1])
            subject_raw = decode_header(msg["Subject"] or "")[0]
            subject = subject_raw[0].decode(subject_raw[1] or "utf-8") if isinstance(subject_raw[0], bytes) else subject_raw[0]
            sender = msg.get("From", "")
            date = msg.get("Date", "")
            body = ""
            if msg.is_multipart():
                for part in msg.walk():
                    if part.get_content_type() == "text/plain":
                        body = part.get_payload(decode=True).decode("utf-8", errors="ignore")[:200]
                        break
            else:
                body = msg.get_payload(decode=True).decode("utf-8", errors="ignore")[:200]
            results.append(f"寄件人：{sender}\n主旨：{subject}\n時間：{date}\n內容：{body}...")
        mail.logout()
        return "\n\n---\n\n".join(results) if results else "收件匣是空的"
    except Exception as e:
        return f"讀取郵件失敗：{str(e)}"


def do_set_reminder(text: str, time_hint: str = "") -> str:
    entry = {
        "text": text,
        "time_hint": time_hint,
        "created": datetime.now().strftime("%Y-%m-%d %H:%M")
    }
    reminders.append(entry)
    hint_str = f"（{time_hint}）" if time_hint else ""
    return f"已記住{hint_str}：{text}"


def do_list_reminders() -> str:
    if not reminders:
        return "目前沒有待辦提醒"
    lines = [f"{i+1}. {r['text']}" + (f"（{r['time_hint']}）" if r.get('time_hint') else "") + f" — 記於 {r['created']}"
             for i, r in enumerate(reminders)]
    return "待辦提醒清單：\n" + "\n".join(lines)


async def process_tool_call(tool_name: str, tool_input: dict) -> str:
    if tool_name == "web_search":
        return await brave_search(tool_input["query"])
    elif tool_name == "send_email":
        return do_send_email(tool_input["to"], tool_input["subject"], tool_input["body"], tool_input.get("confirmed", False))
    elif tool_name == "read_emails":
        return do_read_emails(tool_input.get("count", 5))
    elif tool_name == "set_reminder":
        return do_set_reminder(tool_input["text"], tool_input.get("time_hint", ""))
    elif tool_name == "list_reminders":
        return do_list_reminders()
    return "未知工具"


# ── Claude API ────────────────────────────────────────

async def chat_with_claude(user_id: str, user_message: str, media_images: list[bytes] | None = None) -> str:
    if user_id not in conversation_history:
        conversation_history[user_id] = []

    if media_images:
        current_content = []
        for img in media_images[:5]:
            b64 = base64.b64encode(img).decode("utf-8")
            current_content.append({"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": b64}})
        current_content.append({"type": "text", "text": user_message})
        history_content = f"（傳送了媒體）{user_message}"
    else:
        current_content = user_message
        history_content = user_message

    conversation_history[user_id].append({"role": "user", "content": history_content})
    if len(conversation_history[user_id]) > MAX_HISTORY * 2:
        conversation_history[user_id] = conversation_history[user_id][-MAX_HISTORY * 2:]

    messages = conversation_history[user_id][:-1].copy()
    messages.append({"role": "user", "content": current_content})

    for _ in range(8):  # 最多 8 輪工具呼叫
        response = await anthropic_client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=2000,
            system=build_system_prompt(),
            tools=TOOLS,
            messages=messages
        )

        if response.stop_reason == "end_turn":
            text = "".join(block.text for block in response.content if hasattr(block, "text"))
            conversation_history[user_id].append({"role": "assistant", "content": text})
            return text[:4999]

        elif response.stop_reason == "tool_use":
            messages.append({"role": "assistant", "content": response.content})
            tool_results = []
            for block in response.content:
                if block.type == "tool_use":
                    result = await process_tool_call(block.name, block.input)
                    tool_results.append({"type": "tool_result", "tool_use_id": block.id, "content": result})
            messages.append({"role": "user", "content": tool_results})
        else:
            break

    return "（處理中斷，請再試一次）"


# ── 媒體處理 ──────────────────────────────────────────

async def download_line_media(message_id: str) -> bytes | None:
    url = f"https://api-data.line.me/v2/bot/message/{message_id}/content"
    headers = {"Authorization": f"Bearer {LINE_CHANNEL_ACCESS_TOKEN}"}
    async with httpx.AsyncClient() as client:
        resp = await client.get(url, headers=headers, timeout=30)
        return resp.content if resp.status_code == 200 else None


async def transcribe_audio(audio_bytes: bytes, filename: str = "audio.m4a") -> str:
    if not GROQ_API_KEY:
        return "（語音辨識未設定）"
    url = "https://api.groq.com/openai/v1/audio/transcriptions"
    headers = {"Authorization": f"Bearer {GROQ_API_KEY}"}
    files = {"file": (filename, audio_bytes, "audio/m4a")}
    data = {"model": "whisper-large-v3-turbo", "response_format": "text"}
    async with httpx.AsyncClient() as client:
        resp = await client.post(url, headers=headers, files=files, data=data, timeout=60)
        return resp.text.strip() if resp.status_code == 200 else f"（語音轉文字失敗：{resp.status_code}）"


async def extract_video_data(video_bytes: bytes) -> tuple[str, list[bytes]]:
    with tempfile.TemporaryDirectory() as tmpdir:
        video_path = f"{tmpdir}/video.mp4"
        audio_path = f"{tmpdir}/audio.m4a"
        frame_pattern = f"{tmpdir}/frame_%02d.jpg"
        with open(video_path, "wb") as f:
            f.write(video_bytes)
        p1 = await asyncio.create_subprocess_exec("ffmpeg", "-i", video_path, "-vn", "-acodec", "copy", audio_path,
            stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
        await asyncio.wait_for(p1.wait(), timeout=30)
        p2 = await asyncio.create_subprocess_exec("ffmpeg", "-i", video_path, "-vf", "fps=1/10,scale=720:-1",
            "-frames:v", "5", frame_pattern, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
        await asyncio.wait_for(p2.wait(), timeout=30)
        transcription = ""
        if os.path.exists(audio_path):
            with open(audio_path, "rb") as f:
                transcription = await transcribe_audio(f.read())
        frames = []
        for i in range(1, 6):
            fp = f"{tmpdir}/frame_{i:02d}.jpg"
            if os.path.exists(fp):
                with open(fp, "rb") as f:
                    frames.append(f.read())
        return transcription, frames


# ── LINE API ──────────────────────────────────────────

def verify_signature(body: bytes, signature: str) -> bool:
    if not LINE_CHANNEL_SECRET:
        return True
    hash_val = hmac.new(LINE_CHANNEL_SECRET.encode(), body, hashlib.sha256).digest()
    return hmac.compare_digest(base64.b64encode(hash_val).decode(), signature)


async def send_loading_animation(user_id: str, seconds: int = 30):
    async with httpx.AsyncClient() as client:
        await client.post("https://api.line.me/v2/bot/chat/loading/start",
            headers={"Content-Type": "application/json", "Authorization": f"Bearer {LINE_CHANNEL_ACCESS_TOKEN}"},
            json={"chatId": user_id, "loadingSeconds": seconds}, timeout=5)


async def send_line_reply(reply_token: str, text: str):
    async with httpx.AsyncClient() as client:
        await client.post("https://api.line.me/v2/bot/message/reply",
            headers={"Content-Type": "application/json", "Authorization": f"Bearer {LINE_CHANNEL_ACCESS_TOKEN}"},
            json={"replyToken": reply_token, "messages": [{"type": "text", "text": text}]}, timeout=10)


async def send_line_push(user_id: str, text: str):
    """主動推播訊息給 user"""
    async with httpx.AsyncClient() as client:
        await client.post("https://api.line.me/v2/bot/message/push",
            headers={"Content-Type": "application/json", "Authorization": f"Bearer {LINE_CHANNEL_ACCESS_TOKEN}"},
            json={"to": user_id, "messages": [{"type": "text", "text": text}]}, timeout=10)


# ── Webhook ───────────────────────────────────────────

@app.post("/webhook")
async def webhook(request: Request):
    body = await request.body()
    sig = request.headers.get("X-Line-Signature", "")
    if not verify_signature(body, sig):
        raise HTTPException(status_code=400, detail="Invalid signature")
    try:
        data = json.loads(body)
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON")

    for event in data.get("events", []):
        if event.get("type") != "message":
            continue
        msg_type = event.get("message", {}).get("type")
        if msg_type not in ("text", "image", "audio", "video"):
            continue

        user_id = event["source"]["userId"]
        reply_token = event["replyToken"]
        msg_id = event["message"]["id"]

        # 記錄 user_id 供主動推播用
        if user_id not in known_user_ids:
            known_user_ids.append(user_id)

        try:
            await send_loading_animation(user_id, seconds=60)

            if msg_type == "image":
                img_data = await download_line_media(msg_id)
                reply = await chat_with_claude(user_id, "請描述這張圖片，並依據我的背景給出相關見解。", media_images=[img_data] if img_data else None)
                log_label = "（圖片）"

            elif msg_type == "audio":
                audio_data = await download_line_media(msg_id)
                if audio_data:
                    transcription = await transcribe_audio(audio_data)
                    reply = await chat_with_claude(user_id, transcription)
                    log_label = f"（語音）{transcription[:100]}"
                else:
                    reply = "語音下載失敗"
                    log_label = "（語音失敗）"

            elif msg_type == "video":
                video_data = await download_line_media(msg_id)
                if video_data:
                    transcription, frames = await extract_video_data(video_data)
                    msg = f"影片語音：{transcription}\n請分析影片內容。" if transcription else "請分析影片畫面內容。"
                    reply = await chat_with_claude(user_id, msg, media_images=frames if frames else None)
                    log_label = f"（影片）{transcription[:100]}"
                else:
                    reply = "影片下載失敗"
                    log_label = "（影片失敗）"

            else:
                user_text = event["message"]["text"]

                if user_text.startswith("/記住") or user_text.startswith("/remember"):
                    content = user_text.replace("/記住", "").replace("/remember", "").strip()
                    if content:
                        ok = save_to_memory(content)
                        reply = "已記住。" if ok else "儲存失敗"
                    else:
                        reply = "用法：/記住 [要記的內容]"
                    await send_line_reply(reply_token, reply)
                    continue

                if user_text.strip() in ("/清除", "/clear"):
                    conversation_history.pop(user_id, None)
                    await send_line_reply(reply_token, "對話記錄已清除")
                    continue

                if user_text.strip() == "/提醒" or user_text.strip() == "/reminders":
                    reply = do_list_reminders()
                    await send_line_reply(reply_token, reply)
                    continue

                reply = await chat_with_claude(user_id, user_text)
                log_label = user_text

                important_kw = ["決定", "確認", "待辦", "簽約", "錄用", "開幕", "結論", "記住", "協議"]
                if any(kw in user_text for kw in important_kw):
                    save_to_memory(f"Aaron：{user_text}\n助理：{reply[:300]}")

            await send_line_reply(reply_token, reply)
            daily_log.append({"time": datetime.now().strftime("%H:%M"), "user": log_label[:200], "reply": reply[:500]})

        except Exception as e:
            import traceback
            print(f"[ERROR] user={user_id} type={msg_type}\n{traceback.format_exc()}")
            await send_line_reply(reply_token, f"出錯了：{str(e)[:200]}")

    return JSONResponse({"status": "ok"})


# ── 管理端點 ──────────────────────────────────────────

@app.get("/health")
async def health():
    import shutil
    return {
        "status": "ok",
        "memory_loaded": bool(os.environ.get("MEMORY_CONTENT", "")) or MEMORY_PATH.exists(),
        "groq_ready": bool(GROQ_API_KEY),
        "ffmpeg_ready": shutil.which("ffmpeg") is not None,
        "gmail_ready": bool(GMAIL_ADDRESS and GMAIL_APP_PASSWORD),
        "known_users": len(known_user_ids),
        "reminders": len(reminders),
        "daily_log_count": len(daily_log)
    }


@app.get("/morning-briefing")
async def morning_briefing(secret: str = ""):
    """每日晨報：主動推播給所有已知用戶"""
    if not SYNC_SECRET or secret != SYNC_SECRET:
        raise HTTPException(status_code=403, detail="Forbidden")
    if not known_user_ids:
        return {"status": "no_users", "message": "沒有已知用戶，需要先傳訊息給 bot"}

    today = datetime.now().strftime("%Y-%m-%d %A")
    reminder_text = do_list_reminders()

    briefing_prompt = f"今天是 {today}。請用 3-5 行給 Aaron 一個晨報：今日重點提醒 + 最近待辦清單中最緊急的事項。簡潔，像朋友發訊息。\n\n{reminder_text}"

    response = await anthropic_client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=500,
        system=build_system_prompt(),
        messages=[{"role": "user", "content": briefing_prompt}]
    )
    briefing = response.content[0].text

    sent = 0
    for uid in known_user_ids:
        await send_line_push(uid, briefing)
        sent += 1

    return {"status": "sent", "recipients": sent, "briefing": briefing}


@app.get("/daily-summary")
async def daily_summary(secret: str = ""):
    if not SYNC_SECRET or secret != SYNC_SECRET:
        raise HTTPException(status_code=403, detail="Forbidden")
    if not daily_log:
        return {"summary": "", "count": 0, "date": datetime.now().strftime("%Y-%m-%d")}

    log_text = "\n".join([f"[{i['time']}] Aaron：{i['user']}\n助理：{i['reply']}" for i in daily_log])
    date_str = datetime.now().strftime("%Y-%m-%d")

    response = await anthropic_client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=800,
        messages=[{"role": "user", "content": f"以下是 {date_str} 的 LINE 對話，請用繁體中文摘要 3-5 個重點（決策、待辦、重要資訊、結論）。沒重要內容就寫「無重要事項」。\n\n{log_text}"}]
    )
    summary = response.content[0].text
    daily_log.clear()
    return {"summary": summary, "count": len(log_text), "date": date_str}


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8000)
