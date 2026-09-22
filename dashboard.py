import os
import json
import sqlite3
import threading
import subprocess
import time
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse
from datetime import datetime, timezone
from dotenv import load_dotenv

load_dotenv()

PORT = int(os.getenv("DASHBOARD_PORT", os.getenv("PORT", "3000")))
SQLITE_DB_PATH = os.getenv("SQLITE_DB_PATH", os.path.join(os.path.dirname(os.path.abspath(__file__)), "trading_agent.db"))

# Asset statici serviti dalla dashboard (allowlist esplicita: nessun path
# arbitrario arriva al filesystem)
STATIC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")
STATIC_ROUTES = {
    "/favicon.ico": ("favicon.ico", "image/x-icon"),
    "/static/icon.svg": ("icon.svg", "image/svg+xml"),
    "/static/icon-small.svg": ("icon-small.svg", "image/svg+xml"),
    "/static/icon-192.png": ("icon-192.png", "image/png"),
    "/static/icon-512.png": ("icon-512.png", "image/png"),
    "/static/apple-touch-icon.png": ("apple-touch-icon.png", "image/png"),
    "/static/site.webmanifest": ("site.webmanifest", "application/manifest+json"),
}

# Cache in-memory per dati live (60s)
_LIVE_CACHE = {
    "sentiment": None,
    "sentiment_time": 0,
    "news": None,
    "news_time": 0
}

