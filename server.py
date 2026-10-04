"""FastAPI server for PropCopy dashboard."""

import asyncio
import json
import logging
import os
import sys
import time

from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from config_manager import (
    load_config, mask_passwords, save_config,
    load_accounts, save_accounts, mask_accounts_passwords, resolve_account_tag,
    migrate_accounts_from_config,
)
from engine_manager import EngineManager
from insights import compute_insights
from log_manager import log_buffer
from version import CURRENT_VERSION

logger = logging.getLogger("server")

# PyInstaller path resolution
if getattr(sys, "frozen", False):
    BASE_DIR = sys._MEIPASS
else:
    BASE_DIR = os.path.dirname(os.path.abspath(__file__))

STATIC_DIR = os.path.join(BASE_DIR, "static")
TEMPLATES_DIR = os.path.join(BASE_DIR, "templates")

# Config/accounts files live in working directory (not bundled)
CONFIG_PATH = os.path.join(os.getcwd(), "config.yaml")
ACCOUNTS_PATH = os.path.join(os.getcwd(), "accounts.yaml")

# Auto-migrate existing accounts from config.yaml on first run
migrate_accounts_from_config(CONFIG_PATH, ACCOUNTS_PATH)

app = FastAPI(title="PropCopy", docs_url=None, redoc_url=None)
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
templates = Jinja2Templates(directory=TEMPLATES_DIR)

engine = EngineManager()


# ── Pages ──

@app.get("/", response_class=HTMLResponse)
async def dashboard(request: Request):
    return templates.TemplateResponse("index.html", {"request": request})


# ── Config API ──

@app.get("/api/config")
async def get_config():
    config = load_config(CONFIG_PATH)
    result = mask_passwords(config)
    # Always read tag mappings from raw YAML to avoid merge/cache issues
    import yaml
    with open(CONFIG_PATH, "r") as f:
        raw = yaml.safe_load(f) or {}
    result["_master_tag"] = raw.get("_master_tag", "")
    result["_slave_tags"] = raw.get("_slave_tags", [])
    return result


@app.put("/api/config")
async def update_config(request: Request):
    body = await request.json()
    accounts = load_accounts(ACCOUNTS_PATH)

    # Resolve master from account tag
    master_tag = body.get("master_tag", "")
    if master_tag:
        resolved = resolve_account_tag(master_tag, accounts)
        if resolved:
            body["master"] = resolved
            body["master"]["account_tag"] = master_tag
        else:
            return JSONResponse({"ok": False, "error": f"Account '{master_tag}' not found"}, status_code=400)
    else:
        # Legacy: manual master data (preserve passwords if masked)
        existing = load_config(CONFIG_PATH)
        if body.get("master", {}).get("password") == "********":
            body["master"]["password"] = existing["master"]["password"]

    # Resolve slaves from account tags
    raw_slaves = body.get("slaves", [])
    resolved_slaves = []
    for slave_entry in raw_slaves:
        tag = slave_entry.get("account_tag", "")
        size_scaler = float(slave_entry.get("size_scaler", 1.0))
        max_drawdown_pct = float(slave_entry.get("max_drawdown_pct", 0))
        price_offset_points = float(slave_entry.get("price_offset_points", 0))
        price_offset_timeout = float(slave_entry.get("price_offset_timeout", 20))
        price_offset_max_points = float(slave_entry.get("price_offset_max_points", 0))
        volume_jitter_pct = float(slave_entry.get("volume_jitter_pct", 0))
        sltp_offset_points = float(slave_entry.get("sltp_offset_points", 0))
        profit_target_usd = float(slave_entry.get("profit_target_usd", 0))
        sltp_multiplier = float(slave_entry.get("sltp_multiplier", 1.0))
        invert = bool(slave_entry.get("invert", False))
        if tag:
            resolved = resolve_account_tag(tag, accounts)
            if resolved:
                resolved["size_scaler"] = size_scaler
                if max_drawdown_pct > 0:
                    resolved["max_drawdown_pct"] = max_drawdown_pct
                if price_offset_points != 0:
                    resolved["price_offset_points"] = price_offset_points
                if price_offset_timeout != 20:
                    resolved["price_offset_timeout"] = price_offset_timeout
                if price_offset_max_points > 0:
                    resolved["price_offset_max_points"] = price_offset_max_points
                if volume_jitter_pct > 0:
                    resolved["volume_jitter_pct"] = volume_jitter_pct
                if sltp_offset_points > 0:
                    resolved["sltp_offset_points"] = sltp_offset_points
                if profit_target_usd > 0:
                    resolved["profit_target_usd"] = profit_target_usd
                if sltp_multiplier != 1.0:
                    resolved["sltp_multiplier"] = sltp_multiplier
                if invert:
                    resolved["invert"] = True
                resolved["account_tag"] = tag
                resolved_slaves.append(resolved)
            else:
                return JSONResponse({"ok": False, "error": f"Account '{tag}' not found"}, status_code=400)
        else:
            # Legacy: manual slave data
            existing = load_config(CONFIG_PATH)
            if slave_entry.get("password") == "********":
                for i, ex_slave in enumerate(existing.get("slaves", [])):
                    if ex_slave.get("account") == slave_entry.get("account"):
                        slave_entry["password"] = ex_slave["password"]
                        break
            slave_entry["account"] = int(slave_entry.get("account", 0))
            slave_entry["size_scaler"] = size_scaler
            slave_entry["platform"] = slave_entry.get("platform", "mt5")
            if slave_entry["platform"] == "mt4":
                slave_entry["mt4_port"] = int(slave_entry.get("mt4_port", 15555))
            if max_drawdown_pct > 0:
                slave_entry["max_drawdown_pct"] = max_drawdown_pct
            if price_offset_points != 0:
                slave_entry["price_offset_points"] = price_offset_points
            if price_offset_timeout != 20:
                slave_entry["price_offset_timeout"] = price_offset_timeout
            if price_offset_max_points > 0:
                slave_entry["price_offset_max_points"] = price_offset_max_points
            if volume_jitter_pct > 0:
                slave_entry["volume_jitter_pct"] = volume_jitter_pct
            if sltp_offset_points > 0:
                slave_entry["sltp_offset_points"] = sltp_offset_points
            if profit_target_usd > 0:
                slave_entry["profit_target_usd"] = profit_target_usd
            if sltp_multiplier != 1.0:
                slave_entry["sltp_multiplier"] = sltp_multiplier
            if invert:
                slave_entry["invert"] = True
            resolved_slaves.append(slave_entry)
    body["slaves"] = resolved_slaves

    # Store which tags are assigned (for UI to reload selections)
    body["_master_tag"] = master_tag
    body["_slave_tags"] = [
        {
            "account_tag": s.get("account_tag", ""),
            "size_scaler": s.get("size_scaler", 1.0),
            "max_drawdown_pct": s.get("max_drawdown_pct", 0),
            "price_offset_points": s.get("price_offset_points", 0),
            "price_offset_timeout": s.get("price_offset_timeout", 20),
            "price_offset_max_points": s.get("price_offset_max_points", 0),
            "volume_jitter_pct": s.get("volume_jitter_pct", 0),
            "sltp_offset_points": s.get("sltp_offset_points", 0),
            "profit_target_usd": s.get("profit_target_usd", 0),
            "sltp_multiplier": s.get("sltp_multiplier", 1.0),
            "invert": s.get("invert", False),
        }
        for s in raw_slaves
    ]

    # Ensure platform field is set on master
    if "master" in body:
        body["master"]["platform"] = body["master"].get("platform", "mt5")

    # Ensure account numbers are integers
    if "master" in body:
        body["master"]["account"] = int(body["master"].get("account", 0))
    for slave in body.get("slaves", []):
        slave["account"] = int(slave.get("account", 0))
        slave["size_scaler"] = float(slave.get("size_scaler", 1.0))

    # Clean transient fields before save
    body.pop("master_tag", None)
    save_config(body, CONFIG_PATH)
    return {"ok": True}


