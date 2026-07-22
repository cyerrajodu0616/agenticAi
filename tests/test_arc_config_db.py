"""Unit tests -- the `az` CLI is faked via subprocess.run, no real network/Azure."""
import subprocess

import pytest


def test_env_var_override_skips_az_cli(monkeypatch):
    import assistant.arc_config_db as arc_config_db

    monkeypatch.setattr(
        arc_config_db.config, "ARC_CONFIG_KB_DSN",
        "host=x port=5432 dbname=y user=z password=w",
    )
    called = []
    monkeypatch.setattr(arc_config_db.subprocess, "run", lambda *a, **kw: called.append(1))
    assert arc_config_db.resolve_dsn() == "host=x port=5432 dbname=y user=z password=w"
    assert called == []


def test_resolves_via_az_keyvault(monkeypatch):
    import assistant.arc_config_db as arc_config_db

    monkeypatch.setattr(arc_config_db.config, "ARC_CONFIG_KB_DSN", "")
    monkeypatch.setattr(arc_config_db.config, "ARC_CONFIG_KB_ENV", "dev")

    values = {
        "pg-host": "pg.afficiency-dev.az.intra.afficiency.com",
        "pg-port": "5432",
        "application-db": "arcdb",
        "application-user": "arc_app",
        "application-pwd": "s3cret",
    }

    def fake_run(cmd, **kwargs):
        key = cmd[cmd.index("-n") + 1]
        return subprocess.CompletedProcess(cmd, 0, stdout=values[key] + "\n", stderr="")

    monkeypatch.setattr(arc_config_db.subprocess, "run", fake_run)
    dsn = arc_config_db.resolve_dsn()
    assert dsn == (
        "host=pg.afficiency-dev.az.intra.afficiency.com port=5432 "
        "dbname=arcdb user=arc_app password=s3cret"
    )


def test_az_cli_failure_returns_none(monkeypatch):
    import assistant.arc_config_db as arc_config_db

    monkeypatch.setattr(arc_config_db.config, "ARC_CONFIG_KB_DSN", "")

    def fake_run(cmd, **kwargs):
        raise subprocess.CalledProcessError(1, cmd)

    monkeypatch.setattr(arc_config_db.subprocess, "run", fake_run)
    assert arc_config_db.resolve_dsn() is None


def test_az_cli_missing_returns_none(monkeypatch):
    import assistant.arc_config_db as arc_config_db

    monkeypatch.setattr(arc_config_db.config, "ARC_CONFIG_KB_DSN", "")

    def fake_run(cmd, **kwargs):
        raise FileNotFoundError("az not found")

    monkeypatch.setattr(arc_config_db.subprocess, "run", fake_run)
    assert arc_config_db.resolve_dsn() is None


def test_get_connection_returns_none_when_dsn_unresolved(monkeypatch):
    import assistant.arc_config_db as arc_config_db

    monkeypatch.setattr(arc_config_db, "resolve_dsn", lambda: None)
    assert arc_config_db.get_connection() is None


def test_get_connection_returns_none_on_connect_failure(monkeypatch):
    import assistant.arc_config_db as arc_config_db

    monkeypatch.setattr(arc_config_db, "resolve_dsn", lambda: "host=unreachable")

    def fake_connect(*a, **kw):
        raise OSError("connection refused")

    monkeypatch.setattr(arc_config_db.psycopg, "connect", fake_connect)
    assert arc_config_db.get_connection() is None
