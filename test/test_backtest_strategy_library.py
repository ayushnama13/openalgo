import os
import subprocess
import sys
from datetime import datetime, timedelta

import pytest
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.orm import sessionmaker

from database.backtest_db import BacktestStrategy
from services.backtest_strategy_library import (
    TemplateExists,
    create_template,
    delete_template,
    get_template,
    list_templates,
    normalize_template,
    update_template,
)


def _session(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'strategies.db'}")
    BacktestStrategy.__table__.create(engine)
    return sessionmaker(bind=engine)(), engine


def _data(name="Alpha", **kwargs):
    return {"name": name, "strategy_code": "print('hello')", **kwargs}


def test_validation_and_unknown_settings_drop(tmp_path):
    with pytest.raises(ValueError, match="name"):
        normalize_template(_data("   "))
    with pytest.raises(ValueError, match="strategy_code"):
        normalize_template({"name": "A", "strategy_code": " "})
    with pytest.raises(ValueError, match="finite"):
        normalize_template(_data(settings={"initial_capital": float("nan")}))
    with pytest.raises(ValueError, match="string"):
        normalize_template(_data(description=12))
    normalized = normalize_template(_data(settings={"interval": "5m", "ignored": "value"}))
    assert normalized["settings"] == {"interval": "5m"}
    session, engine = _session(tmp_path)
    saved = create_template(session, _data(settings={"interval": "5m", "ignored": "value"}))
    assert saved["settings"] == {"interval": "5m"}
    session.close()
    engine.dispose()


def test_duplicate_overwrite_rename_order_and_delete(tmp_path):
    session, engine = _session(tmp_path)
    first = create_template(session, _data("Alpha", description="first"))
    with pytest.raises(TemplateExists) as exc:
        create_template(session, _data("alpha"))
    assert exc.value.existing_id == first["id"]
    replaced = create_template(
        session, _data("ALPHA", description="changed", settings={"sizing_value": 2}), overwrite=True
    )
    assert replaced["id"] == first["id"]
    assert replaced["description"] == "changed"
    assert replaced["settings"] == {"sizing_value": 2}
    second = create_template(session, _data("Beta"))
    with pytest.raises(TemplateExists) as exc:
        update_template(session, second["id"], {"name": "alpha"})
    assert exc.value.existing_id == first["id"]
    first_row = session.query(BacktestStrategy).filter_by(id=first["id"]).one()
    first_row.updated_at = datetime.now() - timedelta(days=1)
    session.commit()
    update_template(session, second["id"], {"description": "new"})
    assert [s["id"] for s in list_templates(session)] == [second["id"], first["id"]]
    assert get_template(session, first["id"])["id"] == first["id"]
    assert delete_template(session, second["id"])
    assert not delete_template(session, second["id"])
    with pytest.raises(KeyError):
        get_template(session, "missing")
    session.close()
    engine.dispose()


def test_migration_adds_column_once(tmp_path):
    db_path = tmp_path / "legacy.db"
    engine = create_engine(f"sqlite:///{db_path}")
    with engine.begin() as conn:
        conn.execute(
            text(
                "CREATE TABLE backtest_strategies (id VARCHAR(64) PRIMARY KEY, name VARCHAR(128) NOT NULL, description TEXT, strategy_code TEXT NOT NULL, created_at DATETIME, updated_at DATETIME)"
            )
        )
    engine.dispose()
    env = {**os.environ, "BACKTEST_DATABASE_URL": f"sqlite:///{db_path}"}
    script = os.path.join(
        os.path.dirname(os.path.dirname(__file__)),
        "upgrade",
        "migrate_backtest_strategy_settings.py",
    )
    subprocess.run([sys.executable, script], env=env, check=True, capture_output=True, text=True)
    check_engine = create_engine(f"sqlite:///{db_path}")
    columns = {
        column["name"] for column in inspect(check_engine).get_columns("backtest_strategies")
    }
    check_engine.dispose()
    assert "settings_json" in columns
    second = subprocess.run(
        [sys.executable, script], env=env, check=True, capture_output=True, text=True
    )
    assert "already present: True" in second.stderr
