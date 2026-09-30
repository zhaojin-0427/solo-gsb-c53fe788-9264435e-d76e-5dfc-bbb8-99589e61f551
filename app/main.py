"""校准链失效追溯 API。"""
from __future__ import annotations

import uuid
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, Request
from psycopg import IntegrityError

from . import domain
from .db import close_pool, conn_cursor, init_pool, init_schema
from .schemas import (
    ApiJSONResponse,
    CertificateIn,
    MeasurementIn,
    RevokeIn,
    normalize_ts,
)


@asynccontextmanager
async def lifespan(_: FastAPI):
    init_pool()
    init_schema()
    try:
        yield
    finally:
        close_pool()


app = FastAPI(
    title="校准链失效追溯 API",
    version="1.0.0",
    default_response_class=ApiJSONResponse,
    lifespan=lifespan,
)


class DomainError(Exception):
    def __init__(self, status_code: int, code: str, message: str,
                 details: dict[str, Any] | None = None):
        self.status_code = status_code
        self.code = code
        self.message = message
        self.details = details or {}


@app.exception_handler(DomainError)
async def domain_error_handler(_: Request, exc: DomainError) -> ApiJSONResponse:
    body: dict[str, Any] = {"error": {"code": exc.code, "message": exc.message}}
    if exc.details:
        body["error"]["details"] = exc.details
    return ApiJSONResponse(body, status_code=exc.status_code)


# ---------------------------------------------------------------------------
# 写操作通用：行锁 + expected_revision 条件 + 原子 +1
# ---------------------------------------------------------------------------

def begin_write(cur, expected_revision: int) -> int:
    cur.execute("SELECT revision FROM revision_lock WHERE id = 1 FOR UPDATE")
    current = cur.fetchone()["revision"]
    if current != expected_revision:
        raise DomainError(
            409, "REVISION_CONFLICT",
            f"expected_revision={expected_revision} 已过期，当前修订为 {current}",
            {"expected_revision": expected_revision, "current_revision": current},
        )
    new_revision = current + 1
    cur.execute("UPDATE revision_lock SET revision = %s WHERE id = 1",
                (new_revision,))
    return new_revision


# ---------------------------------------------------------------------------
# 证书登记
# ---------------------------------------------------------------------------

@app.post("/certificates", status_code=201)
def register_certificate(body: CertificateIn) -> dict[str, Any]:
    valid_from = normalize_ts(body.valid_from)
    valid_to = normalize_ts(body.valid_to)
    calibrated_at = normalize_ts(body.calibrated_at)

    if not valid_from < valid_to:
        raise DomainError(422, "INVALID_VALIDITY_WINDOW",
                          "有效期起点必须早于终点（半开区间 [valid_from, valid_to)）")
    if body.tolerance < 0:
        raise DomainError(422, "NEGATIVE_TOLERANCE", "容差必须为非负值")
    if abs(body.error) > body.tolerance:
        raise DomainError(422, "ERROR_EXCEEDS_TOLERANCE",
                          "误差绝对值不得超过容差",
                          {"error": float(body.error),
                           "tolerance": float(body.tolerance)})

    cert_id = uuid.uuid4()
    with conn_cursor(commit=True) as cur:
        new_revision = begin_write(cur, body.expected_revision)

        # 当前时刻（新修订写入前）的全量快照，用于判定上级是否有效
        certs, _ = domain.load_snapshot(cur, new_revision)

        if body.parent_id is not None:
            parent = certs.get(str(body.parent_id))
            if parent is None:
                raise DomainError(422, "PARENT_NOT_FOUND",
                                  "引用的上级证书不存在",
                                  {"parent_id": str(body.parent_id)})
            memo: dict[str, bool] = {}
            if not domain.effective_valid_at(certs, parent, calibrated_at, memo):
                raise DomainError(
                    422, "PARENT_NOT_EFFECTIVE_AT_CALIBRATION",
                    "上级证书在校准时刻无效（已撤销、锚点断裂或不在有效期内）",
                    {"parent_id": str(body.parent_id),
                     "calibrated_at": calibrated_at.isoformat()},
                )

        # 同一器具半开有效期不得重叠
        cur.execute(
            """
            SELECT id::text AS id FROM certificates
            WHERE instrument_id = %s
              AND valid_from < %s AND %s < valid_to
            LIMIT 1
            """,
            (body.instrument_id, valid_to, valid_from),
        )
        overlap = cur.fetchone()
        if overlap is not None:
            raise DomainError(422, "VALIDITY_OVERLAP",
                              "同一器具的证书有效期不得重叠",
                              {"conflicting_certificate_id": overlap["id"]})

        try:
            cur.execute(
                """
                INSERT INTO certificates (
                    id, instrument_id, calibrated_at, valid_from, valid_to,
                    error, tolerance, parent_id, created_revision
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                """,
                (cert_id, body.instrument_id, calibrated_at, valid_from, valid_to,
                 body.error, body.tolerance, body.parent_id, new_revision),
            )
        except IntegrityError as exc:  # 数据库约束兜底
            raise DomainError(422, "CONSTRAINT_VIOLATION", str(exc))

        cur.execute("SELECT * FROM certificates WHERE id = %s", (cert_id,))
        cert = cur.fetchone()

    return {
        "certificate": domain.certificate_view(cert),
        "revision": new_revision,
    }


