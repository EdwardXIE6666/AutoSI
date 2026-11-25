"""Orchestrate SYZ extraction per component against a SiwaveSession."""
from __future__ import annotations

import logging
import os
import threading
import time
from dataclasses import dataclass
from typing import Mapping, Sequence

from .config import Config
from .netlist import NetInfo
from rf.segments import clamp_rf_sweep_start_hz, rf_sweep_point_count, safe_component_name
from .siwave_session import _parse_freq

from .project_paths import build_subproject, subproject_file
from .retry import call_with_retries
from .siwave_session import SiwaveSession

_log = logging.getLogger(__name__)

# COM error codes that mean SIwave.exe has crashed or become unreachable
_COM_CRASH_CODES = frozenset({
    -2147023174,  # 0x800706BA  RPC server unavailable
    -2147023170,  # 0x8007077E  RPC call failed
    -2147023099,  # 0x80010105  RPC server fault
    -2147023096,  # 0x80010108  RPC disconnected
})


def _is_com_crash(exc: Exception) -> bool:
    try:
        return exc.args[0] in _COM_CRASH_CODES
    except (IndexError, TypeError):
        return False


def _siwave_dead(exc: Exception) -> bool:
    """COM 掉线，或 siwave_ng 0518 把求解进程打崩后 GUI 会话也半死。"""
    if _is_com_crash(exc):
        return True
    msg = str(exc).lower()
    return "0518" in msg or "memory access violation" in msg


@dataclass
class ComponentResult:
    component: str
    ok: bool
    error: str | None


def _timed(label: str, func):
    """Run func() while printing elapsed seconds in-place. Returns func's result."""
    done = threading.Event()
    start = time.time()
    print(f"  {label}", end="", flush=True)

    def _tick():
        while not done.wait(1.0):
            print(f"\r  {label} {int(time.time() - start)}s", end="", flush=True)

    t = threading.Thread(target=_tick, daemon=True)
    t.start()
    try:
        result = func()
    finally:
        done.set()
        t.join(timeout=2)
        print(f"\r  {label} done ({time.time() - start:.0f}s)          ")
    return result


def _bar(current: int, total: int, width: int = 20) -> str:
    filled = current * width // total if total else 0
    return "█" * filled + "░" * (width - filled)


def clip_pass_io(index: int, total: int) -> tuple[bool, bool]:
    """本段是否打开整板、裁完是否把同一 SIwave 载回整板。index 从 1 计。"""
    if total <= 0:
        return False, False
    return index == 1, index < total


def prepare_component(session: SiwaveSession, config: Config, project_path: str,
                      component: str, net_names: Sequence[str],
                      start_freq: str, end_freq: str,
                      out_dir: str | None = None, *,
                      open_source: bool = True) -> str:
    """端口 + 裁板 + 另存子工程。不求解、不关 SIwave 进程。"""
    sp = build_subproject(project_path, prefix=config.subproject_prefix, create=True,
                          out_dir=out_dir)
    out_path = subproject_file(sp, component)

    if open_source:
        from .siwave_session import odb_er0_fill_reason
        why = odb_er0_fill_reason(project_path)
        if why:
            raise RuntimeError(why)
        _log.info("Component %s: opening project", component)
        _timed("opening project...", lambda: session.open_project(project_path))

    # 先另存成子工程再端口/裁板：裁的是副本，不脏源板，裁完 Save 也不弹确认。
    _log.info("Component %s: saving working copy to %s", component, out_path)
    print(f"  saving → {os.path.basename(out_path)}...", end="", flush=True)
    session.save_as(out_path)
    print(" done")

    _log.info("Component %s: generating ports", component)
    print(f"  generating ports ({len(net_names)} net(s))...", end="", flush=True)
    session.generate_ports(net_names)
    print(" done")

    print("  clipping design...", end="", flush=True)
    session.clip_around_nets(list(net_names), config.clip_margin)
    print(" done")

    print("  saving clipped project...", end="", flush=True)
    session.save()
    print(" done")

    # 串联端口与 SYZ 扫频都用 COM 直接落到打开的工程上，省掉关闭→文本补丁→重开：
    #   - 串联端口：ScrDrawPort（坐标由已存盘的 .siw 探测得到）
    #   - SYZ 扫频：ScrClearAllSweeps("SYZ")+ScrAppendSweep("SYZ", …)
    print(f"  drawing series ports (COM)...", end="", flush=True)
    session.draw_series_ports(
        out_path, list(net_names),
        skip_refdes=getattr(config, "skip_series_nc", ()) or (),
    )
    print(" done")

    start_hz = clamp_rf_sweep_start_hz(_parse_freq(start_freq))
    stop_hz = _parse_freq(end_freq)
    npts = rf_sweep_point_count(
        start_hz, stop_hz, getattr(config, "sweep_step_mhz", 1) or 1
    )
    print(
        f"  configuring sweep ({start_freq}–{end_freq}, "
        f"{config.sweep_step_mhz:g} MHz step, {npts} pts, COM)...",
        end="",
        flush=True,
    )
    session.set_syz_sweep(start_freq, end_freq, npts)
    print(" done")

    print(f"  saving patched project...", end="", flush=True)
    session.save()
    print(" done")
    return out_path


