<img src="static/icon.svg" alt="" width="88" height="88" align="left">

# Degen Trading Agent (Base spot)

<br clear="left">

**English** · [Italiano](README.it.md)

> ⚠️ **Experimental software, not financial advice.** The bot trades real money on Base and can lose some or all of the capital you give it. Start with paper trading or dry-run; when you go live, use a dedicated wallet and only amounts you can afford to lose. See the **Disclaimer** section at the bottom.

A speculative trading agent that trades **straight from the wallet on Base**:
no third-party protocols, no leverage, no perpetuals. It buys and sells ERC-20
tokens (memecoins included) through **Uniswap V3**, with anti-scam screening
in front of every purchase.

It is part of the [Intelligent Trading](https://github.com/scobru/intelligent-trading)
suite of agents for Base, and it is the spot sibling of the
[perp bot](https://github.com/scobru/intelligent-trading-agent-perp), which
trades SynFutures V3 perpetuals. Structure, dashboard, Telegram notifications
and decision cycle are the same: the execution layer changes and token
selection is added.

---

## 🧠 How a cycle works

1. **Screening** — the tradable universe is rebuilt every cycle:
   - discovery of the most traded tokens on Base (GeckoTerminal);
   - **CoinGecko**: if the token is not in the official Base token list, it is out;
   - **liquidity, 24h volume, pool age** (DexScreener) above thresholds;
   - **GoPlus Security**: honeypot, buy/sell tax, mintable, hidden owner,
     pausable transfers, blacklist, selfdestruct.

   Every rejection keeps its reason and shows up in the dashboard: you can see
   *why* a token was not considered.

2. **Portfolio** — balances read on-chain (`balanceOf`), valued by quoting the
   real route on Uniswap. Cost basis, stop loss and take profit live in
   `positions.json`, because they do not exist on-chain.

3. **Automatic exits** — stop loss and take profit are executed *before*
   consulting the model: risk management is not delegated to an LLM.

4. **Analysis** — technical indicators and Prophet forecasts on the cycle's
   assets. Candles come from CEXs for the majors and from the most liquid Base
   pool for memecoins.

5. **Decision** — the LLM (via OpenRouter) receives universe, indicators, news,
   sentiment and forecasts, and answers with a `buy` / `sell` / `hold` JSON.

6. **Execution** — quoting on QuoterV2 (direct route on every fee tier, plus
   two hops via WETH), swap on SwapRouter02, all within the risk limits.

---

## 🛡️ Guardrails before every transaction

| Limit | Default | Variable |
|---|---|---|
| No transaction is signed | **on** | `DRY_RUN=true` |
| Max share in a single token | 25% of the portfolio | `MAX_POSITION_PCT` |
| Tokens held at the same time | 5 | `MAX_OPEN_TOKENS` |
| Amount per trade | $5 – $250 | `MIN_TRADE_USD`, `MAX_TRADE_USD` |
| Max slippage | 3% | `MAX_SLIPPAGE_BPS` |
| Max gas price | 0.5 gwei | `MAX_GAS_PRICE_GWEI` |
| ETH reserve for gas | 0.0015 ETH | `MIN_ETH_RESERVE` |
| Stop loss / take profit | -20% / +40% | `DEFAULT_STOP_LOSS_PCT`, `DEFAULT_TAKE_PROFIT_PCT` |

The model **cannot bypass them**: they are enforced by the executor, not by
the prompt. A signal outside the limits is rejected and the reason is stored
in the DB and sent to Telegram.

On startup the bot checks the configured addresses on-chain (`symbol`,
`decimals`, bytecode present): a wrong address stops the cycle instead of
sending a swap into the void.

---

## 🚀 Getting started

```bash
cp .env.example .env     # fill in wallet, RPC and OpenRouter key
pip install -r requirements.txt
python main.py           # one cycle, in dry-run
python dashboard.py      # dashboard at http://localhost:3000
```

With Docker:

```bash
docker compose up --build
```

### Deploy on CapRover

The repo is ready for CapRover: `captain-definition` at the root, a single
process, no Node service to build (unlike the perp bot).

1. **Create the app** and connect the repo (or `caprover deploy` from the folder).
2. **App Configs → Container HTTP Port: `3000`.** CapRover assumes port 80 by
   default: without this the dashboard does not respond.
3. **Persistent Directories** → map `/app/data`. The SQLite database and
   `positions.json` go there automatically (`config.persistent_path()` uses
   `/app/data` when it exists), so history, swaps, screening **and the cost
   basis of your positions** survive redeploys. Without this mapping you start
   from scratch on every deploy: the bot would find tokens in the wallet
   without knowing what it paid for them, and therefore without a stop loss.
4. **Environment Variables**: at least `OPENROUTER_API_KEY`, `WALLET_ADDRESS`,
   `PRIVATE_KEY`, `BASE_RPC_URL`. Keep `DRY_RUN=true` for the first cycles.
   Keys go here, not in the repo: `.env` is excluded from the image.
5. **Enable HTTPS** if you expose the dashboard: it has no login. The "Run
   cycle now" button stays disabled until you set `DASHBOARD_RUN_TOKEN` (the
   browser asks for it once and remembers it).

The build installs Prophet (a few minutes, it downloads a prebuilt wheel with
cmdstan). On 1 GB RAM instances keep an eye on memory during the first build.

The interval between cycles is `TRADING_INTERVAL` (default 900s), as in the
perp bot.

### The three modes

| Mode | `.env` | What happens |
|---|---|---|
| **Dry-run** (default) | `DRY_RUN=true` | Reads the real wallet and decides, but does not sign. Without USDC, orders are rejected by the risk limits. |
| **Paper trading** | `PAPER_TRADING=true` | Virtual portfolio (`PAPER_START_USDC`, default $1000) with **real prices and routes**: orders are executed against the fake balance, so you can watch P&L, stop loss and take profit work without capital in the wallet. Always implies dry-run. |
| **Live** | `DRY_RUN=false` | Signs and sends the transactions. |

In paper trading everything is real except the balances: Uniswap quotes,
screening, risk limits and model decisions. Fills always pay the maximum
slippage, so the simulation is pessimistic rather than optimistic, and every
swap charges `PAPER_GAS_USD` of notional gas. The state lives in
`paper_portfolio.json` on the persistent volume: delete that file to start
over.

**Always start in dry-run or paper.** When you go live, use a dedicated wallet
holding only what you can afford to lose.

### ⛽ Auto-refuel (ETH → USDC)

If the wallet holds too little USDC (< `USDC_AUTO_SWAP_THRESHOLD`, default
$5.0) but has native ETH, the agent automatically converts the excess ETH into
USDC on Uniswap V3 at the start of the cycle, always keeping the ETH needed for
fees (`ETH_GAS_RESERVE`, default 0.003 ETH). Sending only ETH to the wallet is
enough to make the bot operational.

```bash
# Quick check or manual refuel
python main.py --refuel

# Inspect wallet balances with the dedicated tool
python tools/refuel.py --status
```

---

## 🎯 Following a specific token

The universe builds itself from the most traded tokens on Base, but you can
add your own without touching the code:

```bash
# evaluated every cycle, even if it is not among the most traded right now
TOKEN_WATCHLIST="0xTokenAddress"

# if it is not in the CoinGecko Base token list, mark it as verified by you:
# it skips THAT requirement, not the liquidity and contract checks
TOKEN_TRUSTED="0xTokenAddress"
```

Take the address from a source you trust (the project's official site,
BaseScan, CoinGecko): copying the wrong contract is exactly how people buy a
scam clone, and no screening can save you from an address you told it was
good.

## 📁 Structure

| File | Role |
|---|---|
| `main.py` | the cycle: screening → analysis → decision → execution |
| `config.py` | Base contracts, risk and screening thresholds |
| `base_client.py` | RPC, ERC-20, transaction sending, dry-run, gas cap |
| `uniswap.py` | QuoterV2 quoting, route selection, SwapRouter02 swaps |
| `token_screener.py` | CoinGecko + DexScreener + GoPlus |
| `wallet.py` | on-chain portfolio, cost basis, risk exits |
| `spot_trader.py` | turns the signal into a swap, risk limits |
| `market_data.py` | candles: CEXs for the majors, Base pools for memecoins |
| `http_client.py` | pacing, retries and caching for public APIs |
| `paper.py` | virtual portfolio for paper trading |
| `indicators.py` | technical analysis |
| `forecaster.py` | Prophet forecasts, 15m and 1h |
| `trading_agent.py` | LLM call, JSON schema, safety fallback |
| `db_utils.py` | SQLite persistence (snapshots, swaps, screening, errors) |
| `dashboard.py`, `dashboard_auth.py` | web dashboard and token check for its commands |
| `static/dashboard.css`, `static/dashboard.js` | design system shared with the sibling bots |
| `telegram_bot.py` | notifications and commands |

---

### A consistent dashboard across the suite

All the agents in the suite share the same design system:
`static/dashboard.css` and `static/dashboard.js` are **identical in every
repository** (if you change them, copy them to the others). Every page has the
same structure: header with a mode badge (`LIVE` / `PAPER` / `DRY-RUN`), paper
trading panel, KPIs, equity curve, positions and last AI decision, bot-specific
sections, operation history and errors. Only the accent color and the icon
change.

#### Wallet and gas

Below the header, outside paper trading, the dashboard shows the bot's wallet:
ETH for gas (with its dollar value), free USDC, address with a Basescan link
and a status: **OK**, **RUNNING LOW** (below `GAS_WARN_ETH`) or **TOP UP NOW**
(below the minimum reserve). The same warning appears in the cycle's Telegram
report.

---

## ⚠️ Disclaimer

This software is experimental and provided "as is", without warranty of any
kind (see the MIT license). It is not financial advice nor an invitation to
invest.

- **You can lose money.** Bugs, wrong model decisions, slippage, protocol
  exploits, manipulated oracles and liquidations can cause the loss of some or
  all of your capital.
- **Decisions are made by an LLM.** It can be wrong or behave unpredictably:
  the executor's limits reduce the damage, they do not eliminate it. Past
  results, paper ones included, do not guarantee future ones.
- **Start with paper or dry-run.** When live, use a wallet dedicated to the
  bot, with amounts you can afford to lose, and never reuse that private key
  elsewhere.
- **Protect your keys.** The private key belongs only in the deployment's
  environment variables: never commit it. Without `DASHBOARD_RUN_TOKEN` the
  dashboard commands stay disabled: set it to a long random value before
  exposing the dashboard to the Internet.
- **Laws and taxes.** You are responsible for complying with the rules and tax
  obligations of your country.
- **Memecoins.** New and illiquid tokens can go to zero in minutes, be
  honeypots or carry hidden taxes: the safety filters reduce the risk, they do
  not eliminate it.

**Risks specific to this bot.** This is a speculative bot that buys memecoins
with real capital. Screening reduces the risk of scam contracts, **not** market
risk: a memecoin can lose 90% while remaining a perfectly "healthy" token for
GoPlus. No guarantees, no promised returns.

## 📜 License

MIT.
