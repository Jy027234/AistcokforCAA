-- 每日流水线的调度配置与运行请求（002）
--
-- 为什么单独一个文件
-- ================
-- `apply_migrations()` 按文件名顺序重放 schema/*.sql，靠 `IF NOT EXISTS` 幂等。
-- **已有表的列与约束变更不会被应用**——所以任何新增都必须是新表或新文件，
-- 而不是去改 001（改了也不会在既有的数据目录上生效，而失败是静默的）。
--
-- 为什么要把它放进产品库而不是一个配置文件
-- ======================================
-- 使用者要在界面上设置"每天几点跑"，而这个设置必须能被**独立 worker**
-- 读到、被 API 写到。放进产品库的好处是两者共用同一份事实，
-- 不需要约定一个只有本机能读的路径。
--
-- 同时保留一个刻意的边界：**时间与开关是配置，运行结果不是**。
-- 运行结果留在留痕文件与下面这张请求表里，因为"跑了什么"必须可追溯。

PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS run_schedule (
    -- 单行表：调度是全局的，不是每个组合一份。
    schedule_id       TEXT PRIMARY KEY CHECK (schedule_id = 'daily'),
    enabled           INTEGER NOT NULL DEFAULT 0 CHECK (enabled IN (0, 1)),
    -- 本地时间 HH:MM。**存本地时间而不是 UTC**：使用者说的是"每天 20:30"，
    -- 那是他的墙上时间；存 UTC 会在夏令时或换机器时静默偏移一小时。
    run_at_local      TEXT NOT NULL DEFAULT '20:30' CHECK (length(run_at_local) = 5),
    -- 只在周一至周五跑。周末休市，跑一次只会得到"没有新行情"的跳过留痕。
    weekdays_only     INTEGER NOT NULL DEFAULT 1 CHECK (weekdays_only IN (0, 1)),
    -- 跑流水线的解释器。**必须显式配置**：这条流水线依赖 baostock 与 pytest，
    -- 而系统上默认的 python 未必装了它们——用错的解释器会立刻失败，
    -- 而失败信息是"baostock 未安装"，看起来像数据源问题。
    interpreter       TEXT NOT NULL DEFAULT '',
    -- 数据目录（快照库所在）。空 = 由 worker 按仓库默认解析。
    data_dir          TEXT NOT NULL DEFAULT '',
    -- 快照窗口起点（传给 daily_run 的 --window-start）。
    window_start      TEXT NOT NULL DEFAULT '2026-06-22',
    updated_at        TEXT NOT NULL,
    updated_by        TEXT
);

-- 由**界面**发起的运行请求。自动调度不需要写这里：它直接跑。
--
-- 为什么不让 API 自己执行：一次运行要跑采集与建快照（数十秒到数分钟），
-- 而 API 进程可能没有数据源依赖，也不是"独立 worker"（§14.2）。
-- 因此界面只**请求**，worker 负责执行并把结果写回来——
-- 这样"点一次按钮"与"到点自动跑"走的是同一条执行路径。
CREATE TABLE IF NOT EXISTS pipeline_run_request (
    request_id      TEXT PRIMARY KEY,
    source          TEXT NOT NULL CHECK (source IN ('scheduler', 'manual')),
    reason          TEXT,
    requested_by    TEXT,
    requested_at    TEXT NOT NULL,
    claimed_at      TEXT,
    claimed_by      TEXT,
    status          TEXT NOT NULL CHECK (status IN
                      ('PENDING', 'CLAIMED', 'DONE', 'FAILED', 'CANCELLED')),
    finished_at     TEXT,
    exit_code       INTEGER,
    detail          TEXT
);

CREATE INDEX IF NOT EXISTS idx_pipeline_run_request_status
    ON pipeline_run_request (status, requested_at);

-- 运行结果摘要（单行）。留痕文件仍是权威；这张表只是让界面与 API
-- 不必去解析 JSONL——而"让界面去 grep 日志"正是最容易长出第二套格式的地方。
CREATE TABLE IF NOT EXISTS pipeline_last_run (
    run_id          TEXT PRIMARY KEY CHECK (run_id = 'last'),
    request_id      TEXT REFERENCES pipeline_run_request(request_id),
    source          TEXT,
    trading_day     TEXT,
    snapshot_id     TEXT,
    outcome         TEXT,
    reason          TEXT,
    exit_code       INTEGER,
    started_at      TEXT,
    finished_at     TEXT,
    duration_seconds REAL,
    steps_json      TEXT
);
