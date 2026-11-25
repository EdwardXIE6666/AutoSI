from collections import deque, defaultdict
from schematic.SchematicNetComponentProfiler import *


def parse_band(desc):
    """从滤波器 Description 文本解析干净频段名，如 'B25/B66 DRX'、'B12/B13/B14/B67'。
    解析不到返回空串（天线侧多工器/双工器等共用器件本就无单一频段）。"""
    if not desc:
        return ""
    import re
    d = str(desc).upper()
    bands = []

    def add(prefix, num):
        try:
            n = int(num)
        except Exception:
            return
        if 1 <= n <= 104:                       # 过滤 part-number 误匹配（如 B39741）
            tok = f"{prefix}{n}"
            if tok not in bands:
                bands.append(tok)

    for m in re.finditer(r'BAND\s*(\d{1,3})', d):                 # "Band41" → B41
        add('B', m.group(1))
    for m in re.finditer(r'\bB(\d{1,3})((?:/\d{1,3})*)', d):       # "B12/13/14/67"、"B25"
        add('B', m.group(1))
        for x in re.findall(r'/(\d{1,3})', m.group(2)):
            add('B', x)
    for m in re.finditer(r'\bN(\d{2,3})((?:/\d{2,3})*)', d):       # 5G "n77/78"
        add('n', m.group(1))
        for x in re.findall(r'/(\d{2,3})', m.group(2)):
            add('n', x)

    direction = ''
    for k in ('TRX', 'DRX', 'PRX', 'TX', 'RX'):
        if k in d:
            direction = k
            break
    label = '/'.join(bands)
    if direction:
        label = (label + ' ' + direction).strip()
    return label


