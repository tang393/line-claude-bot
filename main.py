#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
LINE Claude Bot - Aaron 的全知全能私人助理
工具：網路搜尋、Gmail 收發、天氣、網頁瀏覽、提醒事項、圖片/語音/影片分析
主動功能：重要郵件推播、每日晨報（含天氣）
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
MAC_SERVICE_URL          = os.environ.get("MAC_SERVICE_URL", "")  # Mac Computer Use 服務 URL

MEMORY_PATH  = Path.home() / ".claude/projects/-Users-user/memory/MEMORY.md"
LINE_LOG_PATH= Path.home() / ".claude/projects/-Users-user/memory/line-conversations.md"
MAX_HISTORY  = 20

# 重要郵件關鍵字
IMPORTANT_EMAIL_KEYWORDS = [
    "合約", "協議", "緊急", "urgent", "important", "付款", "帳單", "invoice",
    "overdue", "逾期", "截止", "deadline", "簽約", "URGENT", "ACTION REQUIRED",
    "開幕", "執照", "股東", "律師", "legal", "court", "lawsuit", "罰款"
]

# ── 狀態 ──────────────────────────────────────────────
conversation_history: dict[str, list] = {}
daily_log: list = []
known_user_ids: list = []   # 存已知的 LINE user_id，供主動推播用
reminders: list = []        # [{text, time_hint, created}]
seen_email_ids: set = set() # 已推播的郵件 ID，避免重複推播
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
    mac_status = "已連接（可控制瀏覽器）" if MAC_SERVICE_URL else "未設定"

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

get_weather：
・用於：查天氣，預設查胡志明市和台北兩個城市
・Aaron 問天氣時立刻查，不要說「你可以去查」

browse_url：
・用於：直接開啟網頁讀取內容，競品網站、法規文件、新聞原文
・比搜尋更精準，當你有確切網址時優先用這個

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

computer_use_task：
・狀態：{mac_status}
・用於：讓 Mac 電腦實際執行任務（開瀏覽器、填表、發文、訂票、看 Instagram）
・Aaron 說「幫我去」「直接做」「到網站上」「看 Instagram」「看競品」時，MAC 已連接就直接呼叫，不要問要不要用哪個方法
・絕對禁止說「Mac 未設定」然後問替代方案——有設定就直接用

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
自主決定（直接做）：研究分析、起草文件、搜尋資訊、記錄事項、提供建議、查天氣、瀏覽網頁、用 Mac 控制瀏覽器
先請示再做：發送郵件、重大財務建議、對外聯繫、刪除或不可逆操作
嚴禁：收到任務後問「要用哪個方法？」、「需要我做什麼？」——直接判斷並執行

