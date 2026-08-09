"""
services/depth_analysis.py
───────────────────────────
Layer-1 metrics engine for the Depth Recorder.

Loads one row per stored depth tick, computes the order-book micro-metrics
vectorised per tick, then resamples to 1-second buckets with a per-variable
aggregation rule and 5-second gap masking (a hole is masked, never
forward-filled).

Per-tick metrics:
    mid, spread, spread_tk, microprice
    obi_l1 / obi_ln / obi_tot / divergence
    ofi_1        Cont-Kukanov-Stoikov, best level
    signed_vol   Lee-Ready signed volume delta (tick-rule fallback)
    cvd          cumulative signed delta

Resampling rules:
    state  -> last   (prices, quantities, order counts, OBIs, mid, microprice)
    flow   -> sum    (ofi_1, signed_vol, churn, volume)
    width  -> mean   (spread, spread_tk)

The session frame is cached with a short TTL; a live recording session ages
out and is rebuilt on the next request.
"""

from __future__ import annotations

import math
import re
import threading
from collections import OrderedDict
from datetime import datetime, timedelta

import numpy as np
import pandas as pd

from database.depth_recorder_db import get_session_ticks_flat
from utils.logging import get_logger

logger = get_logger(__name__)

# ---------------------------------------------------------------------------
# Tuning knobs
# ---------------------------------------------------------------------------
MAX_CACHE_ENTRIES = 32
CACHE_TTL_SECONDS = 30.0
GAP_WINDOW_SECONDS = 5.0
DEFAULT_TICK_SIZE = 0.05
RESAMPLE_RULE = "1s"

_session_cache: OrderedDict[tuple, dict] = OrderedDict()
_cache_lock = threading.Lock()


def _cache_key(symbol: str, exchange: str, day: str) -> tuple:
    return (symbol.upper(), exchange.upper(), day)


def _cache_set(symbol: str, exchange: str, day: str, value: dict) -> None:
    k = _cache_key(symbol, exchange, day)
    with _cache_lock:
        _session_cache[k] = value
        _session_cache.move_to_end(k)
        while len(_session_cache) > MAX_CACHE_ENTRIES:
            _session_cache.popitem(last=False)


def _cache_get(symbol: str, exchange: str, day: str) -> dict | None:
    k = _cache_key(symbol, exchange, day)
    with _cache_lock:
        hit = _session_cache.get(k)
        if hit is None:
            return None
        age = (datetime.now() - hit["built_at"]).total_seconds()
        if age > CACHE_TTL_SECONDS:
            _session_cache.pop(k, None)
            return None
        _session_cache.move_to_end(k)
        return hit


# ---------------------------------------------------------------------------
# Session inventory
# ---------------------------------------------------------------------------
def list_sessions(exchange: str = "", symbol: str = "") -> list[dict]:
    """One entry per distinct (symbol, exchange, day). Newest first."""
    from database.depth_recorder_db import DepthTick
    from database.depth_recorder_db import db_session as dr_session

    try:
        rows = dr_session.query(DepthTick.symbol, DepthTick.exchange, DepthTick.tick_time).all()
    except Exception:
        logger.exception("depth_analysis: list_sessions query failed")
        return []

    groups: dict[tuple, dict] = {}
    for symbol_row, exchange_row, ts in rows:
        if exchange and exchange_row.upper() != exchange.upper():
            continue
        if symbol and symbol_row.upper() != symbol.upper():
            continue
        day = ts.date().isoformat()
        k = (symbol_row, exchange_row, day)
        g = groups.setdefault(
            k,
            {
                "symbol": symbol_row,
                "exchange": exchange_row,
                "day": day,
                "first": ts,
                "last": ts,
                "n_ticks": 0,
            },
        )
        g["n_ticks"] += 1
        if ts < g["first"]:
            g["first"] = ts
        if ts > g["last"]:
            g["last"] = ts

    out = []
    for g in groups.values():
        out.append(
            {
                "symbol": g["symbol"],
                "exchange": g["exchange"],
                "day": g["day"],
                "n_ticks": g["n_ticks"],
                "first": g["first"].isoformat(),
                "last": g["last"].isoformat(),
            }
        )
    out.sort(key=lambda s: (s["day"], s["symbol"]), reverse=True)
    return out


