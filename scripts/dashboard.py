import argparse
import json
import math
import os
import random
import urllib.request
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

TZ = ZoneInfo("Europe/Lisbon")
API = "https://api.github.com/graphql"
MONTHS = ["jan", "fev", "mar", "abr", "mai", "jun", "jul", "ago", "set", "out", "nov", "dez"]
FONT = "'JetBrains Mono','SF Mono',SFMono-Regular,Menlo,Consolas,'Liberation Mono',monospace"

PALETTES = {
    "dark": {
        "bg": "#0A0E14", "border": "#30363D", "fg": "#E6EDF3", "muted": "#7D8590", "acc": "#8FB4DB",
        "levels": ["#1F2631", "#24456B", "#2F6299", "#5E8DC0", "#A9C8EA"],
    },
    "light": {
        "bg": "#F6F8FA", "border": "#D0D7DE", "fg": "#1F2328", "muted": "#656D76", "acc": "#2C5C8F",
        "levels": ["#E1E6EC", "#B7CCE3", "#8FB4DB", "#5E8DC0", "#2C5C8F"],
    },
}

CYCLE = 10.0
T_SWEEP = (1.0, 3.6)
T_TRACE = (3.6, 5.8)
T_DEPLOY = 6.5
T_SETTLE = 8.0
T_RESET = 9.7
NODES = [
    (0.3, 0.9),
    (0.9, T_SWEEP[1]),
    (T_TRACE[0], T_TRACE[1]),
    (T_TRACE[1], T_DEPLOY),
]


@dataclass
class Activity:
    days: list
    languages: list
    ci: float | None
    hours: list | None
    today: date


def graphql(query, variables, token):
    body = json.dumps({"query": query, "variables": variables}).encode()
    req = urllib.request.Request(API, data=body, headers={
        "Authorization": f"bearer {token}",
        "Content-Type": "application/json",
        "User-Agent": "ndnrs-dashboard",
    })
    with urllib.request.urlopen(req, timeout=30) as resp:
        payload = json.load(resp)
    if payload.get("errors"):
        raise RuntimeError(payload["errors"])
    return payload["data"]


Q_PROFILE = """
query($login: String!) {
  user(login: $login) {
    id
    contributionsCollection {
      contributionCalendar { weeks { contributionDays { date contributionCount } } }
    }
    repositories(first: 100, ownerAffiliations: OWNER, isFork: false,
                 orderBy: {field: PUSHED_AT, direction: DESC}) {
      nodes {
        name
        pushedAt
        languages(first: 10, orderBy: {field: SIZE, direction: DESC}) {
          edges { size node { name } }
        }
      }
    }
  }
}
"""


def profile_query(own):
    if own:
        return Q_PROFILE.replace("query($login: String!) {\n  user(login: $login) {", "query {\n  viewer {")
    return Q_PROFILE


def history_query(batch):
    parts = []
    for i, (name, cursor) in enumerate(batch):
        after = f", after: {json.dumps(cursor)}" if cursor else ""
        parts.append(
            f"r{i}: repository(owner: $login, name: {json.dumps(name)}) {{ defaultBranchRef {{ target {{ ... on Commit {{"
            f" history(first: 100, since: $since, author: {{id: $uid}}{after}) {{"
            " pageInfo { hasNextPage endCursor } nodes { committedDate } }"
            " latest: history(first: 1) { nodes { statusCheckRollup { state } } } } } } }"
        )
    return "query($login: String!, $since: GitTimestamp!, $uid: ID!) {" + " ".join(parts) + "}"


def commits_of(login, uid, names, since, token, max_queries=40):
    dates, latest = [], {}
    pending = [(name, None) for name in names]
    while pending and max_queries:
        batch, pending, max_queries = pending[:10], pending[10:], max_queries - 1
        data = graphql(history_query(batch), {"login": login, "since": since.isoformat(), "uid": uid}, token)
        for i, (name, cursor) in enumerate(batch):
            target = ((data.get(f"r{i}") or {}).get("defaultBranchRef") or {}).get("target") or {}
            history = target.get("history") or {}
            dates += [datetime.fromisoformat(c["committedDate"]) for c in history.get("nodes", [])]
            if cursor is None:
                for c in (target.get("latest") or {}).get("nodes", []):
                    latest[name] = (c["statusCheckRollup"] or {}).get("state")
            if (history.get("pageInfo") or {}).get("hasNextPage"):
                pending.append((name, history["pageInfo"]["endCursor"]))
    return dates, latest