# LAYER 8 — 當前環境
今天：{today}
Gmail：{gmail_status}
語音辨識：{'已啟用' if GROQ_API_KEY else '未啟用'}
Mac 遠端控制：{mac_status}"""


# ── 工具實作 ──────────────────────────────────────────

TOOLS = [
    {
        "name": "web_search",
        "description": "搜尋網路最新資訊：市場數據、競品、法規、新聞、股價等即時內容。",
        "input_schema": {"type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"]}
    },
    {
        "name": "get_weather",
        "description": "取得指定地點的即時天氣和今日預報。Aaron 問天氣時主動查，不需要等他要求。",
        "input_schema": {
            "type": "object",
            "properties": {"location": {"type": "string", "description": "地點，如 'Ho Chi Minh City' 或 'Taipei'"}},
            "required": ["location"]
        }
    },
    {
        "name": "browse_url",
        "description": "直接開啟網頁讀取內容。用於查競品網站、讀新聞原文、查政府法規、取得任何網址的詳細內容。",
        "input_schema": {
            "type": "object",
            "properties": {"url": {"type": "string", "description": "完整網址，包含 https://"}},
            "required": ["url"]
        }
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
    },
    {
        "name": "computer_use_task",
        "description": "讓 Aaron 的 Mac 電腦實際執行任務：開瀏覽器、填寫表單、在網站上操作、訂票、發社群文章等。Aaron 說「幫我去做」「到網站上」「直接幫我」時使用。",
        "input_schema": {
            "type": "object",
            "properties": {
                "task": {"type": "string", "description": "要執行的任務描述，越詳細越好"},
                "url": {"type": "string", "description": "要前往的網址（選填）"}
            },
            "required": ["task"]
        }
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


async def get_weather_impl(location: str) -> str:
    """Open-Meteo 天氣（完全免費，無需 API key）"""
    try:
        async with httpx.AsyncClient() as client:
            geo = await client.get(
                "https://geocoding-api.open-meteo.com/v1/search",
                params={"name": location, "count": 1, "language": "zh"},
                timeout=10
            )
            geo_data = geo.json()
            if not geo_data.get("results"):
                return f"找不到 {location} 的位置"
            r = geo_data["results"][0]
            lat, lon = r["latitude"], r["longitude"]
            name = r.get("name", location)

            weather = await client.get(
                "https://api.open-meteo.com/v1/forecast",
                params={
                    "latitude": lat, "longitude": lon,
                    "current": "temperature_2m,relative_humidity_2m,weather_code,wind_speed_10m,apparent_temperature",
                    "daily": "temperature_2m_max,temperature_2m_min,precipitation_probability_max",
                    "timezone": "auto",
                    "forecast_days": 1
                },
                timeout=10
            )
            w = weather.json()
            cur = w.get("current", {})
            daily = w.get("daily", {})

            wcode_map = {
                0: "晴天", 1: "大致晴朗", 2: "局部多雲", 3: "多雲",
                45: "有霧", 48: "結冰霧", 51: "小毛毛雨", 53: "毛毛雨", 55: "大毛毛雨",
                61: "小雨", 63: "中雨", 65: "大雨", 71: "小雪", 80: "陣雨",
                95: "雷雨", 96: "冰雹雷雨", 99: "大雷雨"
            }
            wdesc = wcode_map.get(cur.get("weather_code", 0), "天氣未知")

            max_t = daily.get("temperature_2m_max", ["-"])[0]
            min_t = daily.get("temperature_2m_min", ["-"])[0]
            rain_prob = daily.get("precipitation_probability_max", ["-"])[0]

            return (
                f"{name}：{wdesc}\n"
                f"氣溫 {cur.get('temperature_2m', '-')}°C（體感 {cur.get('apparent_temperature', '-')}°C）\n"
                f"濕度 {cur.get('relative_humidity_2m', '-')}%　風速 {cur.get('wind_speed_10m', '-')} km/h\n"
                f"今日最高 {max_t}°C / 最低 {min_t}°C　降雨機率 {rain_prob}%"
            )
    except Exception as e:
        return f"{location} 天氣查詢失敗：{str(e)}"


async def browse_url_impl(url: str) -> str:
    """用 httpx + BeautifulSoup 讀取網頁內容"""
    try:
        async with httpx.AsyncClient(follow_redirects=True) as client:
            resp = await client.get(
                url,
                headers={"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 Chrome/120.0"},
                timeout=15
            )
            if resp.status_code != 200:
                return f"無法存取 {url}（HTTP {resp.status_code}）"

            try:
                from bs4 import BeautifulSoup
                soup = BeautifulSoup(resp.text, "lxml")
                for tag in soup(["script", "style", "nav", "footer", "header", "aside", "iframe", "noscript"]):
                    tag.decompose()
                text = soup.get_text(separator="\n", strip=True)
                lines = [l.strip() for l in text.split("\n") if len(l.strip()) > 15]
                return "\n".join(lines[:80])[:3000]
            except ImportError:
                return resp.text[:2000]
    except Exception as e:
        return f"網頁存取失敗：{str(e)}"


async def computer_use_task_impl(task: str, url: str = "") -> str:
    """呼叫 Mac 本地 Computer Use 服務"""
    if not MAC_SERVICE_URL:
        return "Mac Computer Use 服務未設定。請先在 Aaron 的 Mac 上啟動 mac-computer-use.py，並設定 MAC_SERVICE_URL 環境變數。"
    try:
        async with httpx.AsyncClient() as client:
            payload = {"task": task}
            if url:
                payload["url"] = url
            resp = await client.post(
                f"{MAC_SERVICE_URL}/execute",
                json=payload,
                timeout=120  # Computer Use 可能需要一段時間
            )
            if resp.status_code == 200:
                result = resp.json()
                return result.get("result", "執行完成（無詳細結果）")
            else:
                return f"Mac 服務回應錯誤（HTTP {resp.status_code}）：{resp.text[:200]}"
    except httpx.ConnectError:
        return "無法連接到 Mac 服務，請確認 mac-computer-use.py 正在運行且 MAC_SERVICE_URL 正確"
    except Exception as e:
        return f"執行失敗：{str(e)}"


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
            subject = subject_raw[0].decode(subject_raw[1] or "utf-8") if isinstance(subject_raw[0], bytes) else (subject_raw[0] or "")
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


def check_important_unread_emails() -> list[dict]:
    """掃 Gmail 未讀重要郵件，回傳尚未推播過的"""
    if not GMAIL_ADDRESS or not GMAIL_APP_PASSWORD:
        return []
    try:
        mail = imaplib.IMAP4_SSL("imap.gmail.com")
        mail.login(GMAIL_ADDRESS, GMAIL_APP_PASSWORD)
        mail.select("INBOX")
        _, data = mail.search(None, "UNSEEN")
        ids = data[0].split()
        if not ids:
            mail.logout()
            return []

        important = []
        for num in ids[-30:]:  # 最多檢查最新 30 封未讀
            _, msg_data = mail.fetch(num, "(RFC822)")
            msg = email_lib.message_from_bytes(msg_data[0][1])

            subject_raw = decode_header(msg["Subject"] or "")[0]
            subject = subject_raw[0].decode(subject_raw[1] or "utf-8") if isinstance(subject_raw[0], bytes) else (subject_raw[0] or "")
            email_id = msg.get("Message-ID", str(num))

            if email_id in seen_email_ids:
                continue

            is_important = any(kw.lower() in subject.lower() for kw in IMPORTANT_EMAIL_KEYWORDS)
            if not is_important:
                continue

            sender = msg.get("From", "")
            body = ""
            if msg.is_multipart():
                for part in msg.walk():
                    if part.get_content_type() == "text/plain":
                        body = part.get_payload(decode=True).decode("utf-8", errors="ignore")[:300]
                        break
            else:
                body = msg.get_payload(decode=True).decode("utf-8", errors="ignore")[:300]

            important.append({
                "id": email_id,
                "from": sender[:60],
                "subject": subject[:80],
                "preview": body
            })

        mail.logout()
        return important
    except Exception as e:
        print(f"[proactive_check] email error: {e}")
        return []


async def process_tool_call(tool_name: str, tool_input: dict) -> str:
    if tool_name == "web_search":
        return await brave_search(tool_input["query"])
    elif tool_name == "get_weather":
        return await get_weather_impl(tool_input["location"])
    elif tool_name == "browse_url":
        return await browse_url_impl(tool_input["url"])
    elif tool_name == "send_email":
        return do_send_email(tool_input["to"], tool_input["subject"], tool_input["body"], tool_input.get("confirmed", False))
    elif tool_name == "read_emails":
        return do_read_emails(tool_input.get("count", 5))
    elif tool_name == "set_reminder":
        return do_set_reminder(tool_input["text"], tool_input.get("time_hint", ""))
    elif tool_name == "list_reminders":
        return do_list_reminders()
    elif tool_name == "computer_use_task":
        return await computer_use_task_impl(tool_input["task"], tool_input.get("url", ""))
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
                    msg_text = f"影片語音：{transcription}\n請分析影片內容。" if transcription else "請分析影片畫面內容。"
                    reply = await chat_with_claude(user_id, msg_text, media_images=frames if frames else None)
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

                if user_text.strip() in ("/提醒", "/reminders"):
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
        "mac_service_ready": bool(MAC_SERVICE_URL),
        "known_users": len(known_user_ids),
        "reminders": len(reminders),
        "seen_email_ids": len(seen_email_ids),
        "daily_log_count": len(daily_log)
    }


@app.get("/morning-briefing")
async def morning_briefing(secret: str = ""):
    """每日晨報：天氣 + 重要郵件 + 待辦提醒，主動推播給所有已知用戶"""
    if not SYNC_SECRET or secret != SYNC_SECRET:
        raise HTTPException(status_code=403, detail="Forbidden")
    if not known_user_ids:
        return {"status": "no_users", "message": "沒有已知用戶，需要先傳訊息給 bot"}

    today = datetime.now().strftime("%Y-%m-%d %A")
    reminder_text = do_list_reminders()

    # 並行取得兩地天氣
    hcmc_weather, taipei_weather = await asyncio.gather(
        get_weather_impl("Ho Chi Minh City"),
        get_weather_impl("Taipei")
    )

    # 讀最新郵件
    email_summary = do_read_emails(3)

    briefing_prompt = f"""今天是 {today}。

