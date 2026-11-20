"""业务路由汇总：所有模块共用一套资源路由，由领域服务完成分派。"""
from __future__ import annotations

from app.routers import ingest, resource

# ingest 含固定子路径（pending-matches/backfill/by-source），必须排在
# resource 的动态 /api/{module}/{entry_id} 之前注册。
ROUTERS = [ingest, resource]