HTML_TEMPLATE = """<!DOCTYPE html>
<html lang="it">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Degen Trading Agent - Dashboard</title>
    <link rel="icon" href="/favicon.ico" sizes="48x48">
    <link rel="icon" href="/static/icon.svg" type="image/svg+xml">
    <link rel="apple-touch-icon" href="/static/apple-touch-icon.png">
    <link rel="manifest" href="/static/site.webmanifest">
    <meta name="theme-color" content="#3b82f6">
    <link rel="preconnect" href="https://fonts.googleapis.com">
    <link href="https://fonts.googleapis.com/css2?family=Inter:wght@300;400;500;600;700;800&family=JetBrains+Mono:wght@400;500;600&display=swap" rel="stylesheet">
    <!-- Chart.js per la curva di equity -->
    <script src="https://cdn.jsdelivr.net/npm/chart.js"></script>
    <!-- TradingView Advanced Chart Widget -->
    <script type="text/javascript" src="https://s3.tradingview.com/tv.js"></script>
    <style>
        :root {
            --bg: #0b0f19;
            --surface: #131b2e;
            --surface-hover: #19243d;
            --surface-border: #1e2942;
            --primary: #3b82f6;
            --accent: #6366f1;
            --success: #10b981;
            --danger: #ef4444;
            --warning: #f59e0b;
            --text-main: #f3f4f6;
            --text-muted: #9ca3af;
        }
        * { box-sizing: border-box; margin: 0; padding: 0; }
        body {
            font-family: 'Inter', sans-serif;
            background: var(--bg);
            color: var(--text-main);
            padding: 24px;
            min-height: 100vh;
        }
        .header {
            display: flex;
            justify-content: space-between;
            align-items: center;
            margin-bottom: 24px;
            padding-bottom: 16px;
            border-bottom: 1px solid var(--surface-border);
            flex-wrap: wrap;
            gap: 16px;
        }
        .header h1 { font-size: 24px; font-weight: 700; display: flex; align-items: center; gap: 10px; }
        .badge {
            font-size: 12px;
            padding: 4px 10px;
            border-radius: 9999px;
            font-weight: 600;
        }
        .badge-live { background: rgba(16, 185, 129, 0.2); color: var(--success); border: 1px solid var(--success); }
        .badge-long { background: rgba(16, 185, 129, 0.2); color: var(--success); }
        .badge-short { background: rgba(239, 68, 68, 0.2); color: var(--danger); }
        .badge-bull { background: rgba(16, 185, 129, 0.15); color: #34d399; border: 1px solid rgba(16, 185, 129, 0.3); }
        .badge-bear { background: rgba(239, 68, 68, 0.15); color: #f87171; border: 1px solid rgba(239, 68, 68, 0.3); }
        .badge-neutral { background: rgba(156, 163, 175, 0.15); color: #9ca3af; border: 1px solid rgba(156, 163, 175, 0.3); }
        
        .btn {
            background: linear-gradient(135deg, var(--primary), var(--accent));
            color: #fff;
            border: none;
            padding: 10px 18px;
            border-radius: 8px;
            font-weight: 600;
            cursor: pointer;
            transition: all 0.2s ease;
            box-shadow: 0 2px 10px rgba(59, 130, 246, 0.3);
        }
        .btn:hover { opacity: 0.9; transform: translateY(-1px); }

        .grid-stats {
            display: grid;
            grid-template-columns: repeat(auto-fit, minmax(220px, 1fr));
            gap: 16px;
            margin-bottom: 24px;
        }
        .card {
            background: var(--surface);
            border: 1px solid var(--surface-border);
            border-radius: 12px;
            padding: 20px;
            box-shadow: 0 4px 20px rgba(0, 0, 0, 0.25);
        }
        .card h3 { font-size: 13px; font-weight: 600; color: var(--text-muted); text-transform: uppercase; letter-spacing: 0.5px; margin-bottom: 8px; }
        .card .value { font-size: 26px; font-weight: 700; }
        .card .subtext { font-size: 12px; color: var(--text-muted); margin-top: 4px; }

        /* Chart Controls & Tabs */
        .chart-header {
            display: flex;
            justify-content: space-between;
            align-items: center;
            flex-wrap: wrap;
            gap: 12px;
            margin-bottom: 16px;
        }
        .tab-group {
            display: flex;
            background: #090d16;
            padding: 4px;
            border-radius: 8px;
            border: 1px solid var(--surface-border);
            gap: 4px;
        }
        .tab-btn {
            background: transparent;
            color: var(--text-muted);
            border: none;
            padding: 8px 16px;
            border-radius: 6px;
            font-size: 13px;
            font-weight: 600;
            cursor: pointer;
            transition: all 0.2s ease;
        }
        .tab-btn.active {
            background: var(--surface-border);
            color: #ffffff;
            box-shadow: 0 2px 6px rgba(0, 0, 0, 0.3);
        }
        .tab-btn:hover:not(.active) { color: #ffffff; }
        
        .pill-group {
            display: flex;
            align-items: center;
            gap: 8px;
        }
        .pill-btn {
            background: #182238;
            color: var(--text-muted);
            border: 1px solid var(--surface-border);
            padding: 6px 14px;
            border-radius: 20px;
            font-size: 12px;
            font-weight: 600;
            cursor: pointer;
            transition: all 0.2s ease;
        }
        .pill-btn.active {
            background: rgba(59, 130, 246, 0.2);
            color: #60a5fa;
            border-color: #3b82f6;
        }
        .pill-btn:hover:not(.active) {
            border-color: #4b5563;
            color: #f3f4f6;
        }

        /* Decision Section Grid */
        .decision-section-title {
            font-size: 18px;
            font-weight: 700;
            margin: 28px 0 16px 0;
            display: flex;
            align-items: center;
            gap: 10px;
        }
        .intel-grid {
            display: grid;
            grid-template-columns: 1fr 1.2fr 1fr;
            gap: 16px;
            margin-bottom: 20px;
        }
        @media (max-width: 1100px) { .intel-grid { grid-template-columns: 1fr 1fr; } }
        @media (max-width: 768px) { .intel-grid { grid-template-columns: 1fr; } }

        /* Forecast item card */
        .forecast-item {
            background: #0d1424;
            border: 1px solid #1a233a;
            border-radius: 8px;
            padding: 10px 12px;
            margin-bottom: 8px;
            display: flex;
            justify-content: space-between;
            align-items: center;
        }
        .forecast-item:last-child { margin-bottom: 0; }
        .forecast-delta-pos { color: var(--success); font-weight: 700; font-size: 13px; }
        .forecast-delta-neg { color: var(--danger); font-weight: 700; font-size: 13px; }

        /* News item card */
        .news-item {
            padding: 8px 10px;
            background: #0d1424;
            border-left: 3px solid var(--primary);
            border-radius: 0 6px 6px 0;
            margin-bottom: 8px;
            font-size: 12px;
            line-height: 1.4;
        }
        .news-item:last-child { margin-bottom: 0; }
        .news-time { color: var(--text-muted); font-size: 10px; display: block; margin-top: 3px; }

        .main-grid {
            display: grid;
            grid-template-columns: 2fr 1.2fr;
            gap: 20px;
            margin-bottom: 24px;
        }
        @media (max-width: 900px) { .main-grid { grid-template-columns: 1fr; } }

        table { width: 100%; border-collapse: collapse; margin-top: 8px; }
        th, td { text-align: left; padding: 10px 12px; border-bottom: 1px solid var(--surface-border); font-size: 13px; }
        th { color: var(--text-muted); font-weight: 500; font-size: 12px; text-transform: uppercase; letter-spacing: 0.5px; }
        
        .decision-box {
            background: #182238;
            border: 1px solid #2a395c;
            border-radius: 8px;
            padding: 14px;
            margin-top: 12px;
        }
        .decision-title { font-weight: 700; font-size: 15px; margin-bottom: 6px; color: #93c5fd; }
        .decision-desc { font-size: 13px; color: #d1d5db; line-height: 1.5; }
        .footer { text-align: center; color: var(--text-muted); font-size: 12px; margin-top: 36px; padding-top: 16px; border-top: 1px solid var(--surface-border); }
    </style>
</head>
<body>
    <div class="header">
        <div>
            <h1><img src="/static/icon.svg" alt="" width="30" height="30" style="border-radius: 7px;"> Degen Trading Agent <span class="badge badge-live">● ONLINE</span></h1>
            <p style="color: var(--text-muted); font-size: 13px; margin-top: 4px;">Wallet spot su Base • Uniswap V3 • OpenRouter • SQLite</p>
        </div>
        <button class="btn" onclick="triggerRun()">⚡ Esegui Ciclo Ora</button>
    </div>

    <!-- STATS RAPIDE -->
    <div class="grid-stats">
        <div class="card">
            <h3>Valore Portafoglio</h3>
            <div class="value" id="balance">$0.00</div>
            <div class="subtext" id="balance-breakdown">Wallet su Base</div>
        </div>
        <div class="card">
            <h3>Token Detenuti</h3>
            <div class="value" id="open-positions-count">0</div>
            <div class="subtext" id="universe-count">Universo in attesa di screening</div>
        </div>
        <div class="card">
            <h3>Sentiment Fear & Greed</h3>
            <div class="value" id="sentiment-val">-- / 100</div>
            <div class="subtext" id="sentiment-class">Alternative.me Feed</div>
        </div>
        <div class="card">
            <h3>Ultima Operazione</h3>
            <div class="value" id="last-op">--</div>
            <div class="subtext" id="last-op-time">Nessuna operazione registrata</div>
        </div>
    </div>

    <!-- GRAFICI INTERATTIVI (MARKET LIVE & EQUITY) -->
    <div class="card" style="margin-bottom: 24px;">
        <div class="chart-header">
            <div class="tab-group">
                <button class="tab-btn active" id="tab-market-btn" onclick="switchTab('market')">📈 Grafico Mercato Live</button>
                <button class="tab-btn" id="tab-equity-btn" onclick="switchTab('equity')">💼 Storico Capitale & Saldo</button>
            </div>
            <div class="pill-group" id="symbol-pills">
                <span style="font-size: 12px; color: var(--text-muted);">Asset:</span>
                <button class="pill-btn active" onclick="changeTvSymbol('BINANCE:ETHUSDC', this)">ETH/USDC</button>
                <button class="pill-btn" onclick="changeTvSymbol('BINANCE:BTCUSDC', this)">BTC/USDC</button>
                <button class="pill-btn" onclick="changeTvSymbol('BINANCE:SOLUSDC', this)">SOL/USDC</button>
            </div>
        </div>

        <!-- 1. TradingView Live Market Chart -->
        <div id="market-chart-view" style="height: 480px; width: 100%; border-radius: 8px; overflow: hidden; background: #0c101c;">
            <div id="tradingview_widget" style="height: 100%; width: 100%;"></div>
        </div>

        <!-- 2. Chart.js Equity / Balance Curve -->
        <div id="equity-chart-view" style="display: none; height: 480px; width: 100%; position: relative; padding: 10px 10px 20px 10px;">
            <canvas id="balanceChart"></canvas>
        </div>
    </div>

    <!-- SEZIONE DATI DECISIONALI DEL BOT (SENTIMENT, PROPHET, NOTIZIE, INDICATORI) -->
    <div class="decision-section-title">
        🧠 Dati Decisionali dell'AI <span style="font-size: 13px; font-weight: normal; color: var(--text-muted);">(Fattori e modelli quantitativi analizzati dall'agente)</span>
    </div>

    <div class="intel-grid">
        <!-- 1. Fear & Greed Sentiment Card -->
        <div class="card">
            <div style="display: flex; justify-content: space-between; align-items: center; margin-bottom: 12px;">
                <h3>🎭 Sentiment Fear & Greed</h3>
                <span id="sentiment-pill-class" class="badge badge-bull">NEUTRAL</span>
            </div>
            <div style="display: flex; align-items: baseline; gap: 8px; margin-bottom: 8px;">
                <span style="font-size: 36px; font-weight: 800; letter-spacing: -1px;" id="fng-score">--</span>
                <span style="color: var(--text-muted); font-size: 14px;">/ 100</span>
                <span style="margin-left: auto; font-size: 13px; font-weight: 600;" id="fng-label">Caricamento...</span>
            </div>
            <!-- Progress Bar Gradient -->
            <div style="width: 100%; height: 10px; background: #090d16; border-radius: 5px; overflow: hidden; border: 1px solid var(--surface-border); margin-bottom: 8px;">
                <div id="fng-bar" style="height: 100%; width: 50%; background: linear-gradient(90deg, #ef4444 0%, #f59e0b 50%, #10b981 100%); transition: width 0.6s ease;"></div>
            </div>
            <div style="display: flex; justify-content: space-between; font-size: 10px; color: var(--text-muted);">
                <span>0 (Extreme Fear)</span>
                <span>50 (Neutral)</span>
                <span>100 (Extreme Greed)</span>
            </div>
            <p style="font-size: 11px; color: var(--text-muted); margin-top: 14px; line-height: 1.4;">
                Fonte: <em>Alternative.me API</em>. L'agente sfrutta il sentiment per modulare l'esposizione al rischio ed evitare trappole di ipercomprato/ipervenduto.
            </p>
        </div>

        <!-- 2. Previsioni Machine Learning Prophet -->
        <div class="card">
            <div style="display: flex; justify-content: space-between; align-items: center; margin-bottom: 12px;">
                <h3>🔮 Previsioni Prophet AI (15m & 1h)</h3>
                <span class="badge" style="background: rgba(99, 102, 241, 0.2); color: #818cf8;">Machine Learning</span>
            </div>
            <div id="forecasts-container">
                <p style="font-size: 12px; color: var(--text-muted); text-align: center; padding: 20px 0;">In attesa dei dati previsionali del ciclo...</p>
            </div>
            <p style="font-size: 11px; color: var(--text-muted); margin-top: 10px;">
                Serie storiche analizzate dal modello Facebook Prophet per stimare variazione % e intervalli di confidenza.
            </p>
        </div>

        <!-- 3. Rassegna Stampa & News Feed -->
        <div class="card">
            <div style="display: flex; justify-content: space-between; align-items: center; margin-bottom: 12px;">
                <h3>📰 Notizie e Rassegna Crypto</h3>
                <span class="badge" style="background: rgba(59, 130, 246, 0.2); color: #60a5fa;">CoinJournal RSS</span>
            </div>
            <div id="news-container" style="max-height: 180px; overflow-y: auto; padding-right: 4px;">
                <p style="font-size: 12px; color: var(--text-muted); text-align: center; padding: 20px 0;">In attesa delle notizie...</p>
            </div>
        </div>
    </div>

    <!-- 4. Tabella Dettagliata Indicatori Tecnici -->
    <div class="card" style="margin-bottom: 24px;">
        <div style="display: flex; justify-content: space-between; align-items: center; margin-bottom: 8px;">
            <h3>📊 Indicatori Tecnici Multi-Asset (BTC, ETH, SOL)</h3>
            <span style="font-size: 11px; color: var(--text-muted);">Calcolati su timeframe 15m e 1h</span>
        </div>
        <table>
            <thead>
                <tr>
                    <th>Ticker</th>
                    <th>Prezzo Attuale</th>
                    <th>RSI (7)</th>
                    <th>MACD</th>
                    <th>EMA (20)</th>
                    <th>Pivot Point</th>
                    <th>Supporto (S1)</th>
                    <th>Resistenza (R1)</th>
                    <th>Bias Tecnico</th>
                </tr>
            </thead>
            <tbody id="indicators-table">
                <tr><td colspan="9" style="text-align: center; color: var(--text-muted); padding: 20px;">In attesa del calcolo indicatori del ciclo...</td></tr>
            </tbody>
        </table>
    </div>

    <!-- POSIZIONI APERTE E DECISIONE AI -->
    <div class="main-grid">
        <div class="card">
            <div style="display: flex; justify-content: space-between; align-items: center;">
                <h2>🪙 Token in Portafoglio</h2>
                <span id="pos-updated" style="font-size: 12px; color: var(--text-muted);">In attesa dati...</span>
            </div>
            <table>
                <thead>
                    <tr>
                        <th>Simbolo</th>
                        <th>Quantità</th>
                        <th>Prezzo</th>
                        <th>Valore</th>
                        <th>Carico</th>
                        <th>P/L</th>
                        <th>SL / TP</th>
                    </tr>
                </thead>
                <tbody id="positions-table">
                    <tr><td colspan="7" style="text-align: center; color: var(--text-muted); padding: 20px;">Nessun token in portafoglio.</td></tr>
                </tbody>
            </table>
        </div>

        <div class="card">
            <h2>🧠 Ultima Decisione AI</h2>
            <div class="decision-box">
                <div class="decision-title" id="ai-action">In attesa del primo ciclo...</div>
                <div class="decision-desc" id="ai-reason">L'agente elaborerà i dati di mercato al prossimo intervallo programmato o premendo 'Esegui Ciclo Ora'.</div>
            </div>
            <div style="margin-top: 14px; font-size: 12px; color: var(--text-muted); line-height: 1.6;">
                <p><strong>Modello LLM:</strong> OpenRouter (openrouter/free)</p>
                <p><strong>Dati analizzati:</strong> Indicators + News + Sentiment + Prophet</p>
            </div>
        </div>
    </div>

    <!-- SCREENING TOKEN -->
    <div class="card" style="margin-bottom: 24px;">
        <div style="display: flex; justify-content: space-between; align-items: center;">
            <h2>🛡️ Screening Anti-Scam (CoinGecko + liquidità + GoPlus)</h2>
            <span id="screening-summary" style="font-size: 12px; color: var(--text-muted);">In attesa del primo screening...</span>
        </div>
        <table>
            <thead>
                <tr>
                    <th>Esito</th>
                    <th>Token</th>
                    <th>Prezzo</th>
                    <th>Liquidità</th>
                    <th>Volume 24h</th>
                    <th>24h %</th>
                    <th>Età pool</th>
                    <th>Motivo dello scarto</th>
                </tr>
            </thead>
            <tbody id="screening-table">
                <tr><td colspan="8" style="text-align: center; color: var(--text-muted); padding: 20px;">Nessuno screening registrato.</td></tr>
            </tbody>
        </table>
    </div>

    <!-- SWAP ESEGUITI -->
    <div class="card" style="margin-bottom: 24px;">
        <h2>🔀 Swap su Uniswap V3</h2>
        <table>
            <thead>
                <tr>
                    <th>Data (UTC)</th>
                    <th>Operazione</th>
                    <th>Token</th>
                    <th>Importo</th>
                    <th>Rotta</th>
                    <th>Slippage</th>
                    <th>Stato</th>
                </tr>
            </thead>
            <tbody id="swaps-table">
                <tr><td colspan="7" style="text-align: center; color: var(--text-muted); padding: 20px;">Nessuno swap registrato.</td></tr>
            </tbody>
        </table>
    </div>

    <!-- STORICO OPERAZIONI (SQLITE) -->
    <div class="card">
        <h2>📜 Storico Ultime Operazioni (SQLite)</h2>
        <table>
            <thead>
                <tr>
                    <th>Data (UTC)</th>
                    <th>Operazione</th>
                    <th>Token</th>
                    <th>Allocazione</th>
                    <th>Slippage</th>
                    <th>Dettagli / Payload</th>
                </tr>
            </thead>
            <tbody id="operations-table">
                <tr><td colspan="6" style="text-align: center; color: var(--text-muted); padding: 20px;">Caricamento storico...</td></tr>
            </tbody>
        </table>
    </div>

    <div class="footer">
        Degen Trading Agent • Base spot (Uniswap V3) & OpenRouter • CapRover & Docker Edition
    </div>

    <script>
        // Le meme costano 0.00000012: senza decimali adattivi diventano "$0.00"
        function fmtPrice(v) {
            if (v === null || v === undefined || v === 0) return 'n/d';
            const abs = Math.abs(v);
            if (abs >= 1000) return '$' + v.toLocaleString(undefined, {maximumFractionDigits: 2});
            if (abs >= 1) return '$' + v.toFixed(4);
            if (abs >= 0.0001) return '$' + v.toFixed(6);
            return '$' + v.toExponential(3);
        }

        function fmtAmount(v) {
            if (v === null || v === undefined) return '--';
            const abs = Math.abs(v);
            if (abs >= 1000) return v.toLocaleString(undefined, {maximumFractionDigits: 0});
            if (abs >= 1) return v.toFixed(3);
            return v.toPrecision(4);
        }

        let currentTvSymbol = 'BINANCE:ETHUSDC';
        let balanceChartInstance = null;
        let lastHistoryData = [];
        let currentBalanceVal = 0;

        // TradingView Initialization
        function initTradingView(symbol) {
            if (typeof TradingView === 'undefined') {
                console.warn('TradingView library not loaded yet');
                return;
            }
            document.getElementById('tradingview_widget').innerHTML = '';
            new TradingView.widget({
                "autosize": true,
                "symbol": symbol,
                "interval": "15",
                "timezone": "Etc/UTC",
                "theme": "dark",
                "style": "1",
                "locale": "it",
                "toolbar_bg": "#131b2e",
                "enable_publishing": false,
                "hide_top_toolbar": false,
                "hide_legend": false,
                "save_image": false,
                "container_id": "tradingview_widget"
            });
        }

        function changeTvSymbol(symbol, btnElement) {
            currentTvSymbol = symbol;
            document.querySelectorAll('#symbol-pills .pill-btn').forEach(b => b.classList.remove('active'));
            if (btnElement) btnElement.classList.add('active');
            initTradingView(symbol);
        }

        function switchTab(tab) {
            const marketView = document.getElementById('market-chart-view');
            const equityView = document.getElementById('equity-chart-view');
            const pills = document.getElementById('symbol-pills');
            const tabMarketBtn = document.getElementById('tab-market-btn');
            const tabEquityBtn = document.getElementById('tab-equity-btn');

            if (tab === 'market') {
                marketView.style.display = 'block';
                equityView.style.display = 'none';
                pills.style.display = 'flex';
                tabMarketBtn.classList.add('active');
                tabEquityBtn.classList.remove('active');
                initTradingView(currentTvSymbol);
            } else {
                marketView.style.display = 'none';
                equityView.style.display = 'block';
                pills.style.display = 'none';
                tabMarketBtn.classList.remove('active');
                tabEquityBtn.classList.add('active');
                renderBalanceChart(lastHistoryData, currentBalanceVal);
            }
        }

        function renderBalanceChart(history, currentBal) {
            const canvas = document.getElementById('balanceChart');
            if (!canvas) return;
            const ctx = canvas.getContext('2d');

            let labels = [];
            let values = [];

            if (history && history.length > 0) {
                labels = history.map(h => {
                    const d = new Date(h.time);
                    return isNaN(d.getTime()) ? h.time : (d.toLocaleDateString([], { month: 'short', day: 'numeric' }) + ' ' + d.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' }));
                });
                values = history.map(h => h.balance);
            } else {
                labels = ['Avvio', 'Adesso'];
                values = [currentBal, currentBal];
            }

            const gradient = ctx.createLinearGradient(0, 0, 0, 400);
            gradient.addColorStop(0, 'rgba(59, 130, 246, 0.4)');
            gradient.addColorStop(1, 'rgba(59, 130, 246, 0.0)');

            if (balanceChartInstance) {
                balanceChartInstance.data.labels = labels;
                balanceChartInstance.data.datasets[0].data = values;
                balanceChartInstance.update();
                balanceChartInstance.resize();
                return;
            }

            balanceChartInstance = new Chart(ctx, {
                type: 'line',
                data: {
                    labels: labels,
                    datasets: [{
                        label: 'Saldo Conto ($ USDC)',
                        data: values,
                        borderColor: '#3b82f6',
                        borderWidth: 3,
                        backgroundColor: gradient,
                        fill: true,
                        tension: 0.3,
                        pointBackgroundColor: '#60a5fa',
                        pointBorderColor: '#1e2942',
                        pointBorderWidth: 2,
                        pointRadius: 4,
                        pointHoverRadius: 7
                    }]
                },
                options: {
                    responsive: true,
                    maintainAspectRatio: false,
                    interaction: { mode: 'index', intersect: false },
                    plugins: {
                        legend: { display: true, labels: { color: '#9ca3af', font: { family: 'Inter', size: 12 } } },
                        tooltip: {
                            backgroundColor: '#131b2e',
                            titleColor: '#93c5fd',
                            bodyColor: '#f3f4f6',
                            borderColor: '#3b82f6',
                            borderWidth: 1,
                            padding: 12,
                            callbacks: {
                                label: function(context) { return ' Saldo: $' + context.parsed.y.toFixed(2); }
                            }
                        }
                    },
                    scales: {
                        x: {
                            grid: { color: 'rgba(30, 41, 66, 0.6)' },
                            ticks: { color: '#9ca3af', font: { family: 'Inter', size: 11 } }
                        },
                        y: {
                            grid: { color: 'rgba(30, 41, 66, 0.6)' },
                            ticks: {
                                color: '#9ca3af',
                                font: { family: 'Inter', size: 11 },
                                callback: function(val) { return '$' + val; }
                            }
                        }
                    }
                }
            });
        }

        async function fetchStatus() {
            try {
                const res = await fetch('/api/status');
                const data = await res.json();
                
                currentBalanceVal = data.balance || 0;
                lastHistoryData = data.balance_history || [];

                document.getElementById('balance').textContent = '$' + currentBalanceVal.toFixed(2);
                const usdc = data.usdc_balance || 0;
                const eth = data.eth_balance || 0;
                document.getElementById('balance-breakdown').textContent =
                    `USDC $${usdc.toFixed(2)} • ETH ${eth.toFixed(4)}` + (data.dry_run ? ' • DRY-RUN' : '');
                document.getElementById('open-positions-count').textContent = data.positions ? data.positions.length : 0;
                
                // 1. Sentiment Fear & Greed Rendering
                if (data.sentiment && data.sentiment.value !== null && data.sentiment.value !== undefined) {
                    const val = data.sentiment.value;
                    const classification = data.sentiment.classification || 'Neutral';
                    
                    document.getElementById('sentiment-val').textContent = val + ' / 100';
                    document.getElementById('sentiment-class').textContent = classification;
                    
                    document.getElementById('fng-score').textContent = val;
                    document.getElementById('fng-label').textContent = classification.toUpperCase();
                    document.getElementById('fng-bar').style.width = Math.min(Math.max(val, 5), 100) + '%';
                    
                    const badge = document.getElementById('sentiment-pill-class');
                    badge.textContent = classification.toUpperCase();
                    if (val >= 60) {
                        badge.className = 'badge badge-bull';
                    } else if (val <= 40) {
                        badge.className = 'badge badge-bear';
                    } else {
                        badge.className = 'badge badge-neutral';
                    }
                }

                // 2. Previsioni Prophet Rendering
                const fcContainer = document.getElementById('forecasts-container');
                if (data.forecasts && data.forecasts.length > 0) {
                    fcContainer.innerHTML = data.forecasts.map(f => {
                        // Una previsione fallita arriva con valori null: va mostrata
                        // come "N/D", non come $0 (che sembrerebbe un prezzo reale).
                        const hasPrediction = f.prediction !== null && f.prediction !== undefined;
                        const money = v => (v === null || v === undefined) ? 'N/D' : fmtPrice(v);
                        const delta = hasPrediction ? (f.change_pct || 0) : null;
                        const isPos = delta !== null && delta >= 0;
                        const deltaClass = delta === null
                            ? ''
                            : (isPos ? 'forecast-delta-pos' : 'forecast-delta-neg');
                        const deltaTxt = delta === null
                            ? 'N/D'
                            : `${isPos ? '+' : ''}${delta.toFixed(2)}%`;
                        const boundsTxt = hasPrediction
                            ? `[${fmtPrice(f.lower_bound)} - ${fmtPrice(f.upper_bound)}]`
                            : 'previsione non disponibile';
                        return `
                            <div class="forecast-item">
                                <div>
                                    <strong style="font-size: 13px; color: #93c5fd;">${f.ticker}</strong>
                                    <span style="font-size: 11px; color: var(--text-muted); margin-left: 4px;">(${f.timeframe})</span>
                                    <div style="font-size: 11px; color: #cbd5e1; margin-top: 2px;">
                                        ${money(f.last_price)} ➔ <strong>${money(f.prediction)}</strong>
                                    </div>
                                </div>
                                <div style="text-align: right;">
                                    <div class="${deltaClass}" style="${delta === null ? 'color: var(--text-muted); font-size: 13px; font-weight: 700;' : ''}">
                                        ${deltaTxt}
                                    </div>
                                    <div style="font-size: 10px; color: var(--text-muted); margin-top: 2px;">
                                        ${boundsTxt}
                                    </div>
                                </div>
                            </div>
                        `;
                    }).join('');
                } else {
                    fcContainer.innerHTML = '<p style="font-size: 12px; color: var(--text-muted); text-align: center; padding: 20px 0;">In attesa delle previsioni Prophet del prossimo ciclo...</p>';
                }

                // 3. News Feed Rendering
                const newsContainer = document.getElementById('news-container');
                if (data.news && data.news.length > 0) {
                    newsContainer.innerHTML = data.news.map(n => {
                        return `
                            <div class="news-item">
                                📰 ${n}
                            </div>
                        `;
                    }).join('');
                } else {
                    newsContainer.innerHTML = '<p style="font-size: 12px; color: var(--text-muted); text-align: center; padding: 20px 0;">Nessuna notizia recente caricata.</p>';
                }

                // 4. Indicatori Tecnici Table Rendering
                const indTable = document.getElementById('indicators-table');
                if (data.indicators && data.indicators.length > 0) {
                    indTable.innerHTML = data.indicators.map(ind => {
                        const rsi = ind.rsi_7 !== null ? ind.rsi_7 : 50;
                        let rsiBadge = '<span class="badge badge-neutral">' + rsi.toFixed(1) + '</span>';
                        if (rsi > 70) {
                            rsiBadge = '<span class="badge badge-bear">' + rsi.toFixed(1) + ' (Ipercomprato)</span>';
                        } else if (rsi < 30) {
                            rsiBadge = '<span class="badge badge-bull">' + rsi.toFixed(1) + ' (Ipervenduto)</span>';
                        }

                        const price = ind.price || 0;
                        const ema20 = ind.ema20 || 0;
                        let bias = '<span class="badge badge-neutral">Range</span>';
                        if (price > ema20 && rsi > 50) {
                            bias = '<span class="badge badge-bull">🟢 Bullish Momentum</span>';
                        } else if (price < ema20 && rsi < 50) {
                            bias = '<span class="badge badge-bear">🔴 Bearish Trend</span>';
                        }

                        return `
                            <tr>
                                <td><strong style="color: #60a5fa; font-size: 14px;">${ind.ticker}</strong></td>
                                <td style="font-weight: 600; font-family: 'JetBrains Mono', monospace;">${fmtPrice(price)}</td>
                                <td>${rsiBadge}</td>
                                <td style="font-family: 'JetBrains Mono', monospace; font-size: 12px; color: ${ind.macd >= 0 ? 'var(--success)' : 'var(--danger)'};">${(ind.macd || 0).toPrecision(3)}</td>
                                <td style="font-family: 'JetBrains Mono', monospace; font-size: 12px;">${fmtPrice(ema20)}</td>
                                <td style="font-family: 'JetBrains Mono', monospace; font-size: 12px; color: var(--text-muted);">${fmtPrice(ind.pp)}</td>
                                <td style="font-family: 'JetBrains Mono', monospace; font-size: 12px; color: var(--success);">${fmtPrice(ind.s1)}</td>
                                <td style="font-family: 'JetBrains Mono', monospace; font-size: 12px; color: var(--danger);">${fmtPrice(ind.r1)}</td>
                                <td>${bias}</td>
                            </tr>
                        `;
                    }).join('');
                } else {
                    indTable.innerHTML = '<tr><td colspan="9" style="text-align: center; color: var(--text-muted); padding: 20px;">In attesa del calcolo indicatori del primo ciclo...</td></tr>';
                }

                // 5. Posizioni Rendering
                const posTable = document.getElementById('positions-table');
                if (data.positions && data.positions.length > 0) {
                    posTable.innerHTML = data.positions.map(p => {
                        const pnl = p.pnl_percent;
                        const pnlColor = (pnl === null || pnl === undefined)
                            ? 'var(--text-muted)'
                            : (pnl >= 0 ? 'var(--success)' : 'var(--danger)');
                        const pnlTxt = (pnl === null || pnl === undefined)
                            ? 'n/d'
                            : `${pnl >= 0 ? '+' : ''}${pnl.toFixed(1)}%`;
                        return `
                        <tr>
                            <td><strong>${p.symbol}</strong></td>
                            <td>${fmtAmount(p.amount)}</td>
                            <td>${fmtPrice(p.price_usd)}</td>
                            <td>$${(p.value_usd || 0).toFixed(2)}</td>
                            <td>${fmtPrice(p.entry_price)}</td>
                            <td style="color: ${pnlColor}; font-weight: 600;">${pnlTxt}</td>
                            <td style="font-size: 11px; color: var(--text-muted);">
                                -${p.stop_loss_percent || '--'}% / +${p.take_profit_percent || '--'}%
                            </td>
                        </tr>`;
                    }).join('');
                } else {
                    posTable.innerHTML = '<tr><td colspan="7" style="text-align: center; color: var(--text-muted); padding: 20px;">Nessun token in portafoglio.</td></tr>';
                }

                // 5b. Screening Rendering
                const screenTable = document.getElementById('screening-table');
                if (data.screening && data.screening.length > 0) {
                    const approved = data.screening.filter(s => s.approved).length;
                    document.getElementById('screening-summary').textContent =
                        `${approved} approvati su ${data.screening.length} valutati`;
                    document.getElementById('universe-count').textContent =
                        `${approved} token nell'universo tradabile`;
                    screenTable.innerHTML = data.screening.map(s => `
                        <tr>
                            <td><span class="badge ${s.approved ? 'badge-long' : 'badge-short'}">${s.approved ? 'OK' : 'SCARTATO'}</span></td>
                            <td><strong>${s.symbol || '--'}</strong></td>
                            <td>${fmtPrice(s.price_usd)}</td>
                            <td>$${Math.round(s.liquidity_usd || 0).toLocaleString()}</td>
                            <td>$${Math.round(s.volume_24h_usd || 0).toLocaleString()}</td>
                            <td style="color: ${(s.price_change_24h || 0) >= 0 ? 'var(--success)' : 'var(--danger)'};">
                                ${(s.price_change_24h || 0).toFixed(1)}%
                            </td>
                            <td>${s.pool_age_days === null || s.pool_age_days === undefined ? 'n/d' : s.pool_age_days.toFixed(0) + 'g'}</td>
                            <td style="font-size: 11px; color: var(--text-muted);">${s.reasons || '—'}</td>
                        </tr>
                    `).join('');
                } else {
                    screenTable.innerHTML = '<tr><td colspan="8" style="text-align: center; color: var(--text-muted); padding: 20px;">Nessuno screening registrato.</td></tr>';
                }

                // 5c. Swap Rendering
                const swapsTable = document.getElementById('swaps-table');
                if (data.swaps && data.swaps.length > 0) {
                    swapsTable.innerHTML = data.swaps.map(s => {
                        const statusClass = s.status === 'success' ? 'badge-long'
                            : (s.status === 'dry_run' ? 'badge-neutral' : 'badge-short');
                        const link = s.explorer_url
                            ? `<a href="${s.explorer_url}" target="_blank" style="color: var(--primary);">${s.status}</a>`
                            : `<span class="badge ${statusClass}">${s.status || '--'}</span>`;
                        return `
                        <tr>
                            <td>${s.created_at}</td>
                            <td>${(s.operation || '').toUpperCase()}${s.trigger ? ' (' + s.trigger + ')' : ''}</td>
                            <td><strong>${s.symbol || '--'}</strong></td>
                            <td>${s.usd_amount ? '$' + s.usd_amount.toFixed(2) : '--'}</td>
                            <td style="font-size: 11px; font-family: monospace;">${s.route || '--'}</td>
                            <td>${s.slippage_bps ? s.slippage_bps + ' bps' : '--'}</td>
                            <td>${link}</td>
                        </tr>`;
                    }).join('');
                } else {
                    swapsTable.innerHTML = '<tr><td colspan="7" style="text-align: center; color: var(--text-muted); padding: 20px;">Nessuno swap registrato.</td></tr>';
                }

                // 6. Ultima Operazione & AI Reason
                if (data.operations && data.operations.length > 0) {
                    const last = data.operations[0];
                    document.getElementById('last-op').textContent = (last.operation || '--').toUpperCase() + ' ' + (last.symbol || '');
                    document.getElementById('last-op-time').textContent = last.created_at;

                    document.getElementById('ai-action').textContent = `${(last.operation || '').toUpperCase()} ${last.symbol || ''}`;
                    
                    try {
                        const payload = JSON.parse(last.raw_payload);
                        document.getElementById('ai-reason').textContent = payload.reason || 'Nessuna spiegazione salvata.';
                    } catch(e) {
                        document.getElementById('ai-reason').textContent = 'Operazione registrata: ' + last.operation;
                    }

                    // Tabella Operazioni
                    const opsTable = document.getElementById('operations-table');
                    opsTable.innerHTML = data.operations.map(op => `
                        <tr>
                            <td>${op.created_at}</td>
                            <td><span class="badge" style="background:#1e2942;">${(op.operation || '').toUpperCase()}</span></td>
                            <td><strong>${op.symbol || '--'}</strong></td>
                            <td>${(op.target_portion_of_portfolio ? (op.target_portion_of_portfolio * 100).toFixed(1) + '%' : (op.sell_portion ? (op.sell_portion * 100).toFixed(0) + '% venduto' : '--'))}</td>
                            <td>${op.slippage_bps ? op.slippage_bps + ' bps' : '--'}</td>
                            <td style="font-family: monospace; font-size: 11px; color: var(--text-muted);">${op.raw_payload ? op.raw_payload.substring(0, 80) + '...' : '--'}</td>
                        </tr>
                    `).join('');
                } else {
                    document.getElementById('operations-table').innerHTML = '<tr><td colspan="6" style="text-align: center; color: var(--text-muted); padding: 20px;">Nessuna operazione registrata.</td></tr>';
                }

                // Aggiorna grafico bilancio se visibile
                if (document.getElementById('equity-chart-view').style.display !== 'none') {
                    renderBalanceChart(lastHistoryData, currentBalanceVal);
                }

            } catch (err) {
                console.error('Errore aggiornamento dashboard:', err);
            }
        }

        async function triggerRun() {
            if (!confirm('Vuoi forzare un ciclo di trading adesso?')) return;
            try {
                const res = await fetch('/api/run', { method: 'POST' });
                const json = await res.json();
                alert(json.message || 'Ciclo avviato!');
                setTimeout(fetchStatus, 3000);
            } catch(e) {
                alert('Errore: ' + e);
            }
        }

        // Init page
        window.addEventListener('DOMContentLoaded', () => {
            initTradingView(currentTvSymbol);
            fetchStatus();
            setInterval(fetchStatus, 10000);
        });
    </script>
</body>
</html>
"""