# ---------------------------------------------------------------------------
# Ingestion
# ---------------------------------------------------------------------------
def _load_session(symbol: str, exchange: str, day: str) -> pd.DataFrame:
    """Load one session of raw ticks as a DataFrame (old -> new).

    Rows come pre-flattened (bid1_p ... ask5_o) straight from the ORM — no
    nested bids/asks intermediate dicts.
    """
    day = day.split("T")[0]
    start = datetime.fromisoformat(f"{day}T00:00:00")
    end = start + timedelta(days=1)

    ticks = get_session_ticks_flat(symbol, exchange, start, end)
    if not ticks:
        return pd.DataFrame()

    df = pd.DataFrame(ticks)
    df.sort_values("tick_time", inplace=True)
    df.reset_index(drop=True, inplace=True)
    return df


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _obi(long: pd.Series, short: pd.Series) -> pd.Series:
    """(long - short) / (long + short); 0/0 -> NaN."""
    long = pd.to_numeric(long, errors="coerce").fillna(0.0)
    short = pd.to_numeric(short, errors="coerce").fillna(0.0)
    denom = long + short
    return (long - short) / denom.replace(0, np.nan)


# ---------------------------------------------------------------------------
# Per-tick metrics
# ---------------------------------------------------------------------------
def compute_tick_metrics(df: pd.DataFrame) -> pd.DataFrame:
    """Add per-tick columns: mid, spread, spread_tk, microprice, OBI, divergence."""
    if df.empty:
        return df
    out = df.copy()

    bp1 = out["bid1_p"]
    ap1 = out["ask1_p"]
    bq1 = out["bid1_q"].fillna(0)
    aq1 = out["ask1_q"].fillna(0)

    out["mid"] = ((bp1 + ap1) / 2.0).replace(0, np.nan)
    out["spread"] = (ap1 - bp1).abs()
    out["spread_tk"] = out["spread"] / DEFAULT_TICK_SIZE

    # Microprice: each side weighted by the OPPOSITE side's quantity.
    with np.errstate(invalid="ignore", divide="ignore"):
        denom = bq1 + aq1
        micro = (bp1 * aq1 + ap1 * bq1) / denom.replace(0, np.nan)
    out["microprice"] = micro

    out["obi_l1"] = _obi(bq1, aq1)

    sum_bq = out.filter(regex=r"^bid\d_q$").apply(pd.to_numeric, errors="coerce").sum(axis=1)
    sum_aq = out.filter(regex=r"^ask\d_q$").apply(pd.to_numeric, errors="coerce").sum(axis=1)
    out["obi_ln"] = _obi(sum_bq, sum_aq)

    out["obi_tot"] = _obi(out["total_buy_qty"], out["total_sell_qty"])
    out["divergence"] = out["obi_ln"] - out["obi_tot"]
    return out


def compute_ofi_cvd(df: pd.DataFrame) -> pd.DataFrame:
    """Add ofi_1, signed_vol, cvd columns (computed on the tick series)."""
    if df.empty:
        return df
    out = df.copy()

    bp = out["bid1_p"]
    bq = out["bid1_q"]
    ap = out["ask1_p"]
    aq = out["ask1_q"]

    bp_prev = bp.shift(1)
    bq_prev = bq.shift(1).fillna(0)
    ap_prev = ap.shift(1)
    aq_prev = aq.shift(1).fillna(0)
    bq_now = bq.fillna(0)
    aq_now = aq.fillna(0)

    # ---- OFI (Cont-Kukanov-Stoikov, best level) --------------------------
    # bid side: improved -> all new; same -> net change; wiped -> -prev qty
    e_b = pd.Series(0.0, index=out.index, dtype=float)
    e_b = e_b.where(bp_prev.notna(), np.nan)  # first row: no history
    e_b = e_b.mask((bp > bp_prev) & bp_prev.notna(), bq_now)
    e_b = e_b.mask((bp == bp_prev) & bp_prev.notna(), bq_now - bq_prev)
    e_b = e_b.mask(bp.isna() & bp_prev.notna(), -bq_prev)

    # ask side mirrored: improving ask means price DOWN
    e_a = pd.Series(0.0, index=out.index, dtype=float)
    e_a = e_a.where(ap_prev.notna(), np.nan)
    e_a = e_a.mask((ap < ap_prev) & ap_prev.notna(), aq_now)
    e_a = e_a.mask((ap == ap_prev) & ap_prev.notna(), aq_now - aq_prev)
    e_a = e_a.mask(ap.isna() & ap_prev.notna(), -aq_prev)

    out["ofi_1"] = e_b - e_a

    # ---- CVD (Lee-Ready, tick-rule fallback) ------------------------------
    volume = out["volume"]
    if volume is None or volume.isnull().all():
        out["signed_vol"] = 0.0
        out["cvd"] = 0.0
        return out

    dv = volume.diff().fillna(0.0)
    ltp = out["ltp"]
    ask_prev = ap.shift(1)
    bid_prev = bp.shift(1)

    sign = pd.Series(0.0, index=out.index, dtype=float)
    up = (ltp >= ask_prev) & ask_prev.notna() & ltp.notna()
    down = (ltp <= bid_prev) & bid_prev.notna() & ltp.notna()
    sign = sign.mask(up, 1.0)
    sign = sign.mask(down, -1.0)
    # tick-rule fallback: carry the previous sign forward
    sign = sign.mask(~(up | down), sign.shift(1).fillna(0))

    signed = (sign * dv).fillna(0)
    out["signed_vol"] = signed
    out["cvd"] = signed.cumsum()
    return out


