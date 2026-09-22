"""
Serie storiche e dati di mercato, per due mondi diversi.

I major (ETH, BTC, SOL) hanno candele pulite sui CEX e si prendono con CCXT.
Le meme su Base sui CEX non esistono: per quelle si usano le candele della
pool piu' liquida via GeckoTerminal. Chi chiama (indicatori, Prophet) non
deve sapere da dove arrivano.
"""

import logging
import time
from typing import Any, Dict, List, Optional

import ccxt
import pandas as pd
import requests

import config

logger = logging.getLogger(__name__)

# Simboli che hanno un mercato CEX affidabile
CEX_SYMBOLS = {"ETH": "ETH", "WETH": "ETH", "BTC": "BTC", "CBBTC": "BTC", "SOL": "SOL"}

EXCHANGE_CANDIDATES = (("binance", "USDT"), ("kraken", "USD"), ("coinbase", "USD"), ("okx", "USDT"))

GECKOTERMINAL_TIMEFRAMES = {
    "15m": ("minute", 15),
    "1h": ("hour", 1),
    "4h": ("hour", 4),
    "1d": ("day", 1),
}

_REGISTRY: Dict[str, Dict[str, Any]] = {}
_POOL_CACHE: Dict[str, str] = {}
_EXCHANGES: Dict[str, Any] = {}
_PREFERRED_EXCHANGE: Optional[str] = None


# ---------------------------------------------------------------- registry
def register_universe(universe: List[Dict[str, Any]]):
    """Rende noti i token dello screening, con pool e metriche gia' calcolate."""
    for token in universe or []:
        key = token["symbol"].upper()
        _REGISTRY[key] = token
        _REGISTRY[token["address"].lower()] = token
        if token.get("pool_address"):
            _POOL_CACHE[token["address"].lower()] = token["pool_address"]


def lookup(identifier: str) -> Optional[Dict[str, Any]]:
    if not identifier:
        return None
    return _REGISTRY.get(identifier.upper()) or _REGISTRY.get(identifier.lower())


# ---------------------------------------------------------------- CEX
def _get_exchange(exchange_id: str):
    if exchange_id not in _EXCHANGES:
        _EXCHANGES[exchange_id] = getattr(ccxt, exchange_id)({"enableRateLimit": True})
    return _EXCHANGES[exchange_id]


def _cex_ohlcv(cex_symbol: str, interval: str, limit: int) -> pd.DataFrame:
    global _PREFERRED_EXCHANGE

    candidates = list(EXCHANGE_CANDIDATES)
    if _PREFERRED_EXCHANGE:
        candidates.sort(key=lambda c: c[0] != _PREFERRED_EXCHANGE)

    errors = []
    for exchange_id, quote in candidates:
        try:
            raw = _get_exchange(exchange_id).fetch_ohlcv(
                f"{cex_symbol}/{quote}", timeframe=interval, limit=limit
            )
            if raw:
                _PREFERRED_EXCHANGE = exchange_id
                return _to_dataframe(raw, unit="ms")
        except Exception as exc:
            errors.append(f"{exchange_id}: {type(exc).__name__}: {exc}")

    raise RuntimeError(f"Nessuna candela CEX per {cex_symbol} {interval} — {' | '.join(errors)}")


# ---------------------------------------------------------------- DEX
def _get_json(url: str, params: Dict[str, Any] = None) -> Optional[Any]:
    try:
        resp = requests.get(url, params=params, timeout=config.HTTP_TIMEOUT,
                            headers={"accept": "application/json"})
        resp.raise_for_status()
        return resp.json()
    except Exception as exc:
        logger.warning("Richiesta fallita %s: %s", url, exc)
        return None


def get_pool_address(token_address: str) -> Optional[str]:
    """Pool piu' liquida del token su Base, da GeckoTerminal."""
    key = token_address.lower()
    if key in _POOL_CACHE:
        return _POOL_CACHE[key]

    data = _get_json(
        f"{config.GECKOTERMINAL_BASE_URL}/networks/base/tokens/{token_address}/pools",
        params={"page": 1},
    )
    pools = (data or {}).get("data") or []
    if not pools:
        return None

    def liquidity(pool):
        try:
            return float(pool["attributes"].get("reserve_in_usd") or 0)
        except Exception:
            return 0.0

    best = max(pools, key=liquidity)
    address = best.get("attributes", {}).get("address") or best.get("id", "").split("_")[-1]
    if address:
        _POOL_CACHE[key] = address
    return address