def get_db_data():
    global _LIVE_CACHE
    now = time.time()

    data = {
        "balance": 0.0,
        "positions": [],
        "sentiment": {"value": 50, "classification": "Neutral"},
        "indicators": [],
        "forecasts": [],
        "screening": [],
        "swaps": [],
        "news": [],
        "operations": [],
        "balance_history": [],
        "errors": []
    }

    if not os.path.exists(SQLITE_DB_PATH):
        return data

    try:
        conn = sqlite3.connect(SQLITE_DB_PATH)
        conn.row_factory = sqlite3.Row
        cur = conn.cursor()

        # 1. Ultimo snapshot account
        cur.execute("SELECT * FROM account_snapshots ORDER BY id DESC LIMIT 1;")
        snap = cur.fetchone()
        if snap:
            data["balance"] = float(snap["balance_usd"])
            data["usdc_balance"] = snap["usdc_balance"]
            data["eth_balance"] = snap["eth_balance"]
            data["tokens_value_usd"] = snap["tokens_value_usd"]
            data["dry_run"] = bool(snap["dry_run"])
            snapshot_id = snap["id"]

            # Posizioni associate
            cur.execute("SELECT * FROM open_positions WHERE snapshot_id = ?;", (snapshot_id,))
            positions = []
            for row in cur.fetchall():
                positions.append({
                    "symbol": row["symbol"],
                    "address": row["address"],
                    "amount": row["amount"],
                    "price_usd": row["price_usd"],
                    "value_usd": row["value_usd"],
                    "entry_price": row["entry_price"],
                    "pnl_percent": row["pnl_percent"],
                    "stop_loss_percent": row["stop_loss_percent"],
                    "take_profit_percent": row["take_profit_percent"],
                })
            data["positions"] = positions

        # 2. Storico saldo (ultimi 50 snapshot)
        cur.execute("SELECT created_at, balance_usd FROM account_snapshots ORDER BY id DESC LIMIT 50;")
        rows = cur.fetchall()
        data["balance_history"] = [
            {"time": r["created_at"], "balance": float(r["balance_usd"])}
            for r in reversed(rows)
        ]

        # 3. Sentiment (da SQLite o fallback live cache)
        cur.execute("SELECT value, classification, sentiment_timestamp FROM sentiment_contexts ORDER BY id DESC LIMIT 1;")
        sent = cur.fetchone()
        if sent and sent["value"] is not None:
            data["sentiment"] = {
                "value": sent["value"],
                "classification": sent["classification"],
                "timestamp": sent["sentiment_timestamp"]
            }
        else:
            # Fallback a live fetch se non presente nel DB
            if _LIVE_CACHE["sentiment"] and (now - _LIVE_CACHE["sentiment_time"] < 60):
                data["sentiment"] = _LIVE_CACHE["sentiment"]
            else:
                try:
                    from sentiment import get_latest_fear_and_greed
                    live_sent = get_latest_fear_and_greed()
                    if live_sent:
                        sent_obj = {
                            "value": live_sent.get("valore", 50),
                            "classification": live_sent.get("classificazione", "Neutral"),
                            "timestamp": live_sent.get("timestamp")
                        }
                        _LIVE_CACHE["sentiment"] = sent_obj
                        _LIVE_CACHE["sentiment_time"] = now
                        data["sentiment"] = sent_obj
                except Exception:
                    pass

        # 4. Indicatori tecnici (ultimi per ciascun ticker: BTC, ETH, SOL)
        cur.execute("""
            SELECT ticker, ts, price, ema20, macd, rsi_7, pp, s1, s2, r1, r2, liquidity_usd
            FROM indicators_contexts
            WHERE id IN (SELECT MAX(id) FROM indicators_contexts GROUP BY ticker)
            ORDER BY ticker ASC;
        """)
        indicators = []
        for r in cur.fetchall():
            indicators.append({
                "ticker": r["ticker"],
                "ts": r["ts"],
                "price": r["price"],
                "ema20": r["ema20"],
                "macd": r["macd"],
                "rsi_7": r["rsi_7"],
                "pp": r["pp"],
                "s1": r["s1"],
                "s2": r["s2"],
                "r1": r["r1"],
                "r2": r["r2"],
                "liquidity_usd": r["liquidity_usd"]
            })
        data["indicators"] = indicators

        # 5. Previsioni Prophet AI (ultime per ciascun ticker e timeframe)
        cur.execute("""
            SELECT ticker, timeframe, last_price, prediction, lower_bound, upper_bound, change_pct, forecast_timestamp
            FROM forecasts_contexts
            WHERE id IN (SELECT MAX(id) FROM forecasts_contexts GROUP BY ticker, timeframe)
            ORDER BY ticker ASC, timeframe ASC;
        """)
        forecasts = []
        for r in cur.fetchall():
            forecasts.append({
                "ticker": r["ticker"],
                "timeframe": r["timeframe"],
                "last_price": r["last_price"],
                "prediction": r["prediction"],
                "lower_bound": r["lower_bound"],
                "upper_bound": r["upper_bound"],
                "change_pct": r["change_pct"],
                "forecast_timestamp": r["forecast_timestamp"]
            })
        data["forecasts"] = forecasts

        # 6. Notizie di mercato (da SQLite o fallback cache da CoinJournal)
        cur.execute("SELECT news_text FROM news_contexts ORDER BY id DESC LIMIT 1;")
        news_row = cur.fetchone()
        if news_row and news_row["news_text"]:
            data["news"] = [n.strip() for n in news_row["news_text"].split("\n\n") if n.strip()][:5]
        else:
            if _LIVE_CACHE["news"] and (now - _LIVE_CACHE["news_time"] < 120):
                data["news"] = _LIVE_CACHE["news"]
            else:
                try:
                    from news_feed import fetch_latest_news
                    live_news_txt = fetch_latest_news(max_chars=2500)
                    if live_news_txt:
                        news_items = [n.strip() for n in live_news_txt.split("\n\n") if n.strip()][:5]
                        _LIVE_CACHE["news"] = news_items
                        _LIVE_CACHE["news_time"] = now
                        data["news"] = news_items
                except Exception:
                    pass

        # 6b. Screening dell'ultimo ciclo
        cur.execute("""
            SELECT symbol, address, approved, liquidity_usd, volume_24h_usd,
                   price_usd, pool_age_days, price_change_24h, reasons, created_at
            FROM screening_results
            WHERE created_at = (SELECT MAX(created_at) FROM screening_results)
            ORDER BY approved DESC, volume_24h_usd DESC
            LIMIT 40;
        """)
        data["screening"] = [
            {
                "symbol": r["symbol"],
                "address": r["address"],
                "approved": bool(r["approved"]),
                "liquidity_usd": r["liquidity_usd"],
                "volume_24h_usd": r["volume_24h_usd"],
                "price_usd": r["price_usd"],
                "pool_age_days": r["pool_age_days"],
                "price_change_24h": r["price_change_24h"],
                "reasons": r["reasons"],
            }
            for r in cur.fetchall()
        ]

        # 6c. Ultimi swap eseguiti
        cur.execute("""
            SELECT created_at, operation, symbol, status, usd_amount, tokens,
                   route, slippage_bps, explorer_url, trigger
            FROM swaps ORDER BY id DESC LIMIT 15;
        """)
        data["swaps"] = [
            {
                "created_at": r["created_at"],
                "operation": r["operation"],
                "symbol": r["symbol"],
                "status": r["status"],
                "usd_amount": r["usd_amount"],
                "tokens": r["tokens"],
                "route": r["route"],
                "slippage_bps": r["slippage_bps"],
                "explorer_url": r["explorer_url"],
                "trigger": r["trigger"],
            }
            for r in cur.fetchall()
        ]

        # 7. Ultime 15 operazioni
        cur.execute("SELECT * FROM bot_operations ORDER BY id DESC LIMIT 15;")
        ops = []
        for row in cur.fetchall():
            ops.append({
                "id": row["id"],
                "created_at": row["created_at"],
                "operation": row["operation"],
                "symbol": row["symbol"],
                "token_address": row["token_address"],
                "target_portion_of_portfolio": row["target_portion_of_portfolio"],
                "sell_portion": row["sell_portion"],
                "slippage_bps": row["slippage_bps"],
                "raw_payload": row["raw_payload"]
            })
        data["operations"] = ops

        conn.close()
    except Exception as e:
        data["error"] = str(e)

    return data


