import os
import time
import json
import logging
import requests
import threading
import subprocess
from typing import Optional, Dict, Any
from dotenv import load_dotenv

load_dotenv()

logger = logging.getLogger(__name__)

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")
API_BASE_URL = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}" if TELEGRAM_BOT_TOKEN else ""


def is_configured() -> bool:
    """Verifica se il bot Telegram è configurato con token e chat id."""
    return bool(TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID)


def send_telegram_message(text: str, chat_id: Optional[str] = None, parse_mode: str = "HTML") -> bool:
    """
    Invia un messaggio Telegram formattato in HTML.
    Utilizza HTML come default per evitare conflitti con caratteri speciali (come underscore o parentesi).
    """
    if not TELEGRAM_BOT_TOKEN:
        return False

    target_chat_id = chat_id or TELEGRAM_CHAT_ID
    if not target_chat_id:
        logger.warning("[Telegram] Impossibile inviare: TELEGRAM_CHAT_ID non specificato.")
        return False

    url = f"{API_BASE_URL}/sendMessage"
    payload = {
        "chat_id": target_chat_id,
        "text": text,
        "parse_mode": parse_mode,
        "disable_web_page_preview": True,
    }

    try:
        resp = requests.post(url, json=payload, timeout=10)
        res_json = resp.json()
        if not res_json.get("ok"):
            logger.warning(f"[Telegram] Errore API: {res_json.get('description')}")
            return False
        return True
    except Exception as e:
        logger.warning(f"[Telegram] Eccezione invio messaggio: {e}")
        return False


def notify_cycle_result(
    decision: Dict[str, Any],
    execution_result: Any,
    account_status: Optional[Dict[str, Any]] = None,
    sentiment: Optional[Dict[str, Any]] = None,
    indicators: Optional[Any] = None,
    forecasts: Optional[Any] = None,
):
    """Notifica il risultato dell'ultimo ciclo di trading e la decisione dell'AI."""
    if not is_configured():
        return

    op = str(decision.get("operation", "hold")).upper()
    sym = str(decision.get("token") or decision.get("symbol") or "").upper()
    portion = decision.get("target_portion_of_portfolio", 0)
    sell_portion = decision.get("sell_portion", 0)
    reason = decision.get("reason", "Nessuna motivazione salvata.")

    if op == "BUY":
        action_title = f"🟢 <b>ACQUISTO: {sym}</b>"
    elif op == "SELL":
        action_title = f"🔴 <b>VENDITA: {sym}</b>"
    else:
        action_title = "⏸️ <b>DECISIONE: HOLD (nessuna operazione)</b>"

    lines = [
        "🤖 <b>Degen Trading Agent • Report Ciclo</b>",
        "━━━━━━━━━━━━━━━━━━━━━━",
        action_title,
    ]

    if account_status and account_status.get("paper_trading"):
        pnl = account_status.get("pnl_since_start_usd", 0.0)
        lines.append(f"📝 <i>PAPER TRADING — P&L strategia ${pnl:+.2f}</i>")
    elif account_status and account_status.get("dry_run"):
        lines.append("🧪 <i>DRY-RUN: nessuna transazione firmata</i>")

    if op in ("BUY", "SELL"):
        if op == "BUY":
            lines.append(f"📊 <b>Allocazione:</b> {float(portion) * 100:.1f}% del portafoglio")
        else:
            lines.append(f"📊 <b>Quota venduta:</b> {float(sell_portion) * 100:.0f}%")
        lines.append(f"💧 <b>Slippage max:</b> {decision.get('slippage_bps', '--')} bps")

        if execution_result:
            status = str(execution_result.get("status", "")).lower()
            if status == "rejected":
                lines.append(f"🛑 <b>Rifiutato dai limiti:</b> {execution_result.get('message', '')}")
            elif status == "error":
                lines.append(f"❌ <b>Errore:</b> {execution_result.get('message', '')}")
            else:
                if execution_result.get("route_description"):
                    lines.append(f"🔀 <b>Rotta:</b> <code>{execution_result['route_description']}</code>")
                if execution_result.get("explorer"):
                    lines.append(f"🔗 <a href=\"{execution_result['explorer']}\">Transazione su BaseScan</a>")

    # Stato del wallet
    if account_status:
        total = account_status.get("total_value_usd", 0.0)
        usdc = account_status.get("usdc_balance", 0.0)
        positions = account_status.get("open_positions", [])
        lines.append("")
        lines.append(f"💰 <b>Portafoglio:</b> ${float(total):.2f} (USDC liberi ${float(usdc):.2f})")
        lines.append(f"🪙 <b>Token detenuti:</b> {len(positions)}")
        for p in positions[:5]:
            pnl = p.get("pnl_percent")
            pnl_txt = f" ({pnl:+.1f}%)" if pnl is not None else ""
            lines.append(f"   • {p.get('symbol')}: ${p.get('value_usd', 0):.2f}{pnl_txt}")

    # Sentiment
    if sentiment:
        val = sentiment.get("valore", sentiment.get("value", "--"))
        cls_name = sentiment.get("classificazione", sentiment.get("classification", ""))
        lines.append(f"🎭 <b>Fear & Greed:</b> {val}/100 ({cls_name})")

    # Spiegazione AI
    lines.append("")
    lines.append("🧠 <b>AI Rationale:</b>")
    lines.append(f"<i>{reason}</i>")
    lines.append("━━━━━━━━━━━━━━━━━━━━━━")

    msg = "\n".join(lines)
    send_telegram_message(msg)


