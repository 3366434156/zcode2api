"""请求用量台账（SQLite 持久化）——「请求监控」的数据源。

为什么单独一个库、单独一条写线程：
- **请求路径不能等磁盘**：`record()` 只是往队列里放一行，后台线程批量落库
  （一个事务多条），慢盘 / 锁竞争都不会拖住对话。
- **读也不抢写锁**：面板查询同样提交给这条线程，写与读串行，不会出现两个
  连接互相抢 SQLite 写锁的经典事故。
- **数据量比内存环大得多**：默认保留 30 天 / 5 万行，超出的按时间清理；
  内存环只留最近 500 条做「实时」视图，明细永远从库里查。

字段口径（对齐 workbuddy 网关的 usage 行，以本网关的上游形态为准）：
- `outcome` 是终态，和 `http_status` 分开记：失败的请求可能压根没有 HTTP 状态
  （连接断开 / 客户端取消），而 200 的请求也可能被客户端中途取消。
- `ttft_ms` 首字延迟；`gen_ms` 生成窗口（末字 - 首字）；`tokens_per_sec` 用生成
  窗口算——否则长 prompt 的排队时间会把速度算得毫无意义。
- `cached_tokens` / `cache_hit_pct`：上游前缀缓存命中，直接决定钱与首字。
- `usage_missing`：上游没报 usage 也照样落一行。丢掉这种请求会让请求数、成功率、
  时延样本一起失真（只剩 token 合计不准还算是轻的）。
"""

from __future__ import annotations

import queue
import sqlite3
import threading
import time
from contextlib import closing

from . import settings

_SCHEMA = """
CREATE TABLE IF NOT EXISTS requests (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    req_id        TEXT    NOT NULL,
    ts            REAL    NOT NULL,
    endpoint      TEXT    NOT NULL,
    provider      TEXT    NOT NULL DEFAULT 'zai',
    account       TEXT    NOT NULL DEFAULT '',
    account_id    TEXT    NOT NULL DEFAULT '',
    mode          TEXT    NOT NULL DEFAULT '',
    model         TEXT    NOT NULL DEFAULT '',
    requested_model TEXT  NOT NULL DEFAULT '',
    stream        INTEGER NOT NULL DEFAULT 0,
    outcome       TEXT    NOT NULL DEFAULT 'completed',
    http_status   INTEGER,
    error         TEXT    NOT NULL DEFAULT '',
    total_ms      INTEGER,
    ttft_ms       INTEGER,
    gen_ms        INTEGER,
    input_tokens  INTEGER,
    output_tokens INTEGER,
    cached_tokens INTEGER,
    credit        REAL,
    tokens_per_sec REAL,
    cache_hit_pct REAL,
    usage_missing INTEGER NOT NULL DEFAULT 0,
    preview       TEXT    NOT NULL DEFAULT '',
    client        TEXT    NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_req_ts      ON requests (ts);
CREATE INDEX IF NOT EXISTS idx_req_account ON requests (account);
CREATE INDEX IF NOT EXISTS idx_req_model   ON requests (model);
CREATE INDEX IF NOT EXISTS idx_req_outcome ON requests (outcome);
"""

_COLUMNS = (
    "req_id", "ts", "endpoint", "provider", "account", "account_id", "mode",
    "model", "requested_model", "stream", "outcome", "http_status", "error",
    "total_ms", "ttft_ms", "gen_ms", "input_tokens", "output_tokens", "cached_tokens",
    "credit", "tokens_per_sec", "cache_hit_pct", "usage_missing", "preview", "client",
)

# 每批最多写多少行（一次事务 / 一次 fsync）
_BATCH_MAX = 128
# 清理节流：几秒一次 DELETE 足够，不必每批都跑
_PRUNE_INTERVAL = 60.0
# 面板一次最多返回多少行（前端分页）
MAX_PAGE = 500


