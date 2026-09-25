"""
Esecuzione dei segnali sul wallet: l'equivalente spot di SynFuturesTrader.

Nessun protocollo di terze parti — solo swap Uniswap V3 dal wallet — e
nessun ordine che non abbia superato i limiti di rischio qui sotto.
"""

import logging
from typing import Any, Dict, List, Optional

from web3 import Web3

import config
import token_screener
from base_client import BaseChainError, BaseClient
from uniswap import UniswapV3
from wallet import Portfolio, PositionStore

logger = logging.getLogger(__name__)


class RiskRejection(RuntimeError):
    """Il segnale viola un limite di rischio: non si esegue."""


class SpotTrader:
    def __init__(self, client: BaseClient = None, uniswap: UniswapV3 = None,
                 portfolio: Portfolio = None):
        self.client = client or BaseClient()
        self.uniswap = uniswap or UniswapV3(self.client)
        self.store = PositionStore()
        self.portfolio = portfolio or Portfolio(self.client, self.uniswap, self.store)
        self._universe: List[Dict[str, Any]] = []

    # ------------------------------------------------------------ auto-refuel
    def ensure_usdc_balance(self) -> Optional[Dict[str, Any]]:
        """
        Se in modalita' on-chain e il saldo USDC e' insufficiente,
        ma c'e' ETH spendibile oltre la riserva gas, effettua l'auto-refuel.
        """
        if config.PAPER_TRADING or not self.client:
            return None
        return self.uniswap.auto_refuel_usdc()

    # ------------------------------------------------------------ stato
    def set_universe(self, universe: List[Dict[str, Any]]):
        self._universe = universe or []

    def get_account_status(self, universe: List[Dict[str, Any]] = None) -> Dict[str, Any]:
        return self.portfolio.snapshot(universe if universe is not None else self._universe)

    def resolve_token(self, identifier: str) -> Optional[Dict[str, Any]]:
        """Accetta sia un simbolo dell'universo sia un indirizzo 0x."""
        if not identifier:
            return None
        ident = identifier.strip()

        if ident.startswith("0x") and len(ident) == 42:
            address = ident.lower()
            for token in self._universe:
                if token["address"].lower() == address:
                    return token
            return {"address": address, "symbol": self.client.symbol(address)}

        upper = ident.upper()
        for token in self._universe:
            if token["symbol"].upper() == upper:
                return token
        if upper in config.CORE_TOKENS:
            meta = config.CORE_TOKENS[upper]
            return {"address": meta["address"], "symbol": upper}

        held = self.get_account_status().get("open_positions", [])
        for holding in held:
            if holding["symbol"].upper() == upper:
                return {"address": holding["address"], "symbol": holding["symbol"]}
        return None

    # ------------------------------------------------------------ limiti
    def _check_buy_limits(self, token: Dict[str, Any], usd_amount: float,
                          snapshot: Dict[str, Any]) -> float:
        total = snapshot["total_value_usd"]
        free = snapshot["free_capital_usd"]

        if usd_amount < config.MIN_TRADE_USD:
            raise RiskRejection(
                f"Importo ${usd_amount:.2f} sotto il minimo di ${config.MIN_TRADE_USD:.2f}"
            )

        usd_amount = min(usd_amount, config.MAX_TRADE_USD)

        if usd_amount > free:
            usd_amount = free
        if usd_amount < config.MIN_TRADE_USD:
            raise RiskRejection(
                f"USDC disponibili (${free:.2f}) insufficienti per il trade minimo"
            )

        # tetto per singola posizione, tenendo conto di quanto gia' detenuto
        held_value = sum(
            h["value_usd"] for h in snapshot["open_positions"]
            if h["address"].lower() == token["address"].lower()
        )
        max_position = total * config.MAX_POSITION_PCT
        if held_value + usd_amount > max_position:
            usd_amount = max_position - held_value
            if usd_amount < config.MIN_TRADE_USD:
                raise RiskRejection(
                    f"{token['symbol']} e' gia' al tetto del "
                    f"{config.MAX_POSITION_PCT:.0%} del portafoglio"
                )

        distinct = {h["address"].lower() for h in snapshot["open_positions"]}
        if (token["address"].lower() not in distinct
                and len(distinct) >= config.MAX_OPEN_TOKENS):
            raise RiskRejection(
                f"Gia' {len(distinct)} posizioni aperte, il massimo e' {config.MAX_OPEN_TOKENS}"
            )

        if not config.PAPER_TRADING and snapshot["eth_balance"] < config.MIN_ETH_RESERVE:
            raise RiskRejection(
                f"ETH per il gas insufficiente: {snapshot['eth_balance']:.5f} "
                f"< riserva minima {config.MIN_ETH_RESERVE}"
            )

        return usd_amount

    # ------------------------------------------------------------ operazioni
    def buy(self, token: Dict[str, Any], usd_amount: float, slippage_bps: int,
            stop_loss_percent: float, take_profit_percent: float,
            snapshot: Dict[str, Any] = None) -> Dict[str, Any]:
        snapshot = snapshot or self.get_account_status()

        verdict = token_screener.is_tradable(token["address"])
        if not verdict.get("approved"):
            raise RiskRejection(
                f"{token['symbol']} non ha passato lo screening: "
                f"{'; '.join(verdict.get('reasons', [])) or 'motivo sconosciuto'}"
            )

        usd_amount = self._check_buy_limits(token, usd_amount, snapshot)

        usdc_decimals = self.client.decimals(config.USDC)
        amount_in = int(usd_amount * (10 ** usdc_decimals))

        route = self.uniswap.best_route(config.USDC, token["address"], amount_in)
        if not route:
            raise RiskRejection(
                f"Nessuna rotta Uniswap V3 da USDC a {token['symbol']}"
            )

        token_decimals = self.client.decimals(token["address"])
        expected_tokens = route.amount_out / (10 ** token_decimals)

        if config.PAPER_TRADING:
            result = self._paper_fill("buy", token, route, slippage_bps,
                                      usd_amount, expected_tokens)
            # il riempimento simulato sconta lo slippage: e' quello il numero
            # da registrare, non la quotazione piena
            expected_tokens = result["expected_tokens"]
        else:
            result = self.uniswap.swap(route, slippage_bps)

        result.update({
            "operation": "buy",
            "symbol": token["symbol"],
            "address": token["address"],
            "usd_amount": usd_amount,
            "expected_tokens": expected_tokens,
            "route_description": route.describe(self.client),
        })

        # In dry-run puro il registro non si tocca: non possediamo nulla di
        # nuovo. In paper trading invece la posizione virtuale esiste davvero.
        if result.get("status") in ("success", "paper"):
            self.store.record_buy(
                token["address"], token["symbol"], expected_tokens, usd_amount,
                stop_loss_percent, take_profit_percent,
            )
        return result

    def sell(self, token: Dict[str, Any], portion: float, slippage_bps: int,
             snapshot: Dict[str, Any] = None) -> Dict[str, Any]:
        snapshot = snapshot or self.get_account_status()
        portion = max(0.0, min(1.0, portion))

        holding = next(
            (h for h in snapshot["open_positions"]
             if h["address"].lower() == token["address"].lower()),
            None,
        )
        if not holding:
            raise RiskRejection(f"Nessuna posizione aperta su {token['symbol']}")

        token_decimals = self.client.decimals(token["address"])
        if config.PAPER_TRADING:
            # in paper trading la posizione vive nel portafoglio virtuale:
            # on-chain quel token non lo possediamo affatto
            raw_balance = int(self.paper.token_amount(token["address"]) * (10 ** token_decimals))
        else:
            raw_balance = self.client.balance_of(token["address"])
        amount_in = int(raw_balance * portion)
        if amount_in <= 0:
            raise RiskRejection(f"Quantita' da vendere nulla per {token['symbol']}")

        value_usd = holding["value_usd"] * portion
        if value_usd < config.MIN_TRADE_USD and portion < 1.0:
            raise RiskRejection(
                f"Vendita di ${value_usd:.2f} sotto il minimo di ${config.MIN_TRADE_USD:.2f}"
            )

        route = self.uniswap.best_route(token["address"], config.USDC, amount_in)
        if not route:
            raise RiskRejection(
                f"Nessuna rotta Uniswap V3 da {token['symbol']} a USDC "
                f"(attenzione: potrebbe essere un honeypot)"
            )

        usdc_decimals = self.client.decimals(config.USDC)
        expected_usd = route.amount_out / (10 ** usdc_decimals)

        if config.PAPER_TRADING:
            result = self._paper_fill("sell", token, route, slippage_bps,
                                      expected_usd, amount_in / (10 ** token_decimals))
            expected_usd = result["expected_usd"]
        else:
            result = self.uniswap.swap(route, slippage_bps)

        result.update({
            "operation": "sell",
            "symbol": token["symbol"],
            "address": token["address"],
            "portion": portion,
            "tokens_sold": amount_in / (10 ** token_decimals),
            "expected_usd": expected_usd,
            "route_description": route.describe(self.client),
        })

        if result.get("status") in ("success", "paper"):
            self.store.record_sell(
                token["address"], amount_in / (10 ** token_decimals), expected_usd
            )
        return result

    # ------------------------------------------------------------ paper trading
    @property
    def paper(self):
        """Portafoglio virtuale, condiviso con lo snapshot del portafoglio."""
        from paper import PaperWallet

        existing = getattr(self.portfolio, "_paper", None)
        if existing is None:
            existing = PaperWallet()
            self.portfolio._paper = existing
        return existing

    def _paper_fill(self, side: str, token: Dict[str, Any], route, slippage_bps: int,
                    usd_amount: float, token_amount: float) -> Dict[str, Any]:
        """
        Esegue l'ordine contro il saldo virtuale, al prezzo che la rotta ha
        realmente quotato. Il riempimento sconta lo slippage massimo: meglio
        una simulazione pessimista di una ottimista.
        """
        slippage_factor = (10_000 - slippage_bps) / 10_000

        try:
            if side == "buy":
                filled_tokens = token_amount * slippage_factor
                self.paper.buy(token["address"], token["symbol"], usd_amount,
                               filled_tokens, config.PAPER_GAS_USD)
                filled = {"expected_tokens": filled_tokens}
            else:
                filled_usd = usd_amount * slippage_factor
                self.paper.sell(token["address"], token_amount, filled_usd,
                                config.PAPER_GAS_USD)
                filled = {"expected_usd": filled_usd}
        except ValueError as exc:
            raise RiskRejection(str(exc)) from exc

        result = {
            "status": "paper",
            "description": f"swap simulato {route.describe(self.client)}",
            "route": route.to_dict(),
            "slippage_bps": slippage_bps,
            "gas_usd": config.PAPER_GAS_USD,
            "paper_balances": self.paper.summary(),
        }
        result.update(filled)
        return result

    # ------------------------------------------------------------ segnale LLM
    def execute_signal(self, signal: Dict[str, Any]) -> Dict[str, Any]:
        """Traduce il JSON del modello in uno swap, o spiega perche' non lo fa."""
        operation = str(signal.get("operation", "hold")).lower()
        identifier = signal.get("token") or signal.get("symbol") or ""

        if operation == "hold":
            return {"status": "hold", "message": f"Nessuna azione ({signal.get('reason', '')[:120]})"}

        token = self.resolve_token(identifier)
        if not token:
            return {
                "status": "rejected",
                "message": f"Token '{identifier}' non riconosciuto nell'universo corrente",
            }

        slippage_bps = int(signal.get("slippage_bps") or config.DEFAULT_SLIPPAGE_BPS)
        slippage_bps = max(10, min(slippage_bps, config.MAX_SLIPPAGE_BPS))
        snapshot = self.get_account_status()

        try:
            if operation == "buy":
                portion = float(signal.get("target_portion_of_portfolio") or 0.0)
                usd_amount = snapshot["total_value_usd"] * portion
                return self.buy(
                    token,
                    usd_amount,
                    slippage_bps,
                    float(signal.get("stop_loss_percent") or config.DEFAULT_STOP_LOSS_PCT),
                    float(signal.get("take_profit_percent") or config.DEFAULT_TAKE_PROFIT_PCT),
                    snapshot,
                )

            if operation == "sell":
                portion = float(signal.get("sell_portion") or 1.0)
                return self.sell(token, portion, slippage_bps, snapshot)

            return {"status": "rejected", "message": f"Operazione '{operation}' sconosciuta"}

        except RiskRejection as exc:
            logger.warning("Segnale rifiutato dai limiti di rischio: %s", exc)
            return {"status": "rejected", "message": str(exc)}
        except (BaseChainError, Exception) as exc:
            logger.error("Errore on-chain / esecuzione swap: %s", exc)
            return {"status": "error", "message": str(exc)}

    def liquidate(self, holding: Dict[str, Any], reason: str) -> Dict[str, Any]:
        """Uscita integrale, usata dagli stop loss / take profit automatici."""
        token = {"address": holding["address"], "symbol": holding["symbol"]}
        try:
            result = self.sell(token, 1.0, config.DEFAULT_SLIPPAGE_BPS)
            result["trigger"] = reason
            return result
        except (RiskRejection, BaseChainError, Exception) as exc:
            return {"status": "error", "symbol": holding["symbol"], "trigger": reason,
                    "message": str(exc)}
