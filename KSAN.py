#!/usr/bin/env python3
# KSAN single line board with camera friendly timing and baseline nudge
# Callsign centered in white with a 7 px status dot to the right
# Else Padres when tied or winning (LIVE only)
# Else weather "72F  9mph" with a dot
# Smooth scrolling only when text exceeds the viewport

import os
import time
import math
import logging
from datetime import datetime
from typing import Optional, List, Tuple

import requests
from rgbmatrix import RGBMatrix, RGBMatrixOptions, graphics

# ===== Matrix config =====
MATRIX_ROWS, MATRIX_COLS = 32, 64
CHAIN_LENGTH, PARALLEL = 1, 1
HARDWARE_MAPPING = "adafruit-hat"

# Camera profile override at launch: KSAN_PROFILE=1|2|3
CAMERA_PROFILE = int(os.environ.get("KSAN_PROFILE", "1"))

def apply_camera_profile(o: RGBMatrixOptions):
    if CAMERA_PROFILE == 1:
        o.pwm_bits = 8
        o.pwm_lsb_nanoseconds = 50
        o.brightness = 40
        o.gpio_slowdown = 2
        o.limit_refresh_rate_hz = 240
        o.disable_hardware_pulsing = True
        o.scan_mode = 0
    elif CAMERA_PROFILE == 2:
        o.pwm_bits = 7
        o.pwm_lsb_nanoseconds = 40
        o.brightness = 35
        o.gpio_slowdown = 2
        o.limit_refresh_rate_hz = 288
        o.disable_hardware_pulsing = True
        o.scan_mode = 1
    else:
        o.pwm_bits = 9
        o.pwm_lsb_nanoseconds = 70
        o.brightness = 50
        o.gpio_slowdown = 3
        o.limit_refresh_rate_hz = 200
        o.disable_hardware_pulsing = False
        o.scan_mode = 0

# Font candidates
FONT_SMALL_CANDIDATES = [
    "/home/henry/rpi-rgb-led-matrix/fonts/6x10.bdf",
    "/usr/local/share/rgbmatrix/fonts/6x10.bdf",
    "/home/henry/rpi-rgb-led-matrix/fonts/5x8.bdf",
]

# Text baseline and dot
BASELINE_NUDGE_Y = -1
DOT_SIZE = 7
DOT_GAP = 3

# ===== KSAN corridor =====
P1_LAT, P1_LON = 32.69079521369225, -117.00828355500153
P2_LAT, P2_LON = 32.72663595133696, -117.15939039340249
CORRIDOR_HALF_METERS = 0.5 * 1609.344
ALT_FT_MIN = 200.0
ALT_FT_MAX = 4000.0
REQUIRE_ALT = True

POLL_INTERVAL_SEC = 30
HTTP_TIMEOUT_SEC = 8

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
BROWSER_HEADERS = {
    "User-Agent": "Mozilla/5.0",
    "Accept": "application/json,text/plain,*/*",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": "https://www.flightradar24.com/",
    "Origin": "https://www.flightradar24.com",
    "Cache-Control": "no-cache",
}
DETAILS_HEAD = "https://data-live.flightradar24.com/clickhandler/?flight="
_SESS = requests.Session()
_SESS.headers.update(BROWSER_HEADERS)

# ===== Weather simple =====
WEATHER_LAT, WEATHER_LON = 32.7195, -117.1339
WEATHERAPI_KEY = "ffe0bd3b204f429b80f00400251408"
WEATHER_CACHE_TTL = 900
_weather_cache = {"ts": 0.0, "temp_f": None, "wind_mph": None}

# ===== Colors =====
def _col(r, g, b): return graphics.Color(r, g, b)
WHITE = _col(255, 255, 255)
GREEN = _col(0, 255, 0)
YELLOW = _col(255, 255, 0)
RED = _col(255, 0, 0)
CYAN = _col(0, 255, 255)
DIM = _col(80, 80, 80)

# ===== Logging =====
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s: %(message)s")
log = logging.getLogger("KSAN_board")

# ===== Geo helpers =====
def to_local_xy(lat, lon, lat0):
    mlat = 111320.0
    mlon = 111320.0 * math.cos(math.radians(lat0))
    return lon * mlon, lat * mlat

