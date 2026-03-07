#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
LINE Claude Bot - Aaron 的全知全能私人助理 JARVIS
工具：網路搜尋、Gmail 收發、天氣、網頁瀏覽、Google Calendar、知識庫、語音/圖片/影片
主動功能：重要郵件推播、每日晨報（含天氣）、晚報
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
from datetime import datetime, timedelta
from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import JSONResponse
import anthropic
import httpx
import uvicorn
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

app = FastAPI()

# ── 環境變數 ──────────────────────────────────────────
ANTHROPIC_API_KEY         = os.environ.get("ANTHROPIC_API_KEY", "")
LINE_CHANNEL_SECRET       = os.environ.get("LINE_CHANNEL_SECRET", "")
LINE_CHANNEL_ACCESS_TOKEN = os.environ.get("LINE_CHANNEL_ACCESS_TOKEN", "")
BRAVE_API_KEY             = os.environ.get("BRAVE_API_KEY", "")
GROQ_API_KEY              = os.environ.get("GROQ_API_KEY", "")
GMAIL_ADDRESS             = os.environ.get("GMAIL_ADDRESS", "")
GMAIL_APP_PASSWORD        = os.environ.get("GMAIL_APP_PASSWORD", "")
SYNC_SECRET               = os.environ.get("PASSWORD", "")
MAC_SERVICE_URL           = os.environ.get("MAC_SERVICE_URL", "")
GOOGLE_SERVICE_ACCOUNT_JSON = os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON", "")  # 完整 JSON 字串
GOOGLE_CALENDAR_ID        = os.environ.get("GOOGLE_CALENDAR_ID", "primary")
GOOGLE_DRIVE_FOLDER_ID    = os.environ.get("GOOGLE_DRIVE_FOLDER_ID", "")

MEMORY_PATH   = Path.home() / ".claude/projects/-Users-user/memory/MEMORY.md"
LINE_LOG_PATH = Path.home() / ".claude/projects/-Users-user/memory/line-conversations.md"
DATA_PATH     = Path("/tmp/jarvis_data.json")
KB_PATH       = Path("/tmp/jarvis_kb.json")
MAX_HISTORY   = 8

IMPORTANT_EMAIL_KEYWORDS = [
    "合約", "協議", "緊急", "urgent", "important", "付款", "帳單", "invoice",
    "overdue", "逾期", "截止", "deadline", "簽約", "URGENT", "ACTION REQUIRED",
    "開幕", "執照", "股東", "律師", "legal", "court", "lawsuit", "罰款"
]

# ── 持久化狀態 ─────────────────────────────────────────

def load_data() -> dict:
    # 優先本地，沒有就從 Google Drive 拉
    try:
        if DATA_PATH.exists():
            return json.loads(DATA_PATH.read_text(encoding="utf-8"))
    except Exception:
        pass
    # 嘗試從 Google Drive 恢復
    try:
        cloud = gdrive_download("jarvis_data.json")
        if cloud:
            DATA_PATH.parent.mkdir(parents=True, exist_ok=True)
            DATA_PATH.write_text(cloud, encoding="utf-8")
            print("[startup] 從 Google Drive 恢復 jarvis_data.json")
            return json.loads(cloud)
    except Exception:
        pass
    return {"known_user_ids": [], "reminders": [], "seen_email_ids": []}

def save_data():
    try:
        DATA_PATH.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "known_user_ids": known_user_ids,
            "reminders": reminders,
            "seen_email_ids": list(seen_email_ids)
        }
        content = json.dumps(payload, ensure_ascii=False, indent=2)
        DATA_PATH.write_text(content, encoding="utf-8")
        gdrive_upload("jarvis_data.json", content)
    except Exception as e:
        print(f"[save_data] 失敗：{e}")

def load_kb() -> dict:
    try:
        if KB_PATH.exists():
            return json.loads(KB_PATH.read_text(encoding="utf-8"))
    except Exception:
        pass
    try:
        cloud = gdrive_download("jarvis_kb.json")
        if cloud:
            KB_PATH.parent.mkdir(parents=True, exist_ok=True)
            KB_PATH.write_text(cloud, encoding="utf-8")
            print("[startup] 從 Google Drive 恢復 jarvis_kb.json")
            return json.loads(cloud)
    except Exception:
        pass
    return {"clinic": [], "herbalife": [], "contacts": [], "general": []}

def save_kb():
    try:
        KB_PATH.parent.mkdir(parents=True, exist_ok=True)
        content = json.dumps(knowledge_base, ensure_ascii=False, indent=2)
        KB_PATH.write_text(content, encoding="utf-8")
        gdrive_upload("jarvis_kb.json", content)
    except Exception as e:
        print(f"[save_kb] 失敗：{e}")

_data = load_data()

