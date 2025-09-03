#!/usr/bin/env python3
# KSAN approach corridor viewer using FR24 scrape with simple weather and Padres overlay
# Shows ATC callsign exactly as is for example SKW3376
# Displays Padres live score if tied or winning, otherwise weather with temp dot and wind arrow

import math, time, logging, requests
from typing import Optional, List, Dict
from rgbmatrix import RGBMatrix, RGBMatrixOptions, graphics
from airports_db import AIRPORTS_IATA

# ===== User geometry and filters =====
P1_LAT, P1_LON = 32.69079521369225, -117.00828355500153
P2_LAT, P2_LON = 32.72663595133696, -117.15939039340249
CORRIDOR_HALF_MILES = 0.5
ALT_FT_MIN = 200.0
ALT_FT_MAX = 4000.0
REQUIRE_ALT = True

# ===== Polling =====
POLL_INTERVAL_SEC = 30
HTTP_TIMEOUT_SEC  = 8

# ===== FR24 scraping =====
FEED_HOSTS = [
    "https://data-cloud.flightradar24.com",
    "https://data-live.flightradar24.com",
]
FEED_PATH = "/zones/fcgi/feed.js"
FEED_TAIL = (
    "&faa=1&satellite=1&mlat=1&flarm=1&adsb=1"
    "&gnd=0&air=1&vehicles=0&estimated=0&maxage=14400"
    "&gliders=0&stats=0&ems=1&limit=3"
)
DETAILS_HEAD = "https://data-live.flightradar24.com/clickhandler/?flight="
BROWSER_HEADERS = {
    "User-Agent": "Mozilla/5.0",
    "Accept": "application/json,text/plain,*/*",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": "https://www.flightradar24.com/",
    "Origin": "https://www.flightradar24.com",
    "Cache-Control": "no-cache",
}
_SESS = requests.Session(); _SESS.headers.update(BROWSER_HEADERS)

# ===== Matrix config =====
MATRIX_ROWS, MATRIX_COLS = 32, 64
HARDWARE_MAPPING = "adafruit-hat"

PWM_BITS = 10
PWM_LSB_NS = 70
GPIO_SLOWDOWN = 6
BRIGHTNESS = 90
LIMIT_REFRESH_HZ = 271

FONT_SMALL_CANDIDATES = [
    "/home/henry/rpi-rgb-led-matrix/fonts/6x10.bdf",
    "/usr/local/share/rgbmatrix/fonts/6x10.bdf",
    "/home/henry/rpi-rgb-led-matrix/fonts/5x8.bdf",
]
LINE1_Y, LINE2_Y, LINE3_Y = 10, 20, 30
SIDE_MARGIN_PX = 2

# dot settings for seven pixel text with one pixel upward nudge
DOT_DIAM_PX = 7
DOT_GAP_PX  = 3
DOT_BASELINE_NUDGE = -1

# ===== WeatherAPI simple =====
WEATHER_LAT, WEATHER_LON = 32.7195, -117.1339
WEATHERAPI_KEY = "ffe0bd3b204f429b80f00400251408"
WEATHER_CACHE_TTL = 900
_weather_simple_cache = {"ts": 0.0, "temp_text": "", "temp_color": None, "wind_text": ""}

# ===== Padres cache =====
_padres_cache = {"ts": 0.0, "have": False, "l1": "", "l2": "", "color": None}
PADRES_CACHE_TTL = 30

# ===== Logging =====
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s: %(message)s", datefmt="%H:%M:%S")
log = logging.getLogger("KSAN")
DEBUG = True

# ===== Aircraft names =====
AC_FULLNAME_MAP = {
    "A318":"Airbus A318","A319":"Airbus A319","A320":"Airbus A320","A321":"Airbus A321",
    "A20N":"Airbus A320neo","A21N":"Airbus A321neo",
    "B712":"Boeing 717-200","B738":"Boeing 737-800","B739":"Boeing 737-900",
    "B38M":"Boeing 737 MAX 8","B39M":"Boeing 737 MAX 9","B3JM":"Boeing 737 MAX 10",
    "B752":"Boeing 757-200","B763":"Boeing 767-300","B772":"Boeing 777-200","B77W":"Boeing 777-300ER",
    "B788":"Boeing 787-8","B789":"Boeing 787-9",
    "CRJ9":"CRJ900","E75S":"Embraer 175","E175":"Embraer 175","E170":"Embraer 170",
}

