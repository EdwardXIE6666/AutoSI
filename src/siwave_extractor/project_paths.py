"""Derive sub-project folder/file paths from the source .siw project path.

Note: paths are built with os.path (string joins) rather than created on disk by
default; the COM session creates the folder when it saves. build_subproject can
create the directory eagerly when create=True.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from rf.segments import safe_component_name


@dataclass(frozen=True)
class SubProject:
    project_name: str
    project_dir: str
    subproject_dir: str
    subproject_name: str


def sparam_root(out_dir: str | Path) -> Path:
    """抽取根目录：剥掉已有的 SubProject_* 层，避免套娃或把段名留在夹名里。"""
    p = Path(out_dir)
    while p.name.lower().startswith("subproject_"):
        p = p.parent
    return p


def build_subproject(project_path: str, prefix: str = "SubProject_",
                     create: bool = False, out_dir: str | None = None) -> SubProject:
    path = Path(project_path)
    project_name = path.stem
    project_dir = str(path.parent)
    subproject_name = f"{prefix}{project_name}"
    base_out = sparam_root(out_dir) if out_dir else path.parent.parent / "4_PCB_S参数"
    subproject_dir = str(base_out / subproject_name)
    if create:
        os.makedirs(subproject_dir, exist_ok=True)
    return SubProject(project_name, project_dir, subproject_dir, subproject_name)


def subproject_file(sp: SubProject, network_name: str) -> str:
    """Full path to the saved sub-project .siw for a given network/component."""
    stem = safe_component_name(network_name)
    return os.path.join(sp.subproject_dir, f"{stem}.siw")
