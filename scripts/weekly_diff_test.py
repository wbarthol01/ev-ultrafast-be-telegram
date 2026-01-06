
#!/usr/bin/env python3
# Eén testpost: verschil van de afgelopen week (instelbaar via RANGE_DAYS; default 7).
# Vergelijkt huidige Road feed met vorige snapshot in data/snapshot_prev.json.
# Post altijd (handmatig gestart via workflow_dispatch).

import json, os, datetime
import requests
from pathlib import Path
from typing import List, Dict, Any

# --- Config ---
ROAD_JSON_URL = "https://roaming.road.io/files/9ef09c78-2666-418a-aa45-4f2261e2e305/locations.json?force=true"

# Telegram (LET OP: we lezen 'TELEGRAM_TOKEN' en 'TELEGRAM_CHAT_ID' uit de env)
TELEGRAM_TOKEN   = os.environ.get("TELEGRAM_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")
TELEGRAM_SEND_MSG = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"

# Ultrafast drempel (AFIR-consistent: DC >= 150 kW)
MIN_KW = 150.0

# Bestanden
STATE_DIR      = Path("data")
SNAPSHOT_PATH  = STATE_DIR / "snapshot_prev.json"   # vorige snapshot
CHANGELOG_PATH = STATE_DIR / "changelog.log"

# Limits
MAX_LIST_ITEMS = 20

def log(msg: str):
    ts = datetime.datetime.utcnow().isoformat() + "Z"
    print(f"[{ts}] {msg}")
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    with open(CHANGELOG_PATH, "a", encoding="utf-8") as f:
        f.write(f"[{ts}] {msg}\n")

def fetch_locations() -> List[Dict[str, Any]]:
    r = requests.get(ROAD_JSON_URL, timeout=90)
    r.raise_for_status()
    data = r.json()
    # FIX: eerst lijst vs dict onderscheiden
    if isinstance(data, list):
        items = data
    else:
        items = data.get("data") or data.get("locations") or []
    log(f"Fetched feed: {len(items)} locations (raw).")
    return items

def is_belgium(loc: Dict[str, Any]) -> bool:
    cc2 = (loc.get("country_code") or "").upper()
    cc3 = (loc.get("country") or "").upper()
    return cc2 == "BE" or cc3 in ("BEL", "BE")

def iter_evse_connectors(loc: Dict[str, Any]):
    for evse in loc.get("evses", []):
        for con in evse.get("connectors", []):
            yield evse, con

def kw_from_connector(con: Dict[str, Any]) -> float:
    val = con.get("max_electric_power") or con.get("ratedPowerKw") or con.get("powerKw")
    if val is None and con.get("max_power") is not None:  # W -> kW
        try:    return float(con["max_power"]) / 1000.0
        except: return 0.0
    try:        return float(val)
    except:     return 0.0

def is_ultrafast_dc(con: Dict[str, Any]) -> bool:
    current = (con.get("current_type") or con.get("currentType") or "").upper()
    return current == "DC" and kw_from_connector(con) >= MIN_KW

def is_ccs_standard(standard: str) -> bool:
    s = (standard or "").upper()
    return ("CCS" in s) or (s in {"IEC_62196_T2_COMBO","IEC_62196_T1_COMBO"})

def count_ccs_ports(loc: Dict[str, Any]) -> int:
    cnt = 0
    for _, con in iter_evse_connectors(loc):
        std = con.get("standard") or con.get("connector_type") or con.get("connectorType") or ""
        if is_ccs_standard(std):
            cnt += 1
    return cnt

def uid(loc: Dict[str, Any], evse: Dict[str, Any], con: Dict[str, Any]) -> str:
    loc_id  = loc.get("id") or loc.get("location_id") or "loc_unknown"
    evse_id = evse.get("uid") or evse.get("evse_id") or "evse_unknown"
    con_id  = con.get("id") or con.get("connector_id") or "con_unknown"
    return f"{loc_id}::{evse_id}::{con_id}"

def google_maps_link(loc: Dict[str, Any]) -> str:
    coords = loc.get("coordinates") or {}
    lat = coords.get("latitude") or coords.get("lat")
    lon = coords.get("longitude") or coords.get("lng") or coords.get("lon")
    if lat is None or lon is None:
        return ""
    return f"https://www.google.com/maps/search/?api=1&query={lat},{lon}"

def build_index(snapshot: List[Dict[str, Any]]):
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