胡志明市天氣：
{hcmc_weather}

台北天氣：
{taipei_weather}

最新郵件（最近3封）：
{email_summary}

待辦提醒：
{reminder_text}

請給 Aaron 一個晨報，格式：
・今日天氣（兩個城市，一行搞定）
・最重要的 1-2 個待辦
・郵件有沒有重要的（沒有就不用寫）
・一句今日重點提醒

要求：繁體中文，像朋友傳訊息，不用 Markdown，條列用「・」，總長度不超過15行。"""

    response = await anthropic_client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=600,
        system=build_system_prompt(),
        messages=[{"role": "user", "content": briefing_prompt}]
    )
    briefing = response.content[0].text

    sent = 0
    for uid in known_user_ids:
        await send_line_push(uid, briefing)
        sent += 1

    return {"status": "sent", "recipients": sent, "briefing": briefing}


@app.get("/proactive-check")
async def proactive_check(secret: str = ""):
    """主動監測：掃 Gmail 未讀重要郵件，有的話推播到 LINE"""
    if not SYNC_SECRET or secret != SYNC_SECRET:
        raise HTTPException(status_code=403, detail="Forbidden")
    if not known_user_ids:
        return {"status": "no_users", "pushed": 0}

    important_emails = check_important_unread_emails()
    pushed = 0

    for em in important_emails:
        if em["id"] not in seen_email_ids:
            msg = (
                f"重要郵件提醒\n\n"
                f"寄件人：{em['from']}\n"
                f"主旨：{em['subject']}\n\n"
                f"{em['preview'][:200]}"
            )
            for uid in known_user_ids:
                await send_line_push(uid, msg)
            seen_email_ids.add(em["id"])
            pushed += 1

    return {"status": "ok", "checked": len(important_emails), "pushed": pushed}


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