def fetch(login, token):
    now = datetime.now(timezone.utc)
    today = now.astimezone(TZ).date()
    try:
        own = graphql("query { viewer { login } }", {}, token)["viewer"]["login"].lower() == login.lower()
    except Exception:
        own = False
    print("dados via token pessoal" if own else "dados via token do workflow (só o que é público)")
    data = graphql(profile_query(own), {} if own else {"login": login}, token)
    user = data["viewer" if own else "user"]

    repos = user["repositories"]["nodes"]
    sizes = {}
    for repo in repos:
        for edge in repo["languages"]["edges"]:
            sizes[edge["node"]["name"]] = sizes.get(edge["node"]["name"], 0) + edge["size"]

    start = today - timedelta(days=52 * 7 + (today.weekday() + 1) % 7)
    since = datetime.combine(start, datetime.min.time(), TZ)
    pushed = {r["name"]: datetime.fromisoformat(r["pushedAt"]) for r in repos if r["pushedAt"]}
    active = [name for name, t in pushed.items() if t >= since]
    dates, latest = commits_of(login, user["id"], active, since, token) if active else ([], {})

    days = [
        (date.fromisoformat(d["date"]), d["contributionCount"])
        for week in user["contributionsCollection"]["contributionCalendar"]["weeks"]
        for d in week["contributionDays"]
    ]
    if not any(c for _, c in days):
        per_day = {}
        for t in dates:
            d = t.astimezone(TZ).date()
            per_day[d] = per_day.get(d, 0) + 1
        days = [(start + timedelta(days=i), per_day.get(start + timedelta(days=i), 0))
                for i in range((today - start).days + 1)]
        print(f"calendário privado: dias contados a partir de {len(dates)} commits")
    if not any(c for _, c in days):
        raise SystemExit("sem contribuições para mostrar; o painel anterior fica como está")

    recent = now - timedelta(days=90)
    states = [st for name, st in latest.items() if pushed[name] >= recent and st in ("SUCCESS", "FAILURE", "ERROR")]
    ci = states.count("SUCCESS") / len(states) if states else None

    hours = [0] * 24
    recent_dates = [t for t in dates if t >= recent]
    for t in recent_dates:
        hours[t.astimezone(TZ).hour] += 1

    return Activity(
        days=days,
        languages=language_shares(sizes),
        ci=ci,
        hours=hours if len(recent_dates) >= 15 else None,
        today=today,
    )


def demo():
    rnd = random.Random(1985)
    today = date(2026, 10, 24)
    start = today - timedelta(days=52 * 7 + (today.weekday() + 1) % 7)
    days = []
    for i in range((today - start).days + 1):
        d = start + timedelta(days=i)
        dow, wk = (d.weekday() + 1) % 7, i // 7
        phase = 0.55 + 0.4 * math.sin(wk / 5.5) + (0.25 if wk > 40 else 0)
        vacation = d.month == 8 and 4 <= d.day <= 19
        p_active = 0.55 if dow in (0, 6) else 0.95
        count = 0
        if not vacation and rnd.random() < p_active * min(1, phase + 0.45):
            count = 1 + int(rnd.random() ** 1.6 * 14 * phase)
        days.append((d, count))
    sizes = {"TypeScript": 51, "Python": 29, "JavaScript": 15, "PLpgSQL": 2, "HTML": 1.2, "CSS": 0.8, "Shell": 0.6, "Dockerfile": 0.4}
    return Activity(days=days, languages=language_shares(sizes), ci=0.98, hours=None, today=today)


def language_shares(sizes, top=6):
    total = sum(sizes.values())
    if not total:
        return []
    ranked = sorted(sizes.items(), key=lambda kv: kv[1], reverse=True)
    rows = ranked[:top]
    rest = sum(v for _, v in ranked[top:])
    if rest:
        rows.append(("Outras", rest))
    raw = [(name, v / total * 100) for name, v in rows]
    shares = [[name, int(p)] for name, p in raw]
    by_remainder = sorted(range(len(raw)), key=lambda i: raw[i][1] - int(raw[i][1]), reverse=True)
    for i in by_remainder[:100 - sum(s for _, s in shares)]:
        shares[i][1] += 1
    return [(name, share) for name, share in shares]


