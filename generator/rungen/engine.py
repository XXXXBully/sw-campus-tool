#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
rungen/engine.py —— 跑步记录生成引擎

流程:
    1. 时间轴规划: 目标距离/时长 -> 采样点数
    2. 速度曲线: 起步加速 + 稳态波动 + 疲劳衰减 + 红绿灯减速
    3. 路线几何: 打卡点必经 + 样条平滑 + 距离对齐
    4. 逐点推演: 速度积分 -> 真实间距 -> 经纬度/海拔/心率/步频/步幅
    5. 指标汇总 + 每公里分段 + isValidPoint 全量校验
"""
from __future__ import annotations

import math
import os
import random
import time
from datetime import datetime, timedelta
from typing import List, Tuple, Optional, Sequence

from isvalidpoint import isValidPoint, total_score

from .core import (
    GeoPoint, Split, Checkpoint, TrackRecord, RunnerProfile,
    haversine, bearing, dest_point, add_gps_noise, fmt_pace, fmt_duration,
    DEFAULT_SAMPLE_INTERVAL, IVP_W2, IVP_W3, IVP_W4, IVP_W5,
)
from .route import (RouteMode, plan_route, nearest_index, min_tour_length,
                    add_sample_noise, SHAPE_ELLIPSE, SHAPE_TRACK, SHAPES)


class RunningGenerator:
    """
    跑步记录生成器。

    用法::

        gen = RunningGenerator(
            start=(30.1234, 104.1234),
            distance_km=3.0,
            start_time="2026-09-15 07:30:00",
            checkpoints=[("一号点", 30.1245, 104.1255),
                         ("二号点", 30.1225, 104.1215)],
        )
        rec = gen.generate()
        print(rec.summary())
    """

    def __init__(self,
                 start: Tuple[float, float],
                 distance_km: float,
                 start_time,
                 checkpoints: Optional[Sequence] = None,
                 profile: Optional[RunnerProfile] = None,
                 mode: str = RouteMode.LOOP,
                 end: Optional[Tuple[float, float]] = None,
                 sample_interval_s: float = DEFAULT_SAMPLE_INTERVAL,
                 seed: Optional[int] = None,
                 target_duration_s: Optional[float] = None,
                 noise_sigma_m: float = 1.6,
                 shape: str = SHAPE_TRACK,
                 ):

        self.start = (float(start[0]), float(start[1]))
        self.distance_m = float(distance_km) * 1000.0
        self.mode = mode
        self.end = end
        self.dt = float(sample_interval_s)
        self.noise_sigma_m = noise_sigma_m
        # ★★ 轨迹形状（2026-09-29）：ellipse = 现状圆润环；track = 标准田径场
        self.shape = shape if shape in SHAPES else SHAPE_ELLIPSE

        # 打卡点
        self.checkpoints: List[Checkpoint] = []
        for cp in (checkpoints or []):
            if isinstance(cp, Checkpoint):
                self.checkpoints.append(cp)
            elif isinstance(cp, dict):
                self.checkpoints.append(Checkpoint(
                    name=cp.get("name", f"打卡点{len(self.checkpoints)+1}"),
                    lat=float(cp["lat"]), lon=float(cp["lon"]),
                    radius_m=float(cp.get("radius_m", 30.0)),
                ))
            else:
                name, la, lo = cp[0], cp[1], cp[2]
                radius = cp[3] if len(cp) > 3 else 30.0
                self.checkpoints.append(Checkpoint(name, float(la), float(lo), float(radius)))

        # 开始时间
        self.start_time = self._parse_time(start_time)

        self.profile = profile or RunnerProfile()
        self.target_duration_s = target_duration_s

        # 随机源
        # ★ 默认种子必须带熵（2026-09-19 修复）：
        #   原先 `random.Random(int(start_time.timestamp()))` 让轨迹完全由
        #   起跑时间决定 —— 起跑时间相同则轨迹**逐字节相同**。GUI 页面不刷新
        #   时「开始时间」输入框的值不会变，于是连续几次跑步生成出一模一样的
        #   轨迹（用户实测「每次轨迹都一样」）。
        #   现在默认混入纳秒时钟 + 进程号，保证每次生成的路线形状不同；
        #   显式传 seed 时仍完全可复现（调试用）。
        if seed is not None:
            self.rng = random.Random(seed)
        else:
            entropy = (int(self.start_time.timestamp()) * 1000003
                       ^ time.time_ns() ^ (os.getpid() << 17))
            self.rng = random.Random(entropy)

        # 结果缓存
        self.all_points_valid = False
        self.min_point_score = 0
        self.laps = 1
        self.warnings: List[str] = []

    # ------------------------------------------------------------------
    # 时间解析
    # ------------------------------------------------------------------
    @staticmethod
    def _parse_time(t) -> datetime:
        if isinstance(t, datetime):
            dt = t.replace(microsecond=0)
        elif isinstance(t, (int, float)):
            dt = datetime.fromtimestamp(t).replace(microsecond=0)
        else:
            s = str(t).strip()
            fmts = ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y/%m/%d %H:%M:%S",
                    "%Y/%m/%d %H:%M", "%m-%d %H:%M", "%H:%M:%S", "%H:%M")
            dt = None
            for f in fmts:
                try:
                    dt = datetime.strptime(s, f)
                    if "%Y" not in f:                # 补全年月日
                        now = datetime.now()
                        dt = dt.replace(year=now.year, month=now.month,
                                        day=now.day)
                    dt = dt.replace(microsecond=0)
                    break
                except ValueError:
                    continue
            if dt is None:
                raise ValueError(f"无法解析时间: {t!r}")
        # ★ 硬护栏：起跑时间绝不允许在未来 —— 无论来自 GUI/CLI/配置文件，
        #   一律回退到当前时间（否则轨迹点 ts 与提交体 startTime 落未来，
        #   服务端会判异常并触发风控）。
        now = datetime.now().replace(microsecond=0)
        if dt > now:
            dt = now
        return dt

    # ------------------------------------------------------------------
    # 1. 速度曲线
    # ------------------------------------------------------------------
    def _speed_profile(self, n: int) -> List[float]:
        """
        生成 n 个采样点的瞬时速度 (m/s)。

        ★★ 2026-10-08 重做：**曲线必须"平"**（用户真机反馈「配速图波动很大」）
          旧参数下实测（2.0km / 5:40 配速 / 137 点）：
            · 逐点瞬时配速 **51%** 的点偏离平均配速超过 35s
              （min 235 / max 543 s/km，均值 339）；
            · 而速度曲线本身只有 3%（全是起步那 4 个点）。
          真机参考记录（同距离）的配速图基本是**一条平线** ——
          用户要求「所有速度都在平均配速 ±35s 内」。

          构成（幅度按"±35s 容差"反推；340 s/km 基线下 35s ≈ −9.3% / +11.5%）：
            · 起步加速 (前 3%，最慢 0.92× ⇒ +30s，正好落在容差内)
            · 疲劳衰减 (后 40%，幅度随 fitness 减小，最多 −2.5%)
            · 多周期波动 (步态 / 呼吸，合计 ≤ 3.9%)
            · 地形起伏 (长周期，≤ 1.4%)
            · 随机微扰 (σ=1.6%，再经移动平均压到 ~0.7%)
            · 偶发减速 (约每 130 点一次，只降 4~7% ⇒ ≤ +26s)
          实测 6 个种子（2.0km / 5:40）：逐点配速落在 **324~359 s/km**
          （相对平均 340 的偏差 −16 ~ +19s），**0% 的点**超出 ±35s。

        ★★ 末了必须**归一化到基线速度**（`mean(profile) == base_speed`）。
          不能省：`_walk` 的时间步长总和 = 距离 / 基线速度，
          逐点速度若整体偏低，详情页「平均配速」就会与逐点曲线对不上
          （旧版实测 profile 均值只有基线的 **0.9776**，即偏慢 2.2%）。
        """
        base = self.profile.base_speed
        fit = self.profile.fitness
        rng = self.rng
        speeds: List[float] = []

        for i in range(n):
            t = i / max(1, n - 1)

            # 起步加速（幅度收到 0.92 —— 再低就会顶破 ±35s 容差）
            warm = 0.92 + 0.08 * (t / 0.03) if t < 0.03 else 1.0

            # 疲劳衰减（最多 −2.5%）
            if t > 0.6:
                fade = 1.0 - (0.025 * (1.0 - fit)) * ((t - 0.6) / 0.4)
            else:
                fade = 1.0

            # 多周期波动（合计 ≤ 3.9%）
            wave = (1.0
                    + 0.020 * math.sin(2 * math.pi * t * 7.0)
                    + 0.013 * math.sin(2 * math.pi * t * 23.0 + 1.1)
                    + 0.006 * math.sin(2 * math.pi * t * 61.0))

            # 地形起伏 (长周期, ≤ 1.4%)
            terrain = 1.0 + 0.014 * math.sin(2 * math.pi * t * 1.8 + 0.4)

            noise = rng.gauss(0, 0.016)

            v = base * warm * fade * wave * terrain * (1 + noise)

            # 偶发减速: 约每 130 点一次，幅度 4~7%（≤ +26s，不破容差）
            if rng.random() < 1.0 / 130:
                v *= rng.uniform(0.93, 0.96)

            speeds.append(max(0.6, v))

        # 平滑
        w = max(2, n // 120)
        sm = []
        for i in range(n):
            a, b = max(0, i - w), min(n, i + w + 1)
            sm.append(sum(speeds[a:b]) / (b - a))

        # ★ 归一化：均值必须等于基线速度（见 docstring）
        m = sum(sm) / len(sm)
        if m > 1e-9:
            sm = [v * base / m for v in sm]
        return sm

    # ------------------------------------------------------------------
    # 2. 海拔曲线
    # ------------------------------------------------------------------
    def _make_elevation(self, cum_dists: Sequence[float]) -> List[float]:
        """按累计距离生成海拔剖面 (长坡 + 短起伏 + 微噪声)"""
        rng = self.rng
        base = rng.uniform(15, 130)
        a1 = rng.uniform(2.5, 9.0)     # 长坡幅度
        a2 = rng.uniform(0.8, 2.5)     # 短起伏幅度
        p1 = rng.uniform(0, 2 * math.pi)
        p2 = rng.uniform(0, 2 * math.pi)
        w1 = rng.uniform(700, 1400)    # 长坡波长
        w2 = rng.uniform(180, 320)     # 短起伏波长
        out = []
        for d in cum_dists:
            e = (base
                 + a1 * math.sin(2 * math.pi * d / w1 + p1)
                 + a2 * math.sin(2 * math.pi * d / w2 + p2)
                 + rng.gauss(0, 0.35))
            out.append(e)
        return out

    # ------------------------------------------------------------------
    # 3. 主生成
    # ------------------------------------------------------------------
    def generate(self) -> TrackRecord:
        prof = self.profile
        rng = self.rng

        # --- 可行性检查: 目标距离必须够走完所有打卡点 ---
        # ★ 之前用 "2.2 × 最远打卡距离" 估算, 会严重低估:
        #   打卡点散在四周时, 按方位角排序会绕出 3.4km, 而真实最短
        #   访问顺序只要 1.6km —— 估算不足就会导致"一个卡都打不到"。
        #   这里直接求一遍近似最短访问顺序 (TSP), 得到真实下界。
        if self.checkpoints:
            wps = [(c.lat, c.lon) for c in self.checkpoints]
            need = min_tour_length(self.start, wps, mode=self.mode, end=self.end)
            # 路径必然比直线折线长 (转弯/绕行), 留 12% 余量
            min_needed = need * 1.12
            if self.distance_m < min_needed:
                self.warnings.append(
                    f"目标距离 {self.distance_m/1000:.2f}km 不足以经过全部 "
                    f"{len(self.checkpoints)} 个打卡点 (最短需 "
                    f"{min_needed/1000:.2f}km)。已自动上调到 "
                    f"{min_needed/1000:.2f}km。")
                self.distance_m = min_needed

        # --- 估算总时长 & 采样点数 ---
        if self.target_duration_s:
            est_dur = float(self.target_duration_s)
            self.distance_m = prof.base_speed * est_dur
        else:
            # ★ 时长按【输入配速】精确反算 (2026-09-24 修复)
            #   原先这里乘了 0.97 的"起步/疲劳余量"，副作用是**实际均速比输入配速
            #   慢约 2~3%**：用户填 5:37，记录里却是 5:45，界面「总用时 ≈」也对不上。
            #   速度曲线的起步/疲劳形态由 _speed_profile 负责，总时长不该再额外加码。
            est_dur = self.distance_m / prof.base_speed

        # 用 round 而非 int：让总时长更贴近 est_dur（误差 < 半个采样间隔）
        n = max(20, int(round(est_dur / self.dt)) + 1)
        # ★ 交给 _walk 复用同一个目标时长：_walk 会据此把采样步长归一化，
        #   使**总时长精确等于 est_dur**（不再有 ±2~3 秒的随机游走尾巴）。
        self._est_dur = est_dur

        # --- 速度曲线 ---
        speeds = self._speed_profile(n)

        # --- 路线几何 ---
        waypoints = [(c.lat, c.lon) for c in self.checkpoints]
        # ★★ GPS 抖动**不在这里加** (2026-09-25 修复)
        #    `plan_route` 的几何是每 ~2 米一个点, 在这上面叠 1.6m 抖动会把
        #    折线弧长虚增 **29.5%** (实测弦长 2050m / 平滑后真实 1583.6m)。
        #    下游 `_align_geometry` 重采样到 n 点 (~15m 间距) 会把抖动平均掉,
        #    长度回到真实的 ~1554m, 于是 `_correct_length` 只好**放大 1.32×**
        #    去凑目标距离 —— 整体放大把打卡点沿径向推离路线 (离环心最远的
        #    那个被推出去 13m, 越过 App 的 15m 打卡半径 → 手机端地图显示
        #    "根本没经过打卡点"), 形状也被撑成带尖刺的乱麻。
        #    抖动改在 `_align_geometry` **之后**加 (见下), 幅度不变而弧长
        #    只虚增 ~1%, 标定倍数回到 ~1.01, 打卡点几乎不动。
        coords, self.laps = plan_route(self.start, waypoints, self.distance_m,
                                       mode=self.mode, end=self.end, rng=rng,
                                       noise_sigma_m=0.0, shape=self.shape)

        # 几何点数与采样点数对齐: 将几何按弧长重采样到 n 点
        coords = self._align_geometry(coords, n, speeds)

        # ★ 在最终采样点上叠加 GPS 抖动 (切向为主 + 25% 横向微扰)
        #   首点不动; 闭环的末点跟随首点, 闭合精度不受影响。
        coords = add_sample_noise(coords, self.noise_sigma_m, rng)

        # ★ 重采样必然"削掉"高曲率路段的弧长 (折返/多圈路线实测损失 4~11%,
        #   因为密集的折返点被抽样跳过了)。这里把对齐后的折线**整体缩放**
        #   回目标长度, 首末点保持不动 —— 这一步之后总距离就精确了。
        coords = self._correct_length(coords, self.distance_m)

        # ★★ 闭环末段"假尖峰"治理 —— 必须挪到标定**之前** (2026-09-25 修复)
        #    闭环要求末点严格回到起点, 于是 "几何总长 /(n-1)" 除不尽的
        #    残差全部堆在**最后一段**上: 实测末段 27.7m 而中位 15.3m,
        #    _walk 据此算出 5.54 m/s 的假瞬时速度 (真值 3.03), 步幅被
        #    顶到 189cm —— 单点异常污染整份记录。
        #    做法: 把末段的超出部分按权重摊到前面若干个点上 (末点不动),
        #    使各段间距趋于均匀。
        #    ★ 原先它在下面的闭环标定**之后**执行, 于是它带来的长度变化
        #      完全逃过了标定 —— 实测标定已收敛到 err ≤0.2%, 但摊平之后
        #      又偏出 -6.3m (0.31%), 2.00km 的请求会掉到服务端 2000m 下限
        #      以下。放到标定之前, 这一步的长度影响就会被标定一起收掉。
        if self.mode == RouteMode.LOOP and len(coords) >= 6:
            coords = self._flatten_tail_segment(coords)

        # --- 逐点推演 ---
        # ★★ _walk 会因 min_gap 抬升而**系统性偏长** (2026-09 修复)
        #    `_walk` 在间距小于 `speed*dt*0.55` 时会把该点沿路径外推 ——
        #    这是为了避免"折返处几何打结"导致假速度尖峰。但它有个副作用:
        #    被外推的点成为下一个点的 `prev`, 于是下一段变短、又触发外推,
        #    形成**链式抬升**。实测 1500m 目标走成 1542m (+2.8%), 极端
        #    情况下 (240s/km 快配速 + 密集折返) 能到 +6.2%。
        #    做法: 用 _walk 的实际输出做闭环标定 —— 量出偏长比例, 把
        #    coords 的目标长反向下调 `distance_m / k`, 再走一遍, 迭代
        #    几次即收敛到 <0.2%。
        points, cum = self._walk(coords, speeds, n)
        if not points:
            raise RuntimeError("未生成任何轨迹点")
        # ★ 迭代上限 8 (原 5)：摊平末段并入标定后收敛稍慢, 留足余量,
        #   让距离偏差稳在 verify_pkg 的 ±5m 门槛内。
        for _ in range(8):
            if cum <= 1e-6:
                break
            # ★★ 收敛判据必须用**绝对偏差**, 不能用相对偏差 (2026-09-25 修复)
            #   原来 `abs(err) <= 0.002` 是 0.2% —— 5km 上等于 **10m**,
            #   2km 上也有 4m。而 `_walk` 的 min_gap 抬升会带来 0.6m 级的
            #   系统性偏长 (几何 5000.0000 走出 5000.6329, 3 个点被抬升),
            #   完全落在 0.2% 阈值**之内**, 于是标定循环**一轮都不跑**就退出,
            #   零头全堆到末圈上 —— verify_pkg A5/C5 实测末圈 1000.63m
            #   (要求 1000±0.5m)。旧实现之所以看不出这个洞, 只是因为它的
            #   初始 _walk 恰好没有抬升点、偏差本来就是 0。
            #   区间取 [-1e-6, +0.5]:
            #     · 下界不许欠长 —— unid 3305 单次下限 2000m, 欠长会被服务端拒;
            #     · 上界 0.5m 以内, 保证末圈不会超出 1000±0.5m 的容差。
            dev = cum - self.distance_m
            if -1e-6 <= dev <= 0.5:
                break
            # 下一轮的几何目标长: 当前几何长 × (目标/实测)
            cur_geo = sum(haversine(coords[i - 1][0], coords[i - 1][1],
                                    coords[i][0], coords[i][1])
                          for i in range(1, len(coords)))
            nxt = cur_geo * (self.distance_m / cum)
            coords = self._correct_length(coords, max(1.0, nxt))
            points, cum = self._walk(coords, speeds, n)
            if not points:
                raise RuntimeError("未生成任何轨迹点")

        real_dist = points[-1].dist_from_start
        real_dur = (points[-1].ts_ms - points[0].ts_ms) / 1000.0

        # --- 海拔 ---
        eles = self._make_elevation([p.dist_from_start for p in points])
        for p, e in zip(points, eles):
            p.ele = e

        # --- 生理数据 ---
        self._fill_physiology(points, real_dist, real_dur)

        # --- 汇总 ---
        rec = self._build_record(points, real_dist, real_dur)

        # --- 打卡点命中 ---
        rec.checkpoints = self._resolve_checkpoints(points)

        # --- isValidPoint 校验 ---
        self._validate(points)
        rec.all_points_valid = self.all_points_valid
        rec.min_point_score = self.min_point_score
        rec.warnings = list(self.warnings)

        return rec

    # ------------------------------------------------------------------
    def _correct_length(self, coords: List[Tuple[float, float]],
                        target_m: float) -> List[Tuple[float, float]]:
        """
        把重采样后的折线整体缩放回目标长度 (首末点保持不动)。

        为什么需要:
            重采样是"按等弧长取点", 高曲率区段 (折返、绕圈) 的点会被
            跳过, 于是总弧长系统性偏短。闭环路线可以围绕起点缩放;
            开口路线的两端都是硬约束, 所以围绕两端点连线的中点缩放,
            再把残差用幂曲线摊掉 —— 与 route.py 里的做法一致。
        """
        if len(coords) < 3 or target_m <= 0:
            return coords
        cur = sum(haversine(coords[i - 1][0], coords[i - 1][1],
                            coords[i][0], coords[i][1])
                  for i in range(1, len(coords)))
        if cur <= 1e-6:
            return coords

        closed = self.mode == RouteMode.LOOP
        if closed:
            # ★ 闭环必须围绕**环心**缩放, 不能围绕起点:
            #   起点同时也是终点, 以它为不动点缩放会把倒数第二个点
            #   推离终点, 从而在末段撕开一个 2 倍的大缝 (实测末段
            #   28.8m vs 中位 15.8m), _walk 据此算出 5.8m/s 的假速度,
            #   步幅被顶到 195cm。
            #   围绕环心缩放则各点等比例伸缩, 间距分布保持均匀,
            #   最后再把首点平移回原位即可。
            n_pts = len(coords) - 1          # 不含重复的闭合点
            cx = sum(coords[i][0] for i in range(n_pts)) / n_pts
            cy = sum(coords[i][1] for i in range(n_pts)) / n_pts
            k = target_m / cur
            out = [(cx + (a - cx) * k, cy + (b - cy) * k) for a, b in coords]
            # 把首点平移回精确的起点
            la0, lo0 = coords[0]
            d0a, d0b = la0 - out[0][0], lo0 - out[0][1]
            out = [(a + d0a, b + d0b) for a, b in out]
            # ★ 末点**不要**硬钉回起点:
            #   硬钉会在末段撕开一个 2 倍的大缝 (28.8m vs 中位 15.8m),
            #   _walk 据此算出的瞬时速度 5.8m/s 会把步幅顶到 195cm。
            #   这里改成把残差按最大 8% 的间距误差分摊到最后 6 个点上,
            #   既保证闭合 (误差 < 1m, GPS 精度内), 又保持间距均匀。
            gap = haversine(out[-1][0], out[-1][1], out[0][0], out[0][1])
            seg_med = cur / max(1, len(out) - 1)
            if gap > seg_med * 0.5:
                # 把末点朝起点方向"补齐", 但用平滑方式分摊
                brg = bearing(out[-1][0], out[-1][1], out[0][0], out[0][1])
                out[-1] = dest_point(out[-1][0], out[-1][1], brg, gap * 0.5)
            else:
                out[-1] = out[0]
            return out

        # 开口: 围绕两端点中点缩放, 首末点回位
        p0, pN = coords[0], coords[-1]
        cx = (p0[0] + pN[0]) * 0.5
        cy = (p0[1] + pN[1]) * 0.5
        out = list(coords)
        for _ in range(6):
            cur = sum(haversine(out[i - 1][0], out[i - 1][1],
                                out[i][0], out[i][1])
                      for i in range(1, len(out)))
            if cur <= 1e-6:
                break
            k = target_m / cur
            if abs(k - 1.0) < 1e-5:
                break
            out = [(cx + (a - cx) * k, cy + (b - cy) * k) for a, b in out]
            # 首点回位
            d0a, d0b = p0[0] - out[0][0], p0[1] - out[0][1]
            out = [(a + d0a, b + d0b) for a, b in out]
            # 末点回位 (残差按 2.2 次幂摊到全程)
            ra, rb = pN[0] - out[-1][0], pN[1] - out[-1][1]
            if abs(ra) > 1e-12 or abs(rb) > 1e-12:
                m = len(out) - 1
                out = [(a + ra * (i / m) ** 2.2, b + rb * (i / m) ** 2.2)
                       for i, (a, b) in enumerate(out)]
        return out

    # ------------------------------------------------------------------
    def _flatten_tail_segment(self, coords: List[Tuple[float, float]],
                              lookback: int = 8,
                              ) -> List[Tuple[float, float]]:
        """
        闭环: 把**末段**超出中位值的部分, 摊到最后 lookback 个点上。

        问题:
            闭环末点必须等于起点, 于是弧长除不尽的残差全部堆在最后一段。
            实测末段 27.7m vs 中位 15.3m —— `_walk` 据此算出 5.54 m/s 的
            假瞬时速度 (真值 3.03), 步幅冲到 189cm。

        做法:
            1. 取末段前面若干段的**中位间距** med
            2. 若末段 seg_last 明显大于 med (阈值 1.35×), 把超出量
               `excess = seg_last - med` 按线性权重分摊到:
                 · 前面 `lookback` 段的**末顶点** 上 (把它们各自向终点
                   方向"缩"一点, 从而把末段的长度挪走)
            3. 末点本身不动, 所以闭环性保持; 总长变化 < 1m
        """
        out = [tuple(p) for p in coords]
        n = len(out)
        if n < lookback + 3:
            return out

        def seg_at(i):       # 第 i-1 -> i 段的长度
            return haversine(out[i - 1][0], out[i - 1][1],
                             out[i][0], out[i][1])

        segs = [seg_at(i) for i in range(1, n)]
        if not segs:
            return out
        med = sorted(segs)[len(segs) // 2]
        if med <= 1e-6:
            return out
        last = segs[-1]
        if last <= med * 1.35:
            return out

        excess = last - med
        # ★ 直接把"末顶点前的那个点"(out[n-2]) 朝末点方向推进 excess 米,
        #   这样末段就精确变成 med。但推进 out[n-2] 会把倒数第二段
        #   (n-3 -> n-2) **拉长** excess —— 所以再把这个"拉长量"往前
        #   一段传, 如此逐段向前摊, 每段的拉长量按几何衰减 (0.55^i),
        #   传 8 段之后基本归零。末点全程不动, 闭环性不受影响。
        shift = excess
        idx = n - 2
        depth = 0
        while idx > 0 and shift > 0.05 and depth < lookback:
            curp = out[idx]
            nxt = out[idx + 1]
            sl = haversine(curp[0], curp[1], nxt[0], nxt[1])
            if sl <= 1e-9:
                break
            # 向前推进量不能超过 60% 段长, 否则点位会跳过去
            step = min(shift, sl * 0.6)
            f = step / sl
            out[idx] = (curp[0] + (nxt[0] - curp[0]) * f,
                        curp[1] + (nxt[1] - curp[1]) * f)
            # 上一段被拉长了 step, 按衰减系数继续往前传
            shift = step * 0.55
            idx -= 1
            depth += 1
        return out

    # ------------------------------------------------------------------
    def _smooth_closing_gap(self, coords: List[Tuple[float, float]],
                            speeds: List[float],
                            lookback: int = 6) -> List[Tuple[float, float]]:
        """
        把闭环最后一段"多出来的间距"平滑摊到最后 lookback 个点上。

        问题:
            末点必须严格等于起点, 于是"几何总长 / (n-1)" 的除不尽残差
            全部落在最后一段。若末段达到中位间距的 1.8 倍, `_walk` 会
            据此算出 5.8 m/s 的瞬时速度 (平均才 3.0), 步幅随之被顶到
            195cm —— 单点异常会污染整份记录。

        做法:
            1. 算出期望间距 median = 平均(seg)
            2. 若末段 seg_last 明显偏大, 把超出部分按权重摊到
               最后 lookback 段的中间点上 (末点不动, 总长几乎不变)

        仅在闭环时生效 (开口路径的首末点本就是不同位置)。
        """
        if self.mode != RouteMode.LOOP or len(coords) < lookback + 3:
            return coords

        out = [tuple(p) for p in coords]
        m = len(out) - 1
        segs = [haversine(out[i - 1][0], out[i - 1][1], out[i][0], out[i][1])
                for i in range(1, len(out))]
        if not segs:
            return out

        avg = sum(segs) / len(segs)
        if avg <= 1e-6 or segs[-1] <= avg * 1.35:
            return out

        excess = segs[-1] - avg
        rng_pts = min(lookback, m - 1)
        if rng_pts < 2:
            return out

        # 权重: 越靠近末尾权重越大 (平滑钟形)
        w = [i + 1 for i in range(rng_pts)]
        wsum = float(sum(w))
        # 沿路径方向把每个点"往起点方向"挪, 从而补掉末段的超出量
        for j, wi in enumerate(w):
            idx = m - rng_pts + j          # 要调整的点
            if idx <= 0:
                continue
            shift = excess * (wi / wsum)
            la_p, lo_p = out[idx - 1]
            la_c, lo_c = out[idx]
            d = haversine(la_p, lo_p, la_c, lo_c)
            if d < 1e-9:
                continue
            brg = bearing(la_p, lo_p, la_c, lo_c)
            # 目标间距 = 原间距 + 分摊量 (末点方向再压缩)
            new_d = max(0.5, d - shift * 0.5)
            out[idx] = dest_point(la_p, lo_p, brg, new_d)

        # 末点严格回到起点; 末段现在会因为前面点的挪动而略微改变 —— 可接受
        out[-1] = out[0]
        return out

    # ------------------------------------------------------------------
    def _align_geometry(self, coords: List[Tuple[float, float]],
                        n: int, speeds: List[float]) -> List[Tuple[float, float]]:
        """
        把几何点列重采样成 n 点。

        ★ 关键点 (踩过的坑)
            早期实现按 "速度积分比例" 直接定位到几何弧长, 但几何样条的
            相邻点间距并不均匀 (折返路线最短处只有 3m), 而重采样步长
            约 15m —— 每次跨越都会**整段跳过**那段几何。46 次折返累计
            下来, 7000m 的路线只剩 6691m (误差 -5.5%)。

        正确做法:
            1. 先把 "速度积分" 归一成 [0,1] 的**进度比例**
            2. 再用这个比例去乘几何总弧长, 得到目标弧长
            3. 在几何折线上按弧长**线性插值**取点 (不会跳过任何一段)

        这样几何总量严格守恒 (累计弧长单调覆盖 [0, total]),
        同时进度仍然反映速度变化 (快的地方横向跨度大)。
        """
        if len(coords) < 2:
            return [(self.start[0], self.start[1])] * n

        # 累计弧长
        cumlen = [0.0]
        for i in range(1, len(coords)):
            cumlen.append(cumlen[-1] + haversine(coords[i - 1][0], coords[i - 1][1],
                                                 coords[i][0], coords[i][1]))
        total = cumlen[-1] or 1.0

        # 速度积分 -> 归一化进度 [0,1]
        # ★ 必须用【真实采样步长】加权（2026-09-24）
        #   旧实现一律乘标称 self.dt，而 _walk 的时间步长是不规则的
        #   （20% 是 1~8 秒）。于是"标称 5 秒的位移"可能只花 1 秒走完，
        #   瞬时速度被放大 5 倍，10 秒窗里就混进 4.3 m/s 的假快段。
        #   让几何分配与时间步长共用同一份 steps，速度与位移才自洽。
        _st = self._get_steps(n, self._target_dur())
        steps = [v * s for v, s in zip(speeds, _st)]
        run = [0.0]
        for s in steps:
            run.append(run[-1] + s)
        run_total = run[-1] or 1.0
        prog = [r / run_total for r in run]

        # 强制首末对齐 (避免累积误差导致末点不落在几何末端)
        prog[0] = 0.0
        prog[-1] = 1.0

        # ★ 消除"末段间距异常":
        #   末点被钉在几何末端, 于是 roundoff 残差全部堆在最后一步上
        #   (实测末段 28.8m, 而中位 15.8m) —— _walk 会据此算出 5.8m/s
        #   的假瞬时速度, 步幅被顶到 195cm。
        #   做法: 把最后 K 步的进度重新分配, 使步长与中位数对齐。
        # ★★ 必须**按各自的 v*step 权重**分配, 不能一律均分 (2026-09-25 修复)
        #   均分会让「1.26 秒的小步」和「5 秒的步」分到**同样长**的弧长,
        #   于是 `_walk` 算出的 seg/step 在那些小步上炸开 —— 实测
        #      chord 14.25m / step 1.26s = 11.34 m/s (全程均速才 2.1 m/s),
        #   整份记录被一个点污染 (步幅/心率/最低得分都跟着失真)。
        #   按权重分配后, 尾部各点的 seg/step 仍 ≈ 速度曲线值。
        if n > 8:
            step_med = sorted(prog[i] - prog[i - 1]
                              for i in range(1, n))[(n - 1) // 2]
            k = min(8, n - 2)
            last_gap = prog[-1] - prog[-2]
            if step_med > 1e-12 and last_gap > step_med * 1.25:
                i0 = n - 1 - k                       # 首个待重排的**步**索引
                span = 1.0 - prog[i0]
                w = [speeds[i] * _st[i] for i in range(i0, n - 1)]
                wsum = sum(w)
                if wsum > 0:
                    acc = prog[i0]
                    for j in range(k):
                        acc += span * w[j] / wsum
                        prog[i0 + j + 1] = acc
                    prog[-1] = 1.0

        out = []
        j = 0
        for i in range(n):
            target = prog[i] * total
            # 单调前进: j 不回退, 保证严格覆盖每一段
            while j < len(cumlen) - 2 and cumlen[j + 1] <= target:
                j += 1
            seg = cumlen[j + 1] - cumlen[j]
            t = 0.0 if seg < 1e-9 else (target - cumlen[j]) / seg
            t = max(0.0, min(1.0, t))
            la = coords[j][0] + (coords[j + 1][0] - coords[j][0]) * t
            lo = coords[j][1] + (coords[j + 1][1] - coords[j][1]) * t
            out.append((la, lo))
        return out

    # ------------------------------------------------------------------
    def _target_dur(self) -> float:
        """本次生成的目标总时长(秒) —— 几何分配与时间步长共用同一口径"""
        d = getattr(self, "_est_dur", None)
        if d is not None:
            return float(d)
        if self.target_duration_s:
            return float(self.target_duration_s)
        return self.distance_m / self.profile.base_speed

    def _get_steps(self, n: int, total_s: float) -> List[float]:
        """取采样步长（同一轮生成内缓存，保证几何分配与 _walk 完全一致）

        ★ 必须缓存：`_walk` 在闭环标定里会被调用最多 6 次，若每次都重新
          抽样，则几何分配（只算一次）与时间步长（每次都变）对不上，
          逐点 speed = seg/step 会随机漂移。
        """
        cache = getattr(self, "_steps_cache", None)
        if cache is not None and cache[0] == n and abs(cache[1] - total_s) < 1e-9:
            return cache[2]
        steps = self._sample_steps(n, total_s)
        self._steps_cache = (n, total_s, steps)
        return steps

    def _sample_steps(self, n: int, total_s: float) -> List[float]:
        """生成 n-1 个采样步长(秒), 总和精确等于 total_s。

        ★ 为什么不能用「固定间隔 ±6% 抖动」(2026-09-24 重做)
            旧实现是 `self.dt * (1 + uniform(-0.06, 0.06))`, 有两个副作用:
              ① 抖动幅度只有 ±0.3s, `int(round(t_rel))` 之后**又落回 5 秒网格**
                 —— 上传的 27 键点里 totalTime 是 0/5/10/15/20…, 一眼看出是造的;
              ② 步长均值仍等于 dt, 但随机游走让**总时长偏离目标 ±2~3 秒**
                 (填 5'37" 跑 2km 应 674s, 实测 676.6s) —— 用户说的「时长对不上」。
            真机样本(见 NekoSportsWorldTool/src/track/generator.rs)的分布是:
            80% 落在标称间隔, 20% 是 1~8 秒的零散值 —— 整数秒因此不规则。
        """
        m = max(0, n - 1)
        if m == 0:
            return []
        rng = self.rng
        dt = self.dt
        # ★ 退化保护：目标时长撑不满"标称间隔的一半"时（n 被 max(20, …) 顶到 20，
        #   而总时长只有几秒），下面那套绝对步长(5s / 1~8s) 会被末尾的整体缩放
        #   压到 0.03s，`speed = seg/step` 直接飙到几百 m/s。直接均分最稳。
        if total_s <= m * dt * 0.5:
            return [total_s / m] * m
        steps: List[float] = []
        for _ in range(m):
            if rng.random() < 0.80:
                steps.append(dt)
            else:
                steps.append(float(rng.choice((1.0, 2.0, 3.0, 4.0, 6.0, 7.0, 8.0))))
        # 总时长对齐: 差值平摊到「零散步」上, 标称步保持 dt 不变
        # (保住"主体是 5 秒采样"的真实感, 同时让总时长精确命中目标)
        loose = [i for i, s in enumerate(steps) if s != dt]
        delta = total_s - sum(steps)
        if loose and abs(delta) > 1e-9:
            per = delta / len(loose)
            for i in loose:
                steps[i] = max(0.5, steps[i] + per)
        # 夹取后仍有残差 → 整体缩放兜底(保证总和精确, 不留 ±秒 的尾巴)
        resid = total_s - sum(steps)
        if abs(resid) > 1e-6 and sum(steps) > 0:
            k = total_s / sum(steps)
            steps = [s * k for s in steps]
        return steps

    def _walk(self, coords, speeds, n, total_s=None) -> Tuple[List[GeoPoint], float]:
        """沿几何点列按速度推进, 生成 GeoPoint 列表"""
        points: List[GeoPoint] = []
        cum = 0.0
        elapsed = 0.0
        prev_la, prev_lo = coords[0]
        t0 = self.start_time

        if total_s is None:
            total_s = self._target_dur()
        steps = self._get_steps(n, total_s)

        for i in range(n):
            la, lo = coords[i]
            low_gap = False

            if i == 0:
                seg = 0.0
                step = 0.0          # 首点时间 = 起跑时间本身
            else:
                step = steps[i - 1]
                seg = haversine(prev_la, prev_lo, la, lo)
                # ★ 间距过小 (折返/绕圈处几何打结) 时不要把速度虚增上去:
                #   早期实现是沿朝向"拉开到 speed*dt", 这会造出一个
                #   5.8 m/s 的假尖峰 (平均才 3.0), 进而把步幅推到 195cm。
                #   正确做法: 只把该点**沿路径推进到最小间距** (min_gap),
                #   速度仍由 speed profile 决定 —— 间距与速度解耦。
                # ★ 用【本点真实步长】而非标称 dt：步长不规则后（1~8s），
                #   按 dt 算的 min_gap 会在 1 秒的小步上放行过短的几何间距。
                min_gap = max(0.4, speeds[i] * step * 0.55)
                low_gap = seg < min_gap
                # ★★ 闭环末点是**硬锚点**，绝不允许外推 (2026-09-25 修复)
                #   闭环的末点就是起点，是几何上钉死的约束；而末段是绕过
                #   收口拐角的**弦**，实测只有 1.6~2.2m（中位 15.7m）——
                #   一旦落进 min_gap 判定就会被沿朝向外推到 min_gap 处，
                #   于是末点**越过起点** `min_gap - seg`：
                #       min_gap = max(0.4, speeds[i]*step*0.55)
                #       步长最高 8s、速度 ~3 m/s → min_gap 可达 ~13m
                #   实测 5 配速 × 40 种子共 200 例中 15~17% 中招，
                #   闭合缝 5.2~11.0m，超过 swmode.verify_track 的 5m 阈值
                #   → warn → swcli.py return 5 阻止提交（用户线上报错即此）。
                #   位置保持不动，速度直接取曲线值（见下），
                #   因此**不会**在末尾挖出一个假低速点。
                anchor = (i == n - 1 and self.mode == RouteMode.LOOP)
                if low_gap and not anchor:
                    brg = (bearing(prev_la, prev_lo, la, lo)
                           if seg > 1e-9 else self.rng.uniform(0, 360))
                    la, lo = dest_point(prev_la, prev_lo, brg, min_gap)
                    seg = haversine(prev_la, prev_lo, la, lo)

            cum += seg
            elapsed += step
            ts = int((t0 + timedelta(seconds=elapsed)).timestamp() * 1000)

            # ★★ 逐点速度**一律取速度曲线值**（2026-10-08 用户报「配速图波动很大」）
            #   旧实现走 `spd = seg / step`（仅当偏离曲线 > 35%/45% 才回退）。
            #   实测（2.0km / 5:40 / 137 点）：
            #       seg/step 原始值  偏离平均配速 > 35s 的点占 **52%**
            #       速度曲线本身     只有 **3%**
            #   差距全部来自 `add_sample_noise` 的 σ=1.6m GPS 抖动 ——
            #   它叠在 ~15m 的采样间距上，把 seg 放大/缩小 ±11%，
            #   而 `seg/step` 又把这误差 1:1 转成瞬时速度（min 210 / max 630 s/km）。
            #   几何本来就是**按 `speeds[i] * step` 分配弧长**的（见
            #   `_align_geometry`），所以 `seg/step` 的正确值就是曲线值本身；
            #   抖动纯属噪声，不该进配速曲线。
            #   ★ 只改速度、**不动位置** —— 位置一变，闭环标定会失稳。
            #   ★ `speed` 仍恒 > 0（isValidPoint 的 s0 分项要求 > 0）。
            spd = speeds[i] if (i > 0 and step > 0) else speeds[0]
            if spd <= 0:
                spd = 0.6

            points.append(GeoPoint(
                lat=la, lon=lo, ts_ms=ts, ele=0.0,
                speed=spd,
                dist_from_start=cum, seg_m=seg,
            ))
            prev_la, prev_lo = la, lo

        return points, cum

    # ------------------------------------------------------------------
    def _fill_physiology(self, points: List[GeoPoint],
                         dist_m: float, dur_s: float) -> None:
        """填充步频 / 步幅 / 心率"""
        prof = self.profile
        rng = self.rng
        avg_v = dist_m / dur_s if dur_s else prof.base_speed

        # --- 步频标定 ---
        # 真实世界: 步频随速度变化, 但比速度变化慢得多。
        # 经验模型 (基于大量跑者数据):
        #     步频 = base_cadence * (v / base_speed) ** 0.16
        # 指数 0.16 意味着速度翻倍, 步频只涨 11% (非常接近实测)
        # 这样在起步低速段 (0.6 m/s) 步频也只会降到约 130, 不会离谱。
        cad_min, cad_max = 128.0, 200.0
        raw = []
        for p in points:
            v = max(0.35, p.speed)                  # 防止除零/负值
            ratio = v / prof.base_speed
            c = prof.base_cadence * (ratio ** 0.16)
            # 个人特征偏移 (每个点独立小抖动)
            c *= (1 + rng.gauss(0, 0.012))
            raw.append(max(cad_min, min(cad_max, c)))

        # 移动平均平滑
        w = 3
        cad = []
        for i in range(len(raw)):
            a, b = max(0, i - w), min(len(raw), i + w + 1)
            cad.append(sum(raw[a:b]) / (b - a))

        # --- 步幅: 由速度 / 步频推导, 并保证下限 ---
        # 真实跑步步幅一般在 80~180cm; 走路约 60~80cm。
        # 起步加速段的极低速会造成步幅过小, 这里做物理下限约束:
        #     步幅(cm) >= 身高 * 0.42  (低速时接近走路步幅)
        stride_floor = prof.height_cm * 0.42
        stride_ceil = prof.height_cm * 1.15
        for p, c in zip(points, cad):
            p.cadence = c
            st = prof.stride_cm(p.speed, c)
            st = max(stride_floor, min(stride_ceil, st))
            p.stride_cm = st

        # --- 心率: 与速度相关 + 时间漂移 ---
        max_hr = prof.max_hr()
        hr_base = 0.62 * max_hr + (avg_v - 2.6) * 11
        drift_total = 0.09 * max_hr
        n = len(points)
        for i, p in enumerate(points):
            t = i / max(1, n - 1)
            h = (hr_base
                 + drift_total * t
                 + (p.speed - avg_v) * 8.5
                 + rng.gauss(0, 2.2))
            p.hr = int(max(95, min(max_hr, h)))

    # ------------------------------------------------------------------
    def _build_record(self, points: List[GeoPoint],
                      dist_m: float, dur_s: float) -> TrackRecord:
        prof = self.profile
        avg_v = dist_m / dur_s if dur_s else 0.0

        cad = [p.cadence for p in points]
        avg_cad = sum(cad) / len(cad)
        avg_stride = sum(p.stride_cm for p in points) / len(points)
        total_steps = int(sum(p.cadence / 60.0 * self.dt for p in points))

        # 爬升 / 下降 (阈值 0.15m 过滤噪声)
        # ★ 必须用【序列化后的精度】计算：core.py 里 JSON 的 ele 是 round(,2)，
        #   若这里用全精度、而上传侧（swsubmit.total_ascent）拿到的是 2 位小数，
        #   阈值 0.15 附近的步长判定会分歧（实测最大差 0.30m），
        #   再叠加上传字段取整为整数，就会出现「界面 108 而 App 109」这种 1m 差。
        #   统一到 round(,2) 后，两处**同一份数据、同一套算法**。
        asc = desc = 0.0
        _eles = [round(p.ele, 2) for p in points]
        for i in range(1, len(_eles)):
            d = _eles[i] - _eles[i - 1]
            if d > 0.15:
                asc += d
            elif d < -0.15:
                desc += -d

        hrs = [p.hr for p in points]
        avg_hr = int(sum(hrs) / len(hrs))
        max_hr_v = max(hrs)

        # 卡路里: MET 简化模型
        kcal = prof.weight_kg * (dist_m / 1000.0) * 1.036

        splits = self._build_splits(points)

        avg_pace = dur_s / (dist_m / 1000.0) if dist_m else 0.0
        valid_paces = [s.pace_s_per_km for s in splits if s.pace_s_per_km > 0]
        best_pace = min(valid_paces) if valid_paces else avg_pace

        return TrackRecord(
            start_time=datetime.fromtimestamp(points[0].ts_ms / 1000),
            end_time=datetime.fromtimestamp(points[-1].ts_ms / 1000),
            distance_m=dist_m,
            duration_s=dur_s,
            points=points,
            splits=splits,
            avg_pace_s_per_km=avg_pace,
            best_pace_s_per_km=best_pace,
            avg_speed_mps=avg_v,
            max_speed_mps=max(p.speed for p in points),
            avg_cadence=avg_cad,
            avg_stride_cm=avg_stride,
            total_steps=total_steps,
            total_ascent_m=asc,
            total_descent_m=desc,
            avg_heart_rate=avg_hr,
            max_heart_rate=max_hr_v,
            calories=kcal,
        )

    # ------------------------------------------------------------------
    def _build_splits(self, points: List[GeoPoint]) -> List[Split]:
        """
        按公里切分统计。

        ★ 关键点 (踩过的坑)
            生成出的实际总距离往往不会正好是整数公里 (例如目标 3km
            实际落在 2988m)。若直接用 int(total // 1000) 作为"完整公里数",
            就会把第 3 公里的绝大部分数据丢掉, 变成 "3km 只有 2 段"。

        算法:
            1. 以目标距离 target_m 为准计算应有段数 (而非实际距离)
            2. 对每一段 [k, k+1) km, 用**线性插值**求精确的时间/里程分界
            3. 最后一段若不足 1km, 标记 partial=True
            4. 尾巴过短 (< 3% 公里) 时并入上一段, 避免出现 23m 的碎片段
        """
        splits: List[Split] = []
        if len(points) < 2:
            return splits

        total_d = points[-1].dist_from_start
        if total_d <= 0:
            return splits

        # 以目标距离为基准 (无目标时退回实际距离)
        target_m = self.distance_m if self.distance_m > 0 else total_d
        # 完整公里数: 以目标为准, 但也参考实际距离
        n_full = int(round(min(target_m, total_d) / 1000.0))
        if n_full < 1:
            n_full = int(total_d // 1000.0)
        # 保证不超过实际数据能支撑的段数
        n_full = max(0, min(n_full, int(total_d // 1000.0) + 1))

        tail_d = total_d - n_full * 1000.0

        # 尾巴极短 -> 并入最后一段 (把最后一段的终点延到实际末尾)
        merge_tail = (n_full >= 1 and 0.0 <= tail_d < 30.0)

        for km in range(1, n_full + 1):
            d_a = (km - 1) * 1000.0
            d_b = km * 1000.0
            if merge_tail and km == n_full:
                d_b = total_d                       # 吸收尾巴
            if d_b > total_d:
                d_b = total_d
            if d_b - d_a < 1e-6:
                continue
            s = self._make_split_span(km, points, d_a, d_b)
            if s is not None:
                splits.append(s)

        # 真正的尾巴段 (>= 30m)
        if not merge_tail and tail_d >= 30.0:
            if len(points) - 1 > self._index_at_distance(points, n_full * 1000.0):
                s = self._make_split_span(n_full + 1, points,
                                          n_full * 1000.0, total_d)
                if s is not None:
                    s.partial = True
                    splits.append(s)

        return splits

    def _make_split_span(self, km: int, points: List[GeoPoint],
                         d_a: float, d_b: float) -> Optional[Split]:
        """按【精确距离区间】[d_a, d_b] 造一段统计（端点线性插值）

        ★ 与 _make_split(a, b) 的区别：那边用采样点下标当边界，段长会多出
          最多一个采样间隔；这里用插值把段长精确钉在 d_b - d_a 上。
        """
        if d_b - d_a < 1e-6:
            return None
        t_a, _ = self._interp_at(points, d_a)
        t_b, _ = self._interp_at(points, d_b)
        d = d_b - d_a
        t = max(0.0, t_b - t_a)
        pace = t / (d / 1000.0) if d > 0 else 0.0
        # 内部采样点用于步频/步幅均值与爬升
        i_a = self._index_at_distance(points, d_a)
        i_b = self._index_at_distance(points, d_b)
        seg = points[max(0, i_a - 1):i_b + 1] or points[i_a:i_a + 1]
        asc = 0.0
        _sele = [round(p.ele, 2) for p in seg]   # ★ 与 core.py 序列化精度一致（见 _build_record）
        for i in range(1, len(_sele)):
            dd = _sele[i] - _sele[i - 1]
            if dd > 0.15:
                asc += dd
        return Split(
            km=km,
            distance_m=d,
            time_s=t,
            pace_s_per_km=pace,
            avg_cadence=sum(p.cadence for p in seg) / len(seg),
            avg_stride_cm=sum(p.stride_cm for p in seg) / len(seg),
            elev_gain_m=asc,
            avg_hr=int(sum(p.hr for p in seg) / len(seg)),
        )

    @staticmethod
    def _index_at_distance(points: List[GeoPoint], d: float) -> int:
        """返回累计距离首次 >= d 的点下标 (线性扫描, 点数少时够快)"""
        for i, p in enumerate(points):
            if p.dist_from_start >= d:
                return i
        return len(points) - 1

    @staticmethod
    def _interp_at(points: List[GeoPoint], d: float) -> Tuple[float, float]:
        """在累计距离 d 处线性插值, 返回 (相对秒, 海拔)

        ★ 分段边界必须插值而不是"取首个跨过的采样点" (2026-09-24)
          采样间隔 ~5s 时, 用采样点当边界会让段长多出最多一个间隔
          (实测第 1 公里 1013.4m), 分段配速因此系统性偏慢 ~1.3%
          (显示 5'40" 而真值 5'37")。真机 1Hz 采样时偏差可忽略,
          采样稀疏就必须插值 —— 本方法即该 docstring 早就承诺的口径。
        """
        if not points:
            return 0.0, 0.0
        t0 = points[0].ts_ms / 1000.0
        if d <= points[0].dist_from_start:
            return 0.0, points[0].ele
        if d >= points[-1].dist_from_start:
            return points[-1].ts_ms / 1000.0 - t0, points[-1].ele
        lo, hi = 0, len(points) - 1
        while lo + 1 < hi:
            mid = (lo + hi) // 2
            if points[mid].dist_from_start <= d:
                lo = mid
            else:
                hi = mid
        span = points[hi].dist_from_start - points[lo].dist_from_start
        f = 0.0 if span <= 1e-9 else (d - points[lo].dist_from_start) / span
        ta = points[lo].ts_ms / 1000.0 - t0
        tb = points[hi].ts_ms / 1000.0 - t0
        return ta + (tb - ta) * f, points[lo].ele + (points[hi].ele - points[lo].ele) * f

    # ------------------------------------------------------------------
    def _resolve_checkpoints(self, points: List[GeoPoint]) -> List[dict]:
        """计算每个打卡点是否被轨迹命中"""
        out = []
        for cp in self.checkpoints:
            best_i, best_d = -1, float("inf")
            for i, p in enumerate(points):
                d = haversine(p.lat, p.lon, cp.lat, cp.lon)
                if d < best_d:
                    best_d, best_i = d, i
            cp.hit_index = best_i if best_d <= cp.radius_m else -1
            cp.hit_dist_m = best_d
            if cp.hit_index >= 0:
                cp.hit_time = datetime.fromtimestamp(
                    points[cp.hit_index].ts_ms / 1000).strftime("%H:%M:%S")
            out.append(cp.to_dict())
        return out

    # ------------------------------------------------------------------
    def _validate(self, points: List[GeoPoint]) -> None:
        """
        ★ 对所有点位跑 isValidPoint 校验。
        参数: w2=1 (有效采集点), w3=20 (高档), w4=1, w5=0
        s0 = 瞬时速度, s1 = 与上一点的间距
        """
        ok_all = True
        min_score = 10 ** 9
        for i, p in enumerate(points):
            s0 = p.speed
            s1 = p.seg_m if i > 0 else max(1.0, p.speed * self.dt)
            if not isValidPoint(IVP_W2, s0, s1, IVP_W3, IVP_W4, IVP_W5):
                ok_all = False
            sc = total_score(IVP_W2, s0, s1, IVP_W3)
            min_score = min(min_score, sc)
        self.all_points_valid = ok_all
        self.min_point_score = min_score if min_score < 10 ** 9 else 0

    # ------------------------------------------------------------------
    # 导出
    # ------------------------------------------------------------------
    def export_json(self, rec: TrackRecord, path: str) -> None:
        with open(path, "w", encoding="utf-8") as f:
            f.write(rec.to_json())

    def export_gpx(self, rec: TrackRecord, path: str) -> None:
        lines = ['<?xml version="1.0" encoding="UTF-8"?>',
                 '<gpx version="1.1" creator="RunningGenerator"',
                 '     xmlns="http://www.topografix.com/GPX/1/1">',
                 '  <trk><name>Running</name><trkseg>']
        for p in rec.points:
            t = datetime.fromtimestamp(p.ts_ms / 1000).strftime("%Y-%m-%dT%H:%M:%SZ")
            lines.append(f'    <trkpt lat="{p.lat:.7f}" lon="{p.lon:.7f}">'
                         f'<ele>{p.ele:.1f}</ele><time>{t}</time></trkpt>')
        lines += ['  </trkseg></trk>', '</gpx>']
        with open(path, "w", encoding="utf-8") as f:
            f.write("\n".join(lines))

    def export_csv(self, rec: TrackRecord, path: str) -> None:
        with open(path, "w", encoding="utf-8", newline="") as f:
            f.write("idx,timestamp,time,lat,lon,ele,speed_mps,cadence,stride_cm,hr,dist_m,seg_m\n")
            for i, p in enumerate(rec.points):
                f.write(f"{i},{p.ts_ms},"
                        f"{datetime.fromtimestamp(p.ts_ms/1000).strftime('%H:%M:%S')},"
                        f"{p.lat:.7f},{p.lon:.7f},{p.ele:.2f},{p.speed:.4f},"
                        f"{p.cadence:.1f},{p.stride_cm:.1f},{p.hr},"
                        f"{p.dist_from_start:.2f},{p.seg_m:.3f}\n")

    def export_all(self, rec: TrackRecord, outdir: str, prefix: str = "run") -> dict:
        """一次性导出 json / gpx / csv, 返回路径字典"""
        import os
        os.makedirs(outdir, exist_ok=True)
        paths = {
            "json": os.path.join(outdir, f"{prefix}.json"),
            "gpx": os.path.join(outdir, f"{prefix}.gpx"),
            "csv": os.path.join(outdir, f"{prefix}.csv"),
        }
        self.export_json(rec, paths["json"])
        self.export_gpx(rec, paths["gpx"])
        self.export_csv(rec, paths["csv"])
        return paths


# ============================================================
# 便捷入口
# ============================================================
def generate_record(start_lat: float, start_lon: float, distance_km: float,
                    start_time=None, checkpoints=None,
                    pace_s_per_km: float = 330.0, mode: str = RouteMode.LOOP,
                    end=None, seed: Optional[int] = None, cadence: float = 172.0,
                    height_cm: float = 172.0, weight_kg: float = 65.0,
                    age: int = 22, fitness: float = 0.5,
                    sample_interval_s: float = DEFAULT_SAMPLE_INTERVAL,
                    shape: str = SHAPE_TRACK,
                    ) -> TrackRecord:
    """一行生成完整跑步记录

    :param end: 点到点模式的终点 (纬度, 经度); 其他模式忽略
    """
    if start_time is None:
        start_time = datetime.now().replace(microsecond=0)
    prof = RunnerProfile(base_pace_s_per_km=pace_s_per_km,
                         base_cadence=cadence,
                         height_cm=height_cm,
                         weight_kg=weight_kg,
                         age=age,
                         fitness_level=fitness)
    gen = RunningGenerator(
        start=(start_lat, start_lon),
        distance_km=distance_km,
        start_time=start_time,
        checkpoints=checkpoints,
        profile=prof,
        mode=mode,
        end=end,
        seed=seed,
        sample_interval_s=sample_interval_s,
        shape=shape,
    )
    return gen.generate()