def _dex_ohlcv(token_address: str, interval: str, limit: int) -> pd.DataFrame:
    pool = get_pool_address(token_address)
    if not pool:
        raise RuntimeError(f"Nessuna pool trovata su Base per {token_address}")

    timeframe, aggregate = GECKOTERMINAL_TIMEFRAMES.get(interval, ("minute", 15))
    data = _get_json(
        f"{config.GECKOTERMINAL_BASE_URL}/networks/base/pools/{pool}/ohlcv/{timeframe}",
        params={"aggregate": aggregate, "limit": min(limit, 1000), "currency": "usd"},
    )
    ohlcv_list = (((data or {}).get("data") or {}).get("attributes") or {}).get("ohlcv_list")
    if not ohlcv_list:
        raise RuntimeError(f"Nessuna candela DEX per {token_address} ({interval})")

    # GeckoTerminal restituisce dal piu' recente e con timestamp in secondi
    return _to_dataframe(list(reversed(ohlcv_list)), unit="s")


# ---------------------------------------------------------------- comune
def _to_dataframe(raw: List[List[Any]], unit: str) -> pd.DataFrame:
    df = pd.DataFrame(raw, columns=["timestamp", "open", "high", "low", "close", "volume"])
    df["timestamp"] = pd.to_datetime(df["timestamp"], unit=unit, utc=True)
    for col in ("open", "high", "low", "close", "volume"):
        df[col] = pd.to_numeric(df[col], errors="coerce").astype(float)
    df = df.dropna(subset=["close"]).sort_values("timestamp").reset_index(drop=True)
    return df


def get_ohlcv(identifier: str, interval: str = "15m", limit: int = 200) -> pd.DataFrame:
    """Candele per un simbolo CEX o per un token Base (simbolo o indirizzo)."""
    upper = identifier.upper()

    if upper in CEX_SYMBOLS and not identifier.startswith("0x"):
        return _cex_ohlcv(CEX_SYMBOLS[upper], interval, limit)

    token = lookup(identifier)
    address = token["address"] if token else (identifier if identifier.startswith("0x") else None)
    if not address:
        raise RuntimeError(f"Asset sconosciuto: {identifier}")

    return _dex_ohlcv(address, interval, limit)


def get_market_details(identifier: str) -> Dict[str, Any]:
    """Prezzo e contesto di liquidita', dalle metriche dello screening."""
    token = lookup(identifier)
    if token:
        return {
            "price_usd": token.get("price_usd", 0.0),
            "liquidity_usd": token.get("liquidity_usd", 0.0),
            "volume_24h_usd": token.get("volume_24h_usd", 0.0),
            "price_change_1h": token.get("price_change_1h", 0.0),
            "price_change_24h": token.get("price_change_24h", 0.0),
            "pool_age_days": token.get("pool_age_days"),
            "source": "dex",
        }

    upper = identifier.upper()
    if upper in CEX_SYMBOLS:
        try:
            ticker = _get_exchange(_PREFERRED_EXCHANGE or "binance").fetch_ticker(
                f"{CEX_SYMBOLS[upper]}/USDT"
            )
            return {
                "price_usd": float(ticker.get("last") or ticker.get("close") or 0.0),
                "liquidity_usd": 0.0,
                "volume_24h_usd": float(ticker.get("quoteVolume") or 0.0),
                "price_change_1h": 0.0,
                "price_change_24h": float(ticker.get("percentage") or 0.0),
                "pool_age_days": None,
                "source": "cex",
            }
        except Exception:
            pass

    return {"price_usd": 0.0, "liquidity_usd": 0.0, "volume_24h_usd": 0.0,
            "price_change_1h": 0.0, "price_change_24h": 0.0, "pool_age_days": None,
            "source": "unknown"}


def format_price(value: Optional[float]) -> str:
    """
    Le meme costano 0.0000000123: una formattazione fissa a due decimali
    le trasformerebbe tutte in "0.00". Qui i decimali seguono la grandezza.
    """
    if value is None:
        return "n/d"
    value = float(value)
    if value == 0:
        return "0"
    magnitude = abs(value)
    if magnitude >= 1000:
        return f"{value:,.2f}"
    if magnitude >= 1:
        return f"{value:.4f}"
    if magnitude >= 0.0001:
        return f"{value:.6f}"
    return f"{value:.12f}".rstrip("0")