def point_to_segment_dist_m(lat, lon, a_lat, a_lon, b_lat, b_lon):
    lat0 = 0.5 * (a_lat + b_lat)
    ax, ay = to_local_xy(a_lat, a_lon, lat0)
    bx, by = to_local_xy(b_lat, b_lon, lat0)
    px, py = to_local_xy(lat, lon, lat0)
    vx, vy = bx - ax, by - ay
    wx, wy = px - ax, py - ay
    v2 = vx * vx + vy * vy
    if v2 <= 1e-6:
        return math.hypot(px - ax, py - ay), 0.0
    t = max(0.0, min(1.0, (wx * vx + wy * vy) / v2))
    cx, cy = ax + t * vx, ay + t * vy
    return math.hypot(px - cx, py - cy), t

def altitude_ok(alt_ft: Optional[float]) -> bool:
    if alt_ft is None:
        return not REQUIRE_ALT
    return ALT_FT_MIN <= alt_ft <= ALT_FT_MAX

def within_corridor(lat, lon) -> bool:
    d_m, t = point_to_segment_dist_m(lat, lon, P1_LAT, P1_LON, P2_LAT, P2_LON)
    return (0.0 <= t <= 1.0) and (d_m <= CORRIDOR_HALF_METERS)

def corridor_bbox():
    lat1, lon1 = P1_LAT, P1_LON
    lat2, lon2 = P2_LAT, P2_LON
    mid = 0.5 * (lat1 + lat2)
    dlat = (0.5 + 0.1) / 69.0
    dlon = (0.5 + 0.1) / (69.0 * max(0.1, math.cos(math.radians(mid))))
    north = max(lat1, lat2) + dlat
    south = min(lat1, lat2) - dlat
    west = min(lon1, lon2) - dlon
    east = max(lon1, lon2) + dlon
    return north, south, west, east

# ===== FR24 =====
def _feed_url(host, north, south, west, east):
    return f"{host}{FEED_PATH}?bounds={north:.6f},{south:.6f},{west:.6f},{east:.6f}{FEED_TAIL}&_ts={int(time.time())}"

def fetch_live_scrape(north, south, west, east) -> List[dict]:
    last_err = None
    for host in FEED_HOSTS:
        url = _feed_url(host, north, south, west, east)
        try:
            r = _SESS.get(url, timeout=HTTP_TIMEOUT_SEC)
            if r.status_code == 403:
                last_err = f"{host} 403"; continue
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
            return out
        except Exception as e:
            last_err = f"{host} {e}"; continue
    if last_err:
        log.warning(f"Feed scrape error {last_err}")
    return []

# ===== Padres (LIVE only, tied or winning) =====
def padres_winning_or_tied_live() -> Optional[Tuple[str, int, int]]:
    try:
        today = datetime.now().strftime("%Y%m%d")
        url = "https://site.api.espn.com/apis/site/v2/sports/baseball/mlb/scoreboard"
        r = requests.get(url, params={"dates": today}, timeout=6)
        r.raise_for_status()
        j = r.json()
        for ev in j.get("events", []):
            comp = ev.get("competitions", [{}])[0]
            status = (comp.get("status") or {}).get("type") or {}
            state = str(status.get("state") or "").lower()
            if state not in ("in", "inprogress", "live"):
                continue
            teams = comp.get("competitors", [])
            names = {t["team"]["abbreviation"]: t for t in teams if "team" in t}
            if "SD" not in names:
                continue
            sd = int(names["SD"]["score"])
            other = next((t for t in teams if t["team"]["abbreviation"] != "SD"), None)
            if not other:
                continue
            opp = other["team"]["abbreviation"]
            op = int(other["score"])
            if sd >= op:
                return (opp, sd, op)
    except Exception as e:
        log.info(f"Padres fetch failed {e}")
    return None

# ===== Weather minimal =====
def weather_dot_color(t_f: int, w_mph: int) -> graphics.Color:
    def temp_color(t):
        if t < 40: return CYAN
        if t <= 85: return GREEN
        if t <= 95: return YELLOW
        return RED
    def wind_color(w):
        if w < 10: return GREEN
        if w <= 20: return YELLOW
        return RED
    c1 = temp_color(t_f); c2 = wind_color(w_mph)
    s1 = c1.red + c1.green + c1.blue
    s2 = c2.red + c2.green + c2.blue
    return c2 if s2 >= s1 else c1

