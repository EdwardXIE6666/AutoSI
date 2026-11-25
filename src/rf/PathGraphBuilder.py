from collections import defaultdict
from datetime import datetime
from graphviz import Digraph
from graphviz.backend.execute import CalledProcessError as GraphvizCalledProcessError
from rf.RfGraphModel import *
from rf.PdfPinLabel import (
    build_edge_pin_labels,
    format_pin_badge,
    pin_node_size_attrs,
)
from app.LogTool import *
import os
os.environ["PATH"] += os.pathsep + r"C:\Program Files\Graphviz\bin"


def _stderr_text(exc) -> str:
    stderr = getattr(exc, "stderr", b"") or b""
    if isinstance(stderr, bytes):
        return stderr.decode("utf-8", errors="replace")
    return str(stderr)


def _is_pdf_write_blocked(exc) -> bool:
    """占用、只读、加密盘拦截时 Graphviz 常报 Permission denied 或 Invalid argument。"""
    if isinstance(exc, PermissionError):
        return True
    if isinstance(exc, OSError) and getattr(exc, "errno", None) in (13, 22):
        return True
    blob = f"{_stderr_text(exc)} {exc}".lower()
    return any(
        token in blob
        for token in (
            "permission denied",
            "invalid argument",
            "access is denied",
            "for writing",
            "could not open",
        )
    )


def _render_dot_once(dot, output_filename: str) -> str:
    abs_name = os.path.abspath(output_filename)
    folder = os.path.dirname(abs_name)
    stem = os.path.basename(abs_name)
    if folder:
        os.makedirs(folder, exist_ok=True)
    pdf_path = abs_name if abs_name.lower().endswith(".pdf") else abs_name + ".pdf"
    try:
        if os.path.isfile(pdf_path):
            os.remove(pdf_path)
    except OSError:
        pass
    kwargs = {"view": False, "cleanup": True, "format": "pdf"}
    if folder:
        dot.render(filename=stem, directory=folder, **kwargs)
    else:
        dot.render(filename=stem, **kwargs)
    return abs_name


def _render_dot_pdf(dot, output_filename):
    """写出路径图 PDF。目标被占用/锁写时换名；再失败则跳过 PDF，不中断功能5。"""
    try:
        return _render_dot_once(dot, output_filename)
    except (GraphvizCalledProcessError, PermissionError, OSError) as exc:
        if not _is_pdf_write_blocked(exc):
            raise
        fallback_filename = (
            f"{output_filename}_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
        )
        print(
            f"{LogStyle.WARNING}⚠️ 无法覆盖路径图 {output_filename}.pdf，"
            f"可能文件正在被打开；改为保存到 {fallback_filename}.pdf{LogStyle.ENDC}"
        )
        try:
            return _render_dot_once(dot, fallback_filename)
        except (GraphvizCalledProcessError, PermissionError, OSError) as exc2:
            if not _is_pdf_write_blocked(exc2):
                raise
            print(
                f"{LogStyle.WARNING}⚠️ 路径图 PDF 仍无法写出（{output_filename}），"
                f"跳过该图，继续后续步骤。{_stderr_text(exc2).strip()}{LogStyle.ENDC}"
            )
            return output_filename

