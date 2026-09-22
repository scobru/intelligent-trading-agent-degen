"""
Lettura del portafoglio direttamente dal wallet, senza intermediari.

Senza un indexer non esiste "elenca i token che possiedo", quindi il bot
tiene un registro locale (positions.json) dei token che ha comprato e
interroga balanceOf su quelli, piu' i token core e quelli dell'universo
corrente. Nello stesso registro vivono prezzo di carico, stop loss e take
profit, che on-chain non esistono.
"""

import json
import logging
import os
import time
from typing import Any, Dict, List, Optional

import config
from base_client import BaseClient
from uniswap import UniswapV3

logger = logging.getLogger(__name__)

# Stesso criterio del database: su volume persistente quando c'e'
POSITIONS_PATH = config.POSITIONS_PATH


class PositionStore:
    """Prezzo di carico e soglie di uscita, per indirizzo token."""

    def __init__(self, path: str = POSITIONS_PATH):
        self.path = path
        self.data: Dict[str, Dict[str, Any]] = {}
        self.load()

    def load(self):
        try:
            with open(self.path, encoding="utf-8") as fh:
                self.data = json.load(fh)
        except (OSError, ValueError):
            self.data = {}

    def save(self):
        try:
            with open(self.path, "w", encoding="utf-8") as fh:
                json.dump(self.data, fh, indent=2)
        except OSError as exc:
            logger.warning("Impossibile salvare %s: %s", self.path, exc)

    def get(self, address: str) -> Dict[str, Any]:
        return self.data.get(address.lower(), {})

    def addresses(self) -> List[str]:
        return list(self.data.keys())

    def record_buy(self, address: str, symbol: str, amount: float, cost_usd: float,
                   stop_loss_percent: float, take_profit_percent: float):
        """Aggiorna il prezzo medio di carico dopo un acquisto."""
        key = address.lower()
        prev = self.data.get(key, {})
        prev_amount = float(prev.get("amount", 0.0))
        prev_cost = float(prev.get("cost_usd", 0.0))

        total_amount = prev_amount + amount
        total_cost = prev_cost + cost_usd

        self.data[key] = {
            "address": key,
            "symbol": symbol,
            "amount": total_amount,
            "cost_usd": total_cost,
            "entry_price": (total_cost / total_amount) if total_amount else 0.0,
            "stop_loss_percent": stop_loss_percent,
            "take_profit_percent": take_profit_percent,
            "first_buy_at": prev.get("first_buy_at") or time.time(),
            "last_buy_at": time.time(),
        }
        self.save()

    def record_sell(self, address: str, amount_sold: float, proceeds_usd: float):
        """Riduce la posizione proporzionalmente; a zero la rimuove."""
        key = address.lower()
        entry = self.data.get(key)
        if not entry:
            return

        prev_amount = float(entry.get("amount", 0.0))
        if prev_amount <= 0:
            self.data.pop(key, None)
            self.save()
            return

        sold_fraction = min(1.0, amount_sold / prev_amount)
        remaining = prev_amount - amount_sold
        entry["amount"] = max(0.0, remaining)
        entry["cost_usd"] = float(entry.get("cost_usd", 0.0)) * (1 - sold_fraction)
        entry["realized_usd"] = float(entry.get("realized_usd", 0.0)) + proceeds_usd
        entry["last_sell_at"] = time.time()

        if entry["amount"] <= 0:
            self.data.pop(key, None)
        else:
            self.data[key] = entry
        self.save()

    def forget(self, address: str):
        self.data.pop(address.lower(), None)
        self.save()


