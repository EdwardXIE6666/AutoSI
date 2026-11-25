from html import escape
import re


_RLCT_REFDES = re.compile(r"^[RLCT]\d", re.IGNORECASE)


def _pin_number(full_pin):
    """Return the pin token from values such as ``U100.3``."""
    text = str(full_pin or "")
    return text.split(".", 1)[1] if "." in text else text


def build_edge_pin_labels(schematic, displayed_edges):
    """Return the pin label belonging to each displayed arrow.

    ``displayed_edges`` contains ``(source, target)`` pairs in the direction
    shown by the arrow.  Pin facts come only from the schematic net records.
    """
    pins_by_net = {}
    for entry in schematic.signal_infos:
        if len(entry) < 3:
            continue
        net_name, components, pins = entry[:3]
        pins_by_net[net_name] = {
            component: _pin_number(pin)
            for component, pin in zip(components, pins)
        }

    labels = {}
    for source, target in displayed_edges:
        source_is_net = schematic.is_network(source)
        target_is_net = schematic.is_network(target)
        if source_is_net == target_is_net:
            continue

        if source_is_net:
            if _RLCT_REFDES.match(str(target)):
                continue
            pin = pins_by_net.get(source, {}).get(target)
            if pin:
                labels[(source, target)] = pin
        else:
            if _RLCT_REFDES.match(str(source)):
                continue
            pin = pins_by_net.get(target, {}).get(source)
            if pin:
                labels[(source, target)] = pin

    return labels


def format_pin_badge(pin):
    """Return a high-contrast Graphviz HTML label for a pin number."""
    text = escape(str(pin))
    return (
        '<<TABLE BORDER="1" CELLBORDER="0" CELLSPACING="0" '
        'CELLPADDING="3" COLOR="#9C2F00" BGCOLOR="#FFF2A8">'
        f'<TR><TD><FONT FACE="Arial" POINT-SIZE="13" COLOR="#7A1F00">'
        f'<B>{text}</B></FONT></TD></TR></TABLE>>'
    )


def pin_node_size_attrs(part_type):
    """Give dense multi-port device classes more perimeter for edge landing."""
    dense_multiport_types = {
        "Transceiver",
        "RF LNA",
        "RF PA",
        "LPAMiF",
        "Electronic Switch",
    }
    if part_type in dense_multiport_types:
        return {
            "width": "2.2",
            "height": "1.6",
            "margin": "0.14,0.10",
        }
    return {}