# ── Accounts API ──

@app.get("/api/accounts")
async def get_accounts():
    accounts = load_accounts(ACCOUNTS_PATH)
    return mask_accounts_passwords(accounts)


@app.put("/api/accounts")
async def update_accounts(request: Request):
    body = await request.json()
    accounts = body if isinstance(body, list) else body.get("accounts", [])
    # Preserve passwords for masked entries
    existing = load_accounts(ACCOUNTS_PATH)
    existing_by_tag = {a["tag"]: a for a in existing if a.get("tag")}
    for acct in accounts:
        if acct.get("password") == "********" and acct.get("tag") in existing_by_tag:
            acct["password"] = existing_by_tag[acct["tag"]]["password"]
        # symbol_map is hand-edited in accounts.yaml; the dashboard form does not send it
        if "symbol_map" not in acct and existing_by_tag.get(acct.get("tag"), {}).get("symbol_map"):
            acct["symbol_map"] = existing_by_tag[acct["tag"]]["symbol_map"]
        acct["account"] = int(acct.get("account", 0))
    save_accounts(accounts, ACCOUNTS_PATH)
    return {"ok": True}


# ── Engine API ──

@app.post("/api/engine/start")
async def start_engine():
    config = load_config(CONFIG_PATH)
    result = engine.start(config)
    return result


@app.post("/api/engine/stop")
async def stop_engine():
    result = engine.stop()
    return result


@app.get("/api/engine/status")
async def engine_status():
    return engine.get_status()


# ── Data API ──

@app.post("/api/guardian/reset/{account_id}")
async def reset_guardian(account_id: int):
    if not engine.running:
        return JSONResponse({"ok": False, "error": "Engine not running"}, status_code=400)
    ok = engine.reset_guardian(account_id)
    if ok:
        return {"ok": True}
    return JSONResponse({"ok": False, "error": f"Slave {account_id} not found"}, status_code=404)


