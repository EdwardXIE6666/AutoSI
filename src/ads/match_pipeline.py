"""ADS 天线口匹配：功能3 可选后半段门面（开关关则零副作用）。"""

from __future__ import annotations

import json
import re
import time
from collections import defaultdict
from pathlib import Path

from ads.match_ael import (
    STATUS_NAME,
    TRACE_NAME,
    OptimCell,
    append_match_trace,
    cell_sort_key,
    dataset_name,
    is_global_ant,
    parse_status,
    render_match_ael,
    render_match_kick_ael,
)
from ads.match_collapse import apply_collapse, collect_optim_vars
from ads.match_report import BandResult, vswr_pass, write_match_report
from ads.match_setup import attach_match_vars, attach_optim, pin_lookinto_ports
from ads.match_slots import slot_kind

_SP = re.compile(r"^SP(?:_(\w+)|(\d+))$")
_VSWR = re.compile(r"^VSWR(?:_(\w+)|(\d+))$")
_TRUE = {"1", "true", "yes", "on"}


def match_enabled(config: dict) -> bool:
    return str((config or {}).get("antenna_match_50r", "0")).strip().lower() in _TRUE


def _listed_cells(designer, ant_cells: list[str]) -> list[str]:
    fn = getattr(designer, "listed_cells", None)
    listed = []
    if callable(fn):
        try:
            listed = list(fn() or [])
        except Exception:
            listed = []
    if not listed:
        listed = list(ant_cells or [])
    ants = [n for n in (ant_cells or []) if is_global_ant(n)]
    segs = [n for n in listed if not is_global_ant(n)]
    if ants:
        segs = [n for n in segs if any(n == a or n.startswith(a + "_") or f"_{a}_" in n for a in ants)]
    if segs:
        return sorted(segs, key=cell_sort_key)
    return sorted((n for n in listed if is_global_ant(n)), key=cell_sort_key)


def _open_cell(designer, cell: str):
    if designer is None:
        return None
    for name in ("open_design", "get_design"):
        fn = getattr(designer, name, None)
        if callable(fn):
            try:
                return fn(cell)
            except Exception:
                return None
    return None


def _instance_names(design) -> list[str]:
    names: list[str] = []
    instances = getattr(design, "instances", None)
    if instances is not None:
        for inst in instances:
            n = getattr(inst, "name", None)
            if n:
                names.append(str(n))
        return names
    for attr in ("get_instance_iter", "get_instances"):
        fn = getattr(design, attr, None)
        if not callable(fn):
            continue
        try:
            items = fn()
        except Exception:
            continue
        for inst in items:
            n = getattr(inst, "name", None)
            if n:
                names.append(str(n))
        if names:
            return names
    return names


def _pair_key(match: re.Match[str]) -> str:
    return match.group(1) or match.group(2)


def _pair_bands(names: list[str]) -> list[tuple[str, str]]:
    sps: dict[str, str] = {}
    vswrs: dict[str, str] = {}
    for name in names:
        m = _SP.fullmatch(name)
        if m:
            sps[_pair_key(m)] = name
            continue
        m = _VSWR.fullmatch(name)
        if m:
            vswrs[_pair_key(m)] = name
    keys = sorted(sps, key=lambda k: (not k.isdigit(), int(k) if k.isdigit() else k))
    return [(sps[n], vswrs[n]) for n in keys if n in vswrs]


def _gnd_set(designer) -> set[str]:
    gnd_inf = getattr(designer, "gnd_inf", None)
    if gnd_inf and len(gnd_inf) > 2:
        return set(gnd_inf[2] or [])
    return set()


def _placed(designer, design) -> set[str]:
    fn = getattr(designer, "placed_refdes", None)
    if callable(fn):
        try:
            return set(fn(design) or [])
        except Exception:
            return set()
    return set()


def _save(design) -> None:
    fn = getattr(design, "save_design", None) or getattr(design, "save", None)
    if callable(fn):
        try:
            fn()
        except Exception:
            pass


