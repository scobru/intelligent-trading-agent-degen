from openai import OpenAI
from dotenv import load_dotenv
import os
import json
import re
import time
import logging

load_dotenv()

logger = logging.getLogger(__name__)

# Modello configurabile: i router "free" possono restituire risposte vuote,
# in quel caso basta puntare OPENROUTER_MODEL a un modello stabile.
from config import (
    DEFAULT_SLIPPAGE_BPS,
    DEFAULT_STOP_LOSS_PCT,
    DEFAULT_TAKE_PROFIT_PCT,
    MAX_SLIPPAGE_BPS,
)

OPENROUTER_MODEL = os.getenv("OPENROUTER_MODEL", "openrouter/free")
MAX_LLM_ATTEMPTS = int(os.getenv("OPENROUTER_MAX_ATTEMPTS", "3"))

# OpenRouter API Key
OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY")

def get_openrouter_client():
    key = os.getenv("OPENROUTER_API_KEY")
    if not key:
        raise RuntimeError("OPENROUTER_API_KEY mancante nel .env")
    return OpenAI(
        base_url="https://openrouter.ai/api/v1",
        api_key=key,
    )

# Fallback client instance for compatibility
client = OpenAI(
    base_url="https://openrouter.ai/api/v1",
    api_key=OPENROUTER_API_KEY or "missing-openrouter-key",
)

SYSTEM_RULES = """You are a speculative spot trading agent operating a wallet on Base.

You trade ERC-20 tokens (memecoins included) directly from the wallet via
Uniswap V3. There is no leverage, no shorting and no perpetual: you can only
buy a token with USDC, sell a token back to USDC, or hold.

Hard rules:
- You may ONLY buy tokens listed in the screened universe given to you. Every
  token there passed a scam screening; anything else is off limits.
- To reduce exposure or take profit on a token you already hold, use "sell".
- Position sizing is expressed as a fraction of the TOTAL portfolio value.
- Minimum trade size is $5.00 (MIN_TRADE_USD). You CANNOT buy if your free USDC (usdc_balance in portfolio) is below $5.00. In that case, choose "hold" or "sell" an existing token to raise USDC.
- If buying, target_portion_of_portfolio * total_value_usd MUST be >= $5.00 and <= available USDC.
- Gas is paid in ETH: never plan around spending the whole ETH balance.

You MUST output ONLY a valid, raw JSON object (no extra commentary) adhering
strictly to this schema:
{
    "operation": "buy" | "sell" | "hold",
    "token": "SYMBOL or 0x address from the universe / your holdings",
    "target_portion_of_portfolio": float (0.0 to 1.0, only for "buy"),
    "sell_portion": float (0.0 to 1.0, only for "sell", 1.0 = close position),
    "slippage_bps": integer (10 to 300, basis points of max slippage),
    "stop_loss_percent": float (5.0 to 50.0),
    "take_profit_percent": float (5.0 to 500.0),
    "reason": "Brief explanation of the decision (max 300 chars)"
}
"""

def _clean_and_parse_json(text: str) -> dict:
    """Extract and parse JSON safely from model response."""
    if not text or not text.strip():
        raise ValueError("Risposta del modello vuota o nulla (content=None)")

    # Strip conversational or moderation preamble lines (e.g. "User Safety: safe")
    lines = text.splitlines()
    clean_lines = [
        l for l in lines
        if not l.strip().lower().startswith("user safety:")
        and not l.strip().lower().startswith("safety:")
    ]
    text = "\n".join(clean_lines).strip()
    if not text:
        raise ValueError("Risposta del modello non contiene testo valido dopo la rimozione dei prefissi di sicurezza")

    text = text.strip()
    
    # Check for markdown code fence
    fence_match = re.search(r"```(?:json)?\s*([\s\S]*?)\s*```", text)
    if fence_match:
        text = fence_match.group(1).strip()
        
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        # Try finding the first '{' and last '}'
        start = text.find('{')
        end = text.rfind('}')
        if start != -1 and end != -1 and end > start:
            data = json.loads(text[start:end+1])
        else:
            raise ValueError(f"Could not parse valid JSON from response: {text}")
            
    # Validate and normalize essential fields
    if "operation" not in data:
        data["operation"] = "hold"
    data["operation"] = str(data["operation"]).lower()

    # Il modello a volte usa "symbol" invece di "token"
    if "token" not in data and "symbol" in data:
        data["token"] = data["symbol"]
    data.setdefault("token", "")

    data.setdefault("target_portion_of_portfolio", 0.0)
    data.setdefault("sell_portion", 1.0)
    data.setdefault("slippage_bps", DEFAULT_SLIPPAGE_BPS)
    data.setdefault("stop_loss_percent", DEFAULT_STOP_LOSS_PCT)
    data.setdefault("take_profit_percent", DEFAULT_TAKE_PROFIT_PCT)
    data.setdefault("reason", "Default signal")

    # Clamp bounds
    try:
        data["target_portion_of_portfolio"] = max(
            0.0, min(1.0, float(data["target_portion_of_portfolio"]))
        )
        data["sell_portion"] = max(0.0, min(1.0, float(data["sell_portion"])))
        data["slippage_bps"] = max(10, min(MAX_SLIPPAGE_BPS, int(float(data["slippage_bps"]))))
        data["stop_loss_percent"] = max(5.0, min(50.0, float(data["stop_loss_percent"])))
        data["take_profit_percent"] = max(5.0, min(500.0, float(data["take_profit_percent"])))
    except (ValueError, TypeError):
        pass

    return data

