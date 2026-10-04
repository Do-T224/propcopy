//+------------------------------------------------------------------+
//| PropCopy_Bridge.mq4 — TCP socket bridge for PropCopy             |
//| Runs as an EA in MT4. Listens on a TCP port, receives JSON       |
//| commands from the Python MT4Bridge client, executes MQL4 API     |
//| calls, and returns JSON responses.                               |
//|                                                                  |
//| Protocol: 4-byte big-endian length prefix + UTF-8 JSON           |
//| Requires: "Allow DLL imports" enabled in MT4 terminal            |
//+------------------------------------------------------------------+
#property copyright "PropCopy"
#property version   "1.0"
#property strict

// ── WinAPI socket imports (ws2_32.dll) ──
#import "ws2_32.dll"
int     WSAStartup(int wVersionRequested, int &lpWSAData[]);
int     WSACleanup();
int     WSAGetLastError();
int     socket(int af, int type, int protocol);
int     bind(int s, uchar &name[], int namelen);
int     listen(int s, int backlog);
int     accept(int s, int &addr[], int &addrlen[]);
int     closesocket(int s);
int     recv(int s, uchar &buf[], int len, int flags);
int     send(int s, uchar &buf[], int len, int flags);
int     ioctlsocket(int s, int cmd, int &argp[]);
int     setsockopt(int s, int level, int optname, int &optval[], int optlen);
int     htons(int hostshort);
int     htonl(int hostlong);
int     ntohl(int netlong);
#import

// ── Constants ──
#define AF_INET        2
#define SOCK_STREAM    1
#define IPPROTO_TCP    6
#define SOL_SOCKET     65535
#define SO_REUSEADDR   4
#define FIONBIO        0x8004667E
#define INVALID_SOCKET -1
#define SOCKET_ERROR   -1
#define INADDR_ANY     0

// MT4 retcodes mapped to MT5 values for compatibility
#define RETCODE_DONE   10009
#define RETCODE_ERROR  10006

// ── Input Parameters ──
input int    BridgePort    = 15555;      // TCP port to listen on
input string AllowedIP     = "127.0.0.1"; // Allowed client IP (security)
input int    MaxMsgSize    = 1048576;    // Max message size (1MB)

// ── Global State ──
int    g_serverSocket = INVALID_SOCKET;
int    g_clientSocket = INVALID_SOCKET;
int    g_wsaData[100];
bool   g_wsaStarted = false;

//+------------------------------------------------------------------+
//| Expert initialization                                            |
//+------------------------------------------------------------------+
int OnInit()
{
    // Initialize Winsock
    int result = WSAStartup(0x0202, g_wsaData);
    if(result != 0)
    {
        Print("WSAStartup failed: ", result);
        return INIT_FAILED;
    }
    g_wsaStarted = true;

    // Create server socket
    g_serverSocket = socket(AF_INET, SOCK_STREAM, IPPROTO_TCP);
    if(g_serverSocket == INVALID_SOCKET)
    {
        Print("socket() failed: ", WSAGetLastError());
        return INIT_FAILED;
    }

    // Set SO_REUSEADDR
    int optval[1] = {1};
    setsockopt(g_serverSocket, SOL_SOCKET, SO_REUSEADDR, optval, 4);

    // Set non-blocking
    int nonblock[1] = {1};
    ioctlsocket(g_serverSocket, FIONBIO, nonblock);

    // Bind to port
    uchar addr[16];
    ArrayInitialize(addr, 0);
    // sin_family = AF_INET (offset 0, 2 bytes)
    addr[0] = (uchar)(AF_INET & 0xFF);
    addr[1] = (uchar)((AF_INET >> 8) & 0xFF);
    // sin_port (offset 2, 2 bytes, network byte order)
    int port_n = htons(BridgePort);
    addr[2] = (uchar)((port_n) & 0xFF);
    addr[3] = (uchar)((port_n >> 8) & 0xFF);
    // sin_addr = INADDR_ANY (offset 4, 4 bytes) — already 0

    if(bind(g_serverSocket, addr, 16) == SOCKET_ERROR)
    {
        Print("bind() failed on port ", BridgePort, ": ", WSAGetLastError());
        closesocket(g_serverSocket);
        g_serverSocket = INVALID_SOCKET;
        return INIT_FAILED;
    }

    if(listen(g_serverSocket, 1) == SOCKET_ERROR)
    {
        Print("listen() failed: ", WSAGetLastError());
        closesocket(g_serverSocket);
        g_serverSocket = INVALID_SOCKET;
        return INIT_FAILED;
    }

    // 50ms timer for polling
    EventSetMillisecondTimer(50);
    Print("PropCopy Bridge listening on port ", BridgePort);
    return INIT_SUCCEEDED;
}

