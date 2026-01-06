
#!/usr/bin/env python3
# Dagelijkse samenvatting (laatste 24u) van BE ultrafast DC connectors uit Road.io feed.
# Post één bericht om 12:00 Europe/Brussels en logt diff + statuswijzigingen.
# Bevat: CCS-aantal per locatie, Google Maps-link, en optioneel logo's voor toegevoegde items.

import json, os, time, hashlib, datetime
import requests
from pathlib import Path
from zoneinfo import ZoneInfo
from typing import List, Dict, Any, Tuple

# === Config ===
ROAD_JSON_URL = "https://roaming.road.io/files/9ef09c78-2666-418a-aa45-4f2261e2e305/locations.json?force=true"

# Telegram
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")
TELEGRAM_SEND_MESSAGE = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
TELEGRAM_SEND_PHOTO   = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendPhoto"

# AFIR-consistente drempel voor "ultrafast"
MIN_KW = 150.0

# Bestanden
STATE_DIR = Path("data")
STATE_DIR.mkdir(parents=True, exist_ok=True)
SNAPSHOT_PATH = STATE_DIR / "snapshot_prev.json"          # vorige snapshot
POST_MARK_PATH = STATE_DIR / "last_daily_post_date.txt"   # markeer "vandaag al gepost?"
CHANGELOG_PATH = STATE_DIR / "changelog.log"

# Output-limieten
MAX_LIST_ITEMS = 20

# Logo-instellingen (optioneel)
SEND_LOGOS_FOR_ADDED = True   # zet False als je geen logo's wil sturen
MAX_LOGOS_PER_POST = 10       # anti-spam
LOGO_DIR = Path("logos")      # lokale PNG's: logos/<operator>.png (case-insensitive)
LOGO_URL_MAP = {
    # "Fastned": "https://…/fastned.png",
    # "Ionity": "https://…/ionity.png",
    # "TotalEnergies": "https://…/totalenergies.png",
}

# === Helpers ===
def now_brussels() -> datetime.datetime:
    return datetime.datetime.now(ZoneInfo("Europe/Brussels"))

def append_log(text: str) -> None:
    ts = datetime.datetime.utcnow().isoformat() + "Z"
    with open(CHANGELOG_PATH, "a", encoding="utf-8") as f:
        f.write(f"[{ts}] {text}\n")

def load_json(path: Path, default):
    if path.exists():
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    return default

def save_json(path: Path, obj) -> None:
    with open(path, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=2)

def fetch_locations() -> List[Dict[str, Any]]:
    r = requests.get(ROAD_JSON_URL, timeout=90)
    r.raise_for_status()
    data = r.json()
    if isinstance(data, dict):
        return data.get("data") or data.get("locations") or []
    return data

def is_belgium(loc: Dict[str, Any]) -> bool:
    cc2 = (loc.get("country_code") or "").upper()
    cc3 = (loc.get("country") or "").upper()
    return cc2 == "BE" or cc3 in ("BEL", "BE")

def iter_evse_connectors(loc: Dict[str, Any]):
    for evse in loc.get("evses", []):
        for con in evse.get("connectors", []):
            yield evse, con

def kw_from_connector(con: Dict[str, Any]) -> float:
    # OCPI varianten: max_electric_power (kW), ratedPowerKw/powerKw (kW), max_power (W)
    val = con.get("max_electric_power") or con.get("ratedPowerKw") or con.get("powerKw")
    if val is None and con.get("max_power") is not None:  # W -> kW
        try:
            return float(con["max_power"]) / 1000.0
        except Exception:
            return 0.0
    try:
        return float(val)
    except Exception:
        return 0.0

def is_ultrafast_dc(con: Dict[str, Any]) -> bool:
    current = (con.get("current_type") or con.get("currentType") or "").upper()
    return current == "DC" and kw_from_connector(con) >= MIN_KW

def is_ccs_standard(standard: str) -> bool:
    s = (standard or "").upper()
    return ("CCS" in s) or (s in {"IEC_62196_T2_COMBO", "IEC_62196_T1_COMBO"})

def count_ccs_ports(loc: Dict[str, Any]) -> int:
    cnt = 0
    for _, con in iter_evse_connectors(loc):
        std = con.get("standard") or con.get("connector_type") or con.get("connectorType") or ""
        if is_ccs_standard(std):
            cnt += 1
    return cnt

def uid(loc: Dict[str, Any], evse: Dict[str, Any], con: Dict[str, Any]) -> str:
    loc_id = loc.get("id") or loc.get("location_id") or "loc_unknown"
    evse_uid = evse.get("uid") or evse.get("evse_id") or "evse_unknown"
    con_id = con.get("id") or con.get("connector_id") or "con_unknown"
    return f"{loc_id}::{evse_uid}::{con_id}"

