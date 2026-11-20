"""事实入仓所需的自然键映射、分区写入和待匹配回填。

本模块只承载纯领域逻辑，不导入 ``app.store``，避免包内循环引用。
上层（服务、路由、测试）传入 store，并使用 ``store.natural_keys`` 与
``store.fact_partitions`` 两个内存登记表。

核心约束：
- 同一 (维度编码, 自然键) 重复登记返回同一个正式代理键；
- 不同自然键绝不共用同一个代理键；
- 同一业务记录（事实名 + 来源记录ID）重复导入只保留一行，金额不累加；
- 事实先到、维度键迟到时先落为「待匹配」，补键后回填关联，金额不重复计入。
"""
from __future__ import annotations

import copy
import hashlib
from datetime import datetime, timezone
from typing import Any

PENDING = "待匹配"
MATCHED = "已关联"
NOT_BACKFILLED = "未回填"
BACKFILLED = "已回填"

# 记录级元数据字段，写事实分区时不再作为度量值复制。
_RECORD_META_FIELDS = {
    "来源记录ID", "来源记录", "来源系统", "来源", "source", "source_id",
    "source_record_id", "source_system", "分区值", "分区", "partition",
    "partition_value", "粒度", "粒度声明", "维度编码", "自然键", "代理键",
    "事实名称", "业务过程", "分区字段", "dt", "date",
    "销售金额", "金额", "amount", "自然键字段", "代理键字段",
}

_AMOUNT_FIELDS = (
    "金额", "sales_amt", "amount", "order_amt", "refund_amt",
    "gmv_amt", "amt", "销售金额",
)

# 未显式给出映射时，按记录里出现的常见自然键字段推断维度。
_DEFAULT_MAPPINGS: tuple[dict[str, str], ...] = (
    {"维度编码": "DIM_PRODUCT", "自然键字段": "product_nk", "代理键字段": "product_sk"},
    {"维度编码": "DIM_TIME", "自然键字段": "date_key", "代理键字段": "date_sk"},
    {"维度编码": "DIM_STORE", "自然键字段": "store_nk", "代理键字段": "store_sk"},
    {"维度编码": "DIM_CUSTOMER", "自然键字段": "customer_nk", "代理键字段": "customer_sk"},
    {"维度编码": "DIM_SUPPLIER", "自然键字段": "supplier_nk", "代理键字段": "supplier_sk"},
    {"维度编码": "DIM_CHANNEL", "自然键字段": "channel_nk", "代理键字段": "channel_sk"},
)


# ---------------------------------------------------------------------------
# 小工具
# ---------------------------------------------------------------------------


def now_text() -> str:
    return datetime.now(timezone.utc).astimezone().strftime("%Y-%m-%d %H:%M:%S")


def as_int(value: Any) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return int(float(str(value).strip().replace(",", "")))
    except (TypeError, ValueError):
        return None


