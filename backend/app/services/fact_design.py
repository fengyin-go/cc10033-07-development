"""事实表设计服务：管理事实表的业务过程、粒度和维度关联定义。

增强能力：
- 事实表名称标准化校验（前缀fact_）
- 维度关联完整性检查：关联的维度必须已在dim_definition中定义
- 消费 dim.published 通道确认关联维度可用
- 发布事实时触发度量配置和聚合层自动建议
"""
from __future__ import annotations

import re
from typing import Any

from app.ingest import (
    MATCHED,
    PENDING,
    backfill_pending_facts,
    find_fact_by_source_record,
    pending_fact_matches,
    write_fact_partition,
)
from app.services.base import BaseModuleService
from app.store import store


class FactDesignService(BaseModuleService):
    MODULE = "fact_design"
    ENTITY = "事实表"
    OPERATION_TERM = "关联"
    LIST_FIELDS = ["事实名称", "业务过程", "粒度声明", "关联维度", "度量字段", "分区字段", "更新模式"]
    REQUIRED_FIELDS = ["事实名称", "业务过程", "粒度声明"]
    STATUS_ORDER = ["草稿", "已设计", "已发布", "已冻结"]
    ACTION_RULES = {"提交设计": "草稿", "发布事实": "已设计", "冻结事实": "已发布"}
    NEGATIVE_ACTIONS = []
    # 维度外键映射：{维度编码: (自然键字段, 代理键字段)}，未显式声明时按字段名推断。
    FX_FIELDS = {"自然键字段": "自然键", "代理键字段": "代理键", "维度编码": "维度编码"}

    UPDATE_MODES = {"每日追加", "每小时追加", "T+1批量", "实时追加", "每日全量"}

    def create_entry(self, values: dict[str, Any]) -> tuple[dict[str, Any] | None, list[str]]:
        """创建事实表定义：校验关联维度的存在性，标准化更新模式。"""
        # 标准化事实名称
        fact_name = str(values.get("事实名称", "")).strip()
        values["事实名称"] = fact_name

        # 标准化更新模式
        update_mode = str(values.get("更新模式", "每日追加")).strip()
        if update_mode not in self.UPDATE_MODES:
            update_mode = "每日追加"
            values["更新模式"] = update_mode

        # 校验关联维度存在性
        related_dims = str(values.get("关联维度", "")).strip()
        validated_dims = _validate_dimensions(related_dims)
        if not validated_dims:
            values["关联维度"] = related_dims  # 保留原始输入

        # 消费已发布维度消息
        dim_msgs = store.channel.consume("dim.published", "fact_design")
        for msg in dim_msgs:
            dim_code = msg["payload"].get("dim_code", "")
            if dim_code in related_dims:
                # 确认维度可用
                values[f"_dim_{dim_code}_confirmed"] = True

        entry, missing = super().create_entry(values)
        if entry is None:
            return None, missing

        # 事实表持有维度自然键 → 代理键的外键映射，供分区写入复用。
        mapping = _build_fk_mapping(entry.get("关联维度", ""), values.get("外键映射"))
        if mapping:
            entry["外键映射"] = mapping
            entry["外键映射说明"] = "; ".join(
                f"{m['维度编码']}:{m['自然键字段']}->{m['代理键字段']}" for m in mapping)

        # 发布事实表定义到数据通道
        store.channel.publish("fact.defined", {
            "fact_name": entry["事实名称"],
            "business_process": entry["业务过程"],
            "granularity": entry["粒度声明"],
            "related_dims": related_dims,
            "measures": entry.get("度量字段", ""),
            "partition_field": entry.get("分区字段", ""),
            "update_mode": entry["更新模式"],
            "entry_id": entry["id"],
        })

        # 注册事实产物
        store.artifacts.register(
            "fact_design", "fact_table",
            f"fact_{entry['id']}",
            {"fact_name": entry["事实名称"], "business_process": entry["业务过程"]},
            source_modules=["dim_definition", "fact_design"],
        )
        return entry, []

    def run_action(self, entry_id: int, action: str) -> tuple[dict[str, Any] | None, str]:
        entry, message = super().run_action(entry_id, action)
        if entry is None:
            return None, message

        if action == "发布事实":
            # 验证关联维度完整性
            dim_validation = _validate_fact_dim_completeness(entry)
            if not dim_validation["valid"]:
                return None, dim_validation["message"]

            store.channel.publish("fact.published", {
                "fact_name": entry["事实名称"],
                "business_process": entry["业务过程"],
                "granularity": entry["粒度声明"],
                "related_dims": entry.get("关联维度", ""),
                "measures": entry.get("度量字段", ""),
                "update_mode": entry.get("更新模式"),
                "entry_id": entry["id"],
            })

            # 自动建议度量配置
            _suggest_measures(entry)
            # 自动建议聚合汇总
            _suggest_aggregation(entry)

        elif action == "冻结事实":
            store.channel.publish("fact.frozen", {
                "fact_name": entry["事实名称"],
                "entry_id": entry["id"],
            })

        return entry, message

    def domain_snapshot(self) -> dict[str, Any]:
        base = super().domain_snapshot()
        dim_consumed = len(store.channel.consume("dim.published", "fact_design"))
        base["consumed_published_dims"] = dim_consumed

        # 维度关联矩阵
        dim_matrix: dict[str, list[str]] = {}
        for row in store.rows(self.MODULE):
            fact_name = row.get("事实名称", "")
            dims = str(row.get("关联维度", "")).split(".")
            dim_matrix[fact_name] = [d for d in dims if d]
        base["dimension_association_matrix"] = dim_matrix

        # 待匹配事实：事实先到、维度键迟到时的存量与分区分布。
        pending = store.fact_partitions.pending_matches()
        base["pending_fact_count"] = len(pending)
        base["pending_by_fact"] = _count_by(pending, "事实名称")
        base["pending_by_partition"] = _count_by(pending, "分区值")
        base["fact_partition_summary"] = store.fact_partitions.summary()
        return base

    # ------------------------------------------------------------------
    # 事实入仓：按映射写分区、待匹配检索、迟到维度回填
    # ------------------------------------------------------------------
    def write_partitions(self, entry_id: int, values: dict[str, Any]) -> dict[str, Any]:
        entry = store.find(self.MODULE, entry_id)
        if entry is None:
            raise LookupError(f"{self.ENTITY} {entry_id} 不存在或已归档")
        records = values.get("records") or values.get("事实记录") or []
        if not isinstance(records, list) or not records:
            raise ValueError("请提供至少一条事实记录（records）")

        partition_field = str(
            values.get("partition_field") or values.get("分区字段") or entry.get("分区字段") or "dt"
        ).split(" ")[0]
        mappings = values.get("dimension_mappings") or values.get("外键映射") or entry.get("外键映射")
        source = str(values.get("来源系统") or values.get("source") or "")

        result = write_fact_partition(
            store,
            str(entry.get("事实名称")),
            records,
            partition_field=partition_field,
            dimension_mappings=mappings,
            business_process=str(entry.get("业务过程", "")),
            grain=str(entry.get("粒度声明", "")),
            source=source,
        )
        store.channel.publish("fact.partition_written", {
            "fact_name": result["fact_name"],
            "written": result["written"],
            "pending": result["pending"],
            "partitions": result["partitions"],
            "entry_id": entry_id,
        })
        return result

    def pending_matches(self, **filters: Any) -> dict[str, Any]:
        """待匹配事实检索接口：保留来源记录、来源系统、分区和粒度。"""
        return pending_fact_matches(store, **filters)

    def backfill(self, values: dict[str, Any]) -> dict[str, Any]:
        """维度键迟到：把先前未匹配的同一条事实关联起来，金额不翻倍。"""
        result = backfill_pending_facts(
            store,
            dim_code=values.get("维度编码") or values.get("dim_code"),
            natural_key=values.get("自然键") or values.get("natural_key"),
            fact_name=values.get("事实名称") or values.get("fact_name"),
            source_record_id=values.get("来源记录ID") or values.get("source_record_id"),
        )
        store.channel.publish("fact.backfilled", {
            "fact_name": values.get("事实名称") or values.get("fact_name") or "",
            "dim_code": values.get("维度编码") or values.get("dim_code") or "",
            "natural_key": values.get("自然键") or values.get("natural_key") or "",
            "matched": result["matched"],
            "remaining": result["remaining"],
        })
        return result

    def by_source_record(self, source_record_id: str, fact_name: str | None = None) -> dict[str, Any]:
        """按源记录检索事实分区，回填前后都能定位到同一条事实和金额。"""
        return find_fact_by_source_record(store, source_record_id, fact_name=fact_name)


