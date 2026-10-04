"""MT4 Bridge — TCP client that mirrors the MetaTrader5 Python module interface.

Connects to PropCopy_Bridge.mq4 EA running inside an MT4 terminal.
Protocol: 4-byte big-endian length prefix + UTF-8 JSON over TCP.
"""

import json
import logging
import socket
import struct
import time
from collections import namedtuple

import numpy as np

logger = logging.getLogger("mt4_bridge")

# ── Constants (match MT5 module values) ──

ORDER_TYPE_BUY = 0
ORDER_TYPE_SELL = 1
ORDER_TYPE_BUY_LIMIT = 2
ORDER_TYPE_SELL_LIMIT = 3
ORDER_TYPE_BUY_STOP = 4
ORDER_TYPE_SELL_STOP = 5

TRADE_ACTION_DEAL = 1
TRADE_ACTION_SLTP = 6

TRADE_RETCODE_DONE = 10009
TRADE_RETCODE_ERROR = 10006

ORDER_TIME_GTC = 0
ORDER_FILLING_IOC = 1

SYMBOL_TRADE_MODE_FULL = 4
TIMEFRAME_M1 = 1

# ── Named tuples (mirror MT5 return types) ──

AccountInfo = namedtuple("AccountInfo", ["balance", "equity", "margin", "margin_free"])
PositionInfo = namedtuple(
    "PositionInfo",
    ["ticket", "symbol", "type", "volume", "price_open", "sl", "tp",
     "profit", "time", "magic", "comment"],
)
SymbolInfo = namedtuple(
    "SymbolInfo",
    ["name", "volume_step", "volume_min", "volume_max", "trade_mode", "point", "visible"],
)
TickInfo = namedtuple("TickInfo", ["bid", "ask"])
OrderResult = namedtuple("OrderResult", ["retcode", "order", "price", "comment"])
DealInfo = namedtuple("DealInfo", ["price", "time", "profit"])