def _churn_col(df: pd.DataFrame) -> pd.Series:
    """Flag ticks where the top level price is unchanged but qty changed
    (order refresh without net size change)."""
    same_p = df["bid1_p"] == df["bid1_p"].shift()
    qty_chg = df["bid1_q"].fillna(0).diff().abs() > 0
    return (same_p & qty_chg).astype(int)


# ---------------------------------------------------------------------------
# Resampling
# ---------------------------------------------------------------------------
def resample_frame(df: pd.DataFrame) -> pd.DataFrame:
    """1-second resample with per-variable aggregation and gap masking."""
    if df.empty:
        return df

    out = df.copy()
    out["ts"] = pd.to_datetime(out["tick_time"])
    out = out.set_index("ts").sort_index()
    out["churn"] = _churn_col(out)

    state_cols = [c for c in out.columns if re.search(r"_(p|q|o)$", c)]
    state_cols += ["mid", "microprice", "obi_l1", "obi_ln", "obi_tot", "ltt"]
    state_cols = [c for c in state_cols if c in out.columns]
    flow_cols = [c for c in ("ofi_1", "signed_vol", "churn", "volume") if c in out.columns]
    width_cols = [c for c in ("spread", "spread_tk") if c in out.columns]

    agg = {}
    for c in state_cols:
        agg[c] = "last"
    for c in flow_cols:
        agg[c] = "sum"
    for c in width_cols:
        agg[c] = "mean"

    g = out.resample(RESAMPLE_RULE)
    r = g.agg(agg)
    if "ltp" in out.columns:
        r["ltp"] = g["ltp"].last()
    if "cvd" in out.columns:
        r["cvd"] = g["cvd"].last()

    # ---- gap masking -----------------------------------------------------
    # buckets whose leading interval exceeds the window are a feed hole.
    # Mask all state columns after the first hole; never forward-fill through.
    dt = r.index.to_series().diff()
    hole = dt > pd.Timedelta(seconds=GAP_WINDOW_SECONDS)
    after_first_hole = hole.cumsum() > 0
    keep = ~after_first_hole
    for c in r.columns:
        r[c] = r[c].where(keep, np.nan)

    r.reset_index(inplace=True)
    r.rename(columns={"ts": "time"}, inplace=True)
    r["epoch"] = r["time"].astype("int64") // 1_000_000_000
    return r


# ---------------------------------------------------------------------------
# Session frame (cached)
# ---------------------------------------------------------------------------
def compute_session(symbol: str, exchange: str, day: str) -> dict:
    """Build (or fetch) the analytical frame for one session."""
    cached = _cache_get(symbol, exchange, day)
    if cached:
        return cached

    raw = _load_session(symbol, exchange, day)
    if raw.empty:
        return {
            "status": "no_data",
            "symbol": symbol.upper(),
            "exchange": exchange.upper(),
            "day": day.split("T")[0],
        }

    metric = compute_tick_metrics(raw)
    metric = compute_ofi_cvd(metric)
    frame = resample_frame(metric)

    has_orders = bool(
        frame.filter(regex=r"^bid1_o$").notna().any().any() if "bid1_o" in frame.columns else False
    )
    result = {
        "status": "ok",
        "symbol": symbol.upper(),
        "exchange": exchange.upper(),
        "day": day.split("T")[0],
        "ticks": int(len(raw)),
        "rows": int(len(frame)),
        "has_orders": has_orders,
        "built_at": datetime.now(),
        "frame": frame,
        "first": raw["tick_time"].iloc[0].isoformat(),
        "last": raw["tick_time"].iloc[-1].isoformat(),
    }
    _cache_set(symbol, exchange, day, result)
    return result


