"""板材库 Ansys Control/Stackup XML：读现有文件、改属性、写出导入副本。

不改层数/层序（须与 PCB 几何一致）。不覆盖板材库原件。
SIMaterials 与 Materials 电气量保持同名同步。
"""
from __future__ import annotations

import copy
import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path

CONTROL_NS = "http://www.ansys.com/control"
ET.register_namespace("c", CONTROL_NS)

_LAYER_FIRST = ("Name", "Type", "Thickness", "Material", "FillMaterial")
_LOCKED_LAYER = frozenset({"LayerID"})
_DK_DF_NAME = re.compile(r"^\d+(?:\.\d+)?-\d+(?:\.\d+)?$")
_NEW_BLANK = re.compile(r"^__new_\d+$")


def _local(tag: str) -> str:
    if tag.startswith("{") and "}" in tag:
        return tag.split("}", 1)[1]
    return tag


def _find_child(parent: ET.Element, name: str) -> ET.Element | None:
    for child in list(parent):
        if _local(child.tag) == name:
            return child
    return None


def _double_text(parent: ET.Element, prop: str) -> str:
    node = _find_child(parent, prop)
    if node is None:
        return ""
    dbl = _find_child(node, "Double")
    if dbl is not None and (dbl.text or "").strip():
        return dbl.text.strip()
    return (node.text or "").strip()


def _set_double_text(parent: ET.Element, prop: str, value: str) -> None:
    node = _find_child(parent, prop)
    if node is None:
        node = ET.SubElement(parent, prop)
    dbl = _find_child(node, "Double")
    if dbl is None:
        dbl = ET.SubElement(node, "Double")
    dbl.text = str(value)


def _fmt_elev(value: float) -> str:
    return f"{value:.6f}"


def _trim_num(value: str) -> str:
    try:
        text = f"{float(value):.6f}".rstrip("0").rstrip(".")
    except ValueError:
        return str(value)
    return text or "0"


def _as_float(value: str) -> float | None:
    text = str(value or "").strip().lower().replace("mm", "").strip()
    if not text:
        return None
    try:
        return float(text)
    except ValueError:
        return None


LAYER_TABLE_COLUMNS = (
    "Name",
    "Thickness",
    "Material",
    "FillMaterial",
    "Dielectric constant",
    "loss tangent",
)

_STEP_COLORS = {
    "mask": ("#86EFAC", "#BBF7D0", "#D1FAE5", "#DCFCE7", "#F0FDF4", "#F7FEF9"),
    "conductor": ("#FDBA74", "#FED7AA", "#FFEDD5", "#FFF1E6", "#FFF7ED", "#FFFBF7"),
    "dielectric": ("#93C5FD", "#BFDBFE", "#DBEAFE", "#E0F2FE", "#F0F9FF", "#F8FBFF"),
    "other": ("#E2E8F0", "#E8EEF4", "#EEF2F6", "#F1F5F9", "#F8FAFC", "#FCFCFD"),
}


def format_dk(value: str) -> str:
    number = _as_float(value)
    if number is None:
        return "0.0"
    text = f"{number:.3f}".rstrip("0").rstrip(".")
    if "." not in text:
        text += ".0"
    return text


def format_df(value: str) -> str:
    number = _as_float(value)
    if number is None:
        return "0.000"
    return f"{number:.3f}"


def format_layer_thickness(value: str) -> str:
    number = _as_float(value)
    if number is None:
        return str(value or "")
    return f"{number:.3f}"


def parse_layer_thickness(text: str) -> str | None:
    number = _as_float(text)
    if number is None or number < 0:
        return None
    return _trim_num(str(number))


def _is_copper_material(el: ET.Element) -> bool:
    name = (el.get("Name") or "").strip().lower()
    if name == "copper":
        return True
    return bool(_double_text(el, "Conductivity")) and not _double_text(el, "Permittivity")