def fetch_weather_simple() -> Optional[Tuple[int, int, graphics.Color]]:
    now = time.time()
    if now - _weather_cache["ts"] < WEATHER_CACHE_TTL and _weather_cache["temp_f"] is not None:
        t = int(round(_weather_cache["temp_f"]))
        w = int(round(_weather_cache["wind_mph"] or 0))
        return t, w, weather_dot_color(t, w)
    try:
        js = requests.get(
            "https://api.weatherapi.com/v1/current.json",
            params={"key": WEATHERAPI_KEY, "q": f"{WEATHER_LAT},{WEATHER_LON}", "aqi": "no"},
            timeout=6
        ).json()
        cur = js.get("current", {}) or {}
        temp_f = cur.get("temp_f")
        wind_mph = cur.get("wind_mph")
        if isinstance(temp_f, (int, float)) and isinstance(wind_mph, (int, float)):
            _weather_cache.update({"ts": now, "temp_f": temp_f, "wind_mph": wind_mph})
            t = int(round(temp_f)); w = int(round(wind_mph))
            return t, w, weather_dot_color(t, w)
    except Exception as e:
        log.info(f"Weather fetch failed {e}")
    return None

# ===== Draw helpers =====
def measure_text_width(canvas, font: graphics.Font, text: str) -> int:
    # Draw into the provided canvas at 0, this also gives us pixel width
    return graphics.DrawText(canvas, font, 0, font.height, WHITE, text or "")

def draw_seven_px_dot(canvas, x_left, y_baseline, font, color):
    size = DOT_SIZE
    h = font.height
    y_top = y_baseline - h + (h - size) // 2
    for yy in range(y_top, y_top + size):
        for xx in range(x_left, x_left + size):
            canvas.SetPixel(xx, yy, color.red, color.green, color.blue)

def draw_center_or_scroll(matrix: RGBMatrix, canvas, font: graphics.Font, text: str, color: graphics.Color,
                          hold_ms: int = 1200, step_ms: int = 16, step_px: int = 1) -> int:
    """
    Draws centered if it fits; otherwise scrolls smoothly right→left.
    Returns X coordinate of right edge where the dot should start.
    """
    viewport_w = matrix.width
    y = font.height + BASELINE_NUDGE_Y

    # measure on a cleared canvas
    canvas.Clear()
    tw = measure_text_width(canvas, font, text)
    canvas.Clear()

    if tw <= viewport_w:
        x0 = (viewport_w - tw) // 2
        x1 = graphics.DrawText(canvas, font, x0, y, color, text)
        matrix.SwapOnVSync(canvas)
        return x1

    # short hold centered
    x0 = (viewport_w - tw) // 2
    graphics.DrawText(canvas, font, x0, y, color, text)
    matrix.SwapOnVSync(canvas)
    time.sleep(hold_ms / 1000.0)

    # scrolling phase
    offset = 0
    span = tw - viewport_w
    while offset < span:
        canvas.Clear()
        x_scroll = -offset
        graphics.DrawText(canvas, font, x_scroll, y, color, text)
        matrix.SwapOnVSync(canvas)
        time.sleep(step_ms / 1000.0)
        offset += step_px

    # final hold at end
    canvas.Clear()
    graphics.DrawText(canvas, font, -span, y, color, text)
    matrix.SwapOnVSync(canvas)
    time.sleep(hold_ms / 1000.0)
    return viewport_w - 1

# Mode override at launch: KSAN_MODE=auto|flight|padres|weather
START_MODE = os.environ.get("KSAN_MODE", "auto").lower().strip()

# ===== Matrix and tolerant font loader =====
def setup_matrix() -> Tuple[RGBMatrix, graphics.Font, any]:
    o = RGBMatrixOptions()
    o.rows, o.cols = MATRIX_ROWS, MATRIX_COLS
    o.chain_length, o.parallel = CHAIN_LENGTH, PARALLEL
    o.hardware_mapping = HARDWARE_MAPPING
    apply_camera_profile(o)
    matrix = RGBMatrix(options=o)

    # one reusable offscreen canvas for the whole program
    off = matrix.CreateFrameCanvas()

    override = os.environ.get("KSAN_FONT")
    candidates = [override] + FONT_SMALL_CANDIDATES if override else FONT_SMALL_CANDIDATES

    last_err = ""
    for p in candidates:
        try:
            f = graphics.Font()
            f.LoadFont(p)                      # ignore boolean
            off.Clear()
            graphics.DrawText(off, f, 0, f.height, WHITE, ".")  # verify draw works
            log.info("Loaded font %s", p)
            off.Clear()
            return matrix, f, off
        except Exception as e:
            last_err = f"{p} {e}"
            continue

    raise RuntimeError(f"No BDF font could be loaded. Last error {last_err}")

