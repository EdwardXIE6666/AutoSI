"""ADS 天线口匹配：钉 T_A 端口、挂 Optim/Goal（不改默认三件套落图）。"""

from __future__ import annotations

import re

_TA = re.compile(r"^T_A(\d+)$")
_TC = re.compile(r"^T_C(\d+)$")


def _find(design, name):
    find_instance = getattr(design, "find_instance", None)
    if find_instance is not None:
        try:
            return find_instance(name)
        except Exception:
            return None
    instances = getattr(design, "instances", None)
    if instances is None:
        return None
    for inst in instances:
        if getattr(inst, "name", None) == name:
            return inst
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
    find_instance = getattr(design, "find_instance", None)
    if find_instance is None:
        return names
    return names


def _set_param(inst, key: str, value: str) -> None:
    if inst is None:
        return
    params = getattr(inst, "parameters", None)
    if params is None:
        return
    try:
        param = params[key]
        repeats = getattr(param, "repeats", None)
        if repeats is not None and len(repeats) >= 1:
            repeats[0].value = value
            return
        param.value = value
    except Exception:
        return


def _try_add(design, lib: str, cell: str, view: str, name: str, position):
    add = getattr(design, "add_instance", None)
    if not callable(add):
        return None
    try:
        from keysight.ads.de import db_uu

        return add(db_uu.LCVName(lib, cell, view), position, name=name)
    except Exception:
        try:
            return add((lib, cell, view), position, name=name)
        except Exception:
            return None


def pin_ta_ports(design) -> dict[str, int]:
    """给每个 T_A 独立端口号，并把同序号 VSWR 钉到该口的 Snn。

    不依赖「先放的 Term 就是端口 1」。T_C 只当 50Ω 负载，跟在对应 T_A 后面编号。
    """
    names = set(_instance_names(design))
    if not names:
        # find_instance-only fakes: probe T_A0..T_A31
        for i in range(32):
            inst = _find(design, f"T_A{i}")
            if inst is not None:
                names.add(f"T_A{i}")
            inst = _find(design, f"T_C{i}")
            if inst is not None:
                names.add(f"T_C{i}")
            inst = _find(design, f"VSWR{i}")
            if inst is not None:
                names.add(f"VSWR{i}")

    tas = sorted(
        (n for n in names if _TA.fullmatch(n)),
        key=lambda n: int(_TA.fullmatch(n).group(1)),
    )
    tcs = {
        int(_TC.fullmatch(n).group(1)): n
        for n in names
        if _TC.fullmatch(n)
    }
    pinned: dict[str, int] = {}
    num = 1
    for ta in tas:
        idx = int(_TA.fullmatch(ta).group(1))
        _set_param(_find(design, ta), "Num", str(num))
        pinned[ta] = num
        vswr_name = f"VSWR{idx}"
        if vswr_name in names or _find(design, vswr_name) is not None:
            _set_param(
                _find(design, vswr_name),
                "Function",
                f"{vswr_name}=vswr(S{num}{num})",
            )
        num += 1
        tc = tcs.get(idx)
        if tc:
            _set_param(_find(design, tc), "Num", str(num))
            pinned[tc] = num
            num += 1
    return pinned