def build_and_visualize_path_graph(all_path, SchematicRes, output_filename='network_path'):
    """构建有向图并可视化。分频器每个 band 口都是合法出口，只记录不拒收。"""
    directed_graph = defaultdict(list)
    reverse_graph = defaultdict(list)
    existing_edges = set()
    directional_components = defaultdict(set)

    def _check_directional_components(path):
        for i in range(len(path)-1):
            src, dst = path[i], path[i+1]

            # 分频器/双工器是多端口器件：每个 band 口都是合法出口。
            # 旧逻辑把“首次出口”锁死，后续 band 整条路径被拒。只记录观测到的出口，不拒收。
            if SchematicRes.is_frequency_divider(src):
                if dst not in directional_components[src]:
                    directional_components[src].add(dst)
        return True
    
    def _is_path_valid(new_path):
        if not _check_directional_components(new_path):
            return False

        for i in range(len(new_path) - 1):
            src, dst = new_path[i], new_path[i+1]
            edge = (src, dst)
            
            if edge in existing_edges:
                continue
                
            if (dst, src) in existing_edges:
                return False
        return True
    
    def _add_path(new_path):
        if not _is_path_valid(new_path):
            return False
        
        added_any = False
        for i in range(len(new_path) - 1):
            src, dst = new_path[i], new_path[i+1]
            edge = (src, dst)
            
            if edge not in existing_edges:
                directed_graph[src].append(dst)
                reverse_graph[dst].append(src)
                existing_edges.add(edge)
                added_any = True
        
        return added_any
    
    if not all_path:
        print(f"{LogStyle.ERROR}❌ 错误: 输入路径列表为空{LogStyle.ENDC}")
        return None, None, None
    
    first_path = all_path[0]
    if not _add_path(first_path):
        print(f"{LogStyle.ERROR}❌ 错误: 第一条路径违反方向规则，请检查输入数据{LogStyle.ENDC}")
        return None, None, None

    for path in all_path[1:]:
        _add_path(path)

    # 可视化图形
    dot = Digraph(comment='Path Graph', engine='dot')
    dot.attr(rankdir='LR')
    dot.attr('graph', nodesep='0.65', ranksep='0.45')
    
    # 定义节点样式
    node_attrs = {
        'style': 'filled,rounded',
        'fontname': 'Arial',
        'fontsize': '10',
        'shape': 'box',
        'penwidth': '1.5'
    }

    edge_pin_labels = build_edge_pin_labels(SchematicRes, existing_edges)

    all_nodes = set().union(*[edge for edge in existing_edges])
    
    for node in all_nodes:
        # 获取节点颜色
        color = SchematicRes.determine_node_color(node)
        part_type = SchematicRes.get_part_type(node)
        
        # 分频器特殊处理
        if SchematicRes.is_frequency_divider(node):
            node_attrs.update({
                'fillcolor': color,
                'shape': 'box',
                'penwidth': '2.5'
            })
        else:
            node_attrs.update({
                'fillcolor': color,
                'shape': 'ellipse',
                'penwidth': '1.5'
            })
        for attr_name in ('width', 'height', 'margin'):
            node_attrs.pop(attr_name, None)
        node_attrs.update(pin_node_size_attrs(part_type))
        
        dot.node(node, label=node, **node_attrs)
    
    for edge in existing_edges:
        edge_attrs = {'minlen': '1', 'weight': '2'}
        if edge in edge_pin_labels:
            label_position = (
                'headlabel' if SchematicRes.is_network(edge[0])
                else 'taillabel'
            )
            edge_attrs.update({
                label_position: format_pin_badge(edge_pin_labels[edge]),
                'labeldistance': '2.4',
                'labelangle': '0',
            })
        dot.edge(edge[0], edge[1], **edge_attrs)
    
    # 右下角器件类型颜色图例
    add_legend_to_dot(dot, [
        ('#ff7500', 'Antenna (ANT)'),
        ('#a4e2c6', 'Signal Net'),
        ('#ef5b9c', 'LNA'),
        ('#d71345', 'PA'),
        ('#b22c46', 'RF Module'),
        ('#8a2be2', 'SAW Filter'),
        ('#815fcc', 'Diplexer'),
        ('#7d3f98', 'Multiplexer'),
        ('#ffa500', 'Coupler'),
        ('#20b2aa', 'Transceiver'),
        ('#9370db', 'Switch'),
        ('#da70d6', 'Other Filter'),
        ('#cccccc', 'Other'),
    ])

    rendered_filename = _render_dot_pdf(dot, output_filename)
    pdf_path = (
        rendered_filename
        if str(rendered_filename).lower().endswith(".pdf")
        else f"{rendered_filename}.pdf"
    )
    if os.path.isfile(pdf_path):
        print(f"{LogStyle.SUCCESS}✅ 路径图已保存: {pdf_path}{LogStyle.ENDC}")
    else:
        print(
            f"{LogStyle.WARNING}⚠️ 路径图未写出 PDF（{pdf_path}），"
            f"图数据仍可用于后续步骤{LogStyle.ENDC}"
        )
    
    return dict(directed_graph), existing_edges, directional_components
