#!/usr/bin/env python3
import json
import re
import struct
import sys
from collections import Counter
from pathlib import Path

from map_data import MAP_DISPLAY_NAMES


# =======================================================================
# Relic Chunky tree walker
# =======================================================================

class Chunk:
    __slots__ = ("type", "id", "version", "size", "name", "payload",
                 "children", "header_offset", "data_offset")

    def __init__(self):
        self.children = []
        self.payload = None


def parse_chunky(data: bytes) -> Chunk:
    root = Chunk()
    root.type, root.id, root.name = "ROOT", "", ""
    _parse_children(data, 0, len(data), root)
    return root


def _parse_children(data, pos, end, parent):
    while pos < end:
        start = pos
        if end - pos < 20:
            return pos
        type_ = data[pos:pos + 4]
        if type_ not in (b"FOLD", b"DATA"):
            pos = start + 1
            continue
        id_ = data[pos + 4:pos + 8].decode("ascii", "replace")
        version, size, namelen = struct.unpack_from("<iii", data, pos + 8)
        if namelen < 0 or namelen > 1024:
            pos = start + 1
            continue
        name_start = pos + 20
        if name_start + namelen > len(data):
            return pos
        data_offset = name_start + namelen
        payload_end = data_offset + size
        if payload_end > end or size < 0:
            pos = start + 1
            continue
        node = Chunk()
        node.type = type_.decode("ascii")
        node.id = id_
        node.version = version
        node.size = size
        node.name = data[name_start:data_offset].rstrip(b"\x00").decode("ascii", "replace")
        node.header_offset = start
        node.data_offset = data_offset
        if node.type == "FOLD":
            _parse_children(data, data_offset, payload_end, node)
        else:
            node.payload = data[data_offset:payload_end]
        parent.children.append(node)
        pos = payload_end
    return pos


def find_all(node, type_, id_):
    out = []
    def rec(n):
        if n.type == type_ and n.id == id_:
            out.append(n)
        for c in n.children:
            rec(c)
    rec(node)
    return out


def find_child(node, type_, id_):
    for c in node.children:
        if c.type == type_ and c.id == id_:
            return c
    return None


def find_deep(node, type_, id_):
    if node.type == type_ and node.id == id_:
        return node
    for c in node.children:
        r = find_deep(c, type_, id_)
        if r:
            return r
    return None


# =======================================================================
# Anchor-based payload scanners
# =======================================================================

def read_pascal_utf16(payload, offset):
    if offset + 4 > len(payload):
        return None, offset
    n = struct.unpack_from("<i", payload, offset)[0]
    if n < 0 or n > 512:
        return None, offset
    start, end = offset + 4, offset + 4 + n * 2
    if end > len(payload):
        return None, offset
    try:
        return payload[start:end].decode("utf-16-le").rstrip("\x00"), end
    except UnicodeDecodeError:
        return None, offset


def scan_next_pascal_utf16(payload, start, lookahead=48, min_len=1, max_len=64):
    s, end, cand, pad, enc = scan_next_pascal_string(
        payload, start, lookahead, min_len, max_len, encodings=("utf16",))
    return s, end, cand, pad


def scan_next_pascal_string(payload, start, lookahead=48, min_len=1, max_len=64,
                             encodings=("ascii", "utf16")):
    limit = min(start + lookahead, len(payload) - 4)
    for cand in range(start, limit):
        n = struct.unpack_from("<i", payload, cand)[0]
        if not (min_len <= n <= max_len):
            continue
        if "ascii" in encodings:
            a_end = cand + 4 + n
            if a_end <= len(payload):
                achunk = payload[cand + 4:a_end]
                if achunk and all(0x20 <= b <= 0x7E for b in achunk):
                    return achunk.decode("ascii"), a_end, cand, cand - start, "ascii"
        if "utf16" in encodings:
            u_end = cand + 4 + n * 2
            if u_end <= len(payload):
                uchunk = payload[cand + 4:u_end]
                if uchunk and all(
                    uchunk[i + 1] == 0 and 0x20 <= uchunk[i] <= 0x7E
                    for i in range(0, len(uchunk), 2)
                ):
                    try:
                        s = uchunk.decode("utf-16-le")
                    except UnicodeDecodeError:
                        s = None
                    if s and s.strip():
                        return s, u_end, cand, cand - start, "utf16"
    return None, start, None, None, None


def read_pascal_ascii_at(payload, content_offset):
    if content_offset < 4:
        return None
    length = struct.unpack_from("<i", payload, content_offset - 4)[0]
    if 0 < length < 256 and content_offset + length <= len(payload):
        return payload[content_offset:content_offset + length].decode("ascii", "replace")
    return None


# =======================================================================
# Field extractors: map, lobby settings, players, chat
# =======================================================================