def pin_lookinto_ports(design) -> dict[str, int]:
    """分端图：芯片侧(左) term1 Num=1，天线侧(右) term2 Num=2。

    VSWR 看 PA/LNA 侧，用 S11。TRX 的 RX 控件是 term3，公式用 S33。
    合图仍走 T_A*（pin_ta_ports）。子段接口 T_* 用 1,3,4…，不抢 term2 的 2。
    """
    pinned = pin_ta_ports(design)
    if pinned:
        return pinned
    names = set(_instance_names(design))
    if not names:
        for name in ("term1", "term2"):
            if _find(design, name) is not None:
                names.add(name)
        for i in range(32):
            if _find(design, f"VSWR{i}") is not None:
                names.add(f"VSWR{i}")
            if _find(design, f"VSWR_{i}") is not None:
                names.add(f"VSWR_{i}")
    term2 = _find(design, "term2")
    if term2 is None:
        return pinned
    _set_param(term2, "Num", "2")
    pinned["term2"] = 2
    extras = [
        n for n in names
        if n not in pinned and (n == "term1" or str(n).startswith("T_"))
    ]
    extras.sort(key=lambda n: (0 if n == "term1" else 1, n))
    num = 1
    for name in extras:
        if num == 2:
            num = 3
        _set_param(_find(design, name), "Num", str(num))
        pinned[name] = num
        num += 1
    for name in names:
        if not str(name).startswith("VSWR"):
            continue
        sport = "S33" if str(name).startswith("VSWR_RX_") else "S11"
        _set_param(_find(design, name), "Function", f"{name}=vswr({sport})")
    return pinned


def _deactivate(inst) -> None:
    fn = getattr(inst, "deactivate", None)
    if callable(fn):
        try:
            fn()
        except Exception:
            return


def attach_optim(design, bands: list[tuple[str, str]]) -> None:
    """每个 cell 一个 Optim1；每条频段一条 Goal。AEL 按段只激活一条 Goal 再跑。

    Goal.Max=1（尽量靠近 50Ω），验收戳 1.3 不写进 Optim。
    """
    if not callable(getattr(design, "add_instance", None)):
        return
    names = set(_instance_names(design))
    if "Optim1" not in names:
        optim = _try_add(
            design, "ads_simulation", "Optim", "symbol", "Optim1", (0.0, 12.0)
        )
        if optim is not None:
            try:
                optim.parameters["OptimType"].form_name = "Random"
            except Exception:
                pass
            _set_param(optim, "MaxIters", "40")
            _set_param(optim, "DesiredError", "0.0")
            try:
                optim.parameters["EnableCockpit"].form_name = "Yes"
            except Exception:
                _set_param(optim, "EnableCockpit", "yes")
            try:
                optim.parameters["UseAllGoals"].form_name = "Yes"
            except Exception:
                _set_param(optim, "UseAllGoals", "yes")
            _set_param(optim, "GoalName", "")
    for i, (sp_name, vswr_name) in enumerate(bands):
        goal_name = f"Goal{i}"
        if goal_name in names:
            inst = _find(design, goal_name)
        else:
            inst = _try_add(
                design,
                "ads_simulation",
                "Goal",
                "symbol",
                goal_name,
                (4.0 + 3.0 * i, 12.0),
            )
        if inst is None:
            continue
        _set_param(inst, "Expr", vswr_name)
        _set_param(inst, "SimInstanceName", sp_name)
        _set_param(inst, "Max", "1")
        sp = _find(design, sp_name)
        if sp is not None:
            _set_param(sp, "EnableOptim", "yes")
            try:
                sp.parameters["EnableOptim"].form_name = "Yes"
            except Exception:
                pass
            _deactivate(sp)
        _deactivate(inst)
    _deactivate(_find(design, "Optim1"))


def attach_match_vars(design, vars_: list[tuple[str, str, str, str]]) -> None:
    """Official VAR: named i_*/v_* equations. C/L only reference v_*."""
    if not vars_:
        return
    from ads.match_ael import idx_var, library_sum_expr, opt_expr, val_var

    inst = _find(design, "MatchVar")
    add = getattr(design, "add_var_instance", None)
    if inst is None and callable(add):
        try:
            inst = add((0.0, 16.0), name="MatchVar")
        except TypeError:
            inst = add((0.0, 16.0))
    if inst is None:
        return
    table = getattr(inst, "vars", None)
    if table is None:
        return
    for inst_name, param, lo, hi in vars_:
        table[idx_var(inst_name)] = opt_expr(param, lo, hi)
        table[val_var(inst_name)] = library_sum_expr(param, idx_var(inst_name))
    try:
        del table["X"]
    except Exception:
        pass
