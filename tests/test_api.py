"""端到端 API 测试。"""

from decimal import Decimal


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
