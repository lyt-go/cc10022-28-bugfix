"""渠道对账路由：把可用黄金查询数据与不可解释异常一并交给渠道接口。"""
from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query

from app.schemas import ActionResult
from app.services.mdm_api import MdmApiService

router = APIRouter(prefix="/api/channel", tags=["渠道对账"])


def _require_published_api(api_id: int):
    service = MdmApiService()
    api_def = service.get_entry(api_id)
    if not api_def or api_def.get("status") != "已发布":
        raise HTTPException(status_code=404, detail=f"已发布查询接口 {api_id} 不存在或不可用")
    return service, api_def


def _build_filters(api_def: dict, query_field: str | None,
                     keyword: str | None) -> dict[str, str]:
    field = query_field or api_def.get("查询字段", "")
    if field and keyword:
        return {field: keyword}
    return {}


@router.get("/{api_id}/reconcile")
def reconcile_channel(api_id: int,
                        query_field: str | None = Query(default=None),
                        keyword: str | None = Query(default=None)) -> dict:
    """先按名单做一次渠道对账：返回可用黄金记录、异常记录与覆盖结论。"""
    service, api_def = _require_published_api(api_id)
    filters = _build_filters(api_def, query_field, keyword)
    return service.reconcile_channel_golden_records(api_id, filters=filters)


@router.post("/{api_id}/retry")
def retry_channel(api_id: int,
                    query_field: str | None = Query(default=None),
                    keyword: str | None = Query(default=None)) -> dict:
    """改正异常分数后重新查询：重跑对账并核对剩余无法评估的记录。"""
    service, api_def = _require_published_api(api_id)
    filters = _build_filters(api_def, query_field, keyword)
    return service.retry_channel_reconciliation(api_id, filters=filters)


@router.post("/{api_id}/repair-confidence", response_model=ActionResult)
def repair_confidence(api_id: int, payload: dict) -> ActionResult:
    """修正指定黄金记录的异常置信度，配合重试完成全量数据修正。"""
    _require_published_api(api_id)
    golden_id = str(payload.get("golden_id", "")).strip()
    if not golden_id:
        return ActionResult(ok=False, message="缺少必填字段：golden_id")
    from app.services.golden_record import GoldenRecordService
    result = GoldenRecordService().repair_golden_confidence(
        golden_id, payload.get("confidence"))
    if not result["ok"]:
        return ActionResult(ok=False, message=result["reason"])
    return ActionResult(ok=True, message="异常置信度已修正，可重新发起渠道对账", entry=result)
