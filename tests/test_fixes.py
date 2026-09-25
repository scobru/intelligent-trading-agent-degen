import os
import sys
import json
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import market_data
from trading_agent import _clean_and_parse_json


def test_dashboard_imports():
    import dashboard
    assert hasattr(dashboard, "db_utils")


def test_clean_and_parse_json_with_safety_preamble():
    raw_text = """User Safety: safe
```json
{
    "operation": "buy",
    "token": "BNKR",
    "target_portion_of_portfolio": 0.1,
    "slippage_bps": 50,
    "stop_loss_percent": 10.0,
    "take_profit_percent": 30.0,
    "reason": "Test reason"
}
```"""
    parsed = _clean_and_parse_json(raw_text)
    assert parsed["operation"] == "buy"
    assert parsed["token"] == "BNKR"
    assert parsed["target_portion_of_portfolio"] == 0.1


def test_market_data_register_holdings_and_lookup():
    holdings = [
        {"symbol": "EDEL", "address": "0x1234567890123456789012345678901234567890", "price_usd": 0.05},
        {"symbol": "PLAY", "address": "0xabcdefabcdefabcdefabcdefabcdefabcdefabcd", "price_usd": 1.20},
    ]
    market_data.register_holdings(holdings)

    lookup_edel = market_data.lookup("EDEL")
    assert lookup_edel is not None
    assert lookup_edel["address"] == "0x1234567890123456789012345678901234567890"

    lookup_play = market_data.lookup("PLAY")
    assert lookup_play is not None
    assert lookup_play["symbol"] == "PLAY"

    lookup_core = market_data.lookup("ETH")
    assert lookup_core is not None


def test_spot_trader_release_funds():
    from unittest.mock import MagicMock
    from spot_trader import SpotTrader
    
    client = MagicMock()
    trader = SpotTrader(client=client)
    
    # Mock portfolio snapshot with 2 token positions
    trader.portfolio.snapshot = MagicMock(return_value={
        "open_positions": [
            {"symbol": "BNKR", "address": "0x1111", "amount": 1000.0, "value_usd": 20.0},
            {"symbol": "PLAY", "address": "0x2222", "amount": 50.0, "value_usd": 15.0}
        ],
        "usdc_balance": 1.90
    })
    
    trader.sell = MagicMock(return_value={"status": "paper", "expected_usd": 10.0, "tx_hash": "0x_mock_sell"})
    
    res = trader.release_funds(target_usdc=10.0)
    assert res["status"] == "success"
    assert res["released_usd"] == 10.0
    trader.sell.assert_called_once()

