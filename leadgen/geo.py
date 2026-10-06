"""Geometry for planning: distances, search squares (quadtree), Hilbert ordering, balanced splitting."""
from __future__ import annotations

import math
from dataclasses import dataclass, field

EARTH_R_KM = 6371.0088


def haversine_km(lat1: float, lng1: float, lat2: float, lng2: float) -> float:
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lng2 - lng1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * EARTH_R_KM * math.asin(min(1.0, math.sqrt(a)))


def km_per_deg(lat: float) -> tuple[float, float]:
    """(km per degree latitude, km per degree longitude) at latitude `lat`."""
    phi = math.radians(lat)
    klat = (111132.954 - 559.822 * math.cos(2 * phi) + 1.175 * math.cos(4 * phi)) / 1000.0
    klng = (111412.84 * math.cos(phi) - 93.5 * math.cos(3 * phi) + 0.118 * math.cos(5 * phi)) / 1000.0
    return klat, max(klng, 1e-6)


def offset(lat: float, lng: float, north_km: float, east_km: float) -> tuple[float, float]:
    klat, klng = km_per_deg(lat)
    return lat + north_km / klat, lng + east_km / klng


def square_bbox(lat: float, lng: float, size_km: float) -> tuple[float, float, float, float]:
    """(south, west, north, east) of a square of side size_km centred on (lat, lng)."""
    h = size_km / 2.0
    s, w = offset(lat, lng, -h, -h)
    n, e = offset(lat, lng, h, h)
    return s, w, n, e


def bearing_deg(lat1, lng1, lat2, lng2) -> float:
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dl = math.radians(lng2 - lng1)
    x = math.sin(dl) * math.cos(p2)
    y = math.cos(p1) * math.sin(p2) - math.sin(p1) * math.cos(p2) * math.cos(dl)
    return (math.degrees(math.atan2(x, y)) + 360) % 360


_DIRS = ["North", "North-East", "East", "South-East", "South", "South-West", "West", "North-West"]


def direction_name(lat0, lng0, lat, lng) -> str:
    d = haversine_km(lat0, lng0, lat, lng)
    if d < 2:
        return "Centre"
    return f"{_DIRS[int(((bearing_deg(lat0, lng0, lat, lng) + 22.5) % 360) // 45)]} {d:.0f} km"


@dataclass
class Cell:
    lat: float
    lng: float
    size_km: float
    count: int = 0
    depth: int = 0
    names: list = field(default_factory=list)

    def contains(self, lat: float, lng: float) -> bool:
        s, w, n, e = square_bbox(self.lat, self.lng, self.size_km)
        return s <= lat < n and w <= lng < e


def build_cells(center_lat: float, center_lng: float, radius_km: float, points: list[tuple[float, float]],
                min_cell_km: float, max_cell_km: float, split_threshold: int) -> list[Cell]:
    """Quadtree over the square that encloses the circle.

    A square is split while it is bigger than max_cell_km, or while it holds more
    than split_threshold mapped businesses and halving keeps it >= min_cell_km.
    Squares that do not touch the circle are dropped.
    """
    klat, klng = km_per_deg(center_lat)
    # Work in a local km grid (equirectangular around the centre) - accurate enough for <= 300 km.
    pts = [((lng - center_lng) * klng, (lat - center_lat) * klat) for lat, lng in points]
    leaves: list[Cell] = []

    def touches_circle(cx, cy, size):
        h = size / 2
        dx = max(abs(cx) - h, 0.0)
        dy = max(abs(cy) - h, 0.0)
        return math.hypot(dx, dy) <= radius_km

    def recurse(cx, cy, size, idx, depth):
        if not touches_circle(cx, cy, size):
            return
        count = len(idx)
        must_split = size > max_cell_km + 1e-9
        may_split = count > split_threshold and size / 2 >= min_cell_km - 1e-9
        if (must_split or may_split) and depth < 20:
            h = size / 2
            q = h / 2
            buckets = {(0, 0): [], (0, 1): [], (1, 0): [], (1, 1): []}
            for i in idx:
                x, y = pts[i]
                buckets[(1 if x >= cx else 0, 1 if y >= cy else 0)].append(i)
            for (bx, by), sub in buckets.items():
                recurse(cx + (q if bx else -q), cy + (q if by else -q), h, sub, depth + 1)
            return
        lat = center_lat + cy / klat
        lng = center_lng + cx / klng
        leaves.append(Cell(lat=lat, lng=lng, size_km=size, count=count, depth=depth))

    # Root square side: smallest power-of-two multiple of min_cell that covers the circle diameter,
    # so leaves come out at clean sizes (e.g. 1, 2, 4, 8 km).
    side = min_cell_km
    while side < 2 * radius_km:
        side *= 2
    inside = [i for i, (x, y) in enumerate(pts) if math.hypot(x, y) <= radius_km]
    recurse(0.0, 0.0, side, inside, 0)
    return leaves


# --------------------------------------------------------------------------
# Hilbert curve ordering keeps consecutive cells next to each other on the map
# --------------------------------------------------------------------------
def hilbert_index(order: int, x: int, y: int) -> int:
    n = 1 << order
    d = 0
    s = n >> 1
    while s > 0:
        rx = 1 if (x & s) > 0 else 0
        ry = 1 if (y & s) > 0 else 0
        d += s * s * ((3 * rx) ^ ry)
        if ry == 0:
            if rx == 1:
                x = s - 1 - x
                y = s - 1 - y
            x, y = y, x
        s >>= 1
    return d


def hilbert_sort(cells: list[Cell], order: int = 16) -> list[Cell]:
    if not cells:
        return []
    lats = [c.lat for c in cells]
    lngs = [c.lng for c in cells]
    lat0, lat1, lng0, lng1 = min(lats), max(lats), min(lngs), max(lngs)
    span = max(lat1 - lat0, lng1 - lng0, 1e-9)
    n = (1 << order) - 1

    def key(c: Cell):
        x = int((c.lng - lng0) / span * n)
        y = int((c.lat - lat0) / span * n)
        return hilbert_index(order, x, y)

    return sorted(cells, key=key)


def split_balanced(items: list, weights: list[float], n: int) -> list[list]:
    """Split an ordered list into n consecutive groups with roughly equal total weight."""
    if not items:
        return []
    n = max(1, min(n, len(items)))
    total = float(sum(weights)) or float(len(items))
    if not sum(weights):
        weights = [1.0] * len(items)
    target = total / n
    groups: list[list] = [[]]
    acc = 0.0
    remaining_items = len(items)
    for item, w in zip(items, weights):
        groups[-1].append(item)
        acc += w
        remaining_items -= 1
        groups_left = n - len(groups)
        # Close the group when its cumulative weight reaches its share, but always leave
        # at least one item for every group still to be created.
        if groups_left > 0 and (acc >= target * len(groups) or remaining_items <= groups_left):
            if remaining_items > 0:
                groups.append([])
    return [g for g in groups if g]
