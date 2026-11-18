"""请求/响应模型。"""
from __future__ import annotations

from datetime import date, datetime
from typing import Optional

from pydantic import BaseModel, Field


class ScenarioCreate(BaseModel):
    name: str = "临时情景"
    population_as_of: date
    departure_time: datetime
    max_trip_minutes: int = Field(gt=0, le=600)
    road_cutoff: datetime
    boundary_id: Optional[str] = None
    viewer_id: str = "analyst-ui"
    note: Optional[str] = None


class ViewerRelease(BaseModel):
    viewer_id: str


class CompareRequest(BaseModel):
    task_a_id: str
    task_b_id: str


class PublishRequest(BaseModel):
    task_a_id: str
    task_b_id: str
    title: str
    created_by: str = "analyst"
    exposure_estimate_id: Optional[str] = None  # 关联的临时暴露估计版本（输入快照）


class PrecomputeRequest(BaseModel):
    population_as_of: date
    base_day: datetime
    cutoff: datetime
    deadline_minutes: int = 60
    offsets_minutes: list[int] = [0, 30, 60, 90, 120]
    boundary_id: Optional[str] = None


class InvalidateRequest(BaseModel):
    edge_id: str
    reason: str = "道路事件修订"


class RebuildRequest(BaseModel):
    new_cutoff: datetime


class EventCreate(BaseModel):
    """临时封路/限速：立即参与后续即时计算。"""

    event_id: str
    event_type: str  # closure | speed_limit
    edge_id: str
    effective_from: datetime
    effective_to: Optional[datetime] = None
    speed_limit_kmh: Optional[float] = None
    source_title: str = "临时上报事件"
    raw_text: str = ""


# ---------------------------------------------------------------- 临时暴露人口 --
class ExposureEstimateCreate(BaseModel):
    window_start: datetime
    window_end: datetime
    evidence_cutoff: datetime
    privacy_threshold: int = Field(default=10, ge=0)
    boundary_id: Optional[str] = None
    baseline_pop_as_of: date
    created_by: str = "analyst-ui"
    reachable_task_id: Optional[str] = None  # 叠加到基线可达需求时指定覆盖任务


class MobileSignalCreate(BaseModel):
    """移动信号采样登记（覆盖率下降/隐私抑制通过字段表达）。"""

    operator_id: str
    grid_id: str
    stratum: str = "visitor"
    window_start: datetime
    window_end: datetime
    sample_count: Optional[float] = None
    suppressed: bool = False
    operator_coverage: float = Field(gt=0, le=1)
    coverage_stable: bool = True
    field_verified: bool = False
    source_title: str = "移动信号采样上报"
    raw_text: str = ""


class VenuePresenceCreate(BaseModel):
    """景区/寄宿学校在场登记。"""

    venue_id: str
    venue_name: str
    presence_kind: str  # scenic | boarding_school
    grid_id: str
    window_start: datetime
    window_end: datetime
    verified_count: Optional[float] = None
    privacy_suppressed: bool = False
    source_title: str = "场所在场登记"
    raw_text: str = ""


class TransferCreate(BaseModel):
    """临时转移登记（晚到登记用 late_arrival 标记）。"""

    grid_id: str
    window_start: datetime
    window_end: datetime
    registered_persons: float = Field(ge=0)
    destination_grid_id: Optional[str] = None
    late_arrival: bool = False
    source_title: str = "转移安置登记"
    raw_text: str = ""


class OverlapRuleCreate(BaseModel):
    """可证明的跨来源人员重叠关系（须有制度或现场核验依据）。"""

    grid_id: str
    source_a: str  # mobile / scenic / boarding_school / transfer
    source_b: str
    relation: str  # disjoint | subset_of | overlap_known
    direction: str = "none"  # a_in_b | b_in_a | none
    min_overlap_persons: float = Field(default=0, ge=0)
    max_overlap_persons: Optional[float] = None
    evidence_note: str
    evidence_cutoff: Optional[datetime] = None