# ---------------------------------------------------------------------------
# 测量登记
# ---------------------------------------------------------------------------

@app.post("/measurements", status_code=201)
def register_measurement(body: MeasurementIn) -> dict[str, Any]:
    measured_at = normalize_ts(body.measured_at)
    measurement_id = uuid.uuid4()

    with conn_cursor(commit=True) as cur:
        new_revision = begin_write(cur, body.expected_revision)

        certs, _ = domain.load_snapshot(cur, new_revision)
        # 覆盖该时刻的同器具证书（窗口不重叠，最多一条）
        cur.execute(
            """
            SELECT id::text FROM certificates
            WHERE instrument_id = %s
              AND valid_from <= %s AND %s < valid_to
            """,
            (body.instrument_id, measured_at, measured_at),
        )
        rows = cur.fetchall()
        if not rows:
            raise DomainError(422, "NO_COVERING_CERTIFICATE",
                              "测量时刻没有覆盖该器具的证书，登记被拒绝",
                              {"instrument_id": body.instrument_id,
                               "measured_at": measured_at.isoformat()})
        memo: dict[str, bool] = {}
        covering = [certs[r["id"]] for r in rows]
        valid_cover = [c for c in covering
                       if domain.effective_valid_at(certs, c, measured_at, memo)]
        if not valid_cover:
            c = covering[0]
            raise DomainError(
                422, "COVERING_CERTIFICATE_INVALID",
                "覆盖该时刻的证书已失效（被撤销或锚点断裂），登记被拒绝",
                {"certificate_id": c["id"],
                 "failure_chain": domain.failure_chain(certs, c["id"], measured_at)},
            )
        bound = valid_cover[0]

        cur.execute(
            """
            INSERT INTO measurements (
                id, instrument_id, value, measured_at,
                certificate_id, registered_revision
            ) VALUES (%s, %s, %s, %s, %s, %s)
            """,
            (measurement_id, body.instrument_id, body.value, measured_at,
             bound["id"], new_revision),
        )
        view = domain.measurement_view(certs, {
            "id": str(measurement_id),
            "instrument_id": body.instrument_id,
            "value": body.value,
            "measured_at": measured_at,
            "certificate_id": bound["id"],
            "registered_revision": new_revision,
        }, memo)

    return {"measurement": view, "revision": new_revision}


# ---------------------------------------------------------------------------
# 证书撤销
# ---------------------------------------------------------------------------

