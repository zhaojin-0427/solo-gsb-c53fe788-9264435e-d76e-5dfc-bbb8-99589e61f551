# 校准链失效追溯 API

基于 **Python + FastAPI + PostgreSQL** 的校准证书链与测量结论追溯服务。

它支持：

- 登记器具校准证书：校准时刻、半开有效期 `[valid_from, valid_to)`、误差、非负容差；
- 根证书作为信任锚；非根证书必须引用一个在其校准时刻有效的上级证书；
- 同一器具的证书半开有效期不得重叠；
- 所有证书必须满足 `abs(error) <= tolerance`；
- 登记测量时，自动绑定覆盖测量时刻且当前有效的证书；无证书或证书失效则拒绝；
- 证书可从其有效期内的指定时刻起撤销；
- 撤销后既有测量自动重判：
  - 撤销时刻之前的测量保持有效；
  - 直接落入被撤销证书失效区间的测量失效；
  - 子证书仅当其**校准时刻**落入上级失效区间时递归失效；
  - 撤销响应列出本次转为无效的测量及其失效证书链；
- 所有写请求必须携带 `expected_revision`，在单个数据库事务中原子推进全局修订号；修订过期返回 `409`；
- 查询可带任意历史修订号，完整保留该修订下的测量结论。

## 一键启动

要求已安装 Docker 与 Docker Compose v2：

```bash
docker compose up --build
```

启动后访问：

- API 服务：<http://localhost:8000>
- OpenAPI/Swagger UI：<http://localhost:8000/docs>
- OpenAPI JSON：<http://localhost:8000/openapi.json>
- 当前修订号：<http://localhost:8000/revision>
- PostgreSQL：`localhost:5432`，数据库 `calibration`，用户/密码 `calib/calib`

停止服务：

```bash
docker compose down
```

如需同时删除数据库数据卷：

```bash
docker compose down -v
```

## API 概览

| 方法 | 路径 | 说明 |
|---|---|---|
| `GET` | `/revision` | 查询当前全局修订号 |
| `POST` | `/certificates` | 登记根证书或子证书 |
| `GET` | `/certificates` | 按修订查询证书列表，可按 `instrument_id` 过滤 |
| `GET` | `/certificates/{id}` | 按修订查询单个证书 |
| `POST` | `/certificates/{id}/revoke` | 从指定时刻起撤销证书 |
| `POST` | `/measurements` | 登记测量并绑定有效覆盖证书 |
| `GET` | `/measurements` | 按修订查询测量结论，可按 `instrument_id` 过滤 |
| `GET` | `/measurements/{id}` | 按修订查询单个测量结论 |

查询参数：

- `revision`：可选。省略表示当前修订；例如 `/measurements?revision=6`。
- `instrument_id`：可选，仅列表接口支持。

所有时间均使用 ISO 8601；不带时区的时间按 UTC 处理，建议使用 `Z`，如 `2026-02-15T00:00:00Z`。

## 业务规则

### 证书

- `valid_from < valid_to`，有效期是半开区间 `[valid_from, valid_to)`；
- `tolerance >= 0`；
- `abs(error) <= tolerance`；
- 根证书不传 `parent_id`，是信任锚；
- 非根证书必须传 `parent_id`，且上级证书必须在子证书的 `calibrated_at` 时刻有效：
  - 校准时刻落在上级半开有效期内；
  - 上级在该时刻未被撤销；
  - 上级自身的锚点未断裂；
- 同一 `instrument_id` 下证书有效期不得重叠；半开区间允许首尾相接：前一张的 `valid_to` 可以等于后一张的 `valid_from`。

说明：证书的校准时刻描述它何时被校准，并不要求落在证书自身有效期内；非根证书只要求该时刻锚定到有效的上级证书。

### 测量

登记测量：

```json
{
  "instrument_id": "GAUGE-003",
  "value": 4.21,
  "measured_at": "2026-05-10T00:00:00Z",
  "expected_revision": 4
}
```

