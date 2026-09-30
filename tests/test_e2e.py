"""端到端场景测试：证书链、测量绑定、撤销级联、修订并发与历史查询。

直接对真实 FastAPI + PostgreSQL 发起 HTTP 请求。
"""
import json
import os
import sys
import time
import uuid

import httpx

BASE = os.environ.get("API_BASE", "http://127.0.0.1:8000")
c = httpx.Client(base_url=BASE, timeout=10)

PASS, FAIL = 0, 0


def check(name, cond, extra=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  PASS {name}")
    else:
        FAIL += 1
        print(f"  FAIL {name} {extra}")


def expect_error(name, resp, status, code):
    check(name, resp.status_code == status and
          resp.json()["error"]["code"] == code,
          f"got {resp.status_code} {resp.text[:200]}")


# ---------------------------------------------------------------- 根证书 R
r = c.post("/certificates", json={
    "instrument_id": "GAUGE-R",
    "calibrated_at": "2026-01-01T08:00:00Z",
    "valid_from": "2026-01-01T08:00:00Z",
    "valid_to": "2026-12-31T08:00:00Z",
    "error": 0.01, "tolerance": 0.05,
    "expected_revision": 0,
})
check("register root R -> 201 rev1", r.status_code == 201 and
      r.json()["revision"] == 1, r.text)
RID = r.json()["certificate"]["id"]

# 业务校验
def expect_code(resp):
    return resp.json()["error"]["code"] if resp.status_code in (404, 409, 422) else None


check("valid_from >= valid_to", expect_code(c.post("/certificates", json={
    "instrument_id": "X", "calibrated_at": "2026-01-01T00:00:00Z",
    "valid_from": "2026-02-01T00:00:00Z", "valid_to": "2026-01-01T00:00:00Z",
    "error": 0, "tolerance": 0, "expected_revision": 1})) == "INVALID_VALIDITY_WINDOW")
check("negative tolerance", expect_code(c.post("/certificates", json={
    "instrument_id": "X", "calibrated_at": "2026-01-01T00:00:00Z",
    "valid_from": "2026-01-01T00:00:00Z", "valid_to": "2026-02-01T00:00:00Z",
    "error": 0, "tolerance": -1, "expected_revision": 1})) == "NEGATIVE_TOLERANCE")
check("error exceeds tolerance", expect_code(c.post("/certificates", json={
    "instrument_id": "X", "calibrated_at": "2026-01-01T00:00:00Z",
    "valid_from": "2026-01-01T00:00:00Z", "valid_to": "2026-02-01T00:00:00Z",
    "error": 0.9, "tolerance": 0.1, "expected_revision": 1})) ==
      "ERROR_EXCEEDS_TOLERANCE")


# 重叠窗口（同器具）
overlap = c.post("/certificates", json={
    "instrument_id": "GAUGE-R",
    "calibrated_at": "2026-03-01T00:00:00Z",
    "valid_from": "2026-03-01T00:00:00Z",
    "valid_to": "2026-04-01T00:00:00Z",
    "error": 0, "tolerance": 0.05, "expected_revision": 1,
})
expect_error("overlap rejected", overlap, 422, "VALIDITY_OVERLAP")

# 紧贴窗口（半开）允许
adj = c.post("/certificates", json={
    "instrument_id": "GAUGE-R2",
    "calibrated_at": "2027-01-01T00:00:00Z",
    "valid_from": "2026-12-31T08:00:00Z",
    "valid_to": "2027-03-01T00:00:00Z",
    "error": 0, "tolerance": 0.05, "expected_revision": 1,
})
check("adjacent half-open window allowed", adj.status_code == 201, adj.text)

# 过期 expected_revision -> 409
stale = c.post("/certificates", json={
    "instrument_id": "STALE",
    "calibrated_at": "2026-01-01T00:00:00Z",
    "valid_from": "2026-01-01T00:00:00Z", "valid_to": "2026-06-01T00:00:00Z",
    "error": 0, "tolerance": 0.05, "expected_revision": 0,
})
expect_error("stale revision -> 409", stale, 409, "REVISION_CONFLICT")

# ---------------------------------------------------------------- 子证书 A
a = c.post("/certificates", json={
    "instrument_id": "GAUGE-A",
    "calibrated_at": "2026-02-01T00:00:00Z",
    "valid_from": "2026-02-01T00:00:00Z",
    "valid_to": "2026-08-01T00:00:00Z",
    "error": -0.02, "tolerance": 0.05,
    "parent_id": RID, "expected_revision": 2,
})
check("register child A -> rev3", a.status_code == 201 and
      a.json()["revision"] == 3, a.text)
AID = a.json()["certificate"]["id"]

# 上级在校准时刻已撤销的场景：先用临时证书链构造
# 孙证书 B（校准时刻 2026-03-01，锚定 A）
b = c.post("/certificates", json={
    "instrument_id": "GAUGE-B",
    "calibrated_at": "2026-03-01T00:00:00Z",
    "valid_from": "2026-03-01T00:00:00Z",
    "valid_to": "2026-09-01T00:00:00Z",
    "error": 0.03, "tolerance": 0.1,
    "parent_id": AID, "expected_revision": 3,
})
check("register grandchild B -> rev4", b.status_code == 201 and
      b.json()["revision"] == 4, b.text)
BID = b.json()["certificate"]["id"]

# 不存在的上级
np = c.post("/certificates", json={
    "instrument_id": "X", "calibrated_at": "2026-02-01T00:00:00Z",
    "valid_from": "2026-02-01T00:00:00Z", "valid_to": "2026-08-01T00:00:00Z",
    "error": 0, "tolerance": 0.05,
    "parent_id": str(uuid.uuid4()), "expected_revision": 4,
})
expect_error("parent not found", np, 422, "PARENT_NOT_FOUND")

# ---------------------------------------------------------------- 测量
m_before = c.post("/measurements", json={
    "instrument_id": "GAUGE-A", "value": 1.23,
    "measured_at": "2026-04-01T00:00:00Z", "expected_revision": 4,
})
check("measurement on A -> valid rev5", m_before.status_code == 201 and
      m_before.json()["measurement"]["valid"] and
      m_before.json()["measurement"]["certificate_id"] == AID, m_before.text)
M_A = m_before.json()["measurement"]["id"]

m_b = c.post("/measurements", json={
    "instrument_id": "GAUGE-B", "value": 2.5,
    "measured_at": "2026-04-15T00:00:00Z", "expected_revision": 5,
})
check("measurement on B -> valid rev6", m_b.status_code == 201 and
      m_b.json()["measurement"]["valid"], m_b.text)
M_B = m_b.json()["measurement"]["id"]

m_r_early = c.post("/measurements", json={
    "instrument_id": "GAUGE-R", "value": 9.9,
    "measured_at": "2026-02-15T00:00:00Z", "expected_revision": 6,
})
check("measurement on R early -> valid rev7", m_r_early.status_code == 201 and
      m_r_early.json()["measurement"]["valid"], m_r_early.text)
M_R_EARLY = m_r_early.json()["measurement"]["id"]

m_r_late = c.post("/measurements", json={
    "instrument_id": "GAUGE-R", "value": 9.8,
    "measured_at": "2026-07-15T00:00:00Z", "expected_revision": 7,
})
check("measurement on R late -> valid rev8", m_r_late.status_code == 201 and
      m_r_late.json()["measurement"]["valid"], m_r_late.text)
M_R_LATE = m_r_late.json()["measurement"]["id"]

# 无证书覆盖
no_cov = c.post("/measurements", json={
    "instrument_id": "UNREGISTERED", "value": 1.0,
    "measured_at": "2026-04-01T00:00:00Z", "expected_revision": 8,
})
expect_error("no covering cert", no_cov, 422, "NO_COVERING_CERTIFICATE")

# ---------------------------------------------------------------- 撤销 R
rev = c.post(f"/certificates/{RID}/revoke", json={
    "revoked_from": "2026-06-01T00:00:00Z", "expected_revision": 8,
})
check("revoke R -> rev9", rev.status_code == 200 and
      rev.json()["revision"] == 9, rev.text)
newly = {m["id"]: m for m in rev.json()["newly_invalid_measurements"]}
print("    newly invalid:", sorted(newly))
check("R-late invalidated directly", M_R_LATE in newly)
check("R-early NOT invalidated (before revoke)", M_R_EARLY not in newly)
check("A measurement NOT invalidated (A anchor valid at its calibration)",
      M_A not in newly)
check("B measurement NOT invalidated", M_B not in newly)
# 本次 A/B 并未失效，newly_invalid 只含 R 上的测量
check("newly_invalid count == 1", len(newly) == 1)

# 撤销时刻越界（使用未撤销的 A 来单独验证窗口校验）
bad_revoke = c.post(f"/certificates/{AID}/revoke", json={
    "revoked_from": "2026-12-31T09:00:00Z", "expected_revision": 9,
})
expect_error("revoke out of window", bad_revoke, 422,
             "REVOKE_TIME_OUT_OF_WINDOW")

# R 已撤销，不能重复撤销
dup_revoke = c.post(f"/certificates/{RID}/revoke", json={
    "revoked_from": "2026-05-01T00:00:00Z", "expected_revision": 9,
})
expect_error("duplicate revoke rejected", dup_revoke, 422,
             "CERTIFICATE_ALREADY_REVOKED")

# 撤销不存在
nf = c.post(f"/certificates/{uuid.uuid4()}/revoke", json={
    "revoked_from": "2026-06-01T00:00:00Z", "expected_revision": 9,
})
expect_error("revoke missing cert -> 404", nf, 404, "CERTIFICATE_NOT_FOUND")

# 历史结论保留：rev=8 时 R-late 仍有效
hist = c.get(f"/measurements/{M_R_LATE}?revision=8")
check("history rev8 R-late valid", hist.json()["measurement"]["valid"] is True)
cur_view = c.get(f"/measurements/{M_R_LATE}")
check("current R-late invalid", cur_view.json()["measurement"]["valid"] is False)
chain = cur_view.json()["measurement"]["failure_chain"]
check("R-late failure chain length 1", len(chain) == 1 and
      chain[0]["certificate_id"] == RID)
# rev=9 之前不存在的修订
future = c.get("/measurements?revision=999")
expect_error("revision in future", future, 422, "REVISION_IN_FUTURE")

# ---------------------------------------------------------------- 级联失效
# 撤销后创建 C：校准时刻 2026-07-01（落在 R 失效区间），锚定 R -> 直接锚点断裂
cc = c.post("/certificates", json={
    "instrument_id": "GAUGE-C",
    "calibrated_at": "2026-07-01T00:00:00Z",
    "valid_from": "2026-07-01T00:00:00Z",
    "valid_to": "2026-11-01T00:00:00Z",
    "error": 0, "tolerance": 0.1,
    "parent_id": RID, "expected_revision": 9,
})
expect_error("parent revoked at calibration -> rejected", cc, 422,
             "PARENT_NOT_EFFECTIVE_AT_CALIBRATION")

# 撤销 A：revoked_from=2026-06-15。A 校准时刻 02-01 锚点（R 在 02-01 有效），
# A 自 06-15 失效；B 校准时刻 03-01 < 06-15，锚点有效，B 不递归失效。
# 但撤销前 A 测量 04-01 仍有效，撤销后保持有效；B 测量 04-15 仍有效。
# 为制造级联，再造一条在 A 失效后锚定 A 的链：撤销 A 之前不可能登记。
# 转而验证孙级递归：撤销 R 改为更早（2026-02-15）会让 A、B 都断裂——
# 用新链验证：S(root) -> D(cal 03-01) -> E(cal 05-01)
s = c.post("/certificates", json={
    "instrument_id": "GAUGE-S",
    "calibrated_at": "2026-01-01T00:00:00Z",
    "valid_from": "2026-01-01T00:00:00Z",
    "valid_to": "2027-01-01T00:00:00Z",
    "error": 0, "tolerance": 0.05, "expected_revision": 9,
})
SID = s.json()["certificate"]["id"]
d = c.post("/certificates", json={
    "instrument_id": "GAUGE-D",
    "calibrated_at": "2026-03-01T00:00:00Z",
    "valid_from": "2026-03-01T00:00:00Z",
    "valid_to": "2026-10-01T00:00:00Z",
    "error": 0, "tolerance": 0.05, "parent_id": SID,
    "expected_revision": 10,
})
DID = d.json()["certificate"]["id"]
e = c.post("/certificates", json={
    "instrument_id": "GAUGE-E",
    "calibrated_at": "2026-05-01T00:00:00Z",
    "valid_from": "2026-05-01T00:00:00Z",
    "valid_to": "2026-11-01T00:00:00Z",
    "error": 0, "tolerance": 0.05, "parent_id": DID,
    "expected_revision": 11,
})
EID = e.json()["certificate"]["id"]
check("chain S->D->E registered", s.status_code == d.status_code ==
      e.status_code == 201)

m_e = c.post("/measurements", json={
    "instrument_id": "GAUGE-E", "value": 7.7,
    "measured_at": "2026-06-10T00:00:00Z", "expected_revision": 12,
})
M_E = m_e.json()["measurement"]["id"]
check("measurement E valid rev13", m_e.status_code == 201 and
      m_e.json()["measurement"]["valid"])

# 撤销 S 于 2026-02-15（早于 D 校准 03-01）：
#   S 直接失效；D 校准时刻 03-01 落入 S 失效区间 -> D 锚点断裂、整段失效；
#   E 锚点 D 在 05-01 无效 -> E 递归失效；测量 E 重判为无效，链为 E->D->S。
rev2 = c.post(f"/certificates/{SID}/revoke", json={
    "revoked_from": "2026-02-15T00:00:00Z", "expected_revision": 13,
})
check("revoke S -> rev14", rev2.status_code == 200 and
      rev2.json()["revision"] == 14, rev2.text)
newly2 = {m["id"]: m for m in rev2.json()["newly_invalid_measurements"]}
check("E measurement cascaded invalid", M_E in newly2)
if M_E in newly2:
    ch = [x["certificate_id"] for x in newly2[M_E]["failure_chain"]]
    check("failure chain E->D->S", ch == [EID, DID, SID], str(ch))

# A、B 不受 S 链撤销影响（A 校准 02-01 < S 撤销点，但 A 锚的是 R，与 S 无关）
a_now = c.get(f"/measurements/{M_A}").json()["measurement"]
b_now = c.get(f"/measurements/{M_B}").json()["measurement"]
check("A still valid (R valid at 02-01)", a_now["valid"] is True)
check("B still valid (A valid at 03-01)", b_now["valid"] is True)

# 失效证书覆盖时拒绝新测量
refuse = c.post("/measurements", json={
    "instrument_id": "GAUGE-E", "value": 1,
    "measured_at": "2026-07-01T00:00:00Z", "expected_revision": 14,
})
expect_error("register under broken cert refused", refuse, 422,
             "COVERING_CERTIFICATE_INVALID")
if refuse.status_code == 422:
    ch = [x["certificate_id"]
          for x in refuse.json()["error"]["details"]["failure_chain"]]
    check("refusal details chain E->D->S", ch == [EID, DID, SID], str(ch))

# 列表按修订过滤
lst_now = c.get("/measurements").json()
lst_hist = c.get("/measurements?revision=13").json()
now_validity = {m["id"]: m["valid"] for m in lst_now["measurements"]}
hist_validity = {m["id"]: m["valid"] for m in lst_hist["measurements"]}
check("list current: E invalid", now_validity[M_E] is False)
check("list rev13: E valid (history preserved)", hist_validity[M_E] is True)
check("list rev13 has 5 measurements", len(lst_hist["measurements"]) == 5)
check("list current has 5 measurements", len(lst_now["measurements"]) == 5)

# ---------------------------------------------------------------- 并发写
import concurrent.futures as cf


def register_iso(expected):
    return c.post("/certificates", json={
        "instrument_id": f"PAR-{expected}-{time.time_ns()}",
        "calibrated_at": "2026-01-01T00:00:00Z",
        "valid_from": "2026-01-01T00:00:00Z",
        "valid_to": "2026-06-01T00:00:00Z",
        "error": 0, "tolerance": 0.05, "expected_revision": expected,
    })


rev_now = c.get("/revision").json()["revision"]
with cf.ThreadPoolExecutor(5) as ex:
    results = list(ex.map(lambda _: register_iso(rev_now), range(5)))
codes = [r.status_code for r in results]
check("concurrent writes: exactly one 201", codes.count(201) == 1, str(codes))
check("concurrent writes: four 409", codes.count(409) == 4, str(codes))

print(f"\n{PASS} passed, {FAIL} failed")
sys.exit(1 if FAIL else 0)
