"""
blueprints/replay.py
────────────────────
Flask blueprint for OpenAlgo Replay Mode.
"""

from datetime import datetime

from flask import Blueprint, jsonify, request

from replay.clock import clock_instance
from replay.dal import expand_nifty_options, get_range, get_state
from replay.fill_simulator import simulate_order_fill
from replay.patterns import detect_patterns
from replay.recorder import (
    add_auto_record_symbols,
    list_auto_record_symbols,
    remove_auto_record_symbols,
)
from replay.storage import list_available, prune_old_replay_data
from utils.logging import get_logger
from utils.session import check_session_validity

logger = get_logger(__name__)

replay_bp = Blueprint("replay_bp", __name__, url_prefix="/")


@replay_bp.route("/api/replay/status", methods=["GET"])
@check_session_validity
def api_replay_status():
    return jsonify(clock_instance.get_state()), 200


@replay_bp.route("/api/replay/session", methods=["POST"])
@check_session_validity
def api_replay_session():
    data = request.get_json(silent=True) or {}
    session_date = data.get("date", "2026-08-10")
    start_time = data.get("start_time", "09:15:00")
    end_time = data.get("end_time", "15:30:00")
    watchlist = data.get("watchlist", ["NIFTY", "RELIANCE"])

    # Expand Nifty options dynamically if requested
    watchlist = expand_nifty_options(session_date, watchlist)

    clock_instance.configure(session_date, start_time, end_time, watchlist)

    # Automatically prune replay data older than 7 days (weekly retention) to prevent storage overload
    try:
        prune_old_replay_data(7)
    except Exception as exc:
        logger.error(f"[api_replay_session] Automatic pruning error: {exc}")

    return jsonify({"status": "configured", **clock_instance.get_state()}), 200


@replay_bp.route("/api/replay/control", methods=["POST"])
@check_session_validity
def api_replay_control():
    data = request.get_json(silent=True) or {}
    action = data.get("action")  # play, pause, seek, speed, step

    if action == "play":
        clock_instance.play()
    elif action == "pause":
        clock_instance.pause()
    elif action == "seek":
        ts = data.get("timestamp")
        if ts:
            clock_instance.seek(ts)
    elif action == "speed":
        spd = data.get("speed", 1.0)
        clock_instance.set_speed(spd)
    elif action == "step":
        secs = data.get("seconds", 1)
        clock_instance.step(secs)
    else:
        return jsonify({"status": "error", "message": "unknown action"}), 400

    return jsonify({"status": "success", **clock_instance.get_state()}), 200


@replay_bp.route("/api/replay/state", methods=["GET"])
@check_session_validity
def api_replay_state():
    symbol = request.args.get("symbol")
    ts_str = request.args.get("timestamp")

    if not symbol:
        return jsonify({"status": "error", "message": "symbol is required"}), 400

    if ts_str:
        try:
            dt = datetime.fromisoformat(ts_str)
        except Exception:
            dt = datetime.now()
    else:
        dt = clock_instance.current_time

    state = get_state(symbol, dt)
    return jsonify({"symbol": symbol.upper(), "timestamp": dt.isoformat(), "state": state}), 200


@replay_bp.route("/api/replay/range", methods=["GET"])
@check_session_validity
def api_replay_range():
    symbol = request.args.get("symbol")
    start_str = request.args.get("start")
    end_str = request.args.get("end")

    if not symbol or not start_str or not end_str:
        return jsonify({"status": "error", "message": "symbol, start, and end are required"}), 400

    try:
        t_start = datetime.fromisoformat(start_str)
        t_end = datetime.fromisoformat(end_str)
    except Exception as exc:
        return jsonify({"status": "error", "message": f"invalid timestamp format: {exc}"}), 400

    range_data = get_range(symbol, t_start, t_end)
    return jsonify({"symbol": symbol.upper(), "count": len(range_data), "data": range_data}), 200


@replay_bp.route("/api/replay/patterns", methods=["GET"])
@check_session_validity
def api_replay_patterns():
    symbol = request.args.get("symbol")
    start_str = request.args.get("start")
    end_str = request.args.get("end")
    show_fvg = request.args.get("show_fvg", "true").lower() == "true"
    show_mss = request.args.get("show_mss", "true").lower() == "true"
    show_sweep = request.args.get("show_sweep", "true").lower() == "true"

    if not symbol or not start_str or not end_str:
        return jsonify({"status": "error", "message": "symbol, start, and end are required"}), 400

    try:
        t_start = datetime.fromisoformat(start_str)
        t_end = datetime.fromisoformat(end_str)
    except Exception as exc:
        return jsonify({"status": "error", "message": f"invalid timestamp format: {exc}"}), 400

    patterns = detect_patterns(symbol, t_start, t_end, show_fvg, show_mss, show_sweep)
    return jsonify({"symbol": symbol.upper(), "patterns": patterns}), 200


@replay_bp.route("/api/replay/available", methods=["GET"])
@check_session_validity
def api_replay_available():
    date_str = request.args.get("date")
    entries = list_available(date_str)
    return jsonify({"count": len(entries), "data": entries}), 200


@replay_bp.route("/api/replay/auto_record/list", methods=["GET"])
@check_session_validity
def api_replay_auto_record_list():
    return jsonify({"symbols": list_auto_record_symbols()}), 200


@replay_bp.route("/api/replay/auto_record/add", methods=["POST"])
@check_session_validity
def api_replay_auto_record_add():
    data = request.get_json(silent=True) or {}
    symbols = data.get("symbols")
    if not symbols or not isinstance(symbols, list):
        return jsonify({"status": "error", "message": "symbols (list of {symbol, exchange}) is required"}), 400
    result = add_auto_record_symbols(symbols)
    return jsonify(result), 200


@replay_bp.route("/api/replay/auto_record/remove", methods=["POST"])
@check_session_validity
def api_replay_auto_record_remove():
    data = request.get_json(silent=True) or {}
    symbols = data.get("symbols")  # optional; omit to clear the whole list
    result = remove_auto_record_symbols(symbols)
    return jsonify(result), 200


@replay_bp.route("/api/replay/simulate_order", methods=["POST"])
@check_session_validity
def api_replay_simulate_order():
    data = request.get_json(silent=True) or {}
    symbol = data.get("symbol")
    action = data.get("action", "BUY")
    quantity = data.get("quantity", 1)
    price_type = data.get("price_type", "MARKET")
    limit_price = data.get("limit_price", 0.0)
    ts_str = data.get("timestamp")

    if not symbol:
        return jsonify({"status": "error", "message": "symbol is required"}), 400

    dt = datetime.fromisoformat(ts_str) if ts_str else clock_instance.current_time
    result = simulate_order_fill(symbol, dt, action, quantity, price_type, limit_price)
    return jsonify(result), 200


@replay_bp.route("/api/replay/prune", methods=["POST"])
@check_session_validity
def api_replay_prune():
    data = request.get_json(silent=True) or {}
    days = int(data.get("days_to_keep", 7))
    removed = prune_old_replay_data(days)
    return jsonify({"status": "success", "removed_files": removed, "days_retained": days}), 200
