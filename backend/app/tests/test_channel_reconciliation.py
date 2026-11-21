"""渠道对账测试：非数字置信度不再整批报错，可用数据与异常分离并支持修复重试。"""
from __future__ import annotations

import os
import sys

_backend_dir = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _backend_dir not in sys.path:
    sys.path.insert(0, _backend_dir)

from app.services.golden_record import (
    GoldenRecordService,
    parse_confidence,
)
from app.services.mdm_api import MdmApiService
from app.store import store


def _seed_scenario() -> tuple[int, str, str]:
    """构造同模型两条存量记录：一条待核、一条 0.81，并登记发布渠道接口。"""
    golden_rows = store.rows("golden_record")
    if any(r.get("黄金ID") == "GR-CHANNEL-BAD" for r in golden_rows):
        api_rows = store.rows("mdm_api")
        api_row = next(r for r in api_rows if r.get("接口编号") == "API-CHANNEL-001")
        return int(api_row["id"]), "GR-CHANNEL-BAD", "GR-CHANNEL-GOOD"
    next_id = max((int(r.get("id", 0)) for r in golden_rows), default=0) + 1
    bad_id, good_id = next_id, next_id + 1
    model = "channel_test_customer"
    golden_rows.extend([
        {
            "id": bad_id, "status": "已生效", "pending": False, "abnormal": True,
            "黄金ID": "GR-CHANNEL-BAD", "实体模型": model, "关联来源": "SRC-CH-1",
            "关键字段": "name", "代表字段": "{}", "置信度": "待核",
            "最后合并": "2026-11-10", "记录版本": "1.0",
        },
        {
            "id": good_id, "status": "已生效", "pending": False, "abnormal": False,
            "黄金ID": "GR-CHANNEL-GOOD", "实体模型": model, "关联来源": "SRC-CH-2",
            "关键字段": "name", "代表字段": "{}", "置信度": "0.81",
            "最后合并": "2026-11-11", "记录版本": "1.0",
        },
    ])

    api_svc = MdmApiService()
    api_rows = store.rows("mdm_api")
    api_id = max((int(r.get("id", 0)) for r in api_rows), default=0) + 1
    api_rows.append({
        "id": api_id, "status": "已发布", "pending": False, "abnormal": False,
        "接口编号": "API-CHANNEL-001", "实体模型": model, "查询字段": "黄金ID",
        "返回字段": "黄金ID,实体模型,置信度", "缓存时长": "300",
        "限流阈值": "100", "调用量统计": "0", "接口版本": "1.0",
    })
    return api_id, "GR-CHANNEL-BAD", "GR-CHANNEL-GOOD"


