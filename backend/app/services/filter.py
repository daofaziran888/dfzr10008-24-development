"""数据筛选引擎业务规则：模板状态流转、批量执行、冻结口径与结果集追溯都收在这里。

执行口径的关键约束：
- 每次执行先按目标数据源冻结模板的筛选条件、排序/分页与结果格式，结果集只由冻结快照产生；
- 同一批参数（同一目标数据源 + 同一组模板定义）重复提交时复用原批次与原结果集，不重复写入；
- 单个模板条件冲突只标记该模板失败，其他模板继续执行；
- 批次中断或部分失败后，从失败模板继续，已成功的结果集原样保留。
"""
from __future__ import annotations

import hashlib
import json
from copy import deepcopy
from datetime import datetime
from typing import Any

from app.seed import SEED_DATA_SOURCES
from app.store import store

MODULE = "filter"
REQUIRED_FIELDS = ["模板编号", "模板名称", "目标数据源"]
STATUS_ORDER = ["草稿", "已启用", "已暂停", "已废弃"]
ACTION_RULES = {"启用模板": "草稿", "暂停模板": "已启用", "废弃模板": "已暂停"}
NEGATIVE_ACTIONS = []

BATCH_REGISTRY = "filter_batches"
RESULT_REGISTRY = "filter_results"

ITEM_SUCCESS = "成功"
ITEM_FAILED = "失败"
ITEM_PENDING = "未执行"

BATCH_COMPLETED = "已完成"
BATCH_PARTIAL = "部分失败"
BATCH_INTERRUPTED = "已中断"

# 目标数据源级别的执行口径要求：订单表必须带日期字段，否则无法冻结到稳定时间窗。
SOURCE_RULES: dict[str, dict[str, Any]] = {
    "订单表": {"required_fields": {"下单日期": "日期字段"}, "default_sort": {"字段": "下单日期", "方向": "降序"}},
}

SUPPORTED_OPERATORS = ("等于", "区间", "包含")


class FilterConflict(Exception):
    """单个模板的条件冲突：只影响当前模板，不阻断同批其他模板。"""

    def __init__(self, kind: str, message: str) -> None:
        super().__init__(message)
        self.kind = kind
        self.message = message


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _digest(payload: Any) -> str:
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _definition(entry: dict[str, Any], target_source: str) -> dict[str, Any]:
    """模板执行口径（不含状态等展示字段），用于判断是否同一批参数。"""
    return {
        "模板编号": entry.get("模板编号"),
        "目标数据源": target_source,
        "筛选条件明细": entry.get("筛选条件明细") or [],
        "排序规则": entry.get("排序规则") or {},
        "分页规则": entry.get("分页规则") or {},
        "结果字段": entry.get("结果字段") or [],
        "结果格式": entry.get("结果格式"),
    }


def _source(name: str) -> dict[str, Any]:
    source = SEED_DATA_SOURCES.get(name)
    if source is None:
        raise FilterConflict("数据源缺失", f"目标数据源「{name}」不存在，无法冻结筛选口径")
    return source


def _match(row: dict[str, Any], condition: dict[str, Any]) -> bool:
    field = str(condition.get("字段") or "")
    operator = str(condition.get("运算符") or "")
    value = condition.get("值")
    if operator not in SUPPORTED_OPERATORS:
        raise FilterConflict("条件冲突", f"筛选条件字段「{field}」使用了暂不支持的运算符「{operator}」")
    actual = row.get(field)
    if operator == "等于":
        return str(actual) == str(value)
    if operator == "包含":
        return str(value) in str(actual)
    # 区间：值形如 "2026-10-01 至 2026-10-31"
    parts = [part.strip() for part in str(value).replace("~", "至").split("至")]
    if len(parts) != 2 or not parts[0] or not parts[1]:
        raise FilterConflict("条件冲突", f"筛选条件字段「{field}」的区间写法无法解析：{value}")
    start, end = parts
    if isinstance(actual, (int, float)):
        try:
            return float(start) <= float(actual) <= float(end)
        except ValueError:
            raise FilterConflict("条件冲突", f"筛选条件字段「{field}」的区间值与数值型字段不匹配：{value}")
    return str(start) <= str(actual) <= str(end)


