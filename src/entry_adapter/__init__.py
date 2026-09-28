# PART: entry-adapter v0.1.0 (parts@06af7f5)
from .auth import (
    STG_DEFAULT_ORG_ENV,
    STG_DEFAULT_TENANT_ENV,
    VERIFIED_ORG_STATE_ATTR,
    VERIFIED_TENANT_STATE_ATTR,
    authenticate_scope,
)

__all__ = [
    "authenticate_scope",
    "VERIFIED_TENANT_STATE_ATTR",
    "VERIFIED_ORG_STATE_ATTR",
    "STG_DEFAULT_TENANT_ENV",
    "STG_DEFAULT_ORG_ENV",
]
