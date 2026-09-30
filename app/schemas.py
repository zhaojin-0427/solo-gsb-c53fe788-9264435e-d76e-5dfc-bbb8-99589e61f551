"""请求/响应模型与 JSON 编码。"""
from __future__ import annotations

import datetime as dt
import json
from decimal import Decimal
from typing import Any
from uuid import UUID

from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field


def normalize_ts(value: dt.datetime) -> dt.datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=dt.timezone.utc)
    return value.astimezone(dt.timezone.utc)


class CertificateIn(BaseModel):
    instrument_id: str = Field(min_length=1, max_length=200)
    calibrated_at: dt.datetime
    valid_from: dt.datetime
    valid_to: dt.datetime
    error: Decimal
    tolerance: Decimal
    parent_id: UUID | None = None
    expected_revision: int = Field(ge=0)


class MeasurementIn(BaseModel):
    instrument_id: str = Field(min_length=1, max_length=200)
    value: Decimal
    measured_at: dt.datetime
    expected_revision: int = Field(ge=0)


class RevokeIn(BaseModel):
    revoked_from: dt.datetime
    expected_revision: int = Field(ge=0)


def _default(obj: Any) -> Any:
    if isinstance(obj, Decimal):
        # 以数字（而非字符串）输出，保留输入精度
        return float(obj)
    if isinstance(obj, dt.datetime):
        return normalize_ts(obj).isoformat().replace("+00:00", "Z")
    if isinstance(obj, UUID):
        return str(obj)
    raise TypeError(f"不可序列化的类型: {type(obj)!r}")


def dumps(obj: Any) -> str:
    return json.dumps(obj, default=_default, ensure_ascii=False)


class ApiJSONResponse(JSONResponse):
    """统一 JSON 响应：Decimal→数字、datetime→ISO8601 UTC、UUID→字符串。"""

    def render(self, content: Any) -> bytes:
        return dumps(content).encode("utf-8")
