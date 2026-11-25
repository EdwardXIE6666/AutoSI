"""ADS 天线口匹配：Optim AEL 生成与握手 JSON 解析（纯文本，不依赖 ADS Python 包）。"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path

from ads.match_report import BandResult
from ads.match_slots import C_SEED, C_VALUES, L_SEED, L_VALUES

STATUS_NAME = "_match_50r_status.json"
READY_NAME = "_ads_ready.txt"
TRACE_NAME = "_match_50r_trace.txt"
PHASE_NAME = "_match_50r_phase.txt"
DONE_NAME = "_match_50r_done.txt"
FREEZE_NAME = "_match_50r_freeze.txt"
STAMP_NAME = "_match_50r_stamp.txt"
MATCH_RUN = "match_50r_run"
MATCH_RUN_FIRST = "match_50r_run_first"

_GLOBAL_ANT = re.compile(r"^ANT[_-]?\d+$")
_ANT_NUM = re.compile(r"ANT[_-]?(\d+)", re.I)
_CELL_SEQ = re.compile(r"^(\d+)_")
_ROOT_COM = re.compile(r"ANT[_-]?\d+_COM$", re.I)
_STATUS_FIELDS = (
    "ant",
    "band",
    "worst_vswr",
    "s11_mag",
    "status",
    "at_bound",
    "cross_ant",
    "values",
)


@dataclass
class OptimCell:
    cell: str
    bands: list[tuple[str, str]]
    vars: list[tuple[str, str, str, str]]
    band_vars: list[list[tuple[str, str, str, str]]] | None = None


def is_global_ant(name: str) -> bool:
    return bool(_GLOBAL_ANT.fullmatch(name))


def is_root_com(name: str) -> bool:
    """天线公共端：``001_ANT2_COM`` / ``ANT2_COM``。中段 ``..._B1_3_DRX_COM`` 不算。"""
    return bool(_ROOT_COM.search(name or ""))


def cell_sort_key(name: str) -> tuple:
    """天线号从小到大；同一天线先公共端，再带它 symbol 的段（按落图序号）。"""
    ant = _ANT_NUM.search(name or "")
    seq = _CELL_SEQ.match(name or "")
    return (
        int(ant.group(1)) if ant else 10**9,
        0 if is_root_com(name) else 1,
        int(seq.group(1)) if seq else 10**9,
        name or "",
    )


def order_match_cells(cells: list[OptimCell]) -> list[OptimCell]:
    return sorted(cells, key=lambda c: cell_sort_key(c.cell))


def dataset_name(cell: str) -> str:
    """ADS Data Display / VM 层吃不了中文路径，也吃不了以数字开头的 cell 名。

    只改 dataset **名**（仍落在 workspace/data）；真正挡住弹窗的是
    ``db_set_opendds_option(ctx, FALSE)``。名必须 ASCII，且首字符是字母。
    """
    s = re.sub(r"[^A-Za-z0-9]", "", cell or "")
    if not s or s[0].isdigit():
        s = "m" + s
    return s[:64]


def cells_to_render(cells: list[OptimCell]) -> list[OptimCell]:
    segs = [c for c in cells if not is_global_ant(c.cell)]
    picked = segs if segs else [c for c in cells if is_global_ant(c.cell)]
    return order_match_cells(picked)


def _dedup_vars(vars: list[tuple[str, str, str, str]]) -> list[tuple[str, str, str, str]]:
    seen: set[tuple[str, str]] = set()
    out: list[tuple[str, str, str, str]] = []
    for inst, param, lo, hi in vars:
        key = (inst, param)
        if key in seen:
            continue
        seen.add(key)
        out.append((inst, param, lo, hi))
    return out


def _as_float(value: object) -> float | None:
    if value is None or value == "":
        return None
    return float(value)


def _ael_str(text: str) -> str:
    return '"' + str(text).replace("\\", "\\\\").replace('"', '\\"') + '"'


def _ael_list(values: tuple[str, ...]) -> str:
    return "list(" + ", ".join(_ael_str(v) for v in values) + ")"


_UNIT_SI = {
    "pF": "e-12",
    "nF": "e-9",
    "uF": "e-6",
    "nH": "e-9",
    "uH": "e-6",
    "pH": "e-12",
}


def _library(param: str) -> tuple[str, ...]:
    return C_VALUES if param == "C" else L_VALUES


def _seed(param: str) -> str:
    return C_SEED if param == "C" else L_SEED


def idx_var(inst: str) -> str:
    return f"i_{inst}"


def val_var(inst: str) -> str:
    return f"v_{inst}"


def opt_expr(param: str, lo: str = "", hi: str = "") -> str:
    """ADS 只认 ``opt{discrete min to max by step}``，写在 VAR 下标上。

    C/L 实例只能写数字、``opt{}`` 或简单变量名，不能写 ``if`` / 逗号表。
    """
    del lo, hi
    values = _library(param)
    return f"{values.index(_seed(param))} opt{{discrete 0 to {len(values) - 1} by 1}}"


def library_si(token: str) -> str:
    num, unit = str(token).split()
    return f"{num}{_UNIT_SI[unit]}"


def library_sum_expr(param: str, idx_name: str) -> str:
    """表查找：``(idx==0)*0.2e-12+(idx==1)*0.3e-12+...``，给 VAR 方程用。"""
    return "+".join(
        f"({idx_name}=={i})*{library_si(value)}"
        for i, value in enumerate(_library(param))
    )


def append_match_trace(path: Path, step: str, detail: str = "") -> None:
    """Python-side breadcrumb. Same `step|detail` lines as AEL `_match_trace`."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8", newline="\n") as fid:
        fid.write(f"{step}|{detail}\n")