class DashboardHandler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        pass  # Silenzia i log standard di accesso per pulizia console

    def do_GET(self):
        parsed = urlparse(self.path)
        if parsed.path == "/" or parsed.path == "/index.html":
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(HTML_TEMPLATE.encode("utf-8"))
        elif parsed.path == "/api/status":
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            db_data = get_db_data()
            self.wfile.write(json.dumps(db_data).encode("utf-8"))
        elif parsed.path in STATIC_ROUTES:
            filename, content_type = STATIC_ROUTES[parsed.path]
            try:
                with open(os.path.join(STATIC_DIR, filename), "rb") as fh:
                    payload = fh.read()
            except OSError:
                self.send_response(404)
                self.end_headers()
                return
            self.send_response(200)
            self.send_header("Content-Type", content_type)
            self.send_header("Cache-Control", "public, max-age=86400")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
        elif parsed.path == "/health":
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps({"status": "healthy", "service": "dashboard"}).encode("utf-8"))
        else:
            self.send_response(404)
            self.end_headers()

    def do_POST(self):
        parsed = urlparse(self.path)
        if parsed.path == "/api/run":
            # Avvia main.py in background thread
            def run_main():
                subprocess.run(["python", "main.py"], check=False)

            threading.Thread(target=run_main, daemon=True).start()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps({"status": "started", "message": "Ciclo di trading avviato in background!"}).encode("utf-8"))
        else:
            self.send_response(404)
            self.end_headers()


def run_dashboard(port=PORT):
    server = HTTPServer(("0.0.0.0", port), DashboardHandler)
    print(f"🌐 Dashboard Web attiva su http://0.0.0.0:{port}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    run_dashboard()