def extract_map_info(root):
    sdsc = find_deep(root, "DATA", "SDSC")
    if not sdsc:
        return {"confirmed": False}
    p = sdsc.payload
    out = {"confirmed": True, "chunk_version": sdsc.version}

    idx = p.find(b"W40k")
    if idx != -1:
        out["mod_tag"] = "W40k"
        version_tag, _, _, _, _ = scan_next_pascal_string(p, idx + 4)
        out["game_version_tag"] = version_tag

    idx2 = p.find(b"DATA:")
    if idx2 != -1:
        path = read_pascal_ascii_at(p, idx2)
        if path:
            out["map_path"] = path
            out["map_name"] = path.split("\\")[-1]
            out["map_display_name"] = MAP_DISPLAY_NAMES.get(out["map_name"])
            crc_off = idx2 + len(path)
            if crc_off + 4 <= len(p):
                out["map_crc_candidate"] = hex(struct.unpack_from("<I", p, crc_off)[0])
                out["map_crc_candidate_confirmed"] = False
    return out


GAME_OPTION_TAGS = {
    "FDIA": "aidifficulty", "TSSR": "startingresources", "MTKL": "lockteams",
    "AEHC": "enablecheats", "COLS": "startinglocations", "DPSG": "gamespeed",
    "HSSR": "resourcesharing", "TRSR": "resourcerate", "YLFN": "disableflyers",
}


def extract_lobby_settings(root):
    db = find_deep(root, "DATA", "BASE")
    if not db:
        return {"confirmed": False}
    payload = db.payload
    settings = {}
    for tag, name in GAME_OPTION_TAGS.items():
        tag_offset = payload.find(tag.encode("ascii"))
        if tag_offset == -1:
            continue
        value_offset = tag_offset - 4
        if 0 <= value_offset < len(payload):
            settings[name] = payload[value_offset]

    ylfn_tag_offset = payload.find(b"YLFN")
    trailing_name = None
    if ylfn_tag_offset != -1:
        trailing_name, _, _, _ = scan_next_pascal_utf16(payload, ylfn_tag_offset + 4)

    return {
        "settings": settings,
        "settings_confirmed": True,
        "settings_note": ("Tag names/positions confirmed against dowde-replay-parser. "
                           "Numeric value -> human label needs that project's "
                           "gameOptions.json lookup table, not yet checked."),
        "trailing_name_field": trailing_name,
        "trailing_name_field_confirmed": False,
    }


def extract_players(root, out_dir):
    players = []
    for gi, gply in enumerate(find_all(root, "FOLD", "GPLY")):
        p = {}
        info_node = find_child(gply, "DATA", "INFO")
        if info_node:
            payload = info_node.payload
            name, off = read_pascal_utf16(payload, 0)
            race, off2, _, padding, _ = scan_next_pascal_string(payload, off)
            p["name"] = name
            p["race"] = race
            if padding:
                p["_race_padding_bytes_skipped"] = padding

            idx = off2
            while idx < len(payload) and payload[idx] == 0:
                idx += 1
            pid = struct.unpack_from("<I", payload, idx)[0] if idx + 4 <= len(payload) else None
            p["candidate_player_id"] = pid
            p["candidate_player_id_confirmed"] = False

        tcuc = find_child(gply, "FOLD", "TCUC")
        if tcuc:
            uncu = find_child(tcuc, "DATA", "UNCU")
            if uncu:
                cname, off = read_pascal_utf16(uncu.payload, 0)
                p["commander_skin_name"] = cname
                p["commander_skin_name_confirmed"] = False
                colours, cp = [], uncu.payload[off:]
                for i in range(0, len(cp) - 3, 4):
                    colours.append(list(cp[i:i + 4]))
                p["custom_colours_rgba"] = colours[:6]
                p["custom_colours_confirmed"] = False

        for fold_id, key in (("TCBD", "portrait"), ("TCBN", "banner")):
            fold = find_child(gply, "FOLD", fold_id)
            if not fold:
                continue
            img = find_deep(fold, "FOLD", "IMAG")
            if not img:
                continue
            attr = find_child(img, "DATA", "ATTR")
            imgdata = find_child(img, "DATA", "DATA")
            if not attr or not imgdata or len(attr.payload) < 16:
                continue
            fmt, w, h, mips = struct.unpack_from("<iiii", attr.payload, 0)
            entry = {"width": w, "height": h, "confirmed": False}
            if w * h * 4 == len(imgdata.payload):
                try:
                    from PIL import Image
                    out_path = out_dir / f"player{gi}_{key}.png"
                    Image.frombytes("RGBA", (w, h), imgdata.payload).save(out_path)
                    entry["saved_as"] = str(out_path)
                    entry["confirmed"] = True
                except Exception as e:
                    entry["error"] = str(e)
            p[key] = entry

        players.append(p)
    return players


