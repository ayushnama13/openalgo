# blueprints/backtest.py

import json
import uuid
from datetime import datetime

import pandas as pd
import pytz
from flask import Blueprint, jsonify, render_template_string, request

from database.backtest_db import (
    BacktestEquityCurve,
    BacktestRun,
    BacktestStrategy,
    BacktestTrade,
    cleanup_old_runs,
    db_session,
)
from database.historify_db import (
    get_available_symbols,
    get_data_range,
    resolve_base_interval,
)
from services.backtest_analytics import get_benchmark_comparison
from services.backtest_service import (
    cancel_batch,
    cancel_job,
    start_backtest_batch,
)
from utils.logging import get_logger
from utils.session import check_session_validity

logger = get_logger(__name__)

backtest_bp = Blueprint("backtest_api", __name__, url_prefix="/backtest/api")

# Server-side cap for equity curves on the batch overlay — 7 timeframes of 1m
# over a year is ~500k raw points, far too heavy for one chart request.
MAX_CURVE_POINTS = 2000


def _downsample_curve(rows: list, limit: int = MAX_CURVE_POINTS) -> list:
    """Evenly downsample a list of curve points to at most ``limit`` entries."""
    n = len(rows)
    if n <= limit:
        return rows
    step = n / limit
    idxs = sorted({min(n - 1, int(i * step)) for i in range(limit)} | {n - 1})
    return [rows[i] for i in idxs]


@backtest_bp.route("/catalog", methods=["GET"])
@check_session_validity
def get_catalog():
    """Returns symbol/exchange combinations that actually have data in Historify."""
    try:
        symbols_data = get_available_symbols()
        return jsonify({"status": "success", "catalog": symbols_data})
    except Exception as e:
        logger.exception(f"Error fetching backtest data catalog: {e}")
        return jsonify({"status": "error", "message": str(e)}), 500


@backtest_bp.route("/range", methods=["GET"])
@check_session_validity
def get_range():
    """Returns available date/timestamp bounds for a symbol/exchange/interval combo."""
    symbol = request.args.get("symbol", "").upper()
    exchange = request.args.get("exchange", "NSE").upper()
    interval = request.args.get("interval", "1m")

    if not symbol:
        return jsonify({"status": "error", "message": "Symbol is required"}), 400

    try:
        # Historify only stores 1m and D; 5m/15m/1h are aggregated from 1m and
        # W/M/Q/Y from D. The catalog has no row for an aggregated interval, so
        # the bounds must be read off the base interval it is derived from.
        base_interval = resolve_base_interval(interval)
        range_data = get_data_range(symbol, exchange, base_interval)
        if not range_data:
            return jsonify({
                "status": "error",
                "message": f"No {base_interval} data found for requested combination",
            }), 404

        first_ts = range_data["first_timestamp"]
        last_ts = range_data["last_timestamp"]

        start_str = datetime.fromtimestamp(first_ts, pytz.timezone("Asia/Kolkata")).strftime("%Y-%m-%d %H:%M:%S") if first_ts else ""
        end_str = datetime.fromtimestamp(last_ts, pytz.timezone("Asia/Kolkata")).strftime("%Y-%m-%d %H:%M:%S") if last_ts else ""

        return jsonify({
            "status": "success",
            "symbol": symbol,
            "exchange": exchange,
            "interval": interval,
            "base_interval": base_interval,
            "first_timestamp": first_ts,
            "last_timestamp": last_ts,
            "start_date": start_str,
            "end_date": end_str,
            "record_count": range_data["record_count"],
        })
    except Exception as e:
        logger.exception(f"Error fetching symbol data range: {e}")
        return jsonify({"status": "error", "message": str(e)}), 500