def get_resampled_frame(symbol: str, exchange: str, day: str) -> pd.DataFrame:
    """Return only the resampled frame (empty DataFrame on no data)."""
    session = compute_session(symbol, exchange, day)
    if session.get("status") != "ok":
        return pd.DataFrame()
    return session["frame"]


def metrics_series(symbol: str, exchange: str, day: str, max_points: int = 12000) -> dict:
    """Compact per-column series for the charting/replay frontend.

    Returns one flat array per column sharing a common ``time`` axis (epoch
    seconds), downsampled to at most ``max_points`` rows by simple striding.
    ``ladder`` shapes each row's [bids, asks] as page-friendly arrays.
    """
    session = compute_session(symbol, exchange, day)
    if session.get("status") != "ok":
        return {"status": "no_data"}
    frame = session["frame"]
    if frame.empty:
        return {"status": "no_data"}

    n = len(frame)
    if n > max_points:
        stride = math.ceil(n / max_points)
        frame = frame.iloc[::stride].reset_index(drop=True)
        n = len(frame)

    # naive column order (no int32 NaN holes in JSON) — emit best-effort
    numeric_cols = ["ltp", "mid", "microprice", "spread", "spread_tk"]
    numeric_cols += ["obi_l1", "obi_ln", "obi_tot", "divergence"]
    numeric_cols += ["ofi_1", "cvd", "volume", "ltt"]
    for i in range(1, 6):
        numeric_cols += [f"bid{i}_p", f"bid{i}_q", f"bid{i}_o"]
        numeric_cols += [f"ask{i}_p", f"ask{i}_q", f"ask{i}_o"]

    series = {"time": frame["epoch"].astype("int64").tolist()}
    for c in numeric_cols:
        if c not in frame.columns:
            series[c] = []
            continue
        series[c] = [None if v is None else (None if _isnan(v) else float(v)) for v in frame[c]]

    n = len(frame)

    # ladder: one [price, quantity, orders] triplet per level per row.
    # Vectorised column extract (nan where a level is absent — JSON-safe,
    # the frontend renders nan as empty).
    def _col(col: str) -> np.ndarray:
        if col not in frame.columns:
            return np.full(n, np.nan)
        return pd.to_numeric(frame[col], errors="coerce").to_numpy(dtype="float64")

    bid_p = np.column_stack([_col(f"bid{i}_p") for i in range(1, 6)])
    bid_q = np.column_stack([_col(f"bid{i}_q") for i in range(1, 6)])
    bid_o = np.column_stack([_col(f"bid{i}_o") for i in range(1, 6)])
    ask_p = np.column_stack([_col(f"ask{i}_p") for i in range(1, 6)])
    ask_q = np.column_stack([_col(f"ask{i}_q") for i in range(1, 6)])
    ask_o = np.column_stack([_col(f"ask{i}_o") for i in range(1, 6)])

    def ladder_row(j: int) -> dict:
        bids = [
            {
                "price": float(bid_p[j, i]),
                "quantity": float(bid_q[j, i]),
                "orders": float(bid_o[j, i]),
            }
            for i in range(5)
        ]
        asks = [
            {
                "price": float(ask_p[j, i]),
                "quantity": float(ask_q[j, i]),
                "orders": float(ask_o[j, i]),
            }
            for i in range(5)
        ]
        return {"bids": bids, "asks": asks}

    ladder = [ladder_row(j) for j in range(n)]

    return {
        "status": "ok",
        "symbol": session["symbol"],
        "exchange": session["exchange"],
        "day": session["day"],
        "rows": len(frame),
        "series": series,
        "ladder": ladder,
        "has_orders": session["has_orders"],
        "first": session["first"],
        "last": session["last"],
    }


