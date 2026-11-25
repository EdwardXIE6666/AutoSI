"""TDK / 顺络电感的型号在 PartNo、PartNumber 里，电感量在元件库零件表里。

ADS 实例的 form_name 就是型号字符串。表对不上时不编一个数。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

_GROUND = {"0", "gnd", "gnd!"}
_PIN_NET = re.compile(r"^([A-Za-z]+\d+)_(\d+)$")
_ATF_L = re.compile(r"([A-Z][A-Z0-9]{4,}),\s*L=([0-9.]+)nH")
_ITEMDEF = re.compile(
    r'create_constant_form\(\s*"([^"]+)"\s*,\s*"(\d+):[^"]*?,\s*([0-9.]+)\s*nH',
)
_NH_NUMBER = re.compile(r"\d+(?:\.\d+)?")


@dataclass(frozen=True)
class InductorCatalog:
    by_name: dict[str, str]
    by_index: dict[str, str]

    def lookup(self, form_name: str, index: str) -> str:
        form = str(form_name or "").strip().strip('"')
        if form and form.lower() != "stdform":
            hit = self.by_name.get(form)
            if hit:
                return hit
        key = str(index or "").strip().strip('"')
        if key.endswith(".0"):
            key = key[:-2]
        return self.by_index.get(key, "")


EMPTY_CATALOG = InductorCatalog({}, {})


def format_nh(number: str) -> str:
    text = str(number).strip()
    if not _NH_NUMBER.fullmatch(text):
        raise ValueError(number)
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return f"{text} nH"


def refdes_from_pin_nets(names) -> str | None:
    """两脚网上的位号。地网忽略；两脚不是同一个位号则不猜。"""
    found: list[str] = []
    for raw in names:
        text = str(raw or "").strip()
        if not text or text.lower() in _GROUND:
            continue
        match = _PIN_NET.match(text)
        if not match:
            continue
        refdes = match.group(1)
        if refdes not in found:
            found.append(refdes)
    if len(found) == 1:
        return found[0]
    return None


def oa_cell_dir(cell: str) -> str:
    """OpenAccess 在 Windows 上把单元名里的字母写成 %A 这种目录名。"""
    return "".join(f"%{ch}" if ch.isalpha() else ch for ch in cell)


def parse_atf(text: str) -> InductorCatalog:
    """TDK .atf。PartNo 是首次出现的序号，从 0 起。后半段是整表再抄一遍。"""
    by_name: dict[str, str] = {}
    order: list[str] = []
    ambiguous: set[str] = set()
    for name, number in _ATF_L.findall(text):
        if not any(ch.isdigit() for ch in name):
            continue
        try:
            shown = format_nh(number)
        except ValueError:
            continue
        if name in ambiguous:
            continue
        previous = by_name.get(name)
        if previous is None:
            by_name[name] = shown
            order.append(name)
            continue
        if previous != shown:
            ambiguous.add(name)
            del by_name[name]
    by_index = {
        str(index): by_name[name]
        for index, name in enumerate(order)
        if name in by_name
    }
    return InductorCatalog(by_name, by_index)


def parse_itemdef(text: str) -> InductorCatalog:
    """顺络 itemdef.ael。索引写在条目里，从 1 起，不按行号数。"""
    by_name: dict[str, str] = {}
    by_index: dict[str, str] = {}
    for name, index, number in _ITEMDEF.findall(text):
        try:
            shown = format_nh(number)
        except ValueError:
            continue
        by_name[name] = shown
        by_index[str(int(index))] = shown
    return InductorCatalog(by_name, by_index)


def load_cell_catalog(library_path: Path | None, cell_name: str) -> InductorCatalog:
    """只读这个单元自己的零件表。顺络在库目录里，TDK 的 .atf 在套件的 circuit/ael。"""
    if library_path is None or not cell_name:
        return EMPTY_CATALOG
    root = Path(library_path)
    itemdef = root / oa_cell_dir(cell_name) / "itemdef.ael"
    if itemdef.is_file():
        return parse_itemdef(itemdef.read_text(encoding="utf-8", errors="ignore"))
    for atf in (
        root / f"{cell_name}.atf",
        root.parent / "circuit" / "ael" / f"{cell_name}.atf",
    ):
        if atf.is_file():
            return parse_atf(atf.read_bytes().decode("latin1", errors="ignore"))
    return EMPTY_CATALOG