@backtest_bp.route("/run", methods=["POST"])
@check_session_validity
def submit_run():
    """Submits a new async backtest job — one job per interval, one batch.

    ``intervals`` (list) fans one request out across N timeframes; the scalar
    ``interval`` is kept for backward compatibility and is used when
    ``intervals`` is absent.
    """
    data = request.get_json() or {}
    try:
        name = data.get("name", "Untitled Backtest")
        strategy_code = data.get("strategy_code", "")
        symbols = data.get("symbols", ["NSE:SBIN"])
        exchange = data.get("exchange", "NSE")
        interval = data.get("interval", "1m")
        intervals = data.get("intervals")
        if intervals is not None:
            intervals = [iv for iv in (intervals if isinstance(intervals, list) else [intervals]) if iv]
            if not intervals:
                return jsonify({"status": "error", "message": "intervals list must not be empty"}), 400
        else:
            intervals = [interval]
        start_date = data.get("start_date")
        end_date = data.get("end_date")
        initial_capital = float(data.get("initial_capital", 100000.0))
        sizing_type = data.get("sizing_type", "fixed_qty")
        sizing_value = float(data.get("sizing_value", 1.0))
        slippage_bps = float(data.get("slippage_bps", 0.0))
        brokerage_bps = float(data.get("brokerage_bps", 0.0))
        product_type = data.get("product_type", "MIS")
        missing_data_policy = data.get("missing_data_policy", "skip")
        stop_loss_pct = float(data["stop_loss_pct"]) if data.get("stop_loss_pct") is not None else None
        take_profit_pct = float(data["take_profit_pct"]) if data.get("take_profit_pct") is not None else None
        daily_loss_limit = float(data["daily_loss_limit"]) if data.get("daily_loss_limit") is not None else None
        max_positions = int(data.get("max_positions", 5))

        if not strategy_code:
            return jsonify({"status": "error", "message": "strategy_code is required"}), 400

        batch = start_backtest_batch(
            name=name,
            strategy_code=strategy_code,
            symbols=symbols if isinstance(symbols, list) else [symbols],
            exchange=exchange,
            intervals=intervals,
            start_date=start_date,
            end_date=end_date,
            initial_capital=initial_capital,
            sizing_type=sizing_type,
            sizing_value=sizing_value,
            slippage_bps=slippage_bps,
            brokerage_bps=brokerage_bps,
            product_type=product_type,
            missing_data_policy=missing_data_policy,
            stop_loss_pct=stop_loss_pct,
            take_profit_pct=take_profit_pct,
            daily_loss_limit=daily_loss_limit,
            max_positions=max_positions,
        )
        first_job = batch["jobs"][0]["job_id"]
        return jsonify({
            "status": "success",
            "job_id": first_job,
            "batch_id": batch["batch_id"],
            "jobs": batch["jobs"],
            "message": f"Backtest batch of {len(batch['jobs'])} timeframe(s) submitted successfully",
        })
    except Exception as e:
        logger.exception(f"Error submitting backtest run: {e}")
        return jsonify({"status": "error", "message": str(e)}), 500


@backtest_bp.route("/cancel/<job_id>", methods=["POST"])
@check_session_validity
def cancel_backtest(job_id):
    """Cancels a running backtest job."""
    try:
        success = cancel_job(job_id)
        if success:
            return jsonify({"status": "success", "message": f"Job {job_id} cancelled"})
        else:
            return jsonify({"status": "error", "message": f"Job {job_id} is not currently running"}), 400
    except Exception as e:
        logger.exception(f"Error cancelling backtest job {job_id}: {e}")
        return jsonify({"status": "error", "message": str(e)}), 500