def _prelude(trace_fwd: str) -> list[str]:
    return [
        "// match_50r.ael -- define only; call match_50r_run after ADS desktop is up.",
        "// Do not run this file as ads.exe -m boot: debootcb + de_analyze freezes DDS.",
        "// ADS 2025: de_analyze() netlists every active controller.",
        "// Cells: ANT small to large; root COM first, then cells with its symbol.",
        "// One band at a time: only that SP + Optim1 + that Goal are on.",
        "// One cell at a time: start Optim, return to ADS, later kick stamps.",
        "// Call builtin de_optimize(); do not defun that name (it would hide Optim).",
        "// After de_optimize, return to ADS so DE can handshake; do not wait",
        "// in this AEL call. Same kick later stamps when the log is ready.",
        "// After Optim, C/L still name v_*; db_get_instance_parm would evaluate",
        "// opt{} and fail without a sim. Read i_* from MatchVar and write a table token.",
        "// Do not wait for 'Simulation finished' in logFile: the GUI shows it, the file",
        "// writes Resource usage / Total stopwatch time instead.",
        "// Do not treat .ds mtime as done: it is touched when the sim starts.",
        "// stamp_keep commits this layer to done.txt and freeze.txt (table pF/nH).",
        "// Do not trace stamp_keep if those ledgers were not written.",
        "// After a cell, turn Term/SP/Optim/Goal off and save before the next",
        "// cell. Nested symbols otherwise flatten parent Term numbers",
        "// (Duplicate Port) or parent SP.",
        "// Do not call de_update_optimization_values: it opens DDS and fails",
        "// creating a dataset control for the .ds. Freeze C/L with identify_value.",
        "// Do not call de_close_all_datadisplay: it send_server_command to",
        "// hpeesofdds and errors when the server is still busy.",
        "// VAR netlist %F writes i_*/v_* before instances. append() needs a list,",
        "// so collect with list(prm). Wrap once at set: list(_match_var_prms).",
        "// Passing the flat prm list as top-level params lands equations after C/L.",
        "// A broken MatchVar must be dropped and rebuilt; do not keep 'already'.",
        "// de_optimize returns while Optim still runs. Do not wait in this AEL",
        "// call. Arm an outside stamp so one load writes back after Optim ends.",
        "",
        f"decl _match_trace_path = {_ael_str(trace_fwd)};",
        f"decl _match_phase_path = {_ael_str(_fwd(str(Path(trace_fwd).with_name(PHASE_NAME))))};",
        f"decl _match_done_path = {_ael_str(_fwd(str(Path(trace_fwd).with_name(DONE_NAME))))};",
        f"decl _match_freeze_path = {_ael_str(_fwd(str(Path(trace_fwd).with_name(FREEZE_NAME))))};",
        f"decl _match_stamp_path = {_ael_str(_fwd(str(Path(trace_fwd).with_name(STAMP_NAME))))};",
        f"decl _match_autostamp_py = {_ael_str(_fwd(str(Path(trace_fwd).resolve().parent / 'scripts' / '_match_autostamp.py')))};",
        f"decl _match_c_lib = {_ael_list(C_VALUES)};",
        f"decl _match_l_lib = {_ael_list(L_VALUES)};",
        "decl _match_inc_path = \"\";",
        "decl _match_inc_name = \"\";",
        "decl _match_dsn = \"\";",
        "decl _match_ds = \"\";",
        "decl _match_dumped = FALSE;",
        "decl _match_armed = FALSE;",
        "decl _match_var_prms = list();",
        "decl _match_freeze_buf = \"\";",
        "decl _match_read_ok = FALSE;",
        "",
        "defun _match_dump_var_names(ctx)",
        "{",
        "  if (_match_dumped)",
        "    return;",
        "  _match_dumped = TRUE;",
        "  decl mv = db_find_instance_ex(ctx, \"MatchVar\");",
        "  if (mv == NULL)",
        "  {",
        "    _match_trace(\"parm\", \"no_MatchVar\");",
        "    return;",
        "  }",
        "  decl p = db_first_parm(mv);",
        "  decl n = 0;",
        "  while ((p != NULL) && (n < 20))",
        "  {",
        "    decl nm = db_get_parm_attribute(p, PARM_NAME);",
        "    decl code = db_get_parm_attribute(p, PARM_VALUE_CODE);",
        "    _match_trace(\"parm\", strcat(identify_value(nm), \"|\", identify_value(code)));",
        "    decl q = db_first_parm(p);",
        "    decl m = 0;",
        "    while ((q != NULL) && (m < 20))",
        "    {",
        "      decl nm2 = db_get_parm_attribute(q, PARM_NAME);",
        "      decl code2 = db_get_parm_attribute(q, PARM_VALUE_CODE);",
        "      _match_trace(\"parm2\", strcat(identify_value(nm2), \"|\", identify_value(code2)));",
        "      q = db_next_parm(q);",
        "      m = m + 1;",
        "    }",
        "    p = db_next_parm(p);",
        "    n = n + 1;",
        "  }",
        "}",
        "",
        "defun _match_trace(step, detail)",
        "{",
        "  if (_match_trace_path == \"\")",
        "    return;",
        "  decl fid = fopen(_match_trace_path, \"A\");",
        "  if (fid == NULL)",
        "    return;",
        '  fputs(fid, strcat(step, "|", detail, "\\n"));',
        "  fclose(fid);",
        "}",
        "",
        "defun _match_yield()",
        "{",
        "  de_refresh_view();",
        "  de_refresh_workspace();",
        "}",
        "",
        "defun _match_idle()",
        "{",
        "  _match_yield();",
        "}",
        "",
        "defun _match_nth(lst, idx)",
        "{",
        "  if ((idx == NULL) || (idx < 0) || (idx > 34))",
        "    return \"\";",
        "  decl n = 0;",
        "  while ((lst != NULL) && (n < idx) && (n < 40))",
        "  {",
        "    lst = cdr(lst);",
        "    n = n + 1;",
        "  }",
        "  if (lst == NULL)",
        "    return \"\";",
        "  return car(lst);",
        "}",
        "",
        "defun _match_find_parm(inst, name)",
        "{",
        "  return _match_find_parm_d(inst, name, 0);",
        "}",
        "",
        "defun _match_find_parm_d(root, name, depth)",
        "{",
        "  if (depth > 4)",
        "    return NULL;",
        "  decl p = db_first_parm(root);",
        "  decl n = 0;",
        "  while ((p != NULL) && (n < 80))",
        "  {",
        "    decl nm = db_get_parm_attribute(p, PARM_NAME);",
        "    if (nm == name)",
        "      return p;",
        "    if ((index(nm, \"v_\") != 1) && (index(nm, \"mv_\") != 1))",
        "    {",
        "      decl child = _match_find_parm_d(p, name, depth + 1);",
        "      if (child != NULL)",
        "        return child;",
        "    }",
        "    p = db_next_parm(p);",
        "    n = n + 1;",
        "  }",
        "  return NULL;",
        "}",
        "",
        "defun _match_read_idx(ctx, name)",
        "{",
        "  decl mv = db_find_instance_ex(ctx, \"MatchVar\");",
        "  if (mv == NULL)",
        "    return -1;",
        "  decl p = _match_find_parm(mv, name);",
        "  if (p == NULL)",
        "    return -1;",
        "  decl code = db_get_parm_attribute(p, PARM_VALUE_CODE);",
        "  if ((code == 1) || (code == 2))",
        "  {",
        "    decl d = db_get_parm_attribute(p, PARM_VALUE_DVALUE);",
        "    if ((d < 0) || (d > 34))",
        "      return -1;",
        "    return d;",
        "  }",
        "  decl s = db_get_parm_attribute(p, PARM_VALUE_SVALUE);",
        "  if ((s == NULL) || (s == \"\"))",
        "  {",
        "    decl ch = db_first_parm(p);",
        "    if (ch == NULL)",
        "      return -1;",
        "    decl cd = db_get_parm_attribute(ch, PARM_VALUE_CODE);",
        "    if ((cd == 1) || (cd == 2))",
        "    {",
        "      decl d2 = db_get_parm_attribute(ch, PARM_VALUE_DVALUE);",
        "      if ((d2 < 0) || (d2 > 34))",
        "        return -1;",
        "      return d2;",
        "    }",
        "    s = db_get_parm_attribute(ch, PARM_VALUE_SVALUE);",
        "    if ((s == NULL) || (s == \"\"))",
        "      return -1;",
        "  }",
        "  decl sp = index(s, \" \");",
        "  if (sp > 1)",
        "    s = leftstr(s, sp - 1);",
        "  decl n = identify_value(s);",
        "  if ((n < 0) || (n > 34))",
        "    return -1;",
        "  return n;",
        "}",
        "",
        "defun _match_digit(ch)",
        "{",
        "  if (ch == \"0\")",
        "    return 0;",
        "  if (ch == \"1\")",
        "    return 1;",
        "  if (ch == \"2\")",
        "    return 2;",
        "  if (ch == \"3\")",
        "    return 3;",
        "  if (ch == \"4\")",
        "    return 4;",
        "  if (ch == \"5\")",
        "    return 5;",
        "  if (ch == \"6\")",
        "    return 6;",
        "  if (ch == \"7\")",
        "    return 7;",
        "  if (ch == \"8\")",
        "    return 8;",
        "  if (ch == \"9\")",
        "    return 9;",
        "  return -1;",
        "}",
        "",
        "defun _match_int_tok(s)",
        "{",
        "  if ((s == NULL) || (s == \"\"))",
        "    return -1;",
        "  decl nlen = strlen(s);",
        "  while (nlen > 0)",
        "  {",
        "    decl last = rightstr(s, 1);",
        "    if ((last == \"\\n\") || (last == \"\\r\") || (last == \" \"))",
        "    {",
        "      s = leftstr(s, nlen - 1);",
        "      nlen = nlen - 1;",
        "    }",
        "    else",
        "      break;",
        "  }",
        "  while (strlen(s) > 0)",
        "  {",
        "    decl head = leftstr(s, 1);",
        "    if ((head != \" \") && (head != \"=\") && (head != \"\\t\"))",
        "      break;",
        "    s = rightstr(s, strlen(s) - 1);",
        "  }",
        "  decl sp = index(s, \" \");",
        "  if (sp > 1)",
        "    s = leftstr(s, sp - 1);",
        "  if (s == \"\")",
        "    return -1;",
        "  decl n = 0;",
        "  decl got = 0;",
        "  while (strlen(s) > 0)",
        "  {",
        "    decl d = _match_digit(leftstr(s, 1));",
        "    if (d < 0)",
        "      break;",
        "    n = ((n * 10) + d);",
        "    got = 1;",
        "    s = rightstr(s, strlen(s) - 1);",
        "  }",
        "  if (got < 1)",
        "    return -1;",
        "  if (n > 34)",
        "    return -1;",
        "  return n;",
        "}",
        "",
        "defun _match_read_idx_log(name)",
        "{",
        "  if (_match_ds == \"\")",
        "    return -1;",
        "  decl ws = de_get_open_workspace_pathname();",
        "  decl logp = strcat(ws, \"/data/\", _match_ds, \"_data/logFile.txt\");",
        "  decl fid = fopen(logp, \"r\");",
        "  if (fid == NULL)",
        "  {",
        "    _match_trace(\"log_open\", logp);",
        "    return -1;",
        "  }",
        "  decl line = fgets(fid);",
        "  decl last = -1;",
        "  while (line != NULL)",
        "  {",
        "    if ((index(line, strcat(\".\", name)) < 1) && (index(line, name) > 0))",
        "    {",
        "      decl pos = index(line, \"=\");",
        "      if (pos > 0)",
        "      {",
        "        decl n = _match_int_tok(rightstr(line, strlen(line) - pos));",
        "        if (n >= 0)",
        "          last = n;",
        "      }",
        "    }",
        "    line = fgets(fid);",
        "  }",
        "  fclose(fid);",
        "  if (last < 0)",
        "    _match_trace(\"log_fail\", logp);",
        "  else",
        "    _match_trace(\"log_idx\", strcat(name, \"|\", identify_value(last)));",
        "  return last;",
        "}",
        "",
        "defun _match_freeze_inst(ctx, inst, param, i_name)",
        "{",
        "  _match_trace(\"freeze_try\", inst);",
        "  if (db_find_instance_ex(ctx, inst) == NULL)",
        "    return \"\";",
        "  decl idx = _match_read_idx(ctx, i_name);",
        "  if (idx < 0)",
        "    idx = _match_read_idx_log(i_name);",
        "  if (idx < 0)",
        "  {",
        "    _match_trace(\"freeze_miss\", i_name);",
        "    _match_dump_var_names(ctx);",
        "    return \"\";",
        "  }",
        "  decl tok = \"\";",
        "  if (param == \"C\")",
        "    tok = _match_nth(_match_c_lib, idx);",
        "  else",
        "    tok = _match_nth(_match_l_lib, idx);",
        "  if (tok == \"\")",
        "  {",
        "    if (param == \"C\")",
        "      tok = \"1 pF\";",
        "    else",
        "      tok = \"1 nH\";",
        "  }",
        "  _match_trace(\"freeze\", strcat(inst, \"|\", identify_value(idx), \"|\", tok));",
        "  db_update_parameters_ex(ctx, list(inst, param, tok));",
        "  _match_freeze_save(inst, param, idx, tok);",
        "  if (db_find_instance_ex(ctx, \"MatchVar\") != NULL)",
        "    db_update_parameters_ex(ctx, list(\"MatchVar\", i_name, identify_value(idx)));",
        "  return tok;",
        "}",
        "",
        "defun _match_trim_nl(line)",
        "{",
        "  decl n = strlen(line);",
        "  while (n > 0)",
        "  {",
        "    decl last = rightstr(line, 1);",
        "    if ((last == \"\\n\") || (last == \"\\r\"))",
        "    {",
        "      line = leftstr(line, n - 1);",
        "      n = n - 1;",
        "    }",
        "    else",
        "      break;",
        "  }",
        "  return line;",
        "}",
        "",
        "defun _match_pipe_field(line, nth)",
        "{",
        "  decl i = 1;",
        "  decl rest = line;",
        "  while ((rest != NULL) && (rest != \"\") && (i < 20))",
        "  {",
        "    decl p = index(rest, \"|\");",
        "    if (i == nth)",
        "    {",
        "      if (p < 1)",
        "        return rest;",
        "      return leftstr(rest, p - 1);",
        "    }",
        "    if (p < 1)",
        "      return \"\";",
        "    rest = rightstr(rest, strlen(rest) - p);",
        "    i = i + 1;",
        "  }",
        "  return \"\";",
        "}",
        "",
        "defun _match_slurp(path)",
        "{",
        "  _match_read_ok = FALSE;",
        "  decl fid = fopen(path, \"r\");",
        "  if (fid == NULL)",
        "    return \"\";",
        "  _match_read_ok = TRUE;",
        "  decl all = \"\";",
        "  decl line = fgets(fid);",
        "  while (line != NULL)",
        "  {",
        "    all = strcat(all, line);",
        "    line = fgets(fid);",
        "  }",
        "  fclose(fid);",
        "  return all;",
        "}",
        "",
        "defun _match_no_cr(s)",
        "{",
        "  decl out = \"\";",
        "  decl rest = s;",
        "  if (rest == NULL)",
        "    return \"\";",
        "  while (rest != \"\")",
        "  {",
        "    decl p = index(rest, \"\\r\");",
        "    if (p < 1)",
        "      return strcat(out, rest);",
        "    if (p > 1)",
        "      out = strcat(out, leftstr(rest, p - 1));",
        "    rest = rightstr(rest, strlen(rest) - p);",
        "  }",
        "  return out;",
        "}",
        "",
        "defun _match_contains(hay, needle)",
        "{",
        "  if ((needle == NULL) || (needle == \"\"))",
        "    return TRUE;",
        "  if (hay == NULL)",
        "    return FALSE;",
        "  if (index(_match_no_cr(hay), _match_no_cr(needle)) > 0)",
        "    return TRUE;",
        "  return FALSE;",
        "}",
        "",
        "defun _match_append_file(path, chunk)",
        "{",
        "  if ((chunk == NULL) || (chunk == \"\"))",
        "    return TRUE;",
        "  decl mark = chunk;",
        "  decl nl = index(mark, \"\\n\");",
        "  if (nl > 0)",
        "    mark = leftstr(mark, nl - 1);",
        "  decl n = 0;",
        "  decl fid = NULL;",
        "  while (n < 5)",
        "  {",
        "    if (ael_file_exists(path) == FALSE)",
        "    {",
        "      if (_match_write(path, chunk) == TRUE)",
        "        return TRUE;",
        "    }",
        "    else",
        "    {",
        "      fid = fopen(path, \"a\");",
        "      if (fid == NULL)",
        "        fid = fopen(path, \"A\");",
        "      if (fid != NULL)",
        "      {",
        "        fputs(fid, chunk);",
        "        fclose(fid);",
        "        decl prev = _match_slurp(path);",
        "        if (_match_read_ok && (_match_contains(prev, mark) == TRUE))",
        "          return TRUE;",
        "      }",
        "    }",
        "    _match_idle();",
        "    n = n + 1;",
        "  }",
        "  _match_trace(\"append_fail\", path);",
        "  return FALSE;",
        "}",
        "",
        "defun _match_freeze_save(inst, param, idx, tok)",
        "{",
        "  if (_match_ds == \"\")",
        "    return;",
        "  decl row = strcat(_match_ds, \"|\", inst, \"|\", param, \"|\", identify_value(idx), \"|\", tok, \"\\n\");",
        "  _match_freeze_buf = strcat(_match_freeze_buf, row);",
        "}",
        "",
        "defun _match_freeze_flush()",
        "{",
        "  if (_match_freeze_buf == \"\")",
        "    return TRUE;",
        "  decl mark = strcat(_match_ds, \"|\");",
        "  if (_match_append_file(_match_freeze_path, _match_freeze_buf) == FALSE)",
        "  {",
        "    decl had = _match_slurp(_match_freeze_path);",
        "    if (_match_read_ok && (_match_contains(had, mark) == TRUE))",
        "    {",
        "      _match_freeze_buf = \"\";",
        "      return TRUE;",
        "    }",
        "    _match_trace(\"freeze_fail\", _match_ds);",
        "    return FALSE;",
        "  }",
        "  decl rows = _match_slurp(_match_freeze_path);",
        "  if ((_match_read_ok == FALSE) || (_match_contains(rows, mark) == FALSE))",
        "  {",
        "    _match_trace(\"freeze_fail\", _match_ds);",
        "    return FALSE;",
        "  }",
        "  _match_freeze_buf = \"\";",
        "  return TRUE;",
        "}",
        "",
        "defun _match_restore_frozen(ctx)",
        "{",
        "  if ((_match_ds == \"\") || (ctx == NULL))",
        "    return;",
        "  decl fid = fopen(_match_freeze_path, \"r\");",
        "  if (fid == NULL)",
        "  {",
        "    _match_trace(\"restore_open\", _match_freeze_path);",
        "    return;",
        "  }",
        "  _match_trace(\"restore_try\", _match_ds);",
        "  decl line = fgets(fid);",
        "  while (line != NULL)",
        "  {",
        "    line = _match_trim_nl(line);",
        "    decl ds = _match_pipe_field(line, 1);",
        "    if (ds == _match_ds)",
        "    {",
        "      decl inst = _match_pipe_field(line, 2);",
        "      decl param = _match_pipe_field(line, 3);",
        "      decl tok = _match_pipe_field(line, 5);",
        "      if ((inst != \"\") && (param != \"\") && (tok != \"\") && (db_find_instance_ex(ctx, inst) != NULL))",
        "      {",
        "        db_update_parameters_ex(ctx, list(inst, param, tok));",
        "        _match_trace(\"restore\", strcat(inst, \"|\", tok));",
        "      }",
        "    }",
        "    line = fgets(fid);",
        "  }",
        "  fclose(fid);",
        "}",
        "",
        "defun _match_drop_matchvar(ctx)",
        "{",
        "  if (db_find_instance_ex(ctx, \"MatchVar\") == NULL)",
        "    return;",
        "  de_deselect_all();",
        "  de_select_by_name(\"MatchVar\", 1);",
        "  de_delete();",
        "  de_deselect_all();",
        "  _match_trace(\"drop_var\", \"MatchVar\");",
        "}",
        "",
        "defun de_open_design(name)",
        "{",
        "  decl ctx = db_get_design_context_from_name(name, APPEND_DSN_MODE);",
        "  if (ctx == NULL)",
        "  {",
        '    de_info(strcat("match_50r: cannot open ", name));',
        "    return NULL;",
        "  }",
        "  de_bring_context_to_top_or_open_new_window(ctx);",
        "  db_set_opendds_option(ctx, FALSE);",
        "  _match_idle();",
        "  return ctx;",
        "}",
        "",
        "defun _match_is_term_cell(cell)",
        "{",
        "  return ((cell == \"Term\") || (cell == \"TermG\"));",
        "}",
        "",
        "defun _match_set_sim_items(ctx, on)",
        "{",
        "  decl iter = db_create_inst_iter(ctx);",
        "  while (db_inst_iter_is_valid(iter))",
        "  {",
        "    decl inst = db_inst_iter_get_instance(iter);",
        "    decl cell = db_get_instance_cell_name(inst);",
        "    decl nm = db_get_instance_name(inst);",
        "    if (nm != \"\")",
        "    {",
        "      if (on)",
        "      {",
        "        if (_match_is_term_cell(cell))",
        "          _match_goal_on(nm, TRUE);",
        "      }",
        "      else if (_match_is_term_cell(cell) || (cell == \"S_Param\") || (cell == \"Goal\") || (cell == \"Optim\"))",
        "      {",
        "        _match_goal_on(nm, FALSE);",
        "        if (cell == \"S_Param\")",
        "          db_update_parameters_ex(ctx, list(nm, \"EnableOptim\", \"no\"));",
        "      }",
        "    }",
        "    iter = db_inst_iter_get_next(iter);",
        "  }",
        "}",
        "",
        "defun _match_wake_cell(ctx)",
        "{",
        "  _match_set_sim_items(ctx, TRUE);",
        '  _match_trace("wake", "TermG");',
        "}",
        "",
        "defun _match_shutdown_cell(ctx)",
        "{",
        "  _match_set_sim_items(ctx, FALSE);",
        "  db_save_design_without_prompting(ctx);",
        '  _match_trace("shutdown", "Term SP Goal Optim");',
        "}",
        "",
        "defun _match_finish_cell(ctx)",
        "{",
        "  _match_shutdown_cell(ctx);",
        "  de_release_simulator();",
        "  _match_idle();",
        "  de_close_window();",
        "  _match_idle();",
        "}",
        "",
        "defun _match_park_done(name)",
        "{",
        "  decl ctx = de_open_design(name);",
        "  if (ctx == NULL)",
        "    return;",
        "  _match_restore_frozen(ctx);",
        "  _match_drop_matchvar(ctx);",
        "  _match_finish_cell(ctx);",
        "  _match_trace(\"park_done\", name);",
        "}",
        "",
        "defun _match_log_done(path)",
        "{",
        "  if (ael_file_exists(path) == FALSE)",
        "    return FALSE;",
        "  decl fid = fopen(path, \"r\");",
        "  if (fid == NULL)",
        "    return FALSE;",
        "  decl line = fgets(fid);",
        "  decl found = FALSE;",
        "  while (line != NULL)",
        "  {",
        "    if (index(line, \"Resource usage\") > 0)",
        "      found = TRUE;",
        "    if (index(line, \"Total stopwatch time\") > 0)",
        "      found = TRUE;",
        "    if (index(line, \"Simulation finished\") > 0)",
        "      found = TRUE;",
        "    if (index(line, \"Simulation terminated\") > 0)",
        "      found = TRUE;",
        "    if (index(line, \"Simulation Aborted\") > 0)",
        "      found = TRUE;",
        "    line = fgets(fid);",
        "  }",
        "  fclose(fid);",
        "  return found;",
        "}",
        "",
        "defun _match_log_optim_done(path)",
        "{",
        "  if (ael_file_exists(path) == FALSE)",
        "    return FALSE;",
        "  decl fid = fopen(path, \"r\");",
        "  if (fid == NULL)",
        "    return FALSE;",
        "  decl line = fgets(fid);",
        "  decl found = FALSE;",
        "  while (line != NULL)",
        "  {",
        "    if (index(line, \"Optimization completed\") > 0)",
        "      found = TRUE;",
        "    if (index(line, \"Optimization finished\") > 0)",
        "      found = TRUE;",
        "    if (index(line, \"Maximum number of iterations\") > 0)",
        "      found = TRUE;",
        "    if (index(line, \"reached the iteration limit\") > 0)",
        "      found = TRUE;",
        "    if (index(line, \"Random optimizer ends\") > 0)",
        "      found = TRUE;",
        "    if (index(line, \"Goals are satisfied\") > 0)",
        "      found = TRUE;",
        "    line = fgets(fid);",
        "  }",
        "  fclose(fid);",
        "  return found;",
        "}",
        "",
        "defun _match_phase_get()",
        "{",
        "  decl fid = fopen(_match_phase_path, \"r\");",
        "  if (fid == NULL)",
        "    return \"\";",
        "  decl line = fgets(fid);",
        "  fclose(fid);",
        "  if (line == NULL)",
        "    return \"\";",
        "  decl n = strlen(line);",
        "  while (n > 0)",
        "  {",
        "    decl last = rightstr(line, 1);",
        "    if ((last == \"\\n\") || (last == \"\\r\"))",
        "    {",
        "      line = leftstr(line, n - 1);",
        "      n = n - 1;",
        "    }",
        "    else",
        "      break;",
        "  }",
        "  return line;",
        "}",
        "",
        "defun _match_done_has(key)",
        "{",
        "  decl fid = fopen(_match_done_path, \"r\");",
        "  if (fid == NULL)",
        "    return FALSE;",
        "  decl line = fgets(fid);",
        "  decl found = FALSE;",
        "  while (line != NULL)",
        "  {",
        "    decl n = strlen(line);",
        "    while (n > 0)",
        "    {",
        "      decl last = rightstr(line, 1);",
        "      if ((last == \"\\n\") || (last == \"\\r\"))",
        "      {",
        "        line = leftstr(line, n - 1);",
        "        n = n - 1;",
        "      }",
        "      else",
        "        break;",
        "    }",
        "    if (line == key)",
        "      found = TRUE;",
        "    line = fgets(fid);",
        "  }",
        "  fclose(fid);",
        "  return found;",
        "}",
        "",
        "defun _match_done_add(key)",
        "{",
        "  if (_match_done_has(key))",
        "    return TRUE;",
        "  if (_match_append_file(_match_done_path, strcat(key, \"\\n\")) == FALSE)",
        "  {",
        "    _match_trace(\"done_fail\", key);",
        "    return FALSE;",
        "  }",
        "  if (_match_done_has(key) == FALSE)",
        "  {",
        "    _match_trace(\"done_fail\", key);",
        "    return FALSE;",
        "  }",
        "  return TRUE;",
        "}",
        "",
        "defun _match_phase_set(state, key)",
        "{",
        "  if (state == \"done\")",
        "  {",
        "    _match_freeze_flush();",
        "    _match_done_add(key);",
        "  }",
        "  _match_write(_match_phase_path, strcat(state, \"|\", key, \"\\n\"));",
        "}",
        "",
        "defun _match_commit_keep(key)",
        "{",
        "  decl rows = \"\";",
        "  if (_match_ds == \"\")",
        "  {",
        "    _match_trace(\"commit_fail\", key);",
        "    return FALSE;",
        "  }",
        "  if (_match_freeze_flush() == FALSE)",
        "  {",
        "    rows = _match_slurp(_match_freeze_path);",
        "    if ((_match_read_ok == FALSE) || (_match_contains(rows, strcat(_match_ds, \"|\")) == FALSE))",
        "    {",
        "      _match_trace(\"commit_fail\", key);",
        "      return FALSE;",
        "    }",
        "  }",
        "  if (_match_done_add(key) == FALSE)",
        "  {",
        "    _match_trace(\"commit_fail\", key);",
        "    return FALSE;",
        "  }",
        "  rows = _match_slurp(_match_freeze_path);",
        "  if ((_match_read_ok == FALSE) || (_match_contains(rows, strcat(_match_ds, \"|\")) == FALSE))",
        "  {",
        "    _match_trace(\"commit_fail\", key);",
        "    return FALSE;",
        "  }",
        "  if (_match_done_has(key) == FALSE)",
        "  {",
        "    _match_trace(\"commit_fail\", key);",
        "    return FALSE;",
        "  }",
        "  if (_match_write(_match_phase_path, strcat(\"done\", \"|\", key, \"\\n\")) == FALSE)",
        "  {",
        "    _match_trace(\"commit_fail\", key);",
        "    return FALSE;",
        "  }",
        "  _match_write(_match_stamp_path, \"\");",
        "  _match_trace(\"stamp_keep\", key);",
        "  return TRUE;",
        "}",
        "",
        "defun _match_phase_is(state, key)",
        "{",
        "  if (state == \"done\")",
        "    return _match_done_has(key);",
        "  decl cur = _match_phase_get();",
        "  decl want = strcat(state, \"|\", key);",
        "  return (cur == want);",
        "}",
        "",
        "defun _match_sim_failed(dsname)",
        "{",
        "  decl ws = de_get_open_workspace_pathname();",
        "  decl logp = strcat(ws, \"/data/\", dsname, \"_data/logFile.txt\");",
        "  if (ael_file_exists(logp) == FALSE)",
        "    return FALSE;",
        "  decl fid = fopen(logp, \"r\");",
        "  if (fid == NULL)",
        "    return FALSE;",
        "  decl line = fgets(fid);",
        "  decl failed = FALSE;",
        "  while (line != NULL)",
        "  {",
        "    if (index(line, \"Simulation terminated due to error\") > 0)",
        "      failed = TRUE;",
        "    if (index(line, \"Simulation finished with errors\") > 0)",
        "      failed = TRUE;",
        "    if (index(line, \"Undefined parameter\") > 0)",
        "      failed = TRUE;",
        "    if (index(line, \"Error detected by hpeesofsim\") > 0)",
        "      failed = TRUE;",
        "    line = fgets(fid);",
        "  }",
        "  fclose(fid);",
        "  return failed;",
        "}",
        "",
        "defun _match_sim_running(dsname)",
        "{",
        "  if (_match_sim_failed(dsname))",
        "    return FALSE;",
        "  if (_match_sim_ready(dsname))",
        "    return FALSE;",
        "  decl ws = de_get_open_workspace_pathname();",
        "  decl logp = strcat(ws, \"/data/\", dsname, \"_data/logFile.txt\");",
        "  if (ael_file_exists(logp) == FALSE)",
        "    return FALSE;",
        "  decl fid = fopen(logp, \"r\");",
        "  if (fid == NULL)",
        "    return FALSE;",
        "  decl line = fgets(fid);",
        "  decl found = FALSE;",
        "  while (line != NULL)",
        "  {",
        "    if (index(line, \"optIter\") > 0)",
        "      found = TRUE;",
        "    if (index(line, \"CurrentEF\") > 0)",
        "      found = TRUE;",
        "    if (index(line, \"OPTIM Optim\") > 0)",
        "      found = TRUE;",
        "    line = fgets(fid);",
        "  }",
        "  fclose(fid);",
        "  return found;",
        "}",
        "",
        "defun _match_stamp_has(dsname)",
        "{",
        "  decl fid = fopen(_match_stamp_path, \"r\");",
        "  if (fid == NULL)",
        "    return FALSE;",
        "  decl line = fgets(fid);",
        "  decl found = FALSE;",
        "  while (line != NULL)",
        "  {",
        "    if (index(line, dsname) > 0)",
        "      found = TRUE;",
        "    line = fgets(fid);",
        "  }",
        "  fclose(fid);",
        "  return found;",
        "}",
        "",
        "defun _match_sim_ready(dsname)",
        "{",
        "  if (_match_sim_failed(dsname))",
        "    return FALSE;",
        "  if (_match_stamp_has(dsname))",
        "    return TRUE;",
        "  decl ws = de_get_open_workspace_pathname();",
        "  decl logp = strcat(ws, \"/data/\", dsname, \"_data/logFile.txt\");",
        "  return _match_log_optim_done(logp);",
        "}",
        "",
        "defun _match_arm_stamp()",
        "{",
        "  if (_match_armed)",
        "    return;",
        "  _match_armed = TRUE;",
        f"  system({_ael_str('cmd /c start \"\" /b python \"' + _fwd(str(Path(trace_fwd).resolve().parent / 'scripts' / '_match_autostamp.py')) + '\"')});",
        '  _match_trace("arm_stamp", _match_autostamp_py);',
        "}",
        "",
        "defun _match_run_sim(dsname)",
        "{",
        "  de_optimize();",
        '  _match_trace("analyze_entered", dsname);',
        "  _match_arm_stamp();",
        "}",
        "",
        "defun _match_goal_on(name, on)",
        "{",
        "  de_deselect_all();",
        "  de_select_by_name(name, 1);",
        "  if (on)",
        "    de_activate();",
        "  else",
        "    de_deactivate();",
        "  de_deselect_all();",
        "}",
        "",
        "defun _match_write(path, text)",
        "{",
        '  decl fid = fopen(path, "W");',
        "  if (fid == NULL)",
        "  {",
        '    de_info(strcat("match_50r: cannot write ", path));',
        "    return FALSE;",
        "  }",
        "  fputs(fid, text);",
        "  fclose(fid);",
        "  return TRUE;",
        "}",
        "",
        "defun _match_purge_x_vars(ctx)",
        "{",
        "  decl iter = db_create_inst_iter(ctx);",
        "  decl names = list();",
        "  while (db_inst_iter_is_valid(iter))",
        "  {",
        "    decl inst = db_inst_iter_get_instance(iter);",
        "    decl cell = db_get_instance_cell_name(inst);",
        "    decl nm = db_get_instance_name(inst);",
        "    if ((nm != \"\") && (((cell == \"VAR\") && ((index(nm, \"i_\") == 1) || (index(nm, \"v_\") == 1) || (nm == \"X\"))) || ((cell == \"MeasEqn\") && ((index(nm, \"mi_\") == 1) || (index(nm, \"mv_\") == 1))) || (nm == \"MatchIncL\")))",
        "      names = append(names, list(nm));",
        "    iter = db_inst_iter_get_next(iter);",
        "  }",
        "  while (listlen(names) > 0)",
        "  {",
        "    decl nm = car(names);",
        "    names = cdr(names);",
        "    de_deselect_all();",
        "    de_select_by_name(nm, 1);",
        "    de_delete();",
        "  }",
        "  de_deselect_all();",
        "}",
        "",
        "defun _match_inc_put(name, eqn)",
        "{",
        "  if (_match_inc_path == \"\")",
        "    return;",
        "  decl fid = fopen(_match_inc_path, \"A\");",
        "  if (fid == NULL)",
        "    return;",
        "  fputs(fid, strcat(name, \"=\", eqn, \"\\n\"));",
        "  fclose(fid);",
        "}",
        "",
        "defun _match_ensure_var(ctx, name, eqn)",
        "{",
        "  _match_inc_put(name, eqn);",
        "  _match_var_prms = append(_match_var_prms, list(prm(\"VarFormStdForm\", prm(\"VarNameForm\", name), prm(\"VarValueForm\", eqn))));",
        "}",
        "",
        "defun _match_has_eqns(ctx)",
        "{",
        "  return (db_find_instance_ex(ctx, \"MatchVar\") != NULL);",
        "}",
        "",
        "defun _match_attach_inc(ctx)",
        "{",
        "  if (listlen(_match_var_prms) < 1)",
        "  {",
        "    if (db_find_instance_ex(ctx, \"MatchVar\") != NULL)",
        "      _match_trace(\"apply_var\", \"already\");",
        "    else",
        "      _match_trace(\"apply_var\", \"missing\");",
        "    return;",
        "  }",
        "  _match_drop_matchvar(ctx);",
        "  decl item = de_init_item(\"VAR\");",
        "  if (item == NULL)",
        "    item = de_init_item(\"ads_datacmps:VAR:symbol\");",
        "  if (item == NULL)",
        "  {",
        "    _match_trace(\"apply_var\", \"no_var\");",
        "    _match_var_prms = list();",
        "    return;",
        "  }",
        "  de_set_item_id(item, \"MatchVar\");",
        "  de_set_item_parameters(item, list(_match_var_prms));",
        "  de_place_item(item, 0, 16);",
        "  item = de_free_item(item);",
        "  _match_trace(\"apply_var\", strcat(\"ael|\", identify_value(listlen(_match_var_prms))));",
        "  _match_var_prms = list();",
        "}",
        "",
        "defun _match_freeze_var(ctx, dsn, name)",
        "{",
        "  return _match_read_idx(ctx, name);",
        "}",
        "",
    ]


