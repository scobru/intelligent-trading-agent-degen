import requests
import time
import os
import json
import logging
from datetime import datetime, timezone
from dotenv import load_dotenv

load_dotenv()

logger = logging.getLogger(__name__)

# --- Configurazione Endpoint Free ---
# Alternative.me Fear and Greed Index API: 100% gratuita, nessun token o API key richiesto!
FREE_FNG_URL = "https://api.alternative.me/fng/?limit=1"

# Endpoint fallback (CoinMarketCap - opzionale se configurato)
CMC_API_URL = "https://pro-api.coinmarketcap.com/v3/fear-and-greed/historical"
CMC_API_KEY = os.getenv("CMC_PRO_API_KEY")


def get_latest_fear_and_greed():
    """
    Recupera l'ultimo valore del Fear & Greed Index.
    Utilizza come prima scelta l'API gratuita e pubblica di Alternative.me (nessuna chiave richiesta).
    Se configurata, supporta anche CoinMarketCap come fallback.
    """
    # 1. Tentativo primario: API gratuita Alternative.me
    try:
        response = requests.get(FREE_FNG_URL, timeout=10)
        response.raise_for_status()
        data = response.json()

        if data and "data" in data and len(data["data"]) > 0:
            latest = data["data"][0]
            valore = int(latest.get("value", 50))
            classificazione = latest.get("value_classification", "Neutral")
            timestamp = int(latest.get("timestamp", int(time.time())))

            return {
                "valore": valore,
                "classificazione": classificazione,
                "timestamp": timestamp,
                "source": "alternative.me (free)"
            }
    except Exception as e:
        logger.warning(f"Alternative.me API call failed: {e}. Tentativo fallback...")

    # 2. Tentativo secondario: CoinMarketCap (se chiave presente)
    if CMC_API_KEY:
        try:
            headers = {
                "Accepts": "application/json",
                "X-CMC_PRO_API_KEY": CMC_API_KEY,
            }
            response = requests.get(CMC_API_URL, headers=headers, params={"limit": 1}, timeout=10)
            response.raise_for_status()
            data = response.json()

            if data and "data" in data and len(data["data"]) > 0:
                latest = data["data"][0]
                return {
                    "valore": int(latest.get("value", 50)),
                    "classificazione": latest.get("value_classification", "Neutral"),
                    "timestamp": latest.get("timestamp", int(time.time())),
                    "source": "coinmarketcap"
                }
        except Exception as e:
            logger.error(f"CoinMarketCap API call failed: {e}")

    # Fallback neutrale di sicurezza
    return {
        "valore": 50,
        "classificazione": "Neutral",
        "timestamp": int(time.time()),
        "source": "fallback"
    }


def get_sentiment() -> tuple:
    """
    Restituisce una stringa formattata con l'ultimo Fear & Greed Index e i dati strutturati.
    """
    sentiment_data = get_latest_fear_and_greed()
    if sentiment_data:
        # Formatta timestamp leggibile
        try:
            dt = datetime.fromtimestamp(sentiment_data["timestamp"], tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
        except Exception:
            dt = str(sentiment_data["timestamp"])

        formatted_str = (
            f"Sentiment del mercato (Fear & Greed Index):\n"
            f"  Valore: {sentiment_data['valore']}/100\n"
            f"  Classificazione: {sentiment_data['classificazione']}\n"
            f"  Data: {dt}\n"
            f"  Fonte: {sentiment_data.get('source', 'free')}"
        )
        return formatted_str, sentiment_data
    else:
        return "Impossibile recuperare il sentiment del mercato.", None