@dataclass
class StackupDoc:
    path: Path
    tree: ET.ElementTree
    dirty: bool = False
    _source_key: str = ""

    @property
    def root(self) -> ET.Element:
        return self.tree.getroot()

    def _stackup(self) -> ET.Element | None:
        return _find_child(self.root, "Stackup")

    def _materials_parent(self) -> ET.Element | None:
        stack = self._stackup()
        return None if stack is None else _find_child(stack, "Materials")

    def _layers_parent(self) -> ET.Element | None:
        stack = self._stackup()
        return None if stack is None else _find_child(stack, "Layers")

    def _si_parent(self) -> ET.Element | None:
        return _find_child(self.root, "SIMaterials")

    def _huray_parent(self) -> ET.Element | None:
        return _find_child(self.root, "HurayModels")

    def material_elements(self) -> list[ET.Element]:
        parent = self._materials_parent()
        if parent is None:
            return []
        return [el for el in list(parent) if _local(el.tag) == "Material"]

    def layer_elements(self) -> list[ET.Element]:
        parent = self._layers_parent()
        if parent is None:
            return []
        return [el for el in list(parent) if _local(el.tag) == "Layer"]

    def huray_elements(self) -> list[ET.Element]:
        parent = self._huray_parent()
        if parent is None:
            return []
        return [el for el in list(parent) if _local(el.tag) == "HurayModel"]

    def editable_material_elements(self) -> list[ET.Element]:
        return [el for el in self.material_elements() if not _is_copper_material(el)]

    def material_rows(self) -> list[dict[str, str]]:
        rows = []
        for el in self.editable_material_elements():
            raw = el.get("Name", "") or ""
            row = {
                "Name": "" if _NEW_BLANK.fullmatch(raw) else raw,
                "Permittivity": _double_text(el, "Permittivity"),
                "DielectricLossTangent": _double_text(el, "DielectricLossTangent"),
            }
            rows.append(row)
        return rows

    def material_columns(self) -> list[str]:
        return ["Name", "Permittivity", "DielectricLossTangent"]

    def material_dk_df(self, name: str) -> tuple[str, str]:
        """按材料名取介电常数和损耗。copper 也查，不限于左侧可编辑名单。"""
        key = (name or "").strip()
        if not key:
            return "", ""
        for el in self.material_elements():
            if (el.get("Name") or "").strip() == key:
                return _double_text(el, "Permittivity"), _double_text(el, "DielectricLossTangent")
        parent = self._si_parent()
        if parent is None:
            return "", ""
        for el in list(parent):
            if (el.get("Name") or "").strip() == key:
                return (el.get("Permittivity") or "").strip(), (el.get("LossTangent") or "").strip()
        return "", ""

    def layer_dk_df_text(self, row: dict) -> tuple[str, str]:
        """介质层跟 Material，铜层和阻焊跟 FillMaterial。缺数时显示 0，格子不留空。"""
        source = row.get("Material") if layer_role(row) == "dielectric" else row.get("FillMaterial")
        dk, df = self.material_dk_df(str(source or ""))
        return format_dk(dk), format_df(df)

    def layer_columns(self) -> list[str]:
        seen = set()
        for el in self.layer_elements():
            seen.update(el.attrib)
        return [k for k in _LAYER_FIRST if k in seen]

    def layer_rows(self) -> list[dict[str, str]]:
        cols = self.layer_columns()
        rows = []
        for el in self.layer_elements():
            rows.append({k: el.get(k, "") for k in cols})
        return rows

    def huray_columns(self) -> list[str]:
        keys: list[str] = []
        seen: set[str] = set()
        for el in self.huray_elements():
            for key in el.attrib:
                if key not in seen:
                    seen.add(key)
                    keys.append(key)
        return keys

    def huray_rows(self) -> list[dict[str, str]]:
        cols = self.huray_columns()
        return [{k: el.get(k, "") for k in cols} for el in self.huray_elements()]

    def length_unit(self) -> str:
        parent = self._layers_parent()
        return (parent.get("LengthUnit") if parent is not None else "") or "mm"

    def fill_material_choices(self) -> list[str]:
        names = []
        seen: set[str] = set()
        for el in self.editable_material_elements():
            name = (el.get("Name") or "").strip()
            if name and name not in seen and not _NEW_BLANK.fullmatch(name):
                seen.add(name)
                names.append(name)
        return names

    def add_material(self, permittivity: str = "", loss: str = "") -> str:
        parent = self._materials_parent()
        if parent is None:
            raise ValueError("叠层 XML 没有 Materials")
        existing = {(el.get("Name") or "") for el in self.material_elements()}
        n = 1
        name = f"__new_{n}"
        while name in existing:
            n += 1
            name = f"__new_{n}"
        el = ET.SubElement(parent, "Material")
        el.set("Name", name)
        _set_double_text(el, "Permittivity", permittivity)
        _set_double_text(el, "DielectricLossTangent", loss)
        self._append_si_insulator(name, permittivity, loss)
        self.dirty = True
        return name

    def remove_material(self, index: int) -> str:
        """删掉可编辑材料（不含 copper）。层上引用清空，SIMaterials 绝缘体一并删。"""
        mats = self.editable_material_elements()
        if index < 0 or index >= len(mats):
            raise IndexError(index)
        el = mats[index]
        name = (el.get("Name") or "").strip()
        parent = self._materials_parent()
        if parent is None:
            raise ValueError("叠层 XML 没有 Materials")
        parent.remove(el)
        si = self._si_parent()
        if si is not None and name:
            for child in list(si):
                if child.get("Name") == name and _local(child.tag) == "Insulator":
                    si.remove(child)
        if name:
            for layer in self.layer_elements():
                if layer.get("Material") == name:
                    layer.set("Material", "")
                if layer.get("FillMaterial") == name:
                    layer.set("FillMaterial", "")
        self.dirty = True
        return name

    def _append_si_insulator(self, name: str, permittivity: str, loss: str) -> None:
        parent = self._si_parent()
        if parent is None:
            return
        tmpl = None
        for child in list(parent):
            if _local(child.tag) == "Insulator":
                tmpl = child
                break
        if tmpl is not None:
            ins = copy.deepcopy(tmpl)
            ins.set("Name", name)
            ins.set("Permittivity", str(permittivity))
            ins.set("LossTangent", str(loss))
            parent.append(ins)
            return
        ins = ET.SubElement(parent, "Insulator")
        ins.set("Name", name)
        ins.set("Permittivity", str(permittivity))
        ins.set("LossTangent", str(loss))

    def set_material_value(self, index: int, field: str, value: str) -> None:
        mats = self.editable_material_elements()
        if index < 0 or index >= len(mats):
            raise IndexError(index)
        el = mats[index]
        old_name = el.get("Name", "")
        value = str(value)
        if field == "Name":
            if value.strip().lower() == "copper":
                return
            if value and value != old_name:
                self._rename_material(old_name, value)
        else:
            _set_double_text(el, field, value)
            self._sync_si_material(el.get("Name", ""), field, value)
            if field in ("Permittivity", "DielectricLossTangent"):
                self._sync_dk_df_material_name(el)
        self.dirty = True

    def rename_editable_material(self, index: int, new_name: str) -> str | None:
        """改可编辑材料的名字。层上的 Material / FillMaterial 和 SI 材料名一起改。"""
        mats = self.editable_material_elements()
        if index < 0 or index >= len(mats):
            raise IndexError(index)
        new = (new_name or "").strip()
        if not new:
            return "请填写材料名"
        if new.lower() == "copper":
            return "不能改成 copper"
        el = mats[index]
        old = el.get("Name") or ""
        if new == old:
            return None
        for other in self.material_elements():
            if other is not el and (other.get("Name") or "").strip() == new:
                return "已有同名材料"
        self._rename_material(old, new)
        self.dirty = True
        return None

    def set_material_properties(self, index: int, permittivity: str, loss: str) -> None:
        """改介电常数和损耗角正切。不改材料名，层上的 Material / FillMaterial 保持原引用。"""
        mats = self.editable_material_elements()
        if index < 0 or index >= len(mats):
            raise IndexError(index)
        el = mats[index]
        name = el.get("Name", "")
        _set_double_text(el, "Permittivity", permittivity)
        _set_double_text(el, "DielectricLossTangent", loss)
        self._sync_si_material(name, "Permittivity", permittivity)
        self._sync_si_material(name, "DielectricLossTangent", loss)
        self.dirty = True

    def set_layer_value(self, index: int, field: str, value: str) -> None:
        if field in _LOCKED_LAYER:
            return
        layers = self.layer_elements()
        if index < 0 or index >= len(layers):
            raise IndexError(index)
        el = layers[index]
        el.set(field, str(value))
        if field == "Type":
            el.set("TypeSI", str(value))
        if field == "Material" and (el.get("Type") or "").lower() == "dielectric":
            el.set("FillMaterial", str(value))
        if field == "FillMaterial" and (el.get("Type") or "").lower() == "dielectric":
            el.set("Material", str(value))
        if field == "Thickness":
            self.recompute_elevations()
        self.dirty = True

    def set_huray_value(self, index: int, field: str, value: str) -> None:
        models = self.huray_elements()
        if index < 0 or index >= len(models):
            raise IndexError(index)
        models[index].set(field, str(value))
        self.dirty = True

    def _sync_dk_df_material_name(self, el: ET.Element) -> None:
        """库里介质名常是 Dk-Df（如 3.2-0.009）。改 Dk/Df 后同步改名，叠层引用跟着走。"""
        old = el.get("Name") or ""
        if not _DK_DF_NAME.fullmatch(old):
            return
        dk = _double_text(el, "Permittivity")
        df = _double_text(el, "DielectricLossTangent")
        if not dk or not df:
            return
        new = f"{_trim_num(dk)}-{_trim_num(df)}"
        if new != old:
            self._rename_material(old, new)

    def _rename_material(self, old: str, new: str) -> None:
        for el in self.material_elements():
            if el.get("Name") == old:
                el.set("Name", new)
        for el in self.layer_elements():
            if el.get("Material") == old:
                el.set("Material", new)
            if el.get("FillMaterial") == old:
                el.set("FillMaterial", new)
        parent = self._si_parent()
        if parent is not None:
            for el in list(parent):
                if el.get("Name") == old:
                    el.set("Name", new)

    def _sync_si_material(self, name: str, field: str, value: str) -> None:
        parent = self._si_parent()
        if parent is None or not name:
            return
        attr = {
            "Permittivity": "Permittivity",
            "DielectricLossTangent": "LossTangent",
            "Conductivity": "Conductivity",
            "Permeability": "Permeability",
        }.get(field)
        if not attr:
            return
        for el in list(parent):
            if el.get("Name") == name:
                el.set(attr, str(value))

    def recompute_elevations(self) -> None:
        layers = self.layer_elements()
        acc = 0.0
        for el in reversed(layers):
            el.set("Elevation", _fmt_elev(acc))
            try:
                acc += float(el.get("Thickness") or 0)
            except ValueError:
                pass

    def sanitize_for_siwave(self) -> None:
        """去掉空材料、重名空 copper、空 Double；介质层补 Material。写出/导入前调用。"""
        parent = self._materials_parent()
        if parent is not None:
            for el in list(self.material_elements()):
                if _drop_material_on_write(el):
                    parent.remove(el)
                    continue
                for prop in list(el):
                    if not _double_text(el, _local(prop.tag)):
                        el.remove(prop)
        si = self._si_parent()
        if si is not None:
            for el in list(si):
                if _drop_si_on_write(el):
                    si.remove(el)
        for el in self.layer_elements():
            if (el.get("Type") or "").lower() != "dielectric":
                continue
            if not (el.get("Material") or "").strip() and (el.get("FillMaterial") or "").strip():
                el.set("Material", el.get("FillMaterial") or "")

    def write(self, dest: Path) -> Path:
        dest = Path(dest)
        dest.parent.mkdir(parents=True, exist_ok=True)
        out = clone_stackup(self)
        out.sanitize_for_siwave()
        out.tree.write(dest, encoding="UTF-8", xml_declaration=True)
        return dest


