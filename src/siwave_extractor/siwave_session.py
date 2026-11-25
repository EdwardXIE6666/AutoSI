"""SIwave session abstraction: a COM-backed implementation and a test fake."""
from __future__ import annotations

import glob as _glob
import logging
import os
import re
from collections import defaultdict
from datetime import datetime
from typing import Protocol, runtime_checkable

from rf.segments import clamp_rf_sweep_start_hz

_log = logging.getLogger(__name__)


def _parse_freq(s: str) -> float:
    """Parse '600MHz', '600MHZ', '1.5GHz' etc. to Hz."""
    s = s.strip()
    su = s.upper()
    for suffix, mult in (("GHZ", 1e9), ("MHZ", 1e6), ("KHZ", 1e3), ("HZ", 1.0)):
        if su.endswith(suffix):
            return float(s[:-len(suffix)]) * mult
    return float(s)


def _reformat_snp(snp_path: str) -> None:
    """Rewrite a SIwave-exported .sNp file in Full-Wave-Spice compatible format.

    Changes applied:
      - Option line: '# Hz S MA R 50.000000' -> '# HZ S MA R 50'
      - Number precision: 13 sig-figs -> 17 sig-figs
      - Continuation indent: 19 spaces -> 23 spaces
      - Layout: row-per-S-matrix-row -> continuous 4-pairs-per-line
      - Header restructured to match Full-Wave Spice style
      - Port comments moved to after the option line
    """
    with open(snp_path, encoding="ascii", errors="replace") as f:
        raw = f.read()
    lines = raw.splitlines()

    siwave_comments: list[str] = []
    port_comments: list[str] = []
    option_line: str = ""
    freqs: list[float] = []
    data: dict[float, list[float]] = {}

    scale = 1.0
    for line in lines:
        s = line.strip()
        if not s:
            continue
        if s.startswith("!"):
            if re.match(r"^! Port\[", s):
                port_comments.append(s)
            elif s not in ("! S-parameters", "! Modal data exported"):
                siwave_comments.append(s)
            continue
        if s.startswith("#"):
            option_line = s
            su = s.upper()
            if "GHZ" in su:
                scale = 1e9
            elif "MHZ" in su:
                scale = 1e6
            elif "KHZ" in su:
                scale = 1e3
            else:
                scale = 1.0
            continue
        nums = [float(x) for x in s.split()]
        if not nums:
            continue
        if not line[0].isspace() and nums[0] > 1e4:
            f = nums[0] * scale
            freqs.append(f)
            data[f] = list(nums[1:])
        elif freqs:
            data[freqs[-1]].extend(nums)

    # Build new option line: uppercase freq unit, integer impedance
    opt = re.sub(r"\bHz\b", "HZ", option_line, flags=re.IGNORECASE)
    opt = re.sub(r"\bR\s+[\d.]+", lambda m: f"R {int(float(m.group(0).split()[1]))}", opt)
    opt = opt.strip()

    project = os.path.splitext(os.path.basename(snp_path))[0]
    ts = datetime.now().strftime("%a %b %d %H:%M:%S %Y")
    indent = " " * 23
    pairs_per_line = 4  # 8 values per continuation line

    out_lines: list[str] = [
        "! Touchstone file exported from Ansys Full-Wave Spice",
        f"! Project: {project}",
        "!  Design: unknown",
        f"! Creation time: {ts}",
    ]
    for i, c in enumerate(siwave_comments):
        prefix = "! Notes: " if i == 0 else "!      : "
        out_lines.append(f"{prefix}{c}")
    out_lines.append("")
    out_lines.append(opt)
    out_lines.append("! Modal data exported")
    out_lines.extend(port_comments)
    out_lines.append("")

    for f in freqs:
        vals = data[f]
        # All S-param pairs in flat order, written 4 pairs (8 values) per line
        # First line: freq + first 4 pairs
        all_vals: list[str] = [f"{v:.16e}" for v in vals]
        chunks = [all_vals[i:i + 8] for i in range(0, len(all_vals), 8)]
        first = f"{f:.16e} " + " ".join(chunks[0]) + " "
        out_lines.append(first)
        for chunk in chunks[1:]:
            out_lines.append(indent + " ".join(chunk) + " ")
        out_lines.append("")

    with open(snp_path, "w", encoding="ascii") as f:
        f.write("\n".join(out_lines))
    _log.info("reformat_snp: reformatted %s", snp_path)


