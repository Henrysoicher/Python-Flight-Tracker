#!/usr/bin/env python3
# KSAN approach corridor viewer using FR24 scrape with simple weather and Padres overlay
# MLB view shows stacked left aligned lines with Padres on top
# Inning status is drawn at top right
# No status dots are drawn on the MLB screen

import math, time, logging, requests
from typing import Optional, List, Dict, Tuple
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
    "&faa=1&
