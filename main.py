"""
Un ciclo dell'agente: screening, analisi, decisione, esecuzione.

Rispetto al bot perp su SynFutures cambia solo la parte operativa: qui non
esiste un protocollo di terze parti, si comprano e vendono token nel wallet.
"""

import json
import os
import sys

import config
import db_utils
import market_data
import token_screener
from base_client import BaseClient
from forecaster import get_crypto_forecasts
from indicators import analyze_multiple_tickers
from news_feed import fetch_latest_news
from sentiment import get_sentiment
from spot_trader import SpotTrader
from trading_agent import OPENROUTER_MODEL, previsione_trading_agent

# Quanti asset passano per l'analisi tecnica completa (ogni asset costa
# diverse chiamate HTTP: l'universo intero sarebbe sprecato)
MAX_ANALYZED_ASSETS = int(os.getenv("MAX_ANALYZED_ASSETS", "4"))
AUTO_RISK_EXITS = os.getenv("AUTO_RISK_EXITS", "true").strip().lower() in ("1", "true", "yes", "si")


def _assets_to_analyze(universe, holdings):
    """Sempre ETH come bussola del mercato, piu' ciò che teniamo e i top volume."""
    assets = ["ETH"]
    for holding in holdings:
        if holding["symbol"].upper() not in assets:
            assets.append(holding["symbol"].upper())
    for token in universe:
        if len(assets) >= MAX_ANALYZED_ASSETS:
            break
        if token["symbol"].upper() not in assets:
            assets.append(token["symbol"].upper())
    return assets[:MAX_ANALYZED_ASSETS]


