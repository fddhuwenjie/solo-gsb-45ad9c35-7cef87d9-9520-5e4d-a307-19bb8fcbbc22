"""端到端 API 测试。"""

from decimal import Decimal

from app import db as db_module


def register(client, well, sample, volume):
    resp = client.post("/wells", json={"well": well, "sample": sample, "volume": volume})
    assert resp.status_code == 201, resp.text
    return resp.json()


def amounts(body):
    return {c["sample"]: Decimal(c["amount"]) for c in body["composition"]}


def assert_invariant(client, well):
    """组分量之和必须严格等于孔体积。"""
    body = client.get(f"/wells/{well}").json()
    assert sum(amounts(body).values(), Decimal(0)) == Decimal(body["volume"]), body
    return body


def test_register_initial_sample(client):
    body = register(client, "A1", "S1", "100")
    assert body["well"] == "A1"
    assert body["volume"] == "100"
    assert body["composition"] == [{"sample": "S1", "amount": "100", "fraction": "1"}]


def test_register_top_up_uses_decimal(client):
    register(client, "A1", "S1", "0.1")
    body = register(client, "A1", "S1", "0.2")
    # float 会得到 0.30000000000000004，Decimal 必须精确
    assert body["volume"] == "0.3"
    assert body["composition"][0]["amount"] == "0.3"


def test_unknown_well_is_404(client):
    assert client.get("/wells/Z9").status_code == 404


def test_batch_transfer_and_lineage(client):
    register(client, "A1", "S1", "100")
    register(client, "A2", "S2", "60")
    resp = client.post(
        "/batches",
        json={
            "request_key": "k1",
            "operations": [
                {"source": "A1", "destination": "B1", "volume": "40"},
                {"source": "A2", "destination": "B1", "volume": "30"},
            ],
        },
    )
    assert resp.status_code == 201, resp.text
    batch = resp.json()
    assert batch["status"] == "committed"
    assert batch["replayed"] is False
    assert [op["step"] for op in batch["operations"]] == [1, 2]

    # 目标孔重新混合：S1:S2 = 40:30
    b1 = client.get("/wells/B1").json()
    assert b1["volume"] == "70"
    comp = {c["sample"]: c for c in b1["composition"]}
    assert comp["S1"]["amount"] == "40"
    assert comp["S1"]["fraction"] == "0.571428571"
    assert comp["S2"]["amount"] == "30"
    assert comp["S2"]["fraction"] == "0.428571429"

    # 第二级转移：从混合孔按比例带走 S1/S2
    resp = client.post(
        "/batches",
        json={
            "request_key": "k2",
            "operations": [{"source": "B1", "destination": "C1", "volume": "35"}],
        },
    )
    assert resp.status_code == 201, resp.text
    c1 = client.get("/wells/C1").json()
    assert c1["volume"] == "35"
    assert {c["sample"]: c["amount"] for c in c1["composition"]} == {"S1": "20", "S2": "15"}
    assert client.get("/wells/B1").json()["volume"] == "35"


def test_batch_rolls_back_on_insufficient_volume(client):
    register(client, "A1", "S1", "100")
    resp = client.post(
        "/batches",
        json={
            "request_key": "k-fail",
            "operations": [
                {"source": "A1", "destination": "B1", "volume": "60"},
                {"source": "A1", "destination": "B2", "volume": "50"},  # 只剩 40，失败
            ],
        },
    )
    assert resp.status_code == 409, resp.text
    detail = resp.json()["detail"]
    assert detail["well"] == "A1"
    assert detail["available"] == "40"
    assert detail["requested"] == "50"

    # 整批回滚：A1 不变，B1/B2 从未被创建
    assert client.get("/wells/A1").json()["volume"] == "100"
    assert client.get("/wells/B1").status_code == 404
    assert client.get("/wells/B2").status_code == 404

    # 失败批次也被完整记录
    batch = client.get(f"/batches/{detail['batch_id']}").json()
    assert batch["status"] == "failed"
    assert batch["operations"] == []
    assert "体积不足" in batch["error"]


