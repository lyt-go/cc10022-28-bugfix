"""查询接口领域服务：黄金记录查询、字段标准化输出与调用统计。"""
from __future__ import annotations

from typing import Any

from app.services.base import BaseModuleService
from app.services.golden_record import GoldenRecordService
from app.store import store


class MdmApiService(BaseModuleService):
    MODULE = "mdm_api"
    ENTITY = "查询接口"
    OPERATION_TERM = "标准化"
    LIST_FIELDS = ["接口编号", "实体模型", "查询字段", "返回字段", "缓存时长", "限流阈值", "调用量统计", "接口版本"]
    REQUIRED_FIELDS = ["接口编号", "实体模型", "查询字段"]
    STATUS_ORDER = ["草稿", "待校验", "已发布", "已下线"]
    ACTION_RULES = {"提交校验": "待校验", "发布接口": "已发布", "下线接口": "已下线"}
    NEGATIVE_ACTIONS = ["下线接口"]

    def query_golden_records_by_api(self, api_id: int,
                                      filters: dict[str, str] | None = None) -> dict[str, Any]:
        """按查询接口定义查询黄金记录：标准化字段输出。

        读取查询接口配置，按查询字段标准对黄金记录执行检索，
        标准化返回字段格式并记录调用量统计。

        与渠道对账共用同一份存量记录读取逻辑：可用黄金查询数据与
        无法解释的异常记录同时返回，待核等非数字置信度不再导致整批报错。
        """
        result = self.reconcile_channel_golden_records(api_id, filters=filters)
        if not result["ok"]:
            return {"ok": False, "reason": result.get("reason", "查询接口不可用")}
        return {
            "ok": True,
            "records": result["records"],
            "count": result["count"],
            "api_id": result["api_id"],
            "anomalies": result["anomalies"],
            "anomaly_count": result["anomaly_count"],
            "coverage": result["coverage"],
        }

    def reconcile_channel_golden_records(
        self,
        api_id: int,
        filters: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        """渠道对账：从同一份存量黄金记录取数，分离可用数据与不可解释异常。

        渠道名单列表与黄金检索入口都经由本方法读取，保证两处数据口径一致：
        - 有效置信度（可解析数字）的记录按原数字阈值筛选、原排序规则降序，
          经接口返回字段标准化后作为可用黄金查询数据，支撑正常记录对账；
        - 置信度为待核等非数字值的记录无法评估，不满足任何门槛、不混入名单，
          原样作为异常记录交给渠道接口，并给出异常原因，供修正后重试；
        - 输出覆盖结论，说明名单当前能否用于核对有效客户，还是仍需等待
          全量数据修正，并注明因失败暂不能覆盖的人群。
        """
        api_def = self.get_entry(api_id)
        if not api_def or api_def.get("status") != "已发布":
            return {"ok": False, "reason": "查询接口不可用"}
        golden_svc = GoldenRecordService()
        entity_model = api_def.get("实体模型", "")
        query_field = api_def.get("查询字段", "")
        return_fields = [
            f.strip() for f in api_def.get("返回字段", "").split(",") if f.strip()
        ]
        # 同一次渠道对账的两类记录来自同一份存量记录
        eligible, anomalies = golden_svc.search_golden_records_with_anomalies(
            entity_model=entity_model
        )
        if filters and query_field:
            keyword = filters.get(query_field, "")
            eligible = [
                r for r in eligible
                if keyword.casefold() in str(r.get(query_field, "")).casefold()
            ]
            anomalies = [
                r for r in anomalies
                if keyword.casefold() in str(r.get(query_field, "")).casefold()
            ]
        # 标准化可用记录：按接口定义筛选并格式化返回字段
        if return_fields:
            projected = [
                {f: r.get(f) for f in return_fields if f in r} for r in eligible
            ]
        else:
            projected = list(eligible)
        anomaly_rows = [
            {
                "黄金ID": r.get("黄金ID"),
                "实体模型": r.get("实体模型"),
                "置信度": r.get("置信度"),
                "confidence_status": r.get("confidence_status", "无法评估"),
                "anomaly_reason": r.get(
                    "anomaly_reason", "置信度字段不是可解析数字，无法评估是否满足门槛"
                ),
            }
            for r in anomalies
        ]
        self._record_api_call(api_id)
        return {
            "ok": True,
            "api_id": api_def.get("接口编号"),
            "entity_model": entity_model,
            "records": projected,
            "count": len(projected),
            "anomalies": anomaly_rows,
            "anomaly_count": len(anomaly_rows),
            "coverage": self._build_coverage(entity_model, len(projected), anomaly_rows),
        }

    def retry_channel_reconciliation(
        self,
        api_id: int,
        filters: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        """异常分数改正后的重新对账入口：重跑同一份对账逻辑并核对残留异常。

        修复动作（GoldenRecordService.repair_golden_confidence）完成后调用，
        重新读取存量记录，返回最新可用名单与仍无法解释的异常，
        让使用者判断是否已经可以全量覆盖。
        """
        result = self.reconcile_channel_golden_records(api_id, filters=filters)
        if not result["ok"]:
            return result
        result["retry"] = True
        result["coverage"]["remaining_anomalies"] = result["anomaly_count"]
        return result

    def _build_coverage(self, entity_model: str,
                          usable_count: int,
                          anomalies: list[dict[str, Any]]) -> dict[str, Any]:
        """汇总本次对账的覆盖结论：可用名单规模、未覆盖人群与可用性判断。"""
        uncovered = [
            {
                "黄金ID": row.get("黄金ID"),
                "reason": row.get("anomaly_reason"),
                "raw_confidence": row.get("置信度"),
            }
            for row in anomalies
        ]
        if uncovered:
            decision = "部分可用：名单可用于核对有效客户，仍需等待全量数据修正"
        else:
            decision = "完整覆盖：名单可用于核对全部有效客户"
        return {
            "entity_model": entity_model,
            "usable_records": usable_count,
            "uncovered_count": len(uncovered),
            "uncovered_population": uncovered,
            "has_unexplainable_fields": len(uncovered) > 0,
            "decision": decision,
        }

    def _record_api_call(self, api_id: int) -> None:
        """记录查询接口调用量统计。"""
        api_def = self.get_entry(api_id)
        if api_def:
            try:
                current = int(str(api_def.get("调用量统计", "0") or "0").strip() or "0")
            except (TypeError, ValueError):
                current = 0
            api_def["调用量统计"] = str(current + 1)

    def query_list_active_apis(self, entity_model: str | None = None) -> list[dict[str, Any]]:
        """查询活跃查询接口列表：按实体模型筛选已发布接口。

        遍历查询接口存储，检索已发布状态的查询接口定义，
        包含查询字段、返回字段、缓存时长和限流阈值配置。
        """
        rows = store.rows(self.MODULE)
        apis = [row for row in rows if row.get("status") == "已发布"]
        if entity_model:
            apis = [a for a in apis if a.get("实体模型") == entity_model]
        # 查询活跃查询接口列表：筛选已发布且匹配实体模型的接口
        return apis

    def validate_api_rate_limit(self, api_id: int) -> dict[str, Any]:
        """校验查询接口限流状态：检查当前调用量是否超过限流阈值。

        读取查询接口的调用量统计和限流阈值，校验是否触发
        限流保护并输出当前调用状态报告。
        """
        api_def = self.get_entry(api_id)
        if not api_def:
            return {"ok": False, "reason": "接口不存在"}
        try:
            call_count = int(str(api_def.get("调用量统计", "0") or "0").strip() or "0")
        except (TypeError, ValueError):
            call_count = 0
        try:
            rate_limit = int(str(api_def.get("限流阈值", "100") or "100").strip() or "100")
        except (TypeError, ValueError):
            rate_limit = 100
        is_limited = call_count >= rate_limit
        return {
            "api_id": api_def.get("接口编号"),
            "call_count": call_count,
            "rate_limit": rate_limit,
            "is_limited": is_limited,
            "remaining": max(rate_limit - call_count, 0),
        }

    def export_api_response(self, api_id: int,
                              format_type: str = "json") -> list[dict[str, Any]]:
        """导出查询接口响应：按指定格式导出黄金记录查询结果。

        标准化查询接口的输出格式，将黄金记录按字段约定
        转换为JSON/CSV格式供下游系统消费。仅导出有效置信度的可用记录，
        无法评估的异常记录通过对账接口单独披露。
        """
        api_def = self.get_entry(api_id)
        if not api_def:
            return []
        golden_svc = GoldenRecordService()
        records = golden_svc.search_golden_records(
            entity_model=api_def.get("实体模型", "")
        )
        return_fields = [
            f.strip() for f in api_def.get("返回字段", "").split(",") if f.strip()
        ]
        # 导出查询接口响应：标准化输出格式并返回可用黄金记录
        if return_fields:
            return [{f: r.get(f) for f in return_fields if f in r} for r in records]
        return list(records)