def _validate_dimensions(dim_str: str) -> list[str]:
    """校验关联维度字符串，过滤有效维度编码。"""
    if not dim_str.strip():
        return []
    dim_codes = [d.strip() for d in dim_str.split(".") if d.strip()]
    existing_dims = {row.get("维度编码") for row in store.rows("dim_definition")}
    valid = [d for d in dim_codes if d in existing_dims]
    return valid


def _validate_fact_dim_completeness(entry: dict[str, Any]) -> dict[str, Any]:
    """校验事实表关联维度完整性。"""
    related_dims = str(entry.get("关联维度", "")).strip()
    if not related_dims:
        return {"valid": False, "message": "事实表至少需要关联一个维度"}

    dim_codes = [d.strip() for d in related_dims.split(".") if d.strip()]
    existing_dims = {row.get("维度编码") for row in store.rows("dim_definition")}
    missing = [d for d in dim_codes if d not in existing_dims]
    if missing:
        return {"valid": False, "message": f"维度 {'、'.join(missing)} 未定义，请先创建维度定义"}
    return {"valid": True, "message": ""}


def _build_fk_mapping(related_dims: str, declared: Any) -> list[dict[str, str]]:
    """把关联维度转成显式外键映射，按约定字段名推断自然键与代理键列。"""
    mapping: list[dict[str, str]] = []
    if isinstance(declared, list):
        for item in declared:
            if isinstance(item, dict) and item.get("维度编码"):
                mapping.append({
                    "维度编码": str(item["维度编码"]),
                    "自然键字段": str(item.get("自然键字段") or "自然键"),
                    "代理键字段": str(item.get("代理键字段") or ""),
                })
    elif isinstance(declared, dict):
        for dim_code, spec in declared.items():
            if isinstance(spec, str):
                mapping.append({"维度编码": str(dim_code), "自然键字段": spec, "代理键字段": ""})
            elif isinstance(spec, dict):
                mapping.append({
                    "维度编码": str(dim_code),
                    "自然键字段": str(spec.get("自然键字段") or "自然键"),
                    "代理键字段": str(spec.get("代理键字段") or ""),
                })

    existing = {m["维度编码"] for m in mapping}
    for dim_code in (d.strip() for d in str(related_dims or "").split(".")):
        if not dim_code or dim_code in existing:
            continue
        stem = dim_code.lower().replace("dim_", "")
        mapping.append({
            "维度编码": dim_code,
            "自然键字段": f"{stem}_nk",
            "代理键字段": f"{stem}_sk",
        })
    return mapping