# ── 狀態 ──────────────────────────────────────────────
conversation_history: dict[str, list] = {}
daily_log: list = []
known_user_ids: list = _data.get("known_user_ids", [])
reminders: list = _data.get("reminders", [])
seen_email_ids: set = set(_data.get("seen_email_ids", []))
knowledge_base: dict = load_kb()
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
            content += f"\n\n# 近期 LINE 對話重點\n{log[-500:]}"
    if reminders:
        reminder_text = "\n".join([f"・{r['text']}（設定於 {r['created']}）" for r in reminders])
        content += f"\n\n# 待辦提醒\n{reminder_text}"
    # 附上知識庫摘要
    kb_summary = []
    for cat, entries in knowledge_base.items():
        if entries:
            cat_names = {"clinic": "診所SOP", "herbalife": "賀寶芙", "contacts": "人脈聯絡", "general": "通用知識"}
            kb_summary.append(f"・{cat_names.get(cat, cat)}：{len(entries)} 筆")
    if kb_summary:
        content += f"\n\n# 知識庫（已有資料）\n" + "\n".join(kb_summary) + "\n（用 search_knowledge_base 工具查詢詳細內容）"
    return content

def save_to_memory(content: str) -> bool:
    try:
        LINE_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        ts = datetime.now().strftime("%Y-%m-%d %H:%M")
        with open(LINE_LOG_PATH, "a", encoding="utf-8") as f:
            f.write(f"\n## {ts}\n{content}\n")
        # 同步到 Google Drive
        try:
            full_content = LINE_LOG_PATH.read_text(encoding="utf-8")
            gdrive_upload("line-conversations.md", full_content)
        except Exception:
            pass
        return True
    except Exception:
        return False

# ── System Prompt ──────────────────────────────────────

def build_system_prompt() -> str:
    memory = get_memory()
    today  = datetime.now().strftime("%Y-%m-%d %A")
    gmail_status    = "已連接（可收發信）" if GMAIL_ADDRESS else "未設定"
    mac_status      = "已連接（可控制瀏覽器）" if MAC_SERVICE_URL else "未設定"
    gcal_status     = "已連接" if GOOGLE_SERVICE_ACCOUNT_JSON else "未設定（需要 Google 憑證）"

    return f"""# 身份
你是 JARVIS，Aaron（湯凱賀/凱總）的私人全知全能執行管家，透過 LINE 24/7 待命。
核心職責：商業決策支援、越南診所管理、賀寶芙團隊、日常事務處理。
自稱「J」或「JARVIS」，稱 Aaron 為「凱總」。

# 語言規則
・預設用繁體中文
・Aaron 提到越南客戶、越南員工、或說「越南文」「用越文」時，切換越南文回覆
・同一則訊息如需要，可中越文混用

# 用戶背景
{memory}

# 工具使用原則
需要最新資訊→web_search，天氣→get_weather，幣別→convert_currency，行程→get_calendar/add_calendar_event，郵件→read_emails/send_email，記事→set_reminder/delete_reminder/list_reminders，知識庫→search_knowledge_base/add_to_knowledge_base，網頁→browse_url，Mac操作→computer_use_task
不確定就搜，別說「我不知道」。一次用對的工具，不要重複呼叫。

# 工作流程
收到任務 → 判斷需要哪些工具 → 先執行工具 → 整合結果直接給答案/成品
草稿類：直接給完整版
研究類：先搜尋，再給有數據支撐的結論
待辦類：執行完回報結果

# 溝通風格
・繁體中文為主，必要時切換越南文
・絕對禁止：* ** # ` 等 Markdown 符號（LINE 顯示亂碼）
・條列用「・」
・不說「當然可以」「很好的問題」「我理解您的需求」
・直接說結論，原因放後面
・能一句話講完就不要兩句

# 自主 vs 先請示
自主決定：研究分析、起草文件、搜尋、查天氣、瀏覽網頁、查行程
先請示：發送郵件、新增/刪除行程、對外聯繫、刪除/不可逆操作
嚴禁：「要用哪個方法？」「需要我做什麼？」——直接判斷並執行

# 環境
今天：{today}
Gmail：{gmail_status}
Google Calendar：{gcal_status}
語音辨識：{'已啟用' if GROQ_API_KEY else '未啟用'}
Mac 遠端：{mac_status}"""


# ── 工具清單 ──────────────────────────────────────────

