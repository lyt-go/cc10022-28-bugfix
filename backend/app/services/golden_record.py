"""黄金记录领域服务：黄金记录创建、关联来源、置信度计算与版本管理。"""
from __future__ import annotations

import datetime
from typing import Any

from app.services.base import BaseModuleService
from app.store import store


def parse_confidence(value: Any) -> float | None:
    """解析置信度字段：合法数字返回 float，「待核」等非数字值返回 None。

    存量历史记录的置信度可能被写成待核文本，直接 float() 会让整批查询报错；
    统一经由本函数读取，非数字值显式表达为「无法评估」，而不是当作 0 分或
    放任异常向上抛出，避免未知客户被误判为满足置信度门槛。
    """
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value or "").strip()
    if not text:
        return None
    try:
        return float(text)
    except (TypeError, ValueError):
        return None


class GoldenRecordService(BaseModuleService):
    MODULE = "golden_record"
    ENTITY = "黄金记录"
    OPERATION_TERM = "关联"
    LIST_FIELDS = ["黄金ID", "实体模型", "关联来源", "关键字段", "代表字段", "置信度", "最后合并", "记录版本"]
    REQUIRED_FIELDS = ["黄金ID", "实体模型", "关联来源"]
    STATUS_ORDER = ["草稿", "待校验", "已生效", "已停用"]
    ACTION_RULES = {"提交校验": "待校验", "标记生效": "已生效", "停用记录": "已停用"}
    NEGATIVE_ACTIONS = ["停用记录"]

    def create_golden_record(self, entity_model: str, merged_data: dict[str, Any],
                               source_ids: list[str]) -> dict[str, Any]:
        """创建黄金记录：将合并后的代表记录写入黄金记录存储。

        写入黄金记录到golden_records存储，关联来源批次与合并追溯信息，
        设置置信度、关联来源和最后合并时间，生成唯一黄金ID。
        """
        rows = store.rows(self.MODULE)
        golden_id = f"GR-{entity_model}-{datetime.datetime.utcnow().strftime('%Y%m%d%H%M%S')}"
        confidence = self._compute_confidence(len(source_ids))
        entry = {
            "id": max((int(r.get("id", 0)) for r in rows), default=0) + 1,
            "黄金ID": golden_id,
            "实体模型": entity_model,
            "关联来源": "|".join(source_ids),
            "关键字段": ",".join(k for k in merged_data if not k.startswith("_")),
            "代表字段": str(merged_data.get("representative", {})),
            "置信度": f"{confidence:.2f}",
            "最后合并": datetime.datetime.utcnow().isoformat(),
            "记录版本": "1.0",
            "status": "已生效",
            "pending": False,
            "abnormal": False,
            "source_count": len(source_ids),
        }
        for key, val in merged_data.items():
            if not key.startswith("_"):
                entry[key] = val
        rows.append(entry)
        # 写入黄金记录到golden_records存储，关联来源批次与合并追溯信息
        return {"ok": True, "golden_record": entry}

    def _compute_confidence(self, source_count: int) -> float:
        """计算黄金记录置信度：基于来源数量和质量指标综合评估。"""
        base = 0.5
        source_bonus = min(source_count * 0.1, 0.3)
        return min(round(base + source_bonus, 2), 0.99)

    def query_golden_record(self, golden_id: str) -> dict[str, Any] | None:
        """查询黄金记录：按黄金ID检索golden_records存储。

        查询黄金记录的完整字段信息、关联来源列表和置信度评分，
        返回黄金记录的完整快照数据。
        """
        rows = store.rows(self.MODULE)
        for row in rows:
            if row.get("黄金ID") == golden_id:
                # 查询黄金记录完整信息：包含关联来源和置信度
                return dict(row)
        return None

    def link_source_to_golden(self, golden_id: str, new_source_id: str) -> dict[str, Any]:
        """关联新来源记录到已有黄金记录。

        将新增来源记录关联到已有黄金记录，更新关联来源列表，
        重新计算置信度评分并更新最后合并时间。
        """
        record = self.query_golden_record(golden_id)
        if not record:
            return {"ok": False, "reason": "黄金记录不存在"}
        sources = record.get("关联来源", "").split("|")
        if "" in sources:
            sources.remove("")
        if new_source_id in sources:
            return {"ok": False, "reason": "来源已关联"}
        sources.append(new_source_id)
        record["关联来源"] = "|".join(sources)
        record["source_count"] = len(sources)
        record["置信度"] = f"{self._compute_confidence(len(sources)):.2f}"
        # 关联新来源记录：更新黄金记录的来源列表和置信度
        return {"ok": True, "golden_id": golden_id, "source_count": len(sources)}

    def validate_golden_completeness(self, golden_id: str,
                                      required_fields: list[str]) -> dict[str, Any]:
        """校验黄金记录的字段完整性：检查所有必填字段是否已填充。

        查询黄金记录数据，读取golden_records存储中的字段值，
        校验必填字段完整性并输出完备率统计。
        """
        record = self.query_golden_record(golden_id)
        if not record:
            return {"ok": False, "reason": "黄金记录不存在"}
        missing = [f for f in required_fields if not str(record.get(f, "")).strip()]
        complete_count = len(required_fields) - len(missing)
        return {
            "golden_id": golden_id,
            "total_required": len(required_fields),
            "filled": complete_count,
            "missing": missing,
            "completeness": round(complete_count / max(len(required_fields), 1) * 100, 2),
        }

    def search_golden_records(self, entity_model: str | None = None,
                                min_confidence: float = 0.0) -> list[dict[str, Any]]:
        """搜索黄金记录：按实体模型和最低置信度筛选。

        查询golden_records存储，按实体模型筛选并过滤置信度
        低于阈值的记录，返回符合条件的黄金记录列表。

        置信度为「待核」等非数字值的存量记录无法评估，不能按 0 分参与
        阈值判定，因此只返回有效置信度记录，异常记录由
        search_golden_records_with_anomalies 单独披露，保证整批查询不再报错。
        """
        eligible, _anomalies = self.search_golden_records_with_anomalies(
            entity_model=entity_model, min_confidence=min_confidence)
        return eligible

    def search_golden_records_with_anomalies(
        self,
        entity_model: str | None = None,
        min_confidence: float = 0.0,
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        """搜索黄金记录并分离异常记录：渠道名单与黄金检索共用同一份读取逻辑。

        数字阈值与排序规则保持不变，只对可解析为数字的有效置信度生效；
        非数字置信度（如待核）被标记为无法评估，既不满足任何门槛、也不
        参与排序，作为异常记录单独返回，支撑正常记录对账与异常后的重试。

        返回 (eligible, anomalies)：
        - eligible：有效置信度且达到 min_confidence 的记录，按置信度降序；
        - anomalies：非数字/缺失置信度的记录，附 confidence_status=无法评估、
          confidence_raw 原值和异常原因。
        """
        rows = store.rows(self.MODULE)
        effective_rows = [row for row in rows if row.get("status") == "已生效"]
        if entity_model:
            effective_rows = [r for r in effective_rows if r.get("实体模型") == entity_model]

        eligible: list[dict[str, Any]] = []
        anomalies: list[dict[str, Any]] = []
        for row in effective_rows:
            score = parse_confidence(row.get("置信度"))
            if score is None:
                flagged = dict(row)
                flagged["confidence_status"] = "无法评估"
                flagged["confidence_raw"] = row.get("置信度")
                flagged["anomaly_reason"] = "置信度字段不是可解析数字，无法评估是否满足门槛"
                anomalies.append(flagged)
                continue
            # 仍按原有数字阈值判定，未知分数不会被当作满足门槛
            if score >= min_confidence:
                eligible.append(row)
        # 仍按原有规则对有效置信度降序排序
        eligible.sort(key=lambda r: parse_confidence(r.get("置信度")) or 0.0, reverse=True)
        return eligible, anomalies

    def repair_golden_confidence(self, golden_id: str, confidence: Any) -> dict[str, Any]:
        """修正黄金记录的异常置信度：把待核等非数字值改为合法数字分数。

        供异常修复后重新对账使用：新值必须可解析为数字，否则拒绝写入，
        避免再次产生无法评估的存量记录。
        """
        record = self.query_golden_record(golden_id)
        if not record:
            return {"ok": False, "reason": "黄金记录不存在"}
        score = parse_confidence(confidence)
        if score is None:
            return {
                "ok": False,
                "reason": f"新置信度 {confidence!r} 不是合法数字，无法完成修正",
            }
        # query_golden_record 返回的是快照副本，必须回写到存储中的原始行
        target = next(
            (row for row in store.rows(self.MODULE) if row.get("黄金ID") == golden_id),
            None,
        )
        if target is None:
            return {"ok": False, "reason": "黄金记录不存在"}
        target["置信度"] = f"{score:.2f}"
        target["abnormal"] = False
        return {
            "ok": True,
            "golden_id": golden_id,
            "置信度": target["置信度"],
            "confidence_status": "有效",
        }

    def export_golden_records(self, entity_model: str) -> list[dict[str, Any]]:
        """导出黄金记录：将指定实体模型的黄金记录导出供下游系统消费。

        查询golden_records存储中已生效的黄金记录，导出完整的
        记录数据，包含关键字段、代表字段和置信度信息。
        """
        records = self.search_golden_records(entity_model=entity_model, min_confidence=0.5)
        # 导出黄金记录：将已生效记录数据导出供下游系统分发同步
        exported = []
        for r in records:
            exported.append({
                "golden_id": r.get("黄金ID"),
                "entity_model": r.get("实体模型"),
                "confidence": parse_confidence(r.get("置信度")) or 0.0,
                "sources": (r.get("关联来源", "")).split("|"),
                "key_fields": (r.get("关键字段", "")).split(","),
            })
        return exported