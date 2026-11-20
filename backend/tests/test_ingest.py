"""事实入仓测试：自然键映射、分区写入、待匹配检索与迟到维度回填。

覆盖需求：
- 维度自然键 → 正式代理键的映射；重复导入保持代理键稳定；
- 不同自然键不共用正式代理键；
- 事实先到、维度键迟到时先落待匹配，回填后仍可按源记录检索且金额不翻倍。
"""
from __future__ import annotations

import pytest

from app.ingest import (
    MATCHED,
    PENDING,
    backfill_pending_facts,
    find_fact_by_source_record,
    pending_fact_matches,
    register_natural_key,
    write_fact_partition,
)
from app.seed import SEED_ROWS
from app.store import Store


@pytest.fixture
def store() -> Store:
    return Store()


def sales_records() -> list[dict]:
    return [
        {
            "来源记录ID": "SO-001",
            "来源系统": "pos",
            "dt": "2026-11-01",
            "product_nk": "P-100",
            "store_nk": "S-001",
            "sales_amt": 120.5,
            "sales_qty": 2,
            "粒度声明": "每笔销售交易明细行",
        },
        {
            "来源记录ID": "SO-002",
            "来源系统": "pos",
            "dt": "2026-11-01",
            "product_nk": "P-200",
            "store_nk": "S-001",
            "sales_amt": 80,
            "sales_qty": 1,
        },
    ]


class TestNaturalKeyMapping:
    """维度自然键与正式代理键的映射。"""

    def test_natural_key_gets_formal_surrogate_key(self, store: Store) -> None:
        record = register_natural_key(store, "DIM_PRODUCT", "P-100", source="product_master")
        assert record["created"] is True
        assert record["代理键"] == 1000000  # 来自 sk_product 的初始值
        assert record["编号"] == record["代理键"]

    def test_repeated_registration_keeps_surrogate_key(self, store: Store) -> None:
        first = register_natural_key(store, "DIM_PRODUCT", "P-100")
        second = register_natural_key(store, "DIM_PRODUCT", "P-100")
        assert second["created"] is False
        assert second["代理键"] == first["代理键"]
        # 再次登记不能额外消耗键段
        third = register_natural_key(store, "DIM_PRODUCT", "P-101")
        assert third["代理键"] == first["代理键"] + 1

    def test_distinct_natural_keys_never_share_surrogate_key(self, store: Store) -> None:
        keys = {
            register_natural_key(store, "DIM_PRODUCT", f"P-{i}")["代理键"]
            for i in range(20)
        }
        assert len(keys) == 20

    def test_explicit_key_conflict_rejected(self, store: Store) -> None:
        first = register_natural_key(store, "DIM_PRODUCT", "P-100")
        with pytest.raises(ValueError, match="已被"):
            register_natural_key(store, "DIM_PRODUCT", "P-200", surrogate_key=first["代理键"])

    def test_binding_is_recorded_on_surrogate_key_module(self, store: Store) -> None:
        register_natural_key(store, "DIM_PRODUCT", "P-100")
        config = [r for r in store.rows("surrogate_key") if r["目标维度"] == "DIM_PRODUCT"][0]
        assert config["已分配数"] == 1
        assert config["下一个键"] == 1000001


class TestFactPartitionWrite:
    """按映射写入事实分区，并保留来源与粒度。"""

    def test_unmatched_fact_is_pending_with_source_and_grain(self, store: Store) -> None:
        result = write_fact_partition(store, "销售明细事实", sales_records(), source="pos")
        assert result["written"] == 2
        assert result["pending"] == 2
        assert result["matched"] == 0
        assert result["partitions"] == {"2026-11-01": 2}

        row = result["items"][0]
        assert row["status"] == PENDING
        assert row["来源记录ID"] == "SO-001"
        assert row["来源系统"] == "pos"
        assert row["粒度声明"] == "每笔销售交易明细行"
        assert row["分区"] == "dt=2026-11-01"
        assert row["待匹配维度"] == ["DIM_PRODUCT", "DIM_STORE"]
        assert row["金额"] == 120.5

    def test_matched_fact_gets_surrogate_key_without_pending(self, store: Store) -> None:
        register_natural_key(store, "DIM_PRODUCT", "P-100")
        register_natural_key(store, "DIM_PRODUCT", "P-200")
        register_natural_key(store, "DIM_STORE", "S-001")
        result = write_fact_partition(store, "销售明细事实", sales_records(), source="pos")
        assert result["pending"] == 0
        assert result["matched"] == 2
        row = result["items"][0]
        assert row["status"] == MATCHED
        assert row["代理键"] == 1000000
        assert row["外键"]["DIM_PRODUCT"]["代理键"] == 1000000

    def test_duplicate_import_keeps_one_row_and_one_amount(self, store: Store) -> None:
        write_fact_partition(store, "销售明细事实", sales_records(), source="pos")
        second = write_fact_partition(store, "销售明细事实", sales_records(), source="pos")
        assert second["duplicates"] == 2
        assert len(store.fact_partitions.all_rows()) == 2
        assert second["measure_totals"]["金额"] == 200.5

    def test_duplicate_import_after_backfill_keeps_proxy_key(self, store: Store) -> None:
        write_fact_partition(store, "销售明细事实", sales_records(), source="pos")
        key = register_natural_key(store, "DIM_PRODUCT", "P-100")["代理键"]
        register_natural_key(store, "DIM_STORE", "S-001")
        replay = write_fact_partition(store, "销售明细事实", sales_records(), source="pos")
        assert replay["duplicates"] == 2
        assert replay["items"][0]["代理键"] == key
        assert replay["items"][0]["status"] == MATCHED
        assert replay["measure_totals"]["金额"] == 200.5

    def test_mapping_is_persisted_on_fact_design(self, store: Store) -> None:
        fact = [r for r in store.rows("fact_design") if r["事实名称"] == "销售明细事实"][0]
        write_fact_partition(store, "销售明细事实", sales_records(), source="pos")
        mapping = {m["维度编码"]: m["自然键字段"] for m in fact["外键映射"]}
        assert mapping["DIM_PRODUCT"] == "product_nk"
        assert mapping["DIM_STORE"] == "store_nk"