# ===== Geo helpers =====
def miles_to_deg_lat(miles): return miles / 69.0
def miles_to_deg_lon(miles, lat): return miles / (69.0 * max(0.1, math.cos(math.radians(lat))))
def corridor_bbox(p1, p2, half):
    (lat1, lon1), (lat2, lon2) = p1, p2
    mid = 0.5 * (lat1 + lat2); pad = half + 0.1
    dlat = miles_to_deg_lat(pad); dlon = miles_to_deg_lon(pad, mid)
    return max(lat1,lat2)+dlat, min(lat1,lat2)-dlat, min(lon1,lon2)-dlon, max(lon1,lon2)+dlon

def to_local_xy(lat, lon, lat0):
    mlat = 111320.0; mlon = 111320.0 * math.cos(math.radians(lat0))
    return lon*mlon, lat*mlat

def point_to_segment_dist_m(lat, lon, a_lat, a_lon, b_lat, b_lon):
    lat0 = 0.5 * (a_lat + b_lat)
    ax,ay = to_local_xy(a_lat,a_lon,lat0); bx,by = to_local_xy(b_lat,b_lon,lat0); px,py = to_local_xy(lat,lon,lat0)
    vx,vy = bx-ax, by-ay; wx,wy = px-ax, py-ay; v2 = vx*vx + vy*vy
    if v2 <= 1e-6: return math.hypot(px-ax,py-ay), 0.0
    t = max(0.0, min(1.0, (wx*vx+wy*vy)/v2)); cx,cy = ax+t*vx, ay+t*vy
    return math.hypot(px-cx,py-cy), t

def altitude_ok(alt): return (ALT_FT_MIN <= alt <= ALT_FT_MAX) if alt is not None else not REQUIRE_ALT
def within_corridor(lat,lon):
    d,t = point_to_segment_dist_m(lat,lon,P1_LAT,P1_LON,P2_LAT,P2_LON)
    return (0<=t<=1) and (d <= CORRIDOR_HALF_MILES*1609.344)

# ===== Utils =====
def clamp_center_x(width, text_w, margin):
    centered = (width - text_w) // 2
    return max(margin, min(centered, width - margin - text_w))

def airport_name_only(dep_code, dep_name, dep_city):
    code = (dep_code or "").upper()
    if code and code in AIRPORTS_IATA:
        val = AIRPORTS_IATA[code] or ""
        name = val.split("/")[-1].split(",")[0].strip()
        return name or code
    if dep_name: return dep_name.split(",")[0].strip()
    return code or ""

# ===== Parsers =====
def _pick_airport_fields(d):
    if not isinstance(d, dict): return (None,None,None)
    code = d.get("iata") or d.get("code") or d.get("icao")
    name = d.get("name")
    city = (d.get("position") or {}).get("region",{}).get("city")
    return (str(code).upper() if code else None, name, city)

# ===== FR24 fetchers =====
def _feed_url(host, north, south, west, east):
    return f"{host}{FEED_PATH}?bounds={north:.6f},{south:.6f},{west:.6f},{east:.6f}{FEED_TAIL}&_ts={int(time.time())}"

def fetch_live_scrape(north, south, west, east) -> List[dict]:
    last_err = None
    for host in FEED_HOSTS:
        url = _feed_url(host, north, south, west, east)
        try:
            r = _SESS.get(url, timeout=HTTP_TIMEOUT_SEC)
            if r.status_code == 403:
                last_err = f"{host} -> 403"; continue
            r.raise_for_status()
            js = r.json()
            out = []
            for fid, info in js.items():
                if fid in ("full_count", "version"): continue
                try:
                    lat = float(info[1]); lon = float(info[2])
                    alt_ft = float(info[4]) if info[4] not in (None, "", "0", 0) else None
                    callsign = str(info[13] or "").strip()
                    out.append({"lat": lat, "lon": lon, "alt_ft": alt_ft, "fn": callsign, "fid": fid})
                except Exception:
                    continue
            if DEBUG: log.info(f"Scrape live {host.split('//')[1]} flights {len(out)}")
            return out
        except Exception as e:
            last_err = f"{host} -> {e}"; continue
    if last_err: log.warning(f"Feed scrape error {last_err}")
    return []