def _drop_material_on_write(el: ET.Element) -> bool:
    name = (el.get("Name") or "").strip()
    if not name or _NEW_BLANK.fullmatch(name):
        return True
    if name.lower() == "copper" and not _double_text(el, "Conductivity"):
        return True
    return False


def _drop_si_on_write(el: ET.Element) -> bool:
    name = (el.get("Name") or "").strip()
    if not name or _NEW_BLANK.fullmatch(name):
        return True
    kind = _local(el.tag)
    if kind == "Insulator" and name.lower() == "copper":
        return True
    if kind == "Insulator" and not (el.get("Permittivity") or "").strip():
        return True
    return False


def stackup_siwave_problems(doc: StackupDoc) -> list[str]:
    """导入前检查。空列表才能喂给 ScrImportLayerStackupXML。"""
    problems: list[str] = []
    names: list[str] = []
    for el in doc.material_elements():
        name = (el.get("Name") or "").strip()
        names.append(name)
        if not name or _NEW_BLANK.fullmatch(name):
            problems.append("有未命名/未填完的材料，请删掉或填 Dk/Df")
            continue
        if name.lower() == "copper" and not _double_text(el, "Conductivity"):
            problems.append("材料 copper 被写成空绝缘体（重名），SIwave 会崩溃")
            continue
        if not _is_copper_material(el) and not _double_text(el, "Permittivity"):
            problems.append(f"材料 {name} 的 Dk 为空")
    seen: set[str] = set()
    for name in names:
        if name and name in seen:
            problems.append(f"材料名重复：{name}")
        if name:
            seen.add(name)
    si = doc._si_parent()
    if si is not None:
        for el in list(si):
            name = (el.get("Name") or "").strip()
            if _local(el.tag) == "Insulator" and name.lower() == "copper":
                problems.append("SIMaterials 里 copper 被当成绝缘体")
            if _local(el.tag) == "Insulator" and name and not (el.get("Permittivity") or "").strip():
                if not _NEW_BLANK.fullmatch(name):
                    problems.append(f"SIMaterials 绝缘体 {name} 的 Dk 为空")
    for el in doc.layer_elements():
        if (el.get("Type") or "").lower() != "dielectric":
            continue
        if not (el.get("Material") or "").strip():
            problems.append(f"介质层 {el.get('Name') or '?'} 的 Material 为空")
    out: list[str] = []
    hit: set[str] = set()
    for p in problems:
        if p not in hit:
            hit.add(p)
            out.append(p)
    return out


