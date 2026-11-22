"""S9: the engine's write guard accepts the real database only with an exact explicit authorization (no database)."""

from __future__ import annotations

import pytest

from nordiccart.activity import run as run_mod

REAL = {"cluster": "7663257437785384692", "port": 5432, "database": "nordiccart_oltp"}
TOKEN = "7663257437785384692/nordiccart_oltp"


@pytest.fixture
def real_target(monkeypatch):
    monkeypatch.delenv("NORDICCART_S6_SANDBOX", raising=False)
    monkeypatch.delenv(run_mod.REAL_AUTHORIZATION, raising=False)
    monkeypatch.setattr(run_mod, "_protected_target", lambda: (5432, "nordiccart_oltp"))
    return monkeypatch


def _guard(m, ident):
    m.setattr(run_mod, "database_identity", lambda conn: dict(ident))
    return run_mod.require_sandbox(None)


def test_real_database_refused_without_authorization(real_target):
    with pytest.raises(run_mod.SandboxGuardError, match="NORDICCART_S6_SANDBOX=1 is not set"):
        _guard(real_target, REAL)


def test_real_database_accepted_with_the_exact_authorization(real_target):
    real_target.setenv(run_mod.REAL_AUTHORIZATION, TOKEN)
    assert _guard(real_target, REAL) == REAL


@pytest.mark.parametrize("ident,token,env", [
    (REAL, "7663257437785384692/other_db", {}),                                         # names another database
    (REAL, "1234/nordiccart_oltp", {}),                                                  # names another cluster
    ({**REAL, "port": 55432}, TOKEN, {}),                                                # not the config/.env target
    ({**REAL, "cluster": "7709316403725191040"}, "7709316403725191040/nordiccart_oltp", {}),   # not a protected cluster
    (REAL, TOKEN, {"NORDICCART_S6_SANDBOX": "1"}),                                       # never with the sandbox switch
])
def test_real_authorization_must_name_this_server_exactly(real_target, ident, token, env):
    real_target.setenv(run_mod.REAL_AUTHORIZATION, token)
    for k, v in env.items():
        real_target.setenv(k, v)
    with pytest.raises(run_mod.SandboxGuardError):
        _guard(real_target, ident)