def _safe_u8(buf, off):
    return buf[off] if 0 <= off < len(buf) else -1


def _safe_u16le(buf, off):
    if 0 <= off and off + 2 <= len(buf):
        return struct.unpack_from("<H", buf, off)[0]
    return -1


def _is_potential_chat_chunk(buf, offset):
    if offset + 17 > len(buf):
        return False
    chunk_length = _safe_u16le(buf, offset)
    if not chunk_length or chunk_length < 20 or chunk_length > 10000:
        return False
    if _safe_u8(buf, offset + 4) != 1:
        return False
    name_length = _safe_u8(buf, offset + 13)
    if not name_length or name_length > 200:
        return False
    if chunk_length < name_length * 2 + 20:
        return False
    return offset + 17 + name_length * 2 <= len(buf)


def _read_chat_chunk(buf, offset, known_players, shared_timestamp=None):
    chunk_length = _safe_u16le(buf, offset)
    if not chunk_length or chunk_length < 20 or chunk_length > 100000:
        return None
    offset += 4
    if _safe_u8(buf, offset) != 1:
        return None
    offset += 9
    name_length = _safe_u8(buf, offset)
    if not name_length or name_length > 200:
        return None
    offset += 4
    if offset + name_length * 2 > len(buf):
        return None
    try:
        alias = buf[offset:offset + name_length * 2].decode("utf-16-le")
    except UnicodeDecodeError:
        return None
    if known_players and alias not in known_players:
        return None
    offset += name_length * 2
    if not _safe_u8(buf, offset):
        return None
    offset += 8
    message_type = _safe_u8(buf, offset)
    if message_type == -1:
        return None
    offset += 4
    msg_length = _safe_u16le(buf, offset)
    if not msg_length or msg_length > 10000:
        return None
    offset += 4
    if offset + msg_length * 2 > len(buf):
        return None
    try:
        message = buf[offset:offset + msg_length * 2].decode("utf-16-le")
    except UnicodeDecodeError:
        return None
    if not message:
        return None
    offset += msg_length * 2

    timestamp = shared_timestamp
    if timestamp is None:
        for i in range(offset, min(len(buf) - 3, offset + 400)):
            if buf[i] == 0x50:
                ticks = _safe_u16le(buf, i + 1)
                if ticks and ticks > 0:
                    timestamp = ticks / 8.0
                    break

    return {"alias": alias, "message": message, "message_type": message_type,
            "timestamp_seconds": timestamp, "_next_offset": offset}


def extract_chat_messages(data, cmd_stream_start, player_names):
    known_players = {n for n in player_names if n}
    buf = data[cmd_stream_start:]
    messages = []
    search_offset = 0
    while search_offset < len(buf) - 20:
        if _is_potential_chat_chunk(buf, search_offset):
            result = _read_chat_chunk(buf, search_offset, known_players)
            if result:
                current_ts = result["timestamp_seconds"]
                next_offset = result.pop("_next_offset")
                messages.append(result)
                while next_offset < len(buf) - 20:
                    if _is_potential_chat_chunk(buf, next_offset):
                        nr = _read_chat_chunk(buf, next_offset, known_players, current_ts)
                        if nr:
                            next_offset = nr.pop("_next_offset")
                            messages.append(nr)
                            continue
                    break
                search_offset = next_offset
                continue
        search_offset += 1
    return messages


# =======================================================================
# Command stream parser
# =======================================================================

CMD_BUILD_POWER_GENERATOR = 0x0000C350
CMD_PLACE_BLUEPRINT = 0x0000C351
CMD_DEMOLISH_BUILDING = 0x0000C352
CMD_SQUAD_APPEAR = 0x000010B7
CMD_SQUAD_ORDER = 0x000010B5