TOOLS = [
    {
        "name": "web_search",
        "description": "搜尋網路最新資訊：市場數據、競品、法規、新聞、越南醫美市場、賀寶芙動態等。遇到需要最新資訊的問題必須呼叫此工具。",
        "input_schema": {"type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"]}
    },
    {
        "name": "get_weather",
        "description": "取得指定地點的即時天氣和今日預報。Aaron 問天氣時立刻查。",
        "input_schema": {
            "type": "object",
            "properties": {"location": {"type": "string", "description": "地點，如 'Ho Chi Minh City' 或 'Taipei'"}},
            "required": ["location"]
        }
    },
    {
        "name": "browse_url",
        "description": "直接開啟網頁讀取內容。用於查競品網站、讀新聞原文、查政府法規。",
        "input_schema": {
            "type": "object",
            "properties": {"url": {"type": "string", "description": "完整網址，包含 https://"}},
            "required": ["url"]
        }
    },
    {
        "name": "send_email",
        "description": "代 Aaron 發送 Gmail 郵件。發送前需先確認，除非 Aaron 說直接發。",
        "input_schema": {
            "type": "object",
            "properties": {
                "to": {"type": "string"},
                "subject": {"type": "string"},
                "body": {"type": "string"},
                "confirmed": {"type": "boolean", "description": "Aaron 是否已確認要發送"}
            },
            "required": ["to", "subject", "body", "confirmed"]
        }
    },
    {
        "name": "read_emails",
        "description": "讀取 Aaron 的 Gmail 最新郵件。",
        "input_schema": {
            "type": "object",
            "properties": {"count": {"type": "integer", "default": 5}},
            "required": []
        }
    },
    {
        "name": "set_reminder",
        "description": "存入提醒/待辦事項，永久保存不消失。Aaron 說「記住」「提醒」「待辦」「別忘了」時使用。",
        "input_schema": {
            "type": "object",
            "properties": {
                "text": {"type": "string"},
                "time_hint": {"type": "string", "description": "時間提示（選填）"}
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
        "name": "get_calendar",
        "description": "查詢 Google Calendar 行程。Aaron 問行程、日程、有沒有會議時使用。",
        "input_schema": {
            "type": "object",
            "properties": {
                "days": {"type": "integer", "description": "查幾天的行程，預設 7", "default": 7}
            },
            "required": []
        }
    },
    {
        "name": "add_calendar_event",
        "description": "在 Google Calendar 新增行程。需先告知 Aaron 再執行。",
        "input_schema": {
            "type": "object",
            "properties": {
                "title": {"type": "string", "description": "行程標題"},
                "start_datetime": {"type": "string", "description": "開始時間，格式 YYYY-MM-DDTHH:MM:SS"},
                "end_datetime": {"type": "string", "description": "結束時間，格式 YYYY-MM-DDTHH:MM:SS"},
                "description": {"type": "string", "description": "備註（選填）"},
                "location": {"type": "string", "description": "地點（選填）"}
            },
            "required": ["title", "start_datetime", "end_datetime"]
        }
    },
    {
        "name": "search_knowledge_base",
        "description": "搜尋本地知識庫。問到診所SOP/療程/價格、賀寶芙產品/制度、人名聯絡資訊時必須先呼叫此工具。",
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "搜尋關鍵字"},
                "category": {"type": "string", "description": "分類（選填）：clinic / herbalife / contacts / general"}
            },
            "required": ["query"]
        }
    },
    {
        "name": "add_to_knowledge_base",
        "description": "新增條目到知識庫。Aaron 說「存入知識庫」「記到知識庫」時使用。",
        "input_schema": {
            "type": "object",
            "properties": {
                "category": {"type": "string", "description": "分類：clinic / herbalife / contacts / general"},
                "title": {"type": "string", "description": "標題"},
                "content": {"type": "string", "description": "內容"}
            },
            "required": ["category", "title", "content"]
        }
    },
    {
        "name": "computer_use_task",
        "description": "讓 Aaron 的 Mac 電腦實際執行任務：開瀏覽器、填寫表單、操作網站等。",
        "input_schema": {
            "type": "object",
            "properties": {
                "task": {"type": "string"},
                "url": {"type": "string", "description": "目標網址（選填）"}
            },
            "required": ["task"]
        }
    },
    {
        "name": "convert_currency",
        "description": "即時匯率換算。Aaron 問到越南盾、台幣、美金之間的換算時使用。支援所有主要貨幣。",
        "input_schema": {
            "type": "object",
            "properties": {
                "amount": {"type": "number", "description": "金額"},
                "from_currency": {"type": "string", "description": "來源幣別，如 TWD, VND, USD"},
                "to_currency": {"type": "string", "description": "目標幣別，如 TWD, VND, USD"}
            },
            "required": ["amount", "from_currency", "to_currency"]
        }
    },
    {
        "name": "delete_reminder",
        "description": "刪除/完成一筆待辦提醒。Aaron 說「完成」「刪掉」「取消提醒」時使用。",
        "input_schema": {
            "type": "object",
            "properties": {
                "index": {"type": "integer", "description": "待辦編號（從1開始）"}
            },
            "required": ["index"]
        }
    }
]


# ── 工具實作 ──────────────────────────────────────────

async def brave_search(query: str) -> str:
    if not BRAVE_API_KEY:
        return await duckduckgo_search(query)
    url = "https://api.search.brave.com/res/v1/web/search"
    headers = {"Accept": "application/json", "X-Subscription-Token": BRAVE_API_KEY}
    try:
        async with httpx.AsyncClient() as client:
            resp = await client.get(url, headers=headers, params={"q": query, "count": 5}, timeout=10)
            if resp.status_code in (429, 403):
                return await duckduckgo_search(query)
            if resp.status_code != 200:
                return await duckduckgo_search(query)
            results = resp.json().get("web", {}).get("results", [])
            if not results:
                return await duckduckgo_search(query)
            return "\n\n".join([f"・{r.get('title','')}\n  {r.get('description','')}\n  {r.get('url','')}" for r in results[:5]])
    except Exception:
        return await duckduckgo_search(query)


async def duckduckgo_search(query: str) -> str:
    try:
        async with httpx.AsyncClient() as client:
            resp = await client.get(
                "https://api.duckduckgo.com/",
                params={"q": query, "format": "json", "no_redirect": "1", "no_html": "1"},
                timeout=10
            )
            data = resp.json()
            results = []
            if data.get("AbstractText"):
                results.append(f"・{data['AbstractText']}\n  {data.get('AbstractURL','')}")
            for r in data.get("RelatedTopics", [])[:4]:
                if isinstance(r, dict) and r.get("Text"):
                    results.append(f"・{r['Text']}")
            return "\n\n".join(results) if results else f"搜尋「{query}」沒有找到相關結果"
    except Exception as e:
        return f"搜尋失敗：{str(e)}"


