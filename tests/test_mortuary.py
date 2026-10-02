from __future__ import annotations


def create_case(client, ref: str = "CASE-001") -> dict:
    response = client.post("/api/mortuary/cases?actor=intake-clerk", json={"external_ref": ref, "decedent_name": "张德安", "identity_number": "ID-440100-1938", "death_time": "2026-09-27T08:30:00Z", "received_from": "市第二医院", "family_contact": "张明", "family_phone": "13800000000", "special_notes": "家属要求核对随身物品"})
    assert response.status_code == 201, response.text
    return response.json()


def test_case_custody_timeline_and_idempotency(client):
    case = create_case(client)
    payload = {"from_location": "接运车辆A", "to_location": "冷藏室C-01", "seal_code": "SEAL-1001", "requested_by": "driver-li", "idempotency_key": "custody-case-001"}
    requested = client.post(f"/api/mortuary/cases/{case['id']}/custody-transfers", json=payload)
    repeated = client.post(f"/api/mortuary/cases/{case['id']}/custody-transfers", json=payload)
    assert requested.status_code == 201
    assert repeated.json()["id"] == requested.json()["id"]
    accepted = client.post(f"/api/mortuary/custody-transfers/{requested.json()['id']}/accept", json={"accepted_by": "keeper-wang", "observed_seal_code": "SEAL-1001", "condition_note": "封签完整"})
    assert accepted.status_code == 200
    detail = client.get(f"/api/mortuary/cases/{case['id']}").json()
    assert detail["status"] == "in_custody"
    assert detail["current_location"] == "冷藏室C-01"
    assert [event["event_type"] for event in detail["timeline"]] == ["case.registered", "custody.requested", "custody.accepted"]


def test_resource_reservation_and_cancellation(client):
    first = create_case(client, "CASE-R-001")
    second = create_case(client, "CASE-R-002")
    assert client.post("/api/mortuary/resources?actor=scheduler", json={"code": "HALL-A", "name": "送别厅A", "kind": "farewell_hall", "site_code": "SITE-1", "capacity": 1, "attributes": {"seats": 120}}).status_code == 201
    payload = {"resource_code": "HALL-A", "case_id": first["id"], "start_at": "2026-09-29T09:00:00Z", "end_at": "2026-09-29T10:00:00Z", "purpose": "家属告别", "created_by": "scheduler", "idempotency_key": "hall-a-first-001"}
    reserved = client.post("/api/mortuary/reservations", json=payload)
    assert reserved.status_code == 201
    next_slot = dict(payload, case_id=second["id"], start_at="2026-09-29T10:00:00Z", end_at="2026-09-29T11:00:00Z", idempotency_key="hall-a-second-001")
    assert client.post(f"/api/mortuary/reservations/{reserved.json()['id']}/cancel?actor=scheduler&reason=家属调整时间").status_code == 200
    assert client.post("/api/mortuary/reservations", json=next_slot).status_code == 201


def test_orders_invoice_and_payment(client):
    case = create_case(client, "CASE-F-001")
    order_ids = []
    for code, quantity, price in (("body-care", 1, 80000), ("farewell-hall", 2, 120000)):
        order = client.post("/api/mortuary/service-orders", json={"case_id": case["id"], "service_code": code, "quantity": quantity, "unit_price_cents": price, "requested_by": "family-service", "notes": "已与家属核对"})
        assert order.status_code == 201
        order_ids.append(order.json()["id"])
        assert client.post(f"/api/mortuary/service-orders/{order.json()['id']}/confirm?actor=finance-reviewer").status_code == 200
    invoice = client.post("/api/mortuary/invoices", json={"case_id": case["id"], "order_ids": order_ids, "created_by": "cashier"})
    assert invoice.status_code == 201
    payment = {"amount_cents": 100000, "channel": "bank", "external_reference": "PAY-20260928-001", "received_by": "cashier"}
    assert client.post(f"/api/mortuary/invoices/{invoice.json()['id']}/payments", json=payment).status_code == 200
    repeated = client.post(f"/api/mortuary/invoices/{invoice.json()['id']}/payments", json=payment)
    assert repeated.status_code == 200 and repeated.json()["paid_cents"] == 100000