CMD_REGISTRY = {
    CMD_BUILD_POWER_GENERATOR: "BUILD_OR_PRODUCE",
    0x0000C35A: "BUILD_STRUCTURE_B",
    CMD_PLACE_BLUEPRINT: "PLACE_LP_DEFENSE",
    CMD_DEMOLISH_BUILDING: "CAPTURE_LP",
    0x0000C35D: "TECH_BUILDING_A",
    0x0000C359: "UNIT_ORDER_A",
    0x0000C35B: "BUILD_TECH_B",
    0x0000C35C: "UNIT_ORDER_C",
    0x0000C35F: "BUILD_DEFENSE_B",
    0x0000C361: "UNIT_ORDER_E",
    0x0000C354: "REINFORCE_SQUAD_A",
    0x0000C356: "REINFORCE_SQUAD_B",
    0x000024AC: "UNIT_DEPLOY",
    0x000024AF: "UNIT_TRAIN_ORDER",
    0x000024AA: "BUILDING_CMD_AA",
    0x000024AE: "BUILDING_UPGRADE",
    0x000024E7: "BUILDING_CMD_E7",
    0x000024BD: "BUILDING_CMD_BD",
    0x000024C4: "BUILDING_CMD_C4",
    0x0000C353: "UNIT_MOVE",
    0x0000C355: "ABILITY_A",
    0x0000C357: "UNIT_ATTACK",
    0x0000C358: "ABILITY_B",
    0x0000C360: "ABILITY_C",
    0x0000C35E: "UNIT_SPECIAL_5",
    0x000003E9: "SPECIAL_ABILITY",
    0x00000736: "ORK_ORDER_A", 0x00000738: "ORK_ORDER_B", 0x0000073A: "ORK_ORDER_C",
    0x0000073B: "ORK_ORDER_D", 0x000007D4: "ORK_ORDER_E", 0x00000783: "ORK_ORDER_F",
    0x00000787: "ORK_ORDER_G", 0x0000079B: "ORK_ORDER_H", 0x000007C9: "ORK_ORDER_I",
    0x000007D7: "ORK_ORDER_J", 0x000007F8: "ORK_ORDER_K", 0x0000077D: "ORK_ORDER_L",
    0x00000809: "ORK_ORDER_M", 0x00000531: "ORK_ORDER_N",
    0x0000C362: "BUILD_OR_UNIT_A", 0x0000C363: "BUILD_OR_UNIT_B",
    0x0000C364: "BUILD_OR_UNIT_C", 0x0000C365: "BUILD_OR_UNIT_D",
    0x0000C368: "ORK_ABILITY_A", 0x0000C369: "ORK_ABILITY_B",
    0x0000C36A: "ORK_ABILITY_C", 0x0000C36B: "ORK_ABILITY_D",
    CMD_SQUAD_APPEAR: "SQUAD_APPEAR", CMD_SQUAD_ORDER: "SQUAD_ORDER",
}

_ANCHOR_A = {0x0000C355, 0x0000C358, 0x0000C35D}
_ANCHOR_B_ORK = {
    0x00000736, 0x00000738, 0x0000073A, 0x0000073B, 0x000007D4,
    0x0000C362, 0x0000C363, 0x0000C364, 0x0000C365,
    0x0000C368, 0x0000C369, 0x0000C36A, 0x0000C36B,
}
_ANCHOR_B_DE = {0x0000C35A, 0x0000C35F, 0x0000C360, 0x0000C361}

CATEGORY_MAP = {
    "BUILD_STRUCTURE_B": "construction", "BUILD_TECH_B": "construction",
    "BUILD_DEFENSE_B": "construction", "BUILD_OR_PRODUCE": "construction",
    "BUILD_OR_UNIT_A": "construction", "BUILD_OR_UNIT_B": "construction",
    "BUILD_OR_UNIT_C": "construction", "BUILD_OR_UNIT_D": "construction",
    "TECH_BUILDING_A": "construction", "PLACE_LP_DEFENSE": "construction",
    "CAPTURE_LP": "map_control",
    "REINFORCE_SQUAD_A": "logistics", "REINFORCE_SQUAD_B": "logistics",
    "UNIT_ATTACK": "combat", "ABILITY_A": "combat", "ABILITY_B": "combat",
    "ABILITY_C": "combat", "SPECIAL_ABILITY": "combat",
    "UNIT_SPECIAL_5": "combat", "ORK_ABILITY_A": "combat",
    "UNIT_ORDER_A": "movement_and_orders", "UNIT_ORDER_C": "movement_and_orders",
    "UNIT_ORDER_E": "movement_and_orders", "UNIT_MOVE": "movement_and_orders",
}

BUILD_KIND_NAMES = {
    204: "barracks_t1", 208: "lp_post", 209: "unit_order",
    90: "generator_heuristic",
}


def build_kind_name(build_sub_type):
    return BUILD_KIND_NAMES.get(build_sub_type, f"type_{build_sub_type}")


def _cat_offset(sc_size):
    return 28 if sc_size == 40 else 22


def _id_offset(sc_size):
    return 29 if sc_size == 40 else 23


def _category_of(byte_val):
    if byte_val == 0x02:
        return "UNIT_ORDER"
    if byte_val == 0x03:
        return "DEMOLISH"
    if byte_val == 0x06:
        return "BUILD_STRUCTURE"
    return "UNKNOWN"


def _packet_cmd_ids(data, payload_start, packet_size, limit):
    out = []
    if payload_start + 30 > limit:
        return out
    inner_size = struct.unpack_from("<i", data, payload_start + 25)[0]
    if inner_size <= 0:
        return out
    p = payload_start + 30
    end = min(payload_start + packet_size, limit)
    while p < end - 2:
        sc = struct.unpack_from("<H", data, p)[0]
        if 28 <= sc <= 45 and p + sc <= end:
            if sc == 40 and data[p + 27] == 3:
                cmd_id = 0x0000C300 | data[p + 28]
            else:
                cmd_id = struct.unpack_from("<i", data, p + _id_offset(sc))[0]
            if cmd_id in CMD_REGISTRY:
                out.append(cmd_id)
                p += sc
                continue
        p += 1
    return out