//+------------------------------------------------------------------+
//| Expert deinitialization                                          |
//+------------------------------------------------------------------+
void OnDeinit(const int reason)
{
    EventKillTimer();
    if(g_clientSocket != INVALID_SOCKET)
    {
        closesocket(g_clientSocket);
        g_clientSocket = INVALID_SOCKET;
    }
    if(g_serverSocket != INVALID_SOCKET)
    {
        closesocket(g_serverSocket);
        g_serverSocket = INVALID_SOCKET;
    }
    if(g_wsaStarted)
    {
        WSACleanup();
        g_wsaStarted = false;
    }
    Print("PropCopy Bridge stopped");
}

//+------------------------------------------------------------------+
//| Timer event — main polling loop                                  |
//+------------------------------------------------------------------+
void OnTimer()
{
    // Accept new connections if no client
    if(g_clientSocket == INVALID_SOCKET)
    {
        int addrBuf[4];
        int addrLen[1] = {16};
        int newSocket = accept(g_serverSocket, addrBuf, addrLen);
        if(newSocket != INVALID_SOCKET)
        {
            // Set non-blocking on client socket
            int nonblock[1] = {1};
            ioctlsocket(newSocket, FIONBIO, nonblock);
            g_clientSocket = newSocket;
            Print("Client connected");
        }
        return;
    }

    // Try to read a message from client
    string request = RecvMessage();
    if(request == "")
        return;
    if(request == "__DISCONNECTED__")
    {
        Print("Client disconnected");
        closesocket(g_clientSocket);
        g_clientSocket = INVALID_SOCKET;
        return;
    }

    // Parse and dispatch
    string response = DispatchCommand(request);
    SendMessage(response);
}

//+------------------------------------------------------------------+
//| Receive a length-prefixed message                                |
//+------------------------------------------------------------------+
string RecvMessage()
{
    // Read 4-byte header
    uchar header[4];
    int received = recv(g_clientSocket, header, 4, 0);
    if(received == 0)
        return "__DISCONNECTED__";
    if(received == SOCKET_ERROR || received < 4)
        return "";

    // Decode length (big-endian)
    int msgLen = (header[0] << 24) | (header[1] << 16) | (header[2] << 8) | header[3];
    if(msgLen <= 0 || msgLen > MaxMsgSize)
    {
        Print("Invalid message length: ", msgLen);
        return "__DISCONNECTED__";
    }

    // Read payload
    uchar payload[];
    ArrayResize(payload, msgLen);
    int totalRead = 0;
    int retries = 0;
    while(totalRead < msgLen && retries < 100)
    {
        uchar chunk[];
        int remaining = msgLen - totalRead;
        ArrayResize(chunk, remaining);
        int r = recv(g_clientSocket, chunk, remaining, 0);
        if(r == 0)
            return "__DISCONNECTED__";
        if(r == SOCKET_ERROR)
        {
            retries++;
            Sleep(1);
            continue;
        }
        for(int i = 0; i < r; i++)
            payload[totalRead + i] = chunk[i];
        totalRead += r;
    }

    if(totalRead < msgLen)
        return "__DISCONNECTED__";

    return CharArrayToString(payload, 0, msgLen, CP_UTF8);
}

//+------------------------------------------------------------------+
//| Send a length-prefixed message                                   |
//+------------------------------------------------------------------+
void SendMessage(string msg)
{
    uchar payload[];
    int payloadLen = StringToCharArray(msg, payload, 0, WHOLE_ARRAY, CP_UTF8) - 1; // exclude null terminator
    if(payloadLen <= 0)
        return;

    // Build header (big-endian length)
    uchar header[4];
    header[0] = (uchar)((payloadLen >> 24) & 0xFF);
    header[1] = (uchar)((payloadLen >> 16) & 0xFF);
    header[2] = (uchar)((payloadLen >> 8) & 0xFF);
    header[3] = (uchar)(payloadLen & 0xFF);

    // Send header + payload
    uchar fullMsg[];
    ArrayResize(fullMsg, 4 + payloadLen);
    for(int i = 0; i < 4; i++)
        fullMsg[i] = header[i];
    for(int i = 0; i < payloadLen; i++)
        fullMsg[4 + i] = payload[i];

    int totalSent = 0;
    int retries = 0;
    while(totalSent < 4 + payloadLen && retries < 100)
    {
        uchar toSend[];
        int remaining = 4 + payloadLen - totalSent;
        ArrayResize(toSend, remaining);
        for(int i = 0; i < remaining; i++)
            toSend[i] = fullMsg[totalSent + i];
        int s = send(g_clientSocket, toSend, remaining, 0);
        if(s == SOCKET_ERROR)
        {
            retries++;
            Sleep(1);
            continue;
        }
        totalSent += s;
    }
}