def as_float(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return float(str(value).strip().replace(",", ""))
    except (TypeError, ValueError):
        return None


def _first_value(source: dict[str, Any], names: tuple[str, ...]) -> Any:
    for name in names:
        if name in source and source[name] not in (None, ""):
            return source[name]
    return None


def _pick_export_key(record: dict[str, Any]) -> str | None:
    for field in _AMOUNT_FIELDS:
        if field in record and as_float(record.get(field)) is not None:
            return field
    return None


def _record_amount(record: dict[str, Any]) -> float:
    field = _pick_export_key(record)
    if field is None:
        return 0.0
    value = as_float(record.get(field))
    return value if value is not None else 0.0


def _measure_values(record: dict[str, Any]) -> dict[str, float]:
    """从记录中提取可累加的数值字段，保持一条业务记录只贡献一次。"""
    measures: dict[str, float] = {}
    for key, value in record.items():
        if key in _RECORD_META_FIELDS:
            continue
        if isinstance(value, bool):
            continue
        numeric = as_float(value)
        if numeric is not None:
            measures[str(key)] = numeric
    if "金额" not in measures:
        amount = _record_amount(record)
        if amount or _pick_export_key(record):
            measures["金额"] = amount
    return measures


def _copy_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [copy.deepcopy(row) for row in rows]


# ---------------------------------------------------------------------------
# 维度自然键 → 代理键登记
# ---------------------------------------------------------------------------


class NaturalKeyRegistry:
    """维度自然键登记表：为自然键确定稳定且唯一的正式代理键。"""

    def __init__(self) -> None:
        self._records: dict[tuple[str, str], dict[str, Any]] = {}
        self._key_owner: dict[int, tuple[str, str]] = {}
        self._counters: dict[str, int] = {}
        self._next_id = 1

    # -- 查询 ---------------------------------------------------------------
    def get(self, dim_code: str, natural_key: str) -> dict[str, Any] | None:
        record = self._records.get((str(dim_code).strip(), str(natural_key).strip()))
        return copy.deepcopy(record) if record else None

    def resolve(self, dim_code: str, natural_key: str) -> int | None:
        record = self._records.get((str(dim_code).strip(), str(natural_key).strip()))
        if record is None:
            return None
        return int(record["代理键"])

    def owner_of(self, surrogate_key: int) -> tuple[str, str] | None:
        return self._key_owner.get(int(surrogate_key))

    def list_records(
        self,
        *,
        dim_code: str | None = None,
        natural_key: str | None = None,
        surrogate_key: int | None = None,
        keyword: str | None = None,
        status: str | None = None,
    ) -> list[dict[str, Any]]:
        rows = list(self._records.values())
        if dim_code:
            rows = [r for r in rows if r.get("目标维度") == dim_code]
        if natural_key:
            rows = [r for r in rows if str(r.get("自然键", "")) == natural_key]
        if surrogate_key is not None:
            rows = [r for r in rows if int(r.get("代理键", -1)) == int(surrogate_key)]
        if status:
            rows = [r for r in rows if r.get("status") == status]
        if keyword:
            needle = keyword.casefold()
            rows = [
                r for r in rows
                if needle in str(r.get("自然键", "")).casefold()
                or needle in str(r.get("目标维度", "")).casefold()
                or needle in str(r.get("键值名称", "")).casefold()
            ]
        return _copy_rows(sorted(rows, key=lambda r: int(r.get("id", 0))))

    def count(self) -> int:
        return len(self._records)

    # -- 登记 ---------------------------------------------------------------
    def register(
        self,
        store: Any,
        dim_code: str,
        natural_key: str,
        *,
        source: str = "",
        key_name: str = "",
        surrogate_key: int | None = None,
    ) -> dict[str, Any]:
        """登记自然键并返回代理键；重复登记不改变已有正式代理键。"""
        dim_code = str(dim_code or "").strip()
        natural_key = str(natural_key or "").strip()
        if not dim_code:
            raise ValueError("缺少目标维度，无法建立自然键映射")
        if not natural_key:
            raise ValueError("缺少自然键，无法分配代理键")

        existing = self._records.get((dim_code, natural_key))
        if existing is not None:
            # 重复导入同一业务记录：保持原代理键稳定，不再消耗键段。
            return {**copy.deepcopy(existing), "created": False}

        config = self._pick_config(store, dim_code)
        strategy = str((config or {}).get("生成策略") or "自增序列")
        if surrogate_key is not None:
            key = int(surrogate_key)
            owner = self._key_owner.get(key)
            if owner is not None:
                raise ValueError(
                    f"代理键 {key} 已被 {owner[0]} 的自然键 {owner[1]} 占用，不能重复分配"
                )
            self._reserve_counter(dim_code, key, config)
        else:
            key = self._allocate_key(store, dim_code, natural_key, config, strategy)

        self._key_owner[key] = (dim_code, natural_key)
        record_key = str((config or {}).get("键值名称") or key_name or "").strip()
        record = {
            "id": self._next_id,
            "status": "已分配",
            "pending": False,
            "abnormal": False,
            "目标维度": dim_code,
            "维度编码": dim_code,
            "键值名称": record_key,
            "自然键": natural_key,
            "代理键": key,
            "编号": key,
            "来源系统": str(source or ""),
            "来源": str(source or ""),
            "生成策略": strategy,
            "登记时间": now_text(),
        }
        self._next_id += 1
        self._records[(dim_code, natural_key)] = record
        return {**copy.deepcopy(record), "created": True}

    # -- 分配 ---------------------------------------------------------------
    def _pick_config(self, store: Any, dim_code: str) -> dict[str, Any] | None:
        rows = [
            r for r in store.rows("surrogate_key")
            if str(r.get("目标维度", "")) == dim_code and r.get("status") != "已耗尽"
        ]
        if not rows:
            return None
        priority = {"已分配": 0, "草稿": 1, "已重置": 2}
        rows.sort(key=lambda r: priority.get(str(r.get("status", "")), 3))
        return rows[0]

    def _allocate_key(
        self,
        store: Any,
        dim_code: str,
        natural_key: str,
        config: dict[str, Any] | None,
        strategy: str,
    ) -> int:
        if config is None:
            # 未配置键值定义时，按维度散列到独立键段，避免不同维度互相占用编号。
            digest = hashlib.sha256(dim_code.encode("utf-8")).hexdigest()
            base = 1000000 + (int(digest[:8], 16) % 800) * 1000
        else:
            base = as_int(config.get("初始值")) or 1000000
        step = as_int((config or {}).get("步长")) or 1
        step = max(step, 1)
        range_end = as_int((config or {}).get("键段结束"))

        if strategy in ("UUID_HASH", "HASH_MOD"):
            digest = hashlib.sha256(f"{dim_code}\u0000{natural_key}".encode("utf-8")).hexdigest()
            key = base + (int(digest[:12], 16) % 900000) * step
        else:
            key = self._counters.get(dim_code)
            if key is None:
                key = as_int((config or {}).get("下一个键"))
            if key is None:
                key = base
            key = max(key, base)

        # 不同自然键绝不能落到同一个正式代理键上。
        while key in self._key_owner:
            key += step
        if range_end is not None and key > range_end:
            raise ValueError(
                f"维度 {dim_code} 的键段已耗尽（上限 {range_end}），请先分配新的键段"
            )

        self._counters[dim_code] = key + step
        self._reserve_counter(dim_code, key, config)
        return key

    def _reserve_counter(self, dim_code: str, key: int, config: dict[str, Any] | None) -> None:
        step = as_int((config or {}).get("步长")) or 1
        step = max(step, 1)
        current = self._counters.get(dim_code)
        if current is None or current <= key:
            self._counters[dim_code] = key + step
        if config is None:
            return
        config["已分配数"] = int(as_int(config.get("已分配数")) or 0) + 1
        config["下一个键"] = self._counters[dim_code]
        if config.get("status") == "草稿":
            config["status"] = "已分配"
            config["pending"] = False
        range_end = as_int(config.get("键段结束"))
        if range_end is not None and self._counters[dim_code] > range_end:
            config["status"] = "已耗尽"
            config["pending"] = True
            config["abnormal"] = True


# ---------------------------------------------------------------------------
# 事实分区
# ---------------------------------------------------------------------------


class FactPartitionStore:
    """事实分区登记表：映射写入、待匹配筛选、迟到维度回填和来源检索。"""

    def __init__(self) -> None:
        self._rows: list[dict[str, Any]] = []
        self._next_id = 1

    # -- 查询 ---------------------------------------------------------------
    def all_rows(self) -> list[dict[str, Any]]:
        return _copy_rows(sorted(self._rows, key=lambda r: int(r.get("id", 0))))

    def find(
        self,
        *,
        status: str | None = None,
        fact_name: str | None = None,
        source_record_id: str | None = None,
        partition_value: str | None = None,
        partition_field: str | None = None,
        dim_code: str | None = None,
        natural_key: str | None = None,
        keyword: str | None = None,
        offset: int = 0,
        limit: int | None = None,
    ) -> list[dict[str, Any]]:
        rows = [r for r in self._rows if _match_row(
            r,
            status=status,
            fact_name=fact_name,
            source_record_id=source_record_id,
            partition_value=partition_value,
            partition_field=partition_field,
            dim_code=dim_code,
            natural_key=natural_key,
            keyword=keyword,
        )]
        rows.sort(key=lambda r: int(r.get("id", 0)))
        if offset:
            rows = rows[offset:]
        if limit is not None:
            rows = rows[:limit]
        return _copy_rows(rows)

    def pending_matches(
        self,
        *,
        dim_code: str | None = None,
        natural_key: str | None = None,
        **filters: Any,
    ) -> list[dict[str, Any]]:
        """待匹配事实：按维度筛选时只返回该维度尚未拿到代理键的记录。"""
        filters.setdefault("status", PENDING)
        rows = self.find(**filters)
        if not dim_code and not natural_key:
            return rows
        result = []
        for row in rows:
            associations = row.get("外键") or {}
            if dim_code:
                assoc = associations.get(dim_code)
                if not assoc or as_int(assoc.get("代理键")) is not None:
                    continue
            if natural_key and not any(
                str(assoc.get("自然键", "")) == natural_key
                and as_int(assoc.get("代理键")) is None
                for assoc in associations.values()
            ):
                continue
            result.append(row)
        return result

    def summary(self, *, fact_name: str | None = None, include_rows: bool = False) -> dict[str, Any]:
        rows = self.find(fact_name=fact_name, limit=None)
        by_status = {PENDING: 0, MATCHED: 0}
        measure_totals: dict[str, float] = {}
        partitions: dict[str, int] = {}
        sources: set[str] = set()
        for row in rows:
            by_status[row.get("status", "")] = by_status.get(row.get("status", ""), 0) + 1
            partitions[str(row.get("分区值", ""))] = partitions.get(str(row.get("分区值", "")), 0) + 1
            source_id = str(row.get("来源记录ID", ""))
            if source_id:
                sources.add(source_id)
            for name, value in _row_measures(row).items():
                measure_totals[name] = round(measure_totals.get(name, 0.0) + float(value), 6)
        result: dict[str, Any] = {
            "record_count": len(rows),
            "pending_count": by_status.get(PENDING, 0),
            "matched_count": by_status.get(MATCHED, 0),
            "by_status": by_status,
            "measure_totals": measure_totals,
            "partitions": partitions,
            "source_records": len(sources),
        }
        if include_rows:
            result["items"] = rows
        return result

    # -- 写入 ---------------------------------------------------------------
    def import_records(
        self,
        store: Any,
        fact_name: str,
        records: list[dict[str, Any]],
        *,
        partition_field: str = "dt",
        dimension_mappings: Any = None,
        business_process: str = "",
        grain: str = "",
        source: str = "",
        persist_mapping: bool = True,
    ) -> dict[str, Any]:
        """按自然键映射把一批事实写入分区。

        已登记的自然键立即关联正式代理键；未登记的记录以「待匹配」落盘，
        来源记录ID、来源系统、粒度声明和金额完整保留。
        """
        fact_name = str(fact_name or "").strip()
        if not fact_name:
            raise ValueError("缺少事实名称")
        if not isinstance(records, list):
            raise ValueError("records 必须是事实记录数组")
        partition_field = str(partition_field or "dt").strip() or "dt"

        fact_row = _find_fact(store, fact_name)
        if fact_row is not None:
            business_process = business_process or str(fact_row.get("业务过程", ""))
            grain = grain or str(fact_row.get("粒度声明", ""))
        mappings = _normalize_mappings(dimension_mappings, records)
        if persist_mapping and fact_row is not None and mappings:
            # 事实设计持有可复用的外键映射，后续分区写入直接按映射关联。
            fact_row["外键映射"] = copy.deepcopy(mappings)
            fact_row["外键映射说明"] = "; ".join(
                f"{m['维度编码']}:{m['自然键字段']}->{m['代理键字段']}" for m in mappings
            )

        written = 0
        duplicates = 0
        matched = 0
        pending = 0
        items: list[dict[str, Any]] = []
        partitions: dict[str, int] = {}

        for raw in records:
            if not isinstance(raw, dict):
                continue
            record = copy.deepcopy(raw)
            source_id = str(
                _first_value(record, ("来源记录ID", "来源记录", "source_record_id", "source_id"))
                or ""
            ).strip()
            record_source = str(
                _first_value(record, ("来源系统", "来源", "source_system", "source"))
                or source or "manual"
            )
            partition_value = str(
                _first_value(record, (partition_field, "分区值", "partition", "partition_value"))
                or "default"
            )
            record_grain = str(
                _first_value(record, ("粒度声明", "粒度")) or grain or ""
            )
            if not source_id:
                source_id = _synthetic_source_id(record, fact_name, partition_value, mappings)

            associations, unresolved = _resolve_associations(
                store, record, mappings, existing=None
            )
            status = MATCHED if not unresolved else PENDING
            measures = _measure_values(record)
            amount = measures.get("金额", _record_amount(record))
            dim_code, natural_key, surrogate_key = _primary_binding(associations, mappings)

            existing = self._find_by_source(fact_name, source_id)
            if existing is not None:
                # 同一业务记录的重复导入：原代理键保持不变，金额覆盖而不是累加。
                associations, unresolved = _resolve_associations(
                    store, record, mappings, existing=existing
                )
                status = MATCHED if not unresolved else PENDING
                dim_code, natural_key, surrogate_key = _primary_binding(associations, mappings)
                if not measures:
                    # 重复导入缺少度量值时保留原金额，避免把已入仓金额覆盖成 0。
                    measures = dict(existing.get("度量") or {})
                    amount = existing.get("金额", 0.0)
                else:
                    _copy_measure_columns(existing, record)
                existing.update({
                    "status": status,
                    "pending": status == PENDING,
                    "事实名称": fact_name,
                    "业务过程": business_process or existing.get("业务过程", ""),
                    "粒度声明": record_grain or existing.get("粒度声明", ""),
                    "分区字段": partition_field,
                    "分区值": partition_value,
                    "分区": f"{partition_field}={partition_value}",
                    "来源系统": record_source,
                    "来源记录ID": source_id,
                    "来源": record_source,
                    "维度编码": dim_code,
                    "自然键": natural_key,
                    "代理键": surrogate_key,
                    "待匹配维度": unresolved,
                    "外键": associations,
                    "金额": amount,
                    "度量": measures,
                    "匹配状态": status,
                })
                if status == MATCHED and existing.get("回填标记") != BACKFILLED:
                    existing["回填标记"] = NOT_BACKFILLED
                    existing["关联时间"] = now_text()
                duplicates += 1
                written += 1
                items.append(copy.deepcopy(existing))
                if status == PENDING:
                    pending += 1
                else:
                    matched += 1
                continue

            row: dict[str, Any] = {
                "id": self._next_id,
                "status": status,
                "pending": status == PENDING,
                "abnormal": False,
                "事实名称": fact_name,
                "业务过程": business_process,
                "粒度声明": record_grain,
                "分区字段": partition_field,
                "分区值": partition_value,
                "分区": f"{partition_field}={partition_value}",
                "来源系统": record_source,
                "来源记录ID": source_id,
                "来源": record_source,
                "维度编码": dim_code,
                "自然键": natural_key,
                "代理键": surrogate_key,
                "待匹配维度": unresolved,
                "外键": associations,
                "金额": amount,
                "度量": measures,
                "匹配状态": status,
                "回填标记": NOT_BACKFILLED,
                "写入时间": now_text(),
                "关联时间": now_text() if status == MATCHED else None,
                "回填时间": None,
            }
            _copy_measure_columns(row, record)
            self._next_id += 1
            self._rows.append(row)
            written += 1
            if status == PENDING:
                pending += 1
            else:
                matched += 1
            items.append(copy.deepcopy(row))

        final_partitions: dict[str, int] = {}
        for item in items:
            key = str(item.get("分区值", "default"))
            final_partitions[key] = final_partitions.get(key, 0) + 1
        partitions = final_partitions

        return {
            "fact_name": fact_name,
            "partition_field": partition_field,
            "written": written,
            "inserted": written - duplicates,
            "duplicates": duplicates,
            "matched": matched,
            "pending": pending,
            "reused_keys": sum(1 for item in items if item.get("代理键")),
            "partitions": partitions,
            "mappings": copy.deepcopy(mappings),
            "measure_totals": self.summary(fact_name=fact_name)["measure_totals"],
            "items": items,
        }

    # -- 回填 ---------------------------------------------------------------
    def backfill(
        self,
        store: Any,
        *,
        dim_code: str | None = None,
        natural_key: str | None = None,
        fact_name: str | None = None,
        source_record_id: str | None = None,
    ) -> dict[str, Any]:
        """把迟到维度成员补到先前未匹配的同一条事实，金额不做任何累加。"""
        matched_items: list[dict[str, Any]] = []
        inspected = 0
        partial = 0
        for row in self._rows:
            if row.get("status") != PENDING:
                continue
            if fact_name and row.get("事实名称") != fact_name:
                continue
            if source_record_id and row.get("来源记录ID") != source_record_id:
                continue
            if dim_code and dim_code not in (row.get("外键") or {}):
                continue
            if natural_key and row.get("自然键") != natural_key and not any(
                assoc.get("自然键") == natural_key
                for assoc in (row.get("外键") or {}).values()
            ):
                continue

            inspected += 1
            associations = copy.deepcopy(row.get("外键") or {})
            unresolved: list[str] = []
            for dim, assoc in associations.items():
                if as_int(assoc.get("代理键")) is not None:
                    continue
                natural = str(assoc.get("自然键", ""))
                key = store.natural_keys.resolve(dim, natural) if natural else None
                if key is None:
                    unresolved.append(dim)
                else:
                    assoc["代理键"] = key
                    assoc["状态"] = MATCHED
            if unresolved:
                # 部分维度已补上时也要落回事实行，剩余维度仍保持待匹配。
                row["外键"] = associations
                row["待匹配维度"] = unresolved
                row["部分回填标记"] = BACKFILLED
                dim, nk, sk = _primary_binding(associations, [])
                row["维度编码"] = dim or row.get("维度编码", "")
                row["自然键"] = nk or row.get("自然键", "")
                row["代理键"] = sk
                row["已关联维度"] = [
                    d for d, assoc in associations.items()
                    if as_int(assoc.get("代理键")) is not None
                ]
                partial += 1
                continue

            row["外键"] = associations
            row["status"] = MATCHED
            row["pending"] = False
            row["匹配状态"] = MATCHED
            row["回填标记"] = BACKFILLED
            row["回填时间"] = now_text()
            row["关联时间"] = row.get("关联时间") or now_text()
            dim, nk, sk = _primary_binding(associations, [])
            row["维度编码"] = dim or row.get("维度编码", "")
            row["自然键"] = nk or row.get("自然键", "")
            row["代理键"] = sk
            row["待匹配维度"] = []
            row["已关联维度"] = list(associations)
            matched_items.append(copy.deepcopy(row))

        return {
            "inspected": inspected,
            "matched": len(matched_items),
            "partial": partial,
            "remaining": len(self.find(
                status=PENDING, fact_name=fact_name, source_record_id=source_record_id,
                dim_code=dim_code, natural_key=natural_key,
            )),
            "measure_totals": self.summary(fact_name=fact_name)["measure_totals"],
            "items": matched_items,
        }

    # -- 内部 ---------------------------------------------------------------
    def _find_by_source(self, fact_name: str, source_record_id: str) -> dict[str, Any] | None:
        for row in self._rows:
            if row.get("事实名称") == fact_name and row.get("来源记录ID") == source_record_id:
                return row
        return None


# ---------------------------------------------------------------------------
# 模块级便捷函数
# ---------------------------------------------------------------------------


def register_natural_key(
    store: Any,
    dim_code: str,
    natural_key: str,
    *,
    source: str = "",
    key_name: str = "",
    surrogate_key: int | None = None,
    auto_backfill: bool = True,
) -> dict[str, Any]:
    """登记一个自然键，并在补到维度成员时自动回填此前未匹配的事实。"""
    record = store.natural_keys.register(
        store, dim_code, natural_key,
        source=source, key_name=key_name, surrogate_key=surrogate_key,
    )
    backfill = {"matched": 0, "remaining": 0, "items": [], "measure_totals": {}}
    if auto_backfill and record.get("created"):
        backfill = store.fact_partitions.backfill(
            store, dim_code=dim_code, natural_key=str(natural_key)
        )
    record["backfill"] = backfill
    # 部分维度补齐也算「关联推进」：整行可能仍待其它维度键。
    record["backfilled_matches"] = len(backfill.get("items", [])) + int(backfill.get("partial", 0))
    record["backfilled"] = record["backfilled_matches"]
    return record


def write_fact_partition(
    store: Any,
    fact_name: str,
    records: list[dict[str, Any]],
    *,
    partition_field: str = "dt",
    dimension_mappings: Any = None,
    business_process: str = "",
    grain: str = "",
    source: str = "",
) -> dict[str, Any]:
    return store.fact_partitions.import_records(
        store, fact_name, records,
        partition_field=partition_field,
        dimension_mappings=dimension_mappings,
        business_process=business_process,
        grain=grain,
        source=source,
    )


def pending_fact_matches(
    store: Any,
    *,
    fact_name: str | None = None,
    source_record_id: str | None = None,
    partition_value: str | None = None,
    dim_code: str | None = None,
    natural_key: str | None = None,
    keyword: str | None = None,
    include_matched: bool = False,
    limit: int | None = None,
) -> dict[str, Any]:
    if include_matched:
        items = store.fact_partitions.find(
            fact_name=fact_name, source_record_id=source_record_id,
            partition_value=partition_value, dim_code=dim_code, natural_key=natural_key,
            keyword=keyword, limit=limit,
        )
    else:
        # 待匹配按「该维度还没拿到代理键」筛选，而不是整行未关联。
        items = store.fact_partitions.pending_matches(
            fact_name=fact_name, source_record_id=source_record_id,
            partition_value=partition_value, dim_code=dim_code, natural_key=natural_key,
            keyword=keyword, limit=limit,
        )
    return {
        "items": items,
        "total": len(items),
        "status": "全部" if include_matched else PENDING,
        "summary": store.fact_partitions.summary(fact_name=fact_name),
    }


def backfill_pending_facts(
    store: Any,
    *,
    dim_code: str | None = None,
    natural_key: str | None = None,
    fact_name: str | None = None,
    source_record_id: str | None = None,
) -> dict[str, Any]:
    return store.fact_partitions.backfill(
        store,
        dim_code=dim_code,
        natural_key=natural_key,
        fact_name=fact_name,
        source_record_id=source_record_id,
    )


def find_fact_by_source_record(
    store: Any,
    source_record_id: str,
    *,
    fact_name: str | None = None,
) -> dict[str, Any]:
    items = store.fact_partitions.find(
        fact_name=fact_name, source_record_id=str(source_record_id)
    )
    amounts = [float(item.get("金额") or 0.0) for item in items]
    return {
        "items": items,
        "total": len(items),
        "source_record_id": str(source_record_id),
        "amount": round(sum(amounts), 6),
        "amounts": amounts,
    }


# ---------------------------------------------------------------------------
# 映射解析
# ---------------------------------------------------------------------------


def _normalize_mappings(raw: Any, records: list[dict[str, Any]]) -> list[dict[str, str]]:
    mappings: list[dict[str, str]] = []

    def add(dim: Any, nk_field: Any, sk_field: Any) -> None:
        dim_code = str(dim or "").strip()
        if not dim_code:
            return
        entry = {
            "维度编码": dim_code,
            "自然键字段": str(nk_field or "自然键").strip() or "自然键",
            "代理键字段": str(sk_field or "").strip(),
        }
        for existing in mappings:
            if existing["维度编码"] == dim_code:
                existing.update(entry)
                return
        mappings.append(entry)

    if isinstance(raw, dict):
        for dim, spec in raw.items():
            if isinstance(spec, str):
                add(dim, spec, "")
            elif isinstance(spec, dict):
                add(
                    dim,
                    spec.get("自然键字段") or spec.get("nk_field") or spec.get("natural_key_field"),
                    spec.get("代理键字段") or spec.get("sk_field") or spec.get("surrogate_key_field"),
                )
    elif isinstance(raw, list):
        for spec in raw:
            if isinstance(spec, str):
                add(spec, "自然键", "")
            elif isinstance(spec, dict):
                add(
                    spec.get("维度编码") or spec.get("dim_code") or spec.get("dimension"),
                    spec.get("自然键字段") or spec.get("nk_field") or spec.get("natural_key_field"),
                    spec.get("代理键字段") or spec.get("sk_field") or spec.get("surrogate_key_field"),
                )

    # 记录级显式声明：{"维度编码": "DIM_PRODUCT", "自然键": "P-1"}
    for record in records:
        if not isinstance(record, dict):
            continue
        if record.get("维度编码") and (
            record.get("自然键") or record.get("natural_key") or record.get("product_nk")
        ):
            add(
                record.get("维度编码"),
                "自然键" if record.get("自然键") else ("natural_key" if record.get("natural_key") else "product_nk"),
                "代理键",
            )

    for default in _DEFAULT_MAPPINGS:
        if any(m["维度编码"] == default["维度编码"] for m in mappings):
            continue
        if any(isinstance(r, dict) and default["自然键字段"] in r for r in records):
            add(default["维度编码"], default["自然键字段"], default["代理键字段"])

    return mappings


def _resolve_associations(
    store: Any,
    record: dict[str, Any],
    mappings: list[dict[str, str]],
    *,
    existing: dict[str, Any] | None,
) -> tuple[dict[str, dict[str, Any]], list[str]]:
    associations: dict[str, dict[str, Any]] = {}
    if existing:
        associations = copy.deepcopy(existing.get("外键") or {})

    for mapping in mappings:
        dim_code = mapping["维度编码"]
        nk_field = mapping["自然键字段"]
        natural_key = record.get(nk_field)
        if natural_key in (None, ""):
            natural_key = record.get("自然键") if dim_code == record.get("维度编码") else None
        if natural_key in (None, ""):
            # 重复导入时字段缺省不应抹掉已有的自然键。
            continue
        natural_key = str(natural_key).strip()

        old = associations.get(dim_code) or {}
        explicit_key = as_int(record.get(mapping.get("代理键字段") or "代理键"))
        key = store.natural_keys.resolve(dim_code, natural_key)
        if key is None and old.get("自然键") == natural_key:
            # 重复导入同一业务记录：沿用首次关联的正式代理键，不因重放而漂移。
            key = as_int(old.get("代理键"))
        if key is None:
            key = explicit_key
        associations[dim_code] = {
            "维度编码": dim_code,
            "自然键": natural_key,
            "代理键": key,
            "状态": MATCHED if key is not None else PENDING,
        }

    unresolved = [
        dim for dim, assoc in associations.items()
        if as_int(assoc.get("代理键")) is None
    ]
    return associations, unresolved


def _primary_binding(
    associations: dict[str, dict[str, Any]],
    mappings: list[dict[str, str]],
) -> tuple[str, str, int | None]:
    """选出事实行上的主关联键：优先已匹配、且映射中排序最靠前的维度。"""
    ordered: list[str] = [m["维度编码"] for m in mappings if m["维度编码"] in associations]
    ordered += [dim for dim in associations if dim not in ordered]
    for dim in ordered:
        assoc = associations[dim]
        key = as_int(assoc.get("代理键"))
        if key is not None:
            return dim, str(assoc.get("自然键", "")), key
    for dim in ordered:
        natural_key = str(associations[dim].get("自然键", ""))
        if natural_key:
            return dim, natural_key, None
    return "", "", None


def _match_row(
    row: dict[str, Any],
    *,
    status: str | None,
    fact_name: str | None,
    source_record_id: str | None,
    partition_value: str | None,
    partition_field: str | None,
    dim_code: str | None,
    natural_key: str | None,
    keyword: str | None,
) -> bool:
    if status and status not in ("全部", "all", "*") and row.get("status") != status:
        return False
    if fact_name and str(row.get("事实名称", "")) != fact_name:
        return False
    if source_record_id and str(row.get("来源记录ID", "")) != str(source_record_id):
        return False
    if partition_value and str(row.get("分区值", "")) != str(partition_value):
        return False
    if partition_field and str(row.get("分区字段", "")) != str(partition_field):
        return False
    if dim_code and dim_code not in (row.get("外键") or {}):
        return False
    if natural_key and natural_key not in {
        str(row.get("自然键", "")),
        *(str(a.get("自然键", "")) for a in (row.get("外键") or {}).values()),
    }:
        return False
    if keyword:
        needle = keyword.casefold()
        haystack = " ".join(str(row.get(field, "")) for field in (
            "来源记录ID", "来源系统", "事实名称", "自然键", "分区值", "粒度声明",
        )).casefold()
        if needle not in haystack:
            return False
    return True


def _row_measures(row: dict[str, Any]) -> dict[str, float]:
    measures = dict(row.get("度量") or {})
    if "金额" not in measures and row.get("金额") not in (None, ""):
        amount = as_float(row.get("金额"))
        if amount is not None:
            measures["金额"] = amount
    return measures


def _copy_measure_columns(target: dict[str, Any], record: dict[str, Any]) -> None:
    for key, value in record.items():
        if key in _RECORD_META_FIELDS:
            continue
        if key in ("分区值",):
            continue
        if as_float(value) is not None:
            target[key] = as_float(value)


def _find_fact(store: Any, fact_name: str) -> dict[str, Any] | None:
    for row in store.rows("fact_design"):
        if str(row.get("事实名称", "")) == fact_name:
            return row
    return None


def _synthetic_source_id(
    record: dict[str, Any],
    fact_name: str,
    partition_value: str,
    mappings: list[dict[str, str]],
) -> str:
    parts = [fact_name, partition_value]
    for mapping in mappings:
        parts.append(str(record.get(mapping["自然键字段"], "")))
    parts.extend(
        str(record.get(field, ""))
        for field in ("粒度声明", "金额", *_AMOUNT_FIELDS)
        if field in record
    )
    digest = hashlib.sha256("\u0000".join(parts).encode("utf-8")).hexdigest()[:12]
    return f"AUTO-{digest}"
