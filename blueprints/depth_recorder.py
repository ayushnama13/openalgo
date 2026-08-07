"""
blueprints/depth_recorder.py
─────────────────────────────
Flask blueprint for the Depth Recorder feature.

Routes:
  GET  /depth-recorder                  → React SPA (served by react_app.py)
  GET  /api/depth-recorder/status       → all recorder statuses
  POST /api/depth-recorder/start        → start a recorder
  POST /api/depth-recorder/stop         → stop a recorder
  GET  /api/depth-recorder/ticks        → recent ticks for a symbol
  GET  /api/depth-recorder/stats        → overall DB stats
  GET  /api/depth-recorder/configs      → persisted configs
  DEL  /api/depth-recorder/config       → delete a config
"""

from flask import Blueprint, jsonify, request, session

from database.auth_db import get_api_key_for_tradingview
from database.depth_recorder_db import (
    delete_config,
    get_all_configs,
    get_recent_ticks,
    get_stats,
    get_tick_count,
)
from services.depth_recorder_service import (
    get_all_statuses,
    get_status,
    start_recorder,
    stop_recorder,
)
from utils.logging import get_logger
from utils.session import check_session_validity

logger = get_logger(__name__)

depth_recorder_bp = Blueprint("depth_recorder_bp", __name__, url_prefix="/")


def _api_key() -> str | None:
    """Resolve the current user's API key from session."""
    username = session.get("user")
    if not username:
        return None
    return get_api_key_for_tradingview(username)


# ────────────────────────────────────────────────────────────────────────────
# Status
# ────────────────────────────────────────────────────────────────────────────
@depth_recorder_bp.route("/api/depth-recorder/status", methods=["GET"])
@check_session_validity
def api_status():
    """Return status of all active/stopped recorders."""
    symbol = request.args.get("symbol")
    exchange = request.args.get("exchange")

    if symbol and exchange:
        return jsonify(get_status(symbol, exchange)), 200

    return jsonify(get_all_statuses()), 200


# ────────────────────────────────────────────────────────────────────────────
# Start
# ────────────────────────────────────────────────────────────────────────────
@depth_recorder_bp.route("/api/depth-recorder/start", methods=["POST"])
@check_session_validity
def api_start():
    """Start recording depth for a symbol."""
    data = request.get_json(silent=True) or {}
    symbol = (data.get("symbol") or "").strip().upper()
    exchange = (data.get("exchange") or "NSE").strip().upper()

    if not symbol:
        return jsonify({"status": "error", "message": "symbol is required"}), 400

    api_key = _api_key()
    if not api_key:
        return jsonify({"status": "error", "message": "API key not found — visit /apikey"}), 401

    result = start_recorder(symbol, exchange, api_key)
    return jsonify(result), 200


# ────────────────────────────────────────────────────────────────────────────
# Stop
# ────────────────────────────────────────────────────────────────────────────
@depth_recorder_bp.route("/api/depth-recorder/stop", methods=["POST"])
@check_session_validity
def api_stop():
    """Stop recording depth for a symbol."""
    data = request.get_json(silent=True) or {}
    symbol = (data.get("symbol") or "").strip().upper()
    exchange = (data.get("exchange") or "NSE").strip().upper()

    if not symbol:
        return jsonify({"status": "error", "message": "symbol is required"}), 400

    result = stop_recorder(symbol, exchange)
    return jsonify(result), 200


# ────────────────────────────────────────────────────────────────────────────
# Recent ticks
# ────────────────────────────────────────────────────────────────────────────
@depth_recorder_bp.route("/api/depth-recorder/ticks", methods=["GET"])
@check_session_validity
def api_ticks():
    """Return the N most recent ticks for symbol/exchange."""
    symbol = (request.args.get("symbol") or "").strip().upper()
    exchange = (request.args.get("exchange") or "NSE").strip().upper()
    try:
        limit = max(1, min(int(request.args.get("limit", 100)), 1000))
    except (TypeError, ValueError):
        limit = 100

    if not symbol:
        return jsonify({"status": "error", "message": "symbol is required"}), 400

    ticks = get_recent_ticks(symbol, exchange, limit)
    count = get_tick_count(symbol, exchange)

    return jsonify({
        "status": "success",
        "symbol": symbol,
        "exchange": exchange,
        "total_count": count,
        "returned": len(ticks),
        "ticks": ticks,
    }), 200


# ────────────────────────────────────────────────────────────────────────────
# Stats
# ────────────────────────────────────────────────────────────────────────────
@depth_recorder_bp.route("/api/depth-recorder/stats", methods=["GET"])
@check_session_validity
def api_stats():
    """Return overall database statistics."""
    return jsonify({"status": "success", **get_stats()}), 200


# ────────────────────────────────────────────────────────────────────────────
# Configs
# ────────────────────────────────────────────────────────────────────────────
@depth_recorder_bp.route("/api/depth-recorder/configs", methods=["GET"])
@check_session_validity
def api_configs():
    """Return all persisted recorder configurations."""
    return jsonify({"status": "success", "configs": get_all_configs()}), 200


@depth_recorder_bp.route("/api/depth-recorder/config", methods=["DELETE"])
@check_session_validity
def api_delete_config():
    """Delete a recorder config (and stop it if running)."""
    data = request.get_json(silent=True) or {}
    symbol = (data.get("symbol") or "").strip().upper()
    exchange = (data.get("exchange") or "NSE").strip().upper()

    if not symbol:
        return jsonify({"status": "error", "message": "symbol is required"}), 400

    # Stop first if running
    stop_recorder(symbol, exchange)

    deleted = delete_config(symbol, exchange)
    return jsonify({
        "status": "success" if deleted else "not_found",
        "symbol": symbol,
        "exchange": exchange,
    }), 200
