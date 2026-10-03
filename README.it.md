<img src="static/icon.svg" alt="" width="88" height="88" align="left">

# Degen Trading Agent (Base spot)

<br clear="left">

[English](README.md) · **Italiano**

> ⚠️ **Software sperimentale, non consulenza finanziaria.** Il bot opera con denaro reale su Base e può perdere in parte o del tutto il capitale che gli affidi. Parti in paper trading o dry-run; in live usa un wallet dedicato e solo importi che puoi permetterti di perdere. Dettagli nella sezione **Avvertenza** in fondo.

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
3. **Persistent Directories** → mappa `/app/data`. Database SQLite e
   `positions.json` ci finiscono da soli (`config.persistent_path()` usa
   `/app/data` quando esiste), così storico, swap, screening **e i prezzi di
   carico delle posizioni** sopravvivono ai redeploy. Senza questa mappatura
   riparti da zero a ogni deploy: il bot si ritroverebbe i token nel wallet
   senza sapere a quanto li ha comprati, quindi senza stop loss.
4. **Environment Variables**: almeno `OPENROUTER_API_KEY`, `WALLET_ADDRESS`,
   `PRIVATE_KEY`, `BASE_RPC_URL`. Lascia `DRY_RUN=true` per i primi cicli.
   Le chiavi vanno qui, non nel repo: il `.env` è escluso dall'immagine.
5. **Enable HTTPS** se esponi la dashboard: non ha autenticazione. Il
   pulsante "Esegui ciclo ora" resta disattivato finché non imposti
   `DASHBOARD_RUN_TOKEN` (il browser lo chiede una volta e lo ricorda).

Il build installa Prophet (qualche minuto, scarica un wheel precompilato con
cmdstan). Su istanze da 1 GB di RAM conviene tenere d'occhio la memoria
durante il primo build.

L'intervallo fra i cicli è `TRADING_INTERVAL` (default 900s), come nel bot perp.

### Le tre modalità

| Modalità | `.env` | Cosa succede |
|---|---|---|
| **Dry-run** (default) | `DRY_RUN=true` | Legge il wallet vero, decide, ma non firma. Se non hai USDC gli ordini vengono rifiutati dai limiti di rischio. |
| **Paper trading** | `PAPER_TRADING=true` | Portafoglio virtuale (`PAPER_START_USDC`, default $1000) con **prezzi e rotte reali**: gli ordini vengono eseguiti contro il saldo finto, quindi vedi P&L, stop loss e take profit lavorare senza capitale sul wallet. Implica sempre il dry-run. |
| **Live** | `DRY_RUN=false` | Firma e manda le transazioni. |

In paper trading resta reale tutto tranne i saldi: quotazioni Uniswap,
screening, limiti di rischio e decisioni del modello. Il riempimento sconta
sempre lo slippage massimo, così la simulazione è pessimista invece che
ottimista, e ogni swap addebita `PAPER_GAS_USD` di gas figurato.
Lo stato vive in `paper_portfolio.json` sul volume persistente: per
ricominciare da capo, cancella quel file.

**Parti sempre in dry-run o paper.** Quando passi in live, usa un
wallet dedicato con dentro solo quello che puoi perdere.

### ⛽ Rifornimento Automatico (Auto-Refuel ETH -> USDC)

Se il wallet ha USDC insufficienti (< `USDC_AUTO_SWAP_THRESHOLD`, default $5.0) ma possiede ETH nativo, l'agente converte in automatico l'ETH in eccesso in USDC tramite Uniswap V3 all'inizio del ciclo, riservando sempre l'ETH per pagare le fee (`ETH_GAS_RESERVE`, default 0.003 ETH).
In questo modo è sufficiente inviare solo ETH al wallet per rendere il bot operativo, senza dover inviare separatamente anche USDC.

```bash
# Controllo rapido o esecuzione manuale refuel
python main.py --refuel

# Ispezione saldi wallet con il tool dedicato
python tools/refuel.py --status
```

---

## 🎯 Seguire un token specifico

L'universo si costruisce da solo dai token più scambiati su Base, ma puoi
aggiungerne di tuoi senza toccare il codice:

```bash
# valutato a ogni ciclo, anche se non è fra i più scambiati del momento
TOKEN_WATCHLIST="0xIndirizzoDelToken"

# se non è nella token list CoinGecko di Base, marcalo come verificato da te:
# salta QUEL requisito, non i controlli di liquidità e di contratto
TOKEN_TRUSTED="0xIndirizzoDelToken"
```

L'indirizzo va preso da una fonte che controlli tu (sito ufficiale del
progetto, BaseScan, CoinGecko): copiare un contratto sbagliato è esattamente
il modo in cui si compra un clone-scam, e nessuno screening può salvarti da
un indirizzo che gli hai dato tu come buono.

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
| `http_client.py` | pacing, retry e cache per le API pubbliche |
| `paper.py` | portafoglio virtuale per il paper trading |
| `indicators.py` | analisi tecnica |
| `forecaster.py` | previsioni Prophet 15m e 1h |
| `trading_agent.py` | chiamata LLM, schema JSON, fallback di sicurezza |
| `db_utils.py` | persistenza SQLite (snapshot, swap, screening, errori) |
| `dashboard.py` | dashboard web |
| `static/dashboard.css`, `static/dashboard.js` | design system condiviso con i bot fratelli |
| `telegram_bot.py` | notifiche e comandi |

---

### Dashboard coerente fra i tre agenti

Le dashboard di `intelligent-trading-agent`, `-degen` e `-yield` condividono lo
stesso design system: `static/dashboard.css` e `static/dashboard.js` sono
**identici nei tre repository** (se li modifichi, copiali negli altri due).
Ogni pagina ha la stessa struttura: header con badge di modalità
(`LIVE` / `PAPER` / `DRY-RUN`), pannello paper trading, KPI, andamento del
capitale, posizioni e ultima decisione AI, sezioni specifiche del bot, storico
operazioni ed errori. Cambia solo il colore d'accento (blu, arancio, verde)
e l'icona.


#### Wallet e gas

Sotto l'header, fuori dal paper trading, la dashboard mostra il wallet del
bot: ETH per il gas (con il controvalore), USDC liberi, indirizzo con link a
Basescan e uno stato: **OK**, **IN ESAURIMENTO** (sotto `GAS_WARN_ETH`) o
**RICARICA ORA** (sotto la riserva minima). Lo stesso avviso compare nel
report Telegram del ciclo.

---

## ⚠️ Avvertenza

Questo software è sperimentale ed è fornito "così com'è", senza garanzie di alcun tipo
(vedi la licenza MIT). Non è consulenza finanziaria né un invito a investire.

- **Puoi perdere denaro.** Bug, decisioni sbagliate del modello, slippage, exploit dei protocolli,
  oracoli manipolati e liquidazioni possono far perdere in parte o del tutto il capitale.
- **Le decisioni le prende un LLM.** Può sbagliare o comportarsi in modo imprevedibile: i limiti
  dell'esecutore riducono il danno, non lo azzerano. I rendimenti passati, anche in paper, non
  garantiscono quelli futuri.
- **Parti in paper o dry-run.** In live usa un wallet dedicato al bot, con importi che puoi
  permetterti di perdere, e non riutilizzare quella chiave privata altrove.
- **Proteggi le chiavi.** La chiave privata va solo nelle variabili d'ambiente del deploy: non
  committarla mai. Senza `DASHBOARD_RUN_TOKEN` i comandi della dashboard restano disattivati:
  impostalo con un valore lungo e casuale prima di esporla su Internet.
- **Leggi e tasse.** Sei responsabile del rispetto delle norme e degli obblighi fiscali del tuo paese.
- **Memecoin.** Token nuovi e illiquidi possono andare a zero in pochi minuti, essere honeypot o avere tasse nascoste: i filtri di sicurezza riducono il rischio, non lo eliminano.

**Rischi specifici di questo bot.** Questo è un bot speculativo che compra memecoin con capitale reale. Lo
screening riduce il rischio di scam contract, **non** il rischio di mercato:
una meme può perdere il 90% restando un token perfettamente "sano" per GoPlus.
Nessuna garanzia, nessuna promessa di rendimento. Software fornito così com'è.

## 📜 Licenza

MIT.