_SIW_NET_RE = re.compile(r"^\s+(\d+)\s+(\S+)\s+\d+\s+\d+\s+0x[0-9a-fA-F]+\s+\d+")


def list_siw_net_names(siw_path: str) -> set[str]:
    """从 .siw 文本读出网络名。"""
    names: set[str] = set()
    with open(siw_path, encoding="latin-1") as f:
        for line in f:
            m = _SIW_NET_RE.match(line)
            if m:
                names.add(m.group(2))
    return names


def missing_siw_nets(siw_path: str, requested) -> list[str]:
    """网络表里有、板里没有的网名。空列表表示都能对上。"""
    have = list_siw_net_names(siw_path)
    return [n for n in requested or () if n not in have]


def scr_select_net_ok(rc) -> bool:
    """ScrSelectNet 是 BOOL：1 选中成功，0 失败。网在板里时成功也回 1。"""
    try:
        return int(rc) == 1
    except (TypeError, ValueError):
        return False


def _compute_series_ports(siw_path: str, selected_nets: list[str],
                          zref: float = 50.0, skip_refdes=None) -> list[tuple]:
    """Parse a saved .siw and return [(port_name, px, py, nx, ny), ...] for series
    components (no GND pin) on selected nets, with the negative terminal at the
    nearest GND pad. Shared by the text-inject path and the COM ScrDrawPort path."""
    with open(siw_path, encoding="latin-1") as f:
        lines = f.read().split("\n")

    # Parse net ID ↔ name  (lines like:  "  209 $$$17066 0 0 0x09517b 0")
    net_name_by_id: dict[int, str] = {}
    net_id_by_name: dict[str, int] = {}
    for line in lines:
        m = _SIW_NET_RE.match(line)
        if m:
            nid, nname = int(m.group(1)), m.group(2)
            net_name_by_id[nid] = nname
            net_id_by_name[nname] = nid

    # Parse component pins
    # "  X <netID> 2 2 <layer> <x> <y> <angle> <part> <refdes> <pin> F ..."
    _pin_re = re.compile(
        r"^\s+X\s+(\d+)\s+\d+\s+\d+\s+(\d+)\s+"
        r"([\d.eE+\-]+)\s+([\d.eE+\-]+)\s+[\d.eE+\-]+\s+"
        r"(\S+)\s+(\S+)\s+(\S+)\s+F"
    )
    all_pins: list[tuple] = []
    for line in lines:
        m = _pin_re.match(line)
        if m:
            all_pins.append((
                int(m.group(1)), int(m.group(2)),
                float(m.group(3)), float(m.group(4)),
                m.group(5), m.group(6), m.group(7),
            ))

    gnd_id = net_id_by_name.get("GND", 1)
    gnd_pins = [(p[1], p[2], p[3]) for p in all_pins if p[0] == gnd_id]
    if not gnd_pins:
        # After clip, component GND pads may all be outside the design boundary.
        # Fall back to the neg-terminal coords of existing PORT entries as GND refs.
        _port_neg_re = re.compile(
            r'\s*PORT\s+"[^"]+"\s+[\d.eE+\-]+\s+[\d.eE+\-]+\s+([\d.eE+\-]+)\s+([\d.eE+\-]+)'
        )
        for line in lines:
            m = _port_neg_re.match(line)
            if m:
                gnd_pins.append((0, float(m.group(1)), float(m.group(2))))
    if not gnd_pins:
        _log.warning("inject_series_ports: no GND pins found, skipping")
        return

    selected_ids = {net_id_by_name[n] for n in selected_nets if n in net_id_by_name}
    if not selected_ids:
        _log.warning("inject_series_ports: none of %s found in .siw net list", selected_nets)
        return

    comp_net_ids: dict[str, set] = defaultdict(set)
    comp_pins: dict[str, list] = defaultdict(list)
    for net_id, layer, x, y, part, refdes, pin_num in all_pins:
        comp_net_ids[refdes].add(net_id)
        comp_pins[refdes].append((net_id, layer, x, y, part, pin_num))

    existing = {
        m.group(1)
        for line in lines
        for m in [re.match(r'\s*PORT "([^"]+)"', line)]
        if m
    }

    def _nearest_gnd(x: float, y: float) -> tuple:
        return min(gnd_pins, key=lambda p: (p[1] - x) ** 2 + (p[2] - y) ** 2)

    skip = {str(x) for x in (skip_refdes or ()) if x}
    new_ports: list[tuple] = []
    for refdes in sorted(comp_net_ids):
        if refdes in skip:
            continue
        if not (comp_net_ids[refdes] & selected_ids):
            continue
        # R/L/C 在 SIwave 里是 INACTIVE，匹配焊盘必须建口给 ADS 接分立件。
        # 不再因"器件带 GND 脚"就整体跳过。ScrPlacePortsAtPinsOnSelectedNets 只给普通焊盘
        # 器件建端口，对以电路元件(B_CIRC_ELEM，如 FILTER_SINGLE 低通滤波器)建模、又带 GND
        # 脚的器件会整颗漏建端口。改为逐脚处理：内置已建好的普通并联
        # 器件靠下面的 existing 去重跳过、不会重复；内置漏掉的电路元件信号脚在此补建端口。
        for net_id, layer, x, y, part, pin_num in comp_pins[refdes]:
            if net_id not in selected_ids:
                continue
            net_name = net_name_by_id.get(net_id, str(net_id))
            port_name = f"{net_name}_{refdes}_{pin_num}"
            if port_name in existing:
                continue
            _, gx, gy = _nearest_gnd(x, y)
            new_ports.append((port_name, x, y, gx, gy))

    return new_ports