def _validate(entry: dict[str, Any], target_source: str) -> dict[str, Any]:
    """校验模板是否可以冻结执行；冲突时抛出 FilterConflict，由调用方单独标记失败。"""
    missing = [field for field in REQUIRED_FIELDS if not str(entry.get(field) or "").strip()]
    if missing:
        raise FilterConflict("配置缺失", f"模板缺少必填字段：{'、'.join(missing)}")

    declared = str(entry.get("目标数据源") or "").strip()
    if declared != target_source:
        raise FilterConflict(
            "条件冲突",
            f"模板目标数据源为「{declared}」，与本次执行目标数据源「{target_source}」不一致",
        )

    if entry.get("status") == STATUS_ORDER[-1]:
        raise FilterConflict("状态冲突", "模板已废弃，不再参与批量执行")

    source = _source(target_source)
    conditions = entry.get("筛选条件明细") or []
    if not conditions:
        raise FilterConflict("条件冲突", "筛选条件为空，无法冻结执行口径")

    for condition in conditions:
        field = str(condition.get("字段") or "")
        if field not in source["fields"]:
            raise FilterConflict("条件冲突", f"筛选条件字段「{field}」不在目标数据源「{target_source}」的字段清单里")

    required = SOURCE_RULES.get(target_source, {}).get("required_fields", {})
    condition_fields = {str(condition.get("字段") or "") for condition in conditions}
    for field, label in required.items():
        if field not in condition_fields:
            raise FilterConflict(
                "条件冲突",
                f"筛选条件缺少{label}「{field}」；目标数据源「{target_source}」必须按该字段限定范围才能冻结结果集",
            )

    fields = entry.get("结果字段") or []
    if not fields:
        raise FilterConflict("条件冲突", "结果字段为空，无法确定结果集的展示顺序")
    unknown = [field for field in fields if field not in source["fields"]]
    if unknown:
        raise FilterConflict(
            "条件冲突",
            f"结果字段「{'、'.join(unknown)}」不在目标数据源「{target_source}」的字段清单里",
        )
    return _definition(entry, target_source)


def _build_result(
    *,
    batch_no: str,
    result_no: str,
    entry: dict[str, Any],
    target_source: str,
    definition: dict[str, Any],
    executed_at: str,
) -> dict[str, Any]:
    """按冻结口径从数据源快照生成结果集；结果行按冻结的展示顺序输出。"""
    source = _source(target_source)
    snapshot_at = str(source.get("updated_at") or "")
    source_version = str(source.get("version") or "")
    conditions = deepcopy(definition["筛选条件明细"])
    sort_rule = definition["排序规则"] or SOURCE_RULES.get(target_source, {}).get("default_sort", {})
    page_rule = definition["分页规则"] or {}
    fields = list(definition["结果字段"])

    rows = [row for row in source["rows"] if all(_match(row, condition) for condition in conditions)]

    sort_field = str(sort_rule.get("字段") or "")
    sort_desc = str(sort_rule.get("方向") or "升序") == "降序"
    if sort_field:
        def sort_key(row: dict[str, Any]) -> tuple[int, Any]:
            value = row.get(sort_field)
            return (0, value) if isinstance(value, (int, float)) else (1, str(value))
        rows.sort(key=sort_key, reverse=sort_desc)

    total = len(rows)
    size = int(page_rule.get("每页") or total or 0)
    page = max(int(page_rule.get("页码") or 1), 1)
    if size > 0:
        start = (page - 1) * size
        rows = rows[start:start + size]

    frozen = {
        "目标数据源": target_source,
        "数据源版本": source_version,
        "数据源快照时间": snapshot_at,
        "筛选条件明细": conditions,
        "筛选条件": str(entry.get("筛选条件") or ""),
        "排序规则": sort_rule,
        "分页规则": page_rule,
        "结果字段": fields,
        "结果格式": definition.get("结果格式") or "",
    }
    condition_digest = _digest({"conditions": conditions, "sort": sort_rule, "page": page_rule})

    return {
        "结果集编号": result_no,
        "批次编号": batch_no,
        "模板编号": entry.get("模板编号"),
        "模板名称": entry.get("模板名称"),
        "目标数据源": target_source,
        "冻结时间": executed_at,
        "冻结条件": frozen,
        "展示顺序": fields,
        "行数": len(rows),
        "总行数": total,
        "行": [
            {field: row.get(field) for field in fields}
            for row in rows
        ],
        "条件摘要": condition_digest,
        "追溯信息": {
            "模板编号": entry.get("模板编号"),
            "批次编号": batch_no,
            "目标数据源": target_source,
            "数据源版本": source_version,
            "数据源快照时间": snapshot_at,
            "条件摘要": condition_digest,
            "冻结时间": executed_at,
        },
    }