@backtest_bp.route("/jobs", methods=["GET"])
@check_session_validity
def list_jobs():
    """Returns list of past backtest runs with summary metadata."""
    session = db_session()
    try:
        runs = session.query(BacktestRun).order_by(BacktestRun.created_at.desc()).all()
        results = []
        for r in runs:
            metrics = json.loads(r.metrics_json) if r.metrics_json else {}
            results.append({
                "job_id": r.job_id,
                "name": r.name,
                "status": r.status,
                "symbols": json.loads(r.symbols) if r.symbols else [],
                "exchange": r.exchange,
                "interval": r.interval,
                "start_date": r.start_date,
                "end_date": r.end_date,
                "initial_capital": r.initial_capital,
                "created_at": r.created_at.isoformat() if r.created_at else "",
                "completed_at": r.completed_at.isoformat() if r.completed_at else "",
                "total_return_pct": metrics.get("total_return_pct", 0.0),
                "sharpe_ratio": metrics.get("sharpe_ratio", 0.0),
                "max_drawdown_pct": metrics.get("max_drawdown_pct", 0.0),
                "win_rate_pct": metrics.get("win_rate_pct", 0.0),
                "total_trades": metrics.get("total_trades", 0),
                "batch_id": r.batch_id,
                "batch_name": r.batch_name,
                "batch_seq": r.batch_seq or 0,
                "error_message": r.error_message,
            })
        return jsonify({"status": "success", "jobs": results})
    except Exception as e:
        logger.exception(f"Error listing backtest jobs: {e}")
        return jsonify({"status": "error", "message": str(e)}), 500
    finally:
        db_session.remove()


@backtest_bp.route("/batches", methods=["GET"])
@check_session_validity
def list_batches():
    """Returns one summary row per batch (batch of one for legacy runs)."""
    session = db_session()
    try:
        runs = session.query(BacktestRun).order_by(BacktestRun.created_at.desc()).all()
        groups: dict[str, list] = {}
        for r in runs:
            groups.setdefault(r.batch_id or r.job_id, []).append(r)

        batches = []
        for batch_id, members in groups.items():
            members.sort(key=lambda m: (m.batch_seq or 0, m.created_at or datetime.min))
            statuses = {}
            for m in members:
                statuses[m.status] = statuses.get(m.status, 0) + 1
            if statuses.get("running"):
                overall = "running"
            elif statuses.get("pending"):
                overall = "pending"
            elif statuses.get("failed") or statuses.get("cancelled"):
                overall = "partial"
            else:
                overall = "completed"

            returns = [
                float(json.loads(m.metrics_json).get("total_return_pct", 0.0))
                for m in members
                if m.status == "completed" and m.metrics_json
            ]
            first = members[0]
            # created_at is nullable in the model, so a row written by an older
            # build can carry None; max(..., default=None).isoformat() would
            # then blow up the whole listing.
            latest_created = max((m.created_at for m in members if m.created_at), default=None)
            symbol = ""
            if first.symbols:
                symbols = json.loads(first.symbols)
                if symbols:
                    symbol = symbols[0]
            batches.append({
                "batch_id": batch_id,
                "name": first.batch_name or first.name,
                "symbol": symbol,
                "exchange": first.exchange,
                "timeframe_count": len(members),
                "status": overall,
                "status_counts": statuses,
                "intervals": [m.interval for m in members],
                "best_return_pct": round(max(returns), 2) if returns else None,
                "worst_return_pct": round(min(returns), 2) if returns else None,
                "created_at": latest_created.isoformat() if latest_created else "",
            })

        batches.sort(key=lambda b: b["created_at"], reverse=True)
        return jsonify({"status": "success", "batches": batches})
    except Exception as e:
        logger.exception(f"Error listing backtest batches: {e}")
        return jsonify({"status": "error", "message": str(e)}), 500
    finally:
        db_session.remove()


@backtest_bp.route("/batches/<batch_id>", methods=["GET"])
@check_session_validity
def get_batch_details(batch_id):
    """Per-timeframe rows for one batch: metrics only, no curves."""
    session = db_session()
    try:
        runs = (
            session.query(BacktestRun)
            .filter_by(batch_id=batch_id)
            .order_by(BacktestRun.batch_seq.asc())
            .all()
        )
        if not runs:
            return jsonify({"status": "error", "message": "Batch not found"}), 404

        name = runs[0].batch_name or runs[0].name
        return jsonify({
            "status": "success",
            "batch_id": batch_id,
            "name": name,
            "runs": [
                {
                    "job_id": r.job_id,
                    "name": r.name,
                    "interval": r.interval,
                    "batch_seq": r.batch_seq or 0,
                    "status": r.status,
                    "metrics": json.loads(r.metrics_json) if r.metrics_json else {},
                    "error_message": r.error_message,
                }
                for r in runs
            ],
        })
    except Exception as e:
        logger.exception(f"Error fetching batch {batch_id}: {e}")
        return jsonify({"status": "error", "message": str(e)}), 500
    finally:
        db_session.remove()