def solve_prepared(session: SiwaveSession, config: Config, component: str,
                   out_path: str) -> None:
    """在已经裁好的当前工程上求解并导出。不新开 SIwave。"""
    n_cpus = config.num_cpus if config.num_cpus > 0 else (os.cpu_count() or 1)
    print(f"  using {n_cpus} CPU core(s) for solve...", end="", flush=True)
    session.set_num_cpus(n_cpus)
    print(" done")

    sim_net = safe_component_name(component)
    _log.info("Component %s: running SYZ simulation", component)
    _timed("running simulation...", lambda: session.run_simulation(sim_net))

    snp_base = os.path.splitext(out_path)[0]
    _log.info("Component %s: exporting SNP to %s.s*p", component, snp_base)
    print(f"  exporting SNP...", end="", flush=True)
    session.export_snp(snp_base)
    print(" done")


def extract_component(session: SiwaveSession, config: Config, project_path: str,
                      component: str, net_names: Sequence[str],
                      start_freq: str, end_freq: str,
                      out_dir: str | None = None) -> None:
    """Run the full extraction for one component group."""
    out_path = prepare_component(
        session, config, project_path, component, net_names,
        start_freq, end_freq, out_dir=out_dir, open_source=True)
    solve_prepared(session, config, component, out_path)
    print("  closing project...", end="", flush=True)
    session.close_project()
    print(" done")


def run(session: SiwaveSession, config: Config, project_path: str,
        grouped: Mapping[str, list[NetInfo]],
        out_dir: str | None = None) -> list[ComponentResult]:
    """Process every component; honor retries and continue-on-error."""
    sp = build_subproject(project_path, prefix=config.subproject_prefix, create=True,
                          out_dir=out_dir)
    print(f"产物目录: {sp.subproject_dir}", flush=True)
    results: list[ComponentResult] = []
    total = len(grouped)
    for idx, (component, nets) in enumerate(grouped.items(), start=1):
        pct = (idx - 1) * 100 // total if total else 0
        print(f"\n[{_bar(idx - 1, total)}] {pct:3d}%  Component {idx}/{total}: {component}")

        net_names = [n.name for n in nets]
        start, end = nets[0].start_freq, nets[0].end_freq

        def _do() -> None:
            extract_component(session, config, project_path, component,
                              net_names, start, end, out_dir=out_dir)

        def _restart_if_dead(exc: Exception) -> None:
            if _siwave_dead(exc) and hasattr(session, "restart"):
                _log.warning("SIwave died during %s; restarting before retry", component)
                print("  [SIwave crashed — restarting before retry...]")
                session.restart()

        try:
            call_with_retries(_do, attempts=config.retry_attempts,
                              on_retry=_restart_if_dead)
            pct_done = idx * 100 // total if total else 100
            print(f"  [{_bar(idx, total)}] {pct_done:3d}%  ✓ {component}")
            results.append(ComponentResult(component, True, None))
        except Exception as exc:  # noqa: BLE001
            _log.error("Component %s failed: %s", component, exc)
            print(f"  ✗ {component}: {exc}")
            results.append(ComponentResult(component, False, str(exc)))
            if _siwave_dead(exc) and hasattr(session, 'restart'):
                _log.warning("SIwave process died during %s; restarting session", component)
                print("  [SIwave crashed — restarting for next component...]")
                try:
                    session.restart()
                except Exception as re_exc:
                    _log.error("Session restart failed: %s", re_exc)
            if not config.continue_on_error:
                raise
    return results


