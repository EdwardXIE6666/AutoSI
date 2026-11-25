"""从 ADS 工程只读取出每个位号当前使能的那一个 R/L/C 值。

功能4给一个位号摆上电容、电感、电阻，调试时只使能其中一个。
使能了多个就只提醒，不替用户挑一个。
TDK、顺络电感不是 ads_rflib 的 L：型号在 PartNo / PartNumber，
电感量从该库的零件表查。查不到就不写，也不当成 NC。
位号先看实例名（L1131_C 这种）。名字里没有位号，就看脚上的网。
"""

from __future__ import annotations

import re
from collections import defaultdict
from decimal import Decimal, ROUND_HALF_UP
from dataclasses import dataclass
from pathlib import Path

from ads.vendor_inductance import refdes_from_pin_nets

_INST = re.compile(r"^(.+)_([CLR])$")
_ADS_RLC = {"C": "C", "L": "L", "R": "R"}
_IMPORT_OFF = {"0", "false", "no", "off"}
_FLAT_ANT = re.compile(r"^ANT[_-]?\d+$")
_TUNE = re.compile(r"\s+(?:no)?tune\{.*\}\s*$", re.IGNORECASE)
_DECIMAL = re.compile(r"\d+\.\d+")


def import_requested(flag: str | None) -> bool:
    """功能5是否把 ADS 调试值写入手册。缺省为导入；显式关掉才跳过。"""
    return str(flag or "1").strip().lower() not in _IMPORT_OFF


@dataclass(frozen=True)
class EnabledPart:
    refdes: str
    kind: str
    value: str
    cell: str
    instance: str
    enabled: bool = True


def resolve_enabled_values(
    parts: list[EnabledPart],
) -> tuple[dict[str, str], list[str]]:
    """一个位号恰好一个使能件 → 取值。一个都没使能 → NC。多个只出提醒。"""
    grouped: dict[str, list[EnabledPart]] = defaultdict(list)
    for part in parts:
        grouped[part.refdes].append(part)
    values: dict[str, str] = {}
    warnings: list[str] = []
    for refdes in sorted(grouped):
        enabled = [part for part in grouped[refdes] if part.enabled]
        if len(enabled) > 1:
            texts = {str(part.value or "").strip() for part in enabled}
            if len(texts) == 1 and "" not in texts:
                values[refdes] = next(iter(texts))
                continue
            detail = ", ".join(
                f"{part.cell}/{part.instance}={part.value or '空'}"
                for part in enabled
            )
            warnings.append(f"{refdes} 使能了 {len(enabled)} 个：{detail}")
            continue
        if not enabled:
            values[refdes] = "NC"
            continue
        part = enabled[0]
        text = str(part.value or "").strip()
        if not text:
            warnings.append(f"{refdes} 已使能 {part.instance}，参数是空的")
            continue
        values[refdes] = text
    return values, warnings


def round_value_text(text: str) -> str:
    """小数四舍五入到一位。进位后是整数就不写小数点。"""

    def repl(match: re.Match) -> str:
        rounded = Decimal(match.group(0)).quantize(Decimal("0.1"), rounding=ROUND_HALF_UP)
        whole = rounded.to_integral_value(rounding=ROUND_HALF_UP)
        if rounded == whole:
            return str(whole)
        return f"{rounded:.1f}"

    return _DECIMAL.sub(repl, str(text or ""))


def _enabled(inst) -> bool:
    if getattr(inst, "is_deactivated", False):
        return False
    if getattr(inst, "is_deactivated_and_shorted", False):
        return False
    return True


def _param_text(inst, kind: str) -> str:
    try:
        param = inst.parameters[kind]
    except Exception:
        return ""
    value = getattr(param, "value", None)
    if value is None:
        return ""
    return _TUNE.sub("", str(value).strip().strip('"')).strip()


def _has_schematic(cell) -> bool:
    try:
        views = cell.views
    except Exception:
        return False
    for view in views:
        if getattr(view, "is_schematic_view", False):
            return True
        if str(getattr(view, "name", "") or "").lower() == "schematic":
            return True
    return False


def _pin_net_names(inst) -> list[str]:
    names: list[str] = []
    try:
        pins = inst.inst_pins
    except Exception:
        return names
    for pin in pins:
        try:
            net = pin.net
        except Exception:
            continue
        names.append(str(getattr(net, "name", "") or ""))
    return names


