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
    assert response.json()["error"]["context"]["status"] == "rejected"
    detail = client.get(f"/api/mortuary/cases/{case['id']}").json()
    assert detail["status"] == "registered"
    record = detail["custody_transfers"][0]
    # 拒收决定与冲突响应同动作落库：实收封签、异常说明、确认人、时间全部留痕
    assert record["status"] == "rejected"
    assert record["observed_seal_code"] == "SEAL-WRONG"
    assert record["condition_note"] == "编号不符"
    assert record["accepted_by"] == "keeper-zhao"
    assert record["accepted_at"]
    assert record["reject_reason_code"] == "seal_mismatch"
    rejected_event = [event for event in detail["timeline"] if event["event_type"] == "custody.rejected"]
    assert len(rejected_event) == 1
    assert rejected_event[0]["payload"]["observed_seal_code"] == "SEAL-WRONG"
    assert rejected_event[0]["payload"]["rejected_at"] == record["accepted_at"]


def test_repeated_or_late_confirmations_cannot_overwrite_rejection(client):
    case = create_case(client, "CASE-SEAL-002")
    transfer = client.post(f"/api/mortuary/cases/{case['id']}/custody-transfers", json={"from_location": "医院太平间", "to_location": "冷藏室C-03", "seal_code": "SEAL-3001", "requested_by": "driver-chen", "idempotency_key": "custody-seal-002"})
    transfer_id = transfer.json()["id"]
    first = client.post(f"/api/mortuary/custody-transfers/{transfer_id}/accept", json={"accepted_by": "keeper-zhao", "observed_seal_code": "SEAL-WRONG", "condition_note": "编号不符"})
    assert first.status_code == 409
    # 重复回报同一确认（即使换人、换说法）：不能改写拒收事实
    repeat = client.post(f"/api/mortuary/custody-transfers/{transfer_id}/accept", json={"accepted_by": "keeper-sun", "observed_seal_code": "SEAL-3001", "condition_note": "夜班复核其实没问题"})
    assert repeat.status_code == 409
    assert repeat.json()["error"]["context"]["status"] == "rejected"
    # 正确封签的确认后到：同样不能把拒收改成接收
    correct = client.post(f"/api/mortuary/custody-transfers/{transfer_id}/accept", json={"accepted_by": "keeper-sun", "observed_seal_code": "SEAL-3001", "condition_note": "封签完整"})
    assert correct.status_code == 409
    detail = client.get(f"/api/mortuary/cases/{case['id']}").json()
    record = detail["custody_transfers"][0]
    assert record["status"] == "rejected"
    assert record["accepted_by"] == "keeper-zhao"
    assert record["observed_seal_code"] == "SEAL-WRONG"
    assert record["condition_note"] == "编号不符"
    # 只有一条拒收事件，未被重复回报追加或改写
    assert [event["event_type"] for event in detail["timeline"]].count("custody.rejected") == 1
    assert detail["status"] == "registered"
    assert detail["current_location"] == ""


def test_explicit_reject_endpoint_persists_decision(client):
    case = create_case(client, "CASE-SEAL-003")
    transfer = client.post(f"/api/mortuary/cases/{case['id']}/custody-transfers", json={"from_location": "医院太平间", "to_location": "冷藏室C-04", "seal_code": "SEAL-4001", "requested_by": "driver-chen", "idempotency_key": "custody-seal-003"})
    transfer_id = transfer.json()["id"]
    rejected = client.post(f"/api/mortuary/custody-transfers/{transfer_id}/reject", json={"rejected_by": "keeper-zhao", "observed_seal_code": "SEAL-TAMPERED", "condition_note": "封签有拆动痕迹", "reject_reason_code": "seal_tampered"})
    assert rejected.status_code == 409
    ctx = rejected.json()["error"]["context"]
    assert ctx == {**ctx, "transfer_id": transfer_id, "status": "rejected"}
    # 对已拒收记录再次显式拒收：冲突且不覆盖
    again = client.post(f"/api/mortuary/custody-transfers/{transfer_id}/reject", json={"rejected_by": "keeper-sun", "observed_seal_code": "SEAL-OTHER", "condition_note": "另一种说法"})
    assert again.status_code == 409
    detail = client.get(f"/api/mortuary/cases/{case['id']}").json()
    record = detail["custody_transfers"][0]
    assert record["status"] == "rejected"
    assert record["reject_reason_code"] == "seal_tampered"
    assert record["accepted_by"] == "keeper-zhao"
    assert record["observed_seal_code"] == "SEAL-TAMPERED"
    assert record["condition_note"] == "封签有拆动痕迹"


