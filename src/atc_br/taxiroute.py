"""Reconstruct how an aircraft taxied between two ADS-B fixes: the shortest route along SBGR's taxiways/taxilanes/runways.

The receiver loses aircraft on the ground for seconds to minutes. Between two known fixes the aircraft didn't glide across
the grass — it drove along the pavement. `fill(track)` inserts the reconstructed route as extra points so the renderer's
linear interpolation follows the taxiways. Real fixes are never removed or moved.
"""
import heapq
import json
import math
from functools import lru_cache
from pathlib import Path

import numpy as np
from scipy.spatial import cKDTree

ROOT = Path(__file__).resolve().parents[2]
STEP = 10.0          # node spacing along every centerline, m
JOIN = 9.0           # nodes of different lines closer than this are a junction
SNAP = 160.0         # a fix connects to graph nodes within this distance (aprons/stands sit off the centerlines)
RUNWAY_COST = 2.5    # prefer taxiways; runways only when they're clearly the way (backtrack, crossing)


@lru_cache(None)
def graph():
    A = json.loads((ROOT / "web" / "sbgr.json").read_text())["airport"]
    pts, kind, edges = [], [], []
    for k in ("taxiways", "taxilanes", "runways", "stopways"):
        for f in A[k]:
            line = f["p"]
            prev = None
            for (ax, ay), (bx, by) in zip(line, line[1:]):
                n = max(1, int(math.dist((ax, ay), (bx, by)) // STEP))
                for i in range(n + (1 if (bx, by) == tuple(line[-1]) else 0)):
                    u = i / n
                    pts.append((ax + (bx - ax) * u, ay + (by - ay) * u))
                    kind.append(k)
                    cur = len(pts) - 1
                    if prev is not None:
                        edges.append((prev, cur))
                    prev = cur
    P = np.array(pts)
    tree = cKDTree(P)
    for i, j in tree.query_pairs(JOIN):
        edges.append((i, j))
    adj = [[] for _ in P]
    for i, j in edges:
        w = float(np.hypot(*(P[i] - P[j])))
        rw = kind[i] in ("runways", "stopways") and kind[j] in ("runways", "stopways")
        adj[i].append((j, w, rw))
        adj[j].append((i, w, rw))
    return P, tree, adj


def route(a, b, runway_cost=RUNWAY_COST):
    """Shortest pavement route from point a to point b (local meters) as a list of points, or None.
    Taxiing aircraft avoid runways (runway_cost > 1); a landing/takeoff roll uses runway_cost=1."""
    P, tree, adj = graph()
    sa = tree.query_ball_point(a, SNAP)
    sb = set(tree.query_ball_point(b, SNAP))
    if not sa or not sb:
        return None
    # multi-source Dijkstra: entering the graph costs the straight distance from the fix to the node
    dist, prev, pq = {}, {}, []
    for i in sa:
        d0 = float(np.hypot(*(P[i] - a)))
        dist[i] = d0
        heapq.heappush(pq, (d0, i))
    best, end = math.inf, None
    while pq:
        d, i = heapq.heappop(pq)
        if d > dist.get(i, math.inf) or d >= best:
            continue
        if i in sb:
            tot = d + float(np.hypot(*(P[i] - b)))
            if tot < best:
                best, end = tot, i
        for j, w, rw in adj[i]:
            nd = d + (w * runway_cost if rw else w)
            if nd < dist.get(j, math.inf):
                dist[j], prev[j] = nd, i
                heapq.heappush(pq, (nd, j))
    if end is None:
        return None
    path = [end]
    while path[-1] in prev:
        path.append(prev[path[-1]])
    path.reverse()
    return [tuple(a)] + [tuple(P[i]) for i in path] + [tuple(b)]


def on_pavement(a, b, tol=28.0):
    """True if the straight line a->b never strays more than tol meters from a pavement centerline."""
    P, tree, _ = graph()
    n = max(2, int(math.dist(a, b) // 8))
    pts = [(a[0] + (b[0] - a[0]) * i / n, a[1] + (b[1] - a[1]) * i / n) for i in range(n + 1)]
    d, _ = tree.query(pts)
    return float(np.max(d)) <= tol


def _length(pl):
    return sum(math.dist(p, q) for p, q in zip(pl, pl[1:]))


def _along(pl, s):
    """Point and heading (deg, 0 = north, clockwise) at distance s along polyline pl."""
    for p, q in zip(pl, pl[1:]):
        L = math.dist(p, q)
        if s <= L or q is pl[-1]:
            u = 0 if L == 0 else min(1, s / L)
            hdg = (math.degrees(math.atan2(q[0] - p[0], -(q[1] - p[1]))) + 360) % 360   # y points south
            return (p[0] + (q[0] - p[0]) * u, p[1] + (q[1] - p[1]) * u), hdg
        s -= L
    return pl[-1], None


def destale(track):
    """Collapse a receiver repeating one frozen fix (same position, speed > 3 kt) into its first report, so the
    reconstruction starts the taxi from when the plane was really last seen there instead of pinning it then rushing."""
    out = []
    for p in track:
        if out and p[1:3] == out[-1][1:3] and p[4] > 3:
            continue
        out.append(p)
    return out


def fill(track, taxi_kt=(8, 22)):
    """Insert reconstructed taxi routes between consecutive ground fixes of one aircraft.

    track: sorted [(t, x, y, track_deg, gs, alt, ground, callsign, type)]. Returns a new list with every original fix
    kept as-is plus synthetic points along the pavement. Timing: if the aircraft was stopped at the first fix it waits
    there and then taxis so it arrives on time; if it was moving it taxis first and waits at the second fix.
    """
    out = []
    for a, b in zip(track, track[1:]):
        out.append(a)
        if not (a[6] and b[6]):
            continue
        dt = b[0] - a[0]
        chord = math.dist(a[1:3], b[1:3])
        if chord < 25 or dt <= 0:
            continue
        if dt < 30 and on_pavement(a[1:3], b[1:3]):
            continue                                   # short hop that stays on the pavement (rolls, straight taxi): keep it
        pl = route(a[1:3], b[1:3], 1.0 if max(a[4], b[4]) > 35 else RUNWAY_COST)   # fast = still rolling on the runway
        if not pl:
            continue
        L = _length(pl)
        if L < chord * 1.03 and dt < 25:
            continue                                   # the straight line already is the route
        reported = max(a[4], b[4]) * .5144
        v = max(taxi_kt[0] * .5144, min(taxi_kt[1] * .5144, reported if reported > 1.5 else 12 * .5144))
        move = min(dt, L / v)                          # seconds actually taxiing
        if L / dt > v:                                 # had to be quicker than our guess to make it on time
            move = dt
        start = a[0] + (dt - move if a[4] < 3 else 0)  # stopped at A -> wait first; moving -> go first, wait at B
        if start > a[0] + .5:                          # parked at A until it starts taxiing
            out.append((start, a[1], a[2], a[3], 0, a[5], True, a[7], a[8]))
        n = max(2, int(move))                          # about one point per second of taxiing
        for i in range(1, n):
            s = L * i / n
            (x, y), hdg = _along(pl, s)
            out.append((start + move * i / n, x, y, hdg, L / move / .5144, a[5], True, a[7] or b[7], a[8] or b[8]))
        if start + move < b[0] - .5:                   # arrived early: parked at B until the real fix
            out.append((start + move, b[1], b[2], b[3], 0, b[5], True, b[7], b[8]))
    if track:
        out.append(track[-1])
    return out