def notify_error(error_msg: str):
    """Invia un avviso in caso di errore durante l'esecuzione del ciclo."""
    if not is_configured():
        return

    text = (
        "⚠️ <b>Allarme Degen Trading Agent</b>\n"
        "━━━━━━━━━━━━━━━━━━━━━━\n"
        f"Si è verificato un errore durante il ciclo di trading:\n"
        f"<code>{error_msg}</code>\n"
        "━━━━━━━━━━━━━━━━━━━━━━\n"
        "<i>Il bot riproverà automaticamente al prossimo intervallo schedulato.</i>"
    )
    send_telegram_message(text)


# ==============================================================================
# TELEGRAM BOT POLLING LISTENER (COMANDI INTERATTIVI)
# ==============================================================================

def _handle_command(text: str, chat_id: str):
    """Elabora i comandi inviati dall'utente al bot Telegram."""
    cmd = text.strip().split()[0].lower()

    if cmd in ["/start", "/help"]:
        help_msg = (
            "🤖 <b>Degen Trading Agent Bot</b>\n\n"
            "Ecco i comandi disponibili:\n"
            "• /status - Stato del bot, saldo e posizioni attive\n"
            "• /positions - Dettaglio delle posizioni aperte e PnL\n"
            "• /run - Forza l'esecuzione immediata di un ciclo di trading\n"
            "• /sentiment - Indice Fear & Greed attuale\n"
            "• /signals - Ultimo segnale e motivazione dell'AI\n"
            "• /help - Mostra questo messaggio di aiuto\n\n"
            "<i>Il bot opera automaticamente ogni 15 minuti sul wallet (Base, Uniswap V3).</i>"
        )
        send_telegram_message(help_msg, chat_id=chat_id)

    elif cmd == "/status":
        try:
            from dashboard import get_db_data
            data = get_db_data()
            bal = data.get("balance", 0.0)
            pos_count = len(data.get("positions", []))
            sent = data.get("sentiment", {})
            sent_val = sent.get("value", "--")
            sent_cls = sent.get("classification", "")
            last_op = data.get("operations", [{}])[0] if data.get("operations") else {}
            op_name = last_op.get("operation", "Nessuna").upper()
            op_sym = last_op.get("symbol", "")

            msg = (
                "📊 <b>Stato Trading Agent</b>\n"
                "━━━━━━━━━━━━━━━━━━━━━━\n"
                f"💰 <b>Saldo:</b> ${bal:.2f} USDC\n"
                f"📈 <b>Posizioni Aperte:</b> {pos_count}\n"
                f"🎭 <b>Sentiment:</b> {sent_val}/100 ({sent_cls})\n"
                f"⚡ <b>Ultima Operazione:</b> {op_name} {op_sym}\n"
                f"🕒 <b>Data:</b> {last_op.get('created_at', '--')}\n"
                "━━━━━━━━━━━━━━━━━━━━━━"
            )
            send_telegram_message(msg, chat_id=chat_id)
        except Exception as e:
            send_telegram_message(f"❌ Errore recupero stato: {e}", chat_id=chat_id)

    elif cmd == "/positions":
        try:
            from dashboard import get_db_data
            data = get_db_data()
            positions = data.get("positions", [])
            if not positions:
                send_telegram_message("ℹ️ Nessun token in portafoglio al momento.", chat_id=chat_id)
                return

            lines = ["📊 <b>Token in portafoglio (Base):</b>", "━━━━━━━━━━━━━━━━━━━━━━"]
            for p in positions:
                pnl = p.get("pnl_percent")
                pnl_icon = "🟢" if (pnl or 0) >= 0 else "🔴"
                pnl_txt = f"{pnl:+.1f}%" if pnl is not None else "n/d"
                lines.append(
                    f"{pnl_icon} <b>{p.get('symbol')}</b>\n"
                    f"   Valore: ${p.get('value_usd', 0):.2f} | P/L: {pnl_txt}\n"
                    f"   Quantita': {p.get('amount')}\n"
                )

            send_telegram_message("\n".join(lines), chat_id=chat_id)
        except Exception as e:
            send_telegram_message(f"❌ Errore lettura posizioni: {e}", chat_id=chat_id)

    elif cmd == "/sentiment":
        try:
            from sentiment import get_latest_fear_and_greed
            sent = get_latest_fear_and_greed()
            val = sent.get("valore", 50)
            cls_name = sent.get("classificazione", "Neutral")
            icon = "🟢" if val >= 60 else ("🔴" if val <= 40 else "🟡")
            msg = (
                f"🎭 <b>Crypto Fear & Greed Index</b>\n\n"
                f"{icon} <b>Punteggio:</b> {val} / 100\n"
                f"🏷️ <b>Classificazione:</b> {cls_name.upper()}\n"
                f"📡 <i>Fonte: Alternative.me (free)</i>"
            )
            send_telegram_message(msg, chat_id=chat_id)
        except Exception as e:
            send_telegram_message(f"❌ Errore sentiment: {e}", chat_id=chat_id)

    elif cmd == "/signals":
        try:
            from dashboard import get_db_data
            data = get_db_data()
            ops = data.get("operations", [])
            if not ops:
                send_telegram_message("ℹ️ Nessun segnale salvato.", chat_id=chat_id)
                return

            last = ops[0]
            reason = "Nessuna spiegazione salvata."
            try:
                payload = json.loads(last.get("raw_payload", "{}"))
                reason = payload.get("reason", reason)
            except Exception:
                pass

            msg = (
                "🧠 <b>Ultimo Segnale AI</b>\n"
                "━━━━━━━━━━━━━━━━━━━━━━\n"
                f"📍 <b>Azione:</b> {last.get('operation', '').upper()} {last.get('symbol', '')}\n"
                f"📊 <b>Allocazione:</b> {last.get('target_portion_of_portfolio', '--')} | "
                f"<b>Slippage:</b> {last.get('slippage_bps', '--')} bps\n"
                f"🕒 <b>Registrato:</b> {last.get('created_at', '--')}\n\n"
                f"💡 <b>Ragionamento:</b>\n<i>{reason}</i>\n"
                "━━━━━━━━━━━━━━━━━━━━━━"
            )
            send_telegram_message(msg, chat_id=chat_id)
        except Exception as e:
            send_telegram_message(f"❌ Errore segnali: {e}", chat_id=chat_id)

    elif cmd == "/run":
        send_telegram_message("⚡ <b>Avvio ciclo di trading...</b>\nL'AI sta analizzando i mercati e riceverai il report a breve!", chat_id=chat_id)
        def _run_script():
            subprocess.run(["python", "main.py"], check=False)
        threading.Thread(target=_run_script, daemon=True).start()

    else:
        send_telegram_message("Comando non riconosciuto. Usa /help per la lista comandi.", chat_id=chat_id)


