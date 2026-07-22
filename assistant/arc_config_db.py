"""Read-only connection to arcCenter's Config Resolution Engine ("Graphify") Postgres
schema (arc_config_kb) on the arcCenter dev server. Credential priority, mirroring
Graphify-ArcCode's own resolve_dsn() convention:

  1. ARC_CONFIG_KB_DSN env var (explicit override; also what tests use, so the normal
     suite never touches the real remote DB)
  2. Azure Key Vault afficiency-{ARC_CONFIG_KB_ENV}-kv via the `az` CLI (needs `az login`)

Never raises: this is an optional, additive knowledge source (see assistant/graphify.py)
-- any resolution or connection failure returns None so callers degrade to "no data"
rather than crashing the assistant.
"""
import logging
import subprocess

import psycopg
from pgvector.psycopg import register_vector

from assistant import config

_log = logging.getLogger(__name__)

_PG_KEYS = ["pg-host", "pg-port", "application-db", "application-user", "application-pwd"]


def resolve_dsn() -> str | None:
    if config.ARC_CONFIG_KB_DSN:
        return config.ARC_CONFIG_KB_DSN
    vault = f"afficiency-{config.ARC_CONFIG_KB_ENV}-kv"
    secrets = {}
    for key in _PG_KEYS:
        try:
            result = subprocess.run(
                ["az", "keyvault", "secret", "show", "--vault-name", vault,
                 "-n", key, "--query", "value", "-o", "tsv"],
                capture_output=True, text=True, check=True, timeout=10,
            )
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired, FileNotFoundError) as e:
            _log.debug("arc_config_kb DSN resolution failed fetching %s: %s", key, e)
            return None
        secrets[key] = result.stdout.strip()
    return (
        f"host={secrets['pg-host']} port={secrets['pg-port']} "
        f"dbname={secrets['application-db']} user={secrets['application-user']} "
        f"password={secrets['application-pwd']}"
    )


def get_connection() -> psycopg.Connection | None:
    dsn = resolve_dsn()
    if dsn is None:
        return None
    try:
        conn = psycopg.connect(dsn, connect_timeout=config.GRAPHIFY_TIMEOUT, autocommit=True)
        register_vector(conn)
    except Exception as e:
        _log.debug("arc_config_kb connection failed: %s", e)
        return None
    return conn
