"""
Selezione dell'universo di token tradabili su Base.

Pipeline, dal piu' permissivo al piu' severo:

  1. scoperta   GeckoTerminal: pool piu' scambiate e trending su Base
  2. legittimita'  la token list CoinGecko di Base: se un token non e' li',
                per noi non esiste (e' il filtro anti-scam piu' economico)
  3. mercato    DexScreener: liquidita', volume 24h, eta' della pool
  4. contratto  GoPlus Security: honeypot, tax, mint, owner, pausable...

Un token entra nell'universo solo se passa tutti e quattro gli stadi.
Ogni scarto conserva il motivo, cosi' la dashboard mostra perche' un token
non e' stato considerato.
"""

import logging
import time
from typing import Any, Dict, List, Optional

import requests

import config

logger = logging.getLogger(__name__)

_CACHE: Dict[str, Any] = {
    "coingecko_list": None,
    "coingecko_list_time": 0,
    "universe": None,
    "universe_time": 0,
}


def _get_json(url: str, params: Dict[str, Any] = None) -> Optional[Any]:
    try:
        resp = requests.get(url, params=params, timeout=config.HTTP_TIMEOUT,
                            headers={"accept": "application/json"})
        resp.raise_for_status()
        return resp.json()
    except Exception as exc:
        logger.warning("Richiesta fallita %s: %s", url, exc)
        return None


# ---------------------------------------------------------------- 2. CoinGecko
def coingecko_base_tokens(force: bool = False) -> Dict[str, Dict[str, Any]]:
    """
    Token list ufficiale di CoinGecko per Base, indicizzata per indirizzo.
    Essere in questa lista non garantisce nulla sul prezzo, ma tiene fuori
    i contratti appena deployati e i cloni con nomi copiati.
    """
    age = time.time() - _CACHE["coingecko_list_time"]
    if not force and _CACHE["coingecko_list"] and age < 86_400:
        return _CACHE["coingecko_list"]

    data = _get_json(config.COINGECKO_BASE_TOKENLIST)
    if not data or "tokens" not in data:
        logger.warning("Token list CoinGecko non disponibile")
        return _CACHE["coingecko_list"] or {}

    indexed = {}
    for token in data["tokens"]:
        addr = str(token.get("address", "")).lower()
        if addr:
            indexed[addr] = {
                "symbol": token.get("symbol", "?").upper(),
                "name": token.get("name", ""),
                "decimals": token.get("decimals", 18),
                "address": addr,
            }

    _CACHE["coingecko_list"] = indexed
    _CACHE["coingecko_list_time"] = time.time()
    logger.info("Token list CoinGecko Base: %s token", len(indexed))
    return indexed


# ---------------------------------------------------------------- 1. scoperta
def discover_candidates(limit: int = 60) -> List[str]:
    """Indirizzi dei token piu' attivi su Base secondo GeckoTerminal."""
    addresses: List[str] = []
    seen = set()

    endpoints = [
        f"{config.GECKOTERMINAL_BASE_URL}/networks/base/trending_pools",
        f"{config.GECKOTERMINAL_BASE_URL}/networks/base/pools",
    ]

    for url in endpoints:
        data = _get_json(url, params={"page": 1})
        if not data or "data" not in data:
            continue
        for pool in data["data"]:
            rel = pool.get("relationships", {})
            base_token = rel.get("base_token", {}).get("data", {}).get("id", "")
            # formato "base_0xabc..."
            addr = base_token.split("_")[-1].lower() if base_token else ""
            if addr and addr not in seen:
                seen.add(addr)
                addresses.append(addr)
            if len(addresses) >= limit:
                return addresses

    return addresses


# ---------------------------------------------------------------- 3. DexScreener
def dexscreener_metrics(addresses: List[str]) -> Dict[str, Dict[str, Any]]:
    """
    Metriche di mercato per indirizzo: si tiene la pool piu' liquida di ognuno.
    DexScreener accetta fino a 30 indirizzi per chiamata.
    """
    metrics: Dict[str, Dict[str, Any]] = {}

    for i in range(0, len(addresses), 30):
        chunk = addresses[i:i + 30]
        data = _get_json(config.DEXSCREENER_TOKENS_URL + ",".join(chunk))
        if not data:
            continue

        for pair in (data.get("pairs") or []):
            if str(pair.get("chainId", "")).lower() != "base":
                continue
            base_token = pair.get("baseToken", {})
            addr = str(base_token.get("address", "")).lower()
            if not addr:
                continue

            liquidity = float((pair.get("liquidity") or {}).get("usd") or 0)
            previous = metrics.get(addr)
            if previous and previous["liquidity_usd"] >= liquidity:
                continue

            created_ms = pair.get("pairCreatedAt")
            age_days = None
            if created_ms:
                age_days = (time.time() - created_ms / 1000) / 86_400

            metrics[addr] = {
                "symbol": base_token.get("symbol", "?").upper(),
                "address": addr,
                "price_usd": float(pair.get("priceUsd") or 0),
                "liquidity_usd": liquidity,
                "volume_24h_usd": float((pair.get("volume") or {}).get("h24") or 0),
                "price_change_1h": float((pair.get("priceChange") or {}).get("h1") or 0),
                "price_change_24h": float((pair.get("priceChange") or {}).get("h24") or 0),
                "pool_address": pair.get("pairAddress"),
                "dex": pair.get("dexId"),
                "pool_age_days": age_days,
            }

    return metrics