服务在同一事务内查找满足以下条件的证书：

1. `instrument_id` 相同；
2. `valid_from <= measured_at < valid_to`；
3. 在测量时刻有效（未撤销，且沿证书链向上的锚点均有效）。

找不到覆盖证书返回 `422 / NO_COVERING_CERTIFICATE`；覆盖证书存在但已失效返回 `422 / COVERING_CERTIFICATE_INVALID`。

### 撤销与递归失效

撤销请求：

```json
{
  "revoked_from": "2026-02-15T00:00:00Z",
  "expected_revision": 6
}
```

`revoked_from` 必须满足 `valid_from <= revoked_from < valid_to`。撤销是永久性事实，同一证书只能撤销一次；重复撤销返回 `422 / CERTIFICATE_ALREADY_REVOKED`。

证书失效分两类：

- 被直接撤销：失效区间为 `[revoked_from, valid_to)`；
- 锚点断裂的子证书：若子证书的校准时刻落入上级失效区间，则子证书整段 `[valid_from, valid_to)` 不可信，并继续向更深层子证书传播。

撤销接口会比较撤销前后两个修订的全部测量结论，只返回本次新转为无效的测量。每条无效测量包含 `failure_chain`，顺序为：

```text
测量绑定证书 -> 中间子证书 -> ... -> 被撤销的根因证书
```

### 全局修订与并发控制

- 初始修订号为 `0`；
- 每个成功的写请求（登记证书、登记测量、撤销证书）都在事务内把全局修订号加 `1`；
- 写请求体必须携带 `expected_revision`；
- 服务先取得事务级全局锁，再比较修订号：
  - 相等：执行写入并提交新修订；
  - 不相等：回滚并返回 `409 / REVISION_CONFLICT`；
- 校验失败（422/404）不会推进修订号；
- 数据行带有创建/登记/撤销修订；查询某修订时，只装载该修订及之前的数据，因此历史结论不会被后续撤销改变。

## 实际请求与响应样例

以下样例来自一次全新数据库上的真实端到端调用。服务端生成的 UUID 每次会不同；在你自己的实例中执行时，请把路径和后续请求里的 UUID、`expected_revision` 替换为前一步响应中的实际值。

### 1. 查询当前修订

请求：

```http
GET /revision
```

响应：

```json
{
  "revision": 0
}
```

### 2. 登记根证书 R

请求：

```http
POST /certificates
Content-Type: application/json
```

```json
{
  "instrument_id": "GAUGE-001",
  "calibrated_at": "2026-01-01T08:00:00Z",
  "valid_from": "2026-01-01T08:00:00Z",
  "valid_to": "2026-12-31T08:00:00Z",
  "error": 0.01,
  "tolerance": 0.05,
  "expected_revision": 0
}
```

响应 `201`：

```json
{
  "certificate": {
    "id": "2f2d7af5-653b-4615-829b-20b8894f64af",
    "instrument_id": "GAUGE-001",
    "calibrated_at": "2026-01-01T08:00:00Z",
    "valid_from": "2026-01-01T08:00:00Z",
    "valid_to": "2026-12-31T08:00:00Z",
    "error": 0.01,
    "tolerance": 0.05,
    "parent_id": null,
    "is_root": true,
    "created_revision": 1,
    "revoked_from": null,
    "revoke_revision": null
  },
  "revision": 1
}
```

### 3. 过期修订返回 409

仍使用 `expected_revision: 0` 发起下一次写请求：

```json
{
  "instrument_id": "GAUGE-STALE",
  "calibrated_at": "2026-01-01T00:00:00Z",
  "valid_from": "2026-01-01T00:00:00Z",
  "valid_to": "2026-12-31T08:00:00Z",
  "error": 0.01,
  "tolerance": 0.05,
  "expected_revision": 0
}
```

响应 `409`：

