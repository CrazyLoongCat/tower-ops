"""配置合成、schema 版本、受保护路径与配置包 (E16, 02 §10)。

配置分节: site / devices / actions / interlocks / calibrations /
vision_pipelines / recipes / models。相对路径以**配置包根目录**解析, 不相对 cwd。
"""
from application.infrastructure.config.base import (
    CONFIG_SCHEMA_VERSION,
    PROTECTED_PATH_PREFIXES,
    ConfigRoot,
    ConfigVersionError,
    OverrideNotAllowed,
    ProtectedPathError,
    is_protected,
    require_schema_version,
)
from application.infrastructure.config.compose import (
    SECTIONS,
    ComposeInputs,
    ConfigCatalog,
    ConfigReport,
    FrozenConfig,
    check_override_paths,
    compose_config,
    freeze_config,
    validate_composed,
)
from application.infrastructure.config.migrate import diff_config, migrate_config
from application.infrastructure.config.package import load_package

__all__ = [
    "CONFIG_SCHEMA_VERSION", "PROTECTED_PATH_PREFIXES", "SECTIONS", "ComposeInputs",
    "ConfigCatalog", "ConfigReport", "ConfigRoot", "ConfigVersionError",
    "FrozenConfig", "OverrideNotAllowed", "ProtectedPathError",
    "check_override_paths", "compose_config", "diff_config", "freeze_config",
    "is_protected", "load_package", "migrate_config", "require_schema_version",
    "validate_composed",
]
