-- ============================================================================
-- 防汛需求与道路情景覆盖数据分析平台 —— 数据库结构
-- 设计要点：
--   1. 来源目录中的每类原文（人口/节点/有向边/安置点/事件/边界）均带单位、
--      发生时间、登记时间、修订号与截止时间，可追溯到 source_documents。
--   2. 路网按“资料截止点”形成 graph_version；事件带生效区间与修订审计。
--   3. 情景工作区固定人口基准日、出发时刻、总时限、道路资料截止点。
--   4. 计算结果以完整情景签名入缓存索引，证据截止时间随结果保存。
-- ============================================================================

CREATE TABLE IF NOT EXISTS source_documents (
    doc_id              BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    dataset_type        TEXT NOT NULL,
    title               TEXT NOT NULL,
    source_org          TEXT,
    unit_note           TEXT,                     -- 原文计量单位说明
    source_occurred_at  TIMESTAMPTZ,              -- 原文所述事件发生时间
    received_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
    cutoff_at           TIMESTAMPTZ NOT NULL,     -- 该资料代表的证据截止时间
    revision_no         INTEGER NOT NULL DEFAULT 1,
    supersedes_doc_id   BIGINT REFERENCES source_documents(doc_id),
    raw_text            TEXT,                     -- 原文摘录
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT chk_dataset_type CHECK (dataset_type IN
        ('population','road_node','road_edge','shelter',
         'road_event','boundary',
         'mobile_signal','venue_presence','transfer_registration','overlap_rule'))
);

-- 旧库（无命名约束）幂等迁移：把 dataset_type 上的旧 CHECK 替换为含临时来源类型的新约束
DO $$
DECLARE
    con_name TEXT;
BEGIN
    IF EXISTS (SELECT 1 FROM pg_constraint
               WHERE conrelid = 'source_documents'::regclass AND contype = 'c'
                 AND conname <> 'chk_dataset_type') THEN
        FOREACH con_name IN ARRAY (
            SELECT array_agg(conname) FROM pg_constraint
            WHERE conrelid = 'source_documents'::regclass AND contype = 'c'
              AND conname <> 'chk_dataset_type'
        )
        LOOP
            EXECUTE format('ALTER TABLE source_documents DROP CONSTRAINT %I', con_name);
        END LOOP;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_constraint
                   WHERE conrelid = 'source_documents'::regclass
                     AND conname = 'chk_dataset_type') THEN
        ALTER TABLE source_documents
            ADD CONSTRAINT chk_dataset_type CHECK (dataset_type IN
            ('population','road_node','road_edge','shelter',
             'road_event','boundary',
             'mobile_signal','venue_presence','transfer_registration','overlap_rule'));
    END IF;
END $$;