def summarize_line(loc: Dict[str, Any], evse: Dict[str, Any], con: Dict[str, Any]) -> str:
    name    = loc.get("name") or loc.get("site_name") or "Laadlocatie"
    operator= loc.get("operator") or loc.get("operator_name") or loc.get("party_id") or "Onbekend"
    street  = loc.get("address") or ""
    city    = loc.get("city") or ""
    postal  = loc.get("postal_code") or ""
    ctype   = con.get("standard") or con.get("connector_type") or con.get("connectorType") or "n/a"
    pkw     = kw_from_connector(con)
    pkws    = f"{int(pkw) if pkw.is_integer() else round(pkw,1)} kW"
    ccs     = count_ccs_ports(loc)
    gmaps   = google_maps_link(loc)
    line    = f"• {name} — {operator} ({street}, {postal} {city}) | {ctype}, {pkws} • CCS: {ccs}"
    if gmaps:
        line += f" • 🗺️ <a href=\"{gmaps}\">Google Maps</a>"
    return line

def post_to_telegram(text: str):
    payload = {"chat_id": TELEGRAM_CHAT_ID, "text": text, "parse_mode": "HTML", "disable_web_page_preview": True}
    r = requests.post(TELEGRAM_SEND_MSG, json=payload, timeout=30)
    try:
        body = r.json()
    except Exception:
        body = {"non_json": r.text}
    log(f"Telegram response status={r.status_code} body={body}")
    r.raise_for_status()

def main():
    # Veiligheid
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        raise SystemExit("TELEGRAM_TOKEN/TELEGRAM_CHAT_ID ontbreken in env.")

    range_days = int(os.environ.get("RANGE_DAYS","7"))
    log(f"Running weekly diff test (range_days={range_days})")

    # Huidige feed
    curr = fetch_locations()
    if not curr:
        post_to_telegram("<b>Ultrafast DC (BE) — Weektest</b>\nFeed lijkt leeg. Probeer later opnieuw.")
        log("Feed leeg; abort zonder baseline-update.")
        return

    idx_curr, st_curr = build_index(curr)

    # Vorige snapshot
    if not SNAPSHOT_PATH.exists():
        msg = (
            "<b>Ultrafast DC (BE) — Weektest</b>\n"
            "Geen vorige snapshot gevonden in de repo.\n"
            "We hebben de feed zojuist opgehaald en als baseline opgeslagen.\n"
            "Probeer over een paar dagen opnieuw voor een echte weekdiff."
        )
        post_to_telegram(msg)
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        with open(SNAPSHOT_PATH, "w", encoding="utf-8") as f:
            json.dump(curr, f, ensure_ascii=False, indent=2)
        log("Baseline snapshot opgeslagen (eerste run).")
        return

    with open(SNAPSHOT_PATH, "r", encoding="utf-8") as f:
        prev = json.load(f)

    idx_prev, st_prev = build_index(prev)

    prev_ids, curr_ids = set(idx_prev.keys()), set(idx_curr.keys())
    added   = sorted(curr_ids - prev_ids)
    removed = sorted(prev_ids - curr_ids)

    changed = []
    for u in (prev_ids & curr_ids):
        a, b = st_prev.get(u, ""), st_curr.get(u, "")
        if a != b and (a or b):
            changed.append((u, a, b))

    # Bericht opstellen
    def list_lines(ids, idx):
        if not ids: return "• —"
        lines = []
        for u in ids[:MAX_LIST_ITEMS]:
            loc, evse, con = idx[u]
            lines.append(summarize_line(loc, evse, con))
        if len(ids) > MAX_LIST_ITEMS:
            lines.append(f"… +{len(ids) - MAX_LIST_ITEMS} meer")
        return "\n".join(lines)

    date_label = datetime.datetime.now().strftime("%d %b %Y")
    message = [
        f"<b>Ultrafast DC (BE) — Veranderingen laatste {range_days} dagen (TEST)</b>",
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
            tup = idx_curr.get(u) or idx_prev.get(u)
            loc = tup[0] if tup else {}
            nm  = loc.get("name") or loc.get("site_name") or "Laadlocatie"
            message.append(f"• {nm}: {a.title() or '—'} → {b.title() or '—'}")
        if len(changed) > MAX_LIST_ITEMS:
            message.append(f"… +{len(changed) - MAX_LIST_ITEMS} meer")
    else:
        message.append("• —")

    post_to_telegram("\n".join(message))

    # (optioneel) baseline na test updaten:
    # with open(SNAPSHOT_PATH, "w", encoding="utf-8") as f:
    #     json.dump(curr, f, ensure_ascii=False, indent=2)
    # log("Baseline geüpdatet na test.")

if __name__ == "__main__":
    main()