def _top_layer_name(siw_path: str) -> str:
    """Return the name of metal layer index 2 (the top conductor) from a .siw.

    The text-inject hardcodes pnl/nnl = 2; ScrDrawPort needs the layer *name*, so
    we resolve index 2 → its name (e.g. `top`) to stay board-independent."""
    _l2_re = re.compile(r"^\s*2\s+`([^`]+)`\s+METAL")
    try:
        with open(siw_path, encoding="latin-1") as f:
            for line in f:
                m = _l2_re.match(line)
                if m:
                    return m.group(1)
    except OSError:
        pass
    return "top"


def _read_rlc_circuit_elements(siw_path: str) -> list[tuple[str, str]]:
    """Return (refdes, SIwave API type) for CAP/IND/RES circuit elements."""
    type_map = {"CAP": "cap", "IND": "ind", "RES": "res"}
    elements: list[tuple[str, str]] = []
    with open(siw_path, encoding="latin-1") as f:
        lines = f.read().splitlines()
    try:
        start = next(i for i, line in enumerate(lines)
                     if line.strip() == "B_CIRCUIT_ELEMENTS") + 1
        end = next(i for i in range(start, len(lines))
                   if lines[i].strip() == "E_CIRCUIT_ELEMENTS")
    except StopIteration:
        return elements
    for line in lines[start:end]:
        parts = line.strip().split()
        if len(parts) >= 2 and parts[0] in type_map:
            elements.append((parts[1].strip('"'), type_map[parts[0]]))
    return elements