```json
{
  "error": {
    "code": "REVISION_CONFLICT",
    "message": "expected_revision=0 已过期，当前修订为 1",
    "details": {
      "expected_revision": 0,
      "current_revision": 1
    }
  }
}
```

### 4. 登记子证书 A

请求：

```json
{
  "instrument_id": "GAUGE-002",
  "calibrated_at": "2026-03-01T00:00:00Z",
  "valid_from": "2026-03-01T00:00:00Z",
  "valid_to": "2026-10-01T00:00:00Z",
  "error": -0.02,
  "tolerance": 0.05,
  "parent_id": "2f2d7af5-653b-4615-829b-20b8894f64af",
  "expected_revision": 1
}
```

响应 `201`：

```json
{
  "certificate": {
    "id": "9c50ded3-59e4-4a16-b2ce-1a5207f0cd41",
    "instrument_id": "GAUGE-002",
    "calibrated_at": "2026-03-01T00:00:00Z",
    "valid_from": "2026-03-01T00:00:00Z",
    "valid_to": "2026-10-01T00:00:00Z",
    "error": -0.02,
    "tolerance": 0.05,
    "parent_id": "2f2d7af5-653b-4615-829b-20b8894f64af",
    "is_root": false,
    "created_revision": 2,
    "revoked_from": null,
    "revoke_revision": null
  },
  "revision": 2
}
```

### 5. 登记孙证书 B

请求：

```json
{
  "instrument_id": "GAUGE-003",
  "calibrated_at": "2026-05-01T00:00:00Z",
  "valid_from": "2026-05-01T00:00:00Z",
  "valid_to": "2026-11-01T00:00:00Z",
  "error": 0.03,
  "tolerance": 0.1,
  "parent_id": "9c50ded3-59e4-4a16-b2ce-1a5207f0cd41",
  "expected_revision": 2
}
```

响应 `201`：

```json
{
  "certificate": {
    "id": "001b8e7e-6978-4339-b428-654f3e97de6f",
    "instrument_id": "GAUGE-003",
    "calibrated_at": "2026-05-01T00:00:00Z",
    "valid_from": "2026-05-01T00:00:00Z",
    "valid_to": "2026-11-01T00:00:00Z",
    "error": 0.03,
    "tolerance": 0.1,
    "parent_id": "9c50ded3-59e4-4a16-b2ce-1a5207f0cd41",
    "is_root": false,
    "created_revision": 3,
    "revoked_from": null,
    "revoke_revision": null
  },
  "revision": 3
}
```

### 6. 登记撤销点之前的测量 M0

请求：

```json
{
  "instrument_id": "GAUGE-001",
  "value": 10.0,
  "measured_at": "2026-02-01T00:00:00Z",
  "expected_revision": 3
}
```

响应 `201`：

```json
{
  "measurement": {
    "id": "f858bb28-ec1d-4af8-af78-b26a27d1094e",
    "instrument_id": "GAUGE-001",
    "value": 10.0,
    "measured_at": "2026-02-01T00:00:00Z",
    "certificate_id": "2f2d7af5-653b-4615-829b-20b8894f64af",
    "registered_revision": 4,
    "valid": true,
    "failure_chain": null
  },
  "revision": 4
}
```

### 7. 登记绑定孙证书 B 的测量 M1

请求：

```json
{
  "instrument_id": "GAUGE-003",
  "value": 4.21,
  "measured_at": "2026-05-10T00:00:00Z",
  "expected_revision": 4
}
```

响应 `201`：

```json
{
  "measurement": {
    "id": "8f4ad26e-27ed-423d-bde4-fae87a4f2dc3",
    "instrument_id": "GAUGE-003",
    "value": 4.21,
    "measured_at": "2026-05-10T00:00:00Z",
    "certificate_id": "001b8e7e-6978-4339-b428-654f3e97de6f",
    "registered_revision": 5,
    "valid": true,
    "failure_chain": null
  },
  "revision": 5
}
```

