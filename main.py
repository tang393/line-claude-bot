#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
LINE Claude Bot - Aaron 的全知全能手機助理
連接 LINE Messaging API + Claude API + MEMORY.md + Brave Search
"""

import os
import json
import hashlib
import hmac
import base64
from pathlib import Path
from datetime import datetime
from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import JSONResponse
import anthropic
import httpx
import uvicorn

app = FastAPI()

# ── 設定 ──────────────────────────────────────────────
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
LINE_CHANNEL_SECRET = os.environ.get("LINE_CHANNEL_SECRET", "")
LINE_CHANNEL_ACCESS_TOKEN = os.environ.get("LINE_CHANNEL_ACCESS_TOKEN", "")
BRAVE_API_KEY = os.environ.get("BRAVE_API_KEY", "")

MEMORY_PATH = Path.home() / ".claude/projects/-Users-user/memory/MEMORY.md"
LINE_LOG_PATH = Path.home() / ".claude/projects/-Users-user/memory/line-conversations.md"
MAX_HISTORY = 20  # 每個用戶保留最多幾輪對話

# ── 狀態 ──────────────────────────────────────────────
conversation_history: dict[str, list] = {}  # user_id -> message list
daily_log: list = []  # 當天所有對話記錄，供每日摘要用
anthropic_client = anthropic.AsyncAnthropic(api_key=ANTHROPIC_API_KEY)
SYNC_SECRET = os.environ.get("PASSWORD", "")

# ── 工具定義 ──────────────────────────────────────────
TOOLS = [
    {
        "name": "web_search",
        "description": "搜尋網路上的最新資訊。當需要查詢市場數據、新聞、法規、競品等即時資訊時使用。",
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "搜尋關鍵字"}
            },
            "required": ["query"]
        }
    }
]


def get_memory() -> str:
    # 優先用環境變數（雲端部署用）
    env_memory = os.environ.get("MEMORY_CONTENT", "")
    if env_memory:
        content = env_memory
    else:
        try:
            content = MEMORY_PATH.read_text(encoding="utf-8")
        except Exception:
            return "（記憶檔案無法讀取）"
    # 附加 LINE 對話記錄（本地模式才有）
    if LINE_LOG_PATH.exists():
        log = LINE_LOG_PATH.read_text(encoding="utf-8")
        if log.strip():
            content += f"\n\n# LINE 對話記錄（最近重要事項）\n{log}"
    return content


def save_to_memory(content: str):
    """將重要內容追加寫入 LINE 對話記錄"""
    try:
        LINE_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M")
        entry = f"\n## {timestamp}\n{content}\n"
        with open(LINE_LOG_PATH, "a", encoding="utf-8") as f:
            f.write(entry)
        return True
    except Exception:
        return False


def build_system_prompt() -> str:
    memory = get_memory()
    today = datetime.now().strftime("%Y-%m-%d")
    return f"""你是 Aaron（湯凱賀）的全知全能商業助理，透過 LINE 接收訊息。

今天日期：{today}

以下是關於 Aaron 的完整背景資料（MEMORY.md）：
{memory}

## 行為規則
- 永遠用繁體中文回覆
- 簡潔直接，不廢話
- 遇到問題先自己找解法，不把問題丟回給 Aaron
- 商業建議要有具體執行步驟
- 主動提示風險與下一步
- 若不確定最新資訊（法規、市場數據），主動使用 web_search 工具查詢