def _isnan(v) -> bool:
    try:
        return bool(math.isnan(float(v)))
    except (TypeError, ValueError):
        return False


# ---------------------------------------------------------------------------
# Blueprint-facing reports
# ---------------------------------------------------------------------------
def report_health(symbol: str, exchange: str, day: str) -> dict:
    """Packets per minute + gap table for one session."""
    raw = _load_session(symbol, exchange, day)
    if raw.empty:
        return {"status": "no_data", "symbol": symbol, "exchange": exchange, "day": day}

    times = pd.to_datetime(raw["tick_time"])
    per_min = times.dt.floor("min").value_counts().sort_index()
    packets_per_min = [{"minute": ts.isoformat(), "packets": int(n)} for ts, n in per_min.items()]

    gaps = []
    prev = None
    for ts in times:
        if prev is not None:
            gap_s = (ts - prev).total_seconds()
            if gap_s > 3:
                gaps.append(
                    {"from": prev.isoformat(), "to": ts.isoformat(), "duration_s": round(gap_s, 2)}
                )
        prev = ts

    return {
        "status": "ok",
        "symbol": symbol.upper(),
        "exchange": exchange.upper(),
        "day": day.split("T")[0],
        "total_packets": int(len(raw)),
        "packets_per_min": packets_per_min,
        "gaps": gaps,
    }


def _time_bucket_of(epoch: int) -> str:
    dt = pd.to_datetime(epoch, unit="s")
    h = dt.hour + dt.minute / 60.0
    if h < 9.75:
        return "open"
    if h < 14.75:
        return "midday"
    return "close"


BUCKET_LABELS = {
    "open": "Open (09:15-09:45)",
    "midday": "Midday",
    "close": "Close (14:45-15:30)",
}


def leadlag_report(
    symbol: str,
    exchange: str,
    day: str,
    horizons: list[int] | None = None,
    max_points: int = 800,
) -> dict:
    """OFI vs forward mid-return, Pearson/Spearman per horizon and time band."""
    if horizons is None:
        horizons = [1, 10, 30, 120]
    session = compute_session(symbol, exchange, day)
    if session.get("status") != "ok":
        return {"status": "no_data"}

    f = session["frame"].copy()
    f = f.dropna(subset=["mid", "ofi_1"])
    if len(f) < 20:
        return {"status": "no_data", "reason": "insufficient rows"}

    f["bucket"] = f["epoch"].map(_time_bucket_of)
    f.reset_index(drop=True, inplace=True)

    results = []
    for h in horizons:
        fwd = f["mid"].pct_change(periods=-h)
        for bucket in ("open", "midday", "close"):
            idx = f["bucket"] == bucket
            x = f.loc[idx, "ofi_1"]
            y = fwd[idx]
            valid = x.notna() & y.notna()
            n = int(valid.sum())
            # correlation is undefined for a constant input; skip it
            if n > 10 and x[valid].nunique() > 1 and y[valid].nunique() > 1:
                pearson = _clean(x[valid].corr(y[valid]))
                spearman = _clean(x[valid].corr(y[valid], method="spearman"))
            else:
                pearson = spearman = None
            results.append(
                {
                    "horizon": h,
                    "bucket": bucket,
                    "bucket_label": BUCKET_LABELS[bucket],
                    "pearson": pearson,
                    "spearman": spearman,
                    "n": n,
                }
            )

    # scatter sample for the 10s horizon (most commonly used)
    h10 = f["mid"].pct_change(periods=-10)
    sample = f[["epoch", "ofi_1"]].copy()
    sample["fwd"] = h10
    sample = sample.dropna(subset=["ofi_1", "fwd"])
    if len(sample) > max_points:
        sample = sample.sample(max_points, random_state=7)
    scatter = [
        {"epoch": int(r.epoch), "x": float(r.ofi_1), "y": float(r.fwd)}
        for _, r in sample.iterrows()
    ]

    return {
        "status": "ok",
        "symbol": session["symbol"],
        "exchange": session["exchange"],
        "day": session["day"],
        "horizons": horizons,
        "results": results,
        "scatter": scatter,
    }


def _clean(v) -> float | None:
    if v is None:
        return None
    if isinstance(v, float) and not math.isfinite(v):
        return None
    return round(float(v), 4)


