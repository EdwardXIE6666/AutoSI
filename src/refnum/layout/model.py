"""功能5 阶段 B — 构图领域模型（无算法、无 Excel IO）。

SheetPlan / LayoutRow 等供行序、投放、写表共用。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from refnum.path import Device, Path


@dataclass
class LayoutRow:
    """一条主链行（折叠后可对应多条 band Path）。"""

    row_id: str
    path: Path
    label: str
    devices: list[Device] = field(default_factory=list)


@dataclass
class SheetPlan:
    """单 Sheet 构图计划。"""

    key: str
    rows: list[LayoutRow] = field(default_factory=list)
    # 折叠后：ref → 仍含该 ref 的 row_id 集合（holes / 落位）
    share_map: dict[str, frozenset[str]] = field(default_factory=dict)
    # ref → 互异左邻个数（共享度；≥2 才共享，见 refnum_layout_share）
    left_neighbor_count: dict[str, int] = field(default_factory=dict)
    # 完整 Co-pad/Co-lay 结构（L2 soft multi / path-owner 多主链 mid）
    copad_structures: list = field(default_factory=list)


@dataclass
class PlacedCell:
    """网格一格。

    kind: label | device | gray | copad_tag | copad_leg
    """

    ref: str
    device: Optional[Device] = None
    kind: str = "device"


# 器件实体格：占列、可作左邻 / 柱成员。copad_tag 是结构标签，不占列。
VISIBLE_DEVICE_KINDS = frozenset({"device", "copad_leg"})
# 写出位号、参与唯一性收口的格（比实体格多一个结构标签）。
UNIQUE_BEARING_KINDS = VISIBLE_DEVICE_KINDS | {"copad_tag"}


def is_visible_device(placed: Optional[PlacedCell]) -> bool:
    """该格是否为写出位号的器件实体（灰格 / 标签 / 空壳不是）。"""
    return (
        placed is not None
        and placed.kind in VISIBLE_DEVICE_KINDS
        and bool(str(placed.ref or "").strip())
    )


def is_jump_label(text: str) -> bool:
    """band / A 列标签是否 JUMP 特例（`side_chain._jump_band_label` 是唯一生成点）。

    只判文本本身；row_id 里嵌入的 JUMP 标记另见 `merge_span.is_jump_band_row_id`。
    """
    su = str(text or "").upper()
    return su.startswith("JUMP(") or su == "JUMP"


def is_decoration_row_id(rid: str) -> bool:
    """装饰行：夹心支路腿（``COPADBLK``）与链路空行（``__gap__``）。

    「透明行」的**基础语义**，各层共用这一份。放在最底层是因为 `order` 不能
    依赖 `merge_span`（`merge_span → rows → order` 会成环），而它也要用。

    上层扩展（都必须调本函数，不得再抄字符串判断）：
      - `merge_span.is_merge_passthrough_row_id`：+ 收拢侧的异 ref 短 FBRX
      - `merge_span.is_main_content_row_id`：本函数的反面
      - `order._is_transparent_plan_row`：+ 本层的取样行（不问 ref）
    """
    s = str(rid or "")
    return "|__gap__" in s or "COPADBLK" in s


@dataclass
class MergeRange:
    """纵向合并区间（grid.row_ids 的 0-based 闭区间）。"""

    ref: str
    col: int
    row_start: int
    row_end: int
    row_ids: tuple[str, ...] = ()


@dataclass
class CopadBlockGeom:
    """L4：一块 Co-pad 的局部几何（两支路行 + 结构列带）。

    cells: (branch_row 0|1, local_col≥1, PlacedCell)
    hub_local_cols: 须竖合并的局部列（跨两支路行）
    host_trunk_refs: 合并后干路（trunk_ref/trunk_after）须落在夹心主链行
    """

    block_id: str
    fork_label: str
    merge_label: Optional[str]
    attach_after_row_id: Optional[str]
    cells: list[tuple[int, int, PlacedCell]] = field(default_factory=list)
    hub_local_cols: list[int] = field(default_factory=list)
    max_local_col: int = 1
    structure_refs: frozenset[str] = field(default_factory=frozenset)
    host_trunk_refs: frozenset[str] = field(default_factory=frozenset)


@dataclass
class SheetGrid:
    """主链+结构网格。cells: row_id → col → PlacedCell（列 1 为标签）。"""

    key: str
    row_ids: list[str]
    labels: dict[str, str]
    cells: dict[str, dict[int, PlacedCell]] = field(default_factory=dict)
    max_col: int = 1
    merges: list[MergeRange] = field(default_factory=list)
    # L4 夹心结构件（对腿+支路中段+host_trunk）；unique 仅对这些 ref 给宿主加分，
    # 禁止宿主吸走无关共享尾
    copad_structure_refs: set[str] = field(default_factory=set)
    # merge.trunk_ref / trunk_after 所在主链（钉在夹心三连上一行）。链路空行跳过这些行与夹心块之间。
    copad_chip_trunk_row_ids: set[str] = field(default_factory=set)
    # 贯通宿主 → 画黄腿的 merge 支路主链（不插 COPADBLK 时）
    copad_branch_rows_by_host: dict[str, tuple[str, str]] = field(default_factory=dict)
    # 抽到黄腿外侧并 hub 的主链 → 切断用的 hub ref（hub 之后的干路留夹心）
    copad_outside_hub_cut: dict[str, str] = field(default_factory=dict)
