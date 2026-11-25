"""并行 SYZ 抽取：若干独立 siwave.exe，谁先做完谁接下一段。

绕过 COM 单例（`Dispatch("SIwave.Application")` 多进程会附着到同一实例）。
每路一份未裁整板副本；一段结束即关掉该路 SIwave，从公共队列取下一段再开，
不等另外几路结束。
"""
from __future__ import annotations

import glob as _glob
import logging
import os
import shutil
import subprocess
import time
from typing import Mapping, Sequence

from rf.segments import clamp_rf_sweep_start_hz, rf_sweep_point_count, safe_component_name

from .config import Config
from .netlist import NetInfo
from .project_paths import build_subproject, subproject_file
from .siwave_session import _reformat_snp
from .workflow import ComponentResult, _bar

_log = logging.getLogger(__name__)


def physical_ram_gb() -> float:
    """本机物理内存（GB）。读不到时按 16GB 估，避免 4 路打满小内存机器。"""
    try:
        import ctypes

        class _MemoryStatusEx(ctypes.Structure):
            _fields_ = [
                ("dwLength", ctypes.c_ulong),
                ("dwMemoryLoad", ctypes.c_ulong),
                ("ullTotalPhys", ctypes.c_ulonglong),
                ("ullAvailPhys", ctypes.c_ulonglong),
                ("ullTotalPageFile", ctypes.c_ulonglong),
                ("ullAvailPageFile", ctypes.c_ulonglong),
                ("ullTotalVirtual", ctypes.c_ulonglong),
                ("ullAvailVirtual", ctypes.c_ulonglong),
                ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
            ]

        status = _MemoryStatusEx()
        status.dwLength = ctypes.sizeof(_MemoryStatusEx)
        if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status)):
            return status.ullTotalPhys / (1024 ** 3)
    except Exception:
        pass
    return 16.0


