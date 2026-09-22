"""
Paper trading: il bot opera con un portafoglio finto ma prezzi veri.

Serve per vedere come si comporterebbe senza mettere capitale sul wallet.
La differenza rispetto al dry-run puro e' che qui gli ordini *vengono
eseguiti*, contro un saldo virtuale: cosi' invece di leggere "USDC
insufficienti" a ogni ciclo si vede il portafoglio evolvere, con P&L,
stop loss e take profit che scattano davvero.

Quello che resta reale: prezzi e rotte quotati su Uniswap, screening,
limiti di rischio, decisioni del modello. Quello che e' simulato: i saldi,
il riempimento degli ordini e il costo del gas.
"""

import json
import logging
import os
import time
from typing import Any, Dict, List, Optional

import config

logger = logging.getLogger(__name__)

PAPER_STATE_PATH = os.getenv("PAPER_STATE_PATH") or config.persistent_path("paper_portfolio.json")


class PaperWallet:
    """Saldi virtuali, persistiti come il registro delle posizioni reali."""

    def __init__(self, path: str = None, start_usdc: float = None, start_eth: float = None):
        self.path = path or PAPER_STATE_PATH
        self.start_usdc = start_usdc if start_usdc is not None else config.PAPER_START_USDC
        self.start_eth = start_eth if start_eth is not None else config.PAPER_START_ETH
        self.state: Dict[str, Any] = {}
        self.load()

    # ------------------------------------------------------------ persistenza
    def load(self):
        try:
            with open(self.path, encoding="utf-8") as fh:
                self.state = json.load(fh)
        except (OSError, ValueError):
            self.state = {}

        if not self.state:
            self.state = {
                "usdc": float(self.start_usdc),
                "eth": float(self.start_eth),
                "tokens": {},          # address -> {symbol, amount}
                "created_at": time.time(),
                "gas_spent_usd": 0.0,
                "trades": 0,
            }
            self.save()
            logger.info("Portafoglio paper inizializzato con $%.2f USDC", self.state["usdc"])

    def save(self):
        try:
            os.makedirs(os.path.dirname(os.path.abspath(self.path)), exist_ok=True)
            with open(self.path, "w", encoding="utf-8") as fh:
                json.dump(self.state, fh, indent=2)
        except OSError as exc:
            logger.warning("Impossibile salvare %s: %s", self.path, exc)

    def reset(self):
        self.state = {}
        try:
            os.remove(self.path)
        except OSError:
            pass
        self.load()

    # ------------------------------------------------------------ saldi
    @property
    def usdc(self) -> float:
        return float(self.state.get("usdc", 0.0))

    @property
    def eth(self) -> float:
        return float(self.state.get("eth", 0.0))

    def token_amount(self, address: str) -> float:
        return float((self.state.get("tokens", {}).get(address.lower()) or {}).get("amount", 0.0))

    def tokens(self) -> Dict[str, Dict[str, Any]]:
        return self.state.get("tokens", {})

    # ------------------------------------------------------------ esecuzione
    def buy(self, address: str, symbol: str, usd_amount: float, tokens_out: float,
            gas_usd: float = 0.0) -> Dict[str, Any]:
        address = address.lower()
        if usd_amount > self.usdc:
            raise ValueError(
                f"USDC virtuali insufficienti: servono ${usd_amount:.2f}, ci sono ${self.usdc:.2f}"
            )

        self.state["usdc"] = self.usdc - usd_amount
        holding = self.state.setdefault("tokens", {}).setdefault(
            address, {"symbol": symbol, "amount": 0.0}
        )
        holding["symbol"] = symbol
        holding["amount"] = float(holding["amount"]) + tokens_out
        self.state["gas_spent_usd"] = float(self.state.get("gas_spent_usd", 0.0)) + gas_usd
        self.state["trades"] = int(self.state.get("trades", 0)) + 1
        self.save()

        return {"usdc_after": self.usdc, "token_amount_after": holding["amount"]}

    def sell(self, address: str, tokens_in: float, usd_out: float,
             gas_usd: float = 0.0) -> Dict[str, Any]:
        address = address.lower()
        holding = self.state.get("tokens", {}).get(address)
        if not holding or float(holding["amount"]) <= 0:
            raise ValueError("Nessuna posizione virtuale da vendere")

        tokens_in = min(tokens_in, float(holding["amount"]))
        holding["amount"] = float(holding["amount"]) - tokens_in
        self.state["usdc"] = self.usdc + usd_out
        self.state["gas_spent_usd"] = float(self.state.get("gas_spent_usd", 0.0)) + gas_usd
        self.state["trades"] = int(self.state.get("trades", 0)) + 1

        if holding["amount"] <= 0:
            self.state["tokens"].pop(address, None)
        self.save()

        return {"usdc_after": self.usdc, "tokens_sold": tokens_in}

    # ------------------------------------------------------------ riepilogo
    def summary(self) -> Dict[str, Any]:
        return {
            "usdc": self.usdc,
            "eth": self.eth,
            "tokens": len(self.tokens()),
            "trades": self.state.get("trades", 0),
            "gas_spent_usd": self.state.get("gas_spent_usd", 0.0),
            "started_at": self.state.get("created_at"),
            "initial_usdc": float(self.start_usdc),
        }
