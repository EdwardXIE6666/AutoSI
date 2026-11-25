"""ADS 天线口匹配：位号分类与 C/L/R 实例保留规则。"""

from __future__ import annotations

from dataclasses import dataclass

from ads.instance_names import ge_instance_names

C_SEED = "1 pF"
L_SEED = "1 nH"
L_VALUES = (
    "0.6 nH",
    "0.8 nH",
    "1 nH",
    "1.2 nH",
    "1.5 nH",
    "1.8 nH",
    "2 nH",
    "2.2 nH",
    "2.4 nH",
    "2.7 nH",
    "3 nH",
    "3.3 nH",
    "3.6 nH",
    "3.9 nH",
    "4.3 nH",
    "4.7 nH",
    "5.1 nH",
    "5.6 nH",
    "6.2 nH",
    "6.8 nH",
    "7.5 nH",
    "8.2 nH",
    "9.1 nH",
    "10 nH",
    "12 nH",
    "15 nH",
    "18 nH",
    "22 nH",
    "27 nH",
    "47 nH",
    "56 nH",
    "82 nH",
    "100 nH",
)
C_VALUES = (
    "0.2 pF",
    "0.3 pF",
    "0.5 pF",
    "0.7 pF",
    "0.8 pF",
    "1 pF",
    "1.2 pF",
    "1.5 pF",
    "1.8 pF",
    "2 pF",
    "2.2 pF",
    "2.4 pF",
    "2.7 pF",
    "3 pF",
    "3.3 pF",
    "3.6 pF",
    "3.9 pF",
    "4.7 pF",
    "5.6 pF",
    "6.8 pF",
    "7.5 pF",
    "8.2 pF",
    "10 pF",
    "12 pF",
    "15 pF",
    "18 pF",
    "22 pF",
    "27 pF",
    "33 pF",
    "39 pF",
    "47 pF",
    "100 pF",
    "1 nF",
    "10 nF",
    "0.1 uF",
)
C_LO, C_HI = C_VALUES[0], C_VALUES[-1]
L_LO, L_HI = L_VALUES[0], L_VALUES[-1]


@dataclass(frozen=True)
class SlotAction:
    instance_name: str
    keep: bool
    param: str | None
    seed: str | None
    lo: str | None
    hi: str | None


def slot_kind(refdes: str) -> str:
    s = refdes.strip()
    if s.startswith("LPF"):
        return ""
    if s and s[0] in "CLR":
        return s[0]
    return ""


def _param_from_suffix(instance_name: str) -> str | None:
    if instance_name.endswith("_C"):
        return "C"
    if instance_name.endswith("_L"):
        return "L"
    if instance_name.endswith("_R"):
        return "R"
    return None


def slot_actions(refdes: str, grounded: bool) -> list[SlotAction]:
    kind = slot_kind(refdes)
    if not kind:
        return []

    actions: list[SlotAction] = []
    for name in ge_instance_names(refdes, grounded):
        param = _param_from_suffix(name)
        keep = param == kind

        seed: str | None = None
        lo: str | None = None
        hi: str | None = None
        if keep:
            if kind == "C":
                seed, lo, hi = C_SEED, C_LO, C_HI
            elif kind == "L":
                seed, lo, hi = L_SEED, L_LO, L_HI
            elif kind == "R":
                seed = "0"

        actions.append(
            SlotAction(
                instance_name=name,
                keep=keep,
                param=param,
                seed=seed,
                lo=lo,
                hi=hi,
            )
        )
    return actions


def is_optim_var(action: SlotAction) -> bool:
    return action.keep and action.param in ("C", "L")