def run_cycle():
    if db_utils.is_bot_paused():
        pinfo = db_utils.get_pause_info()
        print(f"⏸️ Bot Degen in stato di PAUSA ({pinfo.get('reason', 'Pausa attiva')}). Ciclo ignorato.")
        return None

    print(f"🚀 Avvio Degen Trading Agent su Base (wallet: {config.WALLET_ADDRESS})")
    if config.PAPER_TRADING:
        print(f"📝 PAPER TRADING: portafoglio virtuale, prezzi e rotte reali. "
              f"Capitale iniziale ${config.PAPER_START_USDC:.2f}.")
    elif config.DRY_RUN:
        print("🧪 DRY-RUN attivo: nessuna transazione verra' firmata.")

    client = BaseClient()
    if not client.is_connected():
        raise RuntimeError(f"RPC Base non raggiungibile: {config.BASE_RPC_URL}")

    problems = client.verify_contracts()
    if problems:
        raise RuntimeError(
            "Verifica dei contratti fallita:\n  - " + "\n  - ".join(problems)
        )
    print(f"⛓️  Connesso a Base (chain {client.chain_id()}), gas {client.gas_price_gwei():.4f} gwei")

    trader = SpotTrader(client=client)

    # 0. Auto-refuel USDC se il saldo e' sotto soglia ma c'e' ETH disponibile
    try:
        refuel_res = trader.ensure_usdc_balance()
        if refuel_res:
            print(f"⛽ Auto-refuel completato: {refuel_res.get('description', '')}")
    except Exception as exc:
        print(f"⚠️  Auto-refuel non riuscito (proseguo con saldo attuale): {exc}")

    # 1. Universo tradabile
    print("🕵️  Screening dei token su Base (CoinGecko + liquidita' + GoPlus)...")
    universe = token_screener.get_universe()
    rejected = token_screener.get_last_rejected()
    print(f"   {len(universe)} token approvati, {len(rejected)} scartati")
    trader.set_universe(universe)
    market_data.register_universe(universe)

    try:
        db_utils.log_screening(universe + rejected)
    except Exception as db_err:
        print(f"[db_utils] screening non salvato: {db_err}")

    # 2. Stato del wallet
    print("👛 Lettura del portafoglio on-chain...")
    account_status = trader.get_account_status(universe)
    print(f"   Valore totale: ${account_status['total_value_usd']:.2f} "
          f"(USDC ${account_status['usdc_balance']:.2f}, "
          f"{len(account_status['open_positions'])} token)")
    if account_status.get("paper_trading"):
        pnl = account_status.get("pnl_since_start_usd", 0.0)
        paper = account_status.get("paper", {})
        print(f"   [paper] P&L strategia: ${pnl:+.2f} su ${paper.get('initial_usdc', 0):.2f} "
              f"iniziali, {paper.get('trades', 0)} swap simulati")

    # 3. Stop loss e take profit: si eseguono prima di sentire il modello
    risk_exits = trader.portfolio.risk_exits(account_status)
    exit_results = []
    for holding in risk_exits:
        print(f"🚨 {holding['trigger']} su {holding['symbol']} "
              f"({holding['pnl_percent']:+.1f}%): chiusura posizione")
        if AUTO_RISK_EXITS:
            result = trader.liquidate(holding, holding["trigger"])
            exit_results.append(result)
            try:
                db_utils.log_swap(result)
            except Exception as db_err:
                print(f"[db_utils] swap non salvato: {db_err}")
    if exit_results:
        account_status = trader.get_account_status(universe)

    # 4. Contesto di mercato
    assets = _assets_to_analyze(universe, account_status["open_positions"])
    print(f"📊 Analisi tecnica su: {assets}...")
    indicators_txt, indicators_json = analyze_multiple_tickers(assets)

    print("📰 Recupero ultime notizie di mercato...")
    news_txt = fetch_latest_news()

    print("🎭 Analisi sentiment Fear & Greed...")
    sentiment_txt, sentiment_json = get_sentiment()

    print("🔮 Calcolo previsioni con Prophet...")
    forecasts_txt, forecasts_json = get_crypto_forecasts(assets)
    if not forecasts_txt:
        forecasts_txt = "Previsioni non disponibili in questo ciclo."
        print("⚠️  Previsioni Prophet non disponibili: il modello deciderà senza forecast.")

    universe_txt = token_screener.format_universe_for_prompt(universe)

    msg_info = f"""<universo_tradabile>\n{universe_txt}\n</universo_tradabile>\n\n
    <indicatori>\n{indicators_txt}\n</indicatori>\n\n
    <news>\n{news_txt}</news>\n\n
    <sentiment>\n{sentiment_txt}\n</sentiment>\n\n
    <forecast>\n{forecasts_txt}\n</forecast>\n\n"""

    portfolio_data = (
        f"{json.dumps(account_status, default=str)}\n"
        f"Uscite automatiche eseguite in questo ciclo: "
        f"{json.dumps(exit_results, default=str) if exit_results else 'nessuna'}"
    )

    try:
        snapshot_id = db_utils.log_account_status(account_status)
        print(f"[db_utils] Snapshot inserito con id={snapshot_id}")
    except Exception as db_err:
        print(f"[db_utils] Nota DB snapshot non salvato: {db_err}")

    # 5. Decisione
    with open("system_prompt.txt", encoding="utf-8") as f:
        system_prompt = f.read().format(portfolio_data, msg_info)

    print(f"🤖 L'agente AI (OpenRouter {OPENROUTER_MODEL}) sta decidendo...")
    decision = previsione_trading_agent(system_prompt)
    print(f"   Segnale generato: {json.dumps(decision, indent=2)}")

    # 6. Esecuzione
    print("⚡ Esecuzione del segnale su Uniswap V3...")
    execution_result = trader.execute_signal(decision)
    print(f"   Risultato: {execution_result}")

    try:
        op_id = db_utils.log_bot_operation(
            decision,
            system_prompt=system_prompt,
            indicators=indicators_json,
            news_text=news_txt,
            sentiment=sentiment_json,
            forecasts=forecasts_json,
        )
        print(f"[db_utils] Operazione inserita con id={op_id}")
        if execution_result.get("status") not in ("hold", "rejected"):  # include "paper"
            db_utils.log_swap(execution_result)
    except Exception as db_err:
        print(f"[db_utils] Nota DB operazione non salvata: {db_err}")

    try:
        from telegram_bot import notify_cycle_result
        notify_cycle_result(
            decision=decision,
            execution_result=execution_result,
            account_status=account_status,
            sentiment=sentiment_json,
            indicators=indicators_json,
            forecasts=forecasts_json,
        )
    except Exception as tg_err:
        print(f"[telegram] Nota notifica non inviata: {tg_err}")

    account_status = trader.get_account_status(universe)
    try:
        db_utils.log_account_status(account_status)
    except Exception:
        pass

    print("✅ Ciclo completato con successo.")
    return account_status


if __name__ == "__main__":
    if "--refuel" in sys.argv:
        from tools.refuel import run_refuel_cli
        from base_client import BaseClient
        client = BaseClient()
        run_refuel_cli(client)
        sys.exit(0)

    if not config.WALLET_ADDRESS:
        raise RuntimeError("WALLET_ADDRESS mancante nel .env")
    if not os.getenv("OPENROUTER_API_KEY"):
        raise RuntimeError("OPENROUTER_API_KEY mancante nel .env")

    try:
        run_cycle()
    except Exception as exc:
        try:
            from telegram_bot import notify_error
            notify_error(str(exc))
        except Exception:
            pass
        try:
            db_utils.log_error(exc, context={"wallet": config.WALLET_ADDRESS},
                               source="degen_agent")
        except Exception:
            pass
        print(f"❌ Si è verificato un errore: {exc}")
        sys.exit(1)