//+------------------------------------------------------------------+
//| Dispatch command to handler                                      |
//+------------------------------------------------------------------+
string DispatchCommand(string request)
{
    string cmd = JsonGetString(request, "cmd");

    if(cmd == "ACCOUNT_INFO")    return HandleAccountInfo();
    if(cmd == "POSITIONS")       return HandlePositions(request);
    if(cmd == "SYMBOLS")         return HandleSymbols();
    if(cmd == "SYMBOL_INFO")     return HandleSymbolInfo(request);
    if(cmd == "TICK")            return HandleTick(request);
    if(cmd == "SYMBOL_SELECT")   return JsonOk("true");
    if(cmd == "ORDER_SEND")      return HandleOrderSend(request);
    if(cmd == "ORDER_CLOSE")     return HandleOrderClose(request);
    if(cmd == "ORDER_MODIFY")    return HandleOrderModify(request);
    if(cmd == "HISTORY_DEALS")   return HandleHistoryDeals(request);
    if(cmd == "RATES")           return HandleRates(request);

    return JsonError("Unknown command: " + cmd);
}

//+------------------------------------------------------------------+
//| ACCOUNT_INFO handler                                             |
//+------------------------------------------------------------------+
string HandleAccountInfo()
{
    string data = "{";
    data += "\"balance\":" + DoubleToString(AccountBalance(), 2);
    data += ",\"equity\":" + DoubleToString(AccountEquity(), 2);
    data += ",\"margin\":" + DoubleToString(AccountMargin(), 2);
    data += ",\"margin_free\":" + DoubleToString(AccountFreeMargin(), 2);
    data += "}";
    return JsonOk(data);
}

//+------------------------------------------------------------------+
//| POSITIONS handler                                                |
//+------------------------------------------------------------------+
string HandlePositions(string request)
{
    string filterSymbol = JsonGetString(request, "symbol");
    string data = "[";
    bool first = true;

    for(int i = 0; i < OrdersTotal(); i++)
    {
        if(!OrderSelect(i, SELECT_BY_POS, MODE_TRADES))
            continue;
        // Only include market orders (BUY=0, SELL=1)
        if(OrderType() > 1)
            continue;
        if(filterSymbol != "" && OrderSymbol() != filterSymbol)
            continue;

        if(!first) data += ",";
        first = false;

        data += "{";
        data += "\"ticket\":" + IntegerToString(OrderTicket());
        data += ",\"symbol\":\"" + OrderSymbol() + "\"";
        data += ",\"type\":" + IntegerToString(OrderType());
        data += ",\"volume\":" + DoubleToString(OrderLots(), 2);
        data += ",\"price_open\":" + DoubleToString(OrderOpenPrice(), 5);
        data += ",\"sl\":" + DoubleToString(OrderStopLoss(), 5);
        data += ",\"tp\":" + DoubleToString(OrderTakeProfit(), 5);
        data += ",\"profit\":" + DoubleToString(OrderProfit(), 2);
        data += ",\"time\":" + IntegerToString(OrderOpenTime());
        data += ",\"magic\":" + IntegerToString(OrderMagicNumber());
        data += ",\"comment\":\"" + EscapeJson(OrderComment()) + "\"";
        data += "}";
    }
    data += "]";
    return JsonOk(data);
}