# ===== Main loop =====
def main():
    matrix, font, off = setup_matrix()
    north, south, west, east = corridor_bbox()
    last_poll = 0.0
    last_best: Optional[dict] = None
    mode = START_MODE if START_MODE in ("auto", "flight", "padres", "weather") else "auto"

    while True:
        ts = time.time()
        if ts - last_poll > POLL_INTERVAL_SEC:
            last_poll = ts
            flights = fetch_live_scrape(north, south, west, east)
            best = None
            best_d = 1e12
            for it in flights:
                if not altitude_ok(it.get("alt_ft")): continue
                if not within_corridor(it["lat"], it["lon"]): continue
                d_m, _ = point_to_segment_dist_m(it["lat"], it["lon"], P1_LAT, P1_LON, P2_LAT, P2_LON)
                if d_m < best_d:
                    best = it; best_d = d_m
            last_best = best

        def show_flight_once() -> bool:
            off.Clear()
            if last_best and (last_best.get("fn") or "").strip():
                callsign = last_best["fn"].strip()
                right = draw_center_or_scroll(matrix, off, font, callsign, WHITE)
                draw_seven_px_dot(off, right + DOT_GAP, font.height + BASELINE_NUDGE_Y, font, GREEN)
                matrix.SwapOnVSync(off)
                return True
            return False

        def show_padres_once() -> bool:
            off.Clear()
            ps = padres_winning_or_tied_live()
            if ps:
                opp, sd, op = ps
                line = f"SD {sd}  {opp} {op}"
                right = draw_center_or_scroll(matrix, off, font, line, WHITE)
                draw_seven_px_dot(off, right + DOT_GAP, font.height + BASELINE_NUDGE_Y, font,
                                  GREEN if sd > op else YELLOW)
                matrix.SwapOnVSync(off)
                return True
            return False

        def show_weather_once() -> bool:
            off.Clear()
            w = fetch_weather_simple()
            if w:
                t_f, w_mph, dot = w
                txt = f"{t_f}F  {w_mph}mph"
                right = draw_center_or_scroll(matrix, off, font, txt, WHITE)
                draw_seven_px_dot(off, right + DOT_GAP, font.height + BASELINE_NUDGE_Y, font, dot)
                matrix.SwapOnVSync(off)
                return True
            return False

        if mode == "flight":
            if not show_flight_once():
                off.Clear()
                graphics.DrawText(off, font, (matrix.width // 2) - 18,
                                  font.height + BASELINE_NUDGE_Y, DIM, "NO FLIGHT")
                matrix.SwapOnVSync(off)
            time.sleep(0.2)
            continue

        if mode == "padres":
            if not show_padres_once():
                off.Clear()
                graphics.DrawText(off, font, (matrix.width // 2) - 14,
                                  font.height + BASELINE_NUDGE_Y, DIM, "NO GAME")
                matrix.SwapOnVSync(off)
            time.sleep(0.5)
            continue

        if mode == "weather":
            if not show_weather_once():
                off.Clear()
                graphics.DrawText(off, font, (matrix.width // 2) - 12,
                                  font.height + BASELINE_NUDGE_Y, DIM, "NO DATA")
                matrix.SwapOnVSync(off)
            time.sleep(0.5)
            continue

        # auto priority
        if show_flight_once():
            time.sleep(0.02); continue
        if show_padres_once():
            time.sleep(0.5); continue
        if show_weather_once():
            time.sleep(0.5); continue

        off.Clear()
        graphics.DrawText(off, font, (matrix.width // 2) - 10,
                          font.height + BASELINE_NUDGE_Y, DIM, "IDLE")
        matrix.SwapOnVSync(off)
        time.sleep(0.5)

if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        pass
