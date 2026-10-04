# Disclaimer

**Read this before connecting PropCopy to any account, especially a funded or live account.**

## Not financial advice
PropCopy is a software tool that replicates orders between trading accounts. It does not
provide investment, financial, legal, or tax advice, and nothing in this repository is a
recommendation to buy, sell, or hold any instrument.

## Trading risk
Trading leveraged products (forex, CFDs, futures, metals) carries a high risk of loss, including
the loss of more than your initial deposit on some products. A trade copier **multiplies**
that risk: one mistake, one bad signal, or one software fault is reproduced on every follower
account at once.

## No warranty, no liability
The software is provided "as is" under the [MIT License](LICENSE), without warranty of any kind.
The authors and contributors are not liable for any loss, including but not limited to:

- missed, delayed, duplicated, partially filled, or wrongly sized copies;
- slippage, requotes, rejected orders, or differences in fills between source and followers;
- bugs, crashes, lost connections, terminal restarts, or corrupted state files;
- positions left open (or closed) unintentionally, including after a crash;
- breached drawdown or consistency limits, failed challenges, or terminated accounts.

Test on demo accounts first and verify behaviour yourself. Keep the software supervised while
real money is at stake.

## Prop firm rules
Many proprietary trading firms restrict or prohibit copy trading, trade copiers, hedging across
accounts, or mirroring trades between accounts you do not control. Rules differ per firm and
change over time. **You are solely responsible for reading and complying with your firm's
terms.** Using this tool may violate them and may result in forfeited profits or account
termination. Features such as `invert` (opposite-direction copies) may be specifically
prohibited by your firm.

## Credentials are stored locally in plain text
Account numbers, passwords, and server names are saved unencrypted in `config.yaml` and
`accounts.yaml` on your machine. Anyone with access to those files can log in to your trading
accounts.

- Never commit those files, share them, or paste them in issues. They are listed in `.gitignore`.
- Use an investor (read-only) password for the source account, where your broker allows it.
- Keep the dashboard bound to `127.0.0.1` (the default). It can edit credentials and start or
  stop copying, and has **no authentication**. Do not expose it to a network.

## Affiliation and trademarks
PropCopy is an independent project. It is not affiliated with, endorsed by, or sponsored by
MetaQuotes Ltd., MetaTrader, or any broker or prop firm. MetaTrader, MT4, and MT5 are
trademarks of their respective owners. Firm names that appear in documentation are examples
only.

## Use at your own risk
By using this software you accept all of the above.
