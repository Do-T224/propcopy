# Copying MT5 trades to NinjaTrader (futures follower)

A `platform: ninjatrader` follower copies the source account's **XAUUSD** trades onto a
NinjaTrader 8 account as **Micro Gold (MGC)** or **Gold (GC)** futures. Typical use: copy a
forex/CFD gold account onto a futures prop firm account that trades through NinjaTrader.

It talks to a **running NinjaTrader 8 desktop** on the same Windows machine through NinjaTrader's
Automated Trading Interface (ATI). There is no cloud API: NinjaTrader must be open, logged in
and connected, otherwise every copy is skipped (`ROUTE_SKIP reason=ninjatrader_disconnected`)
and the dashboard shows the follower as disconnected. That is safe, but nothing is copied.
If NinjaTrader is not running when you **start the engine**, the follower fails to connect and
stops; start NinjaTrader first, then restart the engine.

> **Status: tested against the NinjaTrader 8 simulator (`Sim101`) only.** Read
> [DISCLAIMER.md](../DISCLAIMER.md). Many futures prop firms restrict or forbid automation and
> external trade copying. Check your firm's rules before using this on a funded account.

## What is and is not supported

| | |
|---|---|
| Source | MT5 or MT4 account (any PropCopy source) |
| Instrument | `XAUUSD` -> `MGC` or `GC` only |
| Platform | NinjaTrader 8, ATI, Windows |
| Orders | Market entry, with stop-loss and take-profit brackets placed as absolute prices |
| Not built | Other symbols, the NinjaTrader add-on transport (`nt_transport: addon`), other futures platforms |

## 1. One-time NinjaTrader configuration

1. **Tools > Options > Automated trading interface > General**
   - Tick **AT Interface**.
   - Note the **Server port** (default `36973`). It goes in `nt_server_port`.
   - Note the **Default account**. The account you copy to goes in `account_name`.
   - **Restart NinjaTrader.** The ATI socket only starts listening on launch.
2. Make sure the target account exists and is connected (use `Sim101` for testing).
3. **Control Center > Connections:** connect your data/broker feed. Market data reads 0 until the
   instrument is streaming.
4. Leave NinjaTrader running.

**Use `127.0.0.1`, not `localhost`.** NinjaTrader listens on IPv4. `localhost` can resolve to IPv6
(`::1`) and the connection is refused. The default is `127.0.0.1`; change `nt_host` only if
NinjaTrader runs on another machine.

## 2. Python dependency

`NinjaTrader.Client.dll` (in `C:\Program Files\NinjaTrader 8\bin\`) is a managed .NET assembly,
loaded through [pythonnet](https://pythonnet.github.io/):

```powershell
pip install -r requirements-ninjatrader.txt
```

If NinjaTrader is installed elsewhere, set `nt_dll_path` on the follower.

## 3. Configure the follower

Add an entry to `slaves:` in `config.yaml`. Futures followers are edited in the file (the
dashboard shows them under **Futures Followers** and lets you change a few values).

```yaml
slaves:
  - account: 3                  # any unique integer id for this follower (not the NT account)
    platform: ninjatrader
    account_name: "Sim101"      # EXACT NinjaTrader account name
    nt_server_port: 36973       # NinjaTrader Options -> "Server port"
    nt_host: 127.0.0.1
    nt_instrument_mode: explicit
    symbol_map: { XAUUSD: MGC } # MGC (micro) or GC (full-size)
    size_scaler: 1.0
    sltp_multiplier: 1.0
    small_leg_policy: merge     # merge | skip_strict | floor_1
    merge_window_s: 300
    hard_contract_cap: 50
    setup_allowlist: ["*"]      # copy every source trade (see "Tag filtering")
    max_drawdown_pct: 0         # 0 = off
    profit_target_usd: 0        # absolute equity level; 0 = off
```

### Sizing

Gold on MT5 is usually 1.0 lot = 100 oz. That makes **1.0 lot = 1 GC = 10 MGC**, and 0.1 lot =
1 MGC, with the same dollars per $1 move. At `size_scaler: 1.0` a source trade risks roughly the
same dollars on the future as on the source account. Check your broker's contract size first.
Contracts are whole numbers, so results round (half-up).

- `size_scaler`: multiplies the contract count. Below 1.0 takes less risk than the source; above
  1.0 takes more.
- `hard_contract_cap`: a safety limit on contracts per position.

### Small legs: `small_leg_policy`

A source trade under 0.05 lot rounds to 0 MGC. Strategies that scale in (a small first leg, then
a bigger second leg) are affected most.

| Policy | Behaviour |
|---|---|
| `merge` (default) | Keeps a running total per tag and side, and tops the futures position up to the rounded total. A tiny first leg waits, and later legs place the rest. Needs a comment tag (below) to know which legs belong together; untagged trades are sized one by one. |
| `skip_strict` | Drops any leg that rounds to 0 contracts. Exposure can run under the source. |
| `floor_1` | Forces at least 1 contract. Exposure can run over the source. |

### Tag filtering: `setup_allowlist`

If the source trade's MT5 comment looks like `TAG:anything`, `TAG` is read as its tag.

- `["*"]` (or leaving the key out) copies every trade, tagged or not.
- A list such as `["BREAKOUT", "PULLBACK"]` copies only trades whose comment starts with one of
  those tags. Everything else is skipped (`ROUTE_SKIP reason=setup_not_allowed`).

## 4. How it behaves

- **Open:** a market order for the computed contracts, then SL/TP brackets as absolute prices
  anchored to the actual fill.
- **Close:** when the source closes, an opposing market order flattens the position and the
  brackets are cancelled. NinjaTrader does **not** auto-cancel a bracket pair when the position
  is closed by a separate order, so this cancel step is required.
- **Ledger:** a per-follower file under `data/followers/` records which source ticket owns how many
  contracts. Futures accounts are netting (one net position per instrument), so the net position
  cannot tell you which source trade contributed what. Keep this folder while positions are open.
- **Rejections:** the ATI command return code is `0` even for a bad order. PropCopy checks the
  order status instead.
- **Front month:** NinjaTrader needs an explicit contract such as `MGC DEC26` (a bare `MGC` raises
  "Unknown instrument"). PropCopy picks the liquid gold month (Feb, Apr, Jun, Aug, Dec) from the
  calendar.
- **Dashboard:** the Status tab shows the follower as `NINJA`. Suspend/resume, drawdown guardian,
  and profit target work as for MT5 followers. Skipped copies appear as `ROUTE_SKIP` events.

## 5. Known gaps

- Only tested on the NinjaTrader simulator, not on a live or funded account.
- The `profit` on a `TRADE_CLOSED` event can read 0 (NinjaTrader updates `RealizedPnL`
  asynchronously). Cosmetic: analytics uses the source account's data.
- Modifying SL/TP after entry is implemented but lightly exercised.
- A bracket hit on the exchange side (the backstop) is not exercised by the tests.
- Contract rolls: the contract month is resolved once and cached. New trades are skipped when the
  contract is near its notice date (`ROUTE_SKIP reason=contract_near_notice`). Restart PropCopy
  around a roll.