def levels(days):
    counts = sorted(c for _, c in days if c)
    if not counts:
        return [0] * len(days)
    q = [counts[int(len(counts) * f)] for f in (0.25, 0.5, 0.75)]
    return [0 if c == 0 else 1 + sum(c > t for t in q) for _, c in days]


def day_shape(hours):
    if hours:
        smooth = [
            sum(hours[(h + k) % 24] * math.exp(-k * k / 10) for k in range(-6, 7))
            for h in range(24)
        ]
        peak = max(smooth)
        bins = [0.03 + 0.97 * v / peak for v in smooth]
        return lambda h: _interp(bins, h)

    def g(h, m, s):
        return math.exp(-((h - m) ** 2) / (2 * s * s))

    def neutral(h):
        return 0.03 + g(h, 11, 1.9) * 0.8 + g(h, 16, 2.4) + g(h, 22, 1.6) * 0.75

    peak = max(neutral(h / 4) for h in range(97))
    return lambda h: neutral(h) / peak


def _interp(bins, h):
    i = int(math.floor(h)) % 24
    f = h - math.floor(h)
    return bins[i] * (1 - f) + bins[(i + 1) % 24] * f


def summarize(act):
    counts = [c for _, c in act.days]
    this_week = sum(counts[-7:])
    prev = [sum(counts[-7 * (k + 1):-7 * k]) for k in range(1, 13)]
    avg = sum(prev) / len(prev)
    heat = this_week / avg if avg else (1.0 if this_week else 0.0)
    temp = 1 if heat < 0.5 else 2 if heat < 0.8 else 3 if heat < 1 else 4 if heat < 1.3 else 5
    return {
        "this_week": this_week,
        "temp": temp,
        "temp_label": "idle" if temp <= 1 else "warm" if temp <= 3 else "cooking",
        "version": f"v{act.today:%y}.{act.today:%m}",
    }


def pct(t):
    return f"{t / CYCLE * 100:.2f}%"


def steps(name, prop, start, changes, ramp=0.25):
    frames, cur = [f"0%{{{prop}:{start}}}"], start
    for t, value in changes:
        frames.append(f"{pct(t)}{{{prop}:{cur}}}")
        frames.append(f"{pct(min(CYCLE, t + ramp))}{{{prop}:{value}}}")
        cur = value
    frames.append(f"100%{{{prop}:{cur}}}")
    return f"@keyframes {name}{{{''.join(frames)}}}"


def mix(a, b, t):
    ca, cb = (tuple(int(c[i:i + 2], 16) for i in (1, 3, 5)) for c in (a, b))
    return "#" + "".join(f"{round(x + (y - x) * t):02X}" for x, y in zip(ca, cb))


def esc(s):
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def check(x, y, color, size=1.0):
    s = size
    return (f'<path d="M{x - 4 * s:.1f} {y:.1f}l{3 * s:.1f} {3 * s:.1f}l{5 * s:.1f} {-6 * s:.1f}" '
            f'fill="none" stroke="{color}" stroke-width="{1.8 * s:.1f}" stroke-linecap="round" stroke-linejoin="round"/>')


