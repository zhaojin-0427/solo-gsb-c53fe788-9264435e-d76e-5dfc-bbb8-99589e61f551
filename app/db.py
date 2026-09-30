"""数据库连接与建表初始化。"""
from __future__ import annotations

import os
import time
from contextlib import contextmanager
from typing import Iterator

import psycopg
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

DSN = os.environ.get(
    "DATABASE_DSN", "postgresql://calib:calib@localhost:5432/calibration"
)

pool: ConnectionPool | None = None


def init_pool() -> None:
    global pool

    def configure(conn) -> None:
        # 每个事务使用 REPEATABLE READ：load_snapshot 的多条 SELECT 必须共享
        # 同一快照，避免并发写入在两条查询之间提交后造成「新测量引用了尚未
        # 装载的新证书」。SET 会开启一个事务，立即提交以使连接回到 IDLE，
        # 会话级设置随后对所有事务生效。
        conn.execute(
            "SET SESSION CHARACTERISTICS AS TRANSACTION "
            "ISOLATION LEVEL REPEATABLE READ"
        )
        conn.commit()

    pool = ConnectionPool(
        DSN, min_size=1, max_size=10, configure=configure,
        kwargs={"row_factory": dict_row},
    )
    # 启动阶段等待数据库就绪（容器编排下 db 可能稍慢）
    for attempt in range(60):
        try:
            with pool.connection() as conn:
                conn.execute("SELECT 1")
            break
        except Exception:
            if attempt == 59:
                raise
            time.sleep(1)


def close_pool() -> None:
    if pool is not None:
        pool.close()


@contextmanager
def conn_cursor(commit: bool = False) -> Iterator[psycopg.Cursor]:
    assert pool is not None
    with pool.connection() as conn:
        if commit:
            # 写事务由 advisory lock 串行化，无需快照隔离；在事务首条语句前
            # 降为 READ COMMITTED，避免 RR 下等待行锁后触发 serialization_failure。
            conn.execute("SET TRANSACTION ISOLATION LEVEL READ COMMITTED")
            conn.execute("SELECT pg_advisory_xact_lock(9264435)")
        cur = conn.cursor()
        try:
            yield cur
            if commit:
                conn.commit()
            else:
                conn.rollback()  # 只读事务：释放快照
        except Exception:
            conn.rollback()
            raise


SCHEMA_SQL = """
CREATE EXTENSION IF NOT EXISTS btree_gist;

CREATE TABLE IF NOT EXISTS certificates (
    id UUID PRIMARY KEY,
    instrument_id TEXT NOT NULL,
    calibrated_at TIMESTAMPTZ NOT NULL,
    valid_from TIMESTAMPTZ NOT NULL,
    valid_to   TIMESTAMPTZ NOT NULL,
    error      NUMERIC(18,6) NOT NULL,
    tolerance  NUMERIC(18,6) NOT NULL,
    parent_id  UUID REFERENCES certificates(id),
    created_revision BIGINT NOT NULL,
    revoked_from TIMESTAMPTZ,
    revoke_revision BIGINT,
    CHECK (valid_from < valid_to),
    CHECK (tolerance >= 0),
    CHECK (abs(error) <= tolerance)
);

-- 非根证书的「校准时刻必须落在上级有效窗口内」以及「上级在该时刻未撤销」
-- 属于跨表/跨行业务规则（且撤销可后置），由应用层在登记事务中校验；
-- GiST 约束与上面的行内 CHECK 作为数据库侧兜底。

-- 同一器具的半开有效期 [valid_from, valid_to) 不得重叠
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint WHERE conname = 'certificates_no_overlap'
    ) THEN
        ALTER TABLE certificates
            ADD CONSTRAINT certificates_no_overlap
            EXCLUDE USING gist (
                instrument_id WITH =,
                tstzrange(valid_from, valid_to, '[)') WITH &&
            );
    END IF;
END $$;

CREATE INDEX IF NOT EXISTS idx_certificates_instrument
    ON certificates(instrument_id);
CREATE INDEX IF NOT EXISTS idx_certificates_parent ON certificates(parent_id);

CREATE TABLE IF NOT EXISTS revocations (
    certificate_id UUID NOT NULL REFERENCES certificates(id),
    revoked_from TIMESTAMPTZ NOT NULL,
    revision BIGINT NOT NULL,
    PRIMARY KEY (certificate_id, revision)
);

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint WHERE conname = 'revocations_certificate_id_key'
    ) THEN
        ALTER TABLE revocations
            ADD CONSTRAINT revocations_certificate_id_key UNIQUE (certificate_id);
    END IF;
END $$;

CREATE TABLE IF NOT EXISTS measurements (
    id UUID PRIMARY KEY,
    instrument_id TEXT NOT NULL,
    value NUMERIC(18,6) NOT NULL,
    measured_at TIMESTAMPTZ NOT NULL,
    certificate_id UUID NOT NULL REFERENCES certificates(id),
    registered_revision BIGINT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_measurements_instrument
    ON measurements(instrument_id, measured_at);
CREATE INDEX IF NOT EXISTS idx_measurements_certificate
    ON measurements(certificate_id);

-- 全局修订号：所有写事务先取行锁，再条件检查 + 原子 +1
CREATE TABLE IF NOT EXISTS revision_lock (
    id INTEGER PRIMARY KEY DEFAULT (1),
    revision BIGINT NOT NULL DEFAULT 0,
    CHECK (id = 1)
);

INSERT INTO revision_lock (id, revision) VALUES (1, 0)
ON CONFLICT (id) DO NOTHING;
"""


def init_schema() -> None:
    with conn_cursor() as cur:
        cur.execute(SCHEMA_SQL)
        cur.connection.commit()