def test_request_key_idempotency(client):
    register(client, "A1", "S1", "100")
    payload = {
        "request_key": "same-key",
        "operations": [{"source": "A1", "destination": "B1", "volume": "40"}],
    }
    first = client.post("/batches", json=payload)
    assert first.status_code == 201
    second = client.post("/batches", json=payload)
    assert second.status_code == 200
    assert second.json()["replayed"] is True
    assert second.json()["batch_id"] == first.json()["batch_id"]
    # 不会重复执行
    assert client.get("/wells/A1").json()["volume"] == "60"
    assert client.get("/wells/B1").json()["volume"] == "40"


def test_failed_batch_key_is_consumed(client):
    register(client, "A1", "S1", "10")
    payload = {
        "request_key": "fail-key",
        "operations": [{"source": "A1", "destination": "B1", "volume": "50"}],
    }
    assert client.post("/batches", json=payload).status_code == 409
    replay = client.post("/batches", json=payload)
    assert replay.status_code == 200
    assert replay.json()["status"] == "failed"
    assert replay.json()["replayed"] is True


def test_unknown_source_well_fails_batch(client):
    resp = client.post(
        "/batches",
        json={
            "request_key": "ghost",
            "operations": [{"source": "Z9", "destination": "B1", "volume": "1"}],
        },
    )
    assert resp.status_code == 409
    assert resp.json()["detail"]["available"] == "0"


def test_well_batches_history(client):
    register(client, "A1", "S1", "100")
    client.post(
        "/batches",
        json={
            "request_key": "h1",
            "operations": [{"source": "A1", "destination": "B1", "volume": "10"}],
        },
    )
    client.post(
        "/batches",
        json={
            "request_key": "h2",
            "operations": [{"source": "B1", "destination": "C1", "volume": "5"}],
        },
    )
    body = client.get("/wells/B1/batches").json()
    assert [b["request_key"] for b in body["batches"]] == ["h1", "h2"]
    # 完整批次记录：含该批全部操作
    assert body["batches"][0]["operations"][0]["source"] == "A1"

    all_batches = client.get("/batches").json()["batches"]
    assert [b["request_key"] for b in all_batches] == ["h1", "h2"]


def test_same_source_and_destination_rejected(client):
    register(client, "A1", "S1", "10")
    resp = client.post(
        "/batches",
        json={
            "request_key": "self",
            "operations": [{"source": "A1", "destination": "A1", "volume": "1"}],
        },
    )
    assert resp.status_code == 422


def test_empty_operations_rejected(client):
    resp = client.post("/batches", json={"request_key": "empty", "operations": []})
    assert resp.status_code == 422


# ------------------------------------------------ 微量多组分配比修正回归


def test_tiny_multicomponent_transfer_keeps_invariant(client):
    """三个 1e-9 样本，取走 1e-9：目标孔必须拿到恰好一个完整单位。"""
    for sample in ("S1", "S2", "S3"):
        register(client, "A1", sample, "0.000000001")

    resp = client.post(
        "/batches",
        json={
            "request_key": "tiny-1",
            "operations": [
                {"source": "A1", "destination": "B1", "volume": "0.000000001"}
            ],
        },
    )
    assert resp.status_code == 201, resp.text

    src = assert_invariant(client, "A1")
    assert src["volume"] == "0.000000002"
    # 余量相同（各 1 个单位，份额 1/3），按样本 ID 排序：S1 被带走
    assert set(amounts(src)) == {"S2", "S3"}

    dst = assert_invariant(client, "B1")
    assert dst["volume"] == "0.000000001"
    assert amounts(dst) == {"S1": Decimal("0.000000001")}
    assert dst["composition"][0]["fraction"] == "1"


