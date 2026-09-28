"""JFastFramework -- plugin-based FastAPI framework for microservices."""

from jfastframework.app import create_app, get_context
from jfastframework.context import AppContext
from jfastframework.errors import (
    ConflictError,
    ForbiddenError,
    JFastError,
    NotFoundError,
    PreconditionFailedError,
    ServiceUnavailableError,
    UnauthorizedError,
    ValidationError,
)
from jfastframework.plugins.base import (
    HealthReport,
    InfraService,
    Plugin,
    PluginMeta,
    PluginSettings,
)
from jfastframework.settings import JFastConfig, JFastSettings

__version__ = "0.1.0a9"

__all__ = [
    "AppContext",
    "ConflictError",
    "ForbiddenError",
    "HealthReport",
    "InfraService",
    "JFastConfig",
    "JFastError",
    "JFastSettings",
    "NotFoundError",
    "Plugin",
    "PluginMeta",
    "PluginSettings",
    "PreconditionFailedError",
    "ServiceUnavailableError",
    "UnauthorizedError",
    "ValidationError",
    "__version__",
    "create_app",
    "get_context",
]