def _refdes_from_inst(inst: str) -> str:
    for suf in ("_C", "_L", "_R"):
        if inst.endswith(suf):
            return inst[: -len(suf)]
    return inst


def _shared_refdes(cells: list[OptimCell]) -> set[str]:
    ants: dict[str, set[str]] = defaultdict(set)
    for cell in cells:
        for inst, _param, _lo, _hi in cell.vars:
            ants[_refdes_from_inst(inst)].add(cell.cell)
    return {rd for rd, owners in ants.items() if len(owners) > 1}


def _cross_ant_label(cell: OptimCell, shared: set[str]) -> str:
    hits: list[str] = []
    for inst, _param, _lo, _hi in cell.vars:
        rd = _refdes_from_inst(inst)
        if rd in shared and rd not in hits:
            hits.append(rd)
    return ",".join(hits)


def unrun_rows_from_cells(cells: list[OptimCell]) -> list[BandResult]:
    shared = _shared_refdes(cells)
    rows: list[BandResult] = []
    for cell in cells:
        cross = _cross_ant_label(cell, shared)
        for sp_name, _vswr in cell.bands:
            rows.append(
                BandResult(
                    ant=cell.cell,
                    band=sp_name,
                    worst_vswr=None,
                    s11_mag=None,
                    status="unrun",
                    at_bound=[],
                    cross_ant=cross,
                    values={},
                )
            )
    return rows


def mark_cross_ant(rows: list[BandResult], cells: list[OptimCell]) -> list[BandResult]:
    shared = _shared_refdes(cells)
    by_cell = {c.cell: _cross_ant_label(c, shared) for c in cells}
    out: list[BandResult] = []
    for row in rows:
        label = by_cell.get(row.ant, "")
        if label and not row.cross_ant:
            row = BandResult(
                ant=row.ant,
                band=row.band,
                worst_vswr=row.worst_vswr,
                s11_mag=row.s11_mag,
                status=row.status,
                at_bound=list(row.at_bound),
                cross_ant=label,
                values=dict(row.values),
            )
        out.append(row)
    return out


def _region_map(designer, design) -> dict[int, set[str]]:
    fn = getattr(designer, "region_refdes", None)
    if not callable(fn):
        return {}
    try:
        raw = fn(design) or {}
    except Exception:
        return {}
    return {int(k): set(v) for k, v in raw.items()}


def _collect_vars(designer, design, refdes_set: set[str] | None, gnd: set[str]) -> list[tuple[str, str, str, str]]:
    placed = _placed(designer, design)
    names = sorted(placed if refdes_set is None else (placed & refdes_set))
    vars_: list[tuple[str, str, str, str]] = []
    for refdes in names:
        if not slot_kind(refdes):
            continue
        actions = apply_collapse(design, refdes, refdes in gnd)
        for action in collect_optim_vars(actions):
            vars_.append((action.instance_name, action.param or "", action.lo or "", action.hi or ""))
    return vars_


def run_match_prepare(designer, ant_cells: list[str]) -> list[OptimCell]:
    cells: list[OptimCell] = []
    gnd = _gnd_set(designer)
    for cell_name in _listed_cells(designer, ant_cells):
        design = _open_cell(designer, cell_name)
        if design is None:
            continue
        regions = _region_map(designer, design)
        all_vars = _collect_vars(designer, design, None, gnd)
        bands = _pair_bands(_instance_names(design))
        band_vars: list[list[tuple[str, str, str, str]]] = []
        for i, _band in enumerate(bands):
            if i in regions:
                band_vars.append(_collect_vars(designer, design, regions[i], gnd))
            else:
                band_vars.append(list(all_vars))
        pin_lookinto_ports(design)
        attach_optim(design, bands)
        attach_match_vars(design, all_vars)
        _save(design)
        cells.append(OptimCell(cell=cell_name, bands=bands, vars=all_vars, band_vars=band_vars))
    return cells


