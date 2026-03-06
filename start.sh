#!/bin/bash
# LINE Claude Bot 啟動腳本
cd "$(dirname "$0")"

# 載入環境變數
if [ -f .env ]; then
  export $(grep -v '^#' .env | xargs)
fi

# 啟動 ngrok（背景）
echo "🔗 啟動 ngrok tunnel..."
pkill -f ngrok 2>/dev/null
ngrok http 8000 --log=stdout > /tmp/ngrok.log 2>&1 &
sleep 2

# 取得 ngrok URL
NGROK_URL=$(curl -s http://localhost:4040/api/tunnels | python3 -c "import sys,json; print(json.load(sys.stdin)['tunnels'][0]['public_url'])" 2>/dev/null)
echo "✅ ngrok URL: $NGROK_URL"
echo ""
echo "📱 請把以下網址貼到 LINE Developers Webhook URL："
echo "   $NGROK_URL/webhook"
echo ""

# 啟動 server
echo "🚀 啟動 Claude Bot server..."
python3 -m uvicorn main:app --host 0.0.0.0 --port 8000 --reload