### 8. 登记撤销点之后的测量 M2

请求：

```json
{
  "instrument_id": "GAUGE-001",
  "value": 10.02,
  "measured_at": "2026-07-20T00:00:00Z",
  "expected_revision": 5
}
```

响应 `201`（撤销尚未发生，因此登记时有效）：

```json
{
  "measurement": {
    "id": "adb1a130-a3eb-481a-a61d-0dc8bbbe685e",
    "instrument_id": "GAUGE-001",
    "value": 10.02,
    "measured_at": "2026-07-20T00:00:00Z",
    "certificate_id": "2f2d7af5-653b-4615-829b-20b8894f64af",
    "registered_revision": 6,
    "valid": true,
    "failure_chain": null
  },
  "revision": 6
}
```

### 9. 无覆盖证书则拒绝

请求：

```json
{
  "instrument_id": "GAUGE-UNKNOWN",
  "value": 1.0,
  "measured_at": "2026-05-10T00:00:00Z",
  "expected_revision": 6
}
```

响应 `422`：

```json
{
  "error": {
    "code": "NO_COVERING_CERTIFICATE",
    "message": "测量时刻没有覆盖该器具的证书，登记被拒绝",
    "details": {
      "instrument_id": "GAUGE-UNKNOWN",
      "measured_at": "2026-05-10T00:00:00+00:00"
    }
  }
}
```

### 10. 按历史修订 6 查询 M1（撤销前仍有效）

请求：

```http
GET /measurements/8f4ad26e-27ed-423d-bde4-fae87a4f2dc3?revision=6
```

响应：

```json
{
  "revision": 6,
  "measurement": {
    "id": "8f4ad26e-27ed-423d-bde4-fae87a4f2dc3",
    "instrument_id": "GAUGE-003",
    "value": 4.21,
    "measured_at": "2026-05-10T00:00:00Z",
    "certificate_id": "001b8e7e-6978-4339-b428-654f3e97de6f",
    "registered_revision": 5,
    "valid": true,
    "failure_chain": null
  }
}
```

### 11. 撤销根证书 R

R 自 `2026-02-15T00:00:00Z` 起失效。由于：

- A 的校准时刻是 `2026-03-01`，落入 R 的失效区间，所以 A 锚点断裂、整段失效；
- B 的校准时刻是 `2026-05-01`，此时 A 已不可信，所以 B 递归失效；
- M1 测量时刻为 `2026-05-10`，随 B 失效，链为 `B -> A -> R`；
- M2 测量时刻为 `2026-07-20`，直接落入 R 的撤销失效区间；
- M0 测量时刻为 `2026-02-01`，早于撤销时刻，仍有效。

请求：

```http
POST /certificates/2f2d7af5-653b-4615-829b-20b8894f64af/revoke
```

```json
{
  "revoked_from": "2026-02-15T00:00:00Z",
  "expected_revision": 6
}
```

响应 `200`：

