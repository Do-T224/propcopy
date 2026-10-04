# PropCopy - Open-Source MT4/MT5 and NinjaTrader Trade Copier for Prop Firm Traders

**PropCopy is a free, self-hosted trade copier for MetaTrader 5 (MT5) and MetaTrader 4 (MT4), with
an optional NinjaTrader 8 futures follower, written in Python.**
It watches one source account and copies its trades to any number of follower accounts in about
10 ms, with per-account lot scaling, execution controls, and a local web dashboard with trade
analytics. It runs on your own Windows PC or VPS, so your broker credentials never leave your machine.

Typical uses:

- copy one trading account to several prop firm challenge or funded accounts;
- mirror a source account to followers with different lot sizes or different broker symbol names;
- copy an MT5 gold (XAUUSD) account onto a NinjaTrader futures account as Micro Gold (MGC) or Gold (GC);
- tune copies with price offsets, SL/TP multipliers, drawdown guardians, and profit-target auto-close.

**Keywords:** trade copier, copy trading, MT5 trade copier, MT4 trade copier, MetaTrader copier,
NinjaTrader copier, MT5 to NinjaTrader, futures, prop firm, forex, gold, XAUUSD, local copier,
multi-account, Python, FastAPI, open source.

> **Warning: trading is risky, and copy trading may violate your prop firm's rules.**
> Read [DISCLAIMER.md](DISCLAIMER.md) before connecting any real or funded account.
> Test on demo accounts first. The software is provided as-is, with no warranty ([MIT](LICENSE)).

## Contents

