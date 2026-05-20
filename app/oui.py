"""Tiny built-in OUI -> vendor lookup. Covers the most common consumer
brands so the UI shows something useful next to unfamiliar devices.
Extend at will; the full IEEE OUI file is too large to bundle.
"""

OUI = {
    "001A11": "Google", "001CCC": "Google", "3C5AB4": "Google", "F4F5D8": "Google", "F4F5E8": "Google",
    "F0EF86": "Google", "94EB2C": "Google", "9CA32A": "Amazon", "F0272D": "Amazon", "F0D2F1": "Amazon",
    "44650D": "Amazon", "FC65DE": "Amazon", "8871E5": "Amazon", "FCFFAA": "Amazon",
    "001D4F": "Apple", "0050E4": "Apple", "001124": "Apple", "001451": "Apple", "001B63": "Apple",
    "0023DF": "Apple", "0026B0": "Apple", "0026BB": "Apple", "002608": "Apple", "F0DBE2": "Apple",
    "F0F61C": "Apple", "F4F15A": "Apple", "BC52B7": "Apple", "AC87A3": "Apple", "A4B197": "Apple",
    "001E52": "Apple", "001F5B": "Apple", "002241": "Apple", "002332": "Apple", "002436": "Apple",
    "0024A5": "Apple", "F8FFC2": "Apple",
    "001DC9": "Microsoft", "28182A": "Microsoft", "5404A6": "Microsoft", "60451C": "Microsoft",
    "7C1E52": "Microsoft", "98293F": "Microsoft", "002608": "Microsoft", "C03F0E": "Microsoft",
    "001E8C": "ASUSTek", "001FC6": "ASUSTek", "002354": "ASUSTek", "0024D2": "Belkin",
    "0026F2": "Netgear", "001A5F": "Netgear", "201E88": "Netgear", "204E7F": "Netgear",
    "001D7E": "Cisco-Linksys", "001E58": "Cisco-Linksys", "00226B": "Cisco-Linksys",
    "001C10": "Cisco-Linksys", "001A70": "Cisco-Linksys", "001839": "Cisco-Linksys",
    "00179A": "D-Link", "001B11": "D-Link", "001CF0": "D-Link", "002191": "D-Link",
    "002401": "D-Link", "00265A": "D-Link", "00146C": "Netgear",
    "001E8F": "Canon", "00266C": "Canon", "001CCC": "Brother",
    "001A6B": "DrayTek", "001DAA": "DrayTek", "5896C7": "DrayTek",
    "B827EB": "Raspberry Pi Foundation", "DCA632": "Raspberry Pi Trading",
    "E45F01": "Raspberry Pi", "D83ADD": "Raspberry Pi", "2CCF67": "Raspberry Pi",
    "001999": "Samsung", "001AB6": "Samsung", "001CC0": "Samsung", "001D25": "Samsung",
    "0021D1": "Samsung", "002339": "Samsung", "AC5F3E": "Samsung", "78F882": "Samsung",
    "001E10": "Sony", "002470": "Sony", "00256B": "Sony", "FCF152": "Sony",
    "F8A2D6": "LG Electronics", "001F6B": "LG Electronics", "002419": "LG Electronics",
    "001125": "IBM", "001A64": "IBM", "00059A": "Cisco", "0007EB": "Cisco", "000ED7": "Cisco",
    "00C0CA": "Alfa Network", "8C1F64": "IEEE Registration Authority",
    "001C42": "Parallels",
    "525400": "QEMU/KVM", "020000": "Locally administered",
    "001E37": "ecobee", "F0EE10": "Ring", "0CC4A6": "Wyze", "B0B98A": "Sonos", "C8DB2A": "TP-Link",
    "F4F26D": "TP-Link", "5C628B": "TP-Link", "AC84C6": "TP-Link", "F0A731": "TP-Link",
    "B0BE76": "TP-Link", "1027F5": "TP-Link", "98DAC4": "TP-Link",
    "001583": "Roku", "AC3A7A": "Roku", "0CDB46": "Roku",
    "9C8D7C": "Hewlett-Packard", "B499BA": "Hewlett-Packard", "002655": "Hewlett-Packard",
    "001E0B": "Hewlett-Packard", "001B78": "Hewlett-Packard",
}


def lookup(mac: str) -> str | None:
    if not mac:
        return None
    prefix = mac.upper().replace(":", "").replace("-", "")[:6]
    return OUI.get(prefix)