def _rejected_transfer(client, case, ref_suffix: str):
    payload = {"from_location": "医院太平间", "to_location": "冷藏室C-05", "seal_code": f"SEAL-{ref_suffix}", "requested_by": "driver-chen", "idempotency_key": f"custody-{ref_suffix}"}
    transfer = client.post(f"/api/mortuary/cases/{case['id']}/custody-transfers", json=payload)
    assert transfer.status_code == 201
    response = client.post(f"/api/mortuary/custody-transfers/{transfer.json()['id']}/accept", json={"accepted_by": "keeper-zhao", "observed_seal_code": "SEAL-WRONG", "condition_note": "编号不符"})
    assert response.status_code == 409
    return transfer.json()["id"], payload


def test_retry_must_reference_rejection_and_match_location(client):
    case = create_case(client, "CASE-SEAL-004")
    rejected_id, original_payload = _rejected_transfer(client, case, "5001")
    # 旧幂等键重放：只返回已拒收的旧记录，不会变成新的正常确认
    replay = client.post(f"/api/mortuary/cases/{case['id']}/custody-transfers", json=original_payload)
    assert replay.status_code == 201
    assert replay.json()["id"] == rejected_id
    assert replay.json()["status"] == "rejected"
    # 不引用前次处置直接重新发起：拒绝
    without_ref = client.post(f"/api/mortuary/cases/{case['id']}/custody-transfers", json={**original_payload, "idempotency_key": "custody-5001-retry-1"})
    assert without_ref.status_code == 409
    assert without_ref.json()["error"]["context"]["supersedes_transfer_id"] == rejected_id
    # 引用了但起点位置与当前保管位置不符：拒绝
    wrong_location = client.post(f"/api/mortuary/cases/{case['id']}/custody-transfers", json={**original_payload, "idempotency_key": "custody-5001-retry-2", "from_location": "别处冷库", "supersedes_transfer_id": rejected_id})
    assert wrong_location.status_code == 409
    # 引用一条不存在的处置：404
    missing = client.post(f"/api/mortuary/cases/{case['id']}/custody-transfers", json={**original_payload, "idempotency_key": "custody-5001-retry-3", "supersedes_transfer_id": 9999})
    assert missing.status_code == 404
    # 正确引用前次拒收并核对当前位置：重新发起成功
    retry_payload = {**original_payload, "idempotency_key": "custody-5001-retry-ok", "seal_code": "SEAL-5001-NEW", "supersedes_transfer_id": rejected_id}
    retried = client.post(f"/api/mortuary/cases/{case['id']}/custody-transfers", json=retry_payload)
    assert retried.status_code == 201, retried.text
    new_transfer = retried.json()
    assert new_transfer["supersedes_transfer_id"] == rejected_id
    accepted = client.post(f"/api/mortuary/custody-transfers/{new_transfer['id']}/accept", json={"accepted_by": "keeper-sun", "observed_seal_code": "SEAL-5001-NEW", "condition_note": "复核一致"})
    assert accepted.status_code == 200
    detail = client.get(f"/api/mortuary/cases/{case['id']}").json()
    assert detail["status"] == "in_custody"
    assert detail["current_location"] == "冷藏室C-05"
    records = detail["custody_transfers"]
    assert [record["status"] for record in records] == ["rejected", "accepted"]
    assert records[1]["supersedes_transfer_id"] == rejected_id
    event_types = [event["event_type"] for event in detail["timeline"]]
    assert event_types == ["case.registered", "custody.requested", "custody.rejected", "custody.requested", "custody.accepted"]
    # 旧记录在档案详情中仍为拒收，未被新交接改写
    assert records[0]["observed_seal_code"] == "SEAL-WRONG"


def test_concurrent_confirmations_leave_single_rejection(client):
    import threading

    from app.database import close_connection
    from app.mortuary.service import MortuaryService

    case = create_case(client, "CASE-SEAL-005")
    transfer = client.post(f"/api/mortuary/cases/{case['id']}/custody-transfers", json={"from_location": "医院太平间", "to_location": "冷藏室C-06", "seal_code": "SEAL-6001", "requested_by": "driver-chen", "idempotency_key": "custody-seal-006"})
    transfer_id = transfer.json()["id"]
    outcomes: list[dict] = []
    barrier = threading.Barrier(2)

    def confirm(actor: str, observed: str) -> None:
        # 工作线程使用各自独立的数据库连接，模拟两个保管员同时回报
        close_connection()
        service = MortuaryService()
        barrier.wait()
        try:
            service.accept_transfer(transfer_id, {"accepted_by": actor, "observed_seal_code": observed, "condition_note": f"{actor}现场记录"})
            outcomes.append({"accepted": True})
        except Exception as exc:  # noqa: BLE001
            outcomes.append({"accepted": False, "status_code": getattr(exc, "status_code", None), "context": getattr(exc, "context", {})})

    threads = [threading.Thread(target=confirm, args=("keeper-zhao", "SEAL-WRONG-A")), threading.Thread(target=confirm, args=("keeper-sun", "SEAL-WRONG-B"))]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert len(outcomes) == 2
    assert all(outcome["accepted"] is False and outcome["status_code"] == 409 for outcome in outcomes)
    detail = client.get(f"/api/mortuary/cases/{case['id']}").json()
    record = detail["custody_transfers"][0]
    assert record["status"] == "rejected"
    # 先到的拒收事实完整保留，后到请求没有覆盖任何字段
    assert record["observed_seal_code"] in {"SEAL-WRONG-A", "SEAL-WRONG-B"}
    assert record["accepted_by"] in {"keeper-zhao", "keeper-sun"}
    assert [event["event_type"] for event in detail["timeline"]].count("custody.rejected") == 1


