"""Runtime configuration with JSON override of code defaults."""
from __future__ import annotations

import json
from dataclasses import dataclass, fields
from pathlib import Path


@dataclass
class Config:
    clip_margin: str = "2.2mm"
    sweep_step_mhz: float = 1.0
    subproject_prefix: str = "SubProject_"
    retry_attempts: int = 2          # total attempts per SIwave action (>=1)
    continue_on_error: bool = True   # skip a failing component instead of aborting
    log_file: str = "8_log/功能2_SIwave抽取/siwave_extractor.log"
    ref_net: str = "GND"             # reference (ground) net name for port placement
    com_timeout: int = 1200          # per-segment seconds; hung job is killed (0 = no limit)
    num_cpus: int = 0                # SYZ 求解用的 CPU 核数；0 = 自动取本机全部核（提速）
    parallel_workers: int = 1        # 常驻 SIwave 路数；<=1 走串行 COM，>=2 每路一份整板副本循环裁+求（16GB 最多 2 路）
    parallel_cpus_per_worker: int = 0  # 并行时每个 worker 的求解核数；0 = 自动取 总核数//并行数
    skip_series_nc: tuple = ()  # 串联不贴位号，这些焊盘不建抽取口


def load_config(path: str | Path) -> Config:
    """Load Config from a JSON file; unknown keys ignored; missing file => defaults."""
    path = Path(path)
    if not path.exists():
        return Config()
    data = json.loads(path.read_text(encoding="utf-8"))
    allowed = {f.name for f in fields(Config)}
    known = {k: v for k, v in data.items() if k in allowed}
    return Config(**known)