def test_consecutive_tiny_transfers_drain_in_id_order(client):
    """同一批连续移液使用修正后的组成：逐单位抽干，平局顺序随剩余 ID 前进。"""
    for sample in ("S1", "S2", "S3"):
        register(client, "A1", sample, "0.000000001")

    resp = client.post(
        "/batches",
        json={
            "request_key": "tiny-drain",
            "operations": [
                {"source": "A1", "destination": "B1", "volume": "0.000000001"},
                {"source": "A1", "destination": "B2", "volume": "0.000000001"},
                {"source": "A1", "destination": "B3", "volume": "0.000000001"},
            ],
        },
    )
    assert resp.status_code == 201, resp.text

    src = assert_invariant(client, "A1")
    assert src["volume"] == "0"
    assert src["composition"] == []
    for well, sample in (("B1", "S1"), ("B2", "S2"), ("B3", "S3")):
        body = assert_invariant(client, well)
        assert amounts(body) == {sample: Decimal("0.000000001")}

    # 抽干后再取按体积不足处理，且整批回滚不破坏不变量
    resp = client.post(
        "/batches",
        json={
            "request_key": "tiny-drain-over",
            "operations": [
                {"source": "A1", "destination": "B1", "volume": "0.000000001"}
            ],
        },
    )
    assert resp.status_code == 409
    assert assert_invariant(client, "B1")["volume"] == "0.000000001"


def test_largest_remainder_assignment_order(client):
    """7 单位中取 3：余数 2/1/0 排序，两个单位分给 S1、S2；S3 留整。"""
    register(client, "A1", "S1", "0.000000004")
    register(client, "A1", "S2", "0.000000002")
    register(client, "A1", "S3", "0.000000001")

    resp = client.post(
        "/batches",
        json={
            "request_key": "remainder",
            "operations": [
                {"source": "A1", "destination": "B1", "volume": "0.000000003"}
            ],
        },
    )
    assert resp.status_code == 201, resp.text

    src = assert_invariant(client, "A1")
    assert amounts(src) == {
        "S1": Decimal("0.000000002"),
        "S2": Decimal("0.000000001"),
        "S3": Decimal("0.000000001"),
    }
    dst = assert_invariant(client, "B1")
    assert amounts(dst) == {
        "S1": Decimal("0.000000002"),
        "S2": Decimal("0.000000001"),
    }

    # 再取 2 单位：当前 2/1/1（共 4），份额 1r0、0r2、0r2；floor 给 S1 一个，
    # 最后 1 个单位按「余数降序、ID 升序」归 S2
    resp = client.post(
        "/batches",
        json={
            "request_key": "remainder-2",
            "operations": [
                {"source": "A1", "destination": "B1", "volume": "0.000000002"}
            ],
        },
    )
    assert resp.status_code == 201, resp.text
    src = assert_invariant(client, "A1")
    assert amounts(src) == {
        "S1": Decimal("0.000000001"),
        "S3": Decimal("0.000000001"),
    }
    dst = assert_invariant(client, "B1")
    assert amounts(dst) == {
        "S1": Decimal("0.000000003"),
        "S2": Decimal("0.000000002"),
    }


def test_corrected_composition_flows_between_batches(client):
    """修正后的组成供后续批次的按比例吸取与占比查询使用。"""
    register(client, "A1", "S1", "0.000000002")
    register(client, "A1", "S2", "0.000000002")

    # 取 3 单位：各 floor=1（2/4 平局），第三单位按 ID 给 S1 => S1=2, S2=1
    resp = client.post(
        "/batches",
        json={
            "request_key": "flow-1",
            "operations": [
                {"source": "A1", "destination": "B1", "volume": "0.000000003"}
            ],
        },
    )
    assert resp.status_code == 201, resp.text
    b1 = assert_invariant(client, "B1")
    assert amounts(b1) == {
        "S1": Decimal("0.000000002"),
        "S2": Decimal("0.000000001"),
    }
    fractions = {c["sample"]: c["fraction"] for c in b1["composition"]}
    # 占比基于修正后的组成：2/3 与 1/3
    assert fractions == {"S1": "0.666666667", "S2": "0.333333333"}

    # 下一批从 B1 取 2 单位：S1 必中（4/3=1r1），S2 得 1（2/3）
    resp = client.post(
        "/batches",
        json={
            "request_key": "flow-2",
            "operations": [
                {"source": "B1", "destination": "C1", "volume": "0.000000002"}
            ],
        },
    )
    assert resp.status_code == 201, resp.text
    c1 = assert_invariant(client, "C1")
    assert amounts(c1) == {
        "S1": Decimal("0.000000001"),
        "S2": Decimal("0.000000001"),
    }
    assert_invariant(client, "B1")
    assert_invariant(client, "A1")