def render(act, theme):
    p = PALETTES[theme]
    lv = p["levels"]
    s = summarize(act)
    W, H, C, GX, GY = 860, 556, 14, 62, 184
    out, css, base = [], [], []

    def text(x, y, content, size=12, color=None, weight=400, anchor="start", spacing=None, cls=None):
        attrs = f'x="{x:.1f}" y="{y:.1f}" font-size="{size}" fill="{color or p["fg"]}"'
        if weight != 400:
            attrs += f' font-weight="{weight}"'
        if anchor != "start":
            attrs += f' text-anchor="{anchor}"'
        if spacing:
            attrs += f' letter-spacing="{spacing}"'
        if cls:
            attrs += f' class="{cls}"'
        out.append(f"<text {attrs}>{content}</text>")

    def label(x, y, content, anchor="start"):
        text(x, y, content, 10, p["muted"], 700, anchor, "1.8")

    out.append(f'<rect x=".5" y=".5" width="{W - 1}" height="{H - 1}" rx="12" fill="{p["bg"]}" stroke="{p["border"]}"/>')
    out.append(f'<line x1="0" y1="34" x2="{W}" y2="34" stroke="{p["border"]}"/>')
    for cx, color in ((20, "#FF5F57"), (40, "#FEBC2E"), (60, "#28C840")):
        out.append(f'<circle cx="{cx}" cy="17" r="6" fill="{color}"/>')
    text(W / 2, 21, "ndnrs — zsh — 120×40", 10, p["muted"], anchor="middle")

    out.append(f'<text x="28" y="84" font-size="28" font-weight="700"><tspan fill="{p["acc"]}">\\</tspan>'
               f'<tspan fill="{p["fg"]}">ndnrs</tspan></text>')
    out.append(f'<rect x="28" y="98" width="76" height="22" rx="11" fill="none" stroke="{p["acc"]}"/>')
    out.append(f'<g fill="none" stroke="{p["acc"]}" stroke-width="1.3" stroke-linecap="round">'
               '<circle cx="42" cy="104.5" r="1.6"/><circle cx="42" cy="114" r="1.6"/><circle cx="49" cy="105.5" r="1.6"/>'
               '<path d="M42 106.1v6.3M49 107.1c0 3-7 2.2-7 5.3"/></g>')
    text(56, 113, "main", 12, p["acc"])
    vw = len(s["version"]) * 7.4 + 22
    out.append(f'<rect x="112" y="98" width="{vw:.1f}" height="22" rx="11" fill="none" stroke="{p["acc"]}"/>')
    text(123, 113, s["version"], 12, p["acc"])
    label(470, 66, "ÚLTIMOS 7 DIAS", "middle")
    text(470, 108, str(s["this_week"]), 38, p["fg"], 700, "middle")
    label(W - 28, 66, "CI", "end")
    if act.ci is None:
        text(W - 28, 108, "—", 38, p["muted"], 700, "end")
    else:
        text(W - 28, 108, f"{round(act.ci * 100)}%", 38, p["acc"], 700, "end")
        width = len(f"{round(act.ci * 100)}%") * 23
        out.append(check(W - 28 - width - 22, 96, p["acc"], 2.0))

    label(28, 152, "ÚLTIMOS 12 MESES")
    lvls = levels(act.days)
    n_weeks = math.ceil(len(act.days) / 7)
    for w in range(1, n_weeks):
        first = next((d for d, _ in act.days[w * 7:w * 7 + 7] if d.day == 1), None)
        if first:
            text(GX + w * C, GY - 8, MONTHS[first.month - 1], 10, p["muted"])
    for row, name in ((1, "seg"), (3, "qua"), (5, "sex")):
        text(GX - 34, GY + row * C + C * 0.75, name, 10, p["muted"])

    def slash(i, color):
        d, _ = act.days[i]
        x0, y0 = GX + (i // 7) * C, GY + ((d.weekday() + 1) % 7) * C
        return (f'<line x1="{x0 + C * .27:.1f}" y1="{y0 + C * .2:.1f}" x2="{x0 + C * .67:.1f}" '
                f'y2="{y0 + C * .74:.1f}" stroke="{color}"/>')

    out.append(f'<g stroke-width="2.3" stroke-linecap="round">')
    out += [slash(i, lv[0]) for i in range(len(act.days))]
    out.append("</g>")
    sweep_len = T_SWEEP[1] - T_SWEEP[0]
    for w in range(n_weeks):
        cells = [i for i in range(w * 7, min(len(act.days), w * 7 + 7)) if lvls[i]]
        if not cells:
            continue
        reveal = T_SWEEP[0] + (w + 0.5) / n_weeks * sweep_len
        css.append(steps(f"c{w}", "opacity", 0, [(reveal, 1), (T_RESET, 0)], 0.15))
        css.append(f".c{w}{{animation:c{w} {CYCLE}s linear infinite}}")
        out.append(f'<g class="c{w}" stroke-width="2.3" stroke-linecap="round">')
        out += [slash(i, lv[lvls[i]]) for i in cells]
        out.append("</g>")

    grid_w = n_weeks * C
    css.append(
        "@keyframes sweep{"
        f"0%,{pct(T_SWEEP[0])}{{transform:translateX(0);opacity:0}}"
        f"{pct(T_SWEEP[0] + .05)}{{transform:translateX(0);opacity:1}}"
        f"{pct(T_SWEEP[1])}{{transform:translateX({grid_w}px);opacity:1}}"
        f"{pct(T_SWEEP[1] + .05)},100%{{transform:translateX({grid_w}px);opacity:0}}}}"
        f".sweep{{animation:sweep {CYCLE}s linear infinite}}"
    )
    base.append(".sweep{opacity:0}")
    out.append(f'<defs><linearGradient id="sw" x1="0" x2="1"><stop offset="0" stop-color="{p["acc"]}" stop-opacity="0"/>'
               f'<stop offset="1" stop-color="{p["acc"]}" stop-opacity=".35"/></linearGradient>'
               '<filter id="gl" x="-10%" y="-60%" width="120%" height="220%"><feGaussianBlur stdDeviation="3.5"/></filter></defs>')
    out.append(f'<g transform="translate({GX} 0)"><g class="sweep">'
               f'<rect x="-44" y="{GY - 6}" width="44" height="{7 * C + 12}" fill="url(#sw)"/>'
               f'<rect x="-1" y="{GY - 6}" width="2" height="{7 * C + 12}" fill="{p["acc"]}"/></g></g>')

    last = len(act.days) - 1
    tx = GX + (last // 7) * C + C * .47
    ty = GY + ((act.days[last][0].weekday() + 1) % 7) * C + C * .47
    css.append(steps("now", "opacity", 0, [(T_SWEEP[1], 1), (T_RESET, 0)]))
    css.append(f".now{{animation:now {CYCLE}s linear infinite}}")
    out.append(f'<g class="now"><circle class="pulse" cx="{tx:.1f}" cy="{ty:.1f}" r="7" fill="{p["acc"]}" opacity=".25"/></g>')

    label(28, 320, "RITMO")
    sx0, sx1, sb, sa = 28, 430, 404, 76
    shape = day_shape(act.hours)
    pts = [(sx0 + j / 96 * (sx1 - sx0), sb - shape(j / 4) * sa) for j in range(97)]
    path = "M" + " L".join(f"{x:.1f} {y:.1f}" for x, y in pts)
    out.append(f'<defs><linearGradient id="wa" x1="0" y1="0" x2="0" y2="1"><stop offset="0" stop-color="{p["acc"]}" '
               f'stop-opacity=".32"/><stop offset="1" stop-color="{p["acc"]}" stop-opacity="0"/></linearGradient></defs>')
    out.append(f'<path d="{path} L{sx1} {sb} L{sx0} {sb} Z" fill="url(#wa)"/>')
    out.append(f'<line x1="{sx0}" y1="{sb}" x2="{sx1}" y2="{sb}" stroke="{p["border"]}"/>')
    for name, h in (("manhã", 9), ("tarde", 15.5), ("noite", 21.5)):
        text(sx0 + h / 24 * (sx1 - sx0), sb + 18, name, 10, p["muted"], anchor="middle")
    length = sum(math.dist(a, b) for a, b in zip(pts, pts[1:]))
    tail = 80
    css.append(
        "@keyframes trace{"
        f"0%,{pct(T_TRACE[0])}{{stroke-dashoffset:{tail};opacity:0}}"
        f"{pct(T_TRACE[0] + .02)}{{stroke-dashoffset:{tail};opacity:1}}"
        f"{pct(T_TRACE[1])}{{stroke-dashoffset:{tail - length:.1f};opacity:1}}"
        f"{pct(T_TRACE[1] + .8)},100%{{stroke-dashoffset:{tail - length:.1f};opacity:0}}}}"
        f".trace{{animation:trace {CYCLE}s linear infinite}}"
    )
    base.append(f".trace{{opacity:0;stroke-dasharray:{tail} {length + tail:.1f}}}.dot{{opacity:0}}")
    css.append(steps("dot", "opacity", 0, [(T_TRACE[0], 1), (T_TRACE[1], 0)], 0.1))
    css.append(f".dot{{animation:dot {CYCLE}s linear infinite}}")
    out.append(f'<path d="{path}" fill="none" stroke="{p["acc"]}" stroke-width="5" stroke-opacity=".35" filter="url(#gl)"/>')
    out.append(f'<path d="{path}" fill="none" stroke="{p["acc"]}" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"/>')
    out.append(f'<path class="trace" d="{path}" fill="none" stroke="{p["acc"]}" stroke-width="8" stroke-opacity=".8" '
               'stroke-linecap="round" filter="url(#gl)"/>')
    out.append(f'<path class="trace" d="{path}" fill="none" stroke="{p["acc"]}" stroke-width="2.4" '
               'stroke-linecap="round" stroke-linejoin="round"/>')
    k0, k1 = T_TRACE[0] / CYCLE, T_TRACE[1] / CYCLE
    out.append(f'<g class="dot"><circle class="pulse" r="8" fill="{p["acc"]}" opacity=".25"/>'
               f'<circle r="3.4" fill="{p["acc"]}"/>'
               f'<animateMotion dur="{CYCLE}s" repeatCount="indefinite" calcMode="linear" '
               f'keyTimes="0;{k0:.3f};{k1:.3f};1" keyPoints="0;0;1;1" path="{path}"/></g>')

    lx0, lw = 470, W - 28 - 470
    label(lx0, 320, "LINGUAGENS")
    x = lx0
    for k, (name, share) in enumerate(act.languages):
        color = lv[0] if name == "Outras" else mix(lv[4], lv[1], k / max(1, len(act.languages) - 1))
        width = lw * share / 100
        out.append(f'<rect x="{x:.1f}" y="340" width="{max(0, width - 2):.1f}" height="10" rx="2" fill="{color}"/>')
        x += width
        col, row = k % 2, k // 2
        ly, cx = 374 + row * 20, lx0 + col * lw / 2
        out.append(f'<rect x="{cx:.1f}" y="{ly - 9}" width="10" height="10" rx="2" fill="{color}"/>')
        text(cx + 16, ly, esc(name), 12, p["fg"])
        text(cx + lw / 2 - 12, ly, f"{share}%", 12, p["muted"], anchor="end")

    label(28, 474, "DO ZERO AO DEPLOY")
    xs, sy = [230, 370, 510, 650], 470
    for k in range(3):
        out.append(f'<line x1="{xs[k] + 10}" y1="{sy}" x2="{xs[k + 1] - 10}" y2="{sy}" stroke="{p["border"]}"/>')
    for k, (run, done) in enumerate(NODES):
        if k:
            css.append(steps(f"l{k}", "transform", "scaleX(0)", [(done, "scaleX(1)"), (T_RESET, "scaleX(0)")], 0.4))
            css.append(f".l{k}{{transform-box:fill-box;transform-origin:0 50%;animation:l{k} {CYCLE}s linear infinite}}")
            out.append(f'<rect class="l{k}" x="{xs[k - 1] + 10}" y="{sy - 1}" width="{xs[k] - xs[k - 1] - 20}" height="2" fill="{p["acc"]}"/>')
        css.append(steps(f"r{k}", "opacity", 0, [(run, 1), (done, 0)], 0.1))
        css.append(steps(f"o{k}", "opacity", 0, [(done, 1), (T_RESET, 0)]))
        css.append(f".r{k}{{animation:r{k} {CYCLE}s linear infinite}}.o{k}{{animation:o{k} {CYCLE}s linear infinite}}")
        base.append(f".r{k}{{opacity:0}}")
        out.append(f'<g class="r{k}"><circle class="pulse" cx="{xs[k]}" cy="{sy}" r="10" fill="{p["acc"]}" opacity=".25"/></g>')
        out.append(f'<circle cx="{xs[k]}" cy="{sy}" r="9" fill="{p["bg"]}" stroke="{p["border"]}" stroke-width="1.5"/>')
        out.append(f'<g class="r{k}"><circle cx="{xs[k]}" cy="{sy}" r="9" fill="none" stroke="{p["acc"]}" stroke-width="1.5"/></g>')
        out.append(f'<g class="o{k}"><circle cx="{xs[k]}" cy="{sy}" r="9" fill="{p["acc"]}"/>{check(xs[k], sy, p["bg"], 0.9)}</g>')
        text(xs[k], sy + 28, ("zero", "código", "testes", "deploy")[k], 12, p["muted"], anchor="middle")
    css.append(steps("done", "opacity", 0, [(T_DEPLOY, 1), (T_RESET, 0)]))
    css.append(f".done{{animation:done {CYCLE}s linear infinite}}")
    done_w = (len(s["version"]) + 8) * 7.4
    out.append(f'<g class="done">{check(W - 28 - done_w - 8, sy - 1, p["acc"], 1.0)}')
    out.append(f'<text x="{W - 28}" y="{sy + 4}" font-size="12" font-weight="700" text-anchor="end">'
               f'<tspan fill="{p["fg"]}">{s["version"]}</tspan><tspan fill="{p["acc"]}"> em prod</tspan></text></g>')

    out.append(f'<line x1="0" y1="516" x2="{W}" y2="516" stroke="{p["border"]}"/>')
    text(28, 540, "darwin arm64 · zsh", 12, p["muted"])
    bars_x = W - 28 - 82 - 5 * 8
    text(bars_x - 10, 540, "temp", 12, p["muted"], anchor="end")
    bar_on = [0, 0.9, 2.2, T_TRACE[0], T_TRACE[1]]
    for k in range(5):
        h = 4 + k * 2.4
        attrs = f'x="{bars_x + k * 8}" y="{544 - h:.1f}" width="5" height="{h:.1f}" rx="1"'
        out.append(f'<rect {attrs} fill="{lv[0]}"/>')
        if k == 0:
            out.append(f'<rect {attrs} fill="{p["acc"]}"/>')
            continue
        changes = [(bar_on[k], 1)]
        if k >= s["temp"]:
            changes.append((T_SETTLE, 0))
        changes.append((T_RESET, 0))
        css.append(steps(f"b{k}", "opacity", 0, changes))
        css.append(f".b{k}{{animation:b{k} {CYCLE}s linear infinite}}")
        base.append(f".b{k}{{opacity:{1 if k < s['temp'] else 0}}}")
        out.append(f'<rect class="b{k}" {attrs} fill="{p["acc"]}"/>')
    real = s["temp_label"]
    timelines = {
        "idle": (1, [(0.9, 0), (T_SETTLE, int(real == "idle")), (T_RESET, 1)]),
        "warm": (0, [(0.9, 1), (T_TRACE[0], 0), (T_SETTLE, int(real == "warm")), (T_RESET, 0)]),
        "cooking": (0, [(T_TRACE[0], 1), (T_SETTLE, int(real == "cooking")), (T_RESET, 0)]),
    }
    for name, (start, changes) in timelines.items():
        css.append(steps(f"t{name}", "opacity", start, changes))
        css.append(f".t{name}{{animation:t{name} {CYCLE}s linear infinite}}")
        base.append(f".t{name}{{opacity:{int(name == real)}}}")
        text(W - 28, 540, name, 12, p["acc"], 700, "end", cls=f"t{name}")

    style = (
        f"text{{font-family:{FONT};white-space:pre}}"
        + "".join(base) +
        "@media (prefers-reduced-motion:no-preference){"
        ".pulse{transform-box:fill-box;transform-origin:center;animation:pulse 2s ease-out infinite}"
        "@keyframes pulse{from{transform:scale(.6);opacity:.5}to{transform:scale(2);opacity:0}}"
        + "".join(css) +
        "}"
    )
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {W} {H}" width="{W}" height="{H}" '
        f'role="img" aria-label="ndnrs: {s["this_week"]} contribuições nos últimos 7 dias, do zero ao deploy">'
        f"<style>{style}</style>" + "".join(out) + "</svg>"
    )


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", default="dist", help="pasta de saída")
    parser.add_argument("--demo", action="store_true", help="usar dados de exemplo, sem rede")
    args = parser.parse_args()

    if args.demo:
        act = demo()
    else:
        token, login = os.environ.get("GH_TOKEN"), os.environ.get("GH_USER", "ndnrs")
        if not token:
            parser.error("falta a variável GH_TOKEN (ou usa --demo)")
        act = fetch(login, token)

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    (out / "ndnrs-dashboard.svg").write_text(render(act, "light"), encoding="utf-8")
    (out / "ndnrs-dashboard-dark.svg").write_text(render(act, "dark"), encoding="utf-8")
    print(f"painel gerado em {out}/ ({sum(c for _, c in act.days)} contribuições no último ano)")


if __name__ == "__main__":
    main()