```json
{
  "certificate_id": "2f2d7af5-653b-4615-829b-20b8894f64af",
  "revoked_from": "2026-02-15T00:00:00Z",
  "revision": 7,
  "newly_invalid_measurements": [
    {
      "id": "8f4ad26e-27ed-423d-bde4-fae87a4f2dc3",
      "instrument_id": "GAUGE-003",
      "value": 4.21,
      "measured_at": "2026-05-10T00:00:00Z",
      "certificate_id": "001b8e7e-6978-4339-b428-654f3e97de6f",
      "registered_revision": 5,
      "valid": false,
      "failure_chain": [
        {
          "certificate_id": "001b8e7e-6978-4339-b428-654f3e97de6f",
          "instrument_id": "GAUGE-003",
          "calibrated_at": "2026-05-01T00:00:00Z",
          "valid_from": "2026-05-01T00:00:00Z",
          "valid_to": "2026-11-01T00:00:00Z",
          "revoked_from": null
        },
        {
          "certificate_id": "9c50ded3-59e4-4a16-b2ce-1a5207f0cd41",
          "instrument_id": "GAUGE-002",
          "calibrated_at": "2026-03-01T00:00:00Z",
          "valid_from": "2026-03-01T00:00:00Z",
          "valid_to": "2026-10-01T00:00:00Z",
          "revoked_from": null
        },
        {
          "certificate_id": "2f2d7af5-653b-4615-829b-20b8894f64af",
          "instrument_id": "GAUGE-001",
          "calibrated_at": "2026-01-01T08:00:00Z",
          "valid_from": "2026-01-01T08:00:00Z",
          "valid_to": "2026-12-31T08:00:00Z",
          "revoked_from": "2026-02-15T00:00:00Z"
        }
      ]
    },
    {
      "id": "adb1a130-a3eb-481a-a61d-0dc8bbbe685e",
      "instrument_id": "GAUGE-001",
      "value": 10.02,
      "measured_at": "2026-07-20T00:00:00Z",
      "certificate_id": "2f2d7af5-653b-4615-829b-20b8894f64af",
      "registered_revision": 6,
      "valid": false,
      "failure_chain": [
        {
          "certificate_id": "2f2d7af5-653b-4615-829b-20b8894f64af",
          "instrument_id": "GAUGE-001",
          "calibrated_at": "2026-01-01T08:00:00Z",
          "valid_from": "2026-01-01T08:00:00Z",
          "valid_to": "2026-12-31T08:00:00Z",
          "revoked_from": "2026-02-15T00:00:00Z"
        }
      ]
    }
  ]
}
```

### 12. 撤销后按当前修订查询 M1

请求：

```http
GET /measurements/8f4ad26e-27ed-423d-bde4-fae87a4f2dc3
```

响应：

```json
{
  "revision": 7,
  "measurement": {
    "id": "8f4ad26e-27ed-423d-bde4-fae87a4f2dc3",
    "instrument_id": "GAUGE-003",
    "value": 4.21,
    "measured_at": "2026-05-10T00:00:00Z",
    "certificate_id": "001b8e7e-6978-4339-b428-654f3e97de6f",
    "registered_revision": 5,
    "valid": false,
    "failure_chain": [
      {
        "certificate_id": "001b8e7e-6978-4339-b428-654f3e97de6f",
        "instrument_id": "GAUGE-003",
        "calibrated_at": "2026-05-01T00:00:00Z",
        "valid_from": "2026-05-01T00:00:00Z",
        "valid_to": "2026-11-01T00:00:00Z",
        "revoked_from": null
      },
      {
        "certificate_id": "9c50ded3-59e4-4a16-b2ce-1a5207f0cd41",
        "instrument_id": "GAUGE-002",
        "calibrated_at": "2026-03-01T00:00:00Z",
        "valid_from": "2026-03-01T00:00:00Z",
        "valid_to": "2026-10-01T00:00:00Z",
        "revoked_from": null
      },
      {
        "certificate_id": "2f2d7af5-653b-4615-829b-20b8894f64af",
        "instrument_id": "GAUGE-001",
        "calibrated_at": "2026-01-01T08:00:00Z",
        "valid_from": "2026-01-01T08:00:00Z",
        "valid_to": "2026-12-31T08:00:00Z",
        "revoked_from": "2026-02-15T00:00:00Z"
      }
    ]
  }
}
```

### 13. 撤销点之前的 M0 仍有效

请求：

```http
GET /measurements/f858bb28-ec1d-4af8-af78-b26a27d1094e
```

响应：

```json
{
  "revision": 7,
  "measurement": {
    "id": "f858bb28-ec1d-4af8-af78-b26a27d1094e",
    "instrument_id": "GAUGE-001",
    "value": 10.0,
    "measured_at": "2026-02-01T00:00:00Z",
    "certificate_id": "2f2d7af5-653b-4615-829b-20b8894f64af",
    "registered_revision": 4,
    "valid": true,
    "failure_chain": null
  }
}
```