def copper_layer_count(label: str) -> int:
    nums = re.findall(r"\d+", str(label or ""))
    return int(nums[0]) if nums else 0


def _add_material_el(parent: ET.Element, name: str, props: dict[str, str]) -> ET.Element:
    el = ET.SubElement(parent, "Material")
    el.set("Name", name)
    for key, value in props.items():
        _set_double_text(el, key, value)
    return el


def _add_layer_el(
    parent: ET.Element,
    layer_id: int,
    name: str,
    typ: str,
    material: str,
    fill: str,
    color: str,
) -> ET.Element:
    el = ET.SubElement(parent, "Layer")
    el.set("LayerID", str(layer_id))
    el.set("Name", name)
    el.set("Type", typ)
    el.set("TypeSI", typ)
    el.set("Material", material)
    el.set("FillMaterial", fill)
    el.set("Thickness", "")
    el.set("Color", color)
    el.set("Elevation", "0.000000")
    if typ == "conductor":
        for tag in (
            "GroissSurfaceRoughness",
            "GroissBottomSurfaceRoughness",
            "GroissSideSurfaceRoughness",
        ):
            child = ET.SubElement(el, tag)
            child.set("Roughness", "0mm")
    return el


def new_blank_stackup(copper_layers: int, path: Path | None = None) -> StackupDoc:
    """库里没有对应 XML 时生成可填结构：阻焊 + N 层铜 + 层间介质。"""
    n = max(2, int(copper_layers or 0))
    root = ET.Element(f"{{{CONTROL_NS}}}Control")
    root.set("schemaVersion", "1.0")
    stack = ET.SubElement(root, "Stackup")
    stack.set("schemaVersion", "1.0")
    mats = ET.SubElement(stack, "Materials")
    _add_material_el(mats, "soldermask", {"Permittivity": "", "DielectricLossTangent": ""})
    _add_material_el(mats, "copper", {"Permeability": "0.999991", "Conductivity": "58000000"})
    layers_el = ET.SubElement(stack, "Layers")
    layers_el.set("LengthUnit", "mm")
    lid = 1
    _add_layer_el(layers_el, lid, "top_sm", "dielectric", "soldermask", "soldermask", "#DCEFE0")
    lid += 1
    _add_layer_el(layers_el, lid, "top", "conductor", "copper", "", "#F6E6C8")
    lid += 1
    for idx in range(2, n):
        _add_layer_el(layers_el, lid, f"diel_{idx - 1}", "dielectric", "", "", "#DCE8F6")
        lid += 1
        _add_layer_el(layers_el, lid, f"l{idx}", "conductor", "copper", "", "#F6E6C8")
        lid += 1
    _add_layer_el(layers_el, lid, f"diel_{n - 1}", "dielectric", "", "", "#DCE8F6")
    lid += 1
    _add_layer_el(layers_el, lid, "bottom", "conductor", "copper", "", "#F6E6C8")
    lid += 1
    _add_layer_el(layers_el, lid, "bottom_sm", "dielectric", "soldermask", "soldermask", "#DCEFE0")
    si = ET.SubElement(root, "SIMaterials")
    sm = ET.SubElement(si, "Insulator")
    sm.set("Name", "soldermask")
    sm.set("Permittivity", "")
    sm.set("LossTangent", "")
    cu = ET.SubElement(si, "Conductor")
    cu.set("Name", "copper")
    cu.set("Conductivity", "58000000")
    cu.set("Permeability", "0.999991")
    tree = ET.ElementTree(root)
    doc = StackupDoc(path=Path(path) if path else Path("blank_stackup.xml"), tree=tree, dirty=True)
    doc.recompute_elevations()
    return doc