def _inject_series_ports(siw_path: str, selected_nets: list[str], zref: float = 50.0,
                         skip_refdes=None) -> None:
    """Inject PORT entries for series-component pins into a saved .siw subproject.

    ScrPlacePortsAtPinsOnSelectedNets skips components with no GND pin (series topology).
    Text-patch the .siw to add those missing ports, using the nearest GND pad as the
    negative terminal. Must be called while the project is closed.
    """
    new_ports = _compute_series_ports(siw_path, selected_nets, zref, skip_refdes)
    if not new_ports:
        return

    with open(siw_path, encoding="latin-1") as f:
        lines = f.read().split("\n")

    port_lines = [
        f'PORT "{n}" {px:.4f} {py:.4f} {nx:.4f} {ny:.4f} '
        f'2 2 {px:.4f} {ny:.4f} {zref:.0f} 0 0 1 1 -1 -1 '
        f'0 0 0 0 0 0 0 0 0 0 0'
        for n, px, py, nx, ny in new_ports
    ]
    class_lines = [f"{n} UNASSIGNED" for n, *_ in new_ports]

    e_circ = next(i for i, l in enumerate(lines) if l.strip() == "E_CIRCUIT_ELEMENTS")
    lines[e_circ:e_circ] = port_lines

    e_class = next(i for i, l in enumerate(lines) if l.strip() == "E_PORT_CLASSIFICATION")
    lines[e_class:e_class] = class_lines

    with open(siw_path, "w", encoding="latin-1") as f:
        f.write("\n".join(lines))

    _log.info("inject_series_ports: added %d ports to %s", len(new_ports), siw_path)


def odb_er0_fill_reason(siw_path: str) -> str:
    """源板若仍用 ODB++ 默认介质（Er=0）作金属填充，SYZ 会报 0501。

    返回空字符串表示可求解；否则返回应提示用户的原因（先导入叠层）。
    """
    try:
        with open(siw_path, encoding="latin-1", errors="replace") as f:
            text = f.read()
    except OSError:
        return ""
    er0: list[str] = []
    for line in text.splitlines():
        s = line.strip()
        if not s.startswith("INSULATOR "):
            continue
        if "ODB_DIEL" not in s:
            continue
        parts = s.split()
        if len(parts) < 3:
            continue
        try:
            er = float(parts[2])
        except ValueError:
            continue
        if er <= 0.0:
            er0.append(parts[1].strip('"'))
    if not er0:
        return ""
    fills = ""
    if "B_USERDEFINED_DIELECTRIC_FILLS" in text:
        fills = text.split("B_USERDEFINED_DIELECTRIC_FILLS", 1)[-1]
        fills = fills.split("E_USERDEFINED_DIELECTRIC_FILLS", 1)[0]
    used = []
    for name in er0:
        token = f'"{name}"'
        if token in fills:
            used.append(name)
            continue
        for line in text.splitlines():
            s = line.strip()
            if " DIELECTRIC " not in s and " METAL " not in s:
                continue
            if token in s:
                used.append(name)
                break
    if not used:
        return ""
    return (
        "叠层未导入：金属填充介质 "
        + ", ".join(used)
        + " Er=0（error 0501）。请先导入叠层再抽取"
    )


def _clear_siw_dest(path: str) -> None:
    """删掉目标 .siw 及旁路锁，避免 ScrSaveProjectAs 弹出覆盖确认。"""
    for extra in (path, path + ".lock"):
        try:
            if extra and os.path.isfile(extra):
                os.remove(extra)
        except OSError:
            pass


def _latest_simlog(siw_path: str) -> str:
    """重试会产生 0001/0002_SYZ_Sweep_1，不能写死 0000。"""
    root = os.path.splitext(siw_path)[0] + ".siwaveresults"
    newest, newest_t = "", -1.0
    if not os.path.isdir(root):
        return ""
    for dirpath, _dirs, files in os.walk(root):
        for fn in files:
            if not fn.endswith(".simlog"):
                continue
            path = os.path.join(dirpath, fn)
            try:
                mtime = os.path.getmtime(path)
            except OSError:
                continue
            if mtime >= newest_t:
                newest, newest_t = path, mtime
    return newest


