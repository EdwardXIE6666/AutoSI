"""Export device-to-device Pin edges from structured RF topology paths."""

from __future__ import annotations

import os
import re
import tempfile
from collections import defaultdict, deque
from pathlib import Path
from typing import Callable, Iterable, Mapping, Sequence


class PinPathEvidenceError(ValueError):
    """The topology does not contain unique Pin evidence for a safe export."""


_PASSIVE_REFDES = re.compile(r"^[RLCT]\d", re.IGNORECASE)
_PIN_TOKEN_PATTERN = r"[^.\s]+\.[^\s>]+"
_CHAIN_PATTERN = re.compile(
    rf"^{_PIN_TOKEN_PATTERN}(?:->{_PIN_TOKEN_PATTERN})+$"
)
_OUTPUT_CHAIN_PATTERN = re.compile(
    rf"^{_PIN_TOKEN_PATTERN}(?:->{_PIN_TOKEN_PATTERN})*$"
)
_CONTEXT_TOKEN_PATTERN = re.compile(r"^[^\[\]\s:]+$")
_ANT_PATTERN = re.compile(r"^ANT\d+$", re.IGNORECASE)
_LINE_PATTERN = re.compile(
    rf"^ANT\d+\s{{2,}}\|\s{{2,}}[^\s|]+\s{{2,}}\|\s{{2,}}"
    rf"{_PIN_TOKEN_PATTERN}(?:->{_PIN_TOKEN_PATTERN})*$",
    re.IGNORECASE,
)
_NATURAL_TOKEN = re.compile(r"(\d+)")


def _natural_key(value: str) -> tuple:
    return tuple(
        int(token) if token.isdigit() else token.casefold()
        for token in _NATURAL_TOKEN.split(value)
    )


def resolve_pin_role_pair(
    upstream_label: str,
    downstream_label: str,
    upstream_net: str = "",
    downstream_net: str = "",
) -> bool | None:
    """Resolve common PADS RF port-role conventions without model tables."""
    upstream = str(upstream_label or "").strip().upper()
    downstream = str(downstream_label or "").strip().upper()
    upstream_network = str(upstream_net or "").strip().upper()

    output = re.fullmatch(r"OUT(\d+)", upstream)
    switch = re.search(r"(?:^|_)SW(\d+)$", downstream)
    if output and switch:
        return output.group(1) == switch.group(1)

    rf_input = re.fullmatch(r"RFIN_([A-Z]+)", upstream)
    rf_output = re.fullmatch(r"(?:RFOUT|TRX)_([A-Z]+)\d*", downstream)
    if rf_input and rf_output:
        return rf_input.group(1) == rf_output.group(1)

    lna_output = re.fullmatch(r"LNAOUT(\d+)", upstream)
    antenna_port = re.fullmatch(r"ANT(\d+)", downstream)
    if lna_output and antenna_port:
        return lna_output.group(1) == antenna_port.group(1)
    if output and antenna_port:
        return output.group(1) == antenna_port.group(1)
    if upstream == "TX_IN" and antenna_port:
        return True
    if upstream == "CPL_OUT" and antenna_port:
        return True

    trx_antenna = re.fullmatch(r"TRX_ANT(\d+)", downstream)
    lna_switch_output = re.fullmatch(r"LNA_OUT(\d+)", upstream)
    if trx_antenna and lna_switch_output:
        return trx_antenna.group(1) == lna_switch_output.group(1)
    if trx_antenna and upstream in {"TX_IN", "CPL_OUT"}:
        return True

    rx_output = re.search(r"(?:^|_)RX_OUT(\d+)$", upstream)
    numbered_antenna = re.search(r"_ANT_(\d+)$", downstream)
    if rx_output and numbered_antenna:
        return rx_output.group(1) == numbered_antenna.group(1)

    def band_family(value: str) -> frozenset[str]:
        families = {
            "LMHB": frozenset(("LB", "MB", "HB")),
            "MHB": frozenset(("MB", "HB")),
            "LMB": frozenset(("LB", "MB")),
            "UHB": frozenset(("UHB",)),
            "LB": frozenset(("LB",)),
            "MB": frozenset(("MB",)),
            "HB": frozenset(("HB",)),
            "5G": frozenset(("5G",)),
        }
        for token in (
            "LMHB", "MHB", "LMB", "UHB", "5G", "LB", "MB", "HB"
        ):
            if token in value:
                return families[token]
        return frozenset()

    if "_ANT" in downstream:
        upstream_family = band_family(f"{upstream}_{upstream_network}")
        downstream_family = band_family(downstream)
        if upstream_family and downstream_family:
            return bool(upstream_family & downstream_family)
    rfout_mh = re.fullmatch(r"RFOUT_MH(\d+)", upstream)
    lna_input = re.match(r"^LNA(\d+)_", downstream)
    rfout_lb = re.fullmatch(r"RFOUT_LB(\d+)", upstream)
    lb_input = re.match(r"^LB(\d+)_IN", downstream)
    if upstream.startswith("RFOUT_") and (lna_input or lb_input):
        if rfout_mh and lna_input:
            return int(lna_input.group(1)) == int(rfout_mh.group(1)) + 1
        if rfout_lb and lb_input:
            return rfout_lb.group(1) == lb_input.group(1)
        return False
    return None


