#!/bin/bash
set -u

echo "========================================================"
echo "Starting Degen Trading Agent (Base spot) on Docker"
echo "========================================================"

INTERVAL="${TRADING_INTERVAL:-900}"

echo "[1/2] Starting Web Dashboard on port ${PORT:-3000}..."
python dashboard.py &

if [ -n "${TELEGRAM_BOT_TOKEN:-}" ]; then
    echo "📱 Starting Telegram Bot listener..."
    python telegram_bot.py &
fi

echo "[2/2] Starting trading loop (interval: ${INTERVAL}s)..."
if [ "${DRY_RUN:-true}" = "true" ]; then
    echo "🧪 DRY-RUN attivo: nessuna transazione verrà firmata."
fi
echo ""

while true; do
    echo "⏰ [$(date -u +%Y-%m-%dT%H:%M:%SZ)] Running trading cycle..."
    python main.py
    echo "💤 Sleeping for ${INTERVAL} seconds until next cycle..."
    sleep "${INTERVAL}"
done