class TestPendingAndBackfill:
    """先到事实的待匹配筛选与迟到维度回填。"""

    def test_pending_filter_locates_fact_by_source_record(self, store: Store) -> None:
        write_fact_partition(store, "销售明细事实", sales_records(), source="pos")
        found = pending_fact_matches(store, fact_name="销售明细事实", source_record_id="SO-001")
        assert found["total"] == 1
        assert found["items"][0]["金额"] == 120.5
        assert found["items"][0]["status"] == PENDING

    def test_backfill_links_same_fact_and_keeps_amount(self, store: Store) -> None:
        write_fact_partition(store, "销售明细事实", sales_records(), source="pos")
        register_natural_key(store, "DIM_PRODUCT", "P-100", auto_backfill=False)
        register_natural_key(store, "DIM_STORE", "S-001", auto_backfill=False)

        result = backfill_pending_facts(store, fact_name="销售明细事实", source_record_id="SO-001")
        assert result["matched"] == 1
        assert result["items"][0]["status"] == MATCHED
        assert result["items"][0]["回填标记"] == "已回填"
        assert result["measure_totals"]["金额"] == 200.5

        # 回填之后仍能按源记录检索到同一条事实和金额
        located = find_fact_by_source_record(store, "SO-001")
        assert located["total"] == 1
        assert located["amount"] == 120.5
        # 另一条仍待匹配，金额不因回填而重复累计
        assert pending_fact_matches(store, fact_name="销售明细事实")["total"] == 1

    def test_register_natural_key_auto_backfills_pending_facts(self, store: Store) -> None:
        write_fact_partition(store, "销售明细事实", sales_records(), source="pos")
        record = register_natural_key(store, "DIM_PRODUCT", "P-100")
        assert record["backfilled_matches"] == 1
        row = find_fact_by_source_record(store, "SO-001")["items"][0]
        # 产品键已补上，门店键仍未到，整行仍待匹配但外键已更新
        assert row["status"] == PENDING
        assert row["外键"]["DIM_PRODUCT"]["代理键"] == record["代理键"]
        # P-200 仍未登记，只有 SO-001 的产品键被推进；整行仍待匹配
        assert pending_fact_matches(
            store, dim_code="DIM_PRODUCT", source_record_id="SO-001")["total"] == 0
        assert pending_fact_matches(store, dim_code="DIM_PRODUCT")["total"] == 1

        register_natural_key(store, "DIM_STORE", "S-001")
        row = find_fact_by_source_record(store, "SO-001")["items"][0]
        assert row["status"] == MATCHED
        assert row["外键"]["DIM_STORE"]["代理键"] is not None

    def test_backfill_is_idempotent(self, store: Store) -> None:
        write_fact_partition(store, "销售明细事实", sales_records(), source="pos")
        register_natural_key(store, "DIM_PRODUCT", "P-100", auto_backfill=False)
        register_natural_key(store, "DIM_STORE", "S-001", auto_backfill=False)
        first = backfill_pending_facts(store, fact_name="销售明细事实")
        second = backfill_pending_facts(store, fact_name="销售明细事实")
        assert first["matched"] == 1
        assert second["matched"] == 0
        assert second["measure_totals"]["金额"] == 200.5

    def test_pending_summary_reports_totals_once(self, store: Store) -> None:
        write_fact_partition(store, "销售明细事实", sales_records(), source="pos")
        write_fact_partition(store, "销售明细事实", sales_records(), source="pos")
        summary = store.fact_partitions.summary(fact_name="销售明细事实")
        assert summary["record_count"] == 2
        assert summary["pending_count"] == 2
        assert summary["measure_totals"]["金额"] == 200.5


class TestSeedSync:
    """种子数据与登记表解耦，不会污染全局示例。"""

    def test_fresh_store_starts_without_bindings(self, store: Store) -> None:
        assert store.natural_keys.count() == 0
        assert store.fact_partitions.summary()["record_count"] == 0
        assert store.rows("fact_design") == [dict(row) for row in SEED_ROWS["fact_design"]]