def resolve_path_context(
    path: Sequence[str],
    segments_by_ant: Mapping[str, Mapping[str, Sequence[str]]],
    is_network: Callable[[str], bool],
) -> tuple[str, str]:
    """Resolve a path's band branch and ANT anchor from Function 1 segments."""
    route = tuple(map(str, path))
    if len(route) < 2 or not _ANT_PATTERN.fullmatch(route[-1]):
        raise PinPathEvidenceError(f"链路缺少 ANT 末端锚点：{route}")
    ant = route[-1].upper()
    segments = segments_by_ant.get(ant, {})
    path_networks = [
        node for node in route[1:-1] if is_network(node)
    ]

    selected_segment = None
    for network in path_networks:
        memberships = {
            str(segment)
            for segment, networks in segments.items()
            if network in networks
        }
        if len(memberships) > 1:
            raise PinPathEvidenceError(
                f"{ant} 链路网络 {network} 的频段分段不唯一："
                f"{sorted(memberships, key=_natural_key)}"
            )
        if memberships:
            selected_segment = next(iter(memberships))
            break

    if selected_segment and selected_segment.upper() != "COM" \
            and not selected_segment.startswith("$$$"):
        return selected_segment, ant
    band_network = next(
        (network for network in path_networks
         if not network.startswith("$$$")),
        None,
    )
    if not band_network:
        raise PinPathEvidenceError(
            f"{ant} 链路缺少可证明的频段网络：{route}"
        )
    return band_network, ant


def _is_transceiver(refdes: str, get_part_type: Callable[[str], str]) -> bool:
    return str(get_part_type(refdes) or "").strip().casefold() == "transceiver"


def _is_lna_or_pa(refdes: str, get_part_type: Callable[[str], str]) -> bool:
    return str(get_part_type(refdes) or "").strip().casefold() in {
        "lna", "pa", "rf lna", "rf pa", "lpamif",
    }


def _is_excluded_passive(
    refdes: str, get_part_type: Callable[[str], str]
) -> bool:
    # Digit boundary is intentional: LPF* is a device role, not L + number.
    return bool(_PASSIVE_REFDES.match(refdes))


def _pin_facts(signal_infos: Iterable[Sequence]) -> dict[tuple[str, str], set[str]]:
    facts: dict[tuple[str, str], set[str]] = defaultdict(set)
    for entry in signal_infos:
        if len(entry) < 3:
            continue
        net_name, components, full_pins = entry[:3]
        for component, full_pin in zip(components, full_pins):
            text = str(full_pin or "").strip()
            _source_token, separator, pin = text.partition(".")
            # PADS can qualify a multipart symbol as ``REFDES-GATE.pin``.
            # Component ownership is the aligned signal record entry; the
            # token after the first dot is the physical Pin evidence.
            if not separator or not pin:
                continue
            facts[(str(net_name), str(component))].add(pin)
    return facts