def test_transfer_cannot_exceed_component_amount(client):
    """任何样本被带走的量不得超过其原有量：全部转给目标后，来源无该样本。"""
    register(client, "A1", "S1", "0.000000005")
    register(client, "A1", "S2", "0.000000003")

    resp = client.post(
        "/batches",
        json={
            "request_key": "cap",
            "operations": [
                {"source": "A1", "destination": "B1", "volume": "0.000000008"}
            ],
        },
    )
    assert resp.status_code == 201, resp.text
    src = assert_invariant(client, "A1")
    assert src["volume"] == "0"
    assert src["composition"] == []
    dst = assert_invariant(client, "B1")
    assert amounts(dst) == {
        "S1": Decimal("0.000000005"),
        "S2": Decimal("0.000000003"),
    }


# ------------------------------------------------ 误操作批次撤销


def _submit_simple_batch(client, key, source, destination, volume):
    resp = client.post(
        "/batches",
        json={
            "request_key": key,
            "operations": [{"source": source, "destination": destination, "volume": volume}],
        },
    )
    assert resp.status_code == 201, resp.text
    return resp.json()["batch_id"]


def _reverse(client, batch_id, key):
    return client.post(f"/batches/{batch_id}/reversals", json={"request_key": key})


def test_reversal_restores_pre_batch_snapshots(client):
    register(client, "A1", "S1", "100")
    register(client, "A2", "S2", "60")
    bid = _submit_simple_batch(client, "b1", "A1", "B1", "40")
    _submit_simple_batch(client, "b2", "A2", "C1", "10")

    resp = _reverse(client, bid, "rev-1")
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["batch_id"] == bid
    assert body["replayed"] is False
    assert body["request_key"] == "rev-1"
    assert [w["well"] for w in body["restored_wells"]] == ["A1", "B1"]

    # 触及孔全部回到批前：A1 恢复 100，B1 批前不存在 -> 撤销后也不存在
    assert client.get("/wells/A1").json()["volume"] == "100"
    assert client.get("/wells/B1").status_code == 404
    # 未触及的孔不受影响
    assert client.get("/wells/A2").json()["volume"] == "50"
    assert client.get("/wells/C1").json()["volume"] == "10"

    # 原批次保留（不删除），但带撤销标记；撤销审计可查
    batch = client.get(f"/batches/{bid}").json()
    assert batch["status"] == "committed"
    assert batch["reversed"] is True
    assert batch["reversal"]["request_key"] == "rev-1"
    assert body["batch"]["reversed"] is True


def test_reversal_conflict_lists_blocking_wells_and_operations(client):
    register(client, "A1", "S1", "100")
    register(client, "A2", "S2", "60")
    b1 = _submit_simple_batch(client, "b1", "A1", "B1", "40")

    # 后续已提交批次改动 B1（作为来源）和 A2
    b2 = _submit_simple_batch(client, "b2", "B1", "A2", "10")
    # 对 A1 补液（批次 b2 之后）
    register(client, "A1", "S9", "5")

    resp = _reverse(client, b1, "rev-blocked")
    assert resp.status_code == 409, resp.text
    detail = resp.json()["detail"]
    assert detail["code"] == "blocked"

    blocked = {entry["well"]: entry["blockers"] for entry in detail["blocked_wells"]}
    assert set(blocked) == {"A1", "B1"}

    b1_blockers = blocked["B1"]
    assert len(b1_blockers) == 1
    assert b1_blockers[0]["type"] == "batch"
    assert b1_blockers[0]["batch_id"] == b2
    assert b1_blockers[0]["operation"]["source"] == "B1"
    assert b1_blockers[0]["request_key"] == "b2"

    a1_blockers = blocked["A1"]
    assert len(a1_blockers) == 1
    assert a1_blockers[0]["type"] == "top_up"
    assert a1_blockers[0]["sample"] == "S9"
    assert a1_blockers[0]["volume"] == "5"

    # 冲突时液体状态保持不变
    assert client.get("/wells/A1").json()["volume"] == "65"
    assert client.get("/wells/B1").json()["volume"] == "30"
    assert client.get("/wells/A2").json()["volume"] == "70"
    # 冲突不落审计：批次仍可撤销态
    assert client.get(f"/batches/{b1}").json()["reversed"] is False