_UNIT = {
    "pf": 1e-12,
    "nf": 1e-9,
    "uf": 1e-6,
    "f": 1.0,
    "nh": 1e-9,
    "uh": 1e-6,
    "h": 1.0,
}


def _to_si(text: str) -> float | None:
    raw = str(text or "").strip()
    if not raw:
        return None
    token = re.sub(r"(?i)opt\{.*", " ", raw).split()[0]
    m = re.match(r"^([+-]?(?:\d+\.?\d*|\.\d+)(?:[eE][+-]?\d+)?)([a-zA-Z]*)$", token)
    if not m:
        return None
    number = float(m.group(1))
    unit = (m.group(2) or "").lower()
    if not unit:
        rest = raw[len(token) :].strip().split()
        if rest:
            unit = rest[0].lower()
    return number * _UNIT.get(unit, 1.0)


def _near_bound(value: str, lo: str, hi: str) -> bool:
    v = _to_si(value)
    lo_si = _to_si(lo)
    hi_si = _to_si(hi)
    if v is None or lo_si is None or hi_si is None:
        return False
    span = max(abs(hi_si - lo_si), 1e-30)
    return abs(v - lo_si) <= 0.02 * span or abs(v - hi_si) <= 0.02 * span


def mark_at_bound(rows: list[BandResult], cells: list[OptimCell]) -> list[BandResult]:
    bounds: dict[str, dict[str, tuple[str, str]]] = {}
    for cell in cells:
        by_inst = bounds.setdefault(cell.cell, {})
        for inst, _param, lo, hi in cell.vars:
            by_inst[inst] = (lo, hi)
            by_inst[_refdes_from_inst(inst)] = (lo, hi)
    out: list[BandResult] = []
    for row in rows:
        hits: list[str] = list(row.at_bound)
        table = bounds.get(row.ant, {})
        for key, val in row.values.items():
            lo_hi = table.get(key)
            if lo_hi and _near_bound(val, lo_hi[0], lo_hi[1]):
                label = _refdes_from_inst(key)
                if label not in hits:
                    hits.append(label)
        if hits != list(row.at_bound):
            row = BandResult(
                ant=row.ant,
                band=row.band,
                worst_vswr=row.worst_vswr,
                s11_mag=row.s11_mag,
                status=row.status,
                at_bound=hits,
                cross_ant=row.cross_ant,
                values=dict(row.values),
            )
        out.append(row)
    return out


def _gamma_from_vswr(vswr: float) -> float:
    return abs((vswr - 1.0) / (vswr + 1.0))


def _dataset_paths(workspace_path, lib: str, ant: str) -> list[Path]:
    ws = Path(workspace_path)
    names: list[str] = []
    for name in (ant, f"{ant}_schematic", f"_{ant}", dataset_name(ant)):
        if name and name not in names:
            names.append(name)
    dirs = (ws / f"{lib}_data", ws / "data", ws / lib / "data")
    return [folder / f"{name}.ds" for folder in dirs for name in names]


def _vswr_peak_from_dataset(path: Path, vswr_name: str) -> float | None:
    try:
        from keysight.ads import dataset as ads_ds
    except ImportError:
        return None
    try:
        with ads_ds.open(path) as ds:
            for vb in ds.find_varblocks_with_var_name(vswr_name):
                for var in list(vb.dvars) + list(vb.ivars):
                    if getattr(var, "name", "") != vswr_name:
                        continue
                    data = getattr(var, "data", None)
                    if data is None:
                        continue
                    values = [abs(float(x.real if hasattr(x, "real") else x)) for x in data]
                    if values:
                        return max(values)
            for vb_name in ds.varblock_names:
                vb = ds[vb_name]
                for var in list(getattr(vb, "dvars", [])) + list(getattr(vb, "ivars", [])):
                    name = str(getattr(var, "name", ""))
                    if vswr_name not in name:
                        continue
                    data = getattr(var, "data", None)
                    if data is None:
                        continue
                    values = [abs(float(x.real if hasattr(x, "real") else x)) for x in data]
                    if values:
                        return max(values)
    except Exception:
        return None
    return None