def run_telegram_listener():
    """
    Loop continuo di polling per ascoltare i messaggi in arrivo.
    Eseguibile in background nel container o come processo separato.
    """
    if not TELEGRAM_BOT_TOKEN:
        logger.info("[Telegram] TELEGRAM_BOT_TOKEN non impostato. Listener disattivato.")
        return

    print("📱 Telegram Bot Listener avviato. In attesa di comandi...")
    offset = 0

    while True:
        try:
            url = f"{API_BASE_URL}/getUpdates"
            params = {"offset": offset, "timeout": 25}
            resp = requests.get(url, params=params, timeout=35)
            data = resp.json()

            if data.get("ok"):
                for update in data.get("result", []):
                    offset = update["update_id"] + 1
                    msg = update.get("message")
                    if msg and "text" in msg:
                        text = msg["text"]
                        chat_id = str(msg["chat"]["id"])
                        _handle_command(text, chat_id)
            else:
                time.sleep(3)
        except Exception as e:
            # Attendi qualche secondo prima di riprovare in caso di disconnessione di rete
            time.sleep(5)


if __name__ == "__main__":
    if not TELEGRAM_BOT_TOKEN:
        print("⚠️ TELEGRAM_BOT_TOKEN non configurato nel .env. Inserisci il token per avviare il bot.")
    else:
        run_telegram_listener()