def test_reversing_later_batch_unblocks_earlier_one(client):
    register(client, "A1", "S1", "100")
    b1 = _submit_simple_batch(client, "b1", "A1", "B1", "40")
    b2 = _submit_simple_batch(client, "b2", "B1", "C1", "10")

    # b2 仍生效时 b1 不可撤销
    resp = _reverse(client, b1, "rev-b1-first")
    assert resp.status_code == 409
    blocked = {e["well"] for e in resp.json()["detail"]["blocked_wells"]}
    assert blocked == {"B1"}

    # 先撤销 b2（它触及 B1/C1，之后无操作），再撤销 b1 即放行
    assert _reverse(client, b2, "rev-b2").status_code == 201
    assert client.get("/wells/C1").status_code == 404
    resp = _reverse(client, b1, "rev-b1-second")
    assert resp.status_code == 201, resp.text
    assert client.get("/wells/A1").json()["volume"] == "100"
    assert client.get("/wells/B1").status_code == 404


def test_topup_before_batch_does_not_block_reversal(client):
    # 提交任何批次之前的补液：after_batch_id 为空，不阻塞之后的任何批次
    register(client, "A1", "S1", "100")
    bid = _submit_simple_batch(client, "b1", "A1", "B1", "40")

    # 批次之后对未触及孔补液：同样不阻塞
    register(client, "Z1", "SZ", "7")

    assert _reverse(client, bid, "rev-1").status_code == 201
    assert client.get("/wells/A1").json()["volume"] == "100"
    assert client.get("/wells/B1").status_code == 404
    assert client.get("/wells/Z1").json()["volume"] == "7"

    # 恢复后的新基线上再补液，孔位状态正确
    register(client, "A1", "S2", "5")
    assert client.get("/wells/A1").json()["volume"] == "105"


def test_reversal_is_idempotent_same_key_same_target(client):
    register(client, "A1", "S1", "100")
    bid = _submit_simple_batch(client, "b1", "A1", "B1", "40")

    first = _reverse(client, bid, "rev-key")
    assert first.status_code == 201
    second = _reverse(client, bid, "rev-key")
    assert second.status_code == 200
    assert second.json()["replayed"] is True
    assert second.json()["reversal_id"] == first.json()["reversal_id"]
    # 只恢复一次
    assert client.get("/wells/A1").json()["volume"] == "100"
    assert client.get("/wells/B1").status_code == 404


def test_reversal_same_key_different_target_is_409(client):
    register(client, "A1", "S1", "100")
    register(client, "A2", "S2", "50")
    b1 = _submit_simple_batch(client, "b1", "A1", "B1", "10")
    b2 = _submit_simple_batch(client, "b2", "A2", "B2", "10")

    assert _reverse(client, b1, "shared-key").status_code == 201
    resp = _reverse(client, b2, "shared-key")
    assert resp.status_code == 409
    detail = resp.json()["detail"]
    assert detail["code"] == "request_key_target_mismatch"
    assert detail["batch_id"] == b1
    # b2 未被撤销，液体不动
    assert client.get("/wells/A2").json()["volume"] == "40"
    assert client.get("/wells/B2").json()["volume"] == "10"