def fetch_details_scrape(fid: str) -> dict:
    url = f"{DETAILS_HEAD}{fid}&_ts={int(time.time())}"
    try:
        r = _SESS.get(url, timeout=HTTP_TIMEOUT_SEC)
        if r.status_code == 403:
            tmp = dict(BROWSER_HEADERS); tmp.pop("Origin", None); tmp.pop("Referer", None)
            with requests.Session() as s2:
                s2.headers.update(tmp); r = s2.get(url, timeout=HTTP_TIMEOUT_SEC)
        r.raise_for_status()
        js = r.json()
        ident = js.get("identification", {}) or {}
        callsign = ident.get("callsign")
        flight_number_default = ((ident.get("number") or {}).get("default") if isinstance(ident.get("number"), dict) else None)
        ac = (js.get("aircraft") or {}).get("model", {}) or {}
        ac_code, ac_text = ac.get("code"), ac.get("text")
        reg = (js.get("aircraft") or {}).get("registration")
        dep = (js.get("airport") or {}).get("origin", {}) or {}
        dep_code, dep_name, dep_city = _pick_airport_fields(dep)
        return {
            "callsign": (str(callsign).strip() if callsign else None),
            "flight_number": (str(flight_number_default).strip() if flight_number_default else None),
            "registration": (str(reg).strip().upper() if reg else None),
            "type": (str(ac_code).strip().upper() if ac_code else None),
            "type_text": ac_text,
            "dep_code": dep_code, "dep_name": dep_name, "dep_city": dep_city,
        }
    except Exception as e:
        log.warning(f"Detail scrape error {fid}: {e}")
        return {}

def fetch_delay_minutes(fid: str) -> Optional[int]:
    url = f"{DETAILS_HEAD}{fid}&_ts={int(time.time())}"
    try:
        r = _SESS.get(url, timeout=HTTP_TIMEOUT_SEC)
        if r.status_code == 403:
            tmp = dict(BROWSER_HEADERS); tmp.pop("Origin", None); tmp.pop("Referer", None)
            with requests.Session() as s2:
                s2.headers.update(tmp); r = s2.get(url, timeout=HTTP_TIMEOUT_SEC)
        r.raise_for_status()
        js = r.json()
        tblock = js.get("time") or {}
        sched = (tblock.get("scheduled") or {})
        esti  = (tblock.get("estimated") or {})
        real  = (tblock.get("real") or {})
        a_sched = sched.get("arrival"); a_best = real.get("arrival") or esti.get("arrival")
        if a_sched and a_best:
            return int(round((int(a_best) - int(a_sched)) / 60.0))
        d_sched = sched.get("departure"); d_best = real.get("departure") or esti.get("departure")
        if d_sched and d_best:
            return int(round((int(d_best) - int(d_sched)) / 60.0))
        return None
    except Exception as e:
        log.info(f"Delay check failed {fid}: {e}")
        return None

# ===== Colors and dots =====
def _col(r,g,b): return graphics.Color(r,g,b)
WHITE=_col(255,255,255); GREEN=_col(0,255,0); YELLOW=_col(255,255,0); RED=_col(255,0,0); CYAN=_col(0,255,255); BLUE=_col(0,128,255)

def map_delay_to_color(d):
    if d is None: return GREEN
    if d <= -5: return CYAN
    if -4 <= d <= 5: return GREEN
    if d <= 20: return YELLOW
    return RED

def temp_to_color(t):
    if t is None: return WHITE
    if t <= 60: return BLUE
    if 65 <= t <= 75: return GREEN
    if t <= 80: return YELLOW
    return RED