class UsageLog:
    """请求台账：非阻塞写入 + 串行读。"""

    # 面板一页最多多少行（对外暴露，供路由层夹取 limit）
    MAX_PAGE = MAX_PAGE

    def __init__(self, path=None) -> None:
        self.path = str(path or settings.USAGE_DB_PATH)
        self._queue: queue.Queue = queue.Queue()
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()
        self._last_prune = 0.0
        self._init_db()

    # ── SQLite 基础 ──────────────────────────────────────────────────────────
    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA busy_timeout=5000")
        return conn

    def _init_db(self) -> None:
        with closing(self._connect()) as conn:
            conn.executescript(_SCHEMA)
            conn.commit()

    # ── 写 ───────────────────────────────────────────────────────────────────
    def record(self, row: dict) -> None:
        """把一行放进队列就返回：永不阻塞请求路径。"""
        try:
            self._ensure_thread()
            self._queue.put(("row", row, None))
        except Exception:  # noqa: BLE001 - 记录失败不能影响对话
            pass

    def flush(self, timeout: float = 5.0) -> None:
        """等队列落盘（测试与「查询前先看到自己的写入」用）。"""
        done = threading.Event()
        self._ensure_thread()
        self._queue.put(("flush", None, done))
        done.wait(timeout)

    def _ensure_thread(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return
            self._thread = threading.Thread(target=self._run, name="usagelog", daemon=True)
            self._thread.start()

    def _run(self) -> None:
        conn = self._connect()
        try:
            while True:
                batch = self._drain()
                if batch is None:      # 收到结束信号
                    return
                for op in batch:
                    if op is None:      # 结束信号可能夹在批次里
                        return
                    kind, payload, done = op
                    if kind == "flush":
                        conn.commit()
                        done.set()
                        continue
                    self._write(conn, payload)
                conn.commit()
                self._maybe_prune(conn)
        finally:
            conn.close()

    def _drain(self) -> list | None:
        """取一批待写操作；空队列则阻塞等待第一条。"""
        first = self._queue.get()
        if first is None:
            return None
        batch = [first]
        while len(batch) < _BATCH_MAX:
            try:
                batch.append(self._queue.get_nowait())
            except queue.Empty:
                break
        return batch

    @staticmethod
    def _write(conn: sqlite3.Connection, row: dict) -> None:
        values = [_normalize(col, row.get(col)) for col in _COLUMNS]
        placeholders = ",".join("?" * len(_COLUMNS))
        conn.execute(
            f"INSERT INTO requests ({','.join(_COLUMNS)}) VALUES ({placeholders})",
            values,
        )

    def _maybe_prune(self, conn: sqlite3.Connection) -> None:
        now = time.time()
        if now - self._last_prune < _PRUNE_INTERVAL:
            return
        self._last_prune = now
        try:
            if settings.USAGE_RETENTION_DAYS > 0:
                cutoff = now - settings.USAGE_RETENTION_DAYS * 86400
                conn.execute("DELETE FROM requests WHERE ts < ?", (cutoff,))
            if settings.USAGE_MAX_ROWS > 0:
                conn.execute(
                    """DELETE FROM requests WHERE id <= (
                           SELECT id FROM requests ORDER BY id DESC LIMIT 1 OFFSET ?
                       )""",
                    (settings.USAGE_MAX_ROWS,),
                )
            conn.commit()
        except sqlite3.Error:
            pass

    def close(self) -> None:
        thread, self._thread = self._thread, None
        if thread is None:
            return
        self._queue.put(None)
        thread.join(timeout=5.0)

    # ── 读 ───────────────────────────────────────────────────────────────────
    def query(self, where: str = "", params: tuple = (), limit: int = 200,
              offset: int = 0) -> tuple[list[dict], int]:
        """明细：返回 (rows, total)。最新在前。"""
        limit = max(1, min(int(limit), MAX_PAGE))
        offset = max(0, int(offset))
        clause = f" WHERE {where}" if where else ""
        with closing(self._connect()) as conn:
            total = conn.execute(f"SELECT COUNT(*) AS n FROM requests{clause}", params).fetchone()["n"]
            rows = conn.execute(
                f"SELECT * FROM requests{clause} ORDER BY ts DESC, id DESC LIMIT ? OFFSET ?",
                (*params, limit, offset),
            ).fetchall()
        return [dict(r) for r in rows], int(total)

    def stats(self, where: str = "", params: tuple = ()) -> dict:
        """区间聚合：总量、成功率、时延、token、缓存命中、tok/s。"""
        clause = f" WHERE {where}" if where else ""
        with closing(self._connect()) as conn:
            row = conn.execute(
                f"""SELECT
                      COUNT(*) AS calls,
                      SUM(CASE WHEN outcome = 'completed' THEN 1 ELSE 0 END) AS ok,
                      SUM(CASE WHEN outcome <> 'completed' THEN 1 ELSE 0 END) AS fail,
                      SUM(COALESCE(input_tokens, 0))  AS input_tokens,
                      SUM(COALESCE(output_tokens, 0)) AS output_tokens,
                      SUM(COALESCE(cached_tokens, 0)) AS cached_tokens,
                      SUM(COALESCE(credit, 0))        AS credit,
                      SUM(CASE WHEN usage_missing = 1 THEN 1 ELSE 0 END) AS usage_missing,
                      AVG(ttft_ms)  AS avg_ttft_ms,
                      AVG(gen_ms)   AS avg_gen_ms,
                      AVG(total_ms) AS avg_total_ms,
                      AVG(tokens_per_sec) AS avg_tps,
                      MAX(tokens_per_sec) AS peak_tps
                    FROM requests{clause}""",
                params,
            ).fetchone()
        out = dict(row)
        for key in ("calls", "ok", "fail", "input_tokens", "output_tokens",
                    "cached_tokens", "usage_missing"):
            out[key] = int(out.get(key) or 0)
        out["credit"] = float(out.get("credit") or 0.0)
        done = out["ok"] + out["fail"]
        out["success_rate"] = round(out["ok"] * 100.0 / done, 1) if done else None
        prompt_all = out["input_tokens"] + out["cached_tokens"]
        out["cache_hit_pct"] = (
            round(out["cached_tokens"] * 100.0 / prompt_all, 1) if prompt_all else None
        )
        for key in ("avg_ttft_ms", "avg_gen_ms", "avg_total_ms", "avg_tps", "peak_tps"):
            out[key] = round(out[key], 1) if out.get(key) is not None else None
        return out

    def group_by(self, column: str, where: str = "", params: tuple = (),
                 limit: int = 50) -> list[dict]:
        """按账号 / 模型分组：调用数、成败、token、首字、tok/s。"""
        if column not in ("account", "model", "mode", "endpoint", "outcome"):
            raise ValueError(f"不支持的分组列: {column}")
        clause = f" WHERE {where}" if where else ""
        with closing(self._connect()) as conn:
            rows = conn.execute(
                f"""SELECT {column} AS name,
                          COUNT(*) AS calls,
                          SUM(CASE WHEN outcome = 'completed' THEN 1 ELSE 0 END) AS ok,
                          SUM(CASE WHEN outcome <> 'completed' THEN 1 ELSE 0 END) AS fail,
                          SUM(COALESCE(input_tokens, 0))  AS input_tokens,
                          SUM(COALESCE(output_tokens, 0)) AS output_tokens,
                          SUM(COALESCE(cached_tokens, 0)) AS cached_tokens,
                          AVG(ttft_ms) AS avg_ttft_ms,
                          AVG(tokens_per_sec) AS avg_tps,
                          MAX(ts) AS last_ts
                    FROM requests{clause}
                    GROUP BY {column}
                    ORDER BY calls DESC
                    LIMIT ?""",
                (*params, max(1, min(int(limit), 200))),
            ).fetchall()
        out = []
        for r in rows:
            item = dict(r)
            done = (item.get("ok") or 0) + (item.get("fail") or 0)
            item["success_rate"] = round(item["ok"] * 100.0 / done, 1) if done else None
            item["avg_ttft_ms"] = round(item["avg_ttft_ms"], 1) if item.get("avg_ttft_ms") is not None else None
            item["avg_tps"] = round(item["avg_tps"], 1) if item.get("avg_tps") is not None else None
            out.append(item)
        return out

    def series(self, where: str = "", params: tuple = (), buckets: int = 24) -> list[dict]:
        """按小时分桶的调用量 / 失败数（趋势图）。"""
        clause = f" WHERE {where}" if where else ""
        with closing(self._connect()) as conn:
            rows = conn.execute(
                f"""SELECT CAST(ts / 3600 AS INTEGER) AS hour,
                          COUNT(*) AS calls,
                          SUM(CASE WHEN outcome <> 'completed' THEN 1 ELSE 0 END) AS fail,
                          SUM(COALESCE(output_tokens, 0)) AS output_tokens
                    FROM requests{clause}
                    GROUP BY hour ORDER BY hour DESC LIMIT ?""",
                (*params, max(1, min(int(buckets), 240))),
            ).fetchall()
        return [dict(r) for r in reversed(rows)]

    def clear(self) -> int:
        with closing(self._connect()) as conn:
            n = conn.execute("SELECT COUNT(*) AS n FROM requests").fetchone()["n"]
            conn.execute("DELETE FROM requests")
            conn.commit()
        return int(n)


def _normalize(key: str, value):
    """按列语义归一化：数据库里不放大对象、不写 None 到 NOT NULL 列。"""
    if key in ("stream", "usage_missing"):
        return 1 if value else 0
    if value is None:
        return None if key in ("http_status", "total_ms", "ttft_ms", "gen_ms",
                               "input_tokens", "output_tokens", "cached_tokens",
                               "credit", "tokens_per_sec", "cache_hit_pct") else ""
    return value


usagelog = UsageLog()
