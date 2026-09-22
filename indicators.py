import pandas as pd
import numpy as np
import ta
from datetime import datetime, timezone
from typing import Dict, List, Tuple, Optional

import market_data
from market_data import format_price

# Costante Fee Taker stimata (0.04%)
TAKER_FEE_RATE = 0.0004

INTERVAL_TO_MS = {
    "1m": 60_000,
    "5m": 5 * 60_000,
    "15m": 15 * 60_000,
    "1h": 60 * 60_000,
    "4h": 4 * 60 * 60_000,
    "1d": 24 * 60 * 60_000,
}

class CryptoTechnicalAnalysis:
    """
    Analisi tecnica su timeframe 15 minuti.

    Le candele arrivano da market_data, che sceglie da solo fra CEX (major)
    e pool Uniswap/Aerodrome su Base (token e meme).
    """

    def __init__(self, testnet: bool = False):
        self.testnet = testnet

    # ==============================
    #       FETCH OHLCV
    # ==============================
    def fetch_ohlcv(self, coin: str, interval: str, limit: int = 500) -> pd.DataFrame:
        """Candele OHLCV, indifferentemente da CEX o da pool su Base."""
        df = market_data.get_ohlcv(coin, interval=interval, limit=limit)
        if df.empty:
            raise RuntimeError(f"Nessuna candela ricevuta per {coin} ({interval})")
        return df

    def get_spot_context(self, ticker: str) -> Dict[str, float]:
        """
        Sostituisce open interest e funding del bot perp: su spot contano
        liquidita' della pool, volume e variazioni recenti.
        """
        return market_data.get_market_details(ticker)

    # ==============================
    #       INDICATORI TECNICI
    # ==============================
    def calculate_ema(self, data: pd.Series, period: int) -> pd.Series:
        return ta.trend.EMAIndicator(data, window=period).ema_indicator()

    def calculate_macd(self, data: pd.Series) -> Tuple[pd.Series, pd.Series, pd.Series]:
        macd = ta.trend.MACD(data)
        return macd.macd(), macd.macd_signal(), macd.macd_diff()

    def calculate_rsi(self, data: pd.Series, period: int) -> pd.Series:
        return ta.momentum.RSIIndicator(data, window=period).rsi()

    def calculate_atr(
        self, high: pd.Series, low: pd.Series, close: pd.Series, period: int
    ) -> pd.Series:
        return ta.volatility.AverageTrueRange(
            high, low, close, window=period
        ).average_true_range()

    def calculate_pivot_points(
        self, high: float, low: float, close: float
    ) -> Dict[str, float]:
        pp = (high + low + close) / 3.0
        s1 = (2 * pp) - high
        s2 = pp - (high - low)
        r1 = (2 * pp) - low
        r2 = pp + (high - low)
        return {"pp": pp, "s1": s1, "s2": s2, "r1": r1, "r2": r2}

    # ==============================
    #       ANALISI COMPLETA A 15m
    # ==============================
    def get_complete_analysis(self, ticker: str) -> Dict:
        coin = ticker.upper()

        # 1) DATI 15 MINUTI
        df_15m = self.fetch_ohlcv(coin, "15m", limit=200)

        df_15m["ema_20"] = self.calculate_ema(df_15m["close"], 20)
        macd_line, signal_line, macd_diff = self.calculate_macd(df_15m["close"])
        df_15m["macd"] = macd_diff
        df_15m["rsi_7"] = self.calculate_rsi(df_15m["close"], 7)
        df_15m["rsi_14"] = self.calculate_rsi(df_15m["close"], 14)

        last_10_15m = df_15m.tail(10)

        # 2) CONTESTO longer term a 15m
        longer_term = df_15m.tail(50).copy()
        longer_term["ema_20"] = self.calculate_ema(longer_term["close"], 20)
        longer_term["ema_50"] = self.calculate_ema(longer_term["close"], 50)
        longer_term["atr_3"] = self.calculate_atr(
            longer_term["high"], longer_term["low"], longer_term["close"], 3
        )
        longer_term["atr_14"] = self.calculate_atr(
            longer_term["high"], longer_term["low"], longer_term["close"], 14
        )
        macd_15m_long, _, macd_diff_15m_long = self.calculate_macd(
            longer_term["close"]
        )
        longer_term["macd"] = macd_diff_15m_long
        longer_term["rsi_14"] = self.calculate_rsi(longer_term["close"], 14)

        avg_volume = longer_term["volume"].tail(20).mean()
        last_10_longer = longer_term.tail(10)

        # 3) PIVOT POINTS daily
        try:
            df_daily = self.fetch_ohlcv(coin, "1d", limit=2)
            if len(df_daily) >= 2:
                prev_day = df_daily.iloc[-2]
                pivot_points = self.calculate_pivot_points(
                    prev_day["high"], prev_day["low"], prev_day["close"]
                )
            else:
                last = df_15m.iloc[-1]
                pivot_points = self.calculate_pivot_points(
                    last["high"], last["low"], last["close"]
                )
        except Exception:
            last = df_15m.iloc[-1]
            pivot_points = self.calculate_pivot_points(
                last["high"], last["low"], last["close"]
            )

        spot = self.get_spot_context(coin)
        mark_px = spot.get("price_usd") or 0.0

        if mark_px == 0:
            mark_px = df_15m.iloc[-1]["close"]

        estimated_fee = mark_px * TAKER_FEE_RATE

        current_15m = df_15m.iloc[-1]
        current_longer = longer_term.iloc[-1]

        result = {
            "ticker": ticker,
            "timestamp": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S"),
            "current": {
                "price": current_15m["close"],
                "ema20": current_15m["ema_20"],
                "macd": current_15m["macd"],
                "rsi_7": current_15m["rsi_7"],
            },
            "pivot_points": pivot_points,
            "spot": {
                "price_usd": mark_px,
                "liquidity_usd": spot.get("liquidity_usd", 0.0),
                "volume_24h_usd": spot.get("volume_24h_usd", 0.0),
                "price_change_1h": spot.get("price_change_1h", 0.0),
                "price_change_24h": spot.get("price_change_24h", 0.0),
                "pool_age_days": spot.get("pool_age_days"),
                "source": spot.get("source", "unknown"),
                "estimated_fee_cost": estimated_fee,
            },
            "intraday": {
                "mid_prices": last_10_15m["close"].tolist(),
                "ema_20": last_10_15m["ema_20"].tolist(),
                "macd": last_10_15m["macd"].tolist(),
                "rsi_7": last_10_15m["rsi_7"].tolist(),
                "rsi_14": last_10_15m["rsi_14"].tolist(),
            },
            "longer_term_15m": {
                "ema_20_current": current_longer["ema_20"],
                "ema_50_current": current_longer["ema_50"],
                "atr_3_current": current_longer["atr_3"],
                "atr_14_current": current_longer["atr_14"],
                "volume_current": current_longer["volume"],
                "volume_average": avg_volume,
                "macd_series": last_10_longer["macd"].tolist(),
                "rsi_14_series": last_10_longer["rsi_14"].tolist(),
            },
        }
        return result

    def format_output(self, data: Dict) -> str:
        output = f"\n<{data['ticker']}_data>\n"
        output += f"Timestamp: {data['timestamp']} (UTC) (Base spot feed, 15m)\n\n"

        curr = data["current"]
        output += (
            f"current_price = {format_price(curr['price'])}, "
            f"current_ema20 = {format_price(curr['ema20'])}, "
            f"current_macd = {curr['macd']:.6f}, "
            f"current_rsi (7 period) = {curr['rsi_7']:.3f}\n\n"
        )

        pivot = data["pivot_points"]
        output += "Pivot Points (based on previous day):\n"
        output += (
            f"R2 = {format_price(pivot['r2'])}, R1 = {format_price(pivot['r1'])}, "
            f"PP = {format_price(pivot['pp'])}, "
            f"S1 = {format_price(pivot['s1'])}, S2 = {format_price(pivot['s2'])}\n\n"
        )

        spot = data["spot"]
        output += f"Spot market context for {data['ticker']} (source: {spot['source']}):\n"
        output += f"Pool liquidity: ${spot['liquidity_usd']:,.0f}\n"
        output += f"24h volume: ${spot['volume_24h_usd']:,.0f}\n"
        output += (
            f"Price change: {spot['price_change_1h']:+.2f}% (1h), "
            f"{spot['price_change_24h']:+.2f}% (24h)\n"
        )
        if spot.get("pool_age_days") is not None:
            output += f"Pool age: {spot['pool_age_days']:.1f} days\n"
        output += "\n"

        intra = data["intraday"]
        output += "Intraday series (15m, oldest → latest):\n"
        output += (
            f"Mid prices: {[format_price(x) for x in intra['mid_prices']]}\n"
            f"EMA indicators (20-period): {[round(x, 3) for x in intra['ema_20']]}\n"
            f"MACD indicators: {[round(x, 3) for x in intra['macd']]}\n"
            f"RSI indicators (7-Period): {[round(x, 3) for x in intra['rsi_7']]}\n"
            f"RSI indicators (14-Period): {[round(x, 3) for x in intra['rsi_14']]}\n\n"
        )

        lt = data["longer_term_15m"]
        output += "Longer-term context (still 15-minute timeframe, wider window):\n"
        output += (
            f"20-Period EMA: {format_price(lt['ema_20_current'])} vs. "
            f"50-Period EMA: {format_price(lt['ema_50_current'])}\n"
            f"3-Period ATR: {format_price(lt['atr_3_current'])} vs. "
            f"14-Period ATR: {format_price(lt['atr_14_current'])}\n"
            f"Current Volume: {lt['volume_current']:.3f} vs. "
            f"Average Volume: {lt['volume_average']:.3f}\n"
            f"MACD indicators: {[round(x, 3) for x in lt['macd_series']]}\n"
            f"RSI indicators (14-Period): {[round(x, 3) for x in lt['rsi_14_series']]}\n"
        )
        output += f"</{data['ticker']}_data>\n"
        return output


# Alias per retrocompatibilità
CryptoTechnicalAnalysisHL = CryptoTechnicalAnalysis

def analyze_multiple_tickers(tickers: List[str], testnet: bool = False) -> Tuple[str, List[Dict]]:
    analyzer = CryptoTechnicalAnalysis(testnet=testnet)
    full_output = ""
    datas = []
    for ticker in tickers:
        try:
            data = analyzer.get_complete_analysis(ticker)
            datas.append(data)
            full_output += analyzer.format_output(data)
        except Exception as e:
            print(f"Errore durante l'analisi di {ticker}: {e}")
            full_output += f"\nError analyzing {ticker}: {e}\n"
    return full_output, datas