### 14. 撤销后尝试登记会被拒绝

请求：

```json
{
  "instrument_id": "GAUGE-003",
  "value": 4.0,
  "measured_at": "2026-06-01T00:00:00Z",
  "expected_revision": 7
}
```

响应 `422`：

```json
{
  "error": {
    "code": "COVERING_CERTIFICATE_INVALID",
    "message": "覆盖该时刻的证书已失效（被撤销或锚点断裂），登记被拒绝",
    "details": {
      "certificate_id": "001b8e7e-6978-4339-b428-654f3e97de6f",
      "failure_chain": [
        {
          "certificate_id": "001b8e7e-6978-4339-b428-654f3e97de6f",
          "instrument_id": "GAUGE-003",
          "calibrated_at": "2026-05-01T00:00:00Z",
          "valid_from": "2026-05-01T00:00:00Z",
          "valid_to": "2026-11-01T00:00:00Z",
          "revoked_from": null
        },
        {
          "certificate_id": "9c50ded3-59e4-4a16-b2ce-1a5207f0cd41",
          "instrument_id": "GAUGE-002",
          "calibrated_at": "2026-03-01T00:00:00Z",
          "valid_from": "2026-03-01T00:00:00Z",
          "valid_to": "2026-10-01T00:00:00Z",
          "revoked_from": null
        },
        {
          "certificate_id": "2f2d7af5-653b-4615-829b-20b8894f64af",
          "instrument_id": "GAUGE-001",
          "calibrated_at": "2026-01-01T08:00:00Z",
          "valid_from": "2026-01-01T08:00:00Z",
          "valid_to": "2026-12-31T08:00:00Z",
          "revoked_from": "2026-02-15T00:00:00Z"
        }
      ]
    }
  }
}
```

### 15. 列表查询保留历史结论

撤销前的修订 6：

```http
GET /measurements?revision=6
```

其中 M1 为：

```json
{
  "id": "8f4ad26e-27ed-423d-bde4-fae87a4f2dc3",
  "instrument_id": "GAUGE-003",
  "value": 4.21,
  "measured_at": "2026-05-10T00:00:00Z",
  "certificate_id": "001b8e7e-6978-4339-b428-654f3e97de6f",
  "registered_revision": 5,
  "valid": true,
  "failure_chain": null
}
```

当前修订 7：

```http
GET /measurements
```

同一测量变为：

```json
{
  "id": "8f4ad26e-27ed-423d-bde4-fae87a4f2dc3",
  "instrument_id": "GAUGE-003",
  "value": 4.21,
  "measured_at": "2026-05-10T00:00:00Z",
  "certificate_id": "001b8e7e-6978-4339-b428-654f3e97de6f",
  "registered_revision": 5,
  "valid": false,
  "failure_chain": [
    {
      "certificate_id": "001b8e7e-6978-4339-b428-654f3e97de6f",
      "instrument_id": "GAUGE-003",
      "calibrated_at": "2026-05-01T00:00:00Z",
      "valid_from": "2026-05-01T00:00:00Z",
      "valid_to": "2026-11-01T00:00:00Z",
      "revoked_from": null
    },
    {
      "certificate_id": "9c50ded3-59e4-4a16-b2ce-1a5207f0cd41",
      "instrument_id": "GAUGE-002",
      "calibrated_at": "2026-03-01T00:00:00Z",
      "valid_from": "2026-03-01T00:00:00Z",
      "valid_to": "2026-10-01T00:00:00Z",
      "revoked_from": null
    },
    {
      "certificate_id": "2f2d7af5-653b-4615-829b-20b8894f64af",
      "instrument_id": "GAUGE-001",
      "calibrated_at": "2026-01-01T08:00:00Z",
      "valid_from": "2026-01-01T08:00:00Z",
      "valid_to": "2026-12-31T08:00:00Z",
      "revoked_from": "2026-02-15T00:00:00Z"
    }
  ]
}
```