def _unique_pin(
    facts: dict[tuple[str, str], set[str]], net_name: str, refdes: str
) -> str:
    candidates = facts.get((net_name, refdes), set())
    if not candidates:
        raise PinPathEvidenceError(
            f"缺少 Pin 事实：网络 {net_name} 上的器件 {refdes}"
        )
    if len(candidates) != 1:
        values = ", ".join(sorted(candidates, key=_natural_key))
        raise PinPathEvidenceError(
            f"多个 Pin 候选：网络 {net_name} 上的器件 {refdes} = {values}"
        )
    return next(iter(candidates))


def _normalize_route(
    route: Sequence[str],
    get_part_type: Callable[[str], str],
    is_network: Callable[[str], bool],
) -> tuple[str, ...]:
    nodes = tuple(str(node) for node in route)
    if len(nodes) < 3:
        raise PinPathEvidenceError("路径过短，无法形成器件 Pin 连接")
    transceiver_indexes = [
        index for index, node in enumerate(nodes)
        if not is_network(node) and _is_transceiver(node, get_part_type)
    ]
    ant_indexes = [
        index for index, node in enumerate(nodes)
        if is_network(node) and _ANT_PATTERN.fullmatch(node)
    ]
    if len(transceiver_indexes) != 1:
        raise PinPathEvidenceError(
            f"路径必须包含且仅包含一个 Transceiver，实际 {len(transceiver_indexes)} 个"
        )
    if len(ant_indexes) != 1:
        raise PinPathEvidenceError(
            f"路径必须包含且仅包含一个 ANT 锚点，实际 {len(ant_indexes)} 个"
        )
    transceiver_index, ant_index = transceiver_indexes[0], ant_indexes[0]
    if transceiver_index not in (0, len(nodes) - 1) or ant_index not in (
        0, len(nodes) - 1
    ):
        raise PinPathEvidenceError("Transceiver 与 ANT 必须是结构化路径两端")
    if transceiver_index > ant_index:
        nodes = tuple(reversed(nodes))
    return nodes


def build_device_pin_edges(
    paths: Iterable[Sequence[str]],
    signal_infos: Iterable[Sequence],
    get_part_type: Callable[[str], str],
    is_network: Callable[[str], bool],
) -> tuple[str, ...]:
    """Return one deterministic Pin-token row for each anchored RF path."""
    facts = _pin_facts(signal_infos)
    normalized_routes = sorted(
        {
            _normalize_route(raw_route, get_part_type, is_network)
            for raw_route in paths
        },
        key=lambda route: tuple(map(_natural_key, route)),
    )
    rows: list[str] = []
    seen_rows: set[str] = set()
    for route in normalized_routes:
        chain_tokens: list[str] = []
        retained = [
            index for index, node in enumerate(route)
            if not is_network(node)
            and not _is_excluded_passive(node, get_part_type)
        ]
        for left_index, right_index in zip(retained, retained[1:]):
            left, right = route[left_index], route[right_index]
            left_net_index, right_net_index = left_index + 1, right_index - 1
            if (
                left_net_index >= len(route)
                or right_net_index < 0
                or not is_network(route[left_net_index])
                or not is_network(route[right_net_index])
            ):
                raise PinPathEvidenceError(
                    f"器件路径侧缺少网络证据：{left} -> {right}"
                )
            left_pin = _unique_pin(facts, route[left_net_index], left)
            right_pin = _unique_pin(facts, route[right_net_index], right)
            chain_tokens.extend((f"{left}.{left_pin}", f"{right}.{right_pin}"))
        if chain_tokens:
            row = "->".join(chain_tokens)
            if not _CHAIN_PATTERN.fullmatch(row):
                raise PinPathEvidenceError(f"Pin 路径格式非法：{row}")
            if row not in seen_rows:
                seen_rows.add(row)
                rows.append(row)
    if not normalized_routes:
        raise PinPathEvidenceError("没有可导出的 Transceiver→ANT 路径")
    if not rows:
        raise PinPathEvidenceError("路径中没有可导出的器件 Pin 连接")
    return tuple(rows)