-- ---------------------------------------------------------------- 区域边界 --
CREATE TABLE IF NOT EXISTS region_boundaries (
    boundary_id         TEXT PRIMARY KEY,
    name                TEXT NOT NULL,
    geometry            JSONB NOT NULL,           -- [[x,y],...] 简单多边形
    revision_no         INTEGER NOT NULL DEFAULT 1,
    source_doc_id       BIGINT REFERENCES source_documents(doc_id),
    revised_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- ---------------------------------------------------------------- 道路节点 --
CREATE TABLE IF NOT EXISTS road_nodes (
    node_id             TEXT PRIMARY KEY,
    name                TEXT,
    geom_x              DOUBLE PRECISION NOT NULL,
    geom_y              DOUBLE PRECISION NOT NULL,
    allow_wait          BOOLEAN NOT NULL DEFAULT FALSE,  -- 是否允许在该节点等待
    source_doc_id       BIGINT REFERENCES source_documents(doc_id),
    first_seen_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- ----------------------------------------------------- 有向边（版本化修订） --
-- 同一边的每次修订一行，用 valid_from/valid_to 表示有效期；截止点取数时
-- 只选 valid_from <= cutoff < valid_to 的修订。
CREATE TABLE IF NOT EXISTS road_edge_revisions (
    edge_id             TEXT NOT NULL,
    revision_no         INTEGER NOT NULL,
    from_node           TEXT NOT NULL REFERENCES road_nodes(node_id),
    to_node             TEXT NOT NULL REFERENCES road_nodes(node_id),
    length_m            DOUBLE PRECISION NOT NULL CHECK (length_m > 0),
    base_speed_kmh      DOUBLE PRECISION NOT NULL CHECK (base_speed_kmh > 0),
    valid_from          TIMESTAMPTZ NOT NULL,
    valid_to            TIMESTAMPTZ,                       -- NULL 表示至今有效
    source_doc_id       BIGINT REFERENCES source_documents(doc_id),
    PRIMARY KEY (edge_id, revision_no),
    CHECK (valid_to IS NULL OR valid_to > valid_from)
);
CREATE INDEX IF NOT EXISTS idx_edge_rev_valid ON road_edge_revisions(valid_from, valid_to);

-- --------------------------------------------- 封路 / 分时限速事件（时变） --
-- 同一事件的每次修订各占一行（复合主键），快照按资料截止点取 received_at <= cutoff
-- 的最新修订，保证旧截止点分析在新修订录入后仍可精确复现。
CREATE TABLE IF NOT EXISTS road_event_registry (
    event_id            TEXT PRIMARY KEY,
    edge_id             TEXT NOT NULL,
    event_type          TEXT NOT NULL CHECK (event_type IN ('closure','speed_limit')),
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS road_events (
    event_id            TEXT NOT NULL REFERENCES road_event_registry(event_id),
    revision_no         INTEGER NOT NULL,
    edge_id             TEXT NOT NULL,
    event_type          TEXT NOT NULL CHECK (event_type IN ('closure','speed_limit')),
    effective_from      TIMESTAMPTZ NOT NULL,
    effective_to        TIMESTAMPTZ,              -- NULL 表示尚未恢复
    speed_limit_kmh     DOUBLE PRECISION CHECK (speed_limit_kmh IS NULL OR speed_limit_kmh > 0),
    supersedes_event_id TEXT,
    status              TEXT NOT NULL DEFAULT 'active'
                        CHECK (status IN ('active','cancelled')),
    source_doc_id       BIGINT REFERENCES source_documents(doc_id),
    received_at         TIMESTAMPTZ NOT NULL DEFAULT now(),  -- 平台收到该修订的时间
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (event_id, revision_no),
    CHECK (event_type = 'closure' OR speed_limit_kmh IS NOT NULL)
);
CREATE INDEX IF NOT EXISTS idx_event_edge ON road_events(edge_id);
CREATE INDEX IF NOT EXISTS idx_event_effective ON road_events(effective_from, effective_to);
CREATE INDEX IF NOT EXISTS idx_event_received ON road_events(received_at);

-- 事件修订审计：封路、延期、恢复（重开）、限速调整均留痕
CREATE TABLE IF NOT EXISTS road_event_revisions (
    audit_id            BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    event_id            TEXT NOT NULL REFERENCES road_event_registry(event_id),
    revision_no         INTEGER NOT NULL,
    change_summary      TEXT NOT NULL,
    change_json         JSONB NOT NULL,
    changed_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    source_doc_id       BIGINT REFERENCES source_documents(doc_id)
);

-- ---------------------------------------------------------------- 安置点 --
CREATE TABLE IF NOT EXISTS shelters (
    shelter_id          TEXT PRIMARY KEY,
    name                TEXT NOT NULL,
    node_id             TEXT NOT NULL REFERENCES road_nodes(node_id),
    geom_x              DOUBLE PRECISION NOT NULL,
    geom_y              DOUBLE PRECISION NOT NULL,
    revision_no         INTEGER NOT NULL DEFAULT 1,
    source_doc_id       BIGINT REFERENCES source_documents(doc_id),
    revised_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    active              BOOLEAN NOT NULL DEFAULT TRUE
);

-- 安置点开放区间：到达时刻必须落在任一区间内才视为开放
CREATE TABLE IF NOT EXISTS shelter_open_windows (
    window_id           BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    shelter_id          TEXT NOT NULL REFERENCES shelters(shelter_id),
    open_from           TIMESTAMPTZ NOT NULL,
    open_to             TIMESTAMPTZ,                 -- NULL 表示长期开放
    revision_no         INTEGER NOT NULL DEFAULT 1,
    UNIQUE (shelter_id, open_from, revision_no)
);
CREATE INDEX IF NOT EXISTS idx_shelter_window ON shelter_open_windows(shelter_id);

-- ---------------------------------------------------------------- 人口网格 --
CREATE TABLE IF NOT EXISTS pop_grids (
    grid_id             TEXT PRIMARY KEY,
    name                TEXT,
    geom_x              DOUBLE PRECISION NOT NULL,   -- 质心，供前端定位
    geom_y              DOUBLE PRECISION NOT NULL,
    boundary_id         TEXT REFERENCES region_boundaries(boundary_id),
    source_doc_id       BIGINT REFERENCES source_documents(doc_id),
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- 人口修订：同一网格按基准日取 as_of_date 当日可见的最新修订
CREATE TABLE IF NOT EXISTS pop_grid_revisions (
    grid_id             TEXT NOT NULL REFERENCES pop_grids(grid_id),
    revision_no         INTEGER NOT NULL,
    population          DOUBLE PRECISION NOT NULL CHECK (population >= 0),
    as_of_date          DATE NOT NULL,               -- 人口基准日
    source_doc_id       BIGINT REFERENCES source_documents(doc_id),
    changed_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (grid_id, revision_no)
);
CREATE INDEX IF NOT EXISTS idx_pop_rev_asof ON pop_grid_revisions(as_of_date);

-- 网格→路网连接位置与互斥分配比例；多出口网格按子份额切分人口
CREATE TABLE IF NOT EXISTS grid_connectors (
    connector_id        BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    grid_id             TEXT NOT NULL REFERENCES pop_grids(grid_id),
    node_id             TEXT NOT NULL REFERENCES road_nodes(node_id),
    allocation_ratio    DOUBLE PRECISION NOT NULL CHECK
                        (allocation_ratio > 0 AND allocation_ratio <= 1),
    revision_no         INTEGER NOT NULL DEFAULT 1,
    source_doc_id       BIGINT REFERENCES source_documents(doc_id),
    UNIQUE (grid_id, node_id, revision_no)
);
CREATE INDEX IF NOT EXISTS idx_connector_grid ON grid_connectors(grid_id);

-- ------------------------------------------------- 路网版本（资料截止点） --
CREATE TABLE IF NOT EXISTS road_network_versions (
    version_id          BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    cutoff_at           TIMESTAMPTZ NOT NULL,
    edge_fingerprint    TEXT NOT NULL,               -- 截止点边修订集合指纹
    event_fingerprint   TEXT NOT NULL,               -- 截止点事件集合指纹
    note                TEXT,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (cutoff_at, edge_fingerprint, event_fingerprint)
);

-- ---------------------------------------------------------------- 情景工作区 --
CREATE TABLE IF NOT EXISTS scenarios (
    scenario_id         UUID PRIMARY KEY,
    name                TEXT NOT NULL,
    population_as_of    DATE NOT NULL,               -- 固定人口基准日
    departure_time      TIMESTAMPTZ NOT NULL,        -- 固定出发时刻
    max_trip_minutes    INTEGER NOT NULL CHECK (max_trip_minutes > 0),
    road_cutoff         TIMESTAMPTZ NOT NULL,        -- 道路资料截止点
    boundary_id         TEXT REFERENCES region_boundaries(boundary_id),  -- 分析边界，NULL=全部
    graph_version_id    BIGINT REFERENCES road_network_versions(version_id),
    signature           TEXT NOT NULL UNIQUE,        -- 完整输入的情景签名
    evidence_cutoff_at  TIMESTAMPTZ NOT NULL,        -- 随结果标注的证据截止时间
    created_by          TEXT NOT NULL DEFAULT 'analyst',
    note                TEXT,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- ---------------------------------------------------------------- 计算任务 --
-- ref_count 记录当前查看者数量；取消单个查看者只减引用，不归零不取消任务。
CREATE TABLE IF NOT EXISTS compute_tasks (
    task_id             UUID PRIMARY KEY,
    scenario_id         UUID NOT NULL REFERENCES scenarios(scenario_id),
    signature           TEXT NOT NULL,
    graph_version_id    BIGINT NOT NULL REFERENCES road_network_versions(version_id),
    kind                TEXT NOT NULL CHECK (kind IN ('precomputed','on_demand')),
    status              TEXT NOT NULL CHECK (status IN
                        ('queued','running','done','failed','cancelled'))
                        DEFAULT 'queued',
    ref_count           INTEGER NOT NULL DEFAULT 0,
    stale               BOOLEAN NOT NULL DEFAULT FALSE,  -- 路网更新后标记失效
    evidence_cutoff_at  TIMESTAMPTZ NOT NULL,
    error               TEXT,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    started_at          TIMESTAMPTZ,
    finished_at         TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS idx_task_signature ON compute_tasks(signature, status);

CREATE TABLE IF NOT EXISTS compute_task_viewers (
    task_id             UUID NOT NULL REFERENCES compute_tasks(task_id),
    viewer_id           TEXT NOT NULL,
    joined_at           TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (task_id, viewer_id)
);

-- 任务实际经过的边依赖（来自见证路径），供封路修订精准定位受影响任务
CREATE TABLE IF NOT EXISTS task_edge_dependencies (
    task_id             UUID NOT NULL REFERENCES compute_tasks(task_id),
    edge_id             TEXT NOT NULL,
    PRIMARY KEY (task_id, edge_id)
);

-- 缓存索引：签名即完整输入，不同出发时刻/开放条件绝不共用缓存行
CREATE TABLE IF NOT EXISTS coverage_cache_index (
    signature           TEXT PRIMARY KEY,
    task_id             UUID NOT NULL REFERENCES compute_tasks(task_id),
    graph_version_id    BIGINT NOT NULL,
    departure_time      TIMESTAMPTZ NOT NULL,
    max_trip_minutes    INTEGER NOT NULL,
    evidence_cutoff_at  TIMESTAMPTZ NOT NULL,
    stale               BOOLEAN NOT NULL DEFAULT FALSE,
    computed_at         TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- ---------------------------------------------------------------- 节点到达 --
CREATE TABLE IF NOT EXISTS node_arrivals (
    task_id             UUID NOT NULL REFERENCES compute_tasks(task_id),
    node_id             TEXT NOT NULL,
    arrival_time        TIMESTAMPTZ NOT NULL,
    wait_cost_sec       DOUBLE PRECISION NOT NULL DEFAULT 0,
    witness_node_path   JSONB NOT NULL,              -- 有序节点序列
    witness_edge_path   JSONB NOT NULL,              -- 有序边序列（见证路径）
    PRIMARY KEY (task_id, node_id)
);

-- ---------------------------------------------------------------- 网格份额结果 --
-- 每个互斥子份额一行；covered 去重在汇总阶段完成，同一网格只计一次。
CREATE TABLE IF NOT EXISTS grid_share_results (
    task_id             UUID NOT NULL REFERENCES compute_tasks(task_id),
    grid_id             TEXT NOT NULL,
    connector_node      TEXT NOT NULL,
    share_index         INTEGER NOT NULL,
    share_population    DOUBLE PRECISION NOT NULL,
    reachable           BOOLEAN NOT NULL,
    shelter_id          TEXT REFERENCES shelters(shelter_id),
    departure_time      TIMESTAMPTZ,
    arrival_time        TIMESTAMPTZ,
    total_wait_sec      DOUBLE PRECISION NOT NULL DEFAULT 0,
    witness_edge_path   JSONB,
    status              TEXT NOT NULL CHECK (status IN
                        ('covered','uncovered','unknown')),
    reason              TEXT,                        -- 被排除/未知原因
    evidence_cutoff_at  TIMESTAMPTZ NOT NULL,
    PRIMARY KEY (task_id, grid_id, share_index)
);
CREATE INDEX IF NOT EXISTS idx_share_task ON grid_share_results(task_id);

-- ---------------------------------------------------------------- 覆盖汇总 --
CREATE TABLE IF NOT EXISTS coverage_summaries (
    task_id             UUID PRIMARY KEY REFERENCES compute_tasks(task_id),
    total_population    DOUBLE PRECISION NOT NULL,   -- 边界内全部分母
    located_population  DOUBLE PRECISION NOT NULL,   -- 可定位到路网的人口
    unknown_population  DOUBLE PRECISION NOT NULL,   -- 无连接/缺分配依据
    covered_population  DOUBLE PRECISION NOT NULL,   -- 网格级去重后覆盖
    uncovered_population DOUBLE PRECISION NOT NULL,
    covered_grid_count  INTEGER NOT NULL,
    unknown_grid_count  INTEGER NOT NULL,
    computed_at         TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- ---------------------------------------------------------------- 结果发布 --
-- input_manifest 保存本次比较采用的完整输入（修订号、截止点、窗口原文等）
CREATE TABLE IF NOT EXISTS publications (
    publication_id      UUID PRIMARY KEY,
    title               TEXT NOT NULL,
    scenario_a_id       UUID REFERENCES scenarios(scenario_id),
    scenario_b_id       UUID REFERENCES scenarios(scenario_id),
    input_manifest      JSONB NOT NULL,
    comparison_json     JSONB,
    evidence_cutoff_at  TIMESTAMPTZ NOT NULL,
    created_by          TEXT NOT NULL DEFAULT 'analyst',
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- ============================================================================
-- 临时暴露人口区间（网格 × 时段）
-- 模块边界（以输入快照关联，模块间不共享可变状态）：
--   冻结人口基线(pop_grids/pop_grid_revisions)
--     → 临时信号接入(mobile_signal_*/venue_*/transfer_registrations)
--     → 覆盖率/重叠校准(exposure_input_snapshots + exposure_overlap_rules)
--     → 区间计算(exposure_estimates / exposure_grid_periods)
--     → 情景发布(publications 引用估计版本号)
-- 纪律：隐私阈值、覆盖率或来源数据修订只生成新估计版本；旧版本结果行不原地
--       改写，旧预案保留当时分母。无法证明互斥/包含的来源绝不相加。
-- ============================================================================

-- ------------------------------------------------- 移动信号：分层采样与校准 --
-- 每行是某运营商在某网格、某时段、某人群分层上的去重采样观测；
-- suppressed=TRUE 表示因隐私小样本抑制，sample_count 不可见（按缺失处理，不得记零）。
CREATE TABLE IF NOT EXISTS mobile_signal_samples (
    sample_id           BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    operator_id         TEXT NOT NULL,
    grid_id             TEXT NOT NULL REFERENCES pop_grids(grid_id),
    stratum             TEXT NOT NULL,               -- 分层（resident/visitor…）
    window_start        TIMESTAMPTZ NOT NULL,
    window_end          TIMESTAMPTZ NOT NULL,
    sample_count        DOUBLE PRECISION,            -- 抑制时为 NULL
    suppressed          BOOLEAN NOT NULL DEFAULT FALSE,
    operator_coverage   DOUBLE PRECISION NOT NULL,   -- 该网格时段运营商人群占比（覆盖率）
    coverage_stable     BOOLEAN NOT NULL DEFAULT TRUE,  -- 覆盖率是否稳定（核验结论）
    field_verified      BOOLEAN NOT NULL DEFAULT FALSE, -- 是否经现场核验
    source_doc_id       BIGINT REFERENCES source_documents(doc_id),
    received_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
    CHECK (window_end > window_start),
    CHECK (operator_coverage > 0 AND operator_coverage <= 1),
    CHECK (suppressed OR sample_count IS NOT NULL)
);
CREATE INDEX IF NOT EXISTS idx_mss_grid_window
    ON mobile_signal_samples(grid_id, window_start, window_end);

-- 分层校准系数：系数把“采样人数”扩展为该分层在场人数；系数与误差仅对
-- 覆盖率稳定且现场核验的分层生效，其余分层不得做比例外推。
CREATE TABLE IF NOT EXISTS mobile_signal_calibration (
    calibration_id      BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    operator_id         TEXT NOT NULL,
    grid_id             TEXT NOT NULL REFERENCES pop_grids(grid_id),
    stratum             TEXT NOT NULL,
    effective_from      TIMESTAMPTZ NOT NULL,
    effective_to        TIMESTAMPTZ,
    calibration_factor  DOUBLE PRECISION NOT NULL CHECK (calibration_factor > 0),
    sampling_rel_error  DOUBLE PRECISION NOT NULL CHECK   -- 相对抽样误差（如 0.10=±10%）
                        (sampling_rel_error >= 0 AND sampling_rel_error < 1),
    coverage_basis      DOUBLE PRECISION NOT NULL,         -- 系数所依据的覆盖率
    field_verified      BOOLEAN NOT NULL DEFAULT FALSE,
    source_doc_id       BIGINT REFERENCES source_documents(doc_id),
    UNIQUE (operator_id, grid_id, stratum, effective_from)
);

-- --------------------------------------------- 场所（景区/寄宿学校）在场记录 --
-- 已核实的在场/签到人数；privacy_suppressed=TRUE 时 verified_count 不可见。
-- presence_kind：scenic（景区流入）/ boarding_school（寄宿学校在校）。
CREATE TABLE IF NOT EXISTS venue_presence_records (
    presence_id         BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    venue_id            TEXT NOT NULL,
    venue_name          TEXT NOT NULL,
    presence_kind       TEXT NOT NULL CHECK (presence_kind IN
                        ('scenic','boarding_school')),
    grid_id             TEXT NOT NULL REFERENCES pop_grids(grid_id),
    window_start        TIMESTAMPTZ NOT NULL,
    window_end          TIMESTAMPTZ NOT NULL,
    verified_count      DOUBLE PRECISION,            -- 已核实在场（签到/点名），抑制时 NULL
    privacy_suppressed  BOOLEAN NOT NULL DEFAULT FALSE,
    source_doc_id       BIGINT REFERENCES source_documents(doc_id),
    received_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
    CHECK (window_end > window_start),
    CHECK (privacy_suppressed OR verified_count IS NOT NULL)
);
CREATE INDEX IF NOT EXISTS idx_vpr_grid_window
    ON venue_presence_records(grid_id, window_start, window_end);

-- 场所容量上限：保守方案中作为上界约束（容量未必满员，不能当作确定在场）。
CREATE TABLE IF NOT EXISTS venue_capacity (
    capacity_id         BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    venue_id            TEXT NOT NULL,
    grid_id             TEXT NOT NULL REFERENCES pop_grids(grid_id),
    capacity_persons    DOUBLE PRECISION NOT NULL CHECK (capacity_persons >= 0),
    capacity_kind       TEXT NOT NULL DEFAULT 'hard' CHECK (capacity_kind IN ('hard','soft')),
    effective_from      TIMESTAMPTZ NOT NULL,
    effective_to        TIMESTAMPTZ,
    source_doc_id       BIGINT REFERENCES source_documents(doc_id),
    UNIQUE (venue_id, effective_from)
);

-- ----------------------------------------------------------- 转移登记（减量） --
-- 网格内因临时转移而离开的登记人数；late_arrival=TRUE 表示晚到登记，
-- 晚到登记只进入新估计版本，旧版本结果保留。
CREATE TABLE IF NOT EXISTS transfer_registrations (
    transfer_id         BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    grid_id             TEXT NOT NULL REFERENCES pop_grids(grid_id),
    window_start        TIMESTAMPTZ NOT NULL,
    window_end          TIMESTAMPTZ NOT NULL,
    registered_persons  DOUBLE PRECISION NOT NULL CHECK (registered_persons >= 0),
    destination_grid_id TEXT REFERENCES pop_grids(grid_id),  -- 去向（可空=区外/未知）
    late_arrival        BOOLEAN NOT NULL DEFAULT FALSE,
    source_doc_id       BIGINT REFERENCES source_documents(doc_id),
    received_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
    CHECK (window_end > window_start)
);
CREATE INDEX IF NOT EXISTS idx_tr_grid_window
    ON transfer_registrations(grid_id, window_start, window_end);

-- --------------------------------------------- 可证明的跨来源人员重叠关系 ----
-- 仅登记“可证明”的互斥/包含关系（现场核验或制度依据）；没有规则的来源组合
-- 在区间计算中按不可识别处理：保留并集包络，绝不直接相加。
-- relation：disjoint（互斥，可相加）/ subset_of（A 的人包含于 B）/ overlap_known
-- （已知至少/至多重叠人数，写入 min_overlap_persons/max_overlap_persons）。
CREATE TABLE IF NOT EXISTS exposure_overlap_rules (
    rule_id             BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    grid_id             TEXT NOT NULL REFERENCES pop_grids(grid_id),
    source_a            TEXT NOT NULL,               -- mobile / scenic / boarding_school / transfer
    source_b            TEXT NOT NULL,
    relation            TEXT NOT NULL CHECK (relation IN
                        ('disjoint','subset_of','overlap_known')),
    direction           TEXT NOT NULL DEFAULT 'a_in_b'
                        CHECK (direction IN ('a_in_b','b_in_a','none')),
    min_overlap_persons DOUBLE PRECISION NOT NULL DEFAULT 0,
    max_overlap_persons DOUBLE PRECISION,            -- NULL 表示仅给出下界
    evidence_note       TEXT NOT NULL,
    source_doc_id       BIGINT REFERENCES source_documents(doc_id),
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    CHECK (source_a <> source_b),
    CHECK (min_overlap_persons >= 0),
    CHECK (max_overlap_persons IS NULL OR max_overlap_persons >= min_overlap_persons)
);

-- ------------------------------------------------- 临时暴露估计输入快照 -------
-- 一次估计的不可变输入清单：时间窗、证据截止、隐私阈值、采用的来源行与规则行。
-- 快照之后到达的修订（晚到登记/覆盖率修订）不修改本快照，只用于产生新版本。
CREATE TABLE IF NOT EXISTS exposure_input_snapshots (
    snapshot_id         UUID PRIMARY KEY,
    window_start        TIMESTAMPTZ NOT NULL,
    window_end          TIMESTAMPTZ NOT NULL,
    evidence_cutoff_at  TIMESTAMPTZ NOT NULL,
    privacy_threshold   INTEGER NOT NULL CHECK (privacy_threshold >= 0),
    boundary_id         TEXT REFERENCES region_boundaries(boundary_id),
    source_row_fingerprint TEXT NOT NULL,            -- 采用来源行集合指纹
    included_mobile_ids     JSONB NOT NULL,
    included_venue_ids      JSONB NOT NULL,
    included_transfer_ids   JSONB NOT NULL,
    included_rule_ids       JSONB NOT NULL,
    calibration_ids         JSONB NOT NULL,
    created_by          TEXT NOT NULL DEFAULT 'analyst',
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    CHECK (window_end > window_start)
);

-- ----------------------------------------------------------- 估计版本（头表） --
CREATE TABLE IF NOT EXISTS exposure_estimates (
    estimate_id         UUID PRIMARY KEY,
    estimate_version    INTEGER NOT NULL,            -- 同输入参数的版本号，修订只增版本
    snapshot_id         UUID NOT NULL REFERENCES exposure_input_snapshots(snapshot_id),
    window_start        TIMESTAMPTZ NOT NULL,
    window_end          TIMESTAMPTZ NOT NULL,
    evidence_cutoff_at  TIMESTAMPTZ NOT NULL,
    privacy_threshold   INTEGER NOT NULL,
    boundary_id         TEXT REFERENCES region_boundaries(boundary_id),
    baseline_pop_as_of  DATE NOT NULL,               -- 固定的常住人口基准日
    method_note         TEXT NOT NULL,               -- 校准方案/保守方案的适用说明
    supersedes_estimate_id UUID REFERENCES exposure_estimates(estimate_id),
    created_by          TEXT NOT NULL DEFAULT 'analyst',
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE UNIQUE INDEX IF NOT EXISTS uq_ee_window_version
    ON exposure_estimates (window_start, window_end, evidence_cutoff_at,
                           estimate_version, COALESCE(boundary_id, ''));
CREATE INDEX IF NOT EXISTS idx_ee_window ON exposure_estimates(window_start, window_end);

-- --------------------------------------------- 网格 × 时段区间结果 -------------
-- 净变化 delta = 临时增加 - 临时减少（分别保留区间，不互相吞掉）。
-- 恒等关系：upper-lower = point_unknown + interval_unknown。
-- 叠加后可达需求 = 常住人口基线 + delta 区间。
CREATE TABLE IF NOT EXISTS exposure_grid_periods (
    period_id           BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    estimate_id         UUID NOT NULL REFERENCES exposure_estimates(estimate_id),
    grid_id             TEXT NOT NULL REFERENCES pop_grids(grid_id),
    window_start        TIMESTAMPTZ NOT NULL,
    window_end          TIMESTAMPTZ NOT NULL,
    baseline_population DOUBLE PRECISION NOT NULL,   -- 冻结基线（固定分母）
    increase_lower      DOUBLE PRECISION NOT NULL,   -- 临时增加下界
    increase_upper      DOUBLE PRECISION,            -- 临时增加上界；NULL=覆盖缺口导致不可估计
    decrease_lower      DOUBLE PRECISION NOT NULL,   -- 临时减少下界
    decrease_upper      DOUBLE PRECISION NOT NULL,   -- 临时减少上界（转移登记为确定值）
    delta_lower         DOUBLE PRECISION NOT NULL,   -- 净变化下界
    delta_upper         DOUBLE PRECISION,            -- 净变化上界；NULL=不可估计
    point_unknown       DOUBLE PRECISION NOT NULL,   -- 抑制/无观测导致的点状未知（受抑制来源条数）
    interval_unknown    DOUBLE PRECISION,            -- 结构性未知宽度；NULL=上界不可估计，无法数值化
    sampling_margin     DOUBLE PRECISION NOT NULL DEFAULT 0, -- 抽样误差半宽（人）
    method_mode         TEXT NOT NULL CHECK (method_mode IN
                        ('calibrated','conservative','mixed')),
    reachable_baseline  DOUBLE PRECISION,            -- 基线可达人口（来自覆盖任务，可空）
    demand_lower        DOUBLE PRECISION,            -- 基线叠加后可达需求下界
    demand_upper        DOUBLE PRECISION,            -- 基线叠加后可达需求上界
    computed_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (estimate_id, grid_id)
);
CREATE INDEX IF NOT EXISTS idx_egp_estimate ON exposure_grid_periods(estimate_id);

-- --------------------------------------------- 区间采用证据（逐来源留痕） ------
CREATE TABLE IF NOT EXISTS exposure_evidence_items (
    evidence_id         BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    estimate_id         UUID NOT NULL REFERENCES exposure_estimates(estimate_id),
    grid_id             TEXT NOT NULL,
    source_kind         TEXT NOT NULL,               -- mobile/scenic/boarding_school/transfer/overlap
    source_ref          TEXT NOT NULL,               -- 来源行标识（sample_id/presence_id…）
    observed_count      DOUBLE PRECISION,            -- 原始观测（抑制时 NULL）
    adopted_lower       DOUBLE PRECISION NOT NULL,
    adopted_upper       DOUBLE PRECISION,            -- NULL=该来源上界不可估计
    suppressed          BOOLEAN NOT NULL DEFAULT FALSE,
    coverage_value      DOUBLE PRECISION,
    calibrated          BOOLEAN NOT NULL DEFAULT FALSE,
    note                TEXT NOT NULL,
    UNIQUE (estimate_id, grid_id, source_kind, source_ref)
);