async def get_weather_impl(location: str) -> str:
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
                    "timezone": "auto", "forecast_days": 1
                },
                timeout=10
            )
            w = weather.json()
            cur = w.get("current", {})
            daily = w.get("daily", {})
            wcode_map = {
                0: "晴天", 1: "大致晴朗", 2: "局部多雲", 3: "多雲",
                45: "有霧", 51: "小毛毛雨", 53: "毛毛雨", 61: "小雨",
                63: "中雨", 65: "大雨", 80: "陣雨", 95: "雷雨"
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


def get_google_service(api: str, version: str, scopes: list):
    """通用 Google API 服務建立"""
    if not GOOGLE_SERVICE_ACCOUNT_JSON:
        return None
    try:
        from google.oauth2 import service_account
        from googleapiclient.discovery import build
        import json as _json
        creds = service_account.Credentials.from_service_account_info(
            _json.loads(GOOGLE_SERVICE_ACCOUNT_JSON), scopes=scopes
        )
        return build(api, version, credentials=creds)
    except Exception as e:
        print(f"[Google {api}] 初始化失敗：{e}")
        return None


# ── Google Drive 同步 ─────────────────────────────────

def gdrive_get_file_id(service, filename: str) -> str | None:
    """在 JARVIS-AI 資料夾中找指定檔案的 ID"""
    try:
        results = service.files().list(
            q=f"name='{filename}' and '{GOOGLE_DRIVE_FOLDER_ID}' in parents and trashed=false",
            fields="files(id, name)"
        ).execute()
        files = results.get("files", [])
        return files[0]["id"] if files else None
    except Exception:
        return None


def gdrive_upload(filename: str, content: str):
    """上傳或更新檔案到 Google Drive JARVIS-AI 資料夾"""
    if not GOOGLE_DRIVE_FOLDER_ID or not GOOGLE_SERVICE_ACCOUNT_JSON:
        return
    try:
        from googleapiclient.http import MediaInMemoryUpload
        service = get_google_service(
            "drive", "v3",
            ["https://www.googleapis.com/auth/drive"]
        )
        if not service:
            return
        media = MediaInMemoryUpload(content.encode("utf-8"), mimetype="text/plain")
        file_id = gdrive_get_file_id(service, filename)
        if file_id:
            service.files().update(fileId=file_id, media_body=media).execute()
        else:
            service.files().create(
                body={"name": filename, "parents": [GOOGLE_DRIVE_FOLDER_ID]},
                media_body=media
            ).execute()
        print(f"[GDrive] 已同步：{filename}")
    except Exception as e:
        print(f"[GDrive] 上傳失敗 {filename}：{e}")


def gdrive_download(filename: str) -> str | None:
    """從 Google Drive 下載檔案內容"""
    if not GOOGLE_DRIVE_FOLDER_ID or not GOOGLE_SERVICE_ACCOUNT_JSON:
        return None
    try:
        service = get_google_service(
            "drive", "v3",
            ["https://www.googleapis.com/auth/drive"]
        )
        if not service:
            return None
        file_id = gdrive_get_file_id(service, filename)
        if not file_id:
            return None
        content = service.files().get_media(fileId=file_id).execute()
        return content.decode("utf-8")
    except Exception as e:
        print(f"[GDrive] 下載失敗 {filename}：{e}")
        return None


def get_google_calendar_service():
    """建立 Google Calendar 服務"""
    return get_google_service("calendar", "v3", ["https://www.googleapis.com/auth/calendar"])


def do_get_calendar(days: int = 7) -> str:
    if not GOOGLE_SERVICE_ACCOUNT_JSON:
        return "Google Calendar 未設定。請提供 GOOGLE_SERVICE_ACCOUNT_JSON 和 GOOGLE_CALENDAR_ID 環境變數。"
    try:
        service = get_google_calendar_service()
        if not service:
            return "Google Calendar 連線失敗"
        now = datetime.utcnow().isoformat() + "Z"
        end = (datetime.utcnow() + timedelta(days=days)).isoformat() + "Z"
        events_result = service.events().list(
            calendarId=GOOGLE_CALENDAR_ID,
            timeMin=now, timeMax=end,
            maxResults=20, singleEvents=True,
            orderBy="startTime"
        ).execute()
        events = events_result.get("items", [])
        if not events:
            return f"未來 {days} 天沒有行程"
        lines = []
        for e in events:
            start = e["start"].get("dateTime", e["start"].get("date", ""))
            if "T" in start:
                dt = datetime.fromisoformat(start.replace("Z", "+00:00"))
                start_str = dt.strftime("%m/%d %H:%M")
            else:
                start_str = start
            title = e.get("summary", "（無標題）")
            loc = f"　地點：{e['location']}" if e.get("location") else ""
            lines.append(f"・{start_str} {title}{loc}")
        return f"未來 {days} 天行程：\n" + "\n".join(lines)
    except Exception as e:
        return f"查詢行程失敗：{str(e)}"


def do_add_calendar_event(title: str, start_datetime: str, end_datetime: str,
                           description: str = "", location: str = "") -> str:
    if not GOOGLE_SERVICE_ACCOUNT_JSON:
        return "Google Calendar 未設定"
    try:
        service = get_google_calendar_service()
        if not service:
            return "Google Calendar 連線失敗"
        event = {
            "summary": title,
            "start": {"dateTime": start_datetime, "timeZone": "Asia/Ho_Chi_Minh"},
            "end": {"dateTime": end_datetime, "timeZone": "Asia/Ho_Chi_Minh"},
        }
        if description:
            event["description"] = description
        if location:
            event["location"] = location
        result = service.events().insert(calendarId=GOOGLE_CALENDAR_ID, body=event).execute()
        return f"已新增行程：{title}\n時間：{start_datetime} ～ {end_datetime}"
    except Exception as e:
        return f"新增行程失敗：{str(e)}"


def do_search_knowledge_base(query: str, category: str = "") -> str:
    query_lower = query.lower()
    results = []
    cats = [category] if category and category in knowledge_base else list(knowledge_base.keys())
    cat_names = {"clinic": "診所", "herbalife": "賀寶芙", "contacts": "人脈", "general": "通用"}

    for cat in cats:
        for entry in knowledge_base.get(cat, []):
            title = entry.get("title", "")
            content = entry.get("content", "")
            if query_lower in title.lower() or query_lower in content.lower():
                results.append(f"【{cat_names.get(cat, cat)}】{title}\n{content}")

    if not results:
        return f"知識庫中沒有找到「{query}」的相關資料。可以用 /kb 指令新增。"
    return "\n\n---\n\n".join(results[:5])


def do_add_to_knowledge_base(category: str, title: str, content: str) -> str:
    if category not in knowledge_base:
        knowledge_base[category] = []
    entry = {
        "title": title,
        "content": content,
        "created": datetime.now().strftime("%Y-%m-%d %H:%M")
    }
    # 更新已有同名條目
    for i, e in enumerate(knowledge_base[category]):
        if e.get("title") == title:
            knowledge_base[category][i] = entry
            save_kb()
            return f"已更新知識庫【{category}】：{title}"
    knowledge_base[category].append(entry)
    save_kb()
    return f"已新增到知識庫【{category}】：{title}"


async def computer_use_task_impl(task: str, url: str = "") -> str:
    if not MAC_SERVICE_URL:
        return "Mac Computer Use 服務未設定。"
    try:
        async with httpx.AsyncClient() as client:
            payload = {"task": task}
            if url:
                payload["url"] = url
            resp = await client.post(f"{MAC_SERVICE_URL}/execute", json=payload, timeout=120)
            if resp.status_code == 200:
                return resp.json().get("result", "執行完成（無詳細結果）")
            return f"Mac 服務回應錯誤（HTTP {resp.status_code}）"
    except httpx.ConnectError:
        return "無法連接到 Mac 服務，Cloudflare tunnel 可能已過期"
    except Exception as e:
        return f"執行失敗：{str(e)}"


async def convert_currency_impl(amount: float, from_cur: str, to_cur: str) -> str:
    from_cur = from_cur.upper()
    to_cur = to_cur.upper()
    try:
        async with httpx.AsyncClient() as client:
            resp = await client.get(
                f"https://open.er-api.com/v6/latest/{from_cur}",
                timeout=10
            )
            if resp.status_code != 200:
                return f"匯率查詢失敗（HTTP {resp.status_code}）"
            data = resp.json()
            if data.get("result") != "success":
                return f"不支援的幣別：{from_cur}"
            rate = data.get("rates", {}).get(to_cur)
            if not rate:
                return f"不支援的目標幣別：{to_cur}"
            converted = amount * rate
            # 格式化數字
            if to_cur == "VND":
                result_str = f"{converted:,.0f}"
            elif converted >= 100:
                result_str = f"{converted:,.0f}"
            else:
                result_str = f"{converted:,.2f}"
            amount_str = f"{amount:,.0f}" if amount >= 100 else f"{amount:,.2f}"
            return f"{amount_str} {from_cur} = {result_str} {to_cur}\n匯率：1 {from_cur} = {rate:,.4f} {to_cur}\n（資料來源：ExchangeRate API）"
    except Exception as e:
        return f"匯率查詢失敗：{str(e)}"


def do_delete_reminder(index: int) -> str:
    if not reminders:
        return "目前沒有待辦可刪除"
    if index < 1 or index > len(reminders):
        return f"編號無效，目前有 {len(reminders)} 筆待辦（輸入 1-{len(reminders)}）"
    removed = reminders.pop(index - 1)
    save_data()
    return f"已完成/刪除：{removed['text']}"


def do_send_email(to: str, subject: str, body: str, confirmed: bool) -> str:
    if not confirmed:
        return f"等待確認。信件草稿：\n收件人：{to}\n主旨：{subject}\n\n{body}\n\n請回覆「確認發送」或「不用了」"
    if not GMAIL_ADDRESS or not GMAIL_APP_PASSWORD:
        return "Gmail 未設定"
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
        return "Gmail 未設定"
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
    entry = {"text": text, "time_hint": time_hint, "created": datetime.now().strftime("%Y-%m-%d %H:%M")}
    reminders.append(entry)
    save_data()
    hint_str = f"（{time_hint}）" if time_hint else ""
    return f"已記住{hint_str}：{text}"


def do_list_reminders() -> str:
    if not reminders:
        return "目前沒有待辦提醒"
    lines = [f"{i+1}. {r['text']}" + (f"（{r['time_hint']}）" if r.get('time_hint') else "") + f" — 記於 {r['created']}"
             for i, r in enumerate(reminders)]
    return "待辦提醒清單：\n" + "\n".join(lines)


def check_important_unread_emails() -> list[dict]:
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
        for num in ids[-30:]:
            _, msg_data = mail.fetch(num, "(RFC822)")
            msg = email_lib.message_from_bytes(msg_data[0][1])
            subject_raw = decode_header(msg["Subject"] or "")[0]
            subject = subject_raw[0].decode(subject_raw[1] or "utf-8") if isinstance(subject_raw[0], bytes) else (subject_raw[0] or "")
            email_id = msg.get("Message-ID", str(num))
            if email_id in seen_email_ids:
                continue
            if not any(kw.lower() in subject.lower() for kw in IMPORTANT_EMAIL_KEYWORDS):
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
            important.append({"id": email_id, "from": sender[:60], "subject": subject[:80], "preview": body})
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
    elif tool_name == "get_calendar":
        return do_get_calendar(tool_input.get("days", 7))
    elif tool_name == "add_calendar_event":
        return do_add_calendar_event(
            tool_input["title"], tool_input["start_datetime"], tool_input["end_datetime"],
            tool_input.get("description", ""), tool_input.get("location", "")
        )
    elif tool_name == "search_knowledge_base":
        return do_search_knowledge_base(tool_input["query"], tool_input.get("category", ""))
    elif tool_name == "add_to_knowledge_base":
        return do_add_to_knowledge_base(tool_input["category"], tool_input["title"], tool_input["content"])
    elif tool_name == "computer_use_task":
        return await computer_use_task_impl(tool_input["task"], tool_input.get("url", ""))
    elif tool_name == "convert_currency":
        return await convert_currency_impl(tool_input["amount"], tool_input["from_currency"], tool_input["to_currency"])
    elif tool_name == "delete_reminder":
        return do_delete_reminder(tool_input["index"])
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

    for _ in range(3):
        response = await anthropic_client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=1200,
            system=[{"type": "text", "text": build_system_prompt(), "cache_control": {"type": "ephemeral"}}],
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


# ── Flex Message 建構 ─────────────────────────────────

def build_quick_reply():
    """常用快捷按鈕，顯示在訊息底部"""
    return {
        "items": [
            {"type": "action", "action": {"type": "message", "label": "查行程", "text": "/行程"}},
            {"type": "action", "action": {"type": "message", "label": "查天氣", "text": "今天天氣"}},
            {"type": "action", "action": {"type": "message", "label": "查郵件", "text": "有沒有新郵件"}},
            {"type": "action", "action": {"type": "message", "label": "待辦清單", "text": "/提醒"}},
            {"type": "action", "action": {"type": "message", "label": "VND匯率", "text": "100萬越南盾等於多少台幣"}},
        ]
    }


def build_flex_info_card(title: str, subtitle: str, items: list[dict], color: str = "#1DB446") -> dict:
    """通用資訊卡片 Flex Message
    items: [{"label": "...", "value": "..."}]
    """
    body_contents = [
        {"type": "text", "text": title, "weight": "bold", "size": "xl", "color": color},
        {"type": "text", "text": subtitle, "size": "xs", "color": "#aaaaaa", "margin": "md"},
        {"type": "separator", "margin": "lg"},
    ]
    for item in items[:10]:
        body_contents.append({
            "type": "box", "layout": "horizontal", "margin": "md",
            "contents": [
                {"type": "text", "text": item["label"], "size": "sm", "color": "#555555", "flex": 0},
                {"type": "text", "text": str(item["value"]), "size": "sm", "color": "#111111", "align": "end"}
            ]
        })
    return {
        "type": "flex", "altText": title,
        "contents": {
            "type": "bubble",
            "body": {"type": "box", "layout": "vertical", "contents": body_contents}
        }
    }


def build_flex_list_card(title: str, items: list[str], color: str = "#1976D2") -> dict:
    """清單式卡片"""
    body_contents = [
        {"type": "text", "text": title, "weight": "bold", "size": "lg", "color": color},
        {"type": "separator", "margin": "md"},
    ]
    for item in items[:15]:
        body_contents.append(
            {"type": "text", "text": item, "size": "sm", "color": "#333333", "margin": "sm", "wrap": True}
        )
    return {
        "type": "flex", "altText": title,
        "contents": {
            "type": "bubble", "size": "mega",
            "body": {"type": "box", "layout": "vertical", "contents": body_contents}
        }
    }


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


async def send_line_reply(reply_token: str, text: str, flex: dict = None, quick_reply: bool = True):
    """發送 LINE 回覆，支援 Flex Message + Quick Reply"""
    if flex:
        msg = flex
    else:
        msg = {"type": "text", "text": text}
    if quick_reply:
        msg["quickReply"] = build_quick_reply()
    async with httpx.AsyncClient() as client:
        await client.post("https://api.line.me/v2/bot/message/reply",
            headers={"Content-Type": "application/json", "Authorization": f"Bearer {LINE_CHANNEL_ACCESS_TOKEN}"},
            json={"replyToken": reply_token, "messages": [msg]}, timeout=10)


async def send_line_push(user_id: str, text: str, flex: dict = None):
    """發送 LINE 主動推播，支援 Flex Message"""
    if flex:
        msg = flex
    else:
        msg = {"type": "text", "text": text}
    async with httpx.AsyncClient() as client:
        await client.post("https://api.line.me/v2/bot/message/push",
            headers={"Content-Type": "application/json", "Authorization": f"Bearer {LINE_CHANNEL_ACCESS_TOKEN}"},
            json={"to": user_id, "messages": [msg]}, timeout=10)


# ── 自動排程任務 ───────────────────────────────────────

async def scheduled_morning_briefing():
    if not known_user_ids:
        return
    today = datetime.now().strftime("%Y-%m-%d %A")
    hcmc_weather, taipei_weather, exchange_rate = await asyncio.gather(
        get_weather_impl("Ho Chi Minh City"),
        get_weather_impl("Taipei"),
        convert_currency_impl(1000000, "VND", "TWD")
    )
    calendar_info = do_get_calendar(1)
    email_summary = do_read_emails(3)
    reminder_text = do_list_reminders()

    briefing_prompt = f"""今天是 {today}。

胡志明市天氣：{hcmc_weather}

台北天氣：{taipei_weather}

匯率：{exchange_rate}

今日行程：{calendar_info}

最新郵件（最近3封）：{email_summary}

待辦提醒：{reminder_text}

請給 Aaron 一個晨報，格式：
・今日天氣（兩城市一行搞定）
・匯率（100萬越南盾 = ?台幣，一行搞定）
・今天有沒有重要行程
・最重要的 1-2 個待辦
・郵件有重要的才寫，沒有就不寫
・一句今日重點提醒

繁體中文，像朋友傳訊息，不用 Markdown，條列用「・」，不超過15行。"""

    response = await anthropic_client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=400,
        messages=[{"role": "user", "content": briefing_prompt}]
    )
    briefing = response.content[0].text
    for uid in known_user_ids:
        await send_line_push(uid, briefing)
    print(f"[scheduler] 晨報已推播給 {len(known_user_ids)} 人")


