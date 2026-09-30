"""校准链领域逻辑（与具体修订无关的纯计算 + 快照装载）。

核心概念
--------
证书具有「失效区间」：

* 直接撤销的证书，自撤销时刻起失效：``[revoked_from, valid_to)``；
* 因上级在其校准时刻无效而递归失效（锚点断裂）的证书，整段有效期都
  不可信：``[valid_from, valid_to)``。

非根证书 c 在其校准时刻锚定到上级 p：仅当 p 在 c.calibrated_at 有效时，
c 的锚点成立。锚点一旦断裂，c 整段有效期失效；进而任何以 c 为锚点的
下级证书也递归失效（沿校准时刻向上传播）。

测量在登记时绑定覆盖其测量时刻的有效证书；查询结论时，只要测量时刻落入
绑定证书的失效区间，该测量即判为无效，并给出从绑定证书到失效根因
（被撤销证书）的失效证书链。

所有计算都基于「某个修订号下的快照」，因此历史修订的结论可被完整复现。
"""
from __future__ import annotations

from typing import Any

from psycopg.cursor import Cursor


def load_snapshot(cur: Cursor, revision: int) -> tuple[
    dict[str, dict[str, Any]], list[dict[str, Any]]
]:
    """装载不晚于 ``revision`` 的证书与测量，构成只读快照。

    撤销信息取不晚于该修订的最近一次撤销。
    """
    cur.execute(
        """
        SELECT c.id::text AS id, c.instrument_id, c.calibrated_at,
               c.valid_from, c.valid_to, c.error, c.tolerance,
               c.parent_id::text AS parent_id, c.created_revision,
               latest.revoked_from, latest.revoke_revision
        FROM certificates c
        LEFT JOIN LATERAL (
            SELECT r.revoked_from, r.revision AS revoke_revision
            FROM revocations r
            WHERE r.certificate_id = c.id AND r.revision <= %s
            ORDER BY r.revision DESC
            LIMIT 1
        ) latest ON TRUE
        WHERE c.created_revision <= %s
        """,
        (revision, revision),
    )
    certs = {row["id"]: row for row in cur.fetchall()}

    cur.execute(
        """
        SELECT id::text AS id, instrument_id, value, measured_at,
               certificate_id::text AS certificate_id, registered_revision
        FROM measurements
        WHERE registered_revision <= %s
        ORDER BY registered_revision, id
        """,
        (revision,),
    )
    measurements = cur.fetchall()
    return certs, measurements


def broken_at_calibration(
    certs: dict[str, dict[str, Any]], cid: str,
    memo: dict[str, bool] | None = None,
) -> bool:
    """证书的锚点是否断裂：其校准时刻上级证书无效。

    沿上级链向上判定。父证书必早于子证书创建，parent 边构成森林，无环。
    不变量：登记时保证过 c.calibrated_at 落在 p 窗口内，且窗口永不变更，
    因此 p 一旦锚点断裂（整段失效），必然在 c.calibrated_at 处失效。
    """
    if memo is None:
        memo = {}
    if cid in memo:
        return memo[cid]

    c = certs[cid]
    pid = c["parent_id"]
    if pid is None:
        memo[cid] = False
        return False

    anchor_t = c["calibrated_at"]
    p = certs[pid]
    if not (p["valid_from"] <= anchor_t < p["valid_to"]):
        memo[cid] = True
        return True
    if p["revoked_from"] is not None and p["revoked_from"] <= anchor_t:
        memo[cid] = True
        return True
    if p["parent_id"] is not None and broken_at_calibration(certs, pid, memo):
        memo[cid] = True
        return True

    memo[cid] = False
    return False


def effective_valid_at(
    certs: dict[str, dict[str, Any]], cert: dict[str, Any], t: Any,
    memo: dict[str, bool] | None = None,
) -> bool:
    """证书在时刻 t 是否有效：窗口内、未撤销、锚点未断裂。"""
    if not (cert["valid_from"] <= t < cert["valid_to"]):
        return False
    if cert["revoked_from"] is not None and cert["revoked_from"] <= t:
        return False
    if cert["parent_id"] is not None and broken_at_calibration(
        certs, cert["id"], memo
    ):
        return False
    return True


def failure_interval(
    certs: dict[str, dict[str, Any]], cid: str, memo: dict[str, bool]
):
    """证书当前的失效区间 (start, end)；未失效返回 None。"""
    cert = certs[cid]
    if cert["parent_id"] is not None and broken_at_calibration(certs, cid, memo):
        return cert["valid_from"], cert["valid_to"]
    if cert["revoked_from"] is not None:
        return cert["revoked_from"], cert["valid_to"]
    return None


def failure_chain(
    certs: dict[str, dict[str, Any]], bound_id: str, t: Any,
) -> list[dict[str, Any]]:
    """构造测量时刻 t 的失效证书链：绑定证书 → … → 被撤销的根因证书。

    在测量已判定无效的前提下调用。每向上走一层，判断时刻切换为下级证书
    的校准时刻——那正是下级锚定上级、受上级失效影响的时刻。
    """
    chain: list[dict[str, Any]] = []
    cur_id: str | None = bound_id
    cur_t = t
    while cur_id is not None:
        cur = certs[cur_id]
        chain.append(_cert_chain_entry(cur))
        # 自身撤销覆盖当前时刻 => 到达失效根因
        if cur["revoked_from"] is not None and cur["revoked_from"] <= cur_t:
            return chain
        # 否则必是锚点断裂，继续向上级追责
        pid = cur["parent_id"]
        if pid is None:
            return chain  # 理论不可达：根证书只能因撤销而失效
        cur_t = cur["calibrated_at"]
        cur_id = pid
    return chain


def _cert_chain_entry(cert: dict[str, Any]) -> dict[str, Any]:
    return {
        "certificate_id": cert["id"],
        "instrument_id": cert["instrument_id"],
        "calibrated_at": cert["calibrated_at"],
        "valid_from": cert["valid_from"],
        "valid_to": cert["valid_to"],
        "revoked_from": cert["revoked_from"],
    }


def measurement_view(
    certs: dict[str, dict[str, Any]], m: dict[str, Any],
    memo: dict[str, bool] | None = None,
) -> dict[str, Any]:
    """单个测量在给定快照下的结论。"""
    cert = certs[m["certificate_id"]]
    valid = effective_valid_at(certs, cert, m["measured_at"], memo)
    return {
        "id": m["id"],
        "instrument_id": m["instrument_id"],
        "value": float(m["value"]),
        "measured_at": m["measured_at"],
        "certificate_id": m["certificate_id"],
        "registered_revision": m["registered_revision"],
        "valid": valid,
        "failure_chain": None if valid else failure_chain(
            certs, m["certificate_id"], m["measured_at"]
        ),
    }


def all_measurement_views(
    certs: dict[str, dict[str, Any]],
    measurements: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    memo: dict[str, bool] = {}
    return [measurement_view(certs, m, memo) for m in measurements]


def certificate_view(cert: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": cert["id"],
        "instrument_id": cert["instrument_id"],
        "calibrated_at": cert["calibrated_at"],
        "valid_from": cert["valid_from"],
        "valid_to": cert["valid_to"],
        "error": float(cert["error"]),
        "tolerance": float(cert["tolerance"]),
        "parent_id": cert["parent_id"],
        "is_root": cert["parent_id"] is None,
        "created_revision": cert["created_revision"],
        "revoked_from": cert["revoked_from"],
        "revoke_revision": cert["revoke_revision"],
    }