def _packet_home_build_x(data, payload_start, packet_size, limit):
    if payload_start + 30 > limit:
        return None
    p = payload_start + 30
    end = min(payload_start + packet_size, limit)
    while p < end - 2:
        sc = struct.unpack_from("<H", data, p)[0]
        if 28 <= sc <= 45 and p + sc <= end:
            if sc == 40 and p + 40 <= end:
                x = struct.unpack_from("<h", data, p + 34)[0]
                y = struct.unpack_from("<h", data, p + 36)[0]
                if 0 < y < 200 and abs(x) > 600:
                    return x
            p += sc
        else:
            p += 1
    return None


def _packet_rep_entity(data, payload_start, packet_size, limit):
    if payload_start + 30 > limit:
        return None
    p = payload_start + 30
    end = min(payload_start + packet_size, limit)
    while p < end - 2:
        sc = struct.unpack_from("<H", data, p)[0]
        if 28 <= sc <= 45 and p + sc <= end:
            if sc == 40 and data[p + 27] == 3:
                cmd_id = 0x0000C300 | data[p + 28]
            else:
                cmd_id = struct.unpack_from("<i", data, p + _id_offset(sc))[0]
            point_ref = cmd_id in (CMD_PLACE_BLUEPRINT, CMD_DEMOLISH_BUILDING)
            if not point_ref and p + 6 <= end:
                entity_id = struct.unpack_from("<i", data, p + 2)[0]
                if 0 < entity_id < 100_000_000 and entity_id != 671088640:
                    return entity_id
            p += sc
        else:
            p += 1
    return None


def _resolve_by_entity_clusters(pkts, pkt_entity):
    ents = sorted(e for e in pkt_entity if e is not None)
    if len(ents) < 20:
        return False
    top_gap = second_gap = -1
    split_lo = split_hi = 0
    for i in range(len(ents) - 1):
        gap = ents[i + 1] - ents[i]
        if gap > top_gap:
            second_gap = top_gap
            top_gap = gap
            split_lo, split_hi = ents[i], ents[i + 1]
        elif gap > second_gap:
            second_gap = gap
    if second_gap <= 0 or top_gap < second_gap * 5 or top_gap < 50_000:
        return False
    split = (split_lo + split_hi) // 2
    for i in range(len(pkts)):
        e = pkt_entity[i]
        pkts[i][1] = -1 if e is None else (0 if e < split else 1)
    for i in range(len(pkts)):
        if pkts[i][1] != -1:
            continue
        left = next((pkts[j][1] for j in range(i - 1, -1, -1) if pkts[j][1] != -1), None)
        right = next((pkts[j][1] for j in range(i + 1, len(pkts)) if pkts[j][1] != -1), None)
        pkts[i][1] = left if left is not None else (right if right is not None else 0)
    return True


def _resolve_players(data, cmd_stream_start):
    n = len(data)
    pkts, pkt_cmds, pkt_home_x, pkt_entity = [], [], [], []
    has_ork = has_de = False

    pos = cmd_stream_start
    while pos < n - 4:
        packet_size = struct.unpack_from("<i", data, pos)[0]
        if packet_size == 0:
            pos += 4
            continue
        if packet_size < 17 or packet_size > 65536:
            pos += 1
            continue
        if pos + 4 + packet_size > n:
            break
        payload_start = pos + 4
        if data[payload_start] != 0x50:
            pos += 1
            continue
        if packet_size > 17:
            cmds = _packet_cmd_ids(data, payload_start, packet_size, n)
            if cmds:
                pkts.append([payload_start, -1])
                pkt_cmds.append(cmds)
                pkt_home_x.append(_packet_home_build_x(data, payload_start, packet_size, n))
                pkt_entity.append(_packet_rep_entity(data, payload_start, packet_size, n))
                for c in cmds:
                    if c in _ANCHOR_B_ORK:
                        has_ork = True
                    if c in _ANCHOR_B_DE:
                        has_de = True
        pos += 4 + packet_size

    if _resolve_by_entity_clusters(pkts, pkt_entity):
        return {p[0]: p[1] for p in pkts}

    anchor_b = _ANCHOR_B_ORK if has_ork else _ANCHOR_B_DE
    for i, cmds in enumerate(pkt_cmds):
        a = any(c in _ANCHOR_A for c in cmds)
        b = any(c in anchor_b for c in cmds)
        if a and not b:
            pkts[i][1] = 0
        elif b and not a:
            pkts[i][1] = 1

    sum_a, cnt_a = 0, 0
    for i in range(len(pkts)):
        if pkts[i][1] == 0 and pkt_home_x[i] is not None:
            sum_a += pkt_home_x[i]
            cnt_a += 1
    if cnt_a >= 3:
        a_sign = 1 if sum_a >= 0 else -1
        for i in range(len(pkts)):
            if pkts[i][1] != -1 or pkt_home_x[i] is None:
                continue
            side = 1 if pkt_home_x[i] >= 0 else -1
            pkts[i][1] = 0 if side == a_sign else 1

    for i in range(len(pkts)):
        if pkts[i][1] != -1:
            continue
        left = next((pkts[j][1] for j in range(i - 1, -1, -1) if pkts[j][1] != -1), None)
        right = next((pkts[j][1] for j in range(i + 1, len(pkts)) if pkts[j][1] != -1), None)
        pkts[i][1] = left if left is not None else (right if right is not None else 0)

    return {p[0]: p[1] for p in pkts}