def enrich_rows_from_dataset(
    rows: list[BandResult], cells: list[OptimCell], workspace_path, lib: str
) -> list[BandResult]:
    vswr_by = {(c.cell, sp): vswr for c in cells for sp, vswr in c.bands}
    out: list[BandResult] = []
    for row in rows:
        worst = row.worst_vswr
        s11 = row.s11_mag
        status = row.status
        if worst is None and workspace_path:
            vswr_name = vswr_by.get((row.ant, row.band), row.band.replace("SP", "VSWR", 1))
            for path in _dataset_paths(workspace_path, lib, row.ant):
                if not path.is_file():
                    continue
                peak = _vswr_peak_from_dataset(path, vswr_name)
                if peak is not None:
                    worst = peak
                    s11 = _gamma_from_vswr(peak)
                    break
        if worst is not None:
            status = "ok" if vswr_pass(worst) else "fail"
        out.append(
            BandResult(
                ant=row.ant,
                band=row.band,
                worst_vswr=worst,
                s11_mag=s11,
                status=status,
                at_bound=list(row.at_bound),
                cross_ant=row.cross_ant,
                values=dict(row.values),
            )
        )
    return out


def finalize_rows(
    rows: list[BandResult],
    cells: list[OptimCell],
    workspace_path=None,
    lib: str = "",
) -> list[BandResult]:
    rows = mark_cross_ant(rows, cells)
    rows = mark_at_bound(rows, cells)
    if workspace_path and lib:
        rows = enrich_rows_from_dataset(rows, cells, workspace_path, lib)
    return rows


def _drop_stale_atf(path: Path) -> None:
    stale = path.with_suffix(".atf")
    if stale.is_file():
        try:
            stale.unlink()
        except OSError:
            pass


def write_match_ael_file(
    path: Path,
    lib: str,
    cells: list[OptimCell],
    status_path: Path,
    workspace_path: str = "",
) -> Path:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        render_match_ael(lib, cells, str(status_path), workspace_path),
        encoding="utf-8",
    )
    _drop_stale_atf(path)
    return path


def write_match_kick_ael(
    path: Path,
    match_ael_path: Path,
    workspace_path: str = "",
    first_only: bool = False,
) -> Path:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        render_match_kick_ael(str(match_ael_path), workspace_path, first_only=first_only),
        encoding="utf-8",
    )
    _drop_stale_atf(path)
    return path


def find_ads_main_hwnd() -> int:
    try:
        import win32gui
    except ImportError:
        return 0
    found: list[int] = []

    def _cb(hwnd, _):
        if win32gui.IsWindowVisible(hwnd):
            title = win32gui.GetWindowText(hwnd) or ""
            if "Advanced Design System" in title:
                found.append(int(hwnd))
        return True

    win32gui.EnumWindows(_cb, None)
    return found[0] if found else 0


def invoke_match_in_running_ads() -> bool:
    """Click AutoSI > Match50R in the already-open ADS. Never start a second ads.exe."""
    try:
        import win32con
        import win32gui
    except ImportError:
        return False
    hwnd = find_ads_main_hwnd()
    if not hwnd:
        return False
    menu = win32gui.GetMenu(hwnd)
    if not menu:
        return False
    top_n = win32gui.GetMenuItemCount(menu)
    for i in range(top_n):
        top = win32gui.GetMenuString(menu, i, win32con.MF_BYPOSITION) or ""
        if "AutoSI" not in top:
            continue
        sub = win32gui.GetSubMenu(menu, i)
        if not sub:
            continue
        sub_n = win32gui.GetMenuItemCount(sub)
        for j in range(sub_n):
            item = win32gui.GetMenuString(sub, j, win32con.MF_BYPOSITION) or ""
            if "Match50R" not in item:
                continue
            item_id = win32gui.GetMenuItemID(sub, j)
            if item_id:
                win32gui.PostMessage(hwnd, win32con.WM_COMMAND, item_id, 0)
                return True
    return False