def _safe_hold_signal(reason: str) -> dict:
    """Segnale neutro usato quando l'LLM non produce una risposta utilizzabile."""
    return {
        "operation": "hold",
        "token": "",
        "target_portion_of_portfolio": 0.0,
        "sell_portion": 0.0,
        "slippage_bps": DEFAULT_SLIPPAGE_BPS,
        "stop_loss_percent": DEFAULT_STOP_LOSS_PCT,
        "take_profit_percent": DEFAULT_TAKE_PROFIT_PCT,
        "reason": reason[:300],
        "fallback": True,
    }


def _extract_message_text(response) -> str:
    """
    Estrae il testo dalla risposta OpenRouter gestendo i casi in cui
    `message.content` è None (tipico dei modelli free) o una lista di parti.
    """
    choices = getattr(response, "choices", None) or []
    if not choices:
        return ""

    message = getattr(choices[0], "message", None)
    if message is None:
        return ""

    content = getattr(message, "content", None)

    # Alcuni provider restituiscono il contenuto come lista di blocchi
    if isinstance(content, list):
        parts = []
        for part in content:
            if isinstance(part, dict):
                parts.append(part.get("text") or "")
            else:
                parts.append(getattr(part, "text", "") or "")
        content = "".join(parts)

    if isinstance(content, str) and content.strip():
        return content

    # Fallback: i modelli con reasoning a volte lasciano il JSON solo lì
    extra = getattr(message, "model_extra", None) or {}
    for key in ("reasoning", "reasoning_content"):
        value = getattr(message, key, None) or extra.get(key)
        if isinstance(value, str) and value.strip():
            return value

    return ""


def _api_error(response):
    """Restituisce l'errore applicativo eventualmente incapsulato nella risposta."""
    err = getattr(response, "error", None)
    if err:
        return err
    extra = getattr(response, "model_extra", None) or {}
    return extra.get("error")


def previsione_trading_agent(prompt: str, max_attempts: int = None) -> dict:
    """
    Invia il prompt all'LLM tramite OpenRouter e restituisce il segnale di trading.

    Se il modello risponde vuoto o con JSON non valido riprova; esaurititi i
    tentativi restituisce un segnale 'hold' invece di far fallire l'intero ciclo.
    """
    cl = get_openrouter_client()
    attempts = max_attempts or MAX_LLM_ATTEMPTS
    last_error = None

    for attempt in range(1, attempts + 1):
        try:
            response = cl.chat.completions.create(
                model=OPENROUTER_MODEL,
                messages=[
                    {"role": "system", "content": SYSTEM_RULES},
                    {"role": "user", "content": prompt}
                ],
                response_format={"type": "json_object"},
                temperature=0.2,
            )
        except Exception as exc:
            last_error = f"Chiamata OpenRouter fallita: {type(exc).__name__}: {exc}"
            logger.warning("[LLM] tentativo %s/%s — %s", attempt, attempts, last_error)
            print(f"⚠️  [LLM] tentativo {attempt}/{attempts}: {last_error}")
            if attempt < attempts:
                time.sleep(2 * attempt)
            continue

        api_error = _api_error(response)
        if api_error:
            last_error = f"OpenRouter ha restituito un errore: {api_error}"
            logger.warning("[LLM] tentativo %s/%s — %s", attempt, attempts, last_error)
            print(f"⚠️  [LLM] tentativo {attempt}/{attempts}: {last_error}")
            if attempt < attempts:
                time.sleep(2 * attempt)
            continue

        output_text = _extract_message_text(response)
        if not output_text.strip():
            finish_reason = None
            try:
                finish_reason = response.choices[0].finish_reason
            except Exception:
                pass
            last_error = (
                f"Risposta vuota dal modello {OPENROUTER_MODEL} "
                f"(finish_reason={finish_reason})"
            )
            logger.warning("[LLM] tentativo %s/%s — %s", attempt, attempts, last_error)
            print(f"⚠️  [LLM] tentativo {attempt}/{attempts}: {last_error}")
            if attempt < attempts:
                time.sleep(2 * attempt)
            continue

        try:
            return _clean_and_parse_json(output_text)
        except Exception as exc:
            last_error = f"JSON non valido: {type(exc).__name__}: {exc}"
            logger.warning("[LLM] tentativo %s/%s — %s", attempt, attempts, last_error)
            print(f"⚠️  [LLM] tentativo {attempt}/{attempts}: {last_error}")
            if attempt < attempts:
                time.sleep(2 * attempt)

    print(f"❌ [LLM] nessuna risposta valida dopo {attempts} tentativi: {last_error}")
    logger.error("[LLM] fallback su 'hold' — %s", last_error)
    return _safe_hold_signal(f"Fallback automatico (nessuna decisione AI): {last_error}")
