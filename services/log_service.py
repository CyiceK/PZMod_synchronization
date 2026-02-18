"""
Log service - Compatibility wrapper for advanced logging system.

This module provides backward compatibility with the old log_service API
while using the new AdvancedLogService under the hood.

@author: Cyicek
"""
from .advanced_log_service import (
    AdvancedLogService,
    LogLevel,
    LogEntry
)

# Create global singleton using the advanced service
log_service = AdvancedLogService()

# Backward compatibility alias (old code may reference LogService)
LogService = AdvancedLogService

# Export all classes and instances for backward compatibility
__all__ = [
    "log_service",
    "LogService",
    "LogLevel",
    "LogEntry",
    "AdvancedLogService",
]