def load_stackup_xml(path: Path | str) -> StackupDoc:
    src = Path(path)
    with src.open("r", encoding="utf-8-sig") as handle:
        tree = ET.parse(handle)
    doc = StackupDoc(path=src, tree=tree, dirty=False, _source_key=str(src.resolve()))
    if doc._stackup() is None:
        raise ValueError(f"不是 Ansys 叠层 XML（缺少 Stackup）：{src}")
    return doc


def clone_stackup(doc: StackupDoc) -> StackupDoc:
    return StackupDoc(
        path=doc.path,
        tree=copy.deepcopy(doc.tree),
        dirty=doc.dirty,
        _source_key=doc._source_key,
    )


def layer_field_locked(name: str) -> bool:
    return name in _LOCKED_LAYER


def layer_role(row: dict) -> str:
    """mask / conductor / dielectric。和行着色同一套角色，不按某一层层名开分支。"""
    typ = str(row.get("Type") or "").lower().replace("-", " ").replace("_", " ")
    name = str(row.get("Name") or "").lower()
    mat = str(row.get("Material") or "").lower()
    if "mask" in typ or "soldermask" in mat or "soldermask" in name or name.endswith("_sm"):
        return "mask"
    if typ == "conductor":
        return "conductor"
    if "dielectric" in typ or "insul" in typ:
        return "dielectric"
    return "other"


def layer_row_color(row: dict) -> str:
    """叠层行代表色：阻焊绿、铜层橙、介质蓝。只看类型和材料角色。"""
    return {
        "mask": "#DCFCE7",
        "conductor": "#FFEDD5",
        "dielectric": "#DBEAFE",
    }.get(layer_role(row), "#F8FAFC")


def layer_step_color(role: str, column: str) -> str:
    """同一行从左到右六块实色，左深右浅。"""
    colors = _STEP_COLORS.get(role, _STEP_COLORS["other"])
    try:
        index = LAYER_TABLE_COLUMNS.index(column)
    except ValueError:
        index = 0
    return colors[min(index, len(colors) - 1)]


def layer_cell_state(role: str, column: str) -> str:
    """text / edit / combo / lock / blank / auto。介质层的 FillMaterial 留空。"""
    if column == "Name":
        return "text"
    if column in ("Dielectric constant", "loss tangent"):
        return "auto"
    if column == "Thickness":
        return "edit"
    if column == "Material":
        return "combo" if role == "dielectric" else "lock"
    if column == "FillMaterial":
        return "blank" if role == "dielectric" else "combo"
    return "text"