async def scheduled_evening_summary():
    if not known_user_ids:
        return
    if not daily_log:
        for uid in known_user_ids:
            await send_line_push(uid, "今天沒有對話紀錄，一切平靜。")
        return

    log_text = "\n".join([f"[{i['time']}] Aaron：{i['user']}\n助理：{i['reply']}" for i in daily_log])
    date_str = datetime.now().strftime("%Y-%m-%d")
    response = await anthropic_client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=300,
        messages=[{"role": "user", "content": f"以下是 {date_str} 的 LINE 對話，請用繁體中文摘要 3-5 個重點（決策、待辦、重要資訊、結論）。不用 Markdown，條列用「・」。沒重要內容就寫「今日無重要事項」。\n\n{log_text}"}]
    )
    summary = response.content[0].text
    daily_log.clear()
    msg = f"今日摘要（{date_str}）\n\n{summary}"
    for uid in known_user_ids:
        await send_line_push(uid, msg)
    print(f"[scheduler] 晚報已推播")


async def scheduled_proactive_check():
    if not known_user_ids:
        return
    important_emails = check_important_unread_emails()
    for em in important_emails:
        if em["id"] not in seen_email_ids:
            msg = f"重要郵件提醒\n\n寄件人：{em['from']}\n主旨：{em['subject']}\n\n{em['preview'][:200]}"
            for uid in known_user_ids:
                await send_line_push(uid, msg)
            seen_email_ids.add(em["id"])
    if important_emails:
        save_data()


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
            save_data()

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

                # /kb 知識庫指令
                if user_text.startswith("/kb ") or user_text.startswith("/知識庫 "):
                    parts = user_text.split(" ", 3)
                    # 格式：/kb [category] [title] [content]
                    # 或：/kb list [category]
                    if len(parts) >= 2 and parts[1] == "list":
                        cat = parts[2] if len(parts) > 2 else ""
                        reply = do_search_knowledge_base("", cat) if cat else "\n".join(
                            [f"【{c}】{len(v)} 筆" for c, v in knowledge_base.items()]
                        )
                    elif len(parts) >= 4:
                        reply = do_add_to_knowledge_base(parts[1], parts[2], parts[3])
                    else:
                        reply = "用法：\n/kb [clinic/herbalife/contacts/general] [標題] [內容]\n/kb list [分類]"
                    await send_line_reply(reply_token, reply)
                    continue

                if user_text.startswith("/記住") or user_text.startswith("/remember"):
                    content = user_text.replace("/記住", "").replace("/remember", "").strip()
                    ok = save_to_memory(content) if content else False
                    reply = "已記住。" if ok else "用法：/記住 [要記的內容]"
                    await send_line_reply(reply_token, reply)
                    continue

                if user_text.strip() in ("/清除", "/clear"):
                    conversation_history.pop(user_id, None)
                    await send_line_reply(reply_token, "對話記錄已清除")
                    continue

                if user_text.strip() in ("/help", "/指令", "/說明"):
                    help_items = [
                        "・/行程 — 查看未來7天行程",
                        "・/提醒 — 查看所有待辦",
                        "・/匯率 — VND/TWD/USD 即時匯率",
                        "・/kb list — 查看知識庫",
                        "・/kb [分類] [標題] [內容] — 新增知識庫",
                        "・/記住 [內容] — 記到長期記憶",
                        "・/清除 — 清除對話記錄",
                        "",
                        "直接打字問我任何事，不用指令也行。",
                    ]
                    flex = build_flex_list_card("JARVIS 指令列表", help_items, "#607D8B")
                    await send_line_reply(reply_token, "\n".join(help_items), flex=flex)
                    continue

                if user_text.strip() in ("/提醒", "/reminders"):
                    reminder_text = do_list_reminders()
                    if reminders:
                        items = [f"{i+1}. {r['text']}" + (f"（{r['time_hint']}）" if r.get('time_hint') else "")
                                 for i, r in enumerate(reminders)]
                        flex = build_flex_list_card("待辦提醒", items, "#FF6B00")
                        await send_line_reply(reply_token, reminder_text, flex=flex)
                    else:
                        await send_line_reply(reply_token, reminder_text)
                    continue

                if user_text.strip() in ("/行程", "/calendar"):
                    cal_text = do_get_calendar(7)
                    lines = [l.strip() for l in cal_text.split("\n") if l.strip().startswith("・")]
                    if lines:
                        flex = build_flex_list_card("未來 7 天行程", lines, "#1976D2")
                        await send_line_reply(reply_token, cal_text, flex=flex)
                    else:
                        await send_line_reply(reply_token, cal_text)
                    continue

                if user_text.strip() in ("/匯率", "/rate"):
                    rate_info = await convert_currency_impl(1000000, "VND", "TWD")
                    usd_info = await convert_currency_impl(1, "USD", "TWD")
                    flex = build_flex_info_card("即時匯率", datetime.now().strftime("%Y-%m-%d %H:%M"), [
                        {"label": "100萬 VND", "value": rate_info.split("=")[1].split("\n")[0].strip() if "=" in rate_info else "查詢失敗"},
                        {"label": "1 USD", "value": usd_info.split("=")[1].split("\n")[0].strip() if "=" in usd_info else "查詢失敗"},
                    ], color="#4CAF50")
                    await send_line_reply(reply_token, f"{rate_info}\n\n{usd_info}", flex=flex)
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
        "gmail_ready": bool(GMAIL_ADDRESS and GMAIL_APP_PASSWORD),
        "gcal_ready": bool(GOOGLE_SERVICE_ACCOUNT_JSON),
        "groq_ready": bool(GROQ_API_KEY),
        "brave_ready": bool(BRAVE_API_KEY),
        "mac_ready": bool(MAC_SERVICE_URL),
        "ffmpeg_ready": shutil.which("ffmpeg") is not None,
        "known_users": len(known_user_ids),
        "reminders": len(reminders),
        "kb_entries": {k: len(v) for k, v in knowledge_base.items()},
        "scheduler_running": scheduler.running if scheduler else False
    }