class MT4Bridge:
    """TCP client that speaks to PropCopy_Bridge.mq4 EA.

    Exposes the same interface as the MetaTrader5 Python module so callers
    can use it as a drop-in replacement.
    """

    def __init__(self, config: dict):
        self.host = config.get("mt4_host", "127.0.0.1")
        self.port = int(config.get("mt4_port", 15555))
        self.timeout = float(config.get("mt4_timeout", 5.0))
        self._sock: socket.socket | None = None
        self._last_error = (0, "")
        self._max_reconnect = 3
        self._reconnect_delay = 0.5

    # ── Connection management ──

    def initialize(self, **kwargs) -> bool:
        """Connect to the MT4 bridge EA. Returns True on success."""
        try:
            self._connect()
            # Verify connection with a ping (account_info)
            info = self.account_info()
            if info is None:
                self._last_error = (-1, "Connected but account_info failed")
                return False
            logger.info(f"MT4 bridge connected on {self.host}:{self.port}")
            return True
        except Exception as e:
            self._last_error = (-1, str(e))
            logger.error(f"MT4 bridge init failed: {e}")
            return False

    def shutdown(self) -> None:
        """Close the TCP connection."""
        if self._sock:
            try:
                self._sock.close()
            except Exception:
                pass
            self._sock = None

    def last_error(self) -> tuple:
        return self._last_error

    def _connect(self) -> None:
        """Establish TCP connection."""
        self.shutdown()
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(self.timeout)
        sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        sock.connect((self.host, self.port))
        self._sock = sock

    def _send_recv(self, request: dict) -> dict | None:
        """Send a JSON request, receive JSON response. Reconnects on failure."""
        for attempt in range(self._max_reconnect + 1):
            try:
                if self._sock is None:
                    self._connect()
                # Send: 4-byte length + JSON
                payload = json.dumps(request).encode("utf-8")
                header = struct.pack(">I", len(payload))
                self._sock.sendall(header + payload)
                # Recv: 4-byte length + JSON
                resp_header = self._recv_exact(4)
                resp_len = struct.unpack(">I", resp_header)[0]
                if resp_len > 10 * 1024 * 1024:  # 10MB sanity limit
                    raise ValueError(f"Response too large: {resp_len}")
                resp_data = self._recv_exact(resp_len)
                response = json.loads(resp_data.decode("utf-8"))
                if not response.get("ok"):
                    self._last_error = (-2, response.get("error", "Unknown error"))
                    return None
                return response.get("data")
            except (ConnectionError, OSError, socket.timeout, struct.error) as e:
                self._sock = None
                if attempt < self._max_reconnect:
                    delay = self._reconnect_delay * (2 ** attempt)
                    logger.warning(f"MT4 bridge reconnect attempt {attempt + 1}: {e}")
                    time.sleep(delay)
                else:
                    self._last_error = (-3, f"Connection lost after retries: {e}")
                    return None
        return None

    def _recv_exact(self, n: int) -> bytes:
        """Receive exactly n bytes."""
        buf = bytearray()
        while len(buf) < n:
            chunk = self._sock.recv(n - len(buf))
            if not chunk:
                raise ConnectionError("Connection closed by MT4 EA")
            buf.extend(chunk)
        return bytes(buf)

    # ── Account ──

    def account_info(self) -> AccountInfo | None:
        data = self._send_recv({"cmd": "ACCOUNT_INFO"})
        if data is None:
            return None
        return AccountInfo(
            balance=data.get("balance", 0.0),
            equity=data.get("equity", 0.0),
            margin=data.get("margin", 0.0),
            margin_free=data.get("margin_free", 0.0),
        )

    # ── Positions ──

    def positions_get(self, symbol: str = None) -> tuple | None:
        req = {"cmd": "POSITIONS"}
        if symbol:
            req["symbol"] = symbol
        data = self._send_recv(req)
        if data is None:
            return None
        positions = []
        for p in data:
            positions.append(PositionInfo(
                ticket=int(p.get("ticket", 0)),
                symbol=p.get("symbol", ""),
                type=int(p.get("type", 0)),
                volume=float(p.get("volume", 0)),
                price_open=float(p.get("price_open", 0)),
                sl=float(p.get("sl", 0)),
                tp=float(p.get("tp", 0)),
                profit=float(p.get("profit", 0)),
                time=int(p.get("time", 0)),
                magic=int(p.get("magic", 0)),
                comment=p.get("comment", ""),
            ))
        return tuple(positions)

    # ── Symbols ──

    def symbols_get(self) -> tuple | None:
        data = self._send_recv({"cmd": "SYMBOLS"})
        if data is None:
            return None
        symbols = []
        for s in data:
            symbols.append(SymbolInfo(
                name=s.get("name", ""),
                volume_step=float(s.get("volume_step", 0.01)),
                volume_min=float(s.get("volume_min", 0.01)),
                volume_max=float(s.get("volume_max", 100.0)),
                trade_mode=int(s.get("trade_mode", SYMBOL_TRADE_MODE_FULL)),
                point=float(s.get("point", 0.00001)),
                visible=True,
            ))
        return tuple(symbols)

    def symbol_info(self, symbol: str) -> SymbolInfo | None:
        data = self._send_recv({"cmd": "SYMBOL_INFO", "symbol": symbol})
        if data is None:
            return None
        return SymbolInfo(
            name=data.get("name", symbol),
            volume_step=float(data.get("volume_step", 0.01)),
            volume_min=float(data.get("volume_min", 0.01)),
            volume_max=float(data.get("volume_max", 100.0)),
            trade_mode=int(data.get("trade_mode", SYMBOL_TRADE_MODE_FULL)),
            point=float(data.get("point", 0.00001)),
            visible=True,
        )

    def symbol_info_tick(self, symbol: str) -> TickInfo | None:
        data = self._send_recv({"cmd": "TICK", "symbol": symbol})
        if data is None:
            return None
        return TickInfo(
            bid=float(data.get("bid", 0)),
            ask=float(data.get("ask", 0)),
        )

    def symbol_select(self, symbol: str, enable: bool = True) -> bool:
        # MT4 doesn't need explicit symbol selection in the same way
        return True

    # ── Trading ──

    def order_send(self, request: dict) -> OrderResult | None:
        """Translate MT5-style order request to MT4 command.

        Key translations:
        - TRADE_ACTION_DEAL + position field → ORDER_CLOSE (MT4 uses OrderClose)
        - TRADE_ACTION_SLTP → ORDER_MODIFY
        - TRADE_ACTION_DEAL (no position) → ORDER_SEND (new trade)
        """
        action = request.get("action", TRADE_ACTION_DEAL)

        if action == TRADE_ACTION_SLTP:
            # SL/TP modification
            cmd_data = {
                "cmd": "ORDER_MODIFY",
                "ticket": int(request.get("position", 0)),
                "sl": float(request.get("sl", 0)),
                "tp": float(request.get("tp", 0)),
            }
        elif action == TRADE_ACTION_DEAL and request.get("position"):
            # Close existing position (MT4 uses OrderClose, not counter-orders)
            cmd_data = {
                "cmd": "ORDER_CLOSE",
                "ticket": int(request["position"]),
                "volume": float(request.get("volume", 0)),
                "price": float(request.get("price", 0)),
                "slippage": int(request.get("deviation", 20)),
            }
        else:
            # New trade
            cmd_data = {
                "cmd": "ORDER_SEND",
                "symbol": request.get("symbol", ""),
                "type": int(request.get("type", 0)),
                "volume": float(request.get("volume", 0)),
                "price": float(request.get("price", 0)),
                "slippage": int(request.get("deviation", 20)),
                "sl": float(request.get("sl", 0)),
                "tp": float(request.get("tp", 0)),
                "magic": int(request.get("magic", 0)),
                "comment": request.get("comment", ""),
            }

        data = self._send_recv(cmd_data)
        if data is None:
            error_msg = self._last_error[1] if self._last_error else "Unknown"
            return OrderResult(retcode=TRADE_RETCODE_ERROR, order=0, price=0.0, comment=error_msg)

        return OrderResult(
            retcode=int(data.get("retcode", TRADE_RETCODE_DONE)),
            order=int(data.get("order", 0)),
            price=float(data.get("price", 0)),
            comment=data.get("comment", ""),
        )

    # ── History ──

    def history_deals_get(self, position: int = 0, **kwargs) -> tuple | None:
        data = self._send_recv({"cmd": "HISTORY_DEALS", "position": position})
        if data is None:
            return None
        deals = []
        for d in data:
            deals.append(DealInfo(
                price=float(d.get("price", 0)),
                time=int(d.get("time", 0)),
                profit=float(d.get("profit", 0)),
            ))
        return tuple(deals) if deals else None

    # ── Rates (for analytics) ──

    def copy_rates_range(self, symbol: str, timeframe: int, date_from, date_to) -> np.ndarray | None:
        """Fetch OHLCV bars. date_from/date_to are datetime objects."""
        # Convert datetime to timestamps
        from_ts = int(date_from.timestamp()) if hasattr(date_from, 'timestamp') else int(date_from)
        to_ts = int(date_to.timestamp()) if hasattr(date_to, 'timestamp') else int(date_to)

        data = self._send_recv({
            "cmd": "RATES",
            "symbol": symbol,
            "timeframe": timeframe,
            "from": from_ts,
            "to": to_ts,
        })
        if data is None or not data:
            return None

        # Build numpy structured array matching MT5 format:
        # time, open, high, low, close, tick_volume, spread, real_volume
        dtype = np.dtype([
            ('time', 'i8'), ('open', 'f8'), ('high', 'f8'), ('low', 'f8'),
            ('close', 'f8'), ('tick_volume', 'i8'), ('spread', 'i4'), ('real_volume', 'i8'),
        ])
        rows = []
        for bar in data:
            rows.append((
                int(bar.get("time", 0)),
                float(bar.get("open", 0)),
                float(bar.get("high", 0)),
                float(bar.get("low", 0)),
                float(bar.get("close", 0)),
                int(bar.get("tick_volume", 0)),
                int(bar.get("spread", 0)),
                0,
            ))
        return np.array(rows, dtype=dtype)