def get_level_matrix(reverse_edges, SchematicRes, ant_num, output_dir=""):

    def find_non_ic_components(reverse_edges, schematic_res):
        """非 IC：非网络、且位号不以 T/C/R/L/LPF 开头。"""
        all_nodes = set()
        for edge in reverse_edges:
            all_nodes.add(edge[0])
            all_nodes.add(edge[1])

        non_ic_comps = []
        for node in all_nodes:
            if schematic_res.is_network(node):
                continue

            if not (node.startswith('T') or
                    node.startswith('C') or
                    node.startswith('R') or
                    node.startswith('L') or
                    node.startswith('LPF')):
                non_ic_comps.append(node)

        return sorted(non_ic_comps)

    def group_edges_for_non_ic_directed(reverse_edges, non_ic_nodes,SchematicRes, start_node="ANT1"):
        """非 IC 节点的从左到右连接组。"""
        if start_node not in non_ic_nodes:
            non_ic_nodes = [start_node]+non_ic_nodes

        forward_edges = {(v, u) for (u, v) in reverse_edges}

        available_edges = set(forward_edges)

        graph = defaultdict(list)
        # available_edges 是 set，迭代顺序受 PYTHONHASHSEED 影响；某节点有多个
        # 并联下游时邻接顺序会随进程变，矩阵列序漂移。按 ref 字符串排序消掉。
        for u, v in sorted(available_edges):
            graph[u].append(v)

        component_groups = {}

        for node in non_ic_nodes:
            if node not in graph:  # 节点没有向右的出边
                component_groups[node] = []
                continue

            # 只沿从左到右 BFS；LNA/PA/TRX/LPAMiF 允许再入（多口器件）
            queue = deque([node])
            visited = set([node])
            group_edges = []

            while queue:
                current = queue.popleft()
                for neighbor in graph.get(current, []):
                    if neighbor in visited and SchematicRes.get_part_type(neighbor) not in ['RF LNA','RF PA','Transceiver','LPAMiF']:
                        continue

                    edge = (current, neighbor)

                    if edge not in available_edges:
                        continue

                    group_edges.append(edge)
                    available_edges.remove(edge)
                    visited.add(neighbor)

                    if neighbor in non_ic_nodes:
                        continue
                    if neighbor in graph:
                        queue.append(neighbor)

            component_groups[node] = group_edges

        return component_groups, available_edges

    def merge_network_paths(component_groups, schematic_res, start_node="ANT1"):
        merged_paths = {}

        for comp_key, edges in component_groups.items():
            if not edges:
                merged_paths[comp_key] = [[comp_key]]
                continue

            graph = {}
            for u, v in edges:
                if u not in graph:
                    graph[u] = []
                graph[u].append(v)

            all_paths = []
            stack = [[comp_key]]

            while stack:
                current_path = stack.pop()
                last_node = current_path[-1]

                if last_node in graph:
                    for next_node in graph[last_node]:
                        stack.append(current_path + [next_node])
                else:
                    all_paths.append(current_path)

            # 路径里去掉网络节点，但天线口起点即使是网络也保留
            cleaned_paths = []
            for path in all_paths:
                new_path = []
                for node in path:
                    if node == start_node or not schematic_res.is_network(node):
                        new_path.append(node)

                if not new_path:
                    new_path = [start_node]

                cleaned_paths.append(new_path)

            merged_paths[comp_key] = cleaned_paths

        return merged_paths

    def extract_ic_component_graph(reverse_edges, SchematicRes, root_node='ANT1'):
        """IC 下游邻接：遇下一颗 IC 即停，天线口作特殊起点。"""
        graph = defaultdict(list)
        all_nodes = set()

        for dst, src in reverse_edges:
            graph[src].append(dst)
            all_nodes.add(src)
            all_nodes.add(dst)

        ic_components = set()
        for node in all_nodes:
            if SchematicRes.is_ic_component(node) or node == root_node:
                ic_components.add(node)

        ic_graph = defaultdict(list)

        for ic_node in ic_components:
            queue = deque([ic_node])
            visited = set([ic_node])
            downstream_ics = set()

            while queue:
                current = queue.popleft()

                for neighbor in graph.get(current, []):
                    if neighbor not in visited:
                        visited.add(neighbor)

                        if neighbor in ic_components and neighbor != ic_node:
                            downstream_ics.add(neighbor)
                        else:
                            queue.append(neighbor)

            ic_graph[ic_node] = list(downstream_ics)

        return dict(ic_graph)

    def generate_level_matrix(ic_graph, merged_groups, start_node="ANT1", level_node=False):
        def separate_leaf_and_non_leaf_nodes(ic_graph):
            leaf_nodes = []
            non_leaf_nodes = []

            for node, children in ic_graph.items():
                if not children:
                    leaf_nodes.append(node)
                else:
                    non_leaf_nodes.append(node)

            return leaf_nodes, non_leaf_nodes

        leaf_nodes, non_leaf_nodes = separate_leaf_and_non_leaf_nodes(ic_graph)
        all_nodes = set()
        for key, paths in merged_groups.items():
            all_nodes.add(key)
            for path in paths:
                for node in path:
                    all_nodes.add(node)

        total_nodes = len(all_nodes)
        matrix = [[None] * total_nodes for _ in range(total_nodes*2)]
        # 兜底：大量串联非 IC + IC 嵌套时 y/x 可能超出静态预分配，
        # 写入按需扩容并保持矩形，避免长链路 IndexError。
        _width = [total_nodes]

        def _set_cell(r, c, val):
            while len(matrix) <= r:
                matrix.append([None] * _width[0])
            if c >= _width[0]:
                new_w = c + 1
                for _row in matrix:
                    if len(_row) < new_w:
                        _row.extend([None] * (new_w - len(_row)))
                _width[0] = new_w
            matrix[r][c] = val

        def build_matrix(start_node, x, y, _chain=frozenset()):
            # 递归链防环：复杂多工器/开关拓扑下 reverse_edges 可能含环，使
            # merged_groups 出现 A↔B 互引；原仅挡直接自环(start_node != componet)
            # 挡不住互环 → 无限递归。沿当前递归链记录已访问节点，回头即截断该分支。
            # 无环图 _chain 永不命中，行为零变化。
            if start_node in _chain:
                return x
            _chain = _chain | {start_node}
            current_paths = merged_groups[start_node]
            current_paths.sort(key=len, reverse=True)
            for current_path in current_paths:
                _set_cell(x, y-1, start_node)
                raw_y = y
                for componet in current_path[1:]:
                    _set_cell(x, y, componet)
                    y += 1
                    if componet in merged_groups and start_node != componet:

                        x = build_matrix(componet, x, y, _chain)

                x += 1
                y = raw_y

            return x
        x = 0
        y = 0
        _set_cell(x, y, start_node)
        build_matrix(start_node, x, y+1)

        def trim_matrix(matrix):
            """裁到最后一个非空格，保持矩形（短行右侧补 None）。"""
            max_valid_row = 0
            max_valid_col = 0

            for i, row in enumerate(matrix):
                for j, cell in enumerate(row):
                    if cell is not None:
                        max_valid_row = max(max_valid_row, i)
                        max_valid_col = max(max_valid_col, j)

            result = []
            for i in range(max_valid_row + 1):
                if i < len(matrix):
                    row = matrix[i]
                    cropped_row = []
                    for j in range(max_valid_col + 1):
                        if j < len(row):
                            cropped_row.append(row[j])
                        else:
                            cropped_row.append(None)
                    result.append(cropped_row)

            return result

        def shift_last_node_to_right(matrix):
            """每行最右非空节点贴到该行最右列（IC 终点对齐）。"""
            if not matrix or not matrix[0]:
                return matrix

            max_cols = len(matrix[0])

            result = []
            for row in matrix:
                if len(row) < max_cols:
                    row += [None] * (max_cols - len(row))

                last_node = None
                last_index = -1

                for col in range(max_cols-1, -1, -1):
                    if row[col] is not None:
                        last_node = row[col]
                        last_index = col
                        break

                if last_node and last_index < max_cols - 1:
                    row[last_index] = None
                    row[max_cols - 1] = last_node

                result.append(row)

            return result

        matrix = trim_matrix(matrix)

        return shift_last_node_to_right(matrix)

    def insert_common_network_components(array_2d_dict, SchematicRes_raw):
        """在相邻节点之间插入同网并联位号，不改原有链路顺序。"""
        if not array_2d_dict or not SchematicRes_raw:
            return array_2d_dict

        new_array_2d = {}

        for key, path_list in array_2d_dict.items():
            new_path_list = []

            for path in path_list:
                new_path = []

                for i in range(len(path)):
                    new_path.append(path[i])

                    if i < len(path) - 1:
                        node1 = path[i]
                        node2 = path[i+1]

                        if node1 and node2:
                            common_components = SchematicRes_raw.get_common_network_components([
                                                                                               node1, node2])

                            if common_components:
                                for comp in common_components:
                                    new_path.append(comp)
                new_path_list.append(new_path)

            new_array_2d[key] = new_path_list

        return new_array_2d
    non_ic_nodes = find_non_ic_components(reverse_edges, SchematicRes)
    groups, remaining_edges = group_edges_for_non_ic_directed(
        reverse_edges, non_ic_nodes,SchematicRes, start_node=ant_num)

    merged_groups = merge_network_paths(groups, SchematicRes,ant_num)
    ic_graph = extract_ic_component_graph(reverse_edges, SchematicRes)
    merged_groups2 = insert_common_network_components(
        merged_groups, SchematicRes)


    level_matrix = generate_level_matrix(
        ic_graph, merged_groups, start_node=ant_num)

    level_matrix = insert_common_components_with_alignment_new_new(
        level_matrix, SchematicRes)
    return level_matrix


