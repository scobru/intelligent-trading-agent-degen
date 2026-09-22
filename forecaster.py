import logging
import traceback

import pandas as pd
from prophet import Prophet
import warnings

import market_data
warnings.filterwarnings('ignore')

logger = logging.getLogger(__name__)


def _first_valid_freq(*candidates: str) -> str:
    """
    Restituisce il primo alias di frequenza accettato dalla versione di pandas
    installata. pandas < 2.2 usa 'H'/'T', pandas >= 3.0 accetta solo 'h'/'min'.
    """
    for candidate in candidates:
        try:
            pd.tseries.frequencies.to_offset(candidate)
            return candidate
        except Exception:
            continue
    return candidates[0]


# Alias risolti una sola volta all'import
FREQ_15M = _first_valid_freq("15min", "15T")
FREQ_1H = _first_valid_freq("h", "H")


def _round_price(value):
    """
    Arrotondamento a cifre significative: una meme a 0.00000012 con
    round(x, 2) diventerebbe 0.0, cioe' il prezzo sparirebbe.
    """
    if value is None:
        return None
    value = float(value)
    if value == 0:
        return 0.0
    magnitude = abs(value)
    if magnitude >= 1:
        return round(value, 2)
    if magnitude >= 0.01:
        return round(value, 6)
    # sotto il centesimo si tengono 6 cifre significative
    import math
    exponent = math.floor(math.log10(magnitude))
    return round(value, -exponent + 5)

class ForecastError(RuntimeError):
    """Errore di previsione che conserva l'ultimo prezzo noto, se disponibile."""

    def __init__(self, message: str, last_price=None):
        super().__init__(message)
        self.last_price = last_price


class CryptoForecaster:
    """
    Forecasting con Prophet basato sui dati storici recuperati tramite CCXT.
    Compatibile con l'interfaccia originale di HyperliquidForecaster.
    """

    def __init__(self, testnet: bool = False):
        self.last_prices = {}

    def _fetch_candles(self, coin: str, interval: str, limit: int) -> pd.DataFrame:
        """Serie storica da market_data (CEX per i major, pool su Base per il resto)."""
        df = market_data.get_ohlcv(coin, interval=interval, limit=limit)
        if df.empty:
            raise RuntimeError(f"Nessuna candela disponibile per {coin} {interval}")

        out = pd.DataFrame({
            "ds": df["timestamp"].dt.tz_convert(None),
            "y": df["close"].astype(float),
        })
        return out.sort_values("ds").reset_index(drop=True)

    def forecast(self, coin: str, interval: str) -> tuple:
        if interval == "15m":
            df = self._fetch_candles(coin, "15m", limit=300)
            freq = FREQ_15M
        else:
            df = self._fetch_candles(coin, "1h", limit=500)
            if len(df) < 48:
                raise ForecastError(
                    f"Storico orario troppo corto per {coin} ({len(df)} candele)",
                    last_price=float(df["y"].iloc[-1]) if len(df) else None,
                )
            freq = FREQ_1H

        last_price = float(df["y"].iloc[-1])
        self.last_prices[f"{coin.upper()}:{interval}"] = last_price

        # Da qui in poi il prezzo è noto: se Prophet fallisce lo conserviamo
        # nell'eccezione, così la dashboard può comunque mostrare un valore reale.
        try:
            model = Prophet(daily_seasonality=True, weekly_seasonality=True)
            model.fit(df)

            future = model.make_future_dataframe(periods=1, freq=freq)
            forecast = model.predict(future)
        except Exception as exc:
            raise ForecastError(
                f"Prophet fallito per {coin} {interval} (freq={freq}): {type(exc).__name__}: {exc}",
                last_price=last_price,
            ) from exc

        return forecast.tail(1)[["ds", "yhat", "yhat_lower", "yhat_upper"]], last_price

    def forecast_many(self, tickers: list, intervals=("15m", "1h")):
        results = []
        for coin in tickers:
            for interval in intervals:
                timeframe = "Prossimi 15 Minuti" if interval == "15m" else "Prossima Ora"
                try:
                    forecast_data, last_price = self.forecast(coin, interval)
                    fc = forecast_data.iloc[0]
                    variazione_pct = ((fc["yhat"] - last_price) / last_price) * 100

                    results.append({
                        "Ticker": coin,
                        "Timeframe": timeframe,
                        "Ultimo Prezzo": _round_price(last_price),
                        "Previsione": _round_price(fc["yhat"]),
                        "Limite Inferiore": _round_price(fc["yhat_lower"]),
                        "Limite Superiore": _round_price(fc["yhat_upper"]),
                        "Variazione %": round(variazione_pct, 2),
                        "Timestamp Previsione": fc["ds"]
                    })
                except Exception as e:
                    # L'errore veniva ingoiato in silenzio: senza log era impossibile
                    # capire perché una riga risultasse vuota (es. $0 in dashboard).
                    last_price = getattr(e, "last_price", None)
                    print(f"⚠️  Previsione fallita per {coin} {interval}: {type(e).__name__}: {e}")
                    logger.warning(
                        "Previsione fallita per %s %s: %s", coin, interval, e,
                        exc_info=True,
                    )
                    logger.debug(traceback.format_exc())

                    results.append({
                        "Ticker": coin,
                        "Timeframe": timeframe,
                        "Ultimo Prezzo": _round_price(last_price),
                        "Previsione": None,
                        "Limite Inferiore": None,
                        "Limite Superiore": None,
                        "Variazione %": None,
                        "Timestamp Previsione": None,
                        "error": f"{type(e).__name__}: {e}"
                    })
        return results

    def get_crypto_forecasts(self, tickers: list):
        self._last_results = self.forecast_many(tickers, intervals=("15m", "1h"))
        df = pd.DataFrame(self._last_results)
        if "error" in df.columns:
            df = df.drop("error", axis=1)
        return df.to_string(index=False)

# Alias retrocompatibilità
# Alias storico mantenuto per i moduli che lo importano
HyperliquidForecaster = CryptoForecaster

def get_forecasts_text(tickers=["BTC", "ETH", "SOL"], testnet=False):
    forecaster = CryptoForecaster(testnet=testnet)
    return forecaster.get_crypto_forecasts(tickers)

def get_crypto_forecasts(tickers=["BTC", "ETH", "SOL"], testnet=False):
    try:
        forecaster = CryptoForecaster(testnet=testnet)
        results = forecaster.forecast_many(tickers)
        df = pd.DataFrame(results)
        # Il testo va nel prompt dell'LLM: fuori la colonna diagnostica.
        # Il JSON conserva l'errore e finisce nel DB per il debug.
        df_txt = df.drop(columns=["error"], errors="ignore")
        return df_txt.to_string(index=False), df.to_json(orient="records")
    except Exception as exc:
        print(f"❌ Previsioni Prophet non disponibili: {type(exc).__name__}: {exc}")
        logger.error("get_crypto_forecasts fallito: %s", exc, exc_info=True)
        return None, None