def _format_solve_error(lines) -> str:
    """把 simlog 译成可执行的根因，0518 明确不是原理图问题。"""
    warn_0311: list[str] = []
    errs: list[str] = []
    for ln in lines:
        low = ln.lower()
        seg = ln.split("siwave_ng.exe:", 1)[-1].strip() if "siwave_ng.exe:" in ln else ln.strip()
        if "warning 0311" in low:
            if seg not in warn_0311:
                warn_0311.append(seg)
        elif (("error 0501" in low) or ("validation failed" in low)
              or ("exit code" in low) or ("error 05" in low)):
            if seg not in errs:
                errs.append(seg)
    if any("0518" in e or "memory access violation" in e.lower() for e in errs):
        msg = (
            "不是原理图问题。SIwave 裁板后几何预处理崩溃（error 0518 Memory access "
            "violation），求解器在 Processing traces 时访问违例。"
        )
        if warn_0311:
            msg += " 裁切边有自交走线：" + "；".join(warn_0311[:2])
        msg += " 请打开该段裁剪后的 .siw 检查裁区；可升级/修补 SIwave 2023 R1。"
        return msg
    return " | ".join(errs)[:400]


def _read_solve_error(siw_path: str) -> str:
    """读最新 SYZ simlog，提取层叠/Er/0518 根因。"""
    simlog = _latest_simlog(siw_path)
    if not simlog:
        return ""
    try:
        with open(simlog, encoding="latin-1", errors="replace") as f:
            lines = f.read().splitlines()
    except OSError:
        return ""
    return _format_solve_error(lines)


class _Watchdog:
    """Kill SIwave.exe if a COM call exceeds `timeout` seconds. Use as a context manager."""

    def __init__(self, timeout: float):
        import threading
        self._event = threading.Event()
        self._t = threading.Thread(target=self._guard, args=(timeout,), daemon=True)

    def _guard(self, timeout: float) -> None:
        if not self._event.wait(timeout):
            import subprocess
            _log.warning("Watchdog: COM call timed out (%.0fs); killing SIwave.exe", timeout)
            subprocess.run(["taskkill", "/f", "/im", "SIwave.exe"],
                           capture_output=True, check=False)

    def __enter__(self):
        self._t.start()
        return self

    def __exit__(self, *_):
        self._event.set()


@runtime_checkable
class SiwaveSession(Protocol):
    def open_project(self, path: str) -> None: ...
    def import_odb(self, odb_path: str, control_xml: str = "",
                   out_siw: str | None = None) -> None: ...
    def import_stackup(self, xml_path: str) -> int: ...
    def set_rlc_circuit_elements_active(self, project_path: str, active: bool) -> tuple[int, int]: ...
    def deactivate_rlc_circuit_elements(self, project_path: str) -> tuple[int, int]: ...
    def generate_ports(self, net_names: list[str]) -> None: ...
    def clip_around_nets(self, nets: list[str], margin: str) -> None: ...
    def save_as(self, path: str) -> None: ...
    def save(self) -> None: ...
    def inject_series_ports(self, path: str, net_names: list[str],
                            skip_refdes=None) -> None: ...
    def draw_series_ports(self, path: str, net_names: list[str],
                          skip_refdes=None) -> None: ...
    def patch_syz_sweep(self, path: str, start: str, stop: str, points: int) -> None: ...
    def set_syz_sweep(self, start: str, stop: str, points: int) -> None: ...
    def set_num_cpus(self, n: int) -> None: ...
    def run_simulation(self, net: str) -> None: ...
    def export_snp(self, path: str) -> None: ...
    def close_project(self) -> None: ...