def wait_ready(ready_path: Path, timeout_s: float = 600) -> bool:
    ready_path = Path(ready_path)
    deadline = time.monotonic() + float(timeout_s)
    while True:
        if ready_path.is_file():
            try:
                text = ready_path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                text = ""
            if "ready" in text:
                return True
        now = time.monotonic()
        if now >= deadline:
            return False
        time.sleep(min(2.0, deadline - now))


def kick_match_after_boot(
    ads_exe: str,
    kick_path: Path,
    ready_path: Path,
    env: dict | None = None,
    cwd: str | None = None,
    settle_s: float = 12.0,
    ready_timeout_s: float = 600.0,
) -> bool:
    """Wait until -m boot wrote the ready marker, then run match in that same ADS."""
    del ads_exe, kick_path, env, cwd
    trace = Path(ready_path).with_name(TRACE_NAME)
    if not wait_ready(ready_path, timeout_s=ready_timeout_s):
        append_match_trace(trace, "py", "ready_timeout")
        print("ADS 桌面未就绪")
        return False
    append_match_trace(trace, "py", "ready")
    time.sleep(float(settle_s))
    if invoke_match_in_running_ads():
        append_match_trace(trace, "py", "kick")
        print("已在同一份 ADS 里启动天线口匹配")
        return True
    if find_ads_main_hwnd():
        append_match_trace(trace, "py", "kick_menu")
        print(
            "请在 ADS 主窗口 Tools > Command Line 执行："
            'load("C:/Project/AutoSI/match_kick.ael");'
        )
        return True
    append_match_trace(trace, "py", "kick_fail")
    print("找不到已打开的 ADS，未启动匹配")
    return False


def _read_status(status_path: Path) -> list[BandResult]:
    text = Path(status_path).read_text(encoding="utf-8")
    if not text.strip():
        return []
    try:
        return parse_status(text)
    except (json.JSONDecodeError, TypeError, ValueError):
        return []


def wait_status(
    status_path: Path, timeout_s: float = 3600, since: float | None = None
) -> list[BandResult] | None:
    status_path = Path(status_path)
    if since is None:
        try:
            ignore_mtime = status_path.stat().st_mtime if status_path.is_file() else None
        except OSError:
            ignore_mtime = None
    else:
        ignore_mtime = float(since)
    print("等待天线口匹配结果")
    deadline = time.monotonic() + float(timeout_s)
    while True:
        if status_path.is_file():
            try:
                mtime = status_path.stat().st_mtime
            except OSError:
                mtime = None
            if mtime is not None and (ignore_mtime is None or mtime > ignore_mtime):
                append_match_trace(status_path.with_name(TRACE_NAME), "py", "status")
                return _read_status(status_path)
        now = time.monotonic()
        if now >= deadline:
            append_match_trace(status_path.with_name(TRACE_NAME), "py", "status_timeout")
            print("未跑成")
            return None
        time.sleep(min(2.0, deadline - now))


def run_match_50r_if_enabled(
    config: dict,
    designer,
    ant_cells: list[str],
    lib: str,
    ads_dir,
    project_name: str,
    workspace_path=None,
    timeout_s: float = 3600,
) -> list[BandResult] | None:
    if not match_enabled(config or {}):
        return None
    ads_dir = Path(ads_dir)
    ws = Path(workspace_path) if workspace_path else ads_dir
    cells = run_match_prepare(designer, ant_cells)
    status_path = ads_dir / STATUS_NAME
    if status_path.is_file():
        status_path.unlink()
    write_match_ael_file(ads_dir / "match_50r.ael", lib, cells, status_path)
    rows = wait_status(status_path, timeout_s=timeout_s)
    if not rows:
        print("未跑成")
        rows = unrun_rows_from_cells(cells)
    rows = finalize_rows(rows, cells, ws, lib)
    write_match_report(ws / f"{project_name}_match_50r.xlsx", rows)
    return rows