def _confidence(e):
    if e["cmd_id"] in CMD_REGISTRY:
        return 0.95
    if (e["cmd_id"] & 0xFFFF0000) == 0x0000C000:
        return 0.75
    if e["category"] == "BUILD_STRUCTURE":
        return 0.70
    if e["category"] in ("UNIT_ORDER", "DEMOLISH"):
        return 0.60
    return 0.35


def _strategy_type(e):
    if e["cmd_id"] == CMD_BUILD_POWER_GENERATOR:
        return "EARLY_GENERATOR" if e["time_seconds"] < 120 else "TECH_STRUCTURE"
    if e["cmd_id"] == CMD_DEMOLISH_BUILDING:
        return "DEMOLISH"
    if e["cmd_id"] in (CMD_SQUAD_APPEAR, CMD_SQUAD_ORDER):
        return "SQUAD_EVENT"
    if e["category"] == "BUILD_STRUCTURE":
        return "EARLY_BARRACKS" if e["time_seconds"] < 120 else "TECH_STRUCTURE"
    if e["category"] == "UNIT_ORDER" and e["time_seconds"] < 120:
        return "EARLY_UNIT_PRODUCTION"
    return "UNKNOWN"


def _parse_subcommand(data, offset, sc_size, tick, seq, player_id, raw_events):
    cat_off = _cat_offset(sc_size)
    id_off = _id_offset(sc_size)
    if offset + id_off + 4 > len(data):
        return
    entity_id = struct.unpack_from("<i", data, offset + 2)[0]
    b27 = data[offset + 27] if offset + 27 < len(data) else 0

    if sc_size == 40 and b27 == 3:
        cmd_id = 0x0000C300 | data[offset + 28]
        category = "BUILD_STRUCTURE"
    else:
        cmd_id = struct.unpack_from("<i", data, offset + id_off)[0]
        category = _category_of(data[offset + cat_off])

    e = {
        "tick": tick, "time_seconds": tick / 8.0, "cmd_id": cmd_id,
        "cmd_name": CMD_REGISTRY.get(cmd_id, f"UNK_0x{cmd_id:08X}"),
        "entity_id": entity_id, "sc_size": sc_size, "packet_seq": seq,
        "player_id": player_id, "category": category,
        "has_coords": False, "x": 0, "y": 0, "z": 0, "build_sub_type": -1,
    }
    if sc_size == 40 and category == "BUILD_STRUCTURE" and offset + 40 <= len(data):
        e["has_coords"] = True
        e["x"] = struct.unpack_from("<h", data, offset + 34)[0]
        e["y"] = struct.unpack_from("<h", data, offset + 36)[0]
        e["z"] = struct.unpack_from("<h", data, offset + 38)[0]
        e["build_sub_type"] = data[offset + 10]

    e["confidence"] = _confidence(e)
    e["strategy_type"] = _strategy_type(e)
    raw_events.append(e)


def _is_valid_subcommand(data, offset, sc_size, end_pos):
    if sc_size < 28 or offset + sc_size > end_pos or offset + sc_size > len(data):
        return False
    id_off = _id_offset(sc_size)
    if offset + id_off + 4 > len(data):
        return False
    return struct.unpack_from("<i", data, offset + id_off)[0] != 0