@app.post("/certificates/{certificate_id}/revoke", status_code=200)
def revoke_certificate(certificate_id: uuid.UUID, body: RevokeIn) -> dict[str, Any]:
    revoked_from = normalize_ts(body.revoked_from)

    with conn_cursor(commit=True) as cur:
        new_revision = begin_write(cur, body.expected_revision)

        cur.execute("SELECT * FROM certificates WHERE id = %s", (certificate_id,))
        cert = cur.fetchone()
        if cert is None:
            raise DomainError(404, "CERTIFICATE_NOT_FOUND",
                              "证书不存在", {"certificate_id": str(certificate_id)})
        if cert["revoked_from"] is not None:
            raise DomainError(
                422, "CERTIFICATE_ALREADY_REVOKED",
                "证书已撤销；撤销是永久性事件，不能重复撤销或修改撤销时刻",
                {"certificate_id": str(certificate_id),
                 "revoked_from": cert["revoked_from"],
                 "revoke_revision": cert["revoke_revision"]},
            )
        if not (cert["valid_from"] <= revoked_from < cert["valid_to"]):
            raise DomainError(
                422, "REVOKE_TIME_OUT_OF_WINDOW",
                "撤销时刻必须落在证书有效期内（[valid_from, valid_to)）",
                {"revoked_from": revoked_from.isoformat(),
                 "valid_from": cert["valid_from"].isoformat(),
                 "valid_to": cert["valid_to"].isoformat()},
            )

        # 撤销前快照与结论（上一修订）
        certs_before, measurements_before = domain.load_snapshot(
            cur, new_revision - 1)
        views_before = {v["id"]: v["valid"]
                        for v in domain.all_measurement_views(
                            certs_before, measurements_before)}

        # 写入撤销（同证书取最新一次）
        cur.execute(
            """
            INSERT INTO revocations (certificate_id, revoked_from, revision)
            VALUES (%s, %s, %s)
            """,
            (certificate_id, revoked_from, new_revision),
        )
        cur.execute(
            """
            UPDATE certificates
            SET revoked_from = %s, revoke_revision = %s
            WHERE id = %s
            """,
            (revoked_from, new_revision, certificate_id),
        )

        # 撤销后快照：找出本次由有效转为无效的测量
        certs_after, measurements_after = domain.load_snapshot(cur, new_revision)
        newly_invalid: list[dict[str, Any]] = []
        memo_after: dict[str, bool] = {}
        for m in measurements_after:
            was_valid = views_before.get(m["id"], False)
            now_view = domain.measurement_view(certs_after, m, memo_after)
            if was_valid and not now_view["valid"]:
                newly_invalid.append(now_view)

    return {
        "certificate_id": str(certificate_id),
        "revoked_from": revoked_from,
        "revision": new_revision,
        "newly_invalid_measurements": newly_invalid,
    }


# ---------------------------------------------------------------------------
# 查询
# ---------------------------------------------------------------------------

def _snapshot_or_422(cur, revision: int | None) -> tuple[
    dict[str, dict[str, Any]], list[dict[str, Any]], int
]:
    cur.execute("SELECT revision FROM revision_lock WHERE id = 1")
    current = cur.fetchone()["revision"]
    if revision is None:
        revision = current
    if revision > current:
        raise DomainError(422, "REVISION_IN_FUTURE",
                          f"修订 {revision} 尚未产生，当前修订为 {current}",
                          {"revision": revision, "current_revision": current})
    certs, measurements = domain.load_snapshot(cur, revision)
    return certs, measurements, revision


@app.get("/revision")
def get_revision() -> dict[str, Any]:
    with conn_cursor() as cur:
        cur.execute("SELECT revision FROM revision_lock WHERE id = 1")
        return {"revision": cur.fetchone()["revision"]}


@app.get("/measurements")
def list_measurements(revision: int | None = None,
                      instrument_id: str | None = None) -> dict[str, Any]:
    with conn_cursor() as cur:
        certs, measurements, rev = _snapshot_or_422(cur, revision)
        views = domain.all_measurement_views(certs, measurements)
    if instrument_id is not None:
        views = [v for v in views if v["instrument_id"] == instrument_id]
    return {"revision": rev, "measurements": views}


@app.get("/measurements/{measurement_id}")
def get_measurement(measurement_id: uuid.UUID,
                    revision: int | None = None) -> dict[str, Any]:
    with conn_cursor() as cur:
        certs, measurements, rev = _snapshot_or_422(cur, revision)
        found = next((m for m in measurements if m["id"] == str(measurement_id)),
                     None)
        if found is None:
            raise DomainError(404, "MEASUREMENT_NOT_FOUND", "测量不存在",
                              {"measurement_id": str(measurement_id)})
        view = domain.measurement_view(certs, found)
    return {"revision": rev, "measurement": view}


@app.get("/certificates")
def list_certificates(revision: int | None = None,
                      instrument_id: str | None = None) -> dict[str, Any]:
    with conn_cursor() as cur:
        certs, _, rev = _snapshot_or_422(cur, revision)
        items = [domain.certificate_view(c) for c in certs.values()]
    if instrument_id is not None:
        items = [c for c in items if c["instrument_id"] == instrument_id]
    return {"revision": rev, "certificates": items}


@app.get("/certificates/{certificate_id}")
def get_certificate(certificate_id: uuid.UUID,
                    revision: int | None = None) -> dict[str, Any]:
    with conn_cursor() as cur:
        certs, _, rev = _snapshot_or_422(cur, revision)
        cert = certs.get(str(certificate_id))
        if cert is None:
            raise DomainError(404, "CERTIFICATE_NOT_FOUND", "证书不存在",
                              {"certificate_id": str(certificate_id)})
        return {"revision": rev, "certificate": domain.certificate_view(cert)}