- [How it works](#how-it-works)
- [Features](#features)
- [Requirements](#requirements)
- [Install](#install)
- [Using PropCopy, step by step](#using-propcopy-step-by-step)
- [Configuration reference](#configuration-reference)
- [NinjaTrader futures follower](#ninjatrader-futures-follower)
- [MT4 accounts](#mt4-accounts)
- [Command-line options](#command-line-options)
- [Tests and building an executable](#tests-and-building-an-executable)
- [Security notes](#security-notes)
- [Troubleshooting](#troubleshooting)
- [FAQ](#faq)

## How it works

```
                                  +--> follower process 1 --> MT5 / MT4 follower account
MT5 source account --> monitor ---+--> follower process 2 --> MT5 / MT4 follower account
   (polled ~10 ms)                +--> follower process 3 --> NinjaTrader 8 (MGC / GC futures)
                                  |
                          dashboard + analytics (local web UI)
```

A monitor process polls the source account's open positions. When it sees a new position, a
closed position, or a changed SL/TP, it sends that event to every follower. Each follower runs in
its own process, applies its own sizing and execution rules, and places the matching order on its
own account.

## Features

- Source -> N followers, each follower in its own process for isolation
- Copies opens, closes, and SL/TP modifications (about 10 ms polling by default)
- Per-follower **size scaler** and per-account **symbol mapping** for brokers that name
  instruments differently
- Per-follower execution options: price offset (better entry or slippage guard), volume jitter,
  SL/TP offset and multiplier, invert (flip direction), drawdown guardian, profit-target auto-close
- Pause or resume the source or any follower from the dashboard without stopping the engine
- **MT5** via the `MetaTrader5` Python package, **MT4** via a bundled EA bridge
- **MT5 -> NinjaTrader futures:** copy XAUUSD trades to MGC or GC on a NinjaTrader 8 account
  (simulator-tested; see [docs/NINJATRADER_SETUP.md](docs/NINJATRADER_SETUP.md))
- Local dashboard (native window or browser): live status over WebSocket, account manager,
  positions, trade history, logs
- Analytics: MFE/MAE, exit efficiency, entry/exit timing, SL/TP hit rate, streak and tilt
  detection, session heatmap, copy latency and slippage
- Optional setup tagging for trades

## Requirements

- **Windows** (the `MetaTrader5` Python package only works on Windows)
- **Python 3.10 or newer**
- One MetaTrader 5 terminal installation **per account**, each in its own folder, so every
  terminal stays logged in to its own account
- In every MT5 terminal: *Tools > Options > Expert Advisors > Allow algorithmic trading*
- For the NinjaTrader follower only: NinjaTrader 8 on the same PC, and `pythonnet`

## Install

```powershell
git clone https://github.com/Do-T224/propcopy.git
cd propcopy
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
```

For the NinjaTrader follower, use `pip install -r requirements-ninjatrader.txt` instead.

## Using PropCopy, step by step

**1. Prepare your terminals.** Install one MT5 terminal per account (copy the installation
folder, or run the broker installer to a different folder each time). Log in once in each,
and note each folder path, for example `C:\Program Files\MetaTrader 5 Second`.

**2. Start the dashboard.**

```powershell
python main.py
```

A native window opens (use `python main.py --browser` for `http://localhost:8181`).
`config.yaml` is created automatically on first run.

**3. Add your accounts.** Open the **Config** tab, expand **Saved Accounts**, and click
**+ Add Account** for each account (source and followers). Enter a tag (any label you like),
the login number, password, broker server name, the terminal folder path, and the platform.
You can instead copy `accounts.example.yaml` to `accounts.yaml` and edit it by hand.

**4. Choose the source.** In **Source Account**, pick the account whose trades should be copied.

**5. Add followers.** In **Follower Accounts**, click **+ Add Follower**, pick an account, and set
its **Vol x** (size scaler; `0.5` copies half the source's lot size). Optional per-follower
settings (price offset, volume jitter, SL/TP offset) are under the row's **Stagger** button;
SL/TP multiplier, invert, drawdown and profit target are in the row itself.

**6. Save.** Click **Save Configuration**.

**7. Test on demo.** Start with demo accounts. Place a small trade on the source account and
confirm it appears on every follower, then close it and confirm the close is copied.

**8. Start copying.** Click **Start Engine** at the top of the dashboard.

- **Status** tab: live connection state, equity, and drawdown for the source and every follower.
  Use the toggle on a row to pause or resume that account without stopping the engine.
  Pausing a follower blocks new copies; closes and SL/TP changes still go through so you do not
  end up with orphaned positions.
- **Positions / History / Analytics** tabs: open trades, closed trades, and performance insights.
- **Logs** tab: filter by level. The same log is written to `data/propcopy.log`.

**9. Stop.** Click **Stop Engine**. Open positions on followers are not closed automatically.

### Headless mode

Once `config.yaml` is set up, you can run without the dashboard:

```powershell
python main.py --headless
```

## Configuration reference

Two YAML files live next to `main.py`. **Both contain credentials in plain text and are
git-ignored. Never commit or share them.**

**`accounts.yaml`** is your account book. Each entry has a `tag` (a label you choose), the login
`account`, `password`, broker `server`, the path to that account's terminal folder `mt5_path`,
and `platform` (`mt5` or `mt4`).

**`config.yaml`** picks the source and followers and holds settings. The dashboard writes it for
you. Minimal example:

```yaml
master:                       # the source account
  account: 12345678
  password: your_password
  server: YourBroker-Server
  mt5_path: C:\Program Files\MetaTrader 5
  platform: mt5
slaves:                       # the followers
  - account: 87654321
    password: your_password
    server: YourBroker-Server
    mt5_path: C:\Program Files\MetaTrader 5 Second
    platform: mt5
    size_scaler: 0.5          # follower lot = source lot x 0.5
```

(The `master` / `slaves` key names are the file format; the UI calls them Source / Follower.)

### Per-follower options

| Key | Default | Effect |
|-----|---------|--------|
| `size_scaler` | `1.0` | Multiplies the source volume |
| `price_offset_points` | `0` | `>0`: wait for a better entry by N points. `<0`: slippage guard, skip if price moved N points against you. `0`: off |
| `price_offset_timeout` | `20` | Seconds to wait for the price offset condition |
| `price_offset_max_points` | `0` | Skip the copy if price moved more than this (`0` = no limit) |
| `invert` | `false` | Flip BUY/SELL and swap SL/TP. **May be prohibited by your firm.** |
| `sltp_multiplier` | `1.0` | Scale SL/TP distance (ignored when `invert` is on) |
| `volume_jitter_pct` | `0` | Randomly vary each copied volume by up to +/- this percent |
| `sltp_offset_points` | `0` | Randomly offset copied SL/TP by up to this many points |
| `max_drawdown_pct` | `0` | Guardian: emergency-close the follower if drawdown from peak equity reaches this percent (`0` = off) |
| `profit_target_usd` | `0` | **Absolute equity level**, not profit. When the follower's *equity* reaches this value, all its positions are closed and it is disconnected from the copier (`0` = off). Example: 10k account, target `11000`. Can be armed/disarmed from the Status tab |

See `config.example.yaml` for global settings (polling interval, magic number, retries, deviation).

### Symbol mapping

If a follower's broker spells an instrument differently from the source's, add a `symbol_map` to
that account in `accounts.yaml` (source name on the left):

```yaml
- tag: My Account
  # ...
  symbol_map:
    GER40.cash: DAX40
    US100.cash: NAS100
```

Unmapped symbols are copied under their original name. `symbol_map` is copied into `config.yaml`
when you save the Config tab, so **after editing `accounts.yaml` by hand, press Save Configuration
in the Config tab** before starting the engine.

## NinjaTrader futures follower

PropCopy can copy the source's **XAUUSD** trades onto a **NinjaTrader 8** account as **MGC**
(micro) or **GC** futures, through NinjaTrader's Automated Trading Interface (ATI).
NinjaTrader must be running on the same PC.

Quick setup:

1. In NinjaTrader: *Tools > Options > Automated trading interface*, tick **AT Interface**, note
   the **Server port** (default `36973`), then restart NinjaTrader and connect your feed.
2. `pip install -r requirements-ninjatrader.txt`
3. Add an entry to `slaves:` in `config.yaml`:

```yaml
  - account: 3                  # any unique integer id
    platform: ninjatrader
    account_name: "Sim101"      # EXACT NinjaTrader account name
    nt_server_port: 36973
    symbol_map: { XAUUSD: MGC } # MGC (micro) or GC (full-size)
    size_scaler: 1.0            # 1.0 lot XAUUSD = 10 MGC = 1 GC
    setup_allowlist: ["*"]      # copy every source trade
```

4. Start NinjaTrader first, then click **Start Engine**. The follower appears with a `NINJA`
   badge and under **Futures Followers** in the Config tab.

Sizing, small-leg handling, tag filtering and known limits are in
[docs/NINJATRADER_SETUP.md](docs/NINJATRADER_SETUP.md). **This follower has only been tested
against the NinjaTrader simulator.**

## MT4 accounts

1. Copy `mt4_ea/PropCopy_Bridge.mq4` into the MT4 terminal's `MQL4/Experts` folder and compile it.
2. In MT4, enable *Allow DLL imports* and *Allow live trading*.
3. Attach the EA to any chart. Defaults: port `15555`, accepts connections from `127.0.0.1` only.
   Use a different port for each MT4 terminal.
4. In `accounts.yaml`, set `platform: mt4` and the matching `mt4_port`.

## Command-line options

| Flag | Meaning |
|------|---------|
| `--port N` | Dashboard port (default `8181`) |
| `--browser` | Open in the browser instead of a native window |
| `--headless` | No dashboard; run the copier from `config.yaml` only |

## Tests and building an executable

```powershell
pip install -r requirements-dev.txt
python -m pytest tests

build.bat                # produces dist\PropCopy.exe (PyInstaller)
iscc installer.iss       # optional: Inno Setup installer into installer_output\
```

## Security notes

- The dashboard binds to `127.0.0.1` only and has **no login**. Don't expose it with a reverse
  proxy, port-forward, or `0.0.0.0` binding.
- Credentials are stored unencrypted. Protect the machine, and use investor passwords where possible.
- Data files (`data/`) hold trade history and mappings between source and follower tickets. Don't
  delete them while positions are open, or closes may not propagate.

## Troubleshooting

- **Account fails to log in:** check the `server` name matches the terminal's server list exactly,
  and that the terminal at `mt5_path` has *Allow algorithmic trading* on.
- **Nothing is copied:** confirm the source account actually trades from the terminal in
  `mt5_path`, that the engine is running, and read the Logs tab or `data/propcopy.log`.
- **A follower is paused:** a paused follower skips new copies. Use the toggle on the Status tab.
- **NinjaTrader follower shows disconnected:** NinjaTrader must be open and connected, with
  *AT Interface* enabled and the port matching `nt_server_port`. Start NinjaTrader before the
  engine. See [docs/NINJATRADER_SETUP.md](docs/NINJATRADER_SETUP.md).
- **Volume differs from expected:** it is rounded to the follower symbol's lot step and clamped
  to its min/max volume (futures: rounded to whole contracts).

## FAQ

**What is PropCopy?**
An open-source local trade copier. It reads the positions on a source MetaTrader account and
replicates opens, closes, and SL/TP changes onto follower accounts.

**Does it work with MT4 and MT5?**
Yes. MT5 through the official `MetaTrader5` Python package, MT4 through the bundled
`PropCopy_Bridge.mq4` Expert Advisor. Windows only.

**Can it copy MT5 trades to NinjaTrader or futures accounts?**
Yes, for gold: XAUUSD on the source becomes MGC or GC futures on a NinjaTrader 8 account. It is
simulator-tested only. See [docs/NINJATRADER_SETUP.md](docs/NINJATRADER_SETUP.md).

**Is it a cloud copier? Does it see my passwords?**
No. It runs entirely on your own machine or VPS. Credentials are stored locally in plain text
in git-ignored YAML files.

**Can I copy to accounts at different brokers?**
Yes. Each account uses its own terminal installation, and `symbol_map` translates instrument
names that differ between brokers (for example `GER40.cash` to `DAX40`).

**Is copy trading allowed by my prop firm?**
Many firms restrict or ban it. Check your firm's rules first. See [DISCLAIMER.md](DISCLAIMER.md).

**Is it free?**
Yes, under the [MIT License](LICENSE).

## Contributing

Issues and pull requests are welcome. Please never include account numbers, passwords, server
names, or logs containing them.

## License

[MIT](LICENSE). See also [DISCLAIMER.md](DISCLAIMER.md).