@app.post("/api/profit-target/toggle/{account_id}")
async def toggle_profit_target(account_id: int, request: Request):
    body = await request.json()
    enabled = bool(body.get("enabled", True))
    ok = engine.toggle_profit_target(account_id, enabled)
    if ok:
        return {"ok": True}
    return JSONResponse({"ok": False, "error": f"Slave {account_id} not found or engine not running"}, status_code=400)


@app.post("/api/account/{account_id}/toggle")
async def toggle_account(account_id: int):
    result = engine.toggle_account(account_id)
    if result["ok"]:
        return result
    return JSONResponse(result, status_code=400)


@app.get("/api/positions")
async def get_positions():
    status = engine.get_status()
    return status.get("positions", [])


@app.get("/api/analytics")
async def get_analytics():
    analytics_path = os.path.join(os.getcwd(), "data", "analytics.json")
    if not os.path.exists(analytics_path):
        return {"equity_stats": {}, "closed_trades": []}
    with open(analytics_path, "r") as f:
        return json.load(f)


@app.get("/api/analytics/insights")
async def get_insights(setup: str = None):
    analytics_path = os.path.join(os.getcwd(), "data", "analytics.json")
    closed_trades = []
    copy_latency = []
    if os.path.exists(analytics_path):
        with open(analytics_path, "r") as f:
            data = json.load(f)
        closed_trades = data.get("closed_trades", [])
        copy_latency = data.get("copy_latency", [])
    # Merge copy latency from engine state (coordinator keeps it in memory)
    engine_state = engine.get_status()
    copy_latency.extend(engine_state.get("copy_latency", []))
    # Filter by setup if specified
    if setup:
        closed_trades = [t for t in closed_trades if t.get("setup") == setup]
    return compute_insights(closed_trades, copy_latency)


@app.put("/api/trade/{ticket}/setup")
async def set_trade_setup(ticket: int, request: Request):
    body = await request.json()
    setup = body.get("setup")  # None or string
    if engine.running:
        # Engine running — send via command queue to master process
        ok = engine.set_trade_setup(ticket, setup)
    else:
        # Engine stopped — update analytics.json directly
        analytics_path = os.path.join(os.getcwd(), "data", "analytics.json")
        if not os.path.exists(analytics_path):
            return JSONResponse({"ok": False, "error": "No analytics data"}, status_code=404)
        with open(analytics_path, "r") as f:
            data = json.load(f)
        ok = False
        for trade in data.get("closed_trades", []):
            if trade.get("ticket") == ticket:
                if setup:
                    trade["setup"] = setup
                else:
                    trade.pop("setup", None)
                ok = True
                break
        if ok:
            tmp = analytics_path + ".tmp"
            with open(tmp, "w") as f:
                json.dump(data, f, indent=2)
            os.replace(tmp, analytics_path)
    return {"ok": ok}


@app.get("/api/setups")
async def get_setups():
    config = load_config(CONFIG_PATH)
    setups = config.get("settings", {}).get("setups", [])
    return {"setups": [s for s in setups if s]}


@app.post("/api/analytics/trades/delete")
async def delete_trades(request: Request):
    body = await request.json()
    tickets = set(body.get("tickets", []))
    if not tickets:
        return JSONResponse({"ok": False, "error": "No tickets specified"}, status_code=400)
    analytics_path = os.path.join(os.getcwd(), "data", "analytics.json")
    if not os.path.exists(analytics_path):
        return JSONResponse({"ok": False, "error": "No analytics data"}, status_code=404)
    with open(analytics_path, "r") as f:
        data = json.load(f)
    before = len(data.get("closed_trades", []))
    data["closed_trades"] = [t for t in data.get("closed_trades", []) if t.get("ticket") not in tickets]
    removed = before - len(data["closed_trades"])
    if removed > 0:
        tmp = analytics_path + ".tmp"
        with open(tmp, "w") as f:
            json.dump(data, f, indent=2)
        os.replace(tmp, analytics_path)
        # Re-export CSV
        csv_path = analytics_path.replace(".json", ".csv")
        if os.path.exists(csv_path):
            os.remove(csv_path)
    return {"ok": True, "removed": removed}


@app.get("/api/analytics/csv")
async def download_csv():
    csv_path = os.path.join(os.getcwd(), "data", "analytics.csv")
    if not os.path.exists(csv_path):
        return JSONResponse({"error": "No analytics CSV yet"}, status_code=404)
    return FileResponse(csv_path, filename="propcopy_analytics.csv", media_type="text/csv")


@app.get("/api/logs")
async def get_logs():
    return log_buffer.get_recent(200)


# ── Version API ──

@app.get("/api/version")
async def get_version():
    return {"current": CURRENT_VERSION}


# ── WebSocket ──

@app.websocket("/ws")
async def websocket_endpoint(ws: WebSocket):
    await ws.accept()
    try:
        while True:
            status = engine.get_status()
            logs = log_buffer.get_recent(20)
            payload = {
                "type": "status_update",
                "data": {
                    **status,
                    "logs": logs,
                },
            }
            await ws.send_json(payload)
            await asyncio.sleep(0.5)
    except WebSocketDisconnect:
        pass
    except Exception:
        pass
