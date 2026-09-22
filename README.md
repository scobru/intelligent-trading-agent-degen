<img src="static/icon.svg" alt="" width="88" height="88" align="left">

# Degen Trading Agent (Base spot)

<br clear="left">

Agente di trading speculativo che opera **direttamente dal wallet su Base**:
niente protocolli di terze parti, niente leva, niente perpetual. Compra e
vende token ERC-20 (meme incluse) via **Uniswap V3**, con uno screening
anti-scam davanti a ogni acquisto.

È la variante spot di
[intelligent-trading-agent](https://github.com/scobru/intelligent-trading-agent),
che invece opera sui perpetual di SynFutures V3. Struttura, dashboard,
notifiche Telegram e ciclo decisionale sono gli stessi: cambia il layer di
esecuzione e si aggiunge la selezione dei token.

---

## 🧠 Come funziona un ciclo

1. **Screening** — l'universo tradabile viene ricostruito a ogni ciclo:
   - scoperta dei token più scambiati su Base (GeckoTerminal);
   - **CoinGecko**: se il token non è nella token list ufficiale di Base, è fuori;
   - **liquidità, volume 24h, età della pool** (DexScreener) sopra soglia;
   - **GoPlus Security**: honeypot, buy/sell tax, mintable, owner nascosto,
     trasferimenti sospendibili, blacklist, selfdestruct.

   Ogni scarto conserva il motivo e finisce in dashboard: si vede *perché* un
   token non è stato considerato.

2. **Portafoglio** — saldi letti on-chain (`balanceOf`), valorizzati quotando
   la rotta reale su Uniswap. Prezzo di carico, stop loss e take profit vivono
   in `positions.json`, perché on-chain non esistono.

3. **Uscite automatiche** — stop loss e take profit vengono eseguiti *prima* di
   sentire il modello: non si delega a un LLM la gestione del rischio.

4. **Analisi** — indicatori tecnici e previsioni Prophet sugli asset del ciclo.
   Le candele arrivano dai CEX per i major e dalla pool più liquida su Base per
   le meme.

5. **Decisione** — l'LLM (via OpenRouter) riceve universo, indicatori, news,
   sentiment e forecast, e risponde con un JSON `buy` / `sell` / `hold`.

6. **Esecuzione** — quoting su QuoterV2 (rotta diretta su tutte le fee tier,
   più due salti via WETH), swap su SwapRouter02, il tutto dentro i limiti di
   rischio.

---

## 🛡️ Barriere prima di ogni transazione

| Limite | Default | Variabile |
|---|---|---|
| Nessuna transazione firmata | **attivo** | `DRY_RUN=true` |
| Quota massima su un token | 25% del portafoglio | `MAX_POSITION_PCT` |
| Token detenuti contemporaneamente | 5 | `MAX_OPEN_TOKENS` |
| Importo per trade | $5 – $250 | `MIN_TRADE_USD`, `MAX_TRADE_USD` |
| Slippage massimo | 3% | `MAX_SLIPPAGE_BPS` |
| Gas price massimo | 0.5 gwei | `MAX_GAS_PRICE_GWEI` |
| Riserva ETH per il gas | 0.0015 ETH | `MIN_ETH_RESERVE` |
| Stop loss / take profit | -20% / +40% | `DEFAULT_STOP_LOSS_PCT`, `DEFAULT_TAKE_PROFIT_PCT` |

Il modello **non può aggirarli**: sono applicati dall'esecutore, non dal prompt.
Un segnale fuori limite viene rifiutato e il motivo finisce a DB e in Telegram.

All'avvio il bot verifica on-chain gli indirizzi configurati (`symbol`,
`decimals`, presenza del bytecode): un indirizzo sbagliato ferma il ciclo
invece di far partire uno swap verso il nulla.

---

## 🚀 Avvio

```bash
cp .env.example .env     # compila wallet, RPC e chiave OpenRouter
pip install -r requirements.txt
python main.py           # un ciclo, in dry-run
python dashboard.py      # dashboard su http://localhost:3000
```

Con Docker:

```bash
docker compose up --build
```

### Deploy su CapRover

Il repo è pronto per CapRover: `captain-definition` in root, un solo
processo, nessun servizio Node da buildare (a differenza del bot perp).

1. **Crea l'app** e collega il repo (o `caprover deploy` dalla cartella).
2. **App Configs → Container HTTP Port: `3000`.** CapRover di default
   assume la 80: senza questo la dashboard non risponde.
3. **Persistent Directories** → mappa `/app/data`. Il database SQLite ci
   finisce da solo (`config.default_db_path()` usa `/app/data` quando esiste),
   così storico, swap e screening sopravvivono ai redeploy. Senza questa
   mappatura riparti da zero a ogni deploy.
4. **Environment Variables**: almeno `OPENROUTER_API_KEY`, `WALLET_ADDRESS`,
   `PRIVATE_KEY`, `BASE_RPC_URL`. Lascia `DRY_RUN=true` per i primi cicli.
   Le chiavi vanno qui, non nel repo: il `.env` è escluso dall'immagine.
5. **Enable HTTPS** se esponi la dashboard: non ha autenticazione, e da lì si
   può lanciare un ciclo con "Esegui Ciclo Ora".

Il build installa Prophet (qualche minuto, scarica un wheel precompilato con
cmdstan). Su istanze da 1 GB di RAM conviene tenere d'occhio la memoria
durante il primo build.

L'intervallo fra i cicli è `TRADING_INTERVAL` (default 900s), come nel bot perp.

**Parti sempre in dry-run.** Il ciclo gira completo e registra tutto, ma non
firma nulla finché non metti `DRY_RUN=false`. Quando passi in live, usa un
wallet dedicato con dentro solo quello che puoi perdere.

---

## 📁 Struttura

| File | Ruolo |
|---|---|
| `main.py` | il ciclo: screening → analisi → decisione → esecuzione |
| `config.py` | contratti Base, soglie di rischio e di screening |
| `base_client.py` | RPC, ERC-20, invio transazioni, dry-run, tetto gas |
| `uniswap.py` | quoting QuoterV2, scelta rotta, swap SwapRouter02 |
| `token_screener.py` | CoinGecko + DexScreener + GoPlus |
| `wallet.py` | portafoglio on-chain, prezzo di carico, uscite di rischio |
| `spot_trader.py` | traduzione del segnale in swap, limiti di rischio |
| `market_data.py` | candele: CEX per i major, pool Base per le meme |
| `indicators.py` | analisi tecnica |
| `forecaster.py` | previsioni Prophet 15m e 1h |
| `trading_agent.py` | chiamata LLM, schema JSON, fallback di sicurezza |
| `db_utils.py` | persistenza SQLite (snapshot, swap, screening, errori) |
| `dashboard.py` | dashboard web |
| `telegram_bot.py` | notifiche e comandi |

---

## ⚠️ Avvertenza

Questo è un bot speculativo che compra memecoin con capitale reale. Lo
screening riduce il rischio di scam contract, **non** il rischio di mercato:
una meme può perdere il 90% restando un token perfettamente "sano" per GoPlus.
Nessuna garanzia, nessuna promessa di rendimento. Software fornito così com'è.

## 📜 Licenza

MIT.
