# PropCopy

A fast, self-hosted trade copier for MetaTrader. It watches one **source** account and replicates
its trades to any number of **follower** accounts, with per-follower sizing, execution tuning,
and a local web dashboard with trade analytics.

Everything runs on your own machine or VPS. Your broker credentials never leave it.

> **Warning: trading is risky, and copy trading may violate your prop firm's rules.**
> Read [DISCLAIMER.md](DISCLAIMER.md) before connecting any real or funded account.
> Test on demo accounts first. The software is provided as-is, with no warranty ([MIT](LICENSE)).

## Features

- Source -> N followers, each follower running in its own process for isolation
- Copies opens, closes, and SL/TP modifications (about 10 ms polling by default)
- Per-follower **size scaler**
- Per-account **symbol mapping** for brokers that name instruments differently
- Per-follower execution options: price offset (better entry or slippage guard), volume jitter,
  SL/TP offset and multiplier, invert (flip direction), profit-target auto-close
- MT5 (via the `MetaTrader5` Python package) and MT4 (via a bundled EA bridge)
- Local dashboard (native window or browser): live status over WebSocket, account manager,
  trade history
- Analytics: MFE/MAE, exit efficiency, entry/exit timing, SL/TP hit rate, streak and tilt
  detection, session heatmap, copy latency and slippage
- Optional setup tagging for trades

## Requirements

- **Windows** (the `MetaTrader5` Python package only works on Windows)
- **Python 3.10 or newer**
- One MetaTrader 5 terminal installation **per account** (each in its own folder, so each
  terminal can stay logged in to its own account)
- In every terminal: *Tools > Options > Expert Advisors > Allow algorithmic trading*

## Quick start

```powershell
git clone <this-repo-url> propcopy
cd propcopy
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt

copy config.example.yaml config.yaml
copy accounts.example.yaml accounts.yaml
# edit both files, or use the dashboard to add accounts

python main.py
```

The dashboard opens in a native window. To use your browser instead:

```powershell
python main.py --browser        # http://localhost:8181
```

### Command-line options

| Flag | Meaning |
|------|---------|
| `--port N` | Dashboard port (default `8181`) |
| `--browser` | Open in the browser instead of a native window |
| `--headless` | No dashboard; run the copier from `config.yaml` only |

## Configuration

Two YAML files live next to `main.py`. **Both contain credentials in plain text and are
git-ignored. Never commit or share them.**

**`accounts.yaml`** is your account book. Each entry has a `tag` (a label you choose), the login
`account`, `password`, broker `server`, the path to that account's terminal folder `mt5_path`,
and `platform` (`mt5` or `mt4`).

**`config.yaml`** picks the source and followers and holds settings. Minimal example:

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

**Symbol mapping.** If a follower's broker spells an instrument differently from the source's,
add a `symbol_map` to that account in `accounts.yaml` (source name on the left):

```yaml
- tag: My Account
  # ...
  symbol_map:
    GER40.cash: DAX40
    US100.cash: NAS100
```

Unmapped symbols are copied under their original name. `symbol_map` is copied into `config.yaml`
when you save the Config tab, so **after editing `accounts.yaml` by hand, press Save in the Config
tab** (then start the engine).

(The `master` / `slaves` key names are the file format; the UI calls them Source / Follower.)

Per-follower options:

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

### MT4 followers or source

1. Copy `mt4_ea/PropCopy_Bridge.mq4` into the MT4 terminal's `MQL4/Experts` folder and compile it.
2. In MT4, enable *Allow DLL imports* and *Allow live trading*.
3. Attach the EA to any chart. Defaults: port `15555`, accepts connections from `127.0.0.1` only.
   Use a different port for each MT4 terminal.
4. In `accounts.yaml`, set `platform: mt4` and the matching `mt4_port`.

## Tests

```powershell
pip install -r requirements-dev.txt
python -m pytest tests
```

## Build a standalone executable (optional)

```powershell
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
  `mt5_path`, and read `data/propcopy.log`.
- **Volume differs from expected:** it is rounded to the follower symbol's lot step and clamped
  to its min/max volume.

## Contributing

Issues and pull requests are welcome. Please never include account numbers, passwords, server
names, or logs containing them.

## License

[MIT](LICENSE). See also [DISCLAIMER.md](DISCLAIMER.md).