# ---------------------------------------------------------------- 4. GoPlus
def goplus_security(addresses: List[str]) -> Dict[str, Dict[str, Any]]:
    """Analisi del contratto: honeypot, tasse, permessi dell'owner."""
    security: Dict[str, Dict[str, Any]] = {}

    for i in range(0, len(addresses), 20):
        chunk = addresses[i:i + 20]
        data = _get_json(
            config.GOPLUS_TOKEN_SECURITY_URL,
            params={"contract_addresses": ",".join(chunk)},
        )
        if not data or data.get("code") != 1:
            continue
        for addr, raw in (data.get("result") or {}).items():
            security[addr.lower()] = raw

    return security


def _as_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _is_true(value: Any) -> bool:
    return str(value) == "1"


def evaluate_security(raw: Dict[str, Any]) -> Dict[str, Any]:
    """Traduce la risposta GoPlus in un verdetto con i motivi di scarto."""
    if not raw:
        return {"ok": False, "reasons": ["nessun dato di sicurezza disponibile"], "raw": {}}

    reasons: List[str] = []

    if _is_true(raw.get("is_honeypot")):
        reasons.append("honeypot: non si riesce a vendere")
    if _is_true(raw.get("cannot_sell_all")):
        reasons.append("non si puo' vendere l'intera posizione")
    if _is_true(raw.get("transfer_pausable")):
        reasons.append("i trasferimenti possono essere sospesi")
    if _is_true(raw.get("is_blacklisted")):
        reasons.append("il contratto puo' mettere indirizzi in blacklist")
    if _is_true(raw.get("can_take_back_ownership")):
        reasons.append("l'ownership puo' essere ripresa")
    if _is_true(raw.get("selfdestruct")):
        reasons.append("il contratto puo' autodistruggersi")
    if _is_true(raw.get("hidden_owner")):
        reasons.append("owner nascosto")
    if _is_true(raw.get("is_mintable")) and not _is_true(raw.get("is_open_source")):
        reasons.append("mintabile e non open source")
    if not _is_true(raw.get("is_open_source")):
        reasons.append("contratto non verificato")

    buy_tax = _as_float(raw.get("buy_tax")) * 100
    sell_tax = _as_float(raw.get("sell_tax")) * 100
    if buy_tax > config.SCREEN_MAX_TAX_PERCENT:
        reasons.append(f"buy tax {buy_tax:.1f}%")
    if sell_tax > config.SCREEN_MAX_TAX_PERCENT:
        reasons.append(f"sell tax {sell_tax:.1f}%")

    return {
        "ok": not reasons,
        "reasons": reasons,
        "buy_tax_percent": buy_tax,
        "sell_tax_percent": sell_tax,
        "is_open_source": _is_true(raw.get("is_open_source")),
        "is_mintable": _is_true(raw.get("is_mintable")),
        "holder_count": raw.get("holder_count"),
    }


def evaluate_market(metrics: Dict[str, Any]) -> Dict[str, Any]:
    """Filtri di mercato: senza liquidita' e volume non si entra."""
    reasons: List[str] = []

    if metrics.get("liquidity_usd", 0) < config.SCREEN_MIN_LIQUIDITY_USD:
        reasons.append(
            f"liquidita' ${metrics.get('liquidity_usd', 0):,.0f} sotto la soglia "
            f"${config.SCREEN_MIN_LIQUIDITY_USD:,.0f}"
        )
    if metrics.get("volume_24h_usd", 0) < config.SCREEN_MIN_VOLUME_24H_USD:
        reasons.append(
            f"volume 24h ${metrics.get('volume_24h_usd', 0):,.0f} sotto la soglia "
            f"${config.SCREEN_MIN_VOLUME_24H_USD:,.0f}"
        )

    age = metrics.get("pool_age_days")
    if age is None:
        reasons.append("eta' della pool sconosciuta")
    elif age < config.SCREEN_MIN_POOL_AGE_DAYS:
        reasons.append(f"pool di soli {age:.1f} giorni")

    if not metrics.get("price_usd"):
        reasons.append("prezzo non disponibile")

    return {"ok": not reasons, "reasons": reasons}