def walls_report(symbol: str, exchange: str, day: str, top: int = 8) -> dict:
    """Top N price levels by time-weighted resting size + avg order size."""
    session = compute_session(symbol, exchange, day)
    if session.get("status") != "ok":
        return {"status": "no_data"}
    raw = session["frame"] if "frame" in session else pd.DataFrame()
    if raw.empty:
        return {"status": "no_data"}

    # Use the resampled frame: time-weighted = mean resting qty at each price.
    # Vectorised: one bincount pass over all 10 levels instead of a Python
    # loop per row x per level (a full 1s session is ~23k rows x 10).
    price_parts: list[np.ndarray] = []
    qty_parts: list[np.ndarray] = []
    ord_parts: list[np.ndarray] = []

    for i in range(1, 6):
        for side in ("bid", "ask"):
            price_col = f"{side}{i}_p"
            qty_col = f"{side}{i}_q"
            ord_col = f"{side}{i}_o"
            if price_col not in raw.columns:
                continue
            p = pd.to_numeric(raw[price_col], errors="coerce").to_numpy(dtype="float64")
            q = pd.to_numeric(raw[qty_col], errors="coerce").to_numpy(dtype="float64")
            valid = np.isfinite(p) & np.isfinite(q)
            if not valid.any():
                continue
            o = pd.to_numeric(raw[ord_col], errors="coerce").to_numpy(dtype="float64")
            o = np.where(np.isfinite(o), o, 0.0)
            price_parts.append(p[valid])
            qty_parts.append(q[valid])
            ord_parts.append(o[valid])

    if not price_parts:
        return {
            "status": "ok",
            "symbol": session["symbol"],
            "exchange": session["exchange"],
            "day": session["day"],
            "walls": [],
            "total_levels": 0,
        }

    prices = np.concatenate(price_parts)
    qty = np.concatenate(qty_parts)
    orders = np.concatenate(ord_parts)
    rounded = np.round(prices, 2)
    uniq, inv = np.unique(rounded, return_inverse=True)

    qty_sum = np.bincount(inv, weights=qty)
    orders_sum = np.bincount(inv, weights=orders)
    counts = np.bincount(inv)

    walls = []
    for j in range(len(uniq)):
        n = int(counts[j])
        if n == 0:
            continue
        avg_qty = float(qty_sum[j]) / n
        avg_orders = float(orders_sum[j]) / n if orders_sum[j] else None
        walls.append(
            {
                "price": float(uniq[j]),
                "time_weighted_qty": round(avg_qty, 2),
                "avg_order_size": round(avg_qty / avg_orders, 2) if avg_orders else None,
                "samples": n,
            }
        )

    walls.sort(key=lambda w: w["time_weighted_qty"], reverse=True)
    return {
        "status": "ok",
        "symbol": session["symbol"],
        "exchange": session["exchange"],
        "day": session["day"],
        "walls": walls[:top],
        "total_levels": len(walls),
    }


# ---------------------------------------------------------------------------
# Liquidity heatmap (server-rendered PNG for interactive canvas overlay)
# ---------------------------------------------------------------------------
_HEATMAP_WIDTH = 1200
_HEATMAP_HEIGHT = 420