@backtest_bp.route("/batches/<batch_id>/curves", methods=["GET"])
@check_session_validity
def get_batch_curves(batch_id):
    """Equity curves for every run in a batch, downsampled to ~2000 pts each."""
    session = db_session()
    try:
        runs = (
            session.query(BacktestRun)
            .filter_by(batch_id=batch_id)
            .order_by(BacktestRun.batch_seq.asc())
            .all()
        )
        if not runs:
            return jsonify({"status": "error", "message": "Batch not found"}), 404

        curves = []
        for r in runs:
            eq_rows = (
                session.query(BacktestEquityCurve)
                .filter_by(job_id=r.job_id)
                .order_by(BacktestEquityCurve.id.asc())
                .all()
            )
            points = [
                {
                    "timestamp": eq.timestamp,
                    "equity": eq.equity,
                    "drawdown_pct": eq.drawdown_pct,
                }
                for eq in eq_rows
            ]
            curves.append({
                "job_id": r.job_id,
                "interval": r.interval,
                "batch_seq": r.batch_seq or 0,
                "status": r.status,
                "points": _downsample_curve(points),
            })

        return jsonify({"status": "success", "batch_id": batch_id, "curves": curves})
    except Exception as e:
        logger.exception(f"Error fetching batch curves for {batch_id}: {e}")
        return jsonify({"status": "error", "message": str(e)}), 500
    finally:
        db_session.remove()


@backtest_bp.route("/batches/<batch_id>/cancel", methods=["POST"])
@check_session_validity
def cancel_batch_run(batch_id):
    """Cancels every sibling run in a batch."""
    try:
        cancelled = cancel_batch(batch_id)
        if not cancelled:
            return jsonify({"status": "error", "message": f"Batch {batch_id} has no running or queued jobs"}), 400
        return jsonify({"status": "success", "cancelled": cancelled, "message": f"Cancelled {len(cancelled)} jobs in batch {batch_id}"})
    except Exception as e:
        logger.exception(f"Error cancelling backtest batch {batch_id}: {e}")
        return jsonify({"status": "error", "message": str(e)}), 500


@backtest_bp.route("/batches/<batch_id>", methods=["DELETE"])
@check_session_validity
def delete_batch(batch_id):
    """Cascade-deletes a batch: runs + trades + equity curves."""
    session = db_session()
    try:
        rows = session.query(BacktestRun.job_id).filter_by(batch_id=batch_id).all()
        job_ids = [row.job_id for row in rows]
        if not job_ids:
            return jsonify({"status": "error", "message": "Batch not found"}), 404
        # Stop anything still running or queued first: a live pipeline holds its
        # own session and would keep writing rows back after the delete.
        cancel_batch(batch_id)
        session.query(BacktestEquityCurve).filter(BacktestEquityCurve.job_id.in_(job_ids)).delete(synchronize_session=False)
        session.query(BacktestTrade).filter(BacktestTrade.job_id.in_(job_ids)).delete(synchronize_session=False)
        session.query(BacktestRun).filter(BacktestRun.batch_id == batch_id).delete(synchronize_session=False)
        session.commit()
        return jsonify({"status": "success", "message": f"Batch {batch_id} and {len(job_ids)} runs deleted successfully"})
    except Exception as e:
        session.rollback()
        logger.exception(f"Error deleting backtest batch {batch_id}: {e}")
        return jsonify({"status": "error", "message": str(e)}), 500
    finally:
        db_session.remove()