def bound_parallel_workers(requested: int, ram_gb: float) -> int:
    """同时载入多份整板受内存限制：大约每路 6GB，16GB 机器最多 2 路。"""
    requested = max(1, int(requested))
    cap = max(1, int(ram_gb // 6))
    return min(requested, cap)


def source_copy_path(work_dir: str, component: str) -> str:
    return os.path.join(work_dir, safe_component_name(component) + ".src.siw")


def copy_project_for_job(project_path: str, dest: str) -> str:
    """每路一份整板副本，避免多个 siwave.exe 抢读同一文件。"""
    os.makedirs(os.path.dirname(dest) or ".", exist_ok=True)
    shutil.copy2(project_path, dest)
    return dest


def cleanup_source_copy(dest: str) -> None:
    if dest and os.path.isfile(dest):
        try:
            os.remove(dest)
        except OSError:
            pass
    results = os.path.splitext(dest)[0] + ".siwaveresults" if dest else ""
    if results and os.path.isdir(results):
        shutil.rmtree(results, ignore_errors=True)


def write_job_file(path: str, job: dict) -> None:
    """写给 IronPython worker 的任务文件：ascii 转义，避免中文路径解码失败。"""
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="ascii") as f:
        f.write(ascii(job))
    os.replace(tmp, path)


def _find_siwave_exe() -> str:
    """定位 siwave.exe：优先环境变量，回退标准安装路径。"""
    for var in ("SIWAVE_INSTALL_DIR", "ANSYSEM_ROOT231"):
        d = os.environ.get(var)
        if d:
            exe = os.path.join(d, "siwave.exe")
            if os.path.exists(exe):
                return exe
    for exe in _glob.glob(r"C:\Program Files\AnsysEM\v*\Win64\siwave.exe"):
        return exe
    raise RuntimeError("找不到 siwave.exe（请设置环境变量 ANSYSEM_ROOT231）")


# IronPython 2.7 脚本体：读取上方注入的 PARAMS 字典，跑完整抽取流程。
# 与 workflow.extract_component 的 Scr* 序列一致；串联端口探测移植自
# siwave_session._compute_series_ports（IronPython 2.7 下 str 即字节，读 latin-1 .siw 免解码）。
_SCRIPT_BODY = r'''
import os, re, sys, traceback, glob
from collections import defaultdict

def utf8_line(s):
    # IronPython 2 默认按系统 ANSI（中文机 GBK）写文件；宿主 Python3 再按 latin-1 读就会乱码。
    try:
        return s.encode("utf-8") + "\n"
    except Exception:
        return str(s) + "\n"

def compute_series_ports(siw_path, selected_nets, skip_refdes=None):
    lines = open(siw_path).read().split("\n")
    net_name_by_id = {}; net_id_by_name = {}
    net_re = re.compile(r"^\s+(\d+)\s+(\S+)\s+\d+\s+\d+\s+0x[0-9a-fA-F]+\s+\d+")
    for line in lines:
        m = net_re.match(line)
        if m:
            nid = int(m.group(1)); nm = m.group(2)
            net_name_by_id[nid] = nm; net_id_by_name[nm] = nid
    pin_re = re.compile(
        r"^\s+X\s+(\d+)\s+\d+\s+\d+\s+(\d+)\s+"
        r"([\d.eE+\-]+)\s+([\d.eE+\-]+)\s+[\d.eE+\-]+\s+"
        r"(\S+)\s+(\S+)\s+(\S+)\s+F")
    all_pins = []
    for line in lines:
        m = pin_re.match(line)
        if m:
            all_pins.append((int(m.group(1)), int(m.group(2)),
                             float(m.group(3)), float(m.group(4)),
                             m.group(5), m.group(6), m.group(7)))
    gnd_id = net_id_by_name.get("GND", 1)
    gnd_pins = [(p[1], p[2], p[3]) for p in all_pins if p[0] == gnd_id]
    if not gnd_pins:
        pn_re = re.compile(r'\s*PORT\s+"[^"]+"\s+[\d.eE+\-]+\s+[\d.eE+\-]+\s+([\d.eE+\-]+)\s+([\d.eE+\-]+)')
        for line in lines:
            m = pn_re.match(line)
            if m:
                gnd_pins.append((0, float(m.group(1)), float(m.group(2))))
    if not gnd_pins:
        return []
    selected_ids = set(net_id_by_name[n] for n in selected_nets if n in net_id_by_name)
    if not selected_ids:
        return []
    comp_net_ids = defaultdict(set); comp_pins = defaultdict(list)
    for (net_id, layer, x, y, part, refdes, pin_num) in all_pins:
        comp_net_ids[refdes].add(net_id)
        comp_pins[refdes].append((net_id, layer, x, y, part, pin_num))
    existing = set()
    for line in lines:
        m = re.match(r'\s*PORT "([^"]+)"', line)
        if m: existing.add(m.group(1))
    def nearest_gnd(x, y):
        best = None; bd = None
        for p in gnd_pins:
            d = (p[1]-x)**2 + (p[2]-y)**2
            if bd is None or d < bd: bd = d; best = p
        return best
    skip = set(str(x) for x in (skip_refdes or []) if x)
    new_ports = []
    for refdes in sorted(comp_net_ids.keys()):
        if refdes in skip: continue
        if not (comp_net_ids[refdes] & selected_ids): continue
        # R/L/C INACTIVE：匹配焊盘必须建口。GND 脚器件也不整颗跳过。
        # circuit-element parts (B_CIRC_ELEM, e.g. FILTER_SINGLE LPF) that also
        # have GND pins; rely on the per-pin `existing` dedup below instead.
        for (net_id, layer, x, y, part, pin_num) in comp_pins[refdes]:
            if net_id not in selected_ids: continue
            net_name = net_name_by_id.get(net_id, str(net_id))
            port_name = "%s_%s_%s" % (net_name, refdes, pin_num)
            if port_name in existing: continue
            g = nearest_gnd(x, y)
            new_ports.append((port_name, x, y, g[1], g[2]))
    return new_ports

def missing_nets(siw_path, requested):
    names = {}
    net_re = re.compile(r"^\s+(\d+)\s+(\S+)\s+\d+\s+\d+\s+0x[0-9a-fA-F]+\s+\d+")
    for line in open(siw_path).read().split("\n"):
        m = net_re.match(line)
        if m:
            names[m.group(2)] = 1
    miss = []
    for n in requested:
        if n not in names:
            miss.append(n)
    return miss

def top_layer_name(siw_path):
    l2 = re.compile(r"^\s*2\s+`([^`]+)`\s+METAL")
    for line in open(siw_path):
        m = l2.match(line)
        if m: return m.group(1)
    return "top"

def read_solve_error(out_siw):
    # 重试会产生 0001/0002_SYZ_Sweep_1，取最新 simlog
    root = os.path.splitext(out_siw)[0] + ".siwaveresults"
    newest, nt = "", -1
    if os.path.isdir(root):
        for dp, _dn, fns in os.walk(root):
            for fn in fns:
                if fn.endswith(".simlog"):
                    p = os.path.join(dp, fn)
                    try:
                        t = os.path.getmtime(p)
                    except Exception:
                        continue
                    if t >= nt:
                        newest, nt = p, t
    if not newest:
        return ""
    try:
        lines = open(newest).read().splitlines()
    except Exception:
        return ""
    w0311, errs = [], []
    for ln in lines:
        low = ln.lower()
        seg = ln.split("siwave_ng.exe:", 1)[-1].strip() if "siwave_ng.exe:" in ln else ln.strip()
        if "warning 0311" in low:
            if seg not in w0311:
                w0311.append(seg)
        elif ("error 0501" in low) or ("validation failed" in low) or ("exit code" in low) or ("error 05" in low):
            if seg not in errs:
                errs.append(seg)
    if [e for e in errs if "0518" in e or "memory access violation" in e.lower()]:
        msg = "不是原理图问题。SIwave 裁板后几何预处理崩溃（error 0518 Memory access violation），求解器在 Processing traces 时访问违例。"
        if w0311:
            msg += " 裁切边有自交走线：" + "；".join(w0311[:2])
        msg += " 请打开该段裁剪后的 .siw 检查裁区；可升级/修补 SIwave 2023 R1。"
        return msg
    return " | ".join(errs)[:400]

def _ret_code(r):
    # Scr* 返回值可能是 System.Int16 或元组；统一成 int（解析失败→None 视为失败）
    if isinstance(r, tuple):
        r = r[0]
    try:
        return int(r)
    except Exception:
        return None

def stackup_er0_reason(path):
    try:
        text = open(path).read()
    except Exception:
        return ""
    er0 = []
    for line in text.split("\n"):
        s = line.strip()
        if not s.startswith("INSULATOR ") or "ODB_DIEL" not in s:
            continue
        parts = s.split()
        if len(parts) < 3:
            continue
        try:
            er = float(parts[2])
        except Exception:
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
        token = '"' + name + '"'
        hit = token in fills
        if not hit:
            for line in text.split("\n"):
                s = line.strip()
                if " DIELECTRIC " not in s and " METAL " not in s:
                    continue
                if token in s:
                    hit = True
                    break
        if hit:
            used.append(name)
    if not used:
        return ""
    return "叠层未导入：金属填充介质 " + ",".join(used) + " Er=0（error 0501）。请先导入叠层再抽取"

def extract_one(PARAMS, oDoc):
    MK = open(PARAMS["marker"], "wb")
    def L(s):
        MK.write(utf8_line(s)); MK.flush()
    try:
        nets = PARAMS["nets"]
        out_siw = PARAMS["out_siw"]
        _failed = ""
        L("start " + PARAMS["component"])
        for _p in (out_siw, out_siw + ".lock"):
            try:
                if os.path.isfile(_p):
                    os.remove(_p)
            except Exception:
                pass
        oDoc.ScrSaveProjectAs(out_siw); L("saved0")
        miss = missing_nets(out_siw, nets)
        if miss:
            _failed = "网络名对不上，板里没有: " + ",".join(miss)
            L("FAIL nets " + _failed)
        if not _failed:
            bad = []
            for n in nets:
                r = oDoc.ScrSelectNet(n, 1)
                rc = _ret_code(r)
                # BOOL：1 成功。网名对得上时成功也回 1，不能把非 0 当成没选中。
                if rc != 1:
                    bad.append(n + "(ret=" + str(rc) + ")")
            if bad:
                _failed = "网络未选中: " + ",".join(bad)
                L("FAIL nets " + _failed)
        if not _failed:
            r = oDoc.ScrPlacePortsAtPinsOnSelectedNets(50.0, PARAMS["ref_net"], 0, [])
            L("ports ret=" + repr(r))
            r = oDoc.ScrClipDesignAroundNets(nets, PARAMS["margin"], True, 0, True, False)
            L("clip ret=" + repr(r))
            try:
                oDoc.Save(); L("saved1")
            except Exception:
                oDoc.ScrSaveProjectAs(out_siw); L("saved1-as")
        if not _failed:
            ports = compute_series_ports(out_siw, nets, PARAMS.get("skip_refdes") or [])
            layer = top_layer_name(out_siw)
            for (name, px, py, nx, ny) in ports:
                oDoc.ScrDrawPort(name, float(px), float(py), float(nx), float(ny), layer, layer, 50.0)
            L("series ports=" + str(len(ports)))
            oDoc.ScrClearAllSweeps("SYZ")
            r = oDoc.ScrAppendSweep("SYZ", PARAMS["start_hz"], PARAMS["stop_hz"], PARAMS["points"], 0)
            L("sweep ret=" + repr(r))
            try:
                oDoc.Save(); L("saved2")
            except Exception:
                oDoc.ScrSaveProjectAs(out_siw); L("saved2-as")
            if PARAMS.get("num_cpus", 0) > 0:
                try: oDoc.ScrSetNumCpusToUse(PARAMS["num_cpus"])
                except: L("setcpus skip")
            r = oDoc.ScrRunSyzParameterSimulation(); L("solve ret=" + repr(r))
            rc = _ret_code(r)
            if rc is None or rc != 0:
                _failed = read_solve_error(out_siw) or ("SOLVE ret=" + repr(rc))
                L("FAIL solve " + _failed)
        if not _failed:
            for _old in glob.glob(PARAMS["snp_base"] + ".s*p"):
                try: os.remove(_old)
                except: pass
            e = oDoc.ScrExportToTouchstone(PARAMS["snp_base"]); L("export ret=" + repr(e))
            rc = _ret_code(e)
            if rc is None or rc != 0:
                _failed = "EXPORT ret=" + repr(rc)
                L("FAIL export " + _failed)
        if not _failed:
            L("DONE OK")
    except:
        L("EXC: " + traceback.format_exc())
    MK.close()

extract_one(PARAMS, oDoc)
'''


_SOLVE_SCRIPT_BODY = r'''
import os, traceback

def utf8_line(s):
    try:
        return s.encode("utf-8") + "\n"
    except Exception:
        return str(s) + "\n"

MK = open(PARAMS["marker"], "wb")
def L(s):
    MK.write(utf8_line(s)); MK.flush()

def _ret_code(r):
    if isinstance(r, tuple):
        r = r[0]
    try:
        return int(r)
    except Exception:
        return None

def read_solve_error(out_siw):
    root = os.path.splitext(out_siw)[0] + ".siwaveresults"
    newest, nt = "", -1
    if os.path.isdir(root):
        for dp, _dn, fns in os.walk(root):
            for fn in fns:
                if fn.endswith(".simlog"):
                    p = os.path.join(dp, fn)
                    try:
                        t = os.path.getmtime(p)
                    except Exception:
                        continue
                    if t >= nt:
                        newest, nt = p, t
    if not newest:
        return ""
    try:
        lines = open(newest).read().splitlines()
    except Exception:
        return ""
    w0311, errs = [], []
    for ln in lines:
        low = ln.lower()
        seg = ln.split("siwave_ng.exe:", 1)[-1].strip() if "siwave_ng.exe:" in ln else ln.strip()
        if "warning 0311" in low:
            if seg not in w0311:
                w0311.append(seg)
        elif ("error 0501" in low) or ("validation failed" in low) or ("exit code" in low) or ("error 05" in low):
            if seg not in errs:
                errs.append(seg)
    if [e for e in errs if "0518" in e or "memory access violation" in e.lower()]:
        msg = "不是原理图问题。SIwave 裁板后几何预处理崩溃（error 0518 Memory access violation），求解器在 Processing traces 时访问违例。"
        if w0311:
            msg += " 裁切边有自交走线：" + "；".join(w0311[:2])
        msg += " 请打开该段裁剪后的 .siw 检查裁区；可升级/修补 SIwave 2023 R1。"
        return msg
    return " | ".join(errs)[:400]

try:
    _failed = ""
    L("start " + PARAMS["component"])
    if PARAMS["num_cpus"] > 0:
        try: oDoc.ScrSetNumCpusToUse(PARAMS["num_cpus"])
        except: L("setcpus skip")
    r = oDoc.ScrRunSyzParameterSimulation(); L("solve ret=" + repr(r))
    rc = _ret_code(r)
    if rc is None or rc != 0:
        _failed = read_solve_error(PARAMS["out_siw"]) or ("SOLVE ret=" + repr(rc))
        L("FAIL solve " + _failed)
    if not _failed:
        e = oDoc.ScrExportToTouchstone(PARAMS["snp_base"]); L("export ret=" + repr(e))
        rc = _ret_code(e)
        if rc is None or rc != 0:
            _failed = "EXPORT ret=" + repr(rc)
            L("FAIL export " + _failed)
    if not _failed:
        L("DONE OK")
except:
    L("EXC: " + traceback.format_exc())
MK.close()
'''


def _build_script(params: dict) -> str:
    return "# -*- coding: utf-8 -*-\nPARAMS = " + repr(params) + "\n" + _SCRIPT_BODY


def _build_solve_script(params: dict) -> str:
    return "# -*- coding: utf-8 -*-\nPARAMS = " + repr(params) + "\n" + _SOLVE_SCRIPT_BODY


_WORKER_LOOP = r'''
def restore_source(src):
    global oDoc
    try:
        oDoc.ScrCloseProjectNoSave()
    except:
        pass
    try:
        app = oApp
    except NameError:
        import win32com.client
        app = win32com.client.Dispatch("SIwave.Application")
    app.OpenProject(src)
    oDoc = app.GetActiveProject()
    if oDoc is None:
        try:
            oDoc = app.GetActiveDocument()
        except:
            oDoc = None
    if oDoc is None:
        raise RuntimeError("OpenProject returned no document: " + src)

_stk = stackup_er0_reason(PARAMS["src_siw"])
if _stk:
    for job in PARAMS["jobs"]:
        mk = open(job["marker"], "wb")
        mk.write(utf8_line("FAIL stackup " + _stk))
        mk.close()
    raise SystemExit(2)

need_restore = False
for job in PARAMS["jobs"]:
    if "num_cpus" not in job:
        job["num_cpus"] = PARAMS.get("num_cpus", 0)
    if need_restore:
        restore_source(PARAMS["src_siw"])
    need_restore = True
    extract_one(job, oDoc)
'''


def _build_worker_script(params: dict) -> str:
    library = _SCRIPT_BODY.replace("\nextract_one(PARAMS, oDoc)\n", "\n", 1)
    if any(ln.strip() == "extract_one(PARAMS, oDoc)" for ln in library.splitlines()):
        raise RuntimeError("worker 脚本仍含一次性 extract_one 调用")
    return "# -*- coding: utf-8 -*-\nPARAMS = " + repr(params) + "\n" + library + _WORKER_LOOP


def kill_all_siwave() -> None:
    subprocess.run(["taskkill", "/F", "/IM", "SIwave.exe"],
                   capture_output=True, check=False)


def _parse_freq(s: str) -> float:
    s = s.strip(); su = s.upper()
    for suf, mult in (("GHZ", 1e9), ("MHZ", 1e6), ("KHZ", 1e3), ("HZ", 1.0)):
        if su.endswith(suf):
            return float(s[:-len(suf)]) * mult
    return float(s)


class _Job:
    __slots__ = ("component", "marker", "snp_base", "script", "proc", "start",
                 "attempts", "src_siw", "nets", "start_hz", "stop_hz", "out_siw")

    def __init__(self, component, marker, snp_base, script):
        self.component = component
        self.marker = marker
        self.snp_base = snp_base
        self.script = script
        self.proc = None
        self.start = 0.0
        self.attempts = 0          # 已启动次数（含重试），用于并行失败重试上限
        self.src_siw = ""
        self.nets: list[str] = []
        self.start_hz = 0.0
        self.stop_hz = 0.0
        self.out_siw = ""


def _read_marker_text(path: str) -> str:
    """IronPython 旧 marker 可能是 GBK，新的是 UTF-8。禁止 latin-1，否则中文变乱码。"""
    try:
        raw = open(path, "rb").read()
    except OSError:
        return ""
    if raw.startswith(b"\xef\xbb\xbf"):
        raw = raw[3:]
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        return raw.decode("gb18030", errors="replace")


def _marker_status(path: str) -> str:
    """读 marker 末状态：'DONE'/'FAIL_*'/'EXC'/'RUN'（未完成）/'NONE'（无文件）。"""
    if not os.path.exists(path):
        return "NONE"
    txt = _read_marker_text(path)
    if not txt:
        return "RUN"
    if "DONE OK" in txt:
        return "DONE"
    if "FAIL stackup" in txt:
        return "FAIL_STACKUP"
    if "FAIL nets" in txt:
        return "FAIL_NETS"
    if "FAIL solve" in txt:
        return "FAIL_SOLVE"
    if "FAIL export" in txt:
        return "FAIL_EXPORT"
    if "FAIL " in txt:
        return "FAIL"
    if "EXC:" in txt:
        return "EXC"
    return "RUN"


def _marker_fail_reason(path: str) -> str:
    """从 marker 提取最后一条 FAIL/EXC 行原文，供错误提示展示。"""
    txt = _read_marker_text(path)
    if not txt:
        return "未知失败"
    for key in ("FAIL stackup", "FAIL nets", "FAIL solve", "FAIL export", "FAIL ", "EXC:"):
        idx = txt.rfind(key)
        if idx >= 0:
            return txt[idx:].split("\n", 1)[0].strip()
    return "未知失败"


def _kill(proc) -> None:
    if proc is None or proc.poll() is not None:
        return
    subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                   capture_output=True, check=False)


