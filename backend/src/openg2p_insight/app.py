"""
OpenG2P Insight backend — FastAPI app.

Uses openg2p-fastapi-common's Initializer pattern. Services and controllers
register themselves via BaseComponent on construction; we just instantiate
them in the right order.
"""

from __future__ import annotations

import logging

from openg2p_fastapi_common.app import Initializer as CommonInitializer
from openg2p_fastapi_common.context import app_registry

from .config import Settings
from .controllers.analytics_controller import AnalyticsController
from .controllers.audit_controller import AuditController
from .controllers.catalog_controller import CatalogController
from .controllers.chat_controller import ChatController
from .controllers.dashboard_controller import DashboardController
from .controllers.geo_controller import GeoController
from .controllers.health_controller import HealthController
from .services.analytics_service import AnalyticsService
from .services.audit_service import AuditService
from .services.cost_gate_service import CostGateService
from .services.llm_service import LlmService
from .services.masterdata_service import MasterDataService
from .services.pii_service import PiiService
from .services.query_executor_service import QueryExecutorService
from .services.schema_service import SchemaService
from .services.sql_planner_service import SqlPlannerService
from .services.superset_service import SupersetService

_config = Settings.get_config(strict=False)
_logger = logging.getLogger(_config.logging_default_logger_name)


class Initializer(CommonInitializer):
    def initialize(self):
        super().initialize()

        # Services — order matters where one depends on another.
        SchemaService()
        PiiService()
        LlmService()
        SqlPlannerService()
        CostGateService()
        QueryExecutorService()
        AnalyticsService()
        SupersetService()
        MasterDataService()
        AuditService()

        # Controllers register their routers via post_init().
        HealthController().post_init()
        ChatController().post_init()
        DashboardController().post_init()
        AnalyticsController().post_init()
        CatalogController().post_init()
        GeoController().post_init()
        AuditController().post_init()

        _logger.info("openg2p-insight initialized")


_initializer = Initializer()
app = app_registry.get()
