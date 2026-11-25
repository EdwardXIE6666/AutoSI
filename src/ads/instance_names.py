"""ADS 实例名：同一 design 内每位号最多一套实例。"""


def snp_instance_names(node: str, num_files: int) -> list[str]:
    """与 process_ic_component 落盘名一致：单文件用位号，多状态文件加 _1/_2。"""
    if num_files <= 0:
        return []
    if num_files == 1:
        return [node]
    return [f"{node}_{i + 1}" for i in range(num_files)]


def snp_display_and_wire_names(names, wire_ports=None):
    """SnP 控件 Display 名与飘线网标分开。

    Display 名就是 ADS Pcell 的 term 名，必须每口一个、非空、不能是 ``-``，
    也不能重名（否则 db_create_pin already has a term）。口数跟名单长度走。
    飘线网标只给通路口；非通路登记空串，修脚宏跳过。
    """
    raw = [str(n or "").split(" ")[0].strip() for n in (names or [])]
    display = []
    seen = set()
    for i, n in enumerate(raw):
        d = n
        if (not d) or d == "-" or d in seen:
            d = f"P{i + 1}"
            extra = 2
            while d in seen:
                d = f"P{i + 1}_{extra}"
                extra += 1
        display.append(d)
        seen.add(d)
    if wire_ports is None:
        wires = list(raw)
    else:
        allowed = set(wire_ports)
        wires = [n if n in allowed else "" for n in raw]
    return display, wires


def snp_pin_wire_label(term_name, allowed_names):
    """SnP 一只脚的飘线网标：跟盒子上的 term 名走，且必须在通路名单里。

    禁止按 term 号/遍历下标去名单里取——Channel 布局下会把 U1501_1 贴到 U1501_14 上。
    """
    nm = str(term_name or "").strip()
    if not nm or nm == "-":
        return ""
    ok = {str(n).strip() for n in (allowed_names or ()) if str(n).strip()}
    return nm if nm in ok else ""


def ge_instance_names(node: str, grounded: bool) -> list[str]:
    """与 process_ge_component 落盘名一致：接地 C+L，串联 C+L+R。"""
    if grounded:
        return [f"{node}_C", f"{node}_L"]
    return [f"{node}_C", f"{node}_L", f"{node}_R"]


def claim_refdes(placed: set, node: str) -> bool:
    """同一 design 内每个物理位号只认领一次。已放过则 False。"""
    if node in placed:
        return False
    placed.add(node)
    return True