# ---------------------------------------------------------------- pipeline
def screen_addresses(addresses: List[str]) -> List[Dict[str, Any]]:
    """Applica tutti i filtri a una lista di indirizzi, con i motivi di scarto."""
    addresses = [a.lower() for a in addresses if a]
    if not addresses:
        return []

    listed = coingecko_base_tokens()
    metrics = dexscreener_metrics(addresses)
    security = goplus_security(addresses)

    results = []
    for addr in addresses:
        entry: Dict[str, Any] = {
            "address": addr,
            "symbol": (metrics.get(addr) or {}).get("symbol")
            or (listed.get(addr) or {}).get("symbol", "?"),
            "approved": False,
            "reasons": [],
        }

        if addr in config.TOKEN_BLOCKLIST:
            entry["reasons"].append("in blocklist locale")
            results.append(entry)
            continue

        if config.TOKEN_ALLOWLIST and addr not in config.TOKEN_ALLOWLIST:
            entry["reasons"].append("non in allowlist locale")
            results.append(entry)
            continue

        cg = listed.get(addr)
        if not cg:
            entry["reasons"].append("non presente nella token list CoinGecko di Base")
            results.append(entry)
            continue
        entry["name"] = cg.get("name")
        entry["decimals"] = cg.get("decimals", 18)
        entry["symbol"] = cg.get("symbol", entry["symbol"])

        market = metrics.get(addr)
        if not market:
            entry["reasons"].append("nessuna pool trovata su DexScreener")
            results.append(entry)
            continue
        entry.update({k: v for k, v in market.items() if k not in ("symbol", "address")})

        market_verdict = evaluate_market(market)
        entry["reasons"].extend(market_verdict["reasons"])

        sec_verdict = evaluate_security(security.get(addr))
        entry["security"] = sec_verdict
        entry["reasons"].extend(sec_verdict["reasons"])

        entry["approved"] = not entry["reasons"]
        results.append(entry)

    return results


def get_universe(force: bool = False) -> List[Dict[str, Any]]:
    """
    Universo tradabile del ciclo corrente, ordinato per volume.
    Il risultato e' in cache per SCREEN_CACHE_SECONDS: lo screening costa
    diverse chiamate HTTP e l'universo non cambia ogni 15 minuti.
    """
    age = time.time() - _CACHE["universe_time"]
    if not force and _CACHE["universe"] and age < config.SCREEN_CACHE_SECONDS:
        return _CACHE["universe"]

    candidates = discover_candidates()
    if config.TOKEN_ALLOWLIST:
        candidates = list({*candidates, *config.TOKEN_ALLOWLIST})

    screened = screen_addresses(candidates)
    approved = [t for t in screened if t["approved"]]
    approved.sort(key=lambda t: t.get("volume_24h_usd", 0), reverse=True)
    approved = approved[: config.SCREEN_MAX_CANDIDATES]

    rejected = [t for t in screened if not t["approved"]]
    logger.info("Screening: %s approvati, %s scartati", len(approved), len(rejected))

    _CACHE["universe"] = approved
    _CACHE["universe_time"] = time.time()
    _CACHE["rejected"] = rejected
    return approved


def get_last_rejected() -> List[Dict[str, Any]]:
    return _CACHE.get("rejected") or []


def is_tradable(address: str) -> Dict[str, Any]:
    """Ricontrolla un singolo token: usato prima di comprare e su ogni holding."""
    results = screen_addresses([address])
    return results[0] if results else {
        "address": address, "approved": False, "reasons": ["indirizzo non valutabile"]
    }


def format_universe_for_prompt(universe: List[Dict[str, Any]]) -> str:
    """Rende l'universo leggibile dall'LLM."""
    if not universe:
        return "Nessun token ha superato lo screening in questo ciclo."

    lines = [
        "Token ammessi (hanno superato CoinGecko + liquidita'/volume/eta' + GoPlus):",
        f"{'SIMBOLO':<12}{'PREZZO':>14}{'LIQ.':>14}{'VOL 24H':>14}{'1H%':>8}{'24H%':>8}  INDIRIZZO",
    ]
    for t in universe:
        lines.append(
            f"{t['symbol']:<12}"
            f"{t.get('price_usd', 0):>14.8f}"
            f"{t.get('liquidity_usd', 0):>14,.0f}"
            f"{t.get('volume_24h_usd', 0):>14,.0f}"
            f"{t.get('price_change_1h', 0):>8.2f}"
            f"{t.get('price_change_24h', 0):>8.2f}  "
            f"{t['address']}"
        )
    return "\n".join(lines)