class ComSiwaveSession:
    """COM-backed SIwave driver. Requires pywin32 and an installed SIwave."""

    def __init__(self, ref_net: str = "GND", com_timeout: int = 300) -> None:
        import time
        import win32com.client.dynamic  # imported lazily so unit tests don't need pywin32
        import subprocess
        # Kill stale SIwave processes to avoid "outstanding COM references" errors
        subprocess.run(["taskkill", "/f", "/im", "SIwave.exe"],
                       capture_output=True, check=False)
        time.sleep(2)  # wait for killed process to fully exit before launching new instance
        self._ref_net = ref_net
        self._com_timeout = com_timeout
        _log.info("Dispatching SIwave.Application")
        # Use dynamic.Dispatch (late binding) to bypass any gen_py type-library cache
        self._app = win32com.client.dynamic.Dispatch("SIwave.Application")
        # RestoreWindow can block if SIwave is still initializing — guard with a timeout
        with _Watchdog(120):
            self._app.RestoreWindow()
        self._doc = None
        self._siw_path = None

    def restart(self) -> None:
        """Kill SIwave and re-establish the COM connection after a crash."""
        import subprocess
        import time
        import win32com.client.dynamic
        _log.info("Restarting SIwave COM session")
        subprocess.run(["taskkill", "/f", "/im", "SIwave.exe"],
                       capture_output=True, check=False)
        time.sleep(2)
        self._app = win32com.client.dynamic.Dispatch("SIwave.Application")
        with _Watchdog(120):
            self._app.RestoreWindow()
        self._doc = None
        self._siw_path = None

    @property
    def doc(self):
        if self._doc is None:
            self._doc = self._app.GetActiveProject()
        return self._doc

    def open_project(self, path: str) -> None:
        with self._ctx():
            self._app.OpenProject(path)
            self._doc = self._app.GetActiveProject()
        if self._doc is None:
            raise RuntimeError(f"OpenProject returned no document: {path}")
        self._siw_path = path

    def import_odb(self, odb_path: str, control_xml: str = "",
                   out_siw: str | None = None) -> None:
        # ImportOdb 是 ISIwaveApplication 接口方法（本机 siwave.tlb 确认：
        # def ImportOdb(self, odbBstr, xmlBstr)），在 Application 对象上调用，
        # 与 OpenProject 同级。第一参数 ODB++ 文件名（.tgz），第二参数 XML 控制
        # 文件路径（不用传空串，见 SIwave Help）。导入后 SIwave 生成一个 untitled
        # 新工程，需要 save_as 落盘成 .siw 供后续抽取/初始化使用。
        _log.info("import_odb: ImportOdb(odb=%s, xml=%r)", odb_path, control_xml)
        with self._ctx():
            self._app.ImportOdb(odb_path, control_xml)
        self._doc = self._app.GetActiveProject()
        if self._doc is None:
            raise RuntimeError(
                f"ImportOdb completed but no active SIwave project: {odb_path}")
        if out_siw:
            _log.info("import_odb: saving imported project -> %s", out_siw)
            self.save_as(out_siw)


    def _ctx(self):
        from contextlib import nullcontext
        return _Watchdog(self._com_timeout) if self._com_timeout > 0 else nullcontext()

    def import_stackup(self, xml_path: str) -> int:
        # SIwave 有两个叠层导入 API：ScrImportLayerStackup 吃 .stk（原生私有格式），
        # ScrImportLayerStackupXML 吃 .xml（Ansys Control/Stackup XML，板材库即此格式）。
        # 此前误用前者喂 XML：API 不报错、返回 1（BOOL: 1=Success），叠层却未真正应用，
        # 表现为“初始化成功但 SIwave 里叠层没变”。返回值 BOOL：1=Success, 0=Failure。
        with self._ctx():
            ret = self.doc.ScrImportLayerStackupXML(xml_path)
        _log.info("import_stackup: ScrImportLayerStackupXML(%s) -> %s", xml_path, ret)
        if ret != 1:
            raise RuntimeError(
                f"ScrImportLayerStackupXML returned {ret} (1=success, 0=failure) "
                f"for {xml_path}")
        return ret

    def set_rlc_circuit_elements_active(self, project_path: str,
                                        active: bool) -> tuple[int, int]:
        """Set CAP/IND/RES circuit elements active/inactive in the active SIwave project."""
        elements = _read_rlc_circuit_elements(project_path)
        failed = 0
        with self._ctx():
            for name, elem_type in elements:
                result = self.doc.ScrActivateCktElem(name, elem_type, active)
                ret = result[0] if isinstance(result, tuple) else result
                if ret is not None and ret < 0:
                    failed += 1
                    _log.warning(
                        "ScrActivateCktElem(%s, %s, %s) returned %s",
                        name, elem_type, active, ret)
        if failed:
            raise RuntimeError(
                f"Set R/L/C circuit elements active={active} failed: "
                f"{failed}/{len(elements)}")
        _log.info("Set %d R/L/C circuit element(s) active=%s", len(elements), active)
        return len(elements), failed

    def deactivate_rlc_circuit_elements(self, project_path: str) -> tuple[int, int]:
        """Deactivate CAP/IND/RES circuit elements in the active SIwave project."""
        return self.set_rlc_circuit_elements_active(project_path, False)

    def generate_ports(self, net_names: list[str]) -> None:
        path = getattr(self, "_siw_path", None)
        if path:
            miss = missing_siw_nets(path, net_names)
            if miss:
                raise RuntimeError("网络名对不上，板里没有: " + ",".join(miss))
        with self._ctx():
            bad = []
            for n in net_names:
                r = self.doc.ScrSelectNet(n, 1)
                ret = r[0] if isinstance(r, tuple) else r
                if not scr_select_net_ok(ret):
                    bad.append(f"{n}(ret={ret})")
            if bad:
                raise RuntimeError("网络未选中: " + ",".join(bad))
            r = self.doc.ScrPlacePortsAtPinsOnSelectedNets(50.0, self._ref_net, 0, [])
        ret = r[0] if isinstance(r, tuple) else r
        if ret != 0:
            raise RuntimeError(f"ScrPlacePortsAtPinsOnSelectedNets returned {ret}")

    def clip_around_nets(self, nets: list[str], margin: str) -> None:
        with self._ctx():
            # simplifiedExtent=True：官方示例如此。False 时裁切边不简化，
            # 几何预处理常 0311 自交，随后 siwave_ng 0518 Memory access violation。
            ret = self.doc.ScrClipDesignAroundNets(nets, margin, True, 0, True, False)
        if ret < 0:
            raise RuntimeError(f"ScrClipDesignAroundNets returned {ret}")

    def save_as(self, path: str) -> None:
        """另存为 path，并让当前文档指向该文件。先清掉目标，避免覆盖确认框。"""
        _clear_siw_dest(path)
        with self._ctx():
            self.doc.ScrSaveProjectAs(path)
        if not os.path.exists(path):
            raise RuntimeError(f"ScrSaveProjectAs: file not found after save: {path}")
        self._siw_path = path  # 记录当前工程路径，供求解失败时读 simlog

    def save(self) -> None:
        """保存当前工程，不弹确认框。当前文档必须已经有路径（先 save_as）。"""
        path = getattr(self, "_siw_path", None)
        with self._ctx():
            saved = False
            try:
                self.doc.Save()
                saved = True
            except Exception:  # noqa: BLE001
                pass
            if not saved:
                if not path:
                    raise RuntimeError("save: no current project path")
                self.doc.ScrSaveProjectAs(path)
        if path and not os.path.exists(path):
            raise RuntimeError(f"save: file not found after save: {path}")

    def inject_series_ports(self, path: str, net_names: list[str],
                            skip_refdes=None) -> None:
        _inject_series_ports(path, net_names, skip_refdes=skip_refdes)

    def draw_series_ports(self, path: str, net_names: list[str],
                          skip_refdes=None) -> None:
        """COM 版串联端口：开着工程时用 ScrDrawPort 画端口，替代关闭后的文本注入。

        端口坐标/负极接最近 GND 的探测逻辑与 _inject_series_ports 完全一致，只是
        改用 COM 落到打开的工程上，从而省掉 close→patch→reopen 的重开。
        """
        ports = _compute_series_ports(path, list(net_names), skip_refdes=skip_refdes)
        if not ports:
            return
        layer = _top_layer_name(path)
        with self._ctx():
            for name, px, py, nx, ny in ports:
                ret = self.doc.ScrDrawPort(name, float(px), float(py),
                                           float(nx), float(ny), layer, layer, 50.0)
                if ret < 0:
                    raise RuntimeError(f"ScrDrawPort({name}) returned {ret}")
        _log.info("draw_series_ports: drew %d series port(s) via ScrDrawPort", len(ports))

    def set_syz_sweep(self, start: str, stop: str, points: int) -> None:
        """COM 版 SYZ 扫频：开着工程时直接设，替代关闭后的文本补丁。

        关键是 sweepType 传 "SYZ"（旧代码错传成默认的 "AC" 才需要文本补丁）。
        ScrClearAllSweeps 先清掉原 SYZ 块，ScrAppendSweep 写入唯一一个新块。
        """
        start_hz = clamp_rf_sweep_start_hz(_parse_freq(start))
        stop_hz = _parse_freq(stop)
        with self._ctx():
            self.doc.ScrClearAllSweeps("SYZ")
            r = self.doc.ScrAppendSweep("SYZ", start_hz, stop_hz, points, 0)
        ret = r[0] if isinstance(r, tuple) else r
        if ret != 1:
            raise RuntimeError(f"ScrAppendSweep('SYZ') returned {ret}")

    def patch_syz_sweep(self, path: str, start: str, stop: str, points: int) -> None:
        """Set the SYZ sweep range by editing the NAME "SYZ" block in the .siw text.

        The COM Scr* sweep setters write the wrong named sweep (e.g. "AC"), not the
        "SYZ" sweep the SYZ simulation actually reads. The project must be closed
        before calling this so the edit is picked up on reopen.
        """
        start_hz = clamp_rf_sweep_start_hz(_parse_freq(start))
        stop_hz = _parse_freq(stop)
        with open(path, encoding="latin-1") as f:
            lines = f.read().split("\n")
        in_syz = False
        found = False
        for i, line in enumerate(lines):
            s = line.strip()
            if s == 'NAME "SYZ"':
                in_syz = True
                found = True
                continue
            if in_syz:
                if s.startswith("START"):
                    lines[i] = f"   START {start_hz:.16e}"
                elif s.startswith("STOP"):
                    lines[i] = f"   STOP {stop_hz:.16e}"
                elif s.startswith("NUM_POINTS"):
                    lines[i] = f"   NUM_POINTS {points}"
                elif s == "E_SWEEP":
                    break
        if not found:
            raise RuntimeError(f'patch_syz_sweep: NAME "SYZ" block not found in {path}')
        with open(path, "w", encoding="latin-1") as f:
            f.write("\n".join(lines))

    def set_num_cpus(self, n: int) -> None:
        """让 SYZ 求解用 n 个 CPU 核（提速）。非致命：失败则退回 SIwave 默认核数。"""
        if n <= 0:
            return
        try:
            with self._ctx():
                self.doc.ScrSetNumCpusToUse(n)
            _log.info("ScrSetNumCpusToUse(%d) set", n)
        except Exception as e:  # noqa: BLE001
            _log.warning("ScrSetNumCpusToUse(%d) failed (用默认核数继续): %s", n, e)

    def run_simulation(self, net: str) -> None:
        with self._ctx():
            ret = self.doc.ScrRunSyzParameterSimulation()
        # SIwave 求解失败返回正数（如 61=层叠验证失败）或负数；仅 0 为成功
        if ret != 0:
            err = _read_solve_error(getattr(self, "_siw_path", ""))
            if err:
                raise RuntimeError(err)
            raise RuntimeError(f"ScrRunSyzParameterSimulation returned {ret}")

    def export_snp(self, path: str) -> None:
        for old in _glob.glob(path + ".s*p"):
            try:
                os.remove(old)
            except OSError:
                pass
        with self._ctx():
            ret = self.doc.ScrExportToTouchstone(path)
        if ret != 0:
            raise RuntimeError(f"ScrExportToTouchstone returned {ret}")
        matches = _glob.glob(path + ".s*p")
        if not matches:
            raise RuntimeError(f"ScrExportToTouchstone: no output file found at {path}.s*p")
        for snp in matches:
            _reformat_snp(snp)

    def close_project(self) -> None:
        try:
            with self._ctx():
                self.doc.ScrCloseProjectNoSave()
        except Exception:  # noqa: BLE001
            _log.debug("close_project: no active project to close")
        self._doc = None