def _unrun_objects(ant: str, bands: list[tuple[str, str]]) -> str:
    parts = []
    for sp_name, _vswr in bands:
        parts.append(
            "{"
            f'"ant":"{ant}","band":"{sp_name}",'
            '"worst_vswr":null,"s11_mag":null,'
            '"status":"unrun","at_bound":[],"cross_ant":"","values":{}'
            "}"
        )
    return ",".join(parts)


def _vars_for_band(cell: OptimCell, index: int) -> list[tuple[str, str, str, str]]:
    if cell.band_vars and index < len(cell.band_vars) and cell.band_vars[index]:
        return _dedup_vars(list(cell.band_vars[index]))
    return _dedup_vars(list(cell.vars))


def _cell_block(lib: str, cell: OptimCell) -> list[str]:
    dsn = f"{lib}:{cell.cell}:schematic"
    vars_ = _dedup_vars(list(cell.vars))
    ds = dataset_name(cell.cell)
    done_pred = " && ".join(
        f'_match_phase_is("done", {_ael_str(f"{ds}|{sp}")})' for sp, _ in cell.bands
    ) or "FALSE"
    lines = [
        "{",
        f"  decl dsn = {_ael_str(dsn)};",
        "  _match_dsn = dsn;",
        f"  _match_ds = {_ael_str(ds)};",
        f"  if ({done_pred})",
        "  {",
        f'    _match_trace("skip_done", {_ael_str(cell.cell)});',
        "    if (limit == 0)",
        "      _match_park_done(dsn);",
        "  }",
        "  else",
        "  {",
        f'  _match_trace("open", {_ael_str(cell.cell)});',
        "  decl ctx = de_open_design(dsn);",
        "  decl vals = \"\";",
        "  decl vcomma = \"\";",
        "  decl st = \"unrun\";",
        "  if (ctx != NULL)",
        "  {",
        f"    db_set_dataset_name(ctx, {_ael_str(dataset_name(cell.cell))});",
        "    _match_idle();",
        "    _match_wake_cell(ctx);",
        "    _match_purge_x_vars(ctx);",
        "    // add ads_simulation:Optim:symbol as Optim1",
        "    if (db_find_instance_ex(ctx, \"Optim1\") == NULL)",
        "    {",
        "      decl optItem = de_init_item(\"Optim\");",
        "      de_set_item_id(optItem, \"Optim1\");",
        "      de_place_item(optItem, 0, 12);",
        "      optItem = de_free_item(optItem);",
        "    }",
        "    db_update_parameters_ex(ctx, list(\"Optim1\", \"EnableCockpit\", \"yes\"));",
        "    db_update_parameters_ex(ctx, list(\"Optim1\", \"OptimType\", \"Random\"));",
        "    db_update_parameters_ex(ctx, list(\"Optim1\", \"MaxIters\", \"40\"));",
        "    // add ads_simulation:Goal:symbol per band, Expr uses VSWR name",
    ]
    for i, (sp_name, vswr_name) in enumerate(cell.bands):
        goal = f"Goal{i}"
        lines.extend(
            [
                f"    // {goal} Expr={vswr_name}",
                f"    if (db_find_instance_ex(ctx, {_ael_str(goal)}) == NULL)",
                "    {",
                "      decl gItem = de_init_item(\"Goal\");",
                f"      de_set_item_id(gItem, {_ael_str(goal)});",
                f"      de_place_item(gItem, {4 + 3 * i}, 12);",
                "      gItem = de_free_item(gItem);",
                "    }",
                f"    db_update_parameters_ex(ctx, list({_ael_str(goal)}, \"Expr\", {_ael_str(vswr_name)}));",
                f"    db_update_parameters_ex(ctx, list({_ael_str(goal)}, \"SimInstanceName\", {_ael_str(sp_name)}));",
                f"    db_update_parameters_ex(ctx, list({_ael_str(goal)}, \"Max\", \"1\"));",
                f"    if (db_find_instance_ex(ctx, {_ael_str(sp_name)}) != NULL)",
                f"      db_update_parameters_ex(ctx, list({_ael_str(sp_name)}, \"EnableOptim\", \"yes\"));",
            ]
        )
    for i, (_sp, vswr_name) in enumerate(cell.bands):
        ta_num = 2 * i + 1
        tc_num = 2 * i + 2
        lines.extend(
            [
                f"    if (db_find_instance_ex(ctx, {_ael_str(f'T_A{i}')}) != NULL)",
                f"      db_update_parameters_ex(ctx, list({_ael_str(f'T_A{i}')}, \"Num\", \"{ta_num}\"));",
                f"    if (db_find_instance_ex(ctx, {_ael_str(f'T_C{i}')}) != NULL)",
                f"      db_update_parameters_ex(ctx, list({_ael_str(f'T_C{i}')}, \"Num\", \"{tc_num}\"));",
                f"    if (db_find_instance_ex(ctx, {_ael_str(vswr_name)}) != NULL)",
                f"      db_update_parameters_ex(ctx, list({_ael_str(vswr_name)}, \"Function\", {_ael_str(f'{vswr_name}=vswr(S{ta_num}{ta_num})')}));",
            ]
        )
    lines.extend(
        [
            "    if (db_find_instance_ex(ctx, \"term1\") != NULL)",
            "      db_update_parameters_ex(ctx, list(\"term1\", \"Num\", \"1\"));",
            "    if (db_find_instance_ex(ctx, \"term2\") != NULL)",
            "      db_update_parameters_ex(ctx, list(\"term2\", \"Num\", \"2\"));",
            "    db_update_parameters_ex(ctx, list(\"Optim1\", \"UseAllGoals\", \"yes\"));",
            "    db_update_parameters_ex(ctx, list(\"Optim1\", \"GoalName\", \"\"));",
        ]
    )
    for i, (sp_name, _vswr) in enumerate(cell.bands):
        goal = f"Goal{i}"
        band_vars = _vars_for_band(cell, i)
        other_vars = [v for v in vars_ if v not in band_vars]
        lines.append(f"    // one band at a time: {sp_name} / {goal}")
        for j, (other_sp, _other_vswr) in enumerate(cell.bands):
            on = "TRUE" if j == i else "FALSE"
            lines.append(f"    _match_goal_on({_ael_str(f'Goal{j}')}, {on});")
            lines.append(f"    _match_goal_on({_ael_str(other_sp)}, {on});")
            en = "yes" if j == i else "no"
            lines.extend(
                [
                    f"    if (db_find_instance_ex(ctx, {_ael_str(other_sp)}) != NULL)",
                    f"      db_update_parameters_ex(ctx, list({_ael_str(other_sp)}, \"EnableOptim\", {_ael_str(en)}));",
                ]
            )
        lines.append("    _match_goal_on(\"Optim1\", TRUE);")
        ds = dataset_name(cell.cell)
        lines.extend(
            [
                f"    _match_inc_name = \"{ds}_match.inc\";",
                "    _match_inc_path = strcat(de_get_open_workspace_pathname(), \"/data/\", _match_inc_name);",
            ]
        )
        for inst, param, _lo, _hi in other_vars:
            lines.append(
                f"    _match_freeze_inst(ctx, {_ael_str(inst)}, {_ael_str(param)}, {_ael_str(idx_var(inst))});"
            )
        key = f"{ds}|{sp_name}"
        seed_lines: list[str] = []
        hook_lines: list[str] = []
        for inst, param, lo, hi in band_vars:
            i_name = idx_var(inst)
            v_name = val_var(inst)
            seed_lines.extend(
                [
                    f"      _match_ensure_var(ctx, {_ael_str(i_name)}, {_ael_str(opt_expr(param, lo, hi))});",
                    f"      _match_ensure_var(ctx, {_ael_str(v_name)}, {_ael_str(library_sum_expr(param, i_name))});",
                ]
            )
            hook_lines.extend(
                [
                    f"      if (db_find_instance_ex(ctx, {_ael_str(inst)}) != NULL)",
                    f"        db_update_parameters_ex(ctx, list({_ael_str(inst)}, {_ael_str(param)}, {_ael_str(v_name)}));",
                ]
            )
        lines.extend(
            [
                "    _match_attach_inc(ctx);",
                f"    if (_match_phase_is(\"done\", {_ael_str(key)}) || (_match_phase_is(\"started\", {_ael_str(key)}) && (_match_sim_ready({_ael_str(ds)}) == TRUE)))",
                "    {",
                f'      _match_trace("analyze_done", {_ael_str(f"{ds} {sp_name}")});',
                "    }",
                f"    else if (_match_phase_is(\"started\", {_ael_str(key)}) && (_match_sim_running({_ael_str(ds)}) == TRUE) && (_match_has_eqns(ctx) == TRUE))",
                "    {",
                f'      _match_trace("wait_idle", {_ael_str(key)});',
                "      _match_50r_running = FALSE;",
                "      return;",
                "    }",
                "    else",
                "    {",
                f'      _match_trace("analyze_start", {_ael_str(f"{ds} {sp_name}")});',
                f'      if (_match_sim_failed({_ael_str(ds)}))',
                f'        _match_trace("retry_err", {_ael_str(key)});',
                "      _match_var_prms = list();",
                "      _match_write(_match_inc_path, \"\");",
            ]
        )
        lines.extend(seed_lines)
        lines.extend(
            [
                "      _match_attach_inc(ctx);",
                "      db_save_design_without_prompting(ctx);",
            ]
        )
        lines.extend(hook_lines)
        lines.extend(
            [
                f"      _match_run_sim({_ael_str(ds)});",
                f"      _match_phase_set(\"started\", {_ael_str(key)});",
                f'      _match_trace("yield_de", {_ael_str(key)});',
                "      _match_50r_running = FALSE;",
                "      return;",
                "    }",
                "    st = \"ok\";",
                "    _match_freeze_buf = \"\";",
                "    vals = \"\";",
                "    vcomma = \"\";",
            ]
        )
        for inst, param, _lo, _hi in band_vars or vars_:
            lines.extend(
                [
                    f"    decl frozen_{i}_{inst} = _match_freeze_inst(ctx, {_ael_str(inst)}, {_ael_str(param)}, {_ael_str(idx_var(inst))});",
                    f"    if (frozen_{i}_{inst} != \"\")",
                    "    {",
                    f'      vals = strcat(vals, vcomma, "\\"{inst}\\":\\"", frozen_{i}_{inst}, "\\"");',
                    '      vcomma = ",";',
                    "    }",
                ]
            )
        lines.extend(
            [
                f"    if (_match_commit_keep({_ael_str(key)}) == FALSE)",
                "    {",
                "      _match_50r_running = FALSE;",
                "      return;",
                "    }",
                f"    _match_phase_set(\"done\", {_ael_str(key)});",
            ]
        )
        lines.append("    db_save_design_without_prompting(ctx);")
        lines.append("    db_set_opendds_option(ctx, TRUE);")
        obj_head = (
            f'{{"ant":"{cell.cell}","band":"{sp_name}",'
            '"worst_vswr":null,"s11_mag":null,"status":"'
        )
        obj_mid = '","at_bound":[],"cross_ant":"","values":{'
        lines.extend(
            [
                "    json = strcat(json, comma, "
                + _ael_str(obj_head)
                + ", st, "
                + _ael_str(obj_mid)
                + ', vals, "}}");',
                '    comma = ",";',
                '    json = strcat(json, "]");',
                "    _match_write(status_path, json);",
                "    _match_shutdown_cell(ctx);",
                "    _match_50r_running = FALSE;",
                "    return;",
            ]
        )
    lines.extend(
        [
            "    // close Term/SP/Goal/Optim on this cell before the next layer",
            "    _match_shutdown_cell(ctx);",
            "    _match_finish_cell(ctx);",
            f'    _match_trace("close", {_ael_str(cell.cell)});',
        ]
    )
    lines.extend(
        [
            "  }",
            "  else",
            "  {",
            f'    _match_trace("open_fail", {_ael_str(cell.cell)});',
            f"    json = strcat(json, comma, {_ael_str(_unrun_objects(cell.cell, cell.bands))});",
            '    comma = ",";',
            "  }",
            "  }",
            "}",
        ]
    )
    return lines