def _parse_packet(data, payload_start, packet_size, tick, seq, packet_player, raw_events):
    n = len(data)
    if payload_start + 30 > n:
        return
    current_player = packet_player.get(payload_start, 0)
    inner_size = struct.unpack_from("<i", data, payload_start + 25)[0]
    inner_start = payload_start + 30
    if inner_size <= 0 or inner_start + inner_size > n:
        return

    pos, end_pos, parsed = inner_start, inner_start + inner_size, 0
    while pos < end_pos - 2:
        sc_size = struct.unpack_from("<H", data, pos)[0]
        if not _is_valid_subcommand(data, pos, sc_size, end_pos):
            pos += 1
            continue
        _parse_subcommand(data, pos, sc_size, tick, seq, current_player, raw_events)
        parsed += 1
        pos += sc_size

    cmd_count = data[payload_start + 13]
    remaining = cmd_count - parsed
    packet_end = payload_start + packet_size
    tp = end_pos
    while remaining > 0 and tp < packet_end - 2:
        sc_size = struct.unpack_from("<H", data, tp)[0]
        if 28 <= sc_size <= 45 and tp + sc_size <= packet_end:
            id_off = _id_offset(sc_size)
            if sc_size == 40 and data[tp + 27] == 3:
                cmd_id = 0x0000C300 | data[tp + 28]
            else:
                cmd_id = struct.unpack_from("<i", data, tp + id_off)[0]
            if cmd_id in CMD_REGISTRY:
                _parse_subcommand(data, tp, sc_size, tick, seq, current_player, raw_events)
                parsed += 1
                remaining -= 1
                tp += sc_size
                continue
        tp += 1


def _dedupe_events(raw_events):
    groups = {}
    for e in raw_events:
        groups.setdefault((e["tick"], e["entity_id"], e["cmd_id"]), []).append(e)
    timeline = []
    for group in groups.values():
        if len(group) == 1:
            timeline.append(group[0])
            continue
        chosen = next((e for e in group if e["has_coords"]), None)
        if chosen is None:
            chosen = next((e for e in group if e["sc_size"] == 28), group[0])
        timeline.append(chosen)
    return timeline


def parse_command_stream(data, cmd_stream_start):
    packet_player = _resolve_players(data, cmd_stream_start)
    raw_events = []
    n = len(data)
    pos, seq = cmd_stream_start, 0
    while pos < n - 4:
        packet_size = struct.unpack_from("<i", data, pos)[0]
        if packet_size == 0:
            pos += 4
            continue
        if packet_size < 17 or packet_size > 65536:
            pos += 1
            continue
        if pos + 4 + packet_size > n:
            break
        payload_start = pos + 4
        if data[payload_start] != 0x50:
            pos += 1
            continue
        tick = struct.unpack_from("<i", data, payload_start + 1)[0]
        if packet_size > 17:
            _parse_packet(data, payload_start, packet_size, tick, seq, packet_player, raw_events)
            seq += 1
        pos += 4 + packet_size

    timeline = _dedupe_events(raw_events)
    timeline.sort(key=lambda e: e["tick"])
    player_events = {}
    for e in timeline:
        player_events.setdefault(e["player_id"], []).append(e)
    return player_events


def categorise(cmd_counts):
    buckets = {"construction": 0, "map_control": 0, "logistics": 0,
               "combat": 0, "movement_and_orders": 0, "unclassified": 0}
    by_opcode = {}
    for cmd_name, count in cmd_counts:
        by_opcode[cmd_name] = count
        buckets[CATEGORY_MAP.get(cmd_name, "unclassified")] += count
    return buckets, by_opcode