def run_clip_then_solve(session: SiwaveSession, config: Config, project_path: str,
                        grouped: Mapping[str, list[NetInfo]],
                        out_dir: str | None = None) -> list[ComponentResult]:
    """一个 SIwave 常驻：裁完立刻在本进程求解，再换回整板。不新开求解进程。"""
    results: list[ComponentResult] = []
    total = len(grouped)
    need_open = True
    print(f"抽取：1 个 SIwave 常驻，共 {total} 段（裁完即求解，段间只换回整板）")
    for idx, (component, nets) in enumerate(grouped.items(), start=1):
        _open_source, restore = clip_pass_io(idx, total)
        open_source = need_open or _open_source
        print(f"\n[{_bar(idx - 1, total)}] {(idx - 1) * 100 // total if total else 0:3d}%  "
              f"Component {idx}/{total}: {component}")
        net_names = [n.name for n in nets]
        start, end = nets[0].start_freq, nets[0].end_freq

        first_try = True

        def _do() -> str:
            nonlocal first_try
            use_open = open_source if first_try else True
            first_try = False
            out_path = prepare_component(
                session, config, project_path, component, net_names,
                start, end, out_dir=out_dir, open_source=use_open)
            solve_prepared(session, config, component, out_path)
            return out_path

        def _restart_if_dead(exc: Exception) -> None:
            nonlocal first_try
            if _siwave_dead(exc) and hasattr(session, "restart"):
                _log.warning("SIwave died during %s; restarting before retry", component)
                print("  [SIwave crashed — restarting before retry...]")
                session.restart()
                first_try = False

        try:
            call_with_retries(_do, attempts=config.retry_attempts,
                              on_retry=_restart_if_dead)
            need_open = False
            print(f"  [{_bar(idx, total)}] {idx * 100 // total if total else 100:3d}%  ✓ {component}")
            results.append(ComponentResult(component, True, None))
            if restore:
                print("  同一进程载回整板...", end="", flush=True)
                session.close_project()
                session.open_project(project_path)
                print(" done")
        except Exception as exc:  # noqa: BLE001
            _log.error("Component %s failed: %s", component, exc)
            print(f"  ✗ {component}: {exc}")
            results.append(ComponentResult(component, False, str(exc)))
            need_open = True
            if _siwave_dead(exc) and hasattr(session, "restart"):
                print("  [SIwave crashed — restarting for next component...]")
                try:
                    session.restart()
                except Exception as re_exc:
                    _log.error("Session restart failed: %s", re_exc)
            if not config.continue_on_error:
                raise
    try:
        session.close_project()
    except Exception:
        pass
    return results


def format_summary(results: Sequence[ComponentResult]) -> str:
    ok = sum(1 for r in results if r.ok)
    failed = len(results) - ok
    lines = [f"Extraction summary: {ok} ok, {failed} failed"]
    for r in results:
        status = "OK " if r.ok else "ERR"
        suffix = "" if r.ok else f" -- {r.error}"
        lines.append(f"  [{status}] {r.component}{suffix}")
    return "\n".join(lines)
