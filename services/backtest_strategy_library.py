"""Persistence and validation for saved backtest strategy templates."""

import json
import math
import uuid
from datetime import datetime

import pytz
from sqlalchemy import func

from database.backtest_db import BacktestStrategy

_IST = pytz.timezone("Asia/Kolkata")


def _now():
    """Same clock as the model defaults, so old and new rows sort together."""
    return datetime.now(_IST)


class TemplateExists(Exception):
    """Raised when a template name is already in use."""

    def __init__(self, existing_id):
        self.existing_id = existing_id
        super().__init__("A strategy template with this name already exists")


_STRING_SETTINGS = {"interval", "sizing_type", "product_type", "missing_data_policy"}
_NUMBER_SETTINGS = {
    "initial_capital",
    "sizing_value",
    "slippage_bps",
    "brokerage_bps",
    "stop_loss_pct",
    "take_profit_pct",
    "daily_loss_limit",
}
_ALLOWED_SETTINGS = _STRING_SETTINGS | _NUMBER_SETTINGS | {"max_positions", "engine_options"}


def normalize_template(data, partial=False):
    if not isinstance(data, dict):
        raise ValueError("Request body must be a JSON object")
    result = {}
    required = ("name", "strategy_code")
    for key in required:
        if key not in data:
            if partial:
                continue
            raise ValueError(f"{key} is required")
    if "name" in data:
        name = data["name"]
        if not isinstance(name, str):
            raise ValueError("name must be a string")
        name = name.strip()
        if not 1 <= len(name) <= 128:
            raise ValueError("name must be between 1 and 128 characters")
        result["name"] = name
    if "description" in data:
        description = data["description"]
        if description is not None and not isinstance(description, str):
            raise ValueError("description must be a string")
        description = (description or "").strip()
        if len(description) > 1000:
            raise ValueError("description must be at most 1000 characters")
        result["description"] = description
    if "strategy_code" in data:
        code = data["strategy_code"]
        if not isinstance(code, str):
            raise ValueError("strategy_code must be a string")
        if not code.strip():
            raise ValueError("strategy_code must not be empty")
        if len(code) > 200_000:
            raise ValueError("strategy_code must be at most 200000 characters")
        result["strategy_code"] = code
    if "settings" in data:
        settings = data["settings"]
        if settings is not None and not isinstance(settings, dict):
            raise ValueError("settings must be an object or null")
        if settings is None:
            result["settings"] = None
        else:
            cleaned = {}
            for key, value in settings.items():
                if key not in _ALLOWED_SETTINGS:
                    continue
                if key in _STRING_SETTINGS:
                    if not isinstance(value, str):
                        raise ValueError(f"settings.{key} must be a string")
                elif key in _NUMBER_SETTINGS:
                    if value is not None and (
                        isinstance(value, bool)
                        or not isinstance(value, (int, float))
                        or not math.isfinite(value)
                    ):
                        raise ValueError(f"settings.{key} must be a finite number or null")
                elif key == "max_positions":
                    if value is not None and (
                        isinstance(value, bool) or not isinstance(value, int)
                    ):
                        raise ValueError("settings.max_positions must be an integer or null")
                elif key == "engine_options" and not isinstance(value, dict):
                    raise ValueError("settings.engine_options must be an object")
                cleaned[key] = value
            try:
                encoded = json.dumps(cleaned, allow_nan=False, separators=(",", ":"))
            except (TypeError, ValueError):
                raise ValueError("settings must contain JSON-compatible values") from None
            if len(encoded.encode("utf-8")) > 20_000:
                raise ValueError("settings must be at most 20000 bytes")
            result["settings"] = cleaned
    return result


def _serialize(template):
    try:
        settings = json.loads(template.settings_json) if template.settings_json else None
        if not isinstance(settings, dict):
            settings = None
    except (TypeError, json.JSONDecodeError):
        settings = None
    return {
        "id": template.id,
        "name": template.name,
        "description": template.description,
        "strategy_code": template.strategy_code,
        "settings": settings,
        "created_at": template.created_at.isoformat() if template.created_at else "",
        "updated_at": template.updated_at.isoformat() if template.updated_at else "",
    }


def _settings_json(data, fallback):
    settings = data.get("settings", fallback)
    return (
        json.dumps(settings, allow_nan=False, separators=(",", ":"))
        if settings is not None
        else None
    )


def list_templates(session):
    return [
        _serialize(row)
        for row in session.query(BacktestStrategy)
        .order_by(BacktestStrategy.updated_at.desc())
        .all()
    ]


def get_template(session, template_id):
    row = session.query(BacktestStrategy).filter_by(id=template_id).first()
    if row is None:
        raise KeyError(template_id)
    return _serialize(row)


def create_template(session, data, overwrite=False):
    normalized = normalize_template(data)
    existing = (
        session.query(BacktestStrategy)
        .filter(func.lower(BacktestStrategy.name) == normalized["name"].lower())
        .first()
    )
    now = _now()
    if existing:
        if not overwrite:
            raise TemplateExists(existing.id)
        existing.description = normalized.get("description", "")
        existing.strategy_code = normalized["strategy_code"]
        existing.settings_json = _settings_json(normalized, None)
        existing.updated_at = now
        session.commit()
        return _serialize(existing)
    row = BacktestStrategy(
        id=f"strat_{uuid.uuid4().hex[:10]}",
        name=normalized["name"],
        description=normalized.get("description", ""),
        strategy_code=normalized["strategy_code"],
        settings_json=_settings_json(normalized, None),
        created_at=now,
        updated_at=now,
    )
    session.add(row)
    session.commit()
    return _serialize(row)


def update_template(session, template_id, data):
    normalized = normalize_template(data, partial=True)
    row = session.query(BacktestStrategy).filter_by(id=template_id).first()
    if row is None:
        raise KeyError(template_id)
    if "name" in normalized:
        other = (
            session.query(BacktestStrategy)
            .filter(
                func.lower(BacktestStrategy.name) == normalized["name"].lower(),
                BacktestStrategy.id != template_id,
            )
            .first()
        )
        if other:
            raise TemplateExists(other.id)
        row.name = normalized["name"]
    for key in ("description", "strategy_code"):
        if key in normalized:
            setattr(row, key, normalized[key])
    if "settings" in normalized:
        row.settings_json = _settings_json(normalized, None)
    row.updated_at = _now()
    session.commit()
    return _serialize(row)


def delete_template(session, template_id):
    row = session.query(BacktestStrategy).filter_by(id=template_id).first()
    if row is None:
        return False
    session.delete(row)
    session.commit()
    return True