def attach_player_stats(players, data, cmd_stream_start, duration_seconds):
    player_events = parse_command_stream(data, cmd_stream_start)

    for gi, p in enumerate(players):
        events = player_events.get(gi, [])
        cmd_counts = Counter(e["cmd_name"] for e in events).items()
        buckets, by_opcode = categorise(cmd_counts)
        recorded_commands = len(events)
        apm = round(recorded_commands / (duration_seconds / 60), 1) if recorded_commands and duration_seconds else None
        p["recorded_commands"] = recorded_commands
        p["recorded_apm"] = apm
        p["activity_by_category"] = buckets
        p["opcode_breakdown"] = dict(sorted(by_opcode.items(), key=lambda kv: -kv[1]))

        minute_counts = Counter(int(e["time_seconds"] // 60) for e in events)
        last_minute = max(minute_counts, default=-1)
        p["commands_per_minute"] = [minute_counts.get(m, 0) for m in range(last_minute + 1)]

        p["build_order"] = [
            {"time_seconds": round(e["time_seconds"], 1), "cmd_name": e["cmd_name"],
             "kind": build_kind_name(e["build_sub_type"]), "x": e["x"], "y": e["y"]}
            for e in events if e["build_sub_type"] > 0
        ]


# =======================================================================
# Winner/loser from filename
# =======================================================================

def _normalise_token(s: str) -> str:
    s = s.lower()
    s = re.sub(r"[^a-z0-9]", "", s)
    s = re.sub(r"race$", "", s)
    return s.rstrip("s")


def infer_result_from_filename(rec_path: Path, players):
    stem = rec_path.stem

    m = re.search(r"\bW[-_]([A-Za-z]+)[-_]L[-_]([A-Za-z]+)\b", stem, re.IGNORECASE)
    if m:
        winner_token, loser_token, method = m.group(1), m.group(2), "explicit_W_L_tag"
    else:
        parts = re.split(r"[_\-\s]+(?:vs|v)[_\-\s]+", stem, maxsplit=1, flags=re.IGNORECASE)
        if len(parts) == 2:
            winner_token, loser_token, method = parts[0], parts[1], "vs_separator_first_token_is_winner"
        else:
            return {"known": False, "note": "No winner/loser pattern recognised in filename",
                    "filename_stem": stem}

    def match_player(token):
        tok_n = _normalise_token(token)
        matches = []
        for p in players:
            name_n = _normalise_token(p.get("name") or "")
            race_n = _normalise_token((p.get("race") or "").replace("_race", ""))
            if tok_n and (tok_n == name_n or (race_n and (tok_n == race_n or tok_n in race_n or race_n in tok_n))):
                matches.append(p.get("name"))
        return matches

    winner_matches = match_player(winner_token)
    loser_matches = match_player(loser_token)

    result = {"known": False, "method": method,
              "winner_token": winner_token, "loser_token": loser_token}
    if len(winner_matches) == 1 and len(loser_matches) == 1 and winner_matches[0] != loser_matches[0]:
        result["known"] = True
        result["winner"] = winner_matches[0]
        result["loser"] = loser_matches[0]
        result["confidence"] = ("Inferred from filename convention only - not "
                                 "stored in the replay itself, and not verified "
                                 "against any other source. Treat as a hypothesis "
                                 "unless you know your source consistently follows "
                                 "this naming pattern.")
    else:
        result["note"] = (f"Could not uniquely match filename tokens to players "
                           f"(winner candidates: {winner_matches or 'none'}, "
                           f"loser candidates: {loser_matches or 'none'})")
    return result


CSV_COLUMNS = [
    ("name", "Player"), ("race", "Race"), ("recorded_commands", "Recorded Commands"),
    ("recorded_apm", "Recorded APM"), ("construction", "Construction"),
    ("map_control", "Map Control"), ("logistics", "Logistics"), ("combat", "Combat"),
    ("movement_and_orders", "Movement & Orders"), ("unclassified", "Unclassified"),
]


def write_player_stats_csv(players, out_dir):
    import csv
    csv_path = out_dir / "player_stats.csv"
    with csv_path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(label for _, label in CSV_COLUMNS)
        for p in players:
            categories = p.get("activity_by_category", {})
            row = []
            for key, _ in CSV_COLUMNS:
                if key in categories:
                    row.append(categories[key])
                else:
                    row.append(p.get(key, ""))
            writer.writerow(row)
    return csv_path


# =======================================================================

def generate_report(rec_path: Path):
    rec_path = Path(rec_path)
    data = rec_path.read_bytes()
    root = parse_chunky(data)

    foldinfo = find_child(root, "FOLD", "INFO")
    cmd_stream_start = foldinfo.data_offset + foldinfo.size if foldinfo else len(data)

    foldpost = find_child(root, "FOLD", "POST")
    duration_ticks = None
    if foldpost:
        dd = find_child(foldpost, "DATA", "DATA")
        if dd and len(dd.payload) >= 4:
            duration_ticks = struct.unpack_from("<i", dd.payload, 0)[0]
    duration_seconds = duration_ticks / 8.0 if duration_ticks else None

    out_dir = rec_path.parent / (rec_path.stem + "_report")
    out_dir.mkdir(exist_ok=True)

    map_info = extract_map_info(root)
    lobby = extract_lobby_settings(root)
    players = extract_players(root, out_dir)
    player_names = [p.get("name") for p in players]

    attach_player_stats(players, data, cmd_stream_start, duration_seconds)
    chat_messages = extract_chat_messages(data, cmd_stream_start, player_names)
    result_guess = infer_result_from_filename(rec_path, players)

    report = {
        "file": str(rec_path),
        "file_size_bytes": len(data),
        "duration_ticks": duration_ticks,
        "duration_seconds": duration_seconds,
        "map": map_info,
        "lobby_settings": lobby,
        "result": result_guess,
        "players": players,
        "chat_messages": chat_messages,
    }

    (out_dir / "report.json").write_text(
        json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
    write_player_stats_csv(players, out_dir)
    return report, out_dir


def main():
    if len(sys.argv) < 2:
        print("usage: dow_replay_report.py <replay.rec>")
        sys.exit(1)

    report, out_dir = generate_report(sys.argv[1])
    print(json.dumps(report, indent=2, ensure_ascii=False))
    print(f"\n--- written to {out_dir} ---", file=sys.stderr)


if __name__ == "__main__":
    main()