def paths_from_anchored_edges(
    ant: str,
    edges: Iterable[Sequence[str]],
    get_part_type: Callable[[str], str],
    is_network: Callable[[str], bool],
) -> tuple[tuple[str, ...], ...]:
    """Build all ANT-distance-monotonic paths, independent of input edge direction."""
    adjacency: dict[str, set[str]] = defaultdict(set)
    for edge in edges:
        if len(edge) != 2:
            raise PinPathEvidenceError(f"非法拓扑边：{edge!r}")
        left, right = map(str, edge)
        adjacency[left].add(right)
        adjacency[right].add(left)
    if ant not in adjacency:
        raise PinPathEvidenceError(f"ANT 锚点不在拓扑边中：{ant}")

    distance = {ant: 0}
    queue = deque([ant])
    while queue:
        node = queue.popleft()
        for neighbor in sorted(adjacency[node], key=_natural_key):
            if neighbor not in distance:
                distance[neighbor] = distance[node] + 1
                queue.append(neighbor)

    starts = sorted(
        (
            node for node in distance
            if not is_network(node) and _is_transceiver(node, get_part_type)
        ),
        key=_natural_key,
    )
    if not starts:
        raise PinPathEvidenceError(f"{ant} 拓扑中没有 Transceiver 锚点")

    for node in sorted(distance, key=_natural_key):
        if is_network(node):
            continue
        upstream = {
            neighbor for neighbor in adjacency[node]
            if distance.get(neighbor, -1) > distance[node]
        }
        downstream = {
            neighbor for neighbor in adjacency[node]
            if 0 <= distance.get(neighbor, -1) < distance[node]
        }
        if len(upstream) > 1 and len(downstream) > 1:
            raise PinPathEvidenceError(
                "分支 Pin 关系不唯一："
                f"{node} 同时存在 {len(upstream)} 个上游口和 "
                f"{len(downstream)} 个下游口，结构化边无法证明端口配对"
            )

    routes: list[tuple[str, ...]] = []

    def walk(node: str, path: tuple[str, ...]) -> None:
        if node == ant:
            routes.append(path)
            return
        next_nodes = sorted(
            (
                neighbor for neighbor in adjacency[node]
                if distance.get(neighbor) == distance[node] - 1
            ),
            key=_natural_key,
        )
        if not next_nodes:
            raise PinPathEvidenceError(f"路径无法单调到达 {ant}：{node}")
        for neighbor in next_nodes:
            walk(neighbor, path + (neighbor,))

    for start in starts:
        walk(start, (start,))
    return tuple(sorted(set(routes), key=lambda route: tuple(map(_natural_key, route))))


