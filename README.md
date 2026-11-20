# 维度建模开发中心

面向数据仓库建设场景，提供维度建模、事实表设计、缓慢变化维和汇总层构建的建模平台

这是一个前后端分离的管理平台：前端 Vue 3 + Vite + TypeScript，后端 FastAPI（Python）。
两边各自独立启动，前端 dev server 已关掉自动打开页面，启动后按终端打印的地址手工打开。

## 目录结构

```text
.
├── frontend/                 Vue 3 + Vite + TypeScript 前端
│   ├── src/views/WorkbenchView.vue|tsx  全部模块共享工作台
│   ├── src/data/modules.ts              18个业务模块配置
│   ├── src/api/              统一请求封装
│   └── vite.config.ts        dev server 配置（open: false）
├── backend/                  FastAPI（Python） 后端
│   ├── app/routers/resource.py  通用查询/导出/登记/动作接口
│   ├── app/services/<key>.py    每个模块独立领域逻辑
│   ├── app/services/base.py     共享服务骨架
│   └── app/store.py          内存数据仓库与示例数据
├── .gitignore
└── docker-compose.yml
```

## 启动

### 后端

```bash
cd backend
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
./run.sh
```

健康检查：`curl http://127.0.0.1:8000/api/health`

### 前端

```bash
cd frontend
npm ci
npm run dev
```

前端默认监听 `http://127.0.0.1:5173/`，dev server 不会自动打开浏览器，
需要自己访问。`/api` 由 vite 代理到后端 `http://127.0.0.1:8000`。

## 事实入仓（自然键映射 → 分区写入 → 待匹配回填）

代理键的键段已经分配，但事实入仓还需要「维度自然键 → 正式代理键」的映射。
`backend/app/ingest.py` 提供 `NaturalKeyRegistry`（自然键登记）和
`FactPartitionStore`（事实分区登记），核心约定：

- 同一 `(维度编码, 自然键)` 重复登记返回同一个正式代理键，代理键保持稳定；
- 不同自然键不会落到同一个正式代理键上，显式指定已占用键会直接报错；
- 事实按映射写入分区，`来源记录ID / 来源系统 / 粒度声明 / 分区` 全部保留；
- 同一业务记录（事实名 + 来源记录ID）重复导入只保留一行，金额覆盖不累加；
- 维度键迟到时事实先记为「待匹配」，补到维度成员后回填关联，金额不翻倍，
  回填前后都可以按源记录检索到同一条事实。

前端工作台：`/fact_ingest`（侧边栏「事实入仓」），可先写销售事实 → 按待匹配筛选 →
登记产品自然键 → 在分区中定位关联成功的源记录和金额。

| 接口 | 说明 |
| --- | --- |
| `GET /api/surrogate_key/natural-keys` | 自然键与正式代理键映射，含待匹配事实数 |
| `POST /api/surrogate_key/natural-keys` | 登记自然键并确定代理键（默认顺带回填） |
| `POST /api/fact_design/{id}/partitions` | 按事实设计的外键映射写入事实分区 |
| `GET /api/fact_design/pending-matches` | 待匹配事实检索（来源记录、分区、粒度、维度/自然键） |
| `GET /api/fact_design/by-source/{来源记录ID}` | 按源记录定位事实与金额 |
| `GET /api/fact_design/partition-summary` | 分区概况、待匹配/已关联数量与金额合计 |
| `POST /api/fact_design/backfill` | 维度键迟到后回填未匹配事实 |

## 业务模块

| 模块 | 目录 | 业务对象 | 主要字段 |
| --- | --- | --- | --- |
| 维度定义 | `dim_definition` | 维度表 | 维度编码、维度名称、主键定义 |
| 维度属性 | `dim_attr` | 属性字段 | 属性名称、所属维度、数据类型 |
| 事实设计 | `fact_design` | 事实表 | 事实名称、业务过程、粒度声明 |
| 度量配置 | `measure_config` | 度量字段 | 度量名称、所属事实、度量类型 |
| SCD处理 | `scd_handler` | SCD策略 | 策略名称、目标维度、SCD类型 |
| 代理键 | `surrogate_key` | 键值定义 | 键值名称、目标维度、生成策略 |
| 层级构建 | `hierarchy_build` | 层级关系 | 层级名称、所属维度、父子字段 |
| 星型查询 | `star_query` | 查询定义 | 查询名称、事实表、关联维度 |
| 汇总构建 | `agg_summary` | 汇总表 | 汇总名称、源事实表、汇总维度 |
| 桥接关联 | `dim_bridge` | 桥接表 | 桥接名称、多对多维度、关联事实 |
| ETL映射 | `etl_mapping` | 映射规则 | 规则名称、源表、目标表 |
| 快照计划 | `snapshot_plan` | 快照策略 | 策略名称、目标表、快照周期 |
| 模型血缘 | `model_lineage` | 依赖关系 | 关系ID、上游模型、下游模型 |
| 一致性维度 | `conform_dim` | 标准维度 | 标准名称、维度主题、标准键值 |
| DataVault | `data_vault` | Vault模型 | 模型名称、Hub实体、Link关系 |
| 多维立方 | `cube_builder` | Cube定义 | Cube名称、源事实表、维度组合 |
| 变更日志 | `slow_change_log` | 维度变更 | 变更ID、目标维度、变更字段 |
| 日期维度 | `date_dim_gen` | 日期记录 | 日期键、完整日期、年份 |
| 模型校验 | `model_validate` | 校验规则 | 规则名称、目标模型、校验类型 |
| 部署流水线 | `deploy_pipeline` | 部署任务 | 任务名称、目标模型、部署环境 |

## 约定

- 全部模块共享`frontend/src/views/WorkbenchView.vue|tsx`和一套资源路由。
- 每个模块仍保留独立`backend/app/services/<模块>.py`；纯前端保留
  `frontend/src/domain/<模块>.ts`，用于承载真实业务差异并维持36题位容量。
- 列表接口统一返回 `{ items, total, page, size }`，动作接口统一返回 `{ ok, message }`。
- 状态流转只允许在 `app/services` 里改，路由层不做业务判断。

领域：数据工程
子方向：数仓与数据湖