class Portfolio:
    """Fotografia del wallet: ETH, USDC e token, valorizzati in USD."""

    def __init__(self, client: BaseClient, uniswap: UniswapV3 = None,
                 store: PositionStore = None):
        self.client = client
        self.uniswap = uniswap or UniswapV3(client)
        self.store = store or PositionStore()

    def _price_usd(self, address: str, fallback: Optional[float] = None) -> Optional[float]:
        """Prezzo on-chain via Uniswap; se la rotta manca usa il fallback passato."""
        try:
            price = self.uniswap.price_in_quote(address, config.USDC)
            if price:
                return price
        except Exception as exc:
            logger.debug("Prezzo on-chain non disponibile per %s: %s", address, exc)
        return fallback

    def tracked_addresses(self, universe: List[Dict[str, Any]] = None) -> List[str]:
        addresses = {meta["address"].lower() for meta in config.CORE_TOKENS.values()}
        addresses.update(self.store.addresses())
        for token in (universe or []):
            addresses.add(token["address"].lower())
        return list(addresses)

    def snapshot(self, universe: List[Dict[str, Any]] = None) -> Dict[str, Any]:
        if config.PAPER_TRADING:
            return self.paper_snapshot(universe or [])
        return self.onchain_snapshot(universe or [])

    def paper_snapshot(self, universe: List[Dict[str, Any]]) -> Dict[str, Any]:
        """Stessi campi dello snapshot reale, ma con i saldi virtuali."""
        from paper import PaperWallet

        paper = getattr(self, "_paper", None) or PaperWallet()
        self._paper = paper

        prices_hint = {t["address"].lower(): t.get("price_usd") for t in universe}
        eth_price = self._price_usd(config.WETH) or 0.0

        holdings: List[Dict[str, Any]] = []
        tokens_value = 0.0

        for address, holding in list(paper.tokens().items()):
            amount = float(holding.get("amount", 0.0))
            if amount <= 0:
                continue

            price = self._price_usd(address, prices_hint.get(address))
            value_usd = (price or 0.0) * amount

            position = self.store.get(address)
            entry_price = float(position.get("entry_price", 0.0))
            pnl_percent = None
            if entry_price and price:
                pnl_percent = ((price - entry_price) / entry_price) * 100

            holdings.append({
                "symbol": holding.get("symbol", "?"),
                "address": address,
                "amount": amount,
                "price_usd": price,
                "value_usd": value_usd,
                "entry_price": entry_price or None,
                "cost_usd": float(position.get("cost_usd", 0.0)) or None,
                "pnl_percent": pnl_percent,
                "stop_loss_percent": position.get("stop_loss_percent"),
                "take_profit_percent": position.get("take_profit_percent"),
                "held_since": position.get("first_buy_at"),
            })
            tokens_value += value_usd

        holdings.sort(key=lambda h: h["value_usd"], reverse=True)
        eth_value = paper.eth * eth_price
        total = paper.usdc + tokens_value + eth_value

        summary = paper.summary()
        return {
            "wallet": self.client.address,
            "dry_run": True,
            "paper_trading": True,
            "paper": summary,
            "eth_balance": paper.eth,
            "eth_price_usd": eth_price,
            "eth_value_usd": eth_value,
            "usdc_balance": paper.usdc,
            "tokens_value_usd": tokens_value,
            "total_value_usd": total,
            "open_positions": holdings,
            "free_capital_usd": paper.usdc,
            # P&L della sola strategia: l'ETH iniziale e' valutato al prezzo
            # corrente, cosi' il movimento di ETH non viene contato come merito
            # o colpa del bot
            "pnl_since_start_usd": total - (summary["initial_usdc"] + paper.eth * eth_price),
        }

    def onchain_snapshot(self, universe: List[Dict[str, Any]] = None) -> Dict[str, Any]:
        universe = universe or []
        prices_hint = {t["address"].lower(): t.get("price_usd") for t in universe}

        eth_balance = self.client.eth_balance()
        eth_price = self._price_usd(config.WETH) or 0.0

        holdings: List[Dict[str, Any]] = []
        usdc_balance = 0.0
        tokens_value = 0.0

        for address in self.tracked_addresses(universe):
            try:
                amount = self.client.balance_of_float(address)
            except Exception as exc:
                logger.warning("balanceOf fallita per %s: %s", address, exc)
                continue
            if amount <= 0:
                continue

            symbol = self.client.symbol(address)

            if address.lower() == config.USDC.lower():
                usdc_balance = amount
                continue

            price = self._price_usd(address, prices_hint.get(address.lower()))
            value_usd = (price or 0.0) * amount
            if value_usd < config.DUST_USD:
                continue

            position = self.store.get(address)
            entry_price = float(position.get("entry_price", 0.0))
            pnl_percent = None
            if entry_price and price:
                pnl_percent = ((price - entry_price) / entry_price) * 100

            holdings.append({
                "symbol": symbol,
                "address": address,
                "amount": amount,
                "price_usd": price,
                "value_usd": value_usd,
                "entry_price": entry_price or None,
                "cost_usd": float(position.get("cost_usd", 0.0)) or None,
                "pnl_percent": pnl_percent,
                "stop_loss_percent": position.get("stop_loss_percent"),
                "take_profit_percent": position.get("take_profit_percent"),
                "held_since": position.get("first_buy_at"),
            })
            tokens_value += value_usd

        holdings.sort(key=lambda h: h["value_usd"], reverse=True)
        eth_value = eth_balance * eth_price
        total = usdc_balance + tokens_value + eth_value

        return {
            "wallet": self.client.address,
            "dry_run": config.DRY_RUN,
            "eth_balance": eth_balance,
            "eth_price_usd": eth_price,
            "eth_value_usd": eth_value,
            "usdc_balance": usdc_balance,
            "tokens_value_usd": tokens_value,
            "total_value_usd": total,
            "open_positions": holdings,
            "free_capital_usd": usdc_balance,
        }

    def risk_exits(self, snapshot: Dict[str, Any]) -> List[Dict[str, Any]]:
        """Posizioni che hanno sfondato stop loss o take profit."""
        exits = []
        for holding in snapshot.get("open_positions", []):
            pnl = holding.get("pnl_percent")
            if pnl is None:
                continue

            stop_loss = holding.get("stop_loss_percent") or config.DEFAULT_STOP_LOSS_PCT
            take_profit = holding.get("take_profit_percent") or config.DEFAULT_TAKE_PROFIT_PCT

            if pnl <= -abs(stop_loss):
                exits.append({**holding, "trigger": "stop_loss", "pnl_percent": pnl})
            elif pnl >= abs(take_profit):
                exits.append({**holding, "trigger": "take_profit", "pnl_percent": pnl})
        return exits