def test_legacy_batch_without_snapshot_is_not_reversible(client, monkeypatch):
    """上线前已存在的批次没有快照：直接写一条老数据模拟，撤销须明确 409。"""
    register(client, "A1", "S1", "100")
    conn = db_module.connect()
    try:
        cur = conn.execute(
            "INSERT INTO batches (request_key, status, error, created_at) "
            "VALUES ('legacy-1', 'committed', NULL, '2026-09-01T00:00:00+00:00')"
        )
        legacy_id = int(cur.lastrowid)
        conn.execute(
            "INSERT INTO operations (batch_id, step, source, destination, volume) "
            "VALUES (?, 1, 'A1', 'B1', '40')",
            (legacy_id,),
        )
        conn.commit()
    finally:
        conn.close()

    resp = _reverse(client, legacy_id, "rev-legacy")
    assert resp.status_code == 409
    detail = resp.json()["detail"]
    assert detail["code"] == "snapshot_missing"
    # 液体状态保持
    assert client.get("/wells/A1").json()["volume"] == "100"
    assert client.get("/wells/B1").status_code == 404


def test_reversing_failed_batch_or_already_reversed_is_409(client):
    register(client, "A1", "S1", "10")
    failed = client.post(
        "/batches",
        json={
            "request_key": "fail",
            "operations": [{"source": "A1", "destination": "B1", "volume": "50"}],
        },
    ).json()["detail"]["batch_id"]
    resp = _reverse(client, failed, "rev-failed")
    assert resp.status_code == 409
    assert resp.json()["detail"]["code"] == "batch_not_committed"

    bid = _submit_simple_batch(client, "ok", "A1", "B1", "5")
    assert _reverse(client, bid, "rev-ok").status_code == 201
    resp = _reverse(client, bid, "rev-ok-again")
    assert resp.status_code == 409
    assert resp.json()["detail"]["code"] == "already_reversed"


def test_reversal_unknown_batch_is_404(client):
    assert _reverse(client, 9999, "rev-ghost").status_code == 404


def test_reversal_restores_mixed_composition_with_decimal_precision(client):
    """混合孔的完整组成随快照恢复，量与占比、精度全部回到批前。"""
    register(client, "A1", "S1", "0.000000002")
    register(client, "A1", "S2", "0.000000002")
    bid = _submit_simple_batch(client, "mix", "A1", "B1", "0.000000003")

    b1_before = client.get("/wells/B1").json()
    assert b1_before["volume"] == "0.000000003"
    assert_invariant(client, "A1")
    assert_invariant(client, "B1")

    assert _reverse(client, bid, "rev-mix").status_code == 201
    a1 = assert_invariant(client, "A1")
    assert amounts(a1) == {
        "S1": Decimal("0.000000002"),
        "S2": Decimal("0.000000002"),
    }
    assert a1["volume"] == "0.000000004"
    assert client.get("/wells/B1").status_code == 404


def test_reversal_restores_then_new_batch_can_run(client):
    """恢复后的孔可作为新批次的基线正常使用；新批次不再被旧撤销影响。"""
    register(client, "A1", "S1", "100")
    bid = _submit_simple_batch(client, "old", "A1", "B1", "60")
    assert _reverse(client, bid, "rev-old").status_code == 201

    new_id = _submit_simple_batch(client, "new", "A1", "C1", "30")
    assert client.get("/wells/A1").json()["volume"] == "70"
    assert client.get("/wells/C1").json()["volume"] == "30"
    assert client.get("/wells/B1").status_code == 404

    # 老批次已撤销，不能再动；新批次可在无后续操作时撤销
    assert _reverse(client, bid, "rev-old-2").status_code == 409
    assert _reverse(client, new_id, "rev-new").status_code == 201
    assert client.get("/wells/A1").json()["volume"] == "100"
    assert client.get("/wells/C1").status_code == 404


def test_reversal_request_key_is_separate_from_batch_keys(client):
    """撤销键与批次键空间独立：同名键可分别用于提交与撤销。"""
    register(client, "A1", "S1", "100")
    # 批次先用了 "same"
    bid = _submit_simple_batch(client, "same", "A1", "B1", "10")
    # 撤销再用 "same" 不冲突
    resp = _reverse(client, bid, "same")
    assert resp.status_code == 201, resp.text