class FilterService:
    def list_entries(
        self,
        *,
        keyword: str | None = None,
        status: str | None = None,
        page: int = 1,
        size: int = 20,
    ) -> tuple[list[dict[str, Any]], int]:
        rows = store.rows(MODULE)
        if keyword:
            rows = [row for row in rows if keyword in str(row.get("模板编号", ""))]
        if status:
            rows = [row for row in rows if row.get("status") == status]
        total = len(rows)
        start = max(page - 1, 0) * size
        return rows[start:start + size], total

    def get_entry(self, entry_id: int) -> dict[str, Any] | None:
        return store.find(MODULE, entry_id)

    def find_by_code(self, code: str) -> dict[str, Any] | None:
        for row in store.rows(MODULE):
            if str(row.get("模板编号")) == code:
                return row
        return None

    def list_data_sources(self) -> list[dict[str, Any]]:
        return [
            {
                "数据源": name,
                "版本": source.get("version"),
                "快照时间": source.get("updated_at"),
                "字段": source.get("fields"),
                "行数": len(source.get("rows") or []),
            }
            for name, source in SEED_DATA_SOURCES.items()
        ]

    def create_entry(self, values: dict[str, Any]) -> tuple[dict[str, Any] | None, list[str]]:
        missing = [field for field in REQUIRED_FIELDS if not str(values.get(field) or "").strip()]
        if missing:
            return None, missing
        rows = store.rows(MODULE)
        entry = {"id": max((int(row.get("id", 0)) for row in rows), default=0) + 1}
        entry.update({field: values.get(field) for field in REQUIRED_FIELDS})
        entry["status"] = STATUS_ORDER[0]
        entry["pending"] = True
        entry["abnormal"] = False
        rows.append(entry)
        return entry, []

    def run_action(self, entry_id: int, action: str) -> tuple[dict[str, Any] | None, str]:
        entry = store.find(MODULE, entry_id)
        if entry is None:
            return None, f"筛选模板 {entry_id} 不存在或已归档"
        if action not in ACTION_RULES:
            return None, f"动作「{action}」不属于数据筛选引擎可执行范围"
        target = ACTION_RULES[action]
        if target not in STATUS_ORDER:
            return None, f"目标状态「{target}」不在允许的状态序列里"
        entry["status"] = target
        entry["pending"] = target != STATUS_ORDER[-1]
        entry["abnormal"] = action in NEGATIVE_ACTIONS
        return entry, f"筛选模板已{action}"

    # ------------------------------------------------------------------ 批量执行
    def execute_batch(
        self,
        template_codes: list[str],
        *,
        target_source: str | None = None,
        stop_after: str | None = None,
    ) -> dict[str, Any]:
        """一次提交多条模板：整组返回逐条说明；失败项单独标记，其余模板继续。

        stop_after 用于模拟执行中断：处理到该模板后停下，剩余模板标记为未执行，
        之后可以从失败/未执行模板继续。
        """
        codes = [str(code).strip() for code in template_codes if str(code).strip()]
        # 多选提交时去掉重复项，保持用户选择的先后顺序。
        codes = list(dict.fromkeys(codes))
        if not codes:
            raise ValueError("至少选择一个筛选模板")

        entries: list[dict[str, Any]] = []
        missing_codes: list[str] = []
        for code in codes:
            entry = self.find_by_code(code)
            if entry is None:
                missing_codes.append(code)
            else:
                entries.append(entry)
        if not entries:
            raise ValueError(f"筛选模板不存在：{'、'.join(missing_codes)}")

        # 目标数据源统一按本次提交冻结；未显式指定时取各模板自身的目标数据源。
        sources = {str(entry.get("目标数据源")) for entry in entries}
        if target_source:
            resolve_source = target_source
        elif len(sources) == 1:
            resolve_source = next(iter(sources))
        else:
            raise ValueError(
                "所选模板的目标数据源不一致（" +
                "、".join(sorted(sources)) +
                "），请显式指定本次执行的目标数据源"
            )

        # 批次标识只由执行参数与模板定义决定：同参数重复提交必然命中同一批次，
        # 模板定义一旦调整就会生成新的批次，避免把旧结果当成新口径复用。
        definitions = [_definition(entry, resolve_source) for entry in entries]
        batch_key = _digest({
            "target_source": resolve_source,
            "templates": definitions,
            "codes": [entry.get("模板编号") for entry in entries],
        })

        existing = store.find_in_registry(BATCH_REGISTRY, "批次标识", batch_key)
        if existing is not None:
            state_note = (
                f"；该批次上次执行状态为「{existing['状态']}」，可从失败模板继续"
                if existing["状态"] != BATCH_COMPLETED
                else ""
            )
            return {
                "ok": existing["状态"] == BATCH_COMPLETED,
                "message": f"同一批参数已执行过，复用原结果集（{existing['批次编号']}）{state_note}",
                "batch": self._public_batch(existing, reused=True),
            }

        batch_no = f"BATCH-{len(store.registry(BATCH_REGISTRY)) + 1:04d}"
        batch: dict[str, Any] = {
            "批次编号": batch_no,
            "批次标识": batch_key,
            "目标数据源": resolve_source,
            "模板编号列表": codes,
            "提交时间": _now(),
            "执行时间": None,
            "状态": BATCH_INTERRUPTED if stop_after else BATCH_COMPLETED,
            "停止于模板": stop_after,
            "结果项": [],
            "汇总": {"成功": 0, "失败": 0, "未执行": 0},
            "缺少模板": missing_codes,
        }
        store.registry(BATCH_REGISTRY).append(batch)

        interrupted = False
        for index, code in enumerate(codes, start=1):
            if interrupted:
                batch["结果项"].append(self._pending_item(index, code, resolve_source))
                continue
            batch["结果项"].append(self._execute_item(index, code, resolve_source, batch))
            if stop_after and code == stop_after:
                interrupted = True
        batch["执行时间"] = _now()
        self._refresh_batch(batch)

        return {
            "ok": batch["状态"] != BATCH_INTERRUPTED,
            "message": (
                f"批量执行已中断于 {stop_after}，可从失败模板继续"
                if batch["状态"] == BATCH_INTERRUPTED
                else f"批量执行完成：成功 {batch['汇总']['成功']} 条，失败 {batch['汇总']['失败']} 条"
            ),
            "batch": self._public_batch(batch, reused=False),
        }

    def resume_batch(self, batch_no: str) -> dict[str, Any]:
        """从中断处继续：已成功的结果项原样保留，只重跑失败与未执行的模板。"""
        batch = store.find_in_registry(BATCH_REGISTRY, "批次编号", batch_no)
        if batch is None:
            raise ValueError(f"批次 {batch_no} 不存在")
        pending = [item for item in batch["结果项"] if item.get("状态") in (ITEM_FAILED, ITEM_PENDING)]
        if not pending:
            return {
                "ok": True,
                "message": f"批次 {batch_no} 没有待继续的模板，原结果集保持不变",
                "batch": self._public_batch(batch, reused=True),
            }

        first = pending[0]["模板编号"]
        for item in pending:
            refreshed = self._execute_item(item["顺序"], item["模板编号"], batch["目标数据源"], batch)
            item.update(refreshed)
        batch["状态"] = BATCH_COMPLETED
        batch["停止于模板"] = None
        batch["执行时间"] = _now()
        self._refresh_batch(batch)
        return {
            "ok": batch["状态"] == BATCH_COMPLETED,
            "message": (
                f"已从失败/未执行模板 {first} 继续执行，成功 {batch['汇总']['成功']} 条，失败 {batch['汇总']['失败']} 条"
            ),
            "batch": self._public_batch(batch, reused=False),
        }

    def retry_item(self, batch_no: str, template_code: str) -> dict[str, Any]:
        """单独重跑失败项；已成功的模板只返回原结果集，绝不重复写入。"""
        batch = store.find_in_registry(BATCH_REGISTRY, "批次编号", batch_no)
        if batch is None:
            raise ValueError(f"批次 {batch_no} 不存在")
        item = next((row for row in batch["结果项"] if row["模板编号"] == template_code), None)
        if item is None:
            raise ValueError(f"模板 {template_code} 不在批次 {batch_no} 里")
        if item.get("状态") == ITEM_SUCCESS:
            return {
                "ok": True,
                "message": f"模板 {template_code} 此前已成功，复用原结果集 {item.get('结果集编号')}，未重复写入",
                "item": item,
                "batch": self._public_batch(batch, reused=True),
            }
        refreshed = self._execute_item(item["顺序"], template_code, batch["目标数据源"], batch)
        item.update(refreshed)
        batch["执行时间"] = _now()
        self._refresh_batch(batch)
        return {
            "ok": item.get("状态") == ITEM_SUCCESS,
            "message": item.get("说明") or f"模板 {template_code} 已重跑",
            "item": item,
            "batch": self._public_batch(batch, reused=False),
        }

    # ------------------------------------------------------------------ 执行细节
    def _execute_item(
        self,
        order: int,
        code: str,
        target_source: str,
        batch: dict[str, Any],
    ) -> dict[str, Any]:
        entry = self.find_by_code(code)
        if entry is None:
            return self._failed_item(order, code, None, target_source, "配置缺失", f"筛选模板 {code} 不存在或已归档")

        try:
            definition = _validate(entry, target_source)
        except FilterConflict as conflict:
            self._mark_failed(entry, code, conflict.kind, conflict.message)
            return self._failed_item(order, code, entry, target_source, conflict.kind, conflict.message)

        result_no = self._result_no(batch["批次编号"], code)
        existing = store.find_in_registry(RESULT_REGISTRY, "结果集编号", result_no)
        if existing is not None:
            reused = True
        else:
            existing = _build_result(
                batch_no=batch["批次编号"],
                result_no=result_no,
                entry=entry,
                target_source=target_source,
                definition=definition,
                executed_at=_now(),
            )
            store.registry(RESULT_REGISTRY).append(existing)
            reused = False

        self._mark_success(entry, existing)
        return {
            "顺序": order,
            "模板编号": code,
            "模板名称": entry.get("模板名称"),
            "目标数据源": target_source,
            "状态": ITEM_SUCCESS,
            "ok": True,
            "失败类型": None,
            "说明": (
                f"模板 {code} 已按冻结口径生成结果集 {existing['结果集编号']}，共 {existing['行数']} 行"
                if not reused
                else f"模板 {code} 已成功，复用原结果集 {existing['结果集编号']}，未重复写入"
            ),
            "结果集编号": existing["结果集编号"],
            "冻结条件": existing["冻结条件"],
            "展示顺序": existing["展示顺序"],
            "行数": existing["行数"],
            "总行数": existing["总行数"],
            "追溯信息": existing["追溯信息"],
            "复用原结果集": reused,
        }

    def _failed_item(
        self,
        order: int,
        code: str,
        entry: dict[str, Any] | None,
        target_source: str,
        kind: str,
        message: str,
    ) -> dict[str, Any]:
        return {
            "顺序": order,
            "模板编号": code,
            "模板名称": (entry or {}).get("模板名称"),
            "目标数据源": target_source,
            "状态": ITEM_FAILED,
            "ok": False,
            "失败类型": kind,
            "说明": message,
            "结果集编号": None,
            "冻结条件": None,
            "展示顺序": None,
            "行数": 0,
            "总行数": 0,
            "追溯信息": {
                "模板编号": code,
                "批次编号": None,
                "目标数据源": target_source,
                "失败类型": kind,
                "说明": message,
            },
            "复用原结果集": False,
        }

    def _pending_item(self, order: int, code: str, target_source: str) -> dict[str, Any]:
        entry = self.find_by_code(code)
        return {
            "顺序": order,
            "模板编号": code,
            "模板名称": (entry or {}).get("模板名称"),
            "目标数据源": target_source,
            "状态": ITEM_PENDING,
            "ok": False,
            "失败类型": None,
            "说明": "批次执行中断，该模板尚未执行，可从中断处继续",
            "结果集编号": None,
            "冻结条件": None,
            "展示顺序": None,
            "行数": 0,
            "总行数": 0,
            "追溯信息": {"模板编号": code, "目标数据源": target_source},
            "复用原结果集": False,
        }

    def _mark_success(self, entry: dict[str, Any], result: dict[str, Any]) -> None:
        entry["status"] = "已启用"
        entry["模板状态"] = "已启用"
        entry["pending"] = False
        entry["abnormal"] = False
        entry["最近执行结果"] = ITEM_SUCCESS
        entry["最近结果集"] = result["结果集编号"]
        entry["最近结果行数"] = result["行数"]
        entry["最近失败说明"] = None

    def _mark_failed(self, entry: dict[str, Any] | None, code: str, kind: str, message: str) -> None:
        if entry is None:
            return
        entry["status"] = "已暂停"
        entry["模板状态"] = "已暂停"
        entry["pending"] = False
        entry["abnormal"] = True
        entry["最近执行结果"] = ITEM_FAILED
        entry["最近结果集"] = None
        entry["最近失败说明"] = message

    def _result_no(self, batch_no: str, code: str) -> str:
        """结果集编号按批次与模板唯一，保证重跑不会产生第二条已成功结果。"""
        return f"{batch_no}-{code}"

    def _refresh_batch(self, batch: dict[str, Any]) -> None:
        items = batch["结果项"]
        batch["汇总"] = {
            "成功": sum(1 for item in items if item.get("状态") == ITEM_SUCCESS),
            "失败": sum(1 for item in items if item.get("状态") == ITEM_FAILED),
            "未执行": sum(1 for item in items if item.get("状态") == ITEM_PENDING),
        }
        if batch["汇总"]["未执行"]:
            batch["状态"] = BATCH_INTERRUPTED
        elif batch["汇总"]["失败"]:
            batch["状态"] = BATCH_PARTIAL
        else:
            batch["状态"] = BATCH_COMPLETED

    def _public_batch(self, batch: dict[str, Any], *, reused: bool) -> dict[str, Any]:
        payload = deepcopy(batch)
        payload["失败模板"] = [
            {"模板编号": item["模板编号"], "说明": item["说明"]}
            for item in batch["结果项"]
            if item.get("状态") in (ITEM_FAILED, ITEM_PENDING)
        ]
        payload["复用原结果集"] = reused
        payload["本次执行"] = "复用" if reused else "新执行"
        return payload

    # ------------------------------------------------------------------ 查询与下游
    def list_batches(self, *, page: int = 1, size: int = 20) -> tuple[list[dict[str, Any]], int]:
        rows = store.registry(BATCH_REGISTRY)
        total = len(rows)
        start = max(page - 1, 0) * size
        return [self._public_batch(row, reused=False) for row in rows[start:start + size]], total

    def get_batch(self, batch_no: str) -> dict[str, Any] | None:
        batch = store.find_in_registry(BATCH_REGISTRY, "批次编号", batch_no)
        return None if batch is None else self._public_batch(batch, reused=False)

    def get_result(self, result_no: str) -> dict[str, Any] | None:
        return store.find_in_registry(RESULT_REGISTRY, "结果集编号", result_no)

    def list_results(
        self,
        *,
        template_code: str | None = None,
        batch_no: str | None = None,
    ) -> list[dict[str, Any]]:
        """列出已写入的结果集，可按模板或批次过滤；用于核对重跑是否产生重复写入。"""
        rows = store.registry(RESULT_REGISTRY)
        if template_code:
            rows = [row for row in rows if row.get("模板编号") == template_code]
        if batch_no:
            rows = [row for row in rows if row.get("批次编号") == batch_no]
        return rows

    def analysis_entry(self, result_no: str) -> dict[str, Any] | None:
        """下游分析入口：与页面读取同一份冻结结果集，条件与展示顺序原样返回。"""
        result = self.get_result(result_no)
        if result is None:
            return None
        frozen = result["冻结条件"]
        return {
            "ok": True,
            "结果集编号": result["结果集编号"],
            "来源批次": result["批次编号"],
            "模板编号": result["模板编号"],
            "模板名称": result["模板名称"],
            "目标数据源": result["目标数据源"],
            "筛选条件": frozen.get("筛选条件"),
            "筛选条件明细": frozen.get("筛选条件明细"),
            "排序规则": frozen.get("排序规则"),
            "分页规则": frozen.get("分页规则"),
            "结果格式": frozen.get("结果格式"),
            "展示顺序": list(result["展示顺序"]),
            "行数": result["行数"],
            "总行数": result["总行数"],
            "行": [dict(row) for row in result["行"]],
            "口径来源": "页面同一冻结结果集",
            "追溯信息": result["追溯信息"],
        }