def test_rejection_survives_restart_with_old_schema(tmp_path):
    import os
    import sqlite3
    from datetime import UTC, datetime

    db_path = tmp_path / "legacy.db"
    # 按旧版本结构建库：没有 reject_reason_code / supersedes_transfer_id 列
    connection = sqlite3.connect(db_path)
    connection.execute("CREATE TABLE mortuary_cases (id INTEGER PRIMARY KEY AUTOINCREMENT, external_ref TEXT NOT NULL UNIQUE, decedent_name TEXT NOT NULL, identity_number TEXT, death_time TEXT NOT NULL, received_from TEXT NOT NULL, family_contact TEXT NOT NULL, family_phone TEXT NOT NULL, special_notes TEXT NOT NULL DEFAULT '', status TEXT NOT NULL DEFAULT 'registered', current_location TEXT NOT NULL DEFAULT '', version INTEGER NOT NULL DEFAULT 1, created_at TEXT NOT NULL, updated_at TEXT NOT NULL)")
    connection.execute("CREATE TABLE custody_transfers (id INTEGER PRIMARY KEY AUTOINCREMENT, case_id INTEGER NOT NULL REFERENCES mortuary_cases(id), from_location TEXT NOT NULL, to_location TEXT NOT NULL, seal_code TEXT NOT NULL, requested_by TEXT NOT NULL, accepted_by TEXT NOT NULL DEFAULT '', observed_seal_code TEXT NOT NULL DEFAULT '', condition_note TEXT NOT NULL DEFAULT '', status TEXT NOT NULL DEFAULT 'pending', idempotency_key TEXT NOT NULL, requested_at TEXT NOT NULL, accepted_at TEXT, UNIQUE(case_id,idempotency_key))")
    connection.execute("CREATE TABLE mortuary_events (id INTEGER PRIMARY KEY AUTOINCREMENT, aggregate_type TEXT NOT NULL, aggregate_id TEXT NOT NULL, event_type TEXT NOT NULL, actor TEXT NOT NULL, payload_json TEXT NOT NULL DEFAULT '{}', created_at TEXT NOT NULL)")
    connection.commit()
    connection.close()

    os.environ["PEACEFUL_CARE_DATABASE_PATH"] = str(db_path)
    from app.database import close_connection
    close_connection()
    try:
        from app.mortuary.service import MortuaryService
        service = MortuaryService()  # 触发幂等列迁移
        case = service.create_case({"external_ref": "CASE-LEGACY-1", "decedent_name": "旧库逝者", "identity_number": None, "death_time": datetime(2026, 9, 30, 8, 0, tzinfo=UTC), "received_from": "市第二医院", "family_contact": "家属", "family_phone": "13800000000", "special_notes": ""}, "intake-clerk")
        transfer = service.request_transfer(case["id"], {"from_location": "市第二医院", "to_location": "冷藏室C-07", "seal_code": "SEAL-7001", "requested_by": "driver-li", "idempotency_key": "custody-legacy-1", "supersedes_transfer_id": None})
        try:
            service.accept_transfer(transfer["id"], {"accepted_by": "keeper-zhao", "observed_seal_code": "SEAL-OLD", "condition_note": "旧库封签不符"})
        except Exception as exc:  # noqa: BLE001
            assert exc.status_code == 409
        # 重新构造服务实例模拟服务重启
        restarted = MortuaryService()
        persisted = restarted.repository.transfer(transfer["id"])
        assert persisted["status"] == "rejected"
        assert persisted["observed_seal_code"] == "SEAL-OLD"
        assert persisted["reject_reason_code"] == "seal_mismatch"
        retried = restarted.request_transfer(case["id"], {"from_location": "市第二医院", "to_location": "冷藏室C-07", "seal_code": "SEAL-7002", "requested_by": "driver-li", "idempotency_key": "custody-legacy-2", "supersedes_transfer_id": transfer["id"]})
        assert retried["supersedes_transfer_id"] == transfer["id"]
    finally:
        close_connection()