def expand_matrix_rows(matrix, top_lines=1, bottom_lines=1, fill_value=None):
    """每行上下各插空行，短行右侧补齐列宽。"""
    if not matrix:
        return []

    max_cols = max(len(row) for row in matrix) if matrix else 0

    expanded_matrix = []

    for row in matrix:
        for _ in range(top_lines):
            expanded_matrix.append([fill_value] * max_cols)

        expanded_row = row[:]
        if len(expanded_row) < max_cols:
            expanded_row.extend([fill_value] * (max_cols - len(expanded_row)))
        expanded_matrix.append(expanded_row)

        for _ in range(bottom_lines):
            expanded_matrix.append([fill_value] * max_cols)

    return expanded_matrix


def insert_common_components_with_alignment_new_new(matrix, schematic_res):
    """先按最大并联数横向留空列，再填同网位号，最后删全空列以保持列对齐。"""
    if not matrix or not schematic_res:
        return matrix
    
    max_common_count = 0
    for row in matrix:
        for col_idx in range(len(row) - 1):
            current_node = row[col_idx]
            next_node = row[col_idx + 1]
            
            if current_node and next_node:
                common_components = schematic_res.get_common_network_components(
                    [current_node, next_node]
                )
                max_common_count = max(max_common_count, len(common_components))
    
    new_matrix = []
    for row in matrix:
        new_row = []
        for col_idx, node in enumerate(row):
            new_row.append(node)
            
            if col_idx < len(row) - 1:
                for _ in range(max_common_count):
                    new_row.append(None)
        
        new_matrix.append(new_row)
    
    for row_idx, row in enumerate(new_matrix):
        col_idx = 0
        while col_idx < len(row) - 1:
            current_node = row[col_idx]
            next_node = next((x for x in row[col_idx+1:] if x is not None), None)

            if current_node and next_node:
                common_components = schematic_res.get_common_network_components(
                    [current_node, next_node]
                )
                
                for i, comp in enumerate(common_components):
                    insert_position = col_idx + 1 + i
                    if insert_position < len(row):
                        if row[insert_position] is None:
                            row[insert_position] = comp
                        else:
                            print(f"警告: 位置({row_idx}, {insert_position})已被占用")
            
            col_idx += max_common_count + 1
    
    empty_columns = []
    for col_idx in range(len(new_matrix[0])):
        is_empty = True
        for row in new_matrix:
            if row[col_idx] is not None:
                is_empty = False
                break
        if is_empty:
            empty_columns.append(col_idx)
    
    # 从后往前删空列，避免索引位移
    for col_idx in sorted(empty_columns, reverse=True):
        for row in new_matrix:
            del row[col_idx]

    return new_matrix