def stitch_anchored_paths(
    ant: str,
    structured_paths: Iterable[Sequence[str]],
    get_part_type: Callable[[str], str],
    is_network: Callable[[str], bool],
    port_pair_resolver: Callable[[str, str, str], bool | None] | None = None,
) -> tuple[tuple[str, ...], ...]:
    """Join path segments without losing their established branch identity."""
    segments = {
        tuple(map(str, path)) for path in structured_paths if len(path) >= 2
    }
    routes = {path for path in segments if path[-1] == ant}
    if not routes:
        raise PinPathEvidenceError(f"没有以 {ant} 结束的结构化路径")

    while True:
        incomplete = {
            route for route in routes
            if not _is_transceiver(route[0], get_part_type)
        }
        if not incomplete:
            break
        routes_by_start: dict[str, list[tuple[str, ...]]] = defaultdict(list)
        for route in incomplete:
            routes_by_start[route[0]].append(route)
        next_routes = routes - incomplete
        progressed = False
        for boundary in sorted(routes_by_start, key=_natural_key):
            tails = sorted(
                routes_by_start[boundary],
                key=lambda path: tuple(map(_natural_key, path)),
            )
            prefixes = sorted(
                (path for path in segments if path[-1] == boundary),
                key=lambda path: tuple(map(_natural_key, path)),
            )
            if not prefixes:
                raise PinPathEvidenceError(
                    f"结构化路径在 {boundary} 前缺少 Transceiver 侧前缀"
                )
            pairs = [(prefix, tail) for prefix in prefixes for tail in tails]
            if len(prefixes) > 1 or len(tails) > 1:
                if port_pair_resolver is None:
                    raise PinPathEvidenceError(
                        "分支 Pin 关系不唯一："
                        f"{boundary} 有 {len(prefixes)} 条上游路径和 "
                        f"{len(tails)} 条下游路径，但没有端口配对证据"
                    )
                resolved_pairs = []
                for tail in tails:
                    decisions = [
                        (
                            prefix,
                            port_pair_resolver(
                                boundary, prefix[-2], tail[1]
                            ),
                        )
                        for prefix in prefixes
                    ]
                    matches = [
                        prefix for prefix, decision in decisions
                        if decision is True
                    ]
                    if (
                        not matches
                        or any(decision is None for _prefix, decision in decisions)
                    ):
                        raise PinPathEvidenceError(
                            "分支 Pin 关系不唯一："
                            f"{boundary} 的下游网络 {tail[1]} "
                            "无法由 Pin 角色唯一匹配上游端口"
                        )
                    resolved_pairs.extend(
                        (matching_prefix, tail)
                        for matching_prefix in matches
                    )
                pairs = resolved_pairs
            for prefix, tail in pairs:
                    merged = prefix + tail[1:]
                    if len(merged) != len(set(merged)):
                        raise PinPathEvidenceError(
                            f"结构化路径拼接形成环路：{boundary}"
                        )
                    next_routes.add(merged)
                    progressed = True
        if not progressed:
            raise PinPathEvidenceError(f"{ant} 路径无法拼接到 Transceiver")
        routes = next_routes

    return tuple(sorted(
        (_normalize_route(route, get_part_type, is_network) for route in routes),
        key=lambda route: tuple(map(_natural_key, route)),
    ))


def write_device_pin_path(
    destination: Path | str,
    paths: Iterable[Sequence[str]],
    signal_infos: Iterable[Sequence],
    get_part_type: Callable[[str], str],
    is_network: Callable[[str], bool],
    path_context_resolver: Callable[[Sequence[str]], tuple[str, str]] | None = None,
) -> tuple[str, ...]:
    """Atomically write and verify a UTF-8 device Pin path file."""
    if path_context_resolver is None:
        raise PinPathEvidenceError("缺少链路频段和天线口证据")
    rows_with_context = set()
    for path in paths:
        normalized_path = _normalize_route(
            path, get_part_type, is_network
        )
        chains = build_device_pin_edges(
            [normalized_path], signal_infos, get_part_type, is_network
        )
        band, ant = map(str, path_context_resolver(path))
        if not _CONTEXT_TOKEN_PATTERN.fullmatch(band) or not _ANT_PATTERN.fullmatch(ant):
            raise PinPathEvidenceError(
                f"链路频段或天线口格式无效：band={band!r}, ant={ant!r}"
            )
        for chain in chains:
            tokens = chain.split("->")
            first_device = tokens[0].split(".", 1)[0]
            if _is_transceiver(first_device, get_part_type):
                tokens = tokens[1:]
            start_device = next(
                (
                    node for node in normalized_path
                    if not is_network(node)
                    and _is_lna_or_pa(node, get_part_type)
                ),
                None,
            )
            if start_device:
                start_indexes = [
                    index for index, token in enumerate(tokens)
                    if token.split(".", 1)[0] == start_device
                ]
                if start_indexes:
                    tokens = tokens[max(start_indexes):]
            output_chain = "->".join(tokens)
            if not output_chain:
                raise PinPathEvidenceError("过滤 Transceiver 后链路没有器件端点")
            if not _OUTPUT_CHAIN_PATTERN.fullmatch(output_chain):
                raise PinPathEvidenceError(
                    f"器件 Pin 链路格式无效：{output_chain}"
                )
            rows_with_context.add((ant.upper(), band, output_chain))
    ordered_rows = sorted(
        rows_with_context,
        key=lambda row: tuple(_natural_key(value) for value in row),
    )
    ant_width = max(12, *(len(ant) for ant, _band, _chain in ordered_rows))
    band_width = max(24, *(len(band) for _ant, band, _chain in ordered_rows))
    rows = tuple(
        f"{ant:<{ant_width}}  |  {band:<{band_width}}  |  {chain}"
        for ant, band, chain in ordered_rows
    )
    payload = "".join(f"{row}\n" for row in rows)
    target = Path(destination)
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary_name = ""
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            newline="\n",
            dir=target.parent,
            prefix=f".{target.name}.",
            suffix=".tmp",
            delete=False,
        ) as handle:
            temporary_name = handle.name
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        verify = Path(temporary_name).read_text(encoding="utf-8")
        if verify != payload or any(
            not _LINE_PATTERN.fullmatch(line) for line in verify.splitlines()
        ):
            raise PinPathEvidenceError("TXT 写后回读校验失败")
        os.replace(temporary_name, target)
        temporary_name = ""
    finally:
        if temporary_name:
            try:
                os.unlink(temporary_name)
            except FileNotFoundError:
                pass
    return rows