def wind_dir_to_arrow(deg):
    if isinstance(deg, (int, float)):
        a = (int(deg) + 180 + 22) % 360
        return ["↑","↗","→","↘","↓","↙","←","↖"][a // 45]
    return ""

def draw_status_dot(canvas, right_edge_x, baseline_y, color):
    d = int(DOT_DIAM_PX); r = d // 2
    cy = baseline_y - r + DOT_BASELINE_NUDGE
    cx = right_edge_x + r
    for dy in range(-r, r + 1):
        span = int((r*r - dy*dy) ** 0.5)
        graphics.DrawLine(canvas, cx - span, cy + dy, cx + span, cy + dy, color)

# ===== Weather simple =====
def fetch_weather_simple():
    now = time.time()
    if now - _weather_simple_cache["ts"] < WEATHER_CACHE_TTL and _weather_simple_cache["temp_text"]:
        return _weather_simple_cache["temp_text"], _weather_simple_cache["temp_color"], _weather_simple_cache["wind_text"]
    try:
        js = requests.get(
            "https://api.weatherapi.com/v1/current.json",
            params={"key": WEATHERAPI_KEY, "q": f"{WEATHER_LAT},{WEATHER_LON}", "aqi": "no"},
            timeout=6
        ).json()
        cur = js.get("current", {}) or {}
        temp = cur.get("temp_f")
        wind = cur.get("wind_mph")
        wdeg = cur.get("wind_degree")
        tt = f"{int(round(temp))}°F" if isinstance(temp, (int,float)) else "—"
        tc = temp_to_color(temp) if isinstance(temp, (int,float)) else WHITE
        wt = f"{int(round(wind))}mph {wind_dir_to_arrow(wdeg)}" if isinstance(wind, (int,float)) else ""
        _weather_simple_cache.update({"ts": now, "temp_text": tt, "temp_color": tc, "wind_text": wt})
        return tt, tc, wt
    except Exception:
        return "—", WHITE, ""

# ===== Padres live only when tied or winning =====
def fetch_padres_score_lines():
    """
    Returns lines only when a Padres game is live and tied or they are winning.
    Robust to ESPN date and status variations.
    """
    now = time.time()
    if now - _padres_cache["ts"] < PADRES_CACHE_TTL:
        return _padres_cache["have"], _padres_cache["l1"], _padres_cache["l2"], _padres_cache["color"]

    def is_live(status_block):
        if not status_block:
            return False
        t = (status_block.get("type") or {})
        state = str(t.get("state") or "").lower()
        return state in ("in", "inprogress", "live")

    def team_is_padres(team_obj):
        name = (team_obj.get("displayName") or team_obj.get("name") or "").lower()
        abbr = (team_obj.get("abbreviation") or "").upper()
        short = (team_obj.get("shortDisplayName") or "").lower()
        return ("padres" in name) or ("padres" in short) or (abbr == "SD")

    def team_abbr(team_obj):
        return (team_obj.get("abbreviation") or team_obj.get("shortDisplayName") or team_obj.get("displayName") or "").upper()

    try:
        dates_to_try = [
            time.strftime("%Y%m%d", time.localtime()),
            time.strftime("%Y%m%d", time.localtime(time.time() - 86400)),
        ]

        for datestr in dates_to_try:
            r = requests.get(
                "https://site.api.espn.com/apis/site/v2/sports/baseball/mlb/scoreboard",
                params={"dates": datestr},
                timeout=6
            )
            r.raise_for_status()
            data = r.json()
            events = data.get("events") or []

            for ev in events:
                ev_status = ev.get("status")
                comps = (ev.get("competitions") or [{}])
                comp = comps[0]
                comp_status = comp.get("status")

                if not (is_live(ev_status) or is_live(comp_status)):
                    continue

                teams = comp.get("competitors") or []
                if len(teams) != 2:
                    continue

                tA = teams[0].get("team") or {}
                tB = teams[1].get("team") or {}

                if not (team_is_padres(tA) or team_is_padres(tB)):
                    continue

                sA = int(teams[0].get("score") or 0)
                sB = int(teams[1].get("score") or 0)

                aA = team_abbr(tA)
                aB = team_abbr(tB)

                padres_is_A = team_is_padres(tA)
                padres_score = sA if padres_is_A else sB
                opp_score = sB if padres_is_A else sA

                if padres_score < opp_score:
                    continue

                color = GREEN if padres_score > opp_score else YELLOW

                t = (ev_status or comp_status or {}).get("type") or {}
                detail = (t.get("detail") or "").lower()
                half = "T" if "top" in detail else ("B" if "bot" in detail or "bottom" in detail else "")
                num = "".join(ch for ch in detail if ch.isdigit()) or ""

                l1 = f"{aA} {sA} {aB} {sB}"[:32]
                l2 = f"{half}{num}" or "Live"

                _padres_cache.update({"ts": now, "have": True, "l1": l1, "l2": l2, "color": color})
                return True, l1, l2, color

        _padres_cache.update({"ts": now, "have": False, "l1": "", "l2": "", "color": WHITE})
        return False, "", "", WHITE

    except Exception as e:
        log.info(f"Padres fetch failed: {e}")
        _padres_cache.update({"ts": now, "have": False, "l1": "", "l2": "", "color": WHITE})
        return False, "", "", WHITE

# ===== Scrolling renderer with true margins and dots =====
def render_cycle_with_margins(matrix: RGBMatrix, font,
                              l1: str, l2: str, l3: str,
                              secs: float, margin: int,
                              dot1: Optional[graphics.Color],
                              dot2: Optional[graphics.Color]):
    end_time = time.time() + secs
    hold_ms = 1600
    step_ms = 80
    step_px = 1

    c = matrix.CreateFrameCanvas()
    viewport_w = matrix.width - 2 * margin

    def width(t: str) -> int:
        return graphics.DrawText(c, font, 0, 0, graphics.Color(0,0,0), t or "")

    w1 = width(l1 or "")
    w2 = width(l2 or "")
    w3 = width(l3 or "")

    l2_fits = (w2 <= viewport_w)
    l3_fits = (w3 <= viewport_w)
    l2_span = 0 if l2_fits else (w2 - viewport_w)
    l3_span = 0 if l3_fits else (w3 - viewport_w)

    def draw(off2: int = 0, off3: int = 0):
        c.Clear()

        # line one centered and clamped
        text1 = l1 or "NO TRAFFIC"
        w1_ = width(text1)
        x1 = clamp_center_x(matrix.width, w1_, margin)
        graphics.DrawText(c, font, x1, LINE1_Y, WHITE, text1)
        if dot1 is not None:
            right1 = min(matrix.width - margin - 1, x1 + w1_ + DOT_GAP_PX)
            draw_status_dot(c, right1, LINE1_Y, dot1)

        # line two with optional scroll
        if l2:
            if l2_fits:
                x2 = clamp_center_x(matrix.width, w2, margin)
                graphics.DrawText(c, font, x2, LINE2_Y, WHITE, l2)
                if dot2 is not None:
                    right2 = min(matrix.width - margin - 1, x2 + w2 + DOT_GAP_PX)
                    draw_status_dot(c, right2, LINE2_Y, dot2)
            else:
                x2 = margin - off2
                graphics.DrawText(c, font, x2, LINE2_Y, WHITE, l2)

        # line three with optional scroll
        if l3:
            if l3_fits:
                x3 = clamp_center_x(matrix.width, w3, margin)
                graphics.DrawText(c, font, x3, LINE3_Y, WHITE, l3)
            else:
                x3 = margin - off3
                graphics.DrawText(c, font, x3, LINE3_Y, WHITE, l3)

        matrix.SwapOnVSync(c)

    # if both non scrolling just hold
    if l2_fits and l3_fits:
        draw()
        time.sleep(max(0.0, end_time - time.time()))
        return

    # scrolling cycle
    while time.time() < end_time:
        # hold before scroll
        draw(0, 0)
        time.sleep(min(hold_ms/1000.0, max(0.0, end_time - time.time())))
        if time.time() >= end_time: break

        # scroll line two
        off2 = 0
        while off2 < l2_span and time.time() < end_time:
            draw(off2, 0)
            time.sleep(step_ms/1000.0)
            off2 += step_px

        # hold end of line two
        draw(l2_span, 0)
        time.sleep(min(hold_ms/1000.0, max(0.0, end_time - time.time())))
        if time.time() >= end_time: break

        # small pause
        draw(0, 0); time.sleep(0.2)
        if time.time() >= end_time: break

        # hold before line three scroll
        draw(0, 0)
        time.sleep(min(hold_ms/1000.0, max(0.0, end_time - time.time())))
        if time.time() >= end_time: break

        # scroll line three
        off3 = 0
        while off3 < l3_span and time.time() < end_time:
            draw(0, off3)
            time.sleep(step_ms/1000.0)
            off3 += step_px

        # hold end of line three
        draw(0, l3_span)
        time.sleep(min(hold_ms/1000.0, max(0.0, end_time - time.time())))
        if time.time() >= end_time: break

        draw(0, 0); time.sleep(0.2)

# ===== Matrix setup and font =====
def load_small_font():
    for p in FONT_SMALL_CANDIDATES:
        try:
            f = graphics.Font(); f.LoadFont(p)
            log.info("Loaded font %s", p)
            return f
        except Exception:
            continue
    raise RuntimeError("No BDF font found")

def setup_matrix():
    o = RGBMatrixOptions()
    o.rows, o.cols = MATRIX_ROWS, MATRIX_COLS
    o.chain_length, o.parallel = 1, 1
    o.hardware_mapping = HARDWARE_MAPPING
    o.pwm_bits = PWM_BITS
    o.pwm_lsb_nanoseconds = PWM_LSB_NS
    o.gpio_slowdown = GPIO_SLOWDOWN
    o.brightness = BRIGHTNESS
    if hasattr(o, "limit_refresh_rate_hz"):
        o.limit_refresh_rate_hz = LIMIT_REFRESH_HZ
    return RGBMatrix(options=o)

# ===== Picking =====
def pick_best(items):
    best, best_d = None, 1e12
    for it in items:
        if not altitude_ok(it.get("alt_ft")): continue
        if not within_corridor(it["lat"], it["lon"]): continue
        d,_ = point_to_segment_dist_m(it["lat"], it["lon"], P1_LAT, P1_LON, P2_LAT, P2_LON)
        if d < best_d: best, best_d = it, d
    if DEBUG: log.info("Pick %s", best.get("fn") if best else "None")
    return best

# ===== Main =====
def main():
    font = load_small_font()
    matrix = setup_matrix()
    n,s,w,e = corridor_bbox((P1_LAT,P1_LON),(P2_LAT,P2_LON),CORRIDOR_HALF_MILES)
    log.info(f"BBox {n:.6f},{s:.6f},{w:.6f},{e:.6f}")

    ENRICH_CACHE: Dict[str, dict] = {}

    while True:
        try:
            items = fetch_live_scrape(n, s, w, e)
            best = pick_best(items)

            if best:
                extra = ENRICH_CACHE.get(best["fid"], {})
                if (not extra) or (not (best.get("fn") or "").strip()):
                    extra = fetch_details_scrape(best["fid"]) or {}
                    ENRICH_CACHE[best["fid"]] = extra

                ident = (extra.get("callsign") or best.get("fn") or extra.get("registration") or "UNKNOWN").strip()
                delay_min = fetch_delay_minutes(best["fid"])
                status_dot = map_delay_to_color(delay_min)

                ac_name = extra.get("type_text") or ""
                if not ac_name:
                    ac_code = (extra.get("type") or "").upper()
                    if ac_code in AC_FULLNAME_MAP:
                        ac_name = AC_FULLNAME_MAP[ac_code]
                line2 = ac_name or ""
                line3 = airport_name_only(extra.get("dep_code"), extra.get("dep_name"), extra.get("dep_city"))

                render_cycle_with_margins(matrix, font, ident, line2, line3,
                                          POLL_INTERVAL_SEC, SIDE_MARGIN_PX,
                                          status_dot, None)
                continue

            show_padres, p1, p2, pcol = fetch_padres_score_lines()
            if show_padres:
                render_cycle_with_margins(matrix, font, p1 or "PADRES", p2, "",
                                          POLL_INTERVAL_SEC, SIDE_MARGIN_PX,
                                          pcol, None)
            else:
                tt, tc, wt = fetch_weather_simple()
                render_cycle_with_margins(matrix, font, "NO TRAFFIC", tt, wt,
                                          POLL_INTERVAL_SEC, SIDE_MARGIN_PX,
                                          None, tc)

        except KeyboardInterrupt:
            break
        except Exception as e:
            log.warning(f"Loop error {e}")
            time.sleep(2)

if __name__ == "__main__":
    main()