## 回覆風格（非常重要）
- 絕對禁止使用 * ** # ` 等符號，LINE 不會渲染，會直接顯示成亂碼
- 要強調就用全形空格縮排或換行，不要用任何 Markdown
- 說話直接、精簡，像朋友發訊息，不是客服機器人
- 不要說「當然可以」「很好的問題」這類廢話
- 回覆短的就短，不要為了顯得有料而拉長
- 條列用「・」或數字加點，不用「-」或「*」"""


async def brave_search(query: str) -> str:
    url = "https://api.search.brave.com/res/v1/web/search"
    headers = {"Accept": "application/json", "X-Subscription-Token": BRAVE_API_KEY}
    params = {"q": query, "count": 5}
    async with httpx.AsyncClient() as client:
        resp = await client.get(url, headers=headers, params=params, timeout=10)
        if resp.status_code != 200:
            return f"搜尋失敗（{resp.status_code}）"
        data = resp.json()
        results = data.get("web", {}).get("results", [])
        if not results:
            return "沒有找到相關結果"
        lines = []
        for r in results[:5]:
            lines.append(f"• {r.get('title','')}\n  {r.get('description','')}\n  {r.get('url','')}")
        return "\n\n".join(lines)


async def process_tool_call(tool_name: str, tool_input: dict) -> str:
    if tool_name == "web_search":
        return await brave_search(tool_input["query"])
    return "未知工具"


async def chat_with_claude(user_id: str, user_message: str) -> str:
    """呼叫 Claude API，支援對話歷史 + 工具"""
    # 初始化歷史
    if user_id not in conversation_history:
        conversation_history[user_id] = []

    conversation_history[user_id].append({
        "role": "user",
        "content": user_message
    })

    # 限制歷史長度
    if len(conversation_history[user_id]) > MAX_HISTORY * 2:
        conversation_history[user_id] = conversation_history[user_id][-MAX_HISTORY * 2:]

    messages = conversation_history[user_id].copy()
    system = build_system_prompt()

    # 最多跑 5 輪工具呼叫
    for _ in range(5):
        response = await anthropic_client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=2000,
            system=system,
            tools=TOOLS,
            messages=messages
        )

        if response.stop_reason == "end_turn":
            # 取得文字回覆
            text = ""
            for block in response.content:
                if hasattr(block, "text"):
                    text += block.text
            conversation_history[user_id].append({
                "role": "assistant",
                "content": text
            })
            return text[:4999]  # LINE 單則上限

        elif response.stop_reason == "tool_use":
            # 執行工具
            messages.append({"role": "assistant", "content": response.content})
            tool_results = []
            for block in response.content:
                if block.type == "tool_use":
                    result = await process_tool_call(block.name, block.input)
                    tool_results.append({
                        "type": "tool_result",
                        "tool_use_id": block.id,
                        "content": result
                    })
            messages.append({"role": "user", "content": tool_results})
        else:
            break

    return "（處理逾時，請再試一次）"


# ── LINE Webhook ──────────────────────────────────────

def verify_signature(body: bytes, signature: str) -> bool:
    if not LINE_CHANNEL_SECRET:
        return True  # 測試模式
    hash_val = hmac.new(
        LINE_CHANNEL_SECRET.encode("utf-8"),
        body,
        hashlib.sha256
    ).digest()
    expected = base64.b64encode(hash_val).decode("utf-8")
    return hmac.compare_digest(expected, signature)


async def send_loading_animation(user_id: str, seconds: int = 30):
    """顯示「回覆中」載入動畫"""
    url = "https://api.line.me/v2/bot/chat/loading/start"
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {LINE_CHANNEL_ACCESS_TOKEN}"
    }
    async with httpx.AsyncClient() as client:
        await client.post(url, headers=headers,
                          json={"chatId": user_id, "loadingSeconds": seconds},
                          timeout=5)


async def send_line_reply(reply_token: str, text: str):
    url = "https://api.line.me/v2/bot/message/reply"
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {LINE_CHANNEL_ACCESS_TOKEN}"
    }
    payload = {
        "replyToken": reply_token,
        "messages": [{"type": "text", "text": text}]
    }
    async with httpx.AsyncClient() as client:
        await client.post(url, headers=headers, json=payload, timeout=10)


@app.post("/webhook")
async def webhook(request: Request):
    body = await request.body()
    signature = request.headers.get("X-Line-Signature", "")

    if not verify_signature(body, signature):
        raise HTTPException(status_code=400, detail="Invalid signature")

    try:
        data = json.loads(body)
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON")

    for event in data.get("events", []):
        if event.get("type") != "message":
            continue
        if event.get("message", {}).get("type") != "text":
            continue

        user_id = event["source"]["userId"]
        user_text = event["message"]["text"]
        reply_token = event["replyToken"]

        try:
            # 指令：/記住 [內容]
            if user_text.startswith("/記住") or user_text.startswith("/remember"):
                content = user_text.replace("/記住", "").replace("/remember", "").strip()
                if content:
                    ok = save_to_memory(content)
                    reply = "✅ 已記住，Claude Code 下次也會知道。" if ok else "❌ 儲存失敗"
                else:
                    reply = "用法：/記住 [要記的內容]"
                await send_line_reply(reply_token, reply)
                continue

            # 指令：/清除 清除對話歷史
            if user_text.strip() in ("/清除", "/clear"):
                conversation_history.pop(user_id, None)
                await send_line_reply(reply_token, "✅ 對話記錄已清除")
                continue

            # 一般對話：先顯示載入動畫，再呼叫 Claude
            await send_loading_animation(user_id, seconds=30)
            reply = await chat_with_claude(user_id, user_text)
            await send_line_reply(reply_token, reply)

            # 記錄到 daily_log
            daily_log.append({
                "time": datetime.now().strftime("%H:%M"),
                "user": user_text,
                "reply": reply[:500]
            })

            # 自動偵測重要資訊（若 Claude 回覆包含決策/待辦/結論，同步記錄）
            important_keywords = ["決定", "確認", "待辦", "簽約", "錄用", "開幕", "結論", "記住"]
            if any(kw in user_text for kw in important_keywords):
                save_to_memory(f"Aaron（LINE）：{user_text}\n助理回覆：{reply[:300]}")

        except Exception as e:
            import traceback
            print(f"[ERROR] user={user_id} msg={user_text[:50]!r}\n{traceback.format_exc()}")
            await send_line_reply(reply_token, f"出錯了：{str(e)[:200]}")

    return JSONResponse({"status": "ok"})


@app.get("/health")
async def health():
    return {"status": "ok", "memory_loaded": MEMORY_PATH.exists(), "daily_log_count": len(daily_log)}


@app.get("/daily-summary")
async def daily_summary(secret: str = ""):
    """每日摘要 endpoint，供 Mac 本地 cron job 呼叫更新 MEMORY.md"""
    if not SYNC_SECRET or secret != SYNC_SECRET:
        raise HTTPException(status_code=403, detail="Forbidden")

    if not daily_log:
        return {"summary": "", "count": 0, "date": datetime.now().strftime("%Y-%m-%d")}

    # 用 Claude 產生摘要
    log_text = "\n".join([
        f"[{item['time']}] Aaron：{item['user']}\n助理：{item['reply']}"
        for item in daily_log
    ])
    date_str = datetime.now().strftime("%Y-%m-%d")

    response = await anthropic_client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=800,
        messages=[{
            "role": "user",
            "content": f"以下是 {date_str} Aaron 與 LINE bot 的對話記錄，請用繁體中文摘要成3-5個重點，格式為條列式，重點包含：決策、待辦、重要資訊、結論。如果沒有重要內容就寫「無重要事項」。\n\n{log_text}"
        }]
    )
    summary = response.content[0].text

    # 清空 daily_log（已摘要）
    daily_log.clear()

    return {"summary": summary, "count": len(log_text), "date": date_str}


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8000)
