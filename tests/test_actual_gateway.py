"""The Python side of the official Actual API worker boundary."""

from __future__ import annotations

import datetime
import sys
from pathlib import Path

import pytest

from actual_clerk.clients.actual import ActualGateway, ActualGatewayError, _normalize_snapshot


def raw_snapshot() -> dict:
    return {
        "accounts": [
            {
                "id": "acct-1",
                "name": "Checking",
                "last_sync": "2026-09-01T12:00:00Z",
                "last_transaction_date": "2026-08-31",
                "balance_cents": 6500,
                "cleared_balance_cents": 7500,
                "uncleared_balance_cents": -1000,
                "unconfirmed_transfers": [
                    {"id": "transfer-1", "date": "2026-08-31", "amount_cents": 2500}
                ],
            }
        ],
        "categories": [],
        "groups": [],
        "budgeted": {},
        "budgeted_history": {},
        "transactions": [
            {
                "id": "txn-1",
                "date": "2026-08-31",
                "payee_name": "SQ *BLUE BOTTLE 4471",
                "imported_description": "BLUE BOTTLE COFFEE #4471 OAKLAND CA",
            },
            {"id": "bad", "date": "not-a-date"},
        ],
        "income_history": [["2026-08-01", 400000]],
        "tags": [],
        "collected_at": "2026-09-01T12:01:00Z",
        "history_start": "2024-09-01",
    }


def fake_worker(path: Path, *, fail_method: str = "", oversized_snapshot: bool = False) -> None:
    path.write_text(
        "\n".join(
            [
                "import json, sys",
                f"SNAPSHOT = {raw_snapshot()!r}",
                f"OVERSIZED_SNAPSHOT = {oversized_snapshot!r}",
                "if OVERSIZED_SNAPSHOT: SNAPSHOT['transport_padding'] = 'x' * 100_000",
                f"FAIL_METHOD = {fail_method!r}",
                "PREFIX = '@@actual-clerk-rpc@@'",
                "for line in sys.stdin:",
                "    request = json.loads(line)",
                "    method = request['method']",
                "    if method == FAIL_METHOD:",
                "        response = {'id': request['id'], 'ok': False, 'error': {'message': 'bank refused', 'code': 'connection-failed'}}",
                "    elif method == 'initialize':",
                "        response = {'id': request['id'], 'ok': True, 'result': {'apiVersion': '26.9.0', 'serverVersion': '26.9.0'}}",
                "    elif method == 'snapshot':",
                "        response = {'id': request['id'], 'ok': True, 'result': SNAPSHOT}",
                "    else:",
                "        response = {'id': request['id'], 'ok': True, 'result': {'shutdown': True}}",
                "    print(PREFIX + json.dumps(response), flush=True)",
                "    if method == 'shutdown': break",
            ]
        ),
        encoding="utf-8",
    )


def configured(settings_manager) -> None:
    settings_manager.update(
        {
            "actual_url": "http://actual:5006",
            "actual_password": "secret",
            "actual_budget_id": "budget-1",
        }
    )


def test_worker_snapshot_is_restored_to_clerks_python_contract():
    snapshot = _normalize_snapshot(raw_snapshot())
    [account] = snapshot["accounts"]
    assert account["last_sync"] == datetime.datetime(2026, 9, 1, 12, tzinfo=datetime.UTC)
    assert account["last_transaction_date"] == datetime.date(2026, 8, 31)
    assert account["unconfirmed_transfers"][0]["date"] == datetime.date(2026, 8, 31)
    [transaction] = snapshot["transactions"]
    assert transaction["merchant_key"] == "blue bottle"
    assert snapshot["income_history"] == [(datetime.date(2026, 8, 1), 400000)]
    assert snapshot["history_start"] == datetime.date(2024, 9, 1)


async def test_gateway_starts_one_worker_and_uses_framed_rpc(
    tmp_path, settings_manager
):
    configured(settings_manager)
    script = tmp_path / "worker.py"
    fake_worker(script)
    gateway = ActualGateway(
        settings_manager,
        tmp_path,
        node_executable=sys.executable,
        worker_path=script,
    )
    try:
        snapshot = await gateway.snapshot(today=datetime.date(2026, 9, 1))
        assert snapshot["accounts"][0]["cleared_balance_cents"] == 7500
        assert gateway.status()["api_version"] == "26.9.0"
        assert gateway.connected is True
    finally:
        await gateway.close()
    assert gateway.connected is False


async def test_gateway_accepts_a_snapshot_larger_than_asyncios_default_line_limit(
    tmp_path, settings_manager
):
    configured(settings_manager)
    script = tmp_path / "worker.py"
    fake_worker(script, oversized_snapshot=True)
    gateway = ActualGateway(
        settings_manager,
        tmp_path,
        node_executable=sys.executable,
        worker_path=script,
    )
    try:
        snapshot = await gateway.snapshot(today=datetime.date(2026, 9, 1))
        assert len(snapshot["transport_padding"]) == 100_000
    finally:
        await gateway.close()


async def test_structured_worker_errors_remain_retryable(tmp_path, settings_manager):
    configured(settings_manager)
    script = tmp_path / "worker.py"
    fake_worker(script, fail_method="bankSync")
    gateway = ActualGateway(
        settings_manager,
        tmp_path,
        node_executable=sys.executable,
        worker_path=script,
    )
    try:
        with pytest.raises(ActualGatewayError, match="bank refused") as raised:
            await gateway.bank_sync()
        assert raised.value.retryable is True
        assert "connection-failed" in str(raised.value)
        assert gateway.connected is False
    finally:
        await gateway.close()


async def test_worker_start_failures_are_visible_in_gateway_status(tmp_path, settings_manager):
    configured(settings_manager)
    gateway = ActualGateway(
        settings_manager,
        tmp_path,
        node_executable=str(tmp_path / "missing-node"),
    )
    try:
        with pytest.raises(ActualGatewayError, match="Could not start"):
            await gateway.test_connection()
        assert "Could not start" in gateway.status()["last_error"]
        assert gateway.connected is False
    finally:
        await gateway.close()


def test_only_one_gateway_can_own_a_data_directory(tmp_path, settings_manager):
    first = ActualGateway(settings_manager, tmp_path)
    second = ActualGateway(settings_manager, tmp_path)
    first._acquire_cache_lock()
    try:
        with pytest.raises(ActualGatewayError, match="already owns"):
            second._acquire_cache_lock()
    finally:
        first._release_cache_lock()


def test_cache_identity_separates_servers_and_budgets(tmp_path, settings_manager):
    configured(settings_manager)
    gateway = ActualGateway(settings_manager, tmp_path)
    first = gateway._cache_directory(settings_manager.get())
    settings_manager.update({"actual_budget_id": "budget-2"})
    second = gateway._cache_directory(settings_manager.get())
    assert first.parent == second.parent
    assert first != second