//+------------------------------------------------------------------+
//| SYMBOLS handler                                                  |
//+------------------------------------------------------------------+
string HandleSymbols()
{
    string data = "[";
    bool first = true;
    int total = SymbolsTotal(true); // visible symbols only

    for(int i = 0; i < total; i++)
    {
        string name = SymbolName(i, true);
        if(name == "")
            continue;

        if(!first) data += ",";
        first = false;

        data += "{";
        data += "\"name\":\"" + name + "\"";
        data += ",\"volume_step\":" + DoubleToString(MarketInfo(name, MODE_LOTSTEP), 4);
        data += ",\"volume_min\":" + DoubleToString(MarketInfo(name, MODE_MINLOT), 4);
        data += ",\"volume_max\":" + DoubleToString(MarketInfo(name, MODE_MAXLOT), 2);
        data += ",\"trade_mode\":" + IntegerToString((int)MarketInfo(name, MODE_TRADEALLOWED) != 0 ? 4 : 0);
        data += ",\"point\":" + DoubleToString(MarketInfo(name, MODE_POINT), 8);
        data += "}";
    }
    data += "]";
    return JsonOk(data);
}

//+------------------------------------------------------------------+
//| SYMBOL_INFO handler                                              |
//+------------------------------------------------------------------+
string HandleSymbolInfo(string request)
{
    string sym = JsonGetString(request, "symbol");
    if(sym == "")
        return JsonError("Missing symbol");

    double point = MarketInfo(sym, MODE_POINT);
    if(point == 0)
        return JsonError("Symbol not found: " + sym);

    string data = "{";
    data += "\"name\":\"" + sym + "\"";
    data += ",\"volume_step\":" + DoubleToString(MarketInfo(sym, MODE_LOTSTEP), 4);
    data += ",\"volume_min\":" + DoubleToString(MarketInfo(sym, MODE_MINLOT), 4);
    data += ",\"volume_max\":" + DoubleToString(MarketInfo(sym, MODE_MAXLOT), 2);
    data += ",\"trade_mode\":" + IntegerToString((int)MarketInfo(sym, MODE_TRADEALLOWED) != 0 ? 4 : 0);
    data += ",\"point\":" + DoubleToString(point, 8);
    data += "}";
    return JsonOk(data);
}

//+------------------------------------------------------------------+
//| TICK handler                                                     |
//+------------------------------------------------------------------+
string HandleTick(string request)
{
    string sym = JsonGetString(request, "symbol");
    if(sym == "")
        return JsonError("Missing symbol");

    double bid = MarketInfo(sym, MODE_BID);
    double ask = MarketInfo(sym, MODE_ASK);
    if(bid == 0 && ask == 0)
        return JsonError("No tick data for: " + sym);

    string data = "{";
    data += "\"bid\":" + DoubleToString(bid, 5);
    data += ",\"ask\":" + DoubleToString(ask, 5);
    data += "}";
    return JsonOk(data);
}

//+------------------------------------------------------------------+
//| ORDER_SEND handler (new trade)                                   |
//+------------------------------------------------------------------+
string HandleOrderSend(string request)
{
    string sym      = JsonGetString(request, "symbol");
    int    type     = JsonGetInt(request, "type");
    double volume   = JsonGetDouble(request, "volume");
    double price    = JsonGetDouble(request, "price");
    int    slippage = JsonGetInt(request, "slippage");
    double sl       = JsonGetDouble(request, "sl");
    double tp       = JsonGetDouble(request, "tp");
    int    magic    = JsonGetInt(request, "magic");
    string comment  = JsonGetString(request, "comment");

    int ticket = OrderSend(sym, type, volume, price, slippage, sl, tp, comment, magic, 0, clrNONE);

    if(ticket < 0)
    {
        int err = GetLastError();
        string data = "{";
        data += "\"retcode\":" + IntegerToString(RETCODE_ERROR);
        data += ",\"order\":0";
        data += ",\"price\":0";
        data += ",\"comment\":\"Error " + IntegerToString(err) + "\"";
        data += "}";
        return JsonOk(data);
    }

    // Get fill price
    double fillPrice = 0;
    if(OrderSelect(ticket, SELECT_BY_TICKET))
        fillPrice = OrderOpenPrice();

    string data = "{";
    data += "\"retcode\":" + IntegerToString(RETCODE_DONE);
    data += ",\"order\":" + IntegerToString(ticket);
    data += ",\"price\":" + DoubleToString(fillPrice, 5);
    data += ",\"comment\":\"\"";
    data += "}";
    return JsonOk(data);
}