def _fwd(path: str) -> str:
    return str(path).replace("\\", "/")


def render_ready_marker_ael(ready_path: str, trace_path: str | None = None) -> str:
    """Write a sidecar after fix_snp so Python knows ads.exe -m boot has returned."""
    lines = [
        f"decl _ready_fid = fopen({_ael_str(_fwd(ready_path))}, \"W\");",
        "if (_ready_fid != NULL)",
        "{",
        '  fputs(_ready_fid, "ready");',
        "  fclose(_ready_fid);",
        "}",
    ]
    if trace_path:
        lines.extend(
            [
                f"decl _tf = fopen({_ael_str(_fwd(trace_path))}, \"A\");",
                "if (_tf != NULL)",
                "{",
                '  fputs(_tf, "ready|fix_snp_done\\n");',
                "  fclose(_tf);",
                "}",
            ]
        )
    lines.append("")
    return "\n".join(lines)


def render_match_kick_ael(
    match_ael_path: str,
    workspace_path: str = "",
    first_only: bool = False,
) -> str:
    """Manual fallback for the already-open ADS. Do not launch a second ads.exe -m."""
    run = MATCH_RUN_FIRST if first_only else MATCH_RUN
    lines = [
        "// match_kick.ael -- same ADS session only; a second ads.exe has no library",
        f"load({_ael_str(_fwd(match_ael_path))});",
    ]
    if workspace_path:
        lines.append(f"de_open_workspace({_ael_str(_fwd(workspace_path))});")
    lines.append(f"{run}();")
    return "\n".join(lines) + "\n"


