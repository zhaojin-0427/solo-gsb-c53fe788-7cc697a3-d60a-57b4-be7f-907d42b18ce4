"""请求/响应数据模型（字段英文，错误信息中文）。"""

from __future__ import annotations

from typing import Optional

from pydantic import BaseModel, Field


class Point(BaseModel):
    x: float
    y: float


class Room(BaseModel):
    width: float = Field(..., description="房间宽度（米）")
    height: float = Field(..., description="房间高度（米）")


class Microphone(BaseModel):
    x: float
    y: float
    label: Optional[str] = None


class CalibrationEvent(BaseModel):
    position: Point
    # 长度等于麦克风数的各麦到达时刻（秒，独立时钟各自计时）
    arrivals: list[float] = Field(..., min_length=4, max_length=8)


class CandidatePeak(BaseModel):
    time: float = Field(..., description="该麦独立时钟下的候选峰时刻（秒）")
    strength: float = Field(0.0, description="峰强度（仅展示，不参与几何裁决）")


class TargetTap(BaseModel):
    # 外层按麦克风排列，每个麦 1～12 个候选峰
    candidates: list[list[CandidatePeak]] = Field(..., min_length=4, max_length=8)


class LocateRequest(BaseModel):
    room: Room
    microphones: list[Microphone] = Field(..., min_length=4, max_length=8)
    sound_speed: float = Field(..., description="声速 m/s，必须为正")
    drift_ppm: float = Field(..., description="相对首麦的时钟漂移上限（ppm）")
    tdoa_tolerance_ms: float = Field(..., description="时差容限（毫秒）")
    resolution: float = Field(..., description="位置分辨率（米）")
    calibrations: list[CalibrationEvent] = Field(..., min_length=2, max_length=2)
    target: TargetTap
