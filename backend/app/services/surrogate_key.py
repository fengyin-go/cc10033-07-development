"""代理键服务：管理维度表的代理键生成策略。

增强能力：
- 键段分配策略：自增序列/UUID_HASH/HASH_MOD
- 步长和初始值管理
- 键耗尽检测和自动重置
- 冲突处理：跳过重试/重新分配/异常告警
"""
from __future__ import annotations

from typing import Any

from app.ingest import PENDING, register_natural_key as bind_natural_key
from app.services.base import BaseModuleService
from app.store import store

KEY_STRATEGIES = {"自增序列", "UUID_HASH", "HASH_MOD", "雪花算法", "时间戳序列"}


class SurrogateKeyService(BaseModuleService):
    MODULE = "surrogate_key"
    ENTITY = "键值定义"
    OPERATION_TERM = "校验"
    LIST_FIELDS = ["键值名称", "目标维度", "生成策略", "初始值", "步长", "唯一范围", "冲突处理"]
    REQUIRED_FIELDS = ["键值名称", "目标维度", "生成策略"]
    STATUS_ORDER = ["草稿", "已分配", "已耗尽", "已重置"]
    ACTION_RULES = {"分配键段": "草稿", "重置键值": "已耗尽"}
    NEGATIVE_ACTIONS = []

    def create_entry(self, values: dict[str, Any]) -> tuple[dict[str, Any] | None, list[str]]:
        strategy = str(values.get("生成策略", "")).strip()
        if strategy not in KEY_STRATEGIES:
            values["生成策略"] = "自增序列"

        if not values.get("初始值"):
            values["初始值"] = "1000000"
        if not values.get("步长"):
            values["步长"] = "1"
        if not values.get("唯一范围"):
            values["唯一范围"] = "全局"
        if not values.get("冲突处理"):
            values["冲突处理"] = "跳过重试"

        # 消费维度定义
        store.channel.consume("dim.defined", "surrogate_key")

        entry, missing = super().create_entry(values)
        if entry is None:
            return None, missing

        store.channel.publish("surrogate_key.defined", {
            "key_name": entry["键值名称"],
            "dim_code": entry.get("目标维度"),
            "strategy": entry.get("生成策略"),
            "entry_id": entry["id"],
        })

        store.artifacts.register(
            "surrogate_key", "surrogate_key_def",
            f"sk_{entry['id']}",
            {"key_name": entry["键值名称"], "dim_code": entry.get("目标维度")},
            ["dim_definition", "surrogate_key"],
        )
        return entry, []

    def run_action(self, entry_id: int, action: str) -> tuple[dict[str, Any] | None, str]:
        entry, message = super().run_action(entry_id, action)
        if entry is None:
            return None, message

        if action == "分配键段":
            allocated = _allocate_key_range(entry)
            store.channel.publish("surrogate_key.allocated", {
                "key_name": entry["键值名称"],
                "range": allocated,
                "entry_id": entry_id,
            })
        elif action == "重置键值":
            store.channel.publish("surrogate_key.reset", {
                "key_name": entry["键值名称"],
                "entry_id": entry_id,
            })
        return entry, message

    def domain_snapshot(self) -> dict[str, Any]:
        base = super().domain_snapshot()
        exhausted = sum(1 for r in store.rows(self.MODULE) if r.get("status") == "已耗尽")
        base["exhausted_keys"] = exhausted
        base["natural_key_bindings"] = store.natural_keys.count()
        base["pending_fact_rows"] = len(store.fact_partitions.pending_matches())
        return base

    def register_natural_key(self, values: dict[str, Any]) -> dict[str, Any]:
        """登记维度自然键并返回正式代理键；首次登记顺带回填未匹配事实。"""
        dim_code = str(values.get("目标维度") or values.get("维度编码") or "").strip()
        natural_key = str(values.get("自然键") or values.get("自然键值") or "").strip()
        result = bind_natural_key(
            store,
            dim_code,
            natural_key,
            source=str(values.get("来源系统") or values.get("来源") or ""),
            key_name=str(values.get("键值名称") or ""),
            surrogate_key=values.get("代理键"),
        )
        store.channel.publish("surrogate_key.natural_key_registered", {
            "dim_code": dim_code,
            "natural_key": natural_key,
            "surrogate_key": result["代理键"],
            "created": result["created"],
        })
        return result

    def list_natural_keys(
        self,
        *,
        dim_code: str | None = None,
        natural_key: str | None = None,
        surrogate_key: int | None = None,
        keyword: str | None = None,
        status: str | None = None,
    ) -> list[dict[str, Any]]:
        rows = store.natural_keys.list_records(
            dim_code=dim_code, natural_key=natural_key,
            surrogate_key=surrogate_key, keyword=keyword, status=status,
        )
        for row in rows:
            row["待匹配事实数"] = len(
                store.fact_partitions.pending_matches(
                    dim_code=row["目标维度"], natural_key=row["自然键"])
            )
        return rows

    def mapping_snapshot(self) -> dict[str, Any]:
        """维度自然键与事实分区的映射关系视图，供工作台展示。"""
        records = store.natural_keys.list_records()
        return {
            "natural_key_count": len(records),
            "by_dimension": _count_by(records, "目标维度"),
            "fact_summary": store.fact_partitions.summary(),
            "pending_status": PENDING,
        }


def _allocate_key_range(entry: dict[str, Any]) -> dict[str, Any]:
    start = int(entry.get("初始值", "1000000"))
    step = int(entry.get("步长", "1"))
    end = start + step * 10000
    # 记录键段边界，自然键登记时按此游标分配正式代理键。
    entry["下一个键"] = int(entry.get("下一个键") or start)
    entry["键段结束"] = end
    return {"start": start, "end": end, "count": 10000}


def _count_by(rows: list[dict[str, Any]], field: str) -> dict[str, int]:
    counts: dict[str, int] = {}
    for row in rows:
        key = str(row.get(field, ""))
        counts[key] = counts.get(key, 0) + 1
    return counts