def export_device_pin_paths(
    destination: Path | str,
    *,
    structured_paths: Iterable[Sequence[str]],
    segments_by_ant: Mapping[str, Mapping[str, Sequence[str]]],
    signal_infos: Iterable[Sequence],
    get_part_type: Callable[[str], str],
    is_network: Callable[[str], bool],
    get_pin_label: Callable[[str, str], str] | None = None,
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """Export Transceiver→ANT Pin rows from Function-1 structured path segments.

    ``structured_paths`` are node sequences (TRX→FE and FE→ANT). Each ANT is
    stitched independently; an ANT that lacks unique Pin evidence is skipped
    and reported instead of aborting the remaining antennas.
    """
    paths = [tuple(map(str, path)) for path in structured_paths if path]
    ants = sorted(
        {
            path[-1].upper()
            for path in paths
            if _ANT_PATTERN.fullmatch(path[-1])
        },
        key=_natural_key,
    )
    facts = _pin_facts(signal_infos)

    def port_pair_resolver(
        device: str, upstream_net: str, downstream_net: str
    ) -> bool | None:
        if get_pin_label is None:
            return None
        try:
            upstream_pin = _unique_pin(facts, upstream_net, device)
            downstream_pin = _unique_pin(facts, downstream_net, device)
        except PinPathEvidenceError:
            return None
        return resolve_pin_role_pair(
            get_pin_label(device, upstream_pin),
            get_pin_label(device, downstream_pin),
            upstream_net,
            downstream_net,
        )

    routes: list[Sequence[str]] = []
    problems: list[str] = []
    for ant in ants:
        try:
            routes.extend(
                stitch_anchored_paths(
                    ant,
                    paths,
                    get_part_type,
                    is_network,
                    port_pair_resolver=port_pair_resolver,
                )
            )
        except PinPathEvidenceError as exc:
            problems.append(f"{ant}: {exc}")

    if not routes:
        detail = "；".join(problems)
        raise PinPathEvidenceError(
            "没有可导出的 Transceiver→ANT 路径"
            + (f"：{detail}" if detail else "")
        )

    def path_context_resolver(path: Sequence[str]) -> tuple[str, str]:
        route = _normalize_route(path, get_part_type, is_network)
        ant = route[-1].upper()
        named = [
            node for node in route[1:-1]
            if is_network(node) and not node.startswith("$$$")
        ]
        # Band comes from the path's own named net, not Function 1 segment IDs:
        # those IDs follow ANT-side DFS order and are not byte-stable across runs.
        if named:
            return named[0], ant
        return resolve_path_context(path, segments_by_ant, is_network)

    rows = write_device_pin_path(
        destination,
        routes,
        signal_infos,
        get_part_type,
        is_network,
        path_context_resolver=path_context_resolver,
    )
    return rows, tuple(problems)