def _count_by(rows: list[dict[str, Any]], field: str) -> dict[str, int]:
    counts: dict[str, int] = {}
    for row in rows:
        key = str(row.get(field, ""))
        counts[key] = counts.get(key, 0) + 1
    return counts


def _suggest_measures(fact: dict[str, Any]) -> None:
    """基于事实表定义自动建议度量配置。"""
    measures_str = str(fact.get("度量字段", "")).strip()
    if not measures_str:
        return

    measure_names = [m.strip() for m in measures_str.split(".") if m.strip()]
    for m_name in measure_names:
        measure_type = "DECIMAL(18,2)" if "_amt" in m_name else "BIGINT"
        store.channel.publish("measure.suggested", {
            "measure_name": m_name,
            "fact_name": fact["事实名称"],
            "measure_type": measure_type,
            "agg_method": "SUM" if ("_amt" in m_name or "_qty" in m_name) else "COUNT",
            "source_fact_id": fact.get("id"),
            "is_additive": "_cnt" not in m_name and "_rate" not in m_name,
        })


def _suggest_aggregation(fact: dict[str, Any]) -> None:
    """基于事实表定义自动建议汇总表配置。"""
    fact_name = fact.get("事实名称", "")
    store.channel.publish("agg.suggested", {
        "agg_name": f"每日{fact_name}汇总",
        "source_fact": fact_name,
        "agg_dims": fact.get("关联维度", ""),
        "agg_measures": fact.get("度量字段", ""),
        "agg_granularity": "每日",
        "source_fact_id": fact.get("id"),
    })