@backtest_bp.route("/jobs/<job_id>", methods=["GET"])
@check_session_validity
def get_job_details(job_id):
    """Returns metadata and parameter configuration for a specific job."""
    session = db_session()
    try:
        run = session.query(BacktestRun).filter_by(job_id=job_id).first()
        if not run:
            return jsonify({"status": "error", "message": "Job not found"}), 404

        metrics = json.loads(run.metrics_json) if run.metrics_json else {}
        return jsonify({
            "status": "success",
            "job": {
                "job_id": run.job_id,
                "name": run.name,
                "status": run.status,
                "strategy_code": run.strategy_code,
                "symbols": json.loads(run.symbols) if run.symbols else [],
                "exchange": run.exchange,
                "interval": run.interval,
                "start_date": run.start_date,
                "end_date": run.end_date,
                "initial_capital": run.initial_capital,
                "sizing_type": run.sizing_type,
                "sizing_value": run.sizing_value,
                "slippage_bps": run.slippage_bps,
                "brokerage_bps": run.brokerage_bps,
                "product_type": run.product_type,
                "missing_data_policy": run.missing_data_policy,
                "stop_loss_pct": run.stop_loss_pct,
                "take_profit_pct": run.take_profit_pct,
                "daily_loss_limit": run.daily_loss_limit,
                "max_positions": run.max_positions,
                "created_at": run.created_at.isoformat() if run.created_at else "",
                "completed_at": run.completed_at.isoformat() if run.completed_at else "",
                "metrics": metrics,
                "error_message": run.error_message,
            }
        })
    except Exception as e:
        logger.exception(f"Error fetching job details for {job_id}: {e}")
        return jsonify({"status": "error", "message": str(e)}), 500
    finally:
        db_session.remove()


@backtest_bp.route("/jobs/<job_id>", methods=["DELETE"])
@check_session_validity
def delete_job(job_id):
    """Deletes a backtest job run from the database."""
    session = db_session()
    try:
        # Stop the run before removing its rows, otherwise the live pipeline
        # keeps its own session open and writes status back after the delete.
        cancel_job(job_id)
        session.query(BacktestEquityCurve).filter_by(job_id=job_id).delete()
        session.query(BacktestTrade).filter_by(job_id=job_id).delete()
        session.query(BacktestRun).filter_by(job_id=job_id).delete()
        session.commit()
        return jsonify({"status": "success", "message": f"Job {job_id} deleted successfully"})
    except Exception as e:
        session.rollback()
        logger.exception(f"Error deleting backtest job {job_id}: {e}")
        return jsonify({"status": "error", "message": str(e)}), 500
    finally:
        db_session.remove()


@backtest_bp.route("/cleanup", methods=["POST"])
@check_session_validity
def cleanup_runs():
    """Purges old backtest runs according to retention policy."""
    data = request.get_json() or {}
    days_to_keep = int(data.get("days_to_keep", 30))
    try:
        cleanup_old_runs(days_to_keep=days_to_keep)
        return jsonify({"status": "success", "message": f"Cleaned up backtest runs older than {days_to_keep} days"})
    except Exception as e:
        logger.exception(f"Error purging old backtest runs: {e}")
        return jsonify({"status": "error", "message": str(e)}), 500


@backtest_bp.route("/results/<job_id>", methods=["GET"])
@check_session_validity
def get_results(job_id):
    """Returns detailed result dataset including equity curve, trades log, metrics, and benchmark."""
    session = db_session()
    try:
        run = session.query(BacktestRun).filter_by(job_id=job_id).first()
        if not run:
            return jsonify({"status": "error", "message": "Job not found"}), 404

        metrics = json.loads(run.metrics_json) if run.metrics_json else {}

        # Fetch Equity Curve
        eq_rows = session.query(BacktestEquityCurve).filter_by(job_id=job_id).order_by(BacktestEquityCurve.id.asc()).all()
        equity_curve = [{
            "timestamp": eq.timestamp,
            "equity": eq.equity,
            "drawdown_pct": eq.drawdown_pct,
            "cash": eq.cash,
            "positions_value": eq.positions_value,
        } for eq in eq_rows]

        # Fetch Trades Log
        trade_rows = session.query(BacktestTrade).filter_by(job_id=job_id).order_by(BacktestTrade.id.asc()).all()
        trades = [{
            "id": t.id,
            "symbol": t.symbol,
            "exchange": t.exchange,
            "action": t.action,
            "entry_time": t.entry_time,
            "entry_price": t.entry_price,
            "exit_time": t.exit_time,
            "exit_price": t.exit_price,
            "quantity": t.quantity,
            "pnl": t.pnl,
            "pnl_pct": t.pnl_pct,
            "holding_period_bars": t.holding_period_bars,
            "entry_reason": t.entry_reason,
            "exit_reason": t.exit_reason,
            "costs": t.costs,
        } for t in trade_rows]

        # Fetch Benchmark Comparison (NIFTY 50)
        start_ts = int(pd.to_datetime(run.start_date).timestamp()) if run.start_date else None
        end_ts = int(pd.to_datetime(run.end_date).timestamp()) if run.end_date else None
        benchmark_curve = get_benchmark_comparison(
            start_time_epoch=start_ts,
            end_time_epoch=end_ts,
            interval=run.interval,
            initial_capital=run.initial_capital,
        )

        return jsonify({
            "status": "success",
            "job_id": job_id,
            "name": run.name,
            "metrics": metrics,
            "equity_curve": equity_curve,
            "trades": trades,
            "benchmark_curve": benchmark_curve,
        })
    except Exception as e:
        logger.exception(f"Error fetching results for job {job_id}: {e}")
        return jsonify({"status": "error", "message": str(e)}), 500
    finally:
        db_session.remove()


@backtest_bp.route("/compare", methods=["POST"])
@check_session_validity
def compare_jobs():
    """Compares metrics and equity curves across multiple job_ids."""
    data = request.get_json() or {}
    job_ids = data.get("job_ids", [])
    if not job_ids:
        return jsonify({"status": "error", "message": "job_ids list is required"}), 400

    session = db_session()
    try:
        comparison_results = []
        for jid in job_ids:
            run = session.query(BacktestRun).filter_by(job_id=jid).first()
            if not run:
                continue
            metrics = json.loads(run.metrics_json) if run.metrics_json else {}

            eq_rows = session.query(BacktestEquityCurve).filter_by(job_id=jid).order_by(BacktestEquityCurve.id.asc()).all()
            equity_curve = [{"timestamp": eq.timestamp, "equity": eq.equity} for eq in eq_rows]

            comparison_results.append({
                "job_id": jid,
                "name": run.name,
                "symbol": run.symbols,
                "interval": run.interval,
                "metrics": metrics,
                "equity_curve": equity_curve,
            })

        return jsonify({"status": "success", "comparison": comparison_results})
    except Exception as e:
        logger.exception(f"Error comparing backtest jobs: {e}")
        return jsonify({"status": "error", "message": str(e)}), 500
    finally:
        db_session.remove()


@backtest_bp.route("/tearsheet/<batch_or_job_id>", methods=["POST", "GET"])
@check_session_validity
def generate_tearsheet(batch_or_job_id):
    """Generates an HTML tearsheet report for export/download.

    Job ids (bt_...) render the single-run tearsheet; batch ids (batch_...)
    render a combined one: the matrix plus per-timeframe tearsheet links.
    """
    session = db_session()
    try:
        run = session.query(BacktestRun).filter_by(job_id=batch_or_job_id).first()
        if run is None:
            runs = (
                session.query(BacktestRun)
                .filter_by(batch_id=batch_or_job_id)
                .order_by(BacktestRun.batch_seq.asc())
                .all()
            )
        else:
            runs = [run]

        if not runs:
            return jsonify({"status": "error", "message": "Job not found"}), 404

        metrics = json.loads(runs[0].metrics_json) if runs[0].metrics_json else {}
        trades = session.query(BacktestTrade).filter_by(job_id=runs[0].job_id).limit(100).all()
        runs_view = [
            {
                "job_id": r.job_id,
                "interval": r.interval,
                "status": r.status,
                "metrics": json.loads(r.metrics_json) if r.metrics_json else {},
            }
            for r in runs
        ]

        html_template = """
        <!DOCTYPE html>
        <html>
        <head>
            <title>Backtest Tearsheet - {{ run.name }}</title>
            <style>
                body { font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, Helvetica, Arial, sans-serif; background: #0f172a; color: #f8fafc; padding: 24px; }
                .card { background: #1e293b; border-radius: 8px; padding: 20px; margin-bottom: 20px; border: 1px solid #334155; }
                h1 { color: #38bdf8; margin-top: 0; }
                table { width: 100%; border-collapse: collapse; margin-top: 12px; }
                th, td { text-align: left; padding: 10px; border-bottom: 1px solid #334155; }
                th { color: #94a3b8; font-weight: 600; }
                .grid { display: grid; grid-template-columns: repeat(4, 1fr); gap: 16px; }
                .metric-box { background: #0f172a; padding: 12px; border-radius: 6px; }
                .metric-label { font-size: 12px; color: #94a3b8; }
                .metric-val { font-size: 20px; font-weight: bold; margin-top: 4px; color: #38bdf8; }
                a { color: #38bdf8; }
            </style>
        </head>
        <body>
            <div class="card">
                <h1>OpenAlgo Backtest Tearsheet</h1>
                <p><strong>Strategy Name:</strong> {{ run.name }} | <strong>Job ID:</strong> {{ run.job_id }}</p>
                <p><strong>Symbols:</strong> {{ run.symbols }} | <strong>Interval:</strong> {{ run.interval }}</p>
            </div>
            {% if runs_view|length > 1 %}
            <div class="card">
                <h2>Multi-Timeframe Comparison Matrix</h2>
                <table>
                    <thead>
                        <tr>
                            <th>Timeframe</th>
                            <th>Status</th>
                            <th>Return %</th>
                            <th>CAGR %</th>
                            <th>Sharpe</th>
                            <th>Sortino</th>
                            <th>Max DD %</th>
                            <th>Win %</th>
                            <th>PF</th>
                            <th>Trades</th>
                            <th>Expectancy</th>
                            <th>Avg Hold (min)</th>
                        </tr>
                    </thead>
                    <tbody>
                        {% for r in runs_view %}
                        <tr>
                            <td><a href="/backtest/api/tearsheet/{{ r.job_id }}">{{ r.interval }}</a></td>
                            <td>{{ r.status }}</td>
                            <td>{{ r.metrics.get('total_return_pct', 0) }}</td>
                            <td>{{ r.metrics.get('cagr_pct', 0) }}</td>
                            <td>{{ r.metrics.get('sharpe_ratio', 0) }}</td>
                            <td>{{ r.metrics.get('sortino_ratio', 0) }}</td>
                            <td>{{ r.metrics.get('max_drawdown_pct', 0) }}</td>
                            <td>{{ r.metrics.get('win_rate_pct', 0) }}</td>
                            <td>{{ r.metrics.get('profit_factor', 0) }}</td>
                            <td>{{ r.metrics.get('total_trades', 0) }}</td>
                            <td>{{ r.metrics.get('expectancy', 0) }}</td>
                            <td>{{ r.metrics.get('avg_holding_minutes', 0) }}</td>
                        </tr>
                        {% endfor %}
                    </tbody>
                </table>
            </div>
            {% endif %}
            <div class="card">
                <h2>Performance Summary</h2>
                <div class="grid">
                    <div class="metric-box"><div class="metric-label">Total Return</div><div class="metric-val">{{ metrics.get('total_return_pct', 0) }}%</div></div>
                    <div class="metric-box"><div class="metric-label">CAGR</div><div class="metric-val">{{ metrics.get('cagr_pct', 0) }}%</div></div>
                    <div class="metric-box"><div class="metric-label">Sharpe Ratio</div><div class="metric-val">{{ metrics.get('sharpe_ratio', 0) }}</div></div>
                    <div class="metric-box"><div class="metric-label">Max Drawdown</div><div class="metric-val">{{ metrics.get('max_drawdown_pct', 0) }}%</div></div>
                    <div class="metric-box"><div class="metric-label">Win Rate</div><div class="metric-val">{{ metrics.get('win_rate_pct', 0) }}%</div></div>
                    <div class="metric-box"><div class="metric-label">Profit Factor</div><div class="metric-val">{{ metrics.get('profit_factor', 0) }}</div></div>
                    <div class="metric-box"><div class="metric-label">Total Trades</div><div class="metric-val">{{ metrics.get('total_trades', 0) }}</div></div>
                    <div class="metric-box"><div class="metric-label">Expectancy</div><div class="metric-val">{{ metrics.get('expectancy', 0) }}</div></div>
                </div>
            </div>
            <div class="card">
                <h2>Recent Trades (Top 100)</h2>
                <table>
                    <thead>
                        <tr>
                            <th>Action</th>
                            <th>Entry Time</th>
                            <th>Entry Price</th>
                            <th>Exit Time</th>
                            <th>Exit Price</th>
                            <th>P&L</th>
                            <th>P&L %</th>
                        </tr>
                    </thead>
                    <tbody>
                        {% for t in trades %}
                        <tr>
                            <td>{{ t.action }}</td>
                            <td>{{ t.entry_time }}</td>
                            <td>{{ t.entry_price }}</td>
                            <td>{{ t.exit_time }}</td>
                            <td>{{ t.exit_price }}</td>
                            <td style="color: {{ 'green' if t.pnl >= 0 else 'red' }}">{{ t.pnl }}</td>
                            <td style="color: {{ 'green' if t.pnl_pct >= 0 else 'red' }}">{{ t.pnl_pct }}%</td>
                        </tr>
                        {% endfor %}
                    </tbody>
                </table>
            </div>
        </body>
        </html>
        """
        return render_template_string(html_template, run=runs[0], metrics=metrics, trades=trades, runs_view=runs_view)
    except Exception as e:
        logger.exception(f"Error generating tearsheet for {batch_or_job_id}: {e}")
        return jsonify({"status": "error", "message": str(e)}), 500
    finally:
        db_session.remove()


