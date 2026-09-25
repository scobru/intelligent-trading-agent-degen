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

import config
import http_client

logger = logging.getLogger(__name__)

# Simboli che hanno un mercato CEX affidabile
CEX_SYMBOLS = {"ETH": "ETH", "WETH": "ETH", "BTC": "BTC", "CBBTC": "BTC", "SOL": "SOL"}

EXCHANGE_CANDIDATES = (("binance", "USDT"), ("kraken", "USD"), ("coinbase", "USD"), ("okx", "USDT"))

# Quante candele si scaricano davvero, qualunque cosa chieda chi chiama
CANONICAL_LIMITS = {"15m": 500, "1h": 500, "4h": 300, "1d": 200}

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


def register_holdings(holdings: List[Dict[str, Any]]):
    """
    Rende noti i token attualmente posseduti nel wallet.
    Permette a market_data (indicatori, Prophet) di recuperare le candele
    e il contesto di mercato anche per token non inclusi nell'universo dello screening.
    """
    for holding in holdings or []:
        addr = (holding.get("address") or "").lower()
        symbol = (holding.get("symbol") or "").upper()
        if not addr:
            continue
        existing = _REGISTRY.get(symbol) or _REGISTRY.get(addr)
        if existing and existing.get("liquidity_usd"):
            continue

        entry = {
            "symbol": symbol,
            "address": addr,
            "price_usd": holding.get("price_usd", 0.0),
        }
        if symbol:
            _REGISTRY[symbol] = entry
        _REGISTRY[addr] = entry


def lookup(identifier: str) -> Optional[Dict[str, Any]]:
    if not identifier:
        return None
    res = _REGISTRY.get(identifier.upper()) or _REGISTRY.get(identifier.lower())
    if res:
        return res
    upper = identifier.upper()
    core_key = "WETH" if upper == "ETH" else upper
    if core_key in config.CORE_TOKENS:
        meta = config.CORE_TOKENS[core_key]
        entry = {
            "symbol": upper,
            "address": meta["address"].lower(),
        }
        _REGISTRY[upper] = entry
        _REGISTRY[meta["address"].lower()] = entry
        return entry
    return None


# ---------------------------------------------------------------- CEX
def _get_exchange(exchange_id: str):
    if exchange_id not in _EXCHANGES:
        _EXCHANGES[exchange_id] = getattr(ccxt, exchange_id)({"enableRateLimit": True})
    return _EXCHANGES[exchange_id]


_CEX_CACHE: Dict[str, Dict[str, Any]] = {}


def _cex_ohlcv(cex_symbol: str, interval: str, limit: int) -> pd.DataFrame:
    global _PREFERRED_EXCHANGE

    cache_key = f"{cex_symbol}:{interval}"
    cached = _CEX_CACHE.get(cache_key)
    if cached and (time.time() - cached["time"]) < config.OHLCV_CACHE_SECONDS:
        return cached["df"].tail(limit).reset_index(drop=True)

    canonical = max(limit, CANONICAL_LIMITS.get(interval, 500))
    candidates = list(EXCHANGE_CANDIDATES)
    if _PREFERRED_EXCHANGE:
        candidates.sort(key=lambda c: c[0] != _PREFERRED_EXCHANGE)

    errors = []
    for exchange_id, quote in candidates:
        try:
            raw = _get_exchange(exchange_id).fetch_ohlcv(
                f"{cex_symbol}/{quote}", timeframe=interval, limit=canonical
            )
            if raw:
                _PREFERRED_EXCHANGE = exchange_id
                df = _to_dataframe(raw, unit="ms")
                _CEX_CACHE[cache_key] = {"time": time.time(), "df": df}
                return df.tail(limit).reset_index(drop=True)
        except Exception as exc:
            errors.append(f"{exchange_id}: {type(exc).__name__}: {exc}")

    raise RuntimeError(f"Nessuna candela CEX per {cex_symbol} {interval} — {' | '.join(errors)}")


# ---------------------------------------------------------------- DEX
def _get_json(url: str, params: Dict[str, Any] = None, cache_ttl: float = 0) -> Optional[Any]:
    return http_client.get_json(url, params=params, timeout=config.HTTP_TIMEOUT,
                                cache_ttl=cache_ttl)


def get_pool_address(token_address: str) -> Optional[str]:
    """Pool piu' liquida del token su Base, da GeckoTerminal."""
    key = token_address.lower()
    if key in _POOL_CACHE:
        return _POOL_CACHE[key]

    data = _get_json(
        f"{config.GECKOTERMINAL_BASE_URL}/networks/base/tokens/{token_address}/pools",
        params={"page": 1},
        cache_ttl=config.OHLCV_CACHE_SECONDS * 4,
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
        token = lookup(token_address)
        if token and not token.get("liquidity_usd"):
            attr = best.get("attributes", {})
            try:
                token["liquidity_usd"] = float(attr.get("reserve_in_usd") or 0.0)
                vol = attr.get("volume_usd", {})
                if isinstance(vol, dict):
                    token["volume_24h_usd"] = float(vol.get("h24") or 0.0)
                changes = attr.get("price_change_percentage", {})
                if isinstance(changes, dict):
                    token["price_change_1h"] = float(changes.get("h1") or 0.0)
                    token["price_change_24h"] = float(changes.get("h24") or 0.0)
                if not token.get("price_usd"):
                    token["price_usd"] = float(attr.get("base_token_price_usd") or 0.0)
            except Exception:
                pass
    return address


def _dex_ohlcv(token_address: str, interval: str, limit: int) -> pd.DataFrame:
    pool = get_pool_address(token_address)
    if not pool:
        raise RuntimeError(f"Nessuna pool trovata su Base per {token_address}")

    timeframe, aggregate = GECKOTERMINAL_TIMEFRAMES.get(interval, ("minute", 15))
    # Indicatori e Prophet chiedono lo stesso intervallo con limiti diversi:
    # si scarica sempre il limite canonico, cosi' la seconda richiesta e'
    # servita dalla cache invece di consumare il rate limit.
    canonical = CANONICAL_LIMITS.get(interval, 500)
    data = _get_json(
        f"{config.GECKOTERMINAL_BASE_URL}/networks/base/pools/{pool}/ohlcv/{timeframe}",
        params={"aggregate": aggregate, "limit": canonical, "currency": "usd"},
        cache_ttl=config.OHLCV_CACHE_SECONDS,
    )
    ohlcv_list = (((data or {}).get("data") or {}).get("attributes") or {}).get("ohlcv_list")
    if not ohlcv_list:
        raise RuntimeError(f"Nessuna candela DEX per {token_address} ({interval})")

    # GeckoTerminal restituisce dal piu' recente e con timestamp in secondi
    df = _to_dataframe(list(reversed(ohlcv_list)), unit="s")
    return df.tail(limit).reset_index(drop=True)


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
        if not token.get("liquidity_usd") and token.get("address"):
            get_pool_address(token["address"])
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
