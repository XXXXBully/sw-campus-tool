#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
rungen —— 跑步记录生成器
=========================

模块:
    core    数据模型 / 地理工具 / 跑者画像
    route   路径规划 (打卡点必经 + 样条平滑)
    engine  生成引擎 (速度曲线 / 生理数据 / 校验)

快速开始::

    from rungen import generate_record

    rec = generate_record(
        start_lat=30.1234, start_lon=104.1234,
        distance_km=3.0,
        start_time="2026-09-15 07:30:00",
        checkpoints=[("一号点", 30.1245, 104.1255),
                     ("二号点", 30.1225, 104.1215)],
    )
    print(rec.summary())
"""
from .core import (
    GeoPoint, Split, Checkpoint, TrackRecord, RunnerProfile,
    haversine, bearing, dest_point, add_gps_noise,
    fmt_pace, fmt_duration, parse_pace,
    DEFAULT_SAMPLE_INTERVAL,
)
from .route import (
    RouteMode, plan_route, catmull_rom,
    nearest_index, polyline_length, min_tour_length,
    SHAPE_ELLIPSE, SHAPE_TRACK, SHAPES,
)
from .engine import RunningGenerator, generate_record

__all__ = [
    "GeoPoint", "Split", "Checkpoint", "TrackRecord", "RunnerProfile",
    "haversine", "bearing", "dest_point", "add_gps_noise",
    "fmt_pace", "fmt_duration", "parse_pace", "DEFAULT_SAMPLE_INTERVAL",
    "RouteMode", "plan_route", "catmull_rom",
    "nearest_index", "polyline_length", "min_tour_length",
    "SHAPE_ELLIPSE", "SHAPE_TRACK", "SHAPES",
    "RunningGenerator", "generate_record",
]

__version__ = "1.0.0"