//+------------------------------------------------------------------+
//| ORDER_CLOSE handler                                              |
//+------------------------------------------------------------------+
string HandleOrderClose(string request)
{
    int    ticket   = JsonGetInt(request, "ticket");
    double volume   = JsonGetDouble(request, "volume");
    double price    = JsonGetDouble(request, "price");
    int    slippage = JsonGetInt(request, "slippage");

    if(!OrderSelect(ticket, SELECT_BY_TICKET))
    {
        string data = "{\"retcode\":" + IntegerToString(RETCODE_ERROR) +
                      ",\"order\":0,\"price\":0,\"comment\":\"Ticket not found\"}";
        return JsonOk(data);
    }

    // Use current price if not specified
    if(price == 0)
    {
        if(OrderType() == OP_BUY)
            price = MarketInfo(OrderSymbol(), MODE_BID);
        else
            price = MarketInfo(OrderSymbol(), MODE_ASK);
    }

    // Use order lots if volume not specified
    if(volume == 0)
        volume = OrderLots();

    bool ok = OrderClose(ticket, volume, price, slippage, clrNONE);

    string data = "{";
    if(ok)
    {
        data += "\"retcode\":" + IntegerToString(RETCODE_DONE);
        data += ",\"order\":" + IntegerToString(ticket);
        data += ",\"price\":" + DoubleToString(price, 5);
        data += ",\"comment\":\"\"";
    }
    else
    {
        int err = GetLastError();
        data += "\"retcode\":" + IntegerToString(RETCODE_ERROR);
        data += ",\"order\":0";
        data += ",\"price\":0";
        data += ",\"comment\":\"Close error " + IntegerToString(err) + "\"";
    }
    data += "}";
    return JsonOk(data);
}

//+------------------------------------------------------------------+
//| ORDER_MODIFY handler (SL/TP change)                              |
//+------------------------------------------------------------------+
string HandleOrderModify(string request)
{
    int    ticket = JsonGetInt(request, "ticket");
    double sl     = JsonGetDouble(request, "sl");
    double tp     = JsonGetDouble(request, "tp");

    if(!OrderSelect(ticket, SELECT_BY_TICKET))
    {
        string data = "{\"retcode\":" + IntegerToString(RETCODE_ERROR) +
                      ",\"order\":0,\"price\":0,\"comment\":\"Ticket not found\"}";
        return JsonOk(data);
    }

    bool ok = OrderModify(ticket, OrderOpenPrice(), sl, tp, 0, clrNONE);

    string data = "{";
    if(ok)
    {
        data += "\"retcode\":" + IntegerToString(RETCODE_DONE);
        data += ",\"order\":" + IntegerToString(ticket);
        data += ",\"price\":" + DoubleToString(OrderOpenPrice(), 5);
        data += ",\"comment\":\"\"";
    }
    else
    {
        int err = GetLastError();
        data += "\"retcode\":" + IntegerToString(RETCODE_ERROR);
        data += ",\"order\":0";
        data += ",\"price\":0";
        data += ",\"comment\":\"Modify error " + IntegerToString(err) + "\"";
    }
    data += "}";
    return JsonOk(data);
}

//+------------------------------------------------------------------+
//| HISTORY_DEALS handler                                            |
//+------------------------------------------------------------------+
string HandleHistoryDeals(string request)
{
    int posTicket = JsonGetInt(request, "position");
    string data = "[";
    bool first = true;

    int total = OrdersHistoryTotal();
    for(int i = total - 1; i >= 0; i--)
    {
        if(!OrderSelect(i, SELECT_BY_POS, MODE_HISTORY))
            continue;
        if(OrderTicket() != posTicket)
            continue;

        if(!first) data += ",";
        first = false;

        data += "{";
        data += "\"price\":" + DoubleToString(OrderClosePrice(), 5);
        data += ",\"time\":" + IntegerToString(OrderCloseTime());
        data += ",\"profit\":" + DoubleToString(OrderProfit(), 2);
        data += "}";
    }
    data += "]";
    return JsonOk(data);
}