class TestChannelReconciliation:
    """渠道名单列表与黄金检索共用同一份存量记录的置信度读取链路。"""

    def test_parse_confidence_marks_non_numeric_unevaluable(self):
        """非数字值必须被标为无法评估，合法数字正常解析。"""
        assert parse_confidence("0.81") == 0.81
        assert parse_confidence("待核") is None
        assert parse_confidence("") is None
        assert parse_confidence(None) is None

    def test_search_no_longer_batch_fails_and_keeps_valid_record(self):
        """待核记录不再让整批搜索报错，0.81 正常进入结果，异常单独披露。"""
        _seed_scenario()
        golden_svc = GoldenRecordService()
        eligible, anomalies = golden_svc.search_golden_records_with_anomalies(
            entity_model="channel_test_customer")
        good = next(r for r in eligible if r["黄金ID"] == "GR-CHANNEL-GOOD")
        assert parse_confidence(good["置信度"]) == 0.81, "合法 0.81 记录应可用"
        assert all(r["黄金ID"] != "GR-CHANNEL-BAD" for r in eligible), \
            "无法评估的待核记录不得混入有效名单"
        bad = next(r for r in anomalies if r["黄金ID"] == "GR-CHANNEL-BAD")
        assert bad["confidence_status"] == "无法评估"
        assert "置信度" in bad["anomaly_reason"] or "门槛" in bad["anomaly_reason"]
        # 排序规则不变：有效置信度降序
        scores = [parse_confidence(r["置信度"]) or 0.0 for r in eligible]
        assert scores == sorted(scores, reverse=True)
        # 兼容旧入口：只返回有效记录且不抛异常
        legacy = golden_svc.search_golden_records("channel_test_customer")
        assert all(r["黄金ID"] != "GR-CHANNEL-BAD" for r in legacy)

    def test_reconcile_then_repair_then_retry(self):
        """先按名单对账，再修正异常分数重新查询，覆盖结论随修复更新。"""
        api_id, bad_golden_id, good_golden_id = _seed_scenario()
        api_svc = MdmApiService()
        # 重置为待核，保证同一进程内重复执行时场景一致
        bad_row = next(
            r for r in store.rows("golden_record") if r["黄金ID"] == bad_golden_id)
        bad_row["置信度"] = "待核"
        bad_row["abnormal"] = True

        # 第一次对账：两类记录同批处理，正常记录可对账、异常被披露
        first = api_svc.reconcile_channel_golden_records(api_id)
        assert first["ok"] is True
        returned_ids = [r["黄金ID"] for r in first["records"]]
        assert good_golden_id in returned_ids, "渠道名单应读取到正常的 0.81 记录"
        assert bad_golden_id not in returned_ids, "待核记录不得被当作满足门槛的客户"
        assert first["anomaly_count"] == 1
        assert first["anomalies"][0]["黄金ID"] == bad_golden_id
        assert first["anomalies"][0]["confidence_status"] == "无法评估"
        coverage = first["coverage"]
        assert coverage["has_unexplainable_fields"] is True
        assert coverage["uncovered_count"] == 1
        assert coverage["uncovered_population"][0]["黄金ID"] == bad_golden_id
        assert "部分可用" in coverage["decision"], "存在异常时应提示仍需全量修正"
        assert first["count"] == 1

        # 黄金检索入口与渠道名单读取同一份数据，口径一致
        search_view = api_svc.query_golden_records_by_api(api_id)
        assert search_view["ok"] is True
        assert [r["黄金ID"] for r in search_view["records"]] == returned_ids
        assert search_view["anomaly_count"] == 1

        # 修正异常分数：非数字新值必须被拒绝
        rejected = GoldenRecordService().repair_golden_confidence(bad_golden_id, "再看看")
        assert rejected["ok"] is False
        # 合法数字修正后重新对账
        repaired = GoldenRecordService().repair_golden_confidence(bad_golden_id, "0.76")
        assert repaired["ok"] is True and repaired["置信度"] == "0.76"

        retry = api_svc.retry_channel_reconciliation(api_id)
        assert retry["ok"] is True and retry["retry"] is True
        assert retry["anomaly_count"] == 0, "修正后不应再有无法解释的字段数据"
        assert retry["coverage"]["has_unexplainable_fields"] is False
        assert retry["coverage"]["uncovered_count"] == 0
        assert "完整覆盖" in retry["coverage"]["decision"]
        retry_ids = [r["黄金ID"] for r in retry["records"]]
        assert good_golden_id in retry_ids and bad_golden_id in retry_ids
        # 排序仍按有效置信度降序：0.81 在 0.76 前
        assert retry_ids.index(good_golden_id) < retry_ids.index(bad_golden_id)

    def test_unpublished_api_rejected(self):
        """未发布接口不能作为渠道对账入口。"""
        api_svc = MdmApiService()
        result = api_svc.reconcile_channel_golden_records(999_999)
        assert result["ok"] is False
        assert "不可用" in result["reason"]


if __name__ == "__main__":
    runner = TestChannelReconciliation()
    tests = [
        ("置信度解析：非数字标记无法评估", runner.test_parse_confidence_marks_non_numeric_unevaluable),
        ("搜索不再整批报错并保留0.81", runner.test_search_no_longer_batch_fails_and_keeps_valid_record),
        ("对账→修正→重试全链路", runner.test_reconcile_then_repair_then_retry),
        ("未发布接口拒绝对账", runner.test_unpublished_api_rejected),
    ]
    passed = failed = 0
    for name, test_fn in tests:
        try:
            test_fn()
            print(f"  PASS: {name}")
            passed += 1
        except Exception as exc:  # noqa: BLE001 - 脚本式测试直接打印失败原因
            print(f"  FAIL: {name} -> {type(exc).__name__}: {exc}")
            failed += 1
    print(f"\n{'='*60}")
    print(f"结果: {passed}/{len(tests)} 通过, {failed} 失败")
    assert failed == 0, f"{failed} 条渠道对账测试失败！"