def test_snapshots_roll_back_with_failed_batch(client):
    """失败批次不留下快照：其 request_key 下批次为 failed，无撤销入口可用。"""
    register(client, "A1", "S1", "10")
    resp = client.post(
        "/batches",
        json={
            "request_key": "snap-fail",
            "operations": [{"source": "A1", "destination": "B1", "volume": "50"}],
        },
    )
    assert resp.status_code == 409
    bid = resp.json()["detail"]["batch_id"]
    conn = db_module.connect()
    try:
        rows = conn.execute(
            "SELECT COUNT(*) AS n FROM batch_well_snapshots WHERE batch_id = ?",
            (bid,),
        ).fetchone()
        assert rows["n"] == 0
    finally:
        conn.close()


def test_concurrent_same_key_reversal_is_idempotent(client):
    """两个同键并发撤销：恰好一个 201，另一个按重放返回 200，液体只恢复一次。"""
    import threading
    from fastapi.testclient import TestClient

    from app.main import app

    register(client, "A1", "S1", "100")
    bid = _submit_simple_batch(client, "concurrent-batch", "A1", "B1", "30")

    results: list = []

    def fire() -> None:
        local = TestClient(app)
        results.append(_reverse(local, bid, "concurrent-rev"))

    t1 = threading.Thread(target=fire)
    t2 = threading.Thread(target=fire)
    t1.start()
    t2.start()
    t1.join()
    t2.join()

    assert sorted(r.status_code for r in results) == [200, 201]
    replay = next(r for r in results if r.status_code == 200)
    assert replay.json()["replayed"] is True
    assert results[0].json()["reversal_id"] == results[1].json()["reversal_id"]
    assert client.get("/wells/A1").json()["volume"] == "100"
    assert client.get("/wells/B1").status_code == 404


def test_concurrent_reversal_and_topup_are_serializable(tmp_path, monkeypatch):
    """并发的撤销与补液：要么补液阻塞撤销（体积 77），要么撤销先成功、补液落在恢复后（107）。

    两种结局都是合法的串行化结果；反复尝试直至两种顺序都被覆盖到，
    并断言不会出现更新丢失（如 70 或 100）。每轮使用独立临时库避免跨轮累积。
    """
    import threading
    from fastapi.testclient import TestClient

    from app.main import app

    seen = set()
    for attempt in range(50):
        db_path = tmp_path / f"race-{attempt}.db"
        monkeypatch.setenv("LINEAGE_DB", str(db_path))

        with TestClient(app) as setup:
            setup.post("/wells", json={"well": "A1", "sample": "S1", "volume": "100"})
            bid = setup.post(
                "/batches",
                json={
                    "request_key": "race-batch",
                    "operations": [{"source": "A1", "destination": "B1", "volume": "30"}],
                },
            ).json()["batch_id"]

        outcome: dict = {}
        barrier = threading.Barrier(2)

        def fire_reverse() -> None:
            barrier.wait()
            local = TestClient(app)
            outcome["rev"] = local.post(
                f"/batches/{bid}/reversals", json={"request_key": "race-rev"}
            ).status_code

        def fire_topup() -> None:
            barrier.wait()
            local = TestClient(app)
            outcome["top"] = local.post(
                "/wells", json={"well": "A1", "sample": "SX", "volume": "7"}
            ).status_code

        t1 = threading.Thread(target=fire_reverse)
        t2 = threading.Thread(target=fire_topup)
        t1.start()
        t2.start()
        t1.join()
        t2.join()

        with TestClient(app) as check:
            volume = check.get("/wells/A1").json()["volume"]
        if outcome["rev"] == 409:  # 补液先 -> 阻塞撤销
            assert outcome["top"] == 201 and volume == "77"
            seen.add("topup_first")
        else:  # 撤销先 -> 补液叠加在恢复后的 100 上
            assert outcome["rev"] == 201 and outcome["top"] == 201 and volume == "107"
            seen.add("reverse_first")
        if seen == {"topup_first", "reverse_first"}:
            break

    assert seen == {"topup_first", "reverse_first"}, seen
