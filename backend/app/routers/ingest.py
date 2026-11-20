"""事实入仓接口：自然键映射、分区写入、待匹配检索、迟到维度回填。

这些接口把「维度自然键 → 正式代理键」的映射和事实分区的待匹配状态暴露给工作台，
避免事实先到、维度键迟到时金额被重复计入。
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field

from app.ingest import (
    MATCHED,
    PENDING,
    backfill_pending_facts,
    pending_fact_matches,
    register_natural_key,
    write_fact_partition,
)
from app.services import service_for
from app.store import store

router = APIRouter(prefix="/api", tags=["事实入仓"])


class FactRecordsPayload(BaseModel):
    records: list[dict[str, Any]] = Field(default_factory=list)
    partition_field: str | None = None
    dimension_mappings: list[dict[str, Any]] | dict[str, Any] | None = None
    source: str | None = None


class NaturalKeyPayload(BaseModel):
    dim_code: str
    natural_key: str
    source: str | None = None
    key_name: str | None = None
    surrogate_key: int | None = None
    auto_backfill: bool = True


class BackfillPayload(BaseModel):
    dim_code: str | None = None
    natural_key: str | None = None
    fact_name: str | None = None
    source_record_id: str | None = None


def _fact_entry(fact_entry_id: int) -> dict[str, Any]:
    entry = store.find("fact_design", fact_entry_id)
    if entry is None:
        raise HTTPException(status_code=404, detail=f"事实表 {fact_entry_id} 不存在或已归档")
    return entry


# ---------------------------------------------------------------------------
# 维度自然键映射
# ---------------------------------------------------------------------------


@router.get("/surrogate_key/natural-keys")
def list_natural_keys(
    dim_code: str | None = Query(default=None),
    natural_key: str | None = Query(default=None),
    surrogate_key: int | None = Query(default=None),
    keyword: str | None = Query(default=None),
) -> dict[str, Any]:
    """列出自然键与正式代理键的对应关系，并给出各自然键的待匹配事实数。"""
    service = service_for("surrogate_key")
    items = service.list_natural_keys(
        dim_code=dim_code, natural_key=natural_key,
        surrogate_key=surrogate_key, keyword=keyword,
    )
    return {"items": items, "total": len(items)}


@router.post("/surrogate_key/natural-keys")
def bind_natural_key(payload: NaturalKeyPayload) -> dict[str, Any]:
    """登记自然键并确定其正式代理键；同一自然键重复登记返回原代理键。"""
    try:
        result = register_natural_key(
            store,
            payload.dim_code,
            payload.natural_key,
            source=payload.source or "",
            key_name=payload.key_name or "",
            surrogate_key=payload.surrogate_key,
            auto_backfill=payload.auto_backfill,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return result


# ---------------------------------------------------------------------------
# 事实分区写入与待匹配检索
# ---------------------------------------------------------------------------


@router.post("/fact_design/{entry_id}/partitions")
def write_partition(entry_id: int, payload: FactRecordsPayload) -> dict[str, Any]:
    """按事实设计的外键映射写入事实分区；未匹配记录保留为待匹配。"""
    entry = _fact_entry(entry_id)
    service = service_for("fact_design")
    try:
        return service.write_partitions(entry_id, {
            "records": payload.records,
            "partition_field": payload.partition_field or entry.get("分区字段"),
            "dimension_mappings": payload.dimension_mappings,
            "来源系统": payload.source,
        })
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/fact_design/pending-matches")
def list_pending_matches(
    fact_name: str | None = Query(default=None),
    source_record_id: str | None = Query(default=None),
    partition_value: str | None = Query(default=None),
    dim_code: str | None = Query(default=None),
    natural_key: str | None = Query(default=None),
    keyword: str | None = Query(default=None),
    include_matched: bool = Query(default=False),
    limit: int | None = Query(default=None, ge=1, le=1000),
) -> dict[str, Any]:
    """待匹配事实检索：保留来源记录、来源系统、分区和粒度。"""
    return pending_fact_matches(
        store,
        fact_name=fact_name,
        source_record_id=source_record_id,
        partition_value=partition_value,
        dim_code=dim_code,
        natural_key=natural_key,
        keyword=keyword,
        include_matched=include_matched,
        limit=limit,
    )


@router.post("/fact_design/backfill")
def backfill(payload: BackfillPayload) -> dict[str, Any]:
    """维度键迟到后回填：关联先前未匹配的同一条事实，金额不重复计入。"""
    return backfill_pending_facts(
        store,
        dim_code=payload.dim_code,
        natural_key=payload.natural_key,
        fact_name=payload.fact_name,
        source_record_id=payload.source_record_id,
    )


@router.get("/fact_design/by-source/{source_record_id}")
def fact_by_source(
    source_record_id: str,
    fact_name: str | None = Query(default=None),
) -> dict[str, Any]:
    """按源记录检索事实分区，回填前后都能找到同一条事实和金额。"""
    result = {
        "items": store.fact_partitions.find(
            fact_name=fact_name, source_record_id=source_record_id),
        "source_record_id": source_record_id,
    }
    result["total"] = len(result["items"])
    result["amount"] = round(
        sum(float(item.get("金额") or 0.0) for item in result["items"]), 6)
    return result


@router.get("/fact_design/partition-summary")
def partition_summary(fact_name: str | None = Query(default=None)) -> dict[str, Any]:
    """事实分区概况：待匹配/已关联数量、金额合计和分区分布。"""
    summary = store.fact_partitions.summary(fact_name=fact_name)
    summary["pending_status"] = PENDING
    summary["matched_status"] = MATCHED
    return summary