def test_burial_right_normal_renewal(client):
    right = client.post("/api/mortuary/burial-rights?actor=cemetery-clerk", json={"plot_code": "A-01-001", "holder_name": "李冬梅", "holder_identity": "ID-3301-001", "starts_on": "2025-01-01", "expires_on": "2045-01-01", "case_id": None})
    assert right.status_code == 201
    renewed = client.post(f"/api/mortuary/burial-rights/{right.json()['id']}/renew", json={"years": 5, "handled_by": "cemetery-clerk", "payment_reference": "RIGHT-PAY-001"})
    assert renewed.status_code == 200 and renewed.json()["expires_on"] == "2050-01-01"


def test_invalid_seal_rejects_transfer(client):
    case = create_case(client, "CASE-SEAL-001")
    transfer = client.post(f"/api/mortuary/cases/{case['id']}/custody-transfers", json={"from_location": "医院太平间", "to_location": "冷藏室C-02", "seal_code": "SEAL-2001", "requested_by": "driver-chen", "idempotency_key": "custody-seal-001"})
    response = client.post(f"/api/mortuary/custody-transfers/{transfer.json()['id']}/accept", json={"accepted_by": "keeper-zhao", "observed_seal_code": "SEAL-WRONG", "condition_note": "编号不符"})
    assert response.status_code == 409
    error = response.json()["error"]
    assert error["context"]["status"] == "rejected"
    assert error["context"]["observed_seal_code"] == "SEAL-WRONG"
    assert error["context"]["accepted_by"] == "keeper-zhao"
    assert error["context"]["accepted_at"]
    detail = client.get(f"/api/mortuary/cases/{case['id']}").json()
    assert detail["status"] == "registered"
    record = detail["custody_transfers"][0]
    assert record["status"] == "rejected"
    assert record["observed_seal_code"] == "SEAL-WRONG"
    assert record["condition_note"] == "编号不符"
    assert record["accepted_by"] == "keeper-zhao"
    assert record["accepted_at"]
    rejected_events = [event for event in detail["timeline"] if event["event_type"] == "custody.rejected"]
    assert len(rejected_events) == 1
    assert rejected_events[0]["actor"] == "keeper-zhao"
    assert rejected_events[0]["payload"]["observed_seal_code"] == "SEAL-WRONG"


def test_rejection_fact_is_immutable(client):
    case = create_case(client, "CASE-SEAL-002")
    transfer = client.post(f"/api/mortuary/cases/{case['id']}/custody-transfers", json={"from_location": "医院太平间", "to_location": "冷藏室C-02", "seal_code": "SEAL-2002", "requested_by": "driver-chen", "idempotency_key": "custody-seal-002"}).json()
    first = client.post(f"/api/mortuary/custody-transfers/{transfer['id']}/accept", json={"accepted_by": "keeper-zhao", "observed_seal_code": "SEAL-WRONG", "condition_note": "编号不符"})
    assert first.status_code == 409
    record = client.get(f"/api/mortuary/cases/{case['id']}").json()["custody_transfers"][0]
    duplicate = client.post(f"/api/mortuary/custody-transfers/{transfer['id']}/accept", json={"accepted_by": "keeper-zhao", "observed_seal_code": "SEAL-WRONG", "condition_note": "编号不符"})
    assert duplicate.status_code == 409
    assert duplicate.json()["error"]["context"]["accepted_at"] == record["accepted_at"]
    late_accept = client.post(f"/api/mortuary/custody-transfers/{transfer['id']}/accept", json={"accepted_by": "keeper-li", "observed_seal_code": "SEAL-2002", "condition_note": "下一班复核"})
    assert late_accept.status_code == 409
    other_report = client.post(f"/api/mortuary/custody-transfers/{transfer['id']}/accept", json={"accepted_by": "keeper-li", "observed_seal_code": "SEAL-OTHER", "condition_note": "再次不符"})
    assert other_report.status_code == 409
    detail = client.get(f"/api/mortuary/cases/{case['id']}").json()
    assert detail["custody_transfers"][0] == record
    assert detail["custody_transfers"][0]["accepted_by"] == "keeper-zhao"
    assert [event["event_type"] for event in detail["timeline"]] == ["case.registered", "custody.requested", "custody.rejected"]


def test_retry_after_rejection_requires_reference_and_location(client):
    case = create_case(client, "CASE-SEAL-003")
    transfer = client.post(f"/api/mortuary/cases/{case['id']}/custody-transfers", json={"from_location": "医院太平间", "to_location": "冷藏室C-02", "seal_code": "SEAL-2003", "requested_by": "driver-chen", "idempotency_key": "custody-seal-003"}).json()
    assert client.post(f"/api/mortuary/custody-transfers/{transfer['id']}/accept", json={"accepted_by": "keeper-zhao", "observed_seal_code": "SEAL-WRONG", "condition_note": "编号不符"}).status_code == 409
    base = {"from_location": "医院太平间", "to_location": "冷藏室C-02", "seal_code": "SEAL-2003", "requested_by": "driver-chen"}
    missing = client.post(f"/api/mortuary/cases/{case['id']}/custody-transfers", json={**base, "idempotency_key": "custody-seal-003-r1"})
    assert missing.status_code == 409
    assert missing.json()["error"]["context"]["previous_transfer_id"] == transfer["id"]
    wrong_ref = client.post(f"/api/mortuary/cases/{case['id']}/custody-transfers", json={**base, "idempotency_key": "custody-seal-003-r2", "previous_transfer_id": transfer["id"] + 100})
    assert wrong_ref.status_code == 409
    wrong_from = client.post(f"/api/mortuary/cases/{case['id']}/custody-transfers", json={**base, "from_location": "其他地点", "idempotency_key": "custody-seal-003-r3", "previous_transfer_id": transfer["id"]})
    assert wrong_from.status_code == 409
    assert wrong_from.json()["error"]["context"]["current_location"] == "医院太平间"
    retry = client.post(f"/api/mortuary/cases/{case['id']}/custody-transfers", json={**base, "idempotency_key": "custody-seal-003-r4", "previous_transfer_id": transfer["id"]})
    assert retry.status_code == 201
    assert retry.json()["previous_transfer_id"] == transfer["id"]
    replay = client.post(f"/api/mortuary/cases/{case['id']}/custody-transfers", json={**base, "idempotency_key": "custody-seal-003-r4", "previous_transfer_id": transfer["id"]})
    assert replay.json()["id"] == retry.json()["id"]
    accepted = client.post(f"/api/mortuary/custody-transfers/{retry.json()['id']}/accept", json={"accepted_by": "keeper-li", "observed_seal_code": "SEAL-2003", "condition_note": "封签已更换并核对一致"})
    assert accepted.status_code == 200
    detail = client.get(f"/api/mortuary/cases/{case['id']}").json()
    assert detail["status"] == "in_custody"
    assert detail["current_location"] == "冷藏室C-02"
    assert [event["event_type"] for event in detail["timeline"]] == ["case.registered", "custody.requested", "custody.rejected", "custody.requested", "custody.accepted"]
    assert detail["timeline"][3]["payload"]["previous_transfer_id"] == transfer["id"]


def test_pending_transfer_blocks_duplicate_request(client):
    case = create_case(client, "CASE-SEAL-004")
    payload = {"from_location": "医院太平间", "to_location": "冷藏室C-02", "seal_code": "SEAL-2004", "requested_by": "driver-chen", "idempotency_key": "custody-seal-004"}
    assert client.post(f"/api/mortuary/cases/{case['id']}/custody-transfers", json=payload).status_code == 201
    second = client.post(f"/api/mortuary/cases/{case['id']}/custody-transfers", json={**payload, "idempotency_key": "custody-seal-004-b"})
    assert second.status_code == 409