class _Slot:
    __slots__ = ("index", "src_siw", "script", "proc", "jobs")

    def __init__(self, index: int, src_siw: str, script: str, jobs: list[_Job]):
        self.index = index
        self.src_siw = src_siw
        self.script = script
        self.proc = None
        self.jobs = jobs


def run_parallel(config: Config, project_path: str,
                 grouped: Mapping[str, list[NetInfo]],
                 out_dir: str | None = None) -> list[ComponentResult]:
    """若干独立 SIwave：每路一份整板副本，一段结束即关进程并从队列接下一段。"""
    siwave = _find_siwave_exe()
    requested = max(1, config.parallel_workers)
    ram_gb = physical_ram_gb()
    workers = bound_parallel_workers(requested, ram_gb)
    total_cores = os.cpu_count() or 1
    if config.parallel_cpus_per_worker > 0:
        per_cpus = config.parallel_cpus_per_worker
    else:
        per_cpus = max(1, total_cores // workers)

    sp = build_subproject(project_path, prefix=config.subproject_prefix, create=True,
                          out_dir=out_dir)
    print(f"产物目录: {sp.subproject_dir}", flush=True)
    work_dir = os.path.join(sp.subproject_dir, "_parallel")
    os.makedirs(work_dir, exist_ok=True)

    pending: list[_Job] = []
    for component, nets in grouped.items():
        net_names = [n.name for n in nets]
        safe = safe_component_name(component)
        out_siw = subproject_file(sp, safe)
        snp_base = os.path.splitext(out_siw)[0]
        marker = os.path.join(work_dir, safe + ".marker")
        if os.path.exists(marker):
            os.remove(marker)
        job = _Job(safe, marker, snp_base, "")
        job.nets = net_names
        job.start_hz = clamp_rf_sweep_start_hz(_parse_freq(nets[0].start_freq))
        job.stop_hz = _parse_freq(nets[0].end_freq)
        job.out_siw = out_siw
        pending.append(job)

    total = len(pending)
    if workers < requested:
        print(f"本机内存约 {ram_gb:.0f}GB，并行从 {requested} 路降为 {workers} 路，避免多份整板同时换页")
    print(f"独立抽取：{total} 段，{workers} 路 SIwave（谁先做完谁接下一段），每路 {per_cpus} 核")

    def _job_payload(job: _Job) -> dict:
        return {
            "component": job.component,
            "nets": job.nets,
            "start_hz": job.start_hz,
            "stop_hz": job.stop_hz,
            "points": rf_sweep_point_count(
                job.start_hz, job.stop_hz, getattr(config, "sweep_step_mhz", 1) or 1
            ),
            "ref_net": config.ref_net,
            "margin": config.clip_margin,
            "out_siw": job.out_siw,
            "snp_base": job.snp_base,
            "marker": job.marker,
            "num_cpus": per_cpus,
            "skip_refdes": list(getattr(config, "skip_series_nc", ()) or ()),
        }

    slots: list[_Slot] = []
    for i in range(workers):
        slot_dir = os.path.join(work_dir, f"slot{i}")
        os.makedirs(slot_dir, exist_ok=True)
        src_siw = os.path.join(slot_dir, "board.src.siw")
        cleanup_source_copy(src_siw)
        copy_project_for_job(project_path, src_siw)
        script = os.path.join(slot_dir, "worker.py")
        slots.append(_Slot(i, src_siw, script, []))

    results: list[ComponentResult] = []
    finished: set[str] = set()
    done = 0
    timeout = config.com_timeout if config.com_timeout > 0 else 1800
    max_launches = max(1, config.retry_attempts)

    def _write_script(slot: _Slot) -> None:
        with open(slot.script, "w", encoding="utf-8") as f:
            f.write(_build_worker_script({
                "src_siw": slot.src_siw,
                "num_cpus": per_cpus,
                "jobs": [_job_payload(job) for job in slot.jobs],
            }))

    def _arm_current(slot: _Slot) -> _Job | None:
        current = _current_job(slot)
        if current is not None and current.start <= 0:
            current.start = time.time()
        return current

    def _start_slot(slot: _Slot) -> None:
        _write_script(slot)
        for job in slot.jobs:
            job.attempts += 1
            job.start = 0.0
        slot.proc = subprocess.Popen(
            [siwave, slot.src_siw, "-RunScript", slot.script],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        _arm_current(slot)
        names = ", ".join(j.component for j in slot.jobs)
        print(f"  ▶ slot{slot.index} 开始 {names}")

    def _finish(job: _Job, ok: bool, err: str | None) -> None:
        nonlocal done
        if job.component in finished:
            return
        finished.add(job.component)
        if ok:
            for snp in _glob.glob(job.snp_base + ".s*p"):
                try:
                    _reformat_snp(snp)
                except Exception as exc:  # noqa: BLE001
                    _log.warning("reformat %s 失败: %s", snp, exc)
        done += 1
        status = "✓" if ok else "✗"
        suffix = "" if ok else f"  -- {err}"
        elapsed = time.time() - job.start if job.start > 0 else 0.0
        print(f"  [{_bar(done, total)}] {done*100//total:3d}%  {status} {job.component}"
              f" ({elapsed:.0f}s){suffix}")
        results.append(ComponentResult(job.component, ok, err))

    def _clear_marker(job: _Job) -> None:
        if os.path.exists(job.marker):
            try:
                os.remove(job.marker)
            except OSError:
                pass

    def _unfinished(slot: _Slot) -> list[_Job]:
        return [job for job in slot.jobs if job.component not in finished]

    def _current_job(slot: _Slot) -> _Job | None:
        for job in slot.jobs:
            if job.component in finished:
                continue
            return job
        return None

    def _apply_marker(job: _Job) -> bool:
        st = _marker_status(job.marker)
        if st == "DONE":
            snps = _glob.glob(job.snp_base + ".s*p")
            if snps:
                _finish(job, True, None)
            else:
                _finish(job, False, "脚本完成但未找到导出 .sNp")
            return True
        if st == "FAIL_STACKUP":
            _finish(job, False, _marker_fail_reason(job.marker))
            return True
        if st == "FAIL_NETS":
            _finish(job, False, _marker_fail_reason(job.marker))
            return True
        if st == "FAIL_SOLVE":
            _finish(job, False, "SYZ 求解失败（" + _marker_fail_reason(job.marker) + "）")
            return True
        if st == "FAIL_EXPORT":
            _finish(job, False, "导出 SNP 失败（" + _marker_fail_reason(job.marker) + "）")
            return True
        if st in ("FAIL", "EXC"):
            _finish(job, False, _marker_fail_reason(job.marker))
            return True
        return False

    queue: list[_Job] = list(pending)

    def _give_job(slot: _Slot) -> None:
        if slot.proc is not None and slot.proc.poll() is None:
            return
        if not queue:
            slot.jobs = []
            return
        job = queue.pop(0)
        slot.jobs = [job]
        _start_slot(slot)

    def _slot_take_next(slot: _Slot) -> None:
        _kill(slot.proc)
        slot.proc = None
        slot.jobs = []
        _give_job(slot)

    def _fail_or_relaunch(slot: _Slot, err: str) -> None:
        current = _current_job(slot)
        if current is not None:
            _apply_marker(current)
        if current is not None and current.component in finished:
            _slot_take_next(slot)
            return
        if (current is not None and slot.proc is not None
                and slot.proc.poll() is None
                and current.start > 0
                and time.time() - current.start <= timeout):
            return
        _kill(slot.proc)
        slot.proc = None
        if current is None:
            _give_job(slot)
            return
        if current.attempts < max_launches:
            current.start = 0.0
            _clear_marker(current)
            slot.jobs = [current]
            print(f"  [重试] slot{slot.index} {current.component} {err}")
            _start_slot(slot)
        else:
            _finish(current, False, err)
            slot.jobs = []
            _give_job(slot)

    try:
        for i, slot in enumerate(slots):
            _give_job(slot)
            if i + 1 < len(slots):
                time.sleep(2.0)

        while len(finished) < total:
            for slot in slots:
                current = _current_job(slot)
                if current is None:
                    if queue:
                        _give_job(slot)
                    continue
                if _apply_marker(current):
                    _slot_take_next(slot)
                    continue
                if slot.proc is not None and slot.proc.poll() is not None:
                    _fail_or_relaunch(
                        slot,
                        f"siwave.exe 提前退出（marker={_marker_status(current.marker)}）",
                    )
                elif current.start > 0 and time.time() - current.start > timeout:
                    _fail_or_relaunch(slot, f"超时 {timeout}s")
            if len(finished) >= total:
                break
            time.sleep(0.5)
    finally:
        for slot in slots:
            _kill(slot.proc)
            cleanup_source_copy(slot.src_siw)

    order = {job.component: i for i, job in enumerate(pending)}
    results.sort(key=lambda r: order.get(r.component, 0))
    return results


def run_solve_only(config: Config, prepared: Sequence[tuple[str, str]],
                   project_path: str, out_dir: str | None = None) -> list[ComponentResult]:
    """对已裁好的子工程并行求解。只打开约 1.5MB 的 .siw，不再载入整板。"""
    if not prepared:
        return []
    siwave = _find_siwave_exe()
    requested = max(1, config.parallel_workers)
    workers = min(4, requested)
    total_cores = os.cpu_count() or 1
    if config.parallel_cpus_per_worker > 0:
        per_cpus = config.parallel_cpus_per_worker
    else:
        per_cpus = max(1, total_cores // workers)

    sp = build_subproject(project_path, prefix=config.subproject_prefix, create=True,
                          out_dir=out_dir)
    work_dir = os.path.join(sp.subproject_dir, "_parallel")
    os.makedirs(work_dir, exist_ok=True)

    pending: list[_Job] = []
    for component, out_siw in prepared:
        safe = safe_component_name(component)
        snp_base = os.path.splitext(out_siw)[0]
        marker = os.path.join(work_dir, safe + ".marker")
        script = os.path.join(work_dir, safe + ".solve.py")
        params = {
            "component": safe,
            "out_siw": out_siw,
            "snp_base": snp_base,
            "num_cpus": per_cpus,
            "marker": marker,
        }
        with open(script, "w", encoding="utf-8") as f:
            f.write(_build_solve_script(params))
        if os.path.exists(marker):
            os.remove(marker)
        job = _Job(safe, marker, snp_base, script)
        job.src_siw = out_siw
        pending.append(job)

    total = len(pending)
    print(f"求解：{total} 段，{workers} 路并行（只开已裁子工程），每路 {per_cpus} 核")
    results: list[ComponentResult] = []
    running: list[_Job] = []
    queue = list(pending)
    done = 0
    timeout = config.com_timeout if config.com_timeout > 0 else 1800
    max_launches = max(1, config.retry_attempts)

    def _launch(job: _Job) -> None:
        job.attempts += 1
        job.start = time.time()
        job.proc = subprocess.Popen(
            [siwave, job.src_siw, "-RunScript", job.script],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        running.append(job)
        again = f"（第 {job.attempts} 次）" if job.attempts > 1 else ""
        print(f"  ▶ 求解 {job.component}{again}")

    def _finish(job: _Job, ok: bool, err: str | None) -> None:
        nonlocal done
        _kill(job.proc)
        if ok:
            for snp in _glob.glob(job.snp_base + ".s*p"):
                try:
                    _reformat_snp(snp)
                except Exception as exc:  # noqa: BLE001
                    _log.warning("reformat %s 失败: %s", snp, exc)
        done += 1
        status = "✓" if ok else "✗"
        suffix = "" if ok else f"  -- {err}"
        elapsed = time.time() - job.start if job.start > 0 else 0.0
        print(f"  [{_bar(done, total)}] {done*100//total:3d}%  {status} {job.component}"
              f" ({elapsed:.0f}s){suffix}")
        results.append(ComponentResult(job.component, ok, err))

    def _retry_or_finish(job: _Job, err: str) -> None:
        if job.attempts < max_launches:
            _kill(job.proc)
            if os.path.exists(job.marker):
                try:
                    os.remove(job.marker)
                except OSError:
                    pass
            job.proc = None
            queue.append(job)
            print(f"  [重试] {job.component} 第 {job.attempts} 次失败（{err}），重排再跑")
        else:
            _finish(job, False, err)

    while queue and len(running) < workers:
        _launch(queue.pop(0))

    while running:
        time.sleep(1.0)
        for job in running[:]:
            st = _marker_status(job.marker)
            elapsed = time.time() - job.start
            if st == "DONE":
                snps = _glob.glob(job.snp_base + ".s*p")
                if snps:
                    running.remove(job); _finish(job, True, None)
                else:
                    running.remove(job); _finish(job, False, "脚本完成但未找到导出 .sNp")
            elif st == "FAIL_NETS":
                running.remove(job)
                _finish(job, False, _marker_fail_reason(job.marker))
            elif st == "FAIL_SOLVE":
                running.remove(job)
                _finish(job, False, "SYZ 求解失败（" + _marker_fail_reason(job.marker) + "）")
            elif st == "FAIL_EXPORT":
                running.remove(job)
                _finish(job, False, "导出 SNP 失败（" + _marker_fail_reason(job.marker) + "）")
            elif st in ("FAIL", "EXC"):
                running.remove(job); _finish(job, False, _marker_fail_reason(job.marker))
            elif job.proc.poll() is not None and st != "DONE":
                running.remove(job); _retry_or_finish(job, f"siwave.exe 提前退出（marker={st}）")
            elif elapsed > timeout:
                running.remove(job); _retry_or_finish(job, f"超时 {timeout}s")
        while queue and len(running) < workers:
            _launch(queue.pop(0))

    return results