def _pool_grid(z: np.ndarray, max_rows: int, max_cols: int) -> np.ndarray:
    """Mean-pool a 2D grid down to at most (max_rows x max_cols) cells.

    Keeps the colour-map math proportional to the output image pixel count
    instead of the (potentially huge) price-bin grid. Edge-pads leftovers so
    pooled cells stay equal-sized.
    """
    r, c = z.shape
    rr = 1 if r <= max_rows else math.ceil(r / max_rows)
    cc = 1 if c <= max_cols else math.ceil(c / max_cols)
    if rr == 1 and cc == 1:
        return z
    rp = math.ceil(r / rr) * rr
    cp = math.ceil(c / cc) * cc
    padded = np.pad(z, ((0, rp - r), (0, cp - c)), mode="edge")
    return padded.reshape(rp // rr, rr, cp // cc, cc).mean(axis=(1, 3))


def render_heatmap_png(
    exchange: str, symbol: str, day: str, step: float | None = None
) -> tuple[bytes, dict]:
    """Render a time x price liquidity heatmap for a session as PNG.

    Returns (png_bytes, meta). meta carries the price axis and time bucket
    labels so the frontend can overlay a live price line and crosshair on the
    image. Log-scaled and clipped at the 95th percentile of resting quantity.
    """
    from io import BytesIO

    from PIL import Image

    step = step or DEFAULT_TICK_SIZE
    session = compute_session(symbol, exchange, day)
    if session.get("status") != "ok":
        return b"", {"status": "no_data"}
    frame = session["frame"]
    if frame.empty or "mid" not in frame.columns:
        return b"", {"status": "no_data"}

    mid = frame["mid"].dropna()
    if mid.empty:
        return b"", {"status": "no_data"}

    pmin, pmax = float(mid.min()), float(mid.max())
    pad = max((pmax - pmin) * 0.02, step * 2)
    pmin, pmax = pmin - pad, pmax + pad
    nbins = int(math.ceil((pmax - pmin) / step))

    # time buckets: 30s bins
    ts = pd.to_datetime(frame["time"])
    bucket = ts.dt.floor("30s")
    uniq_times = bucket.unique()
    # factorize order == appearance order == uniq_times order
    ti, _ = pd.factorize(bucket)
    ti = ti.astype("int64")

    grid = np.zeros((len(uniq_times), nbins))
    for i in range(1, 6):
        for side in ("bid", "ask"):
            pcol = f"{side}{i}_p"
            qcol = f"{side}{i}_q"
            if pcol not in frame.columns or qcol not in frame.columns:
                continue
            p = pd.to_numeric(frame[pcol], errors="coerce").to_numpy(dtype="float64")
            q = pd.to_numeric(frame[qcol], errors="coerce").to_numpy(dtype="float64")
            valid = np.isfinite(p) & np.isfinite(q)
            if not valid.any():
                continue
            bi = np.floor((p[valid] - pmin) / step).astype(np.int64)
            bi = np.clip(bi, 0, nbins - 1)
            np.add.at(grid, (ti[valid], bi), q[valid])

    # log-scale + clip at 95th pct so one liquidity wall does not saturate
    vals = grid[grid > 0]
    cap = float(np.percentile(vals, 95)) if vals.size else 1.0
    cap = max(cap, 1.0)
    z = np.log1p(np.clip(grid, 0, cap))
    zmax = float(z.max()) if z.max() > 0 else 1.0

    # colour ramp dark -> deep blue -> cyan -> amber -> red, fully vectorised:
    # build the (time x price x rgb) buffer with numpy and let PIL rasterise
    # it instead of one draw.rectangle() call per grid cell. The grid is pooled
    # down to the output resolution first so the colour math never touches
    # more cells than the final image has pixels.
    ramps = np.array(
        [
            [10, 14, 19],
            [10, 31, 60],
            [21, 94, 152],
            [59, 130, 246],
            [34, 229, 199],
            [245, 194, 66],
            [248, 93, 57],
        ],
        dtype="float64",
    )
    zq = _pool_grid(z, _HEATMAP_HEIGHT, _HEATMAP_WIDTH)
    pos = np.clip(zq / zmax * (len(ramps) - 1), 0.0, len(ramps) - 2)
    idx0 = pos.astype(np.int64)
    frac = (pos - idx0)[..., None]
    colored = (ramps[idx0] + (ramps[idx0 + 1] - ramps[idx0]) * frac).astype(np.uint8)
    bg = np.array([10, 14, 19], dtype=np.uint8)
    colored = np.where((zq > 0)[..., None], colored, bg)

    img = Image.fromarray(colored, "RGB")
    if img.size != (_HEATMAP_WIDTH, _HEATMAP_HEIGHT):
        img = img.resize((_HEATMAP_WIDTH, _HEATMAP_HEIGHT), Image.NEAREST)

    buf = BytesIO()
    img.save(buf, format="PNG")
    meta = {
        "status": "ok",
        "price_min": round(pmin, 4),
        "price_max": round(pmax, 4),
        "tick_size": step,
        "price_bins": nbins,
        "time_buckets": len(uniq_times),
        "bucket_seconds": 30,
        "times": [pd.Timestamp(t).strftime("%H:%M:%S") for t in uniq_times],
        "prices": [round(pmin + i * step, 4) for i in range(nbins)],
    }
    return buf.getvalue(), meta