def test_accepted_transfer_replay_and_conflicting_report(client):
    case = create_case(client, "CASE-SEAL-005")
    transfer = client.post(f"/api/mortuary/cases/{case['id']}/custody-transfers", json={"from_location": "医院太平间", "to_location": "冷藏室C-02", "seal_code": "SEAL-2005", "requested_by": "driver-chen", "idempotency_key": "custody-seal-005"}).json()
    accepted = client.post(f"/api/mortuary/custody-transfers/{transfer['id']}/accept", json={"accepted_by": "keeper-wang", "observed_seal_code": "SEAL-2005", "condition_note": "封签完整"})
    assert accepted.status_code == 200
    replay = client.post(f"/api/mortuary/custody-transfers/{transfer['id']}/accept", json={"accepted_by": "keeper-wang", "observed_seal_code": "SEAL-2005", "condition_note": "封签完整"})
    assert replay.status_code == 200
    assert replay.json()["accepted_at"] == accepted.json()["accepted_at"]
    conflicting = client.post(f"/api/mortuary/custody-transfers/{transfer['id']}/accept", json={"accepted_by": "keeper-li", "observed_seal_code": "SEAL-WRONG", "condition_note": "迟到的异常上报"})
    assert conflicting.status_code == 409
    detail = client.get(f"/api/mortuary/cases/{case['id']}").json()
    assert detail["custody_transfers"][0]["status"] == "accepted"
    assert detail["custody_transfers"][0]["accepted_by"] == "keeper-wang"
    assert [event["event_type"] for event in detail["timeline"]] == ["case.registered", "custody.requested", "custody.accepted"]


def test_rejection_survives_service_restart(client):
    case = create_case(client, "CASE-SEAL-006")
    transfer = client.post(f"/api/mortuary/cases/{case['id']}/custody-transfers", json={"from_location": "医院太平间", "to_location": "冷藏室C-02", "seal_code": "SEAL-2006", "requested_by": "driver-chen", "idempotency_key": "custody-seal-006"}).json()
    assert client.post(f"/api/mortuary/custody-transfers/{transfer['id']}/accept", json={"accepted_by": "keeper-zhao", "observed_seal_code": "SEAL-WRONG", "condition_note": "编号不符"}).status_code == 409
    from fastapi.testclient import TestClient
    from app.main import app
    with TestClient(app) as restarted:
        detail = restarted.get(f"/api/mortuary/cases/{case['id']}").json()
        assert detail["custody_transfers"][0]["status"] == "rejected"
        assert detail["custody_transfers"][0]["accepted_by"] == "keeper-zhao"
        duplicate = restarted.post(f"/api/mortuary/custody-transfers/{transfer['id']}/accept", json={"accepted_by": "keeper-zhao", "observed_seal_code": "SEAL-WRONG", "condition_note": "编号不符"})
        assert duplicate.status_code == 409
        late_accept = restarted.post(f"/api/mortuary/custody-transfers/{transfer['id']}/accept", json={"accepted_by": "keeper-li", "observed_seal_code": "SEAL-2006", "condition_note": "重启后复核"})
        assert late_accept.status_code == 409
        assert restarted.get(f"/api/mortuary/cases/{case['id']}").json()["custody_transfers"][0]["status"] == "rejected"


def test_ensure_schema_upgrades_legacy_transfer_table(tmp_path):
    import sqlite3
    from app.database import close_connection, get_connection
    path = tmp_path / "legacy.db"
    legacy = sqlite3.connect(path)
    legacy.execute("CREATE TABLE custody_transfers (id INTEGER PRIMARY KEY AUTOINCREMENT, case_id INTEGER NOT NULL, from_location TEXT NOT NULL, to_location TEXT NOT NULL, seal_code TEXT NOT NULL, requested_by TEXT NOT NULL, accepted_by TEXT NOT NULL DEFAULT '', observed_seal_code TEXT NOT NULL DEFAULT '', condition_note TEXT NOT NULL DEFAULT '', status TEXT NOT NULL DEFAULT 'pending', idempotency_key TEXT NOT NULL, requested_at TEXT NOT NULL, accepted_at TEXT)")
    legacy.commit()
    legacy.close()
    import os
    os.environ["PEACEFUL_CARE_DATABASE_PATH"] = str(path)
    close_connection()
    from app.mortuary.repository import MortuaryRepository
    MortuaryRepository(get_connection()).ensure_schema()
    columns = {row[1] for row in get_connection().execute("PRAGMA table_info(custody_transfers)").fetchall()}
    assert "previous_transfer_id" in columns
    close_connection()
