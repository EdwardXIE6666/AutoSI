# -*- coding: utf-8 -*-
"""原理图 NET_CLASS 50R 读取与缺类闸门。"""
from __future__ import annotations

from pathlib import Path


MISSING_50R_MESSAGE = (
    "原理图没有 NET_CLASS 50R（或该类下没有网络）。\n"
    "50R 是射频网络类别，功能 1 / 4 / 5 / 6 都需要。\n"
    "请在 PADS 里把射频网络归入 50R 后再导出原理图。"
)


class Missing50RNetsError(Exception):
    """原理图没有可用的 NET_CLASS 50R。"""


def extract_50r_nets_from_lines(lines) -> list[str]:
    """从 PADS 原理图文本取出 NET_CLASS 50R 下的网名。

    只认类名正好带 ``50R`` 的块，不把 ``RF_S_50ohm`` 当 50R。
    """
    in_50r = False
    nets: list[str] = []
    for line in lines:
        stripped = str(line).strip()
        if stripped.startswith("NET_CLASS") and "50R" in stripped.split():
            in_50r = True
            continue
        if in_50r and stripped == "}":
            break
        if in_50r and stripped.startswith("NET"):
            parts = stripped.split(maxsplit=1)
            if len(parts) > 1 and parts[1]:
                nets.append(parts[1])
    return nets


def decode_pads_text(raw: bytes) -> str:
    """PADS TXT 编码不固定（GBK / UTF-8 / CP1250）。ASCII 结构（NET_CLASS、*SIGNAL*）必须仍能读到。"""
    raw = raw.replace(b"\r\r\n", b"\n").replace(b"\r\n", b"\n").replace(b"\r", b"\n")
    for enc in ("gbk", "utf-8-sig", "utf-8", "cp1250", "cp1252"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("latin-1")


def extract_50r_nets_from_file(file_path) -> list[str]:
    text = decode_pads_text(Path(file_path).read_bytes())
    return extract_50r_nets_from_lines(text.split("\n"))


def require_50r_nets(nets) -> list[str]:
    found = [str(n) for n in (nets or ()) if n]
    if not found:
        raise Missing50RNetsError(MISSING_50R_MESSAGE)
    return found
