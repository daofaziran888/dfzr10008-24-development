"""数据筛选引擎接口：维护筛选模板，并支持一次提交多条模板的批量执行、断点续跑与下游分析入口。"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Query

from app.schemas import ActionResult, BatchPayload, EntryPayload, PageResult
from app.services.filter import FilterService

router = APIRouter(prefix="/api/filter", tags=["数据筛选引擎"])

service = FilterService()

LIST_FIELDS = ["模板编号", "模板名称", "目标数据源", "筛选条件", "排序字段", "分页配置", "结果格式", "模板状态"]
STATUSES = ["草稿", "已启用", "已暂停", "已废弃"]


@router.get("", response_model=PageResult[dict])
def list_entries(
    keyword: str | None = Query(default=None, description="按模板编号检索"),
    status: str | None = Query(default=None, description="草稿、已启用、已暂停、已废弃"),
    page: int = 1,
    size: int = 20,
) -> PageResult[dict]:
    """按模板编号与状态过滤数据筛选引擎列表；没有数据时返回空页，不报错。"""
    if size > 200:
        raise HTTPException(status_code=400, detail="每页最多 200 条，请缩小分页范围")
    items, total = service.list_entries(keyword=keyword, status=status, page=page, size=size)
    return PageResult(items=items, total=total, page=page, size=size)


@router.get("/data-sources", response_model=list[dict])
def list_data_sources() -> list[dict]:
    """读取可执行的目标数据源及字段口径，供批量执行前选择。"""
    return service.list_data_sources()


@router.get("/export")
def export_entries() -> dict[str, Any]:
    """导出数据筛选引擎清单：返回当前过滤条件下的全量数据。"""
    items, total = service.list_entries(page=1, size=10000)
    return {"module": "filter", "total": total, "items": items}


# ---------------------------------------------------------------- 批量执行与结果集
# 注意：具体路径要写在 /{entry_id} 之前，避免被整数路径参数抢先匹配。

@router.post("/batches", response_model=dict)
def execute_batch(payload: BatchPayload) -> dict[str, Any]:
    """一次提交多条模板并整组返回：失败模板单独说明，其他模板继续生成结果集。

    同一批参数重复提交直接复用原批次与原结果集，不重复写入。
    """
    try:
        return service.execute_batch(
            payload.templates,
            target_source=payload.target_source,
            stop_after=payload.stop_after,
        )
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error


@router.get("/batches", response_model=PageResult[dict])
def list_batches(page: int = 1, size: int = 20) -> PageResult[dict]:
    """按提交顺序列出执行批次，方便追溯每次批量执行的结果。"""
    if size > 200:
        raise HTTPException(status_code=400, detail="每页最多 200 条，请缩小分页范围")
    items, total = service.list_batches(page=page, size=size)
    return PageResult(items=items, total=total, page=page, size=size)


@router.get("/batches/{batch_no}", response_model=dict)
def get_batch(batch_no: str) -> dict[str, Any]:
    """读取单个批次的逐条执行结果。"""
    batch = service.get_batch(batch_no)
    if batch is None:
        raise HTTPException(status_code=404, detail=f"批次 {batch_no} 不存在")
    return batch


@router.post("/batches/{batch_no}/resume", response_model=dict)
def resume_batch(batch_no: str) -> dict[str, Any]:
    """批处理中断后从失败/未执行模板继续，已成功的结果集保持不变。"""
    try:
        return service.resume_batch(batch_no)
    except ValueError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error


@router.post("/batches/{batch_no}/items/{template_code}/retry", response_model=dict)
def retry_item(batch_no: str, template_code: str) -> dict[str, Any]:
    """单独重跑失败模板；已成功的模板复用原结果集，不重复写入。"""
    try:
        return service.retry_item(batch_no, template_code)
    except ValueError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error


@router.get("/results/{result_no}", response_model=dict)
def get_result(result_no: str) -> dict[str, Any]:
    """读取冻结的筛选结果集：条件、展示顺序、行数据与追溯信息一并返回。"""
    result = service.get_result(result_no)
    if result is None:
        raise HTTPException(status_code=404, detail=f"结果集 {result_no} 不存在或未生成")
    return result


@router.get("/results/{result_no}/analysis", response_model=dict)
def analysis_entry(result_no: str) -> dict[str, Any]:
    """下游分析入口：与页面读取同一份冻结结果集，条件与展示顺序保持一致。"""
    entry = service.analysis_entry(result_no)
    if entry is None:
        raise HTTPException(status_code=404, detail=f"结果集 {result_no} 不存在或未生成")
    return entry


@router.get("/{entry_id}", response_model=dict)
def get_entry(entry_id: int) -> dict:
    """读取单条筛选模板明细；不存在时给出可读的错误说明。"""
    entry = service.get_entry(entry_id)
    if entry is None:
        raise HTTPException(status_code=404, detail=f"筛选模板 {entry_id} 不存在或已归档")
    return entry


@router.post("", response_model=ActionResult)
def create_entry(payload: EntryPayload) -> ActionResult:
    """登记一条筛选模板，缺字段时说明原因而不是静默丢弃。"""
    entry, missing = service.create_entry(payload.values)
    if missing:
        return ActionResult(ok=False, message=f"缺少必填字段：{'、'.join(missing)}")
    return ActionResult(ok=True, message="筛选模板已登记", entry=entry)


@router.post("/{entry_id}/actions", response_model=ActionResult)
def run_action(entry_id: int, payload: EntryPayload) -> ActionResult:
    """对单条筛选模板执行启用模板、暂停模板、废弃模板；不允许的动作会被拦下并说明原因。"""
    action = str(payload.values.get("action") or "").strip()
    entry, message = service.run_action(entry_id, action)
    if entry is None:
        return ActionResult(ok=False, message=message)
    return ActionResult(ok=True, message=message, entry=entry)
