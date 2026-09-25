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
