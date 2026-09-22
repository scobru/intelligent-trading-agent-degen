"""
Configurazione del bot spot su Base (chain id 8453).

Gli indirizzi qui sotto sono contratti noti di Base mainnet. Non ci fidiamo
della costante scritta a mano: all'avvio `base_client.verify_contracts()`
interroga la chain e confronta symbol/decimals, quindi un indirizzo sbagliato
si manifesta come errore esplicito invece che come swap verso il nulla.
"""

import os

from dotenv import load_dotenv

load_dotenv()


def _f(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, default))
    except (TypeError, ValueError):
        return float(default)


def _i(name: str, default: int) -> int:
    try:
        return int(float(os.getenv(name, default)))
    except (TypeError, ValueError):
        return int(default)


def _b(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in ("1", "true", "yes", "y", "on", "si")


# ---------------------------------------------------------------- rete
CHAIN_ID = 8453
BASE_RPC_URL = os.getenv("BASE_RPC_URL", "https://mainnet.base.org")
WALLET_ADDRESS = os.getenv("WALLET_ADDRESS", "")
PRIVATE_KEY = os.getenv("PRIVATE_KEY", "")

# ---------------------------------------------------------------- contratti Base
WETH = "0x4200000000000000000000000000000000000006"
USDC = "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"

# Uniswap V3 su Base
UNISWAP_V3_FACTORY = "0x33128a8fC17869897dcE68Ed026d694621f6FDfD"
UNISWAP_V3_QUOTER_V2 = "0x3d4e44Eb1374240CE5F1B871ab261CD16335B76a"
UNISWAP_V3_SWAP_ROUTER_02 = "0x2626664c2603336E57B271c5C0b26F421741e481"

# Fee tier esplorate quando si cerca una rotta
FEE_TIERS = (100, 500, 3000, 10000)

# Token di quotazione: ogni rotta passa da uno di questi
QUOTE_TOKENS = {"USDC": USDC, "WETH": WETH}

# Asset "core": sempre ammessi, saltano lo screening di mercato
CORE_TOKENS = {
    "WETH": {"address": WETH, "decimals": 18, "cex_symbol": "ETH"},
    "USDC": {"address": USDC, "decimals": 6, "cex_symbol": None},
}

# La valuta in cui il bot tiene la liquidita' inutilizzata
BASE_CURRENCY = "USDC"

# ---------------------------------------------------------------- sicurezza operativa
# Di default NON firma nulla: mettere DRY_RUN=false per operare davvero.
DRY_RUN = _b("DRY_RUN", True)

# Riserva di ETH da non spendere mai, serve per il gas
MIN_ETH_RESERVE = _f("MIN_ETH_RESERVE", 0.0015)

MAX_SLIPPAGE_BPS = _i("MAX_SLIPPAGE_BPS", 300)          # 3%
DEFAULT_SLIPPAGE_BPS = _i("DEFAULT_SLIPPAGE_BPS", 150)  # 1.5%
MAX_GAS_PRICE_GWEI = _f("MAX_GAS_PRICE_GWEI", 0.5)
TX_DEADLINE_SECONDS = _i("TX_DEADLINE_SECONDS", 300)
TX_TIMEOUT_SECONDS = _i("TX_TIMEOUT_SECONDS", 180)

# ---------------------------------------------------------------- limiti di rischio
MAX_POSITION_PCT = _f("MAX_POSITION_PCT", 0.25)   # per singolo token, sul totale
MAX_OPEN_TOKENS = _i("MAX_OPEN_TOKENS", 5)        # quante posizioni contemporanee
MIN_TRADE_USD = _f("MIN_TRADE_USD", 5.0)
MAX_TRADE_USD = _f("MAX_TRADE_USD", 250.0)
DUST_USD = _f("DUST_USD", 1.0)                    # sotto questa soglia non e' una posizione

DEFAULT_STOP_LOSS_PCT = _f("DEFAULT_STOP_LOSS_PCT", 20.0)
DEFAULT_TAKE_PROFIT_PCT = _f("DEFAULT_TAKE_PROFIT_PCT", 40.0)

# ---------------------------------------------------------------- screening token
SCREEN_MAX_CANDIDATES = _i("SCREEN_MAX_CANDIDATES", 12)
SCREEN_MIN_LIQUIDITY_USD = _f("SCREEN_MIN_LIQUIDITY_USD", 250_000)
SCREEN_MIN_VOLUME_24H_USD = _f("SCREEN_MIN_VOLUME_24H_USD", 400_000)
SCREEN_MIN_POOL_AGE_DAYS = _f("SCREEN_MIN_POOL_AGE_DAYS", 7)
SCREEN_MAX_TAX_PERCENT = _f("SCREEN_MAX_TAX_PERCENT", 5.0)
SCREEN_CACHE_SECONDS = _i("SCREEN_CACHE_SECONDS", 1800)

# Token che non vogliamo mai toccare, qualunque cosa dicano le API
TOKEN_BLOCKLIST = {
    a.strip().lower()
    for a in os.getenv("TOKEN_BLOCKLIST", "").split(",")
    if a.strip()
}

# Se valorizzata, l'universo si limita a questi indirizzi (piu' i core)
TOKEN_ALLOWLIST = {
    a.strip().lower()
    for a in os.getenv("TOKEN_ALLOWLIST", "").split(",")
    if a.strip()
}

# ---------------------------------------------------------------- API esterne
COINGECKO_BASE_TOKENLIST = "https://tokens.coingecko.com/base/all.json"
DEXSCREENER_TOKENS_URL = "https://api.dexscreener.com/latest/dex/tokens/"
GOPLUS_TOKEN_SECURITY_URL = f"https://api.gopluslabs.io/api/v1/token_security/{CHAIN_ID}"
GECKOTERMINAL_BASE_URL = "https://api.geckoterminal.com/api/v2"
HTTP_TIMEOUT = _i("HTTP_TIMEOUT", 20)

def default_db_path() -> str:
    """
    Su CapRover/Docker /app/data e' la directory montata come volume
    persistente: metterci il database evita di perdere lo storico a ogni
    redeploy. In locale resta accanto al codice.
    """
    project_dir = os.path.dirname(os.path.abspath(__file__))
    data_dir = os.path.join(project_dir, "data")
    if os.path.isdir(data_dir) and os.access(data_dir, os.W_OK):
        return os.path.join(data_dir, "trading_agent.db")
    return os.path.join(project_dir, "trading_agent.db")


SQLITE_DB_PATH = os.getenv("SQLITE_DB_PATH") or default_db_path()