def _vendor_part_fields(inst) -> tuple[str, str] | None:
    """PartNo / PartNumber → (索引, 型号)。没有这对参数就不是厂商电感。"""
    try:
        params = inst.parameters
    except Exception:
        return None
    if isinstance(params, dict):
        return None
    for param in params:
        pname = str(getattr(param, "name", "") or "")
        if pname not in {"PartNo", "PartNumber"}:
            continue
        index = str(getattr(param, "value", "") or "").strip().strip('"')
        form = str(getattr(param, "form_name", "") or "").strip().strip('"')
        if not form or form.lower() == "stdform":
            form = str(getattr(param, "display_value", "") or "").strip().strip('"')
        return index, form
    return None


def _instance_name(inst) -> str:
    return str(getattr(inst, "name", "") or "")


def _part_from_instance(inst, cell_name: str, catalog_for) -> EnabledPart | None:
    """名字里有位号就用名字。没有就用脚上网名。不是 R/L/C 的不记。"""
    name = _instance_name(inst)
    matched = _INST.match(name)
    component = str(getattr(inst, "cell_name", "") or "")
    vendor = _vendor_part_fields(inst)
    if matched:
        refdes, kind = matched.group(1), matched.group(2)
    else:
        refdes = refdes_from_pin_nets(_pin_net_names(inst))
        kind = _ADS_RLC.get(component, "")
    if vendor is not None:
        kind = "L"
    if not refdes or not kind:
        return None
    on = _enabled(inst)
    value = ""
    if on and vendor is not None and catalog_for is not None:
        library = str(getattr(inst, "library_name", "") or "")
        index, form = vendor
        value = catalog_for(library, component).lookup(form, index)
    elif on and vendor is None:
        value = _param_text(inst, kind)
    if value:
        value = round_value_text(value)
    return EnabledPart(
        refdes=refdes,
        kind=kind,
        value=value,
        cell=cell_name,
        instance=name,
        enabled=on,
    )


def _parts_on_design(design, cell_name: str, catalog_for=None) -> list[EnabledPart]:
    found: list[EnabledPart] = []
    for inst in design.instances:
        part = _part_from_instance(inst, cell_name, catalog_for)
        if part is not None:
            found.append(part)
    return found


def read_enabled_component_values(
    workspace: str | Path,
) -> tuple[dict[str, str], list[str]]:
    """只读打开工程，扫每个原理图单元里使能的 R/L/C。"""
    path = Path(workspace)
    if not path.is_dir():
        raise FileNotFoundError(f"ADS 工程不存在：{path}")
    from ads.ads_runtime import prepare_ads_python
    from ads.vendor_inductance import load_cell_catalog

    prepare_ads_python()
    from keysight.ads import de
    from keysight.ads.de import db_uu

    if not de.directory_is_workspace(path):
        raise RuntimeError(f"不是 ADS 工程（没有工作区）：{path}")
    if de.workspace_is_open():
        de.close_workspace()
    opened = de.open_workspace(path)
    parts: list[EnabledPart] = []
    read_notes: list[str] = []
    library_paths: dict[str, Path] = {}
    catalogs: dict[tuple[str, str], object] = {}

    def catalog_for(library: str, component: str):
        key = (library, component)
        if key not in catalogs:
            catalogs[key] = load_cell_catalog(library_paths.get(library), component)
        return catalogs[key]

    try:
        user_libs = []
        for lib in opened.libraries:
            if getattr(lib, "is_an_ads_library", False):
                continue
            lib_name = str(getattr(lib, "name", "") or "")
            lib_path = getattr(lib, "path", None)
            if lib_name and lib_path:
                library_paths[lib_name] = Path(lib_path)
            user_libs.append(lib)
        for lib in user_libs:
            # 厂商库是只读的，零件表用它的路径就够了。原理图在工程自己的库里。
            if getattr(lib, "is_read_only", False):
                continue
            lib_name = str(getattr(lib, "name", "") or "")
            for cell in lib.cells:
                if not _has_schematic(cell):
                    continue
                cell_name = str(getattr(cell, "name", "") or "")
                # 旧工程里还有名为天线号的合并页，和分段页是同一批位号。
                if _FLAT_ANT.fullmatch(cell_name):
                    continue
                dsn = f"{lib_name}:{cell_name}:schematic"
                try:
                    design = db_uu.open_design(dsn)
                except Exception as exc:
                    read_notes.append(f"打不开 {dsn}：{exc}")
                    continue
                parts.extend(_parts_on_design(design, cell_name, catalog_for))
    finally:
        if de.workspace_is_open():
            de.close_workspace()
    values, warnings = resolve_enabled_values(parts)
    return values, read_notes + warnings
