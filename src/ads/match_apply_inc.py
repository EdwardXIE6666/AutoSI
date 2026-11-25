"""Apply match index/value equations onto one official VAR instance."""

from __future__ import annotations

import sys
import traceback
from pathlib import Path


def parse_inc(text: str) -> list[tuple[str, str]]:
    pairs: list[tuple[str, str]] = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line or "=" not in line:
            continue
        name, eqn = line.split("=", 1)
        pairs.append((name.strip(), eqn.strip()))
    return pairs


def apply_inc(design, pairs: list[tuple[str, str]]) -> int:
    inst = None
    find = getattr(design, "find_instance", None)
    if callable(find):
        try:
            inst = find("MatchVar")
        except Exception:
            inst = None
    if inst is None:
        instances = getattr(design, "instances", None) or []
        for item in instances:
            if getattr(item, "name", None) == "MatchVar":
                inst = item
                break
    add = getattr(design, "add_var_instance", None)
    if inst is None and callable(add):
        try:
            inst = add((0.0, 16.0), name="MatchVar")
        except TypeError:
            inst = add((0.0, 16.0))
    if inst is None:
        raise RuntimeError("no MatchVar")
    table = inst.vars
    for name, eqn in pairs:
        table[name] = eqn
    try:
        del table["X"]
    except Exception:
        pass
    save = getattr(design, "save_design", None)
    if callable(save):
        save()
    return len(pairs)


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if len(args) < 3:
        print("usage: match_apply_inc.py <workspace> <lib:cell:view> <inc>")
        return 2
    ws, dsn, inc = args[0], args[1], args[2]
    log = Path(inc).with_suffix(".apply.log")
    try:
        pairs = parse_inc(Path(inc).read_text(encoding="ascii"))
        from keysight.ads import de
        from keysight.ads.de import db_uu
        from keysight.ads.de.db import DesignMode

        if not de.workspace_is_open():
            de.open_workspace(ws)
        try:
            design = db_uu.open_design(dsn, mode=DesignMode.WRITE)
        except Exception:
            design = db_uu.open_design(dsn, mode=DesignMode.READ_ONLY)
            inst = design.find_instance("MatchVar")
            have = set(inst.vars.keys()) if inst is not None else set()
            need = {name for name, _eqn in pairs}
            if need and need <= have:
                print(f"ok already {len(need)}")
                return 0
            raise
        n = apply_inc(design, pairs)
        print(f"ok {n}")
        return 0
    except Exception:
        text = traceback.format_exc()
        print(text)
        try:
            log.write_text(text, encoding="utf-8")
        except OSError:
            pass
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