@app.get("/morning-briefing")
async def morning_briefing(secret: str = ""):
    if not SYNC_SECRET or secret != SYNC_SECRET:
        raise HTTPException(status_code=403, detail="Forbidden")
    await scheduled_morning_briefing()
    return {"status": "sent", "recipients": len(known_user_ids)}


@app.get("/proactive-check")
async def proactive_check(secret: str = ""):
    if not SYNC_SECRET or secret != SYNC_SECRET:
        raise HTTPException(status_code=403, detail="Forbidden")
    await scheduled_proactive_check()
    return {"status": "ok"}


@app.get("/daily-summary")
async def daily_summary(secret: str = ""):
    if not SYNC_SECRET or secret != SYNC_SECRET:
        raise HTTPException(status_code=403, detail="Forbidden")
    await scheduled_evening_summary()
    return {"status": "sent", "recipients": len(known_user_ids)}


# ── 啟動 ──────────────────────────────────────────────

scheduler = None

@app.on_event("startup")
async def startup():
    global scheduler
    scheduler = AsyncIOScheduler(timezone="Asia/Ho_Chi_Minh")
    scheduler.add_job(scheduled_morning_briefing, CronTrigger(hour=8, minute=0))
    scheduler.add_job(scheduled_evening_summary, CronTrigger(hour=22, minute=0))
    scheduler.add_job(scheduled_proactive_check, "interval", minutes=30)
    # 恢復對話記錄
    if not LINE_LOG_PATH.exists():
        try:
            cloud_log = gdrive_download("line-conversations.md")
            if cloud_log:
                LINE_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
                LINE_LOG_PATH.write_text(cloud_log, encoding="utf-8")
                print("[startup] 從 Google Drive 恢復 line-conversations.md")
        except Exception:
            pass

    scheduler.start()
    gdrive_status = "已連接" if GOOGLE_DRIVE_FOLDER_ID and GOOGLE_SERVICE_ACCOUNT_JSON else "未設定"
    print(f"[startup] JARVIS 啟動完成，已知用戶：{len(known_user_ids)} 人，知識庫：{sum(len(v) for v in knowledge_base.values())} 筆，GDrive：{gdrive_status}")


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8000)