# CRUD for Strategy Library
@backtest_bp.route("/strategies", methods=["GET"])
@check_session_validity
def list_saved_strategies():
    session = db_session()
    try:
        strats = session.query(BacktestStrategy).order_by(BacktestStrategy.updated_at.desc()).all()
        results = [{
            "id": s.id,
            "name": s.name,
            "description": s.description,
            "strategy_code": s.strategy_code,
            "created_at": s.created_at.isoformat() if s.created_at else "",
            "updated_at": s.updated_at.isoformat() if s.updated_at else "",
        } for s in strats]
        return jsonify({"status": "success", "strategies": results})
    except Exception as e:
        logger.exception(f"Error listing saved strategies: {e}")
        return jsonify({"status": "error", "message": str(e)}), 500
    finally:
        db_session.remove()


@backtest_bp.route("/strategies", methods=["POST"])
@check_session_validity
def create_saved_strategy():
    data = request.get_json() or {}
    name = data.get("name")
    strategy_code = data.get("strategy_code")
    description = data.get("description", "")

    if not name or not strategy_code:
        return jsonify({"status": "error", "message": "name and strategy_code are required"}), 400

    session = db_session()
    try:
        strat_id = f"strat_{uuid.uuid4().hex[:10]}"
        strat = BacktestStrategy(
            id=strat_id,
            name=name,
            description=description,
            strategy_code=strategy_code,
        )
        session.add(strat)
        session.commit()
        return jsonify({"status": "success", "id": strat_id, "message": "Strategy saved successfully"})
    except Exception as e:
        session.rollback()
        logger.exception(f"Error creating strategy: {e}")
        return jsonify({"status": "error", "message": str(e)}), 500
    finally:
        db_session.remove()


@backtest_bp.route("/strategies/<strat_id>", methods=["DELETE"])
@check_session_validity
def delete_saved_strategy(strat_id):
    session = db_session()
    try:
        session.query(BacktestStrategy).filter_by(id=strat_id).delete()
        session.commit()
        return jsonify({"status": "success", "message": f"Strategy {strat_id} deleted successfully"})
    except Exception as e:
        session.rollback()
        logger.exception(f"Error deleting strategy {strat_id}: {e}")
        return jsonify({"status": "error", "message": str(e)}), 500
    finally:
        db_session.remove()