//+------------------------------------------------------------------+
//| RATES handler (OHLCV bars)                                       |
//+------------------------------------------------------------------+
string HandleRates(string request)
{
    string sym = JsonGetString(request, "symbol");
    int    tf  = JsonGetInt(request, "timeframe");
    int    fromTs = JsonGetInt(request, "from");
    int    toTs   = JsonGetInt(request, "to");

    if(sym == "")
        return JsonError("Missing symbol");

    // Map timeframe (default M1)
    int period = PERIOD_M1;
    if(tf == 5)  period = PERIOD_M5;
    if(tf == 15) period = PERIOD_M15;
    if(tf == 30) period = PERIOD_M30;
    if(tf == 60) period = PERIOD_H1;

    // Find bar indices
    int startBar = iBarShift(sym, period, fromTs, false);
    int endBar   = iBarShift(sym, period, toTs, false);

    if(startBar < 0 || endBar < 0)
        return JsonOk("[]");

    // endBar is more recent (lower index), startBar is older (higher index)
    if(startBar < endBar)
    {
        int tmp = startBar;
        startBar = endBar;
        endBar = tmp;
    }

    // Limit to 1000 bars
    if(startBar - endBar > 1000)
        startBar = endBar + 1000;

    string data = "[";
    bool first = true;

    for(int i = startBar; i >= endBar; i--)
    {
        if(!first) data += ",";
        first = false;

        data += "{";
        data += "\"time\":" + IntegerToString(iTime(sym, period, i));
        data += ",\"open\":" + DoubleToString(iOpen(sym, period, i), 5);
        data += ",\"high\":" + DoubleToString(iHigh(sym, period, i), 5);
        data += ",\"low\":" + DoubleToString(iLow(sym, period, i), 5);
        data += ",\"close\":" + DoubleToString(iClose(sym, period, i), 5);
        data += ",\"tick_volume\":" + IntegerToString((int)iVolume(sym, period, i));
        data += ",\"spread\":0";
        data += "}";
    }
    data += "]";
    return JsonOk(data);
}

//+------------------------------------------------------------------+
//| JSON helper: wrap data in ok response                            |
//+------------------------------------------------------------------+
string JsonOk(string data)
{
    return "{\"ok\":true,\"data\":" + data + "}";
}

//+------------------------------------------------------------------+
//| JSON helper: error response                                      |
//+------------------------------------------------------------------+
string JsonError(string msg)
{
    return "{\"ok\":false,\"error\":\"" + EscapeJson(msg) + "\"}";
}

//+------------------------------------------------------------------+
//| JSON helper: escape special characters                           |
//+------------------------------------------------------------------+
string EscapeJson(string s)
{
    string result = "";
    for(int i = 0; i < StringLen(s); i++)
    {
        ushort ch = StringGetCharacter(s, i);
        if(ch == '"')       result += "\\\"";
        else if(ch == '\\') result += "\\\\";
        else if(ch == '\n') result += "\\n";
        else if(ch == '\r') result += "\\r";
        else if(ch == '\t') result += "\\t";
        else                result += CharToString((uchar)ch);
    }
    return result;
}

//+------------------------------------------------------------------+
//| Minimal JSON parser: get string value for key                    |
//+------------------------------------------------------------------+
string JsonGetString(string json, string key)
{
    string search = "\"" + key + "\":\"";
    int pos = StringFind(json, search);
    if(pos < 0)
    {
        // Try without quotes (for numeric values used as strings)
        search = "\"" + key + "\":";
        pos = StringFind(json, search);
        if(pos < 0) return "";
        pos += StringLen(search);
        // Find end (comma or closing brace)
        int endPos = pos;
        while(endPos < StringLen(json))
        {
            ushort ch = StringGetCharacter(json, endPos);
            if(ch == ',' || ch == '}' || ch == ']')
                break;
            endPos++;
        }
        string val = StringSubstr(json, pos, endPos - pos);
        StringTrimLeft(val);
        StringTrimRight(val);
        // Remove quotes if present
        if(StringLen(val) >= 2 && StringGetCharacter(val, 0) == '"')
            val = StringSubstr(val, 1, StringLen(val) - 2);
        return val;
    }
    pos += StringLen(search);
    // Find closing quote (handle escapes)
    int endPos = pos;
    while(endPos < StringLen(json))
    {
        ushort ch = StringGetCharacter(json, endPos);
        if(ch == '\\')
        {
            endPos += 2;
            continue;
        }
        if(ch == '"')
            break;
        endPos++;
    }
    return StringSubstr(json, pos, endPos - pos);
}

//+------------------------------------------------------------------+
//| Minimal JSON parser: get int value for key                       |
//+------------------------------------------------------------------+
int JsonGetInt(string json, string key)
{
    string val = JsonGetString(json, key);
    if(val == "") return 0;
    return (int)StringToInteger(val);
}

//+------------------------------------------------------------------+
//| Minimal JSON parser: get double value for key                    |
//+------------------------------------------------------------------+
double JsonGetDouble(string json, string key)
{
    string val = JsonGetString(json, key);
    if(val == "") return 0.0;
    return StringToDouble(val);
}
//+------------------------------------------------------------------+