def google_maps_link(loc: Dict[str, Any]) -> str:
    coords = loc.get("coordinates") or {}
    lat = coords.get("latitude") or coords.get("lat")
    lon = coords.get("longitude") or coords.get("lng") or coords.get("lon")
    if lat is None or lon is None:
        return ""
    return f"https://www.google.com/maps/search/?api=1&query={lat},{lon}"

def build_index(snapshot: List[Dict[str, Any]]) -> Tuple[Dict[str, Any], Dict[str, str]]:
    """Index: uid -> (loc, evse, con), en statusmap: uid -> STATUS."""
    idx, status = {}, {}
    for loc in snapshot:
        if not is_belgium(loc):
            continue
        for evse, con in iter_evse_connectors(loc):
            if not is_ultrafast_dc(con):
                continue
            u = uid(loc, evse, con)
            idx[u] = (loc, evse, con)
            status[u] = (con.get("status") or "").upper()
    return idx, status

def daily_summary(prev: List[Dict[str, Any]], curr: List[Dict[str, Any]]):
    idx_prev, st_prev = build_index(prev)
    idx_curr, st_curr = build_index(curr)

    prev_ids, curr_ids = set(idx_prev.keys()), set(idx_curr.keys())
    added   = sorted(curr_ids - prev_ids)
    removed = sorted(prev_ids - curr_ids)

    changed = []
    for u in (prev_ids & curr_ids):
        a, b = st_prev.get(u, ""), st_curr.get(u, "")
        if a != b and (a or b):
            changed.append((u, a, b))

    return {"added": added, "removed": removed, "changed": changed,
            "idx_prev": idx_prev, "idx_curr": idx_curr}

def summarize_line(loc: Dict[str, Any], evse: Dict[str, Any], con: Dict[str, Any]) -> str:
    name = loc.get("name") or loc.get("site_name") or "Laadlocatie"
    operator = loc.get("operator") or loc.get("operator_name") or loc.get("party_id") or "Onbekend"
    street = loc.get("address") or ""
    city = loc.get("city") or ""
    postal = loc.get("postal_code") or ""
    ctype = con.get("standard") or con.get("connector_type") or con.get("connectorType") or "n/a"
    pkw = kw_from_connector(con)
    pkws = f"{int(pkw) if pkw.is_integer() else round(pkw,1)} kW"
    ccs_count = count_ccs_ports(loc)
    gmaps = google_maps_link(loc)
    line = f"• {name} — {operator} ({street}, {postal} {city}) | {ctype}, {pkws} • CCS: {ccs_count}"
    if gmaps:
        line += f" • 🗺️ <a href=\"{gmaps}\">Google Maps</a>"
    return line

def format_daily_message(summary: Dict[str, Any], date_label: str) -> str:
    added, removed, changed = summary["added"], summary["removed"], summary["changed"]
    idx_prev, idx_curr = summary["idx_prev"], summary["idx_curr"]

    def list_lines(ids, idx):
        if not ids:
            return "• —"
        lines = []
        for u in ids[:MAX_LIST_ITEMS]:
            loc, evse, con = idx[u]
            lines.append(summarize_line(loc, evse, con))
        if len(ids) > MAX_LIST_ITEMS:
            lines.append(f"… +{len(ids) - MAX_LIST_ITEMS} meer")
        return "\n".join(lines)

    parts = [
        f"<b>Ultrafast DC (BE) — Veranderingen laatste 24u</b>",
        f"🗓️ {date_label}",
        "",
        f"➕ Toegevoegd: {len(added)}",
        list_lines(added, idx_curr),
        "",
        f"➖ Verwijderd: {len(removed)}",
        list_lines(removed, idx_prev),
        "",
        f"🔁 Statuswijzigingen: {len(changed)}",
    ]
    if changed:
        for (u, a, b) in changed[:MAX_LIST_ITEMS]:
            # haal representatie uit curr als kan, anders prev
            tup = idx_curr.get(u) or idx_prev.get(u)
            loc = tup[0] if tup else {}
            name = (loc.get("name") or loc.get("site_name") or "Laadlocatie")
            parts.append(f"• {name}: {a.title() or '—'} → {b.title() or '—'}")
        if len(changed) > MAX_LIST_ITEMS:
            parts.append(f"… +{len(changed) - MAX_LIST_ITEMS} meer")
    else:
        parts.append("• —")
    return "\n".join(parts)

def send_telegram_text(text: str) -> None:
    payload = {"chat_id": TELEGRAM_CHAT_ID, "text": text, "parse_mode": "HTML", "disable_web_page_preview": True}
    r = requests.post(TELEGRAM_SEND_MESSAGE, json=payload, timeout=30)
    r.raise_for_status()

def send_logo_if_available(operator: str, caption: str = "") -> bool:
    if not SEND_LOGOS_FOR_ADDED or not operator:
        return False
    # 1) lokaal PNG-bestand
    local = None
    if LOGO_DIR.exists():
        for p in LOGO_DIR.glob("*.png"):
            if p.stem.lower() == operator.lower():
                local = p
                break
    if local and local.exists():
        with open(local, "rb") as f:
            files = {"photo": f}