def render_match_ael(
    lib: str,
    cells: list[OptimCell],
    status_path: str,
    workspace_path: str = "",
) -> str:
    status_fwd = str(status_path).replace("\\", "/")
    trace_fwd = _fwd(str(Path(status_path).with_name(TRACE_NAME)))
    want_ws = _fwd(workspace_path) if workspace_path else ""
    lines = _prelude(trace_fwd)
    lines.extend(
        [
            "decl _match_50r_running = FALSE;",
            "",
            "defun _match_go(limit)",
            "{",
            "  if (_match_50r_running)",
            "    return;",
            "  _match_50r_running = TRUE;",
            f"  decl status_path = {_ael_str(status_fwd)};",
            f"  decl want_ws = {_ael_str(want_ws)};",
            '  decl json = "[";',
            '  decl comma = "";',
            "  decl ncell = 0;",
            '  _match_trace("run_start", identify_value(limit));',
            '  set_user_menu_label("AutoSI");',
            '  add_menu("Match50R", "match_50r_run", "User");',
            "  _match_armed = FALSE;",
            "  _match_var_prms = list();",
            "  de_close_window();",
            "  _match_idle();",
            "  _match_dumped = FALSE;",
            '  _match_trace("phase_cur", _match_phase_get());',
            "  decl wsnow = de_get_open_workspace_pathname();",
            '  if (((wsnow == NULL) || (wsnow == "")) && (want_ws != ""))',
            "  {",
            "    de_open_workspace(want_ws);",
            '    _match_trace("workspace_reopen", want_ws);',
            "  }",
            "  else",
            '    _match_trace("workspace", wsnow);',
            "  _match_idle();",
            "",
        ]
    )
    for cell in cells_to_render(cells):
        lines.extend(
            [
                "  ncell = ncell + 1;",
                "  if ((limit == 0) || (ncell <= limit))",
                "  {",
            ]
        )
        for raw in _cell_block(lib, cell):
            lines.append("  " + raw if raw else raw)
        lines.extend(["  }", ""])
    lines.extend(
        [
            '  json = strcat(json, "]");',
            "  _match_write(status_path, json);",
            '  _match_trace("run_done", "");',
            "  _match_50r_running = FALSE;",
            "}",
            "",
            f"defun {MATCH_RUN}()",
            "{",
            "  _match_go(0);",
            "}",
            "",
            f"defun {MATCH_RUN_FIRST}()",
            "{",
            "  _match_go(1);",
            "}",
            "// write status json",
            "// fields: ant,band,worst_vswr,s11_mag,status,at_bound,cross_ant,values",
            f"// do not call {MATCH_RUN}() or {MATCH_RUN_FIRST}() at load time",
        ]
    )
    return "\n".join(lines) + "\n"


def parse_status(text: str) -> list[BandResult]:
    data = json.loads(text)
    rows: list[BandResult] = []
    for item in data:
        missing = any(key not in item for key in _STATUS_FIELDS)
        status = item.get("status") or "unrun"
        if missing:
            status = "unrun"
        at_bound = item.get("at_bound")
        values = item.get("values")
        rows.append(
            BandResult(
                ant=str(item.get("ant") or ""),
                band=str(item.get("band") or ""),
                worst_vswr=_as_float(item.get("worst_vswr")),
                s11_mag=_as_float(item.get("s11_mag")),
                status=status,
                at_bound=list(at_bound) if isinstance(at_bound, list) else [],
                cross_ant=str(item.get("cross_ant") or ""),
                values=dict(values) if isinstance(values, dict) else {},
            )
        )
    return rows
