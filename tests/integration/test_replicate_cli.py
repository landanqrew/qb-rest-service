"""Exercises the CLI entrypoint end-to-end with mock-backed clients.

Patches `build_client` so `main()` runs the real precheck → replicate →
summary path against in-memory realms, proving argument wiring, the summary
file, and the exit codes without touching Intuit or GCP.
"""

from __future__ import annotations

import json

from scripts.replicate import __main__ as cli
from tests.replicate_fakes import FakeRealm, client_for


def _patch_clients(monkeypatch, src: FakeRealm, dst: FakeRealm, tmp_path):
    clients = iter(
        [client_for(src, tmp_path, "src"), client_for(dst, tmp_path, "dst")]
    )
    monkeypatch.setattr(cli, "build_client", lambda store, environment: next(clients))
    # _build_stores would import google.cloud; short-circuit to sentinels.
    monkeypatch.setattr(cli, "_build_stores", lambda args: (object(), object()))


def test_cli_happy_path_writes_summary(monkeypatch, tmp_path):
    src = FakeRealm(
        {
            "Account": [{"Id": "1", "Name": "Sales", "AccountType": "Income"}],
            "Customer": [{"Id": "1", "DisplayName": "Acme"}],
            "Item": [
                {"Id": "1", "Name": "Test", "Type": "Service",
                 "IncomeAccountRef": {"value": "1"}}
            ],
        }
    )
    dst = FakeRealm()
    _patch_clients(monkeypatch, src, dst, tmp_path)

    summary_path = tmp_path / "summary.json"
    code = cli.main(["--backend", "file", "--summary", str(summary_path)])

    assert code == cli.EXIT_OK
    summary = json.loads(summary_path.read_text())
    assert summary["totals"]["created"] == 3
    assert summary["id_maps"]["Account"] == {"1": "1000"}


def test_cli_aborts_with_exit_2_on_dirty_sandbox(monkeypatch, tmp_path, capsys):
    src = FakeRealm({"Account": [], "Customer": [], "Item": []})
    dst = FakeRealm({"Customer": [{"Id": str(i)} for i in range(60)], "Item": []})
    _patch_clients(monkeypatch, src, dst, tmp_path)

    code = cli.main(["--backend", "file", "--summary", str(tmp_path / "s.json")])

    assert code == cli.EXIT_NOT_FRESH
    assert "Clear Data and Reset" in capsys.readouterr().err
