"""ADS 天线口匹配：将 slot_actions 应用到 design 实例。"""

from __future__ import annotations

from ads.match_slots import SlotAction, is_optim_var, slot_actions


def _find_instance(design, name):
    find_instance = getattr(design, "find_instance", None)
    if find_instance is not None:
        return find_instance(name)

    instances = getattr(design, "instances", None)
    if instances is not None:
        for inst in instances:
            if getattr(inst, "name", None) == name:
                return inst
    return None


def apply_collapse(design, refdes: str, grounded: bool) -> list[SlotAction]:
    actions = slot_actions(refdes, grounded)
    for action in actions:
        inst = _find_instance(design, action.instance_name)
        if inst is None:
            continue
        if not action.keep:
            inst.deactivate()
        elif action.param and action.seed:
            inst.parameters[action.param].value = action.seed
    return actions


def collect_optim_vars(actions: list[SlotAction]) -> list[SlotAction]:
    return [a for a in actions if is_optim_var(a)]