## cURL 快速试一下

```bash
# 1. 根证书
ROOT=$(curl -s -X POST http://localhost:8000/certificates \
  -H 'Content-Type: application/json' \
  -d '{
    "instrument_id":"GAUGE-001",
    "calibrated_at":"2026-01-01T08:00:00Z",
    "valid_from":"2026-01-01T08:00:00Z",
    "valid_to":"2026-12-31T08:00:00Z",
    "error":0.01,
    "tolerance":0.05,
    "expected_revision":0
  }')
ROOT_ID=$(printf '%s' "$ROOT" | python3 -c 'import json,sys; print(json.load(sys.stdin)["certificate"]["id"])')

# 2. 测量
curl -s -X POST http://localhost:8000/measurements \
  -H 'Content-Type: application/json' \
  -d '{
    "instrument_id":"GAUGE-001",
    "value":10.02,
    "measured_at":"2026-07-20T00:00:00Z",
    "expected_revision":1
  }'

# 3. 撤销
curl -s -X POST "http://localhost:8000/certificates/$ROOT_ID/revoke" \
  -H 'Content-Type: application/json' \
  -d '{"revoked_from":"2026-02-15T00:00:00Z","expected_revision":2}'
```

## 错误码

| HTTP 状态码 | error.code | 场景 |
|---|---|---|
| `409` | `REVISION_CONFLICT` | `expected_revision` 与当前修订不一致 |
| `422` | `INVALID_VALIDITY_WINDOW` | 起点不早于终点 |
| `422` | `NEGATIVE_TOLERANCE` | 容差为负 |
| `422` | `ERROR_EXCEEDS_TOLERANCE` | 误差绝对值超过容差 |
| `422` | `VALIDITY_OVERLAP` | 同一器具证书有效期重叠 |
| `422` | `PARENT_NOT_FOUND` | 上级证书不存在 |
| `422` | `PARENT_NOT_EFFECTIVE_AT_CALIBRATION` | 上级在校准时刻无效 |
| `422` | `NO_COVERING_CERTIFICATE` | 测量时刻没有证书覆盖 |
| `422` | `COVERING_CERTIFICATE_INVALID` | 覆盖证书已失效 |
| `422` | `REVOKE_TIME_OUT_OF_WINDOW` | 撤销时刻不在证书有效期内 |
| `422` | `CERTIFICATE_ALREADY_REVOKED` | 证书已撤销，不能重复撤销 |
| `422` | `REVISION_IN_FUTURE` | 查询了尚未产生的修订号 |
| `404` | `CERTIFICATE_NOT_FOUND` | 证书不存在 |
| `404` | `MEASUREMENT_NOT_FOUND` | 测量不存在，或在指定修订时尚未登记 |

## 本地开发（不使用 Docker）

```bash
python -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt

export DATABASE_DSN='postgresql://calib:calib@localhost:5432/calibration'
uvicorn app.main:app --reload --host 0.0.0.0 --port 8000
```

应用首次启动会自动创建表、索引和 PostgreSQL GiST 排他约束。

## 端到端测试

`tests/test_e2e.py` 直接对一个**全新（revision=0）**的运行实例发起 HTTP 请求，
覆盖证书登记校验、链锚定、测量绑定、撤销级联、失效链构造、历史修订查询与并发
`expected_revision` 冲突。它会写入数据，因此请指向一个空库实例。

```bash
pip install -r requirements-dev.txt

# 假设服务已通过 docker compose up 启动（端口映射到本机 8000）：
python tests/test_e2e.py

# 或指定其他地址：
API_BASE=http://127.0.0.1:8000 python tests/test_e2e.py
```

预期输出最后一行为 `44 passed, 0 failed`。
