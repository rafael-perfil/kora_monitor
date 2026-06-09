#!/usr/bin/env python3
"""Kora Monitor — fetches OpenAI org data and generates the GitHub Pages dashboard."""

import os
import sys
import json
import re
import hashlib
import requests
from datetime import datetime, timezone, timedelta
from collections import defaultdict

# ── Config ─────────────────────────────────────────────────────────────────────
ADMIN_KEY          = os.environ.get("OPENAI_ADMIN_KEY", "")
DASHBOARD_PASSWORD = os.environ.get("DASHBOARD_PASSWORD", "admin")
OUTPUT_PATH        = "docs/index.html"
BASE_URL           = "https://api.openai.com/v1/organization"
DAYS               = 30

if not ADMIN_KEY:
    sys.exit("ERROR: OPENAI_ADMIN_KEY environment variable not set")

HEADERS = {"Authorization": f"Bearer {ADMIN_KEY}"}

# ── API helpers ────────────────────────────────────────────────────────────────
def api_get(url, params=None):
    r = requests.get(url, headers=HEADERS, params=params or {}, timeout=30)
    r.raise_for_status()
    return r.json()

def api_paginate(url, params=None, limit=100):
    # Note: the usage API caps `limit` at 31 for bucket_width=1d; costs allows up to 180.
    p = dict(params or {})
    p["limit"] = limit
    results = []
    while True:
        d = api_get(url, p)
        results.extend(d.get("data", []))
        if not d.get("has_more"):
            break
        if d.get("next_page"):
            p["page"] = d["next_page"]
        else:
            break
    return results

def ts_to_date(ts):
    return datetime.fromtimestamp(int(ts), tz=timezone.utc).strftime("%Y-%m-%d")

def norm_model(mid):
    if not mid:
        return "unknown"
    m = re.sub(r"-20\d\d-\d\d-\d\d$", "", mid)
    return re.sub(r"-(preview|latest)$", "", m)

def fmt_date(v):
    if not v:
        return "—"
    try:
        return datetime.fromtimestamp(int(v), tz=timezone.utc).strftime("%d/%m/%Y")
    except Exception:
        return str(v)

# ── Fetch ──────────────────────────────────────────────────────────────────────
now  = datetime.now(timezone.utc)
t0   = int((now - timedelta(days=DAYS)).timestamp())
t1   = int(now.timestamp())
base = {"start_time": t0, "end_time": t1, "bucket_width": "1d"}

print("→ Fetching costs…")
try:
    cost_buckets = api_paginate(f"{BASE_URL}/costs", base)
except Exception as e:
    sys.exit(f"ERROR fetching costs: {e}")

print("→ Fetching usage…")
try:
    usage_buckets = api_paginate(
        f"{BASE_URL}/usage/completions",
        {**base, "group_by": "model"},
        limit=31,   # max allowed for bucket_width=1d
    )
except Exception as e:
    sys.exit(f"ERROR fetching usage: {e}")

print("→ Fetching API keys…")
try:
    api_keys = api_get(f"{BASE_URL}/admin_api_keys", {"limit": 100}).get("data", [])
except Exception as e:
    print(f"  Warning (keys skipped): {e}")
    api_keys = []

print("→ Fetching USD→BRL rate…")
brl_rate = 5.0  # fallback
try:
    fx = requests.get("https://economia.awesomeapi.com.br/last/USD-BRL", timeout=15).json()
    brl_rate = round(float(fx["USDBRL"]["bid"]), 4)
    print(f"  USD→BRL = {brl_rate}")
except Exception as e:
    print(f"  Warning (using fallback rate {brl_rate}): {e}")

# ── Aggregate costs ────────────────────────────────────────────────────────────
daily_cost = defaultdict(float)
total_cost = 0.0
def to_float(x):
    try:    return float(x)
    except (TypeError, ValueError): return 0.0

def to_int(x):
    try:    return int(x)
    except (TypeError, ValueError): return 0

for b in cost_buckets:
    d = ts_to_date(b["start_time"])
    for r in b.get("results", []):
        v = to_float(r.get("amount", {}).get("value", 0.0))
        daily_cost[d] += v
        total_cost    += v

# ── Aggregate usage ────────────────────────────────────────────────────────────
daily_tok      = defaultdict(lambda: {"i": 0, "o": 0, "c": 0, "r": 0})
model_agg      = defaultdict(lambda: {"i": 0, "o": 0, "c": 0, "r": 0})
total_requests = 0

for b in usage_buckets:
    d = ts_to_date(b["start_time"])
    for r in b.get("results", []):
        i  = to_int(r.get("input_tokens", 0))
        o  = to_int(r.get("output_tokens", 0))
        c  = to_int(r.get("input_cached_tokens", 0))
        n  = to_int(r.get("num_model_requests", 0))
        mn = norm_model(r.get("model_id", ""))
        daily_tok[d]["i"] += i;  daily_tok[d]["o"] += o
        daily_tok[d]["c"] += c;  daily_tok[d]["r"] += n
        model_agg[mn]["i"] += i; model_agg[mn]["o"] += o
        model_agg[mn]["c"] += c; model_agg[mn]["r"] += n
        total_requests += n

# ── Date axis ─────────────────────────────────────────────────────────────────
all_dates    = sorted(set(list(daily_cost) + list(daily_tok)))[-DAYS:]
short_labels = [datetime.strptime(d, "%Y-%m-%d").strftime("%d/%b") for d in all_dates]

# ── Model cost estimates ───────────────────────────────────────────────────────
PRICES = {
    "o1-pro":        (150.0,  600.0),
    "o1":            ( 15.0,   60.0),
    "o1-mini":       (  3.0,   12.0),
    "o3":            ( 10.0,   40.0),
    "o3-mini":       (  1.1,    4.4),
    "o4-mini":       (  1.1,    4.4),
    "gpt-4.1-nano":  (  0.10,   0.40),
    "gpt-4.1-mini":  (  0.40,   1.60),
    "gpt-4.1":       (  2.0,    8.0),
    "gpt-4o-mini":   (  0.15,   0.60),
    "gpt-4o":        (  2.5,   10.0),
    "gpt-4-turbo":   ( 10.0,   30.0),
    "gpt-4":         ( 30.0,   60.0),
    "gpt-3.5-turbo": (  0.5,    1.5),
}

def est_cost(model, inp, out):
    for pfx, (pi, po) in PRICES.items():
        if model.startswith(pfx):
            return inp / 1_000_000 * pi + out / 1_000_000 * po
    return 0.0

model_rows = sorted(
    [
        {
            "name":     m,
            "input":    s["i"],
            "output":   s["o"],
            "cached":   s["c"],
            "requests": s["r"],
            "cost_est": round(est_cost(m, s["i"], s["o"]), 6),
        }
        for m, s in model_agg.items()
    ],
    key=lambda x: x["requests"],
    reverse=True,
)

top5       = sorted(model_rows, key=lambda x: x["cost_est"], reverse=True)[:5]
dnt_labels = [m["name"]     for m in top5]
dnt_values = [m["cost_est"] for m in top5]
if len(model_rows) > 5:
    dnt_labels.append("outros")
    dnt_values.append(round(sum(m["cost_est"] for m in model_rows[5:]), 6))

key_rows = [
    {
        "name":      k.get("name", "—"),
        "created":   fmt_date(k.get("created_at")),
        "last_used": fmt_date(k.get("last_used_at")) or "Nunca",
    }
    for k in api_keys[:20]
]

# ── DATA object ────────────────────────────────────────────────────────────────
DATA = {
    "updated_at":     now.strftime("%d/%m/%Y %H:%M UTC"),
    "brl_rate":       brl_rate,
    "month_cost":     round(total_cost, 6),
    "total_input":    sum(s["i"] for s in model_agg.values()),
    "total_output":   sum(s["o"] for s in model_agg.values()),
    "total_cached":   sum(s["c"] for s in model_agg.values()),
    "total_requests": total_requests,
    "active_models":  len([m for m in model_rows if m["requests"] > 0]),
    "daily": {
        "labels": short_labels,
        "cost":   [round(daily_cost.get(d, 0.0), 6) for d in all_dates],
        "input":  [daily_tok.get(d, {}).get("i", 0)  for d in all_dates],
        "output": [daily_tok.get(d, {}).get("o", 0)  for d in all_dates],
        "cached": [daily_tok.get(d, {}).get("c", 0)  for d in all_dates],
    },
    "doughnut":   {"labels": dnt_labels, "values": dnt_values},
    "model_rows": model_rows[:15],
    "api_keys":   key_rows,
}

PWD_HASH  = hashlib.sha256(DASHBOARD_PASSWORD.encode()).hexdigest()
DATA_JSON = json.dumps(DATA, ensure_ascii=False).replace("</script>", r"<\/script>")

# ── HTML template ──────────────────────────────────────────────────────────────
HTML = """<!DOCTYPE html>
<html lang="pt-BR">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Kora Monitor</title>
<script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.4/dist/chart.umd.min.js"></script>
<style>
/* ── Light theme (default) ──────────────────────────────────────────────────── */
:root{
  --bg:       #f3f5fa;
  --card:     #ffffff;
  --card2:    #eef1f8;
  --border:   #e1e6f0;
  --cyan:     #0aa5c9;
  --purple:   #7c4dff;
  --green:    #00b35c;
  --orange:   #f57c00;
  --red:      #e53860;
  --text:     #1b2438;
  --muted:    #6a7691;
  --overlay:  rgba(243,245,250,.97);
  --input-bg: #f3f5fa;
  --tick:     #6a7691;
  --grid:     rgba(120,130,150,.18);
  --r:        12px;
}
/* ── Dark theme ─────────────────────────────────────────────────────────────── */
:root[data-theme="dark"]{
  --bg:       #07070f;
  --card:     #0d0d1b;
  --card2:    #12122a;
  --border:   #1c1c38;
  --cyan:     #00d4ff;
  --purple:   #7c4dff;
  --green:    #00e676;
  --orange:   #ff9100;
  --red:      #ff4569;
  --text:     #d4dcf0;
  --muted:    #50607a;
  --overlay:  rgba(7,7,15,.98);
  --input-bg: #0a0a18;
  --tick:     #50607a;
  --grid:     rgba(28,28,56,.8);
}
*{margin:0;padding:0;box-sizing:border-box}
body{background:var(--bg);color:var(--text);font-family:system-ui,'Segoe UI',sans-serif;min-height:100vh;transition:background .25s,color .25s}

/* ── Login ──────────────────────────────────────────────────────────────────── */
#login{
  position:fixed;inset:0;background:var(--overlay);
  display:flex;align-items:center;justify-content:center;z-index:999;
}
.lcard{
  background:var(--card);border:1px solid var(--border);
  border-radius:20px;padding:48px 44px;width:360px;
  box-shadow:0 0 80px rgba(0,212,255,.07),0 0 0 1px rgba(0,212,255,.04);
  display:flex;flex-direction:column;gap:22px;
}
.llogo{text-align:center}
.llogo .hex{font-size:42px;line-height:1;filter:drop-shadow(0 0 12px rgba(0,212,255,.5))}
.llogo h1{font-size:20px;font-weight:800;color:var(--cyan);letter-spacing:2px;margin-top:10px}
.llogo p{font-size:11px;color:var(--muted);margin-top:3px;letter-spacing:.3px}
.lfld label{display:block;font-size:10px;color:var(--muted);margin-bottom:7px;text-transform:uppercase;letter-spacing:.6px}
.lfld input{
  width:100%;padding:13px 16px;background:var(--input-bg);
  border:1px solid var(--border);border-radius:8px;
  color:var(--text);font-size:15px;outline:none;transition:border-color .2s;
}
.lfld input:focus{border-color:var(--cyan);box-shadow:0 0 0 3px rgba(0,212,255,.08)}
.lbtn{
  width:100%;padding:14px;
  background:linear-gradient(135deg,var(--cyan),#0099cc);
  border:none;border-radius:8px;color:#000;
  font-size:15px;font-weight:800;cursor:pointer;transition:.15s;
  letter-spacing:.5px;
}
.lbtn:hover{transform:translateY(-1px);box-shadow:0 4px 20px rgba(0,212,255,.3)}
.lbtn:active{transform:translateY(0)}
.lerr{color:var(--red);font-size:12px;text-align:center;display:none;padding:4px 0}

/* ── Layout ─────────────────────────────────────────────────────────────────── */
#dash{display:none;padding:24px;max-width:1440px;margin:0 auto}

/* Header */
.hdr{display:flex;align-items:center;justify-content:space-between;margin-bottom:28px;flex-wrap:wrap;gap:12px}
.hdr-l{display:flex;align-items:center;gap:14px}
.hdr-hex{font-size:30px;filter:drop-shadow(0 0 10px rgba(0,212,255,.4))}
.hdr-title{font-size:20px;font-weight:800;color:var(--cyan);letter-spacing:1.5px}
.hdr-sub{font-size:11px;color:var(--muted);margin-top:3px}
.hdr-r{display:flex;align-items:center;gap:10px;flex-wrap:wrap}
.badge{
  background:var(--card2);border:1px solid var(--border);border-radius:20px;
  padding:7px 14px;font-size:11px;color:var(--muted);white-space:nowrap;
}
.badge b{color:var(--cyan)}
.btn{
  background:transparent;border:1px solid var(--border);border-radius:8px;
  padding:8px 16px;color:var(--text);font-size:12px;cursor:pointer;transition:.15s;
}
.btn:hover{border-color:var(--cyan);color:var(--cyan)}
.btn-out:hover{border-color:var(--red)!important;color:var(--red)!important}

/* Stats */
.stats{display:grid;grid-template-columns:repeat(4,1fr);gap:16px;margin-bottom:20px}
@media(max-width:900px){.stats{grid-template-columns:repeat(2,1fr)}}
@media(max-width:500px){.stats{grid-template-columns:1fr}}
.scard{
  background:var(--card);border:1px solid var(--border);
  border-radius:var(--r);padding:22px 20px;position:relative;overflow:hidden;
}
.scard::after{
  content:'';position:absolute;top:0;left:0;right:0;height:2px;
  background:linear-gradient(90deg,transparent,var(--ac,var(--cyan)),transparent);
}
.scard.g{--ac:var(--green)}.scard.o{--ac:var(--orange)}.scard.p{--ac:var(--purple)}
.scard-glow{
  position:absolute;top:-20px;right:-20px;width:80px;height:80px;
  background:radial-gradient(circle,rgba(var(--acr,0,212,255),.08) 0%,transparent 70%);
  border-radius:50%;
}
.scard.g .scard-glow{background:radial-gradient(circle,rgba(0,230,118,.08) 0%,transparent 70%)}
.scard.o .scard-glow{background:radial-gradient(circle,rgba(255,145,0,.08) 0%,transparent 70%)}
.scard.p .scard-glow{background:radial-gradient(circle,rgba(124,77,255,.08) 0%,transparent 70%)}
.sico{font-size:22px;margin-bottom:12px}
.slbl{font-size:10px;color:var(--muted);text-transform:uppercase;letter-spacing:.6px;margin-bottom:6px}
.sval{font-size:28px;font-weight:800;color:var(--text);line-height:1}
.sval .cur{font-size:13px;font-weight:400;color:var(--muted)}
.ssub{font-size:10px;color:var(--muted);margin-top:6px}

/* Charts row */
.charts-row{display:grid;grid-template-columns:2fr 1fr;gap:16px;margin-bottom:20px}
@media(max-width:900px){.charts-row{grid-template-columns:1fr}}

.ccard{background:var(--card);border:1px solid var(--border);border-radius:var(--r);padding:22px}
.ctitle{
  font-size:12px;font-weight:700;color:var(--text);
  margin-bottom:16px;display:flex;justify-content:space-between;align-items:center;
  text-transform:uppercase;letter-spacing:.5px;
}
.pill{
  background:var(--card2);border:1px solid var(--border);border-radius:20px;
  padding:3px 10px;font-size:10px;color:var(--muted);font-weight:400;
  text-transform:none;letter-spacing:0;
}
.cwrap{position:relative;height:220px}
.dwrap{position:relative;height:220px}
.twrap{position:relative;height:200px}

/* Tables */
.tcard{background:var(--card);border:1px solid var(--border);border-radius:var(--r);padding:22px;margin-bottom:20px}
.ttitle{font-size:12px;font-weight:700;color:var(--text);margin-bottom:16px;text-transform:uppercase;letter-spacing:.5px}
.tbl-wrap{overflow-x:auto}
table{width:100%;border-collapse:collapse;font-size:13px;min-width:500px}
thead th{
  color:var(--muted);font-weight:600;font-size:10px;text-transform:uppercase;
  letter-spacing:.5px;padding:9px 12px;border-bottom:1px solid var(--border);text-align:left;
}
tbody tr{border-bottom:1px solid rgba(255,255,255,.025);transition:.12s}
tbody tr:last-child{border-bottom:none}
tbody tr:hover{background:var(--card2)}
tbody td{padding:10px 12px;color:var(--text)}
.tag{
  display:inline-block;background:rgba(0,212,255,.08);
  border:1px solid rgba(0,212,255,.18);color:var(--cyan);
  border-radius:4px;padding:2px 9px;font-size:11px;font-family:monospace;
}
.num{font-variant-numeric:tabular-nums;color:var(--muted)}

/* Footer */
.foot{text-align:center;color:var(--muted);font-size:11px;padding:16px 0 40px}
.foot .nxt{color:var(--cyan);font-variant-numeric:tabular-nums}

/* Scrollbar */
::-webkit-scrollbar{width:5px;height:5px}
::-webkit-scrollbar-track{background:transparent}
::-webkit-scrollbar-thumb{background:var(--border);border-radius:3px}
</style>
</head>
<body>

<!-- Login ─────────────────────────────────────────────────────────────────── -->
<div id="login">
  <div class="lcard">
    <div class="llogo">
      <div class="hex">⬡</div>
      <h1>KORA MONITOR</h1>
      <p>OpenAI Usage Dashboard</p>
    </div>
    <div class="lfld">
      <label>Senha de acesso</label>
      <input type="password" id="pwd" placeholder="••••••••" autocomplete="current-password">
    </div>
    <button class="lbtn" id="lbtn" onclick="doLogin()">Entrar</button>
    <div class="lerr" id="lerr">Senha incorreta. Tente novamente.</div>
  </div>
</div>

<!-- Dashboard ───────────────────────────────────────────────────────────────── -->
<div id="dash">

  <div class="hdr">
    <div class="hdr-l">
      <div class="hdr-hex">⬡</div>
      <div>
        <div class="hdr-title">KORA MONITOR</div>
        <div class="hdr-sub">OpenAI Usage Dashboard &nbsp;·&nbsp; últimos 30 dias</div>
      </div>
    </div>
    <div class="hdr-r">
      <div class="badge">USD→BRL: <b id="fx-rate">—</b></div>
      <div class="badge">Atualizado: <b id="upd-time">—</b></div>
      <button class="btn" id="theme-btn" onclick="toggleTheme()" title="Alternar tema">🌙</button>
      <button class="btn" onclick="location.reload()">↻&nbsp; Atualizar</button>
      <button class="btn btn-out" onclick="doLogout()">Sair</button>
    </div>
  </div>

  <!-- Stats ──────────────────────────────────────────────────────────────────── -->
  <div class="stats">
    <div class="scard">
      <div class="scard-glow"></div>
      <div class="sico">💰</div>
      <div class="slbl">Custo Real (30d)</div>
      <div class="sval"><span class="cur">$ </span><span id="s-cost">—</span></div>
      <div class="ssub" id="s-cost-brl">—</div>
    </div>
    <div class="scard g">
      <div class="scard-glow"></div>
      <div class="sico">🔢</div>
      <div class="slbl">Total de Tokens (30d)</div>
      <div class="sval"><span id="s-tokens">—</span></div>
      <div class="ssub" id="s-tok-detail">—</div>
    </div>
    <div class="scard o">
      <div class="scard-glow"></div>
      <div class="sico">🤖</div>
      <div class="slbl">Modelos Ativos</div>
      <div class="sval"><span id="s-models">—</span></div>
      <div class="ssub">em uso no período</div>
    </div>
    <div class="scard p">
      <div class="scard-glow"></div>
      <div class="sico">📡</div>
      <div class="slbl">Requisições (30d)</div>
      <div class="sval"><span id="s-reqs">—</span></div>
      <div class="ssub">chamadas à API</div>
    </div>
  </div>

  <!-- Charts ─────────────────────────────────────────────────────────────────── -->
  <div class="charts-row">
    <div class="ccard">
      <div class="ctitle">Custo Diário <span class="pill">USD · 30 dias</span></div>
      <div class="cwrap"><canvas id="c-cost"></canvas></div>
    </div>
    <div class="ccard">
      <div class="ctitle">Custo por Modelo <span class="pill">estimado</span></div>
      <div class="dwrap"><canvas id="c-doughnut"></canvas></div>
    </div>
  </div>

  <div class="ccard" style="margin-bottom:20px">
    <div class="ctitle">Tokens por Dia <span class="pill">entrada · saída · cache</span></div>
    <div class="twrap"><canvas id="c-tokens"></canvas></div>
  </div>

  <!-- Model table ───────────────────────────────────────────────────────────── -->
  <div class="tcard">
    <div class="ttitle">Uso por Modelo (30 dias)</div>
    <div class="tbl-wrap">
      <table>
        <thead><tr>
          <th>Modelo</th>
          <th>Requisições</th>
          <th>Tokens Entrada</th>
          <th>Tokens Saída</th>
          <th>Cache</th>
          <th>Custo Est.</th>
        </tr></thead>
        <tbody id="model-tbody"></tbody>
      </table>
    </div>
  </div>

  <!-- API Keys ──────────────────────────────────────────────────────────────── -->
  <div class="tcard">
    <div class="ttitle">API Keys</div>
    <div class="tbl-wrap">
      <table>
        <thead><tr>
          <th>Nome</th>
          <th>Criado em</th>
          <th>Último uso</th>
        </tr></thead>
        <tbody id="keys-tbody"></tbody>
      </table>
    </div>
  </div>

  <div class="foot">
    Próxima atualização em &nbsp;<span id="nxt" class="nxt">—</span>
    &nbsp;·&nbsp; Dados via OpenAI Admin API &nbsp;·&nbsp; Kora Monitor
  </div>

</div><!-- /#dash -->

<script>
// ── Embedded data ─────────────────────────────────────────────────────────────
const DATA = __DATA_JSON__;
const PWD  = "__PWD_HASH__";

// ── Auth ──────────────────────────────────────────────────────────────────────
const SK  = "kora_session";
const TTL = 7 * 86400000;

async function sha256(str) {
  const buf = await crypto.subtle.digest("SHA-256", new TextEncoder().encode(str));
  return Array.from(new Uint8Array(buf)).map(b => b.toString(16).padStart(2,"0")).join("");
}

function sessionOk() {
  try {
    const s = JSON.parse(localStorage.getItem(SK));
    return s && s.h === PWD && Date.now() < s.e;
  } catch { return false; }
}

async function doLogin() {
  const btn = document.getElementById("lbtn");
  btn.textContent = "Verificando…";
  btn.disabled = true;
  const h = await sha256(document.getElementById("pwd").value);
  if (h === PWD) {
    localStorage.setItem(SK, JSON.stringify({ h, e: Date.now() + TTL }));
    showDash();
  } else {
    document.getElementById("lerr").style.display = "block";
    btn.textContent = "Entrar";
    btn.disabled = false;
  }
}

function doLogout() {
  localStorage.removeItem(SK);
  location.reload();
}

document.getElementById("pwd").addEventListener("keydown", e => {
  if (e.key === "Enter") doLogin();
});

// ── Formatting ────────────────────────────────────────────────────────────────
const RATE = DATA.brl_rate || 5;

function fmt(n, dec = 0) {
  if (n == null) return "—";
  if (n >= 1e9) return (n / 1e9).toFixed(1) + "B";
  if (n >= 1e6) return (n / 1e6).toFixed(1) + "M";
  if (n >= 1e3) return (n / 1e3).toFixed(1) + "K";
  return n.toLocaleString("pt-BR", { maximumFractionDigits: dec });
}
function usd(v, d = 4) { return "$" + Number(v).toFixed(d); }
function brl(v, d = 2) {
  return "R$ " + (Number(v) * RATE).toLocaleString("pt-BR",
    { minimumFractionDigits: d, maximumFractionDigits: d });
}

// ── Theme ─────────────────────────────────────────────────────────────────────
function cssVar(name) {
  return getComputedStyle(document.documentElement).getPropertyValue(name).trim();
}
function curTheme() {
  return document.documentElement.getAttribute("data-theme") === "dark" ? "dark" : "light";
}
function updateThemeBtn() {
  const b = document.getElementById("theme-btn");
  if (b) b.textContent = curTheme() === "dark" ? "☀️" : "🌙";
}
function initTheme() {
  const t = localStorage.getItem("kora_theme") || "light";   // light por padrão
  document.documentElement.setAttribute("data-theme", t);
  updateThemeBtn();
}
function toggleTheme() {
  const next = curTheme() === "dark" ? "light" : "dark";
  document.documentElement.setAttribute("data-theme", next);
  localStorage.setItem("kora_theme", next);
  updateThemeBtn();
  buildCharts();   // recria gráficos com cores do novo tema
}

// ── Render ────────────────────────────────────────────────────────────────────
function showDash() {
  document.getElementById("login").style.display = "none";
  document.getElementById("dash").style.display  = "block";
  render();
}

let charts = {};
function buildCharts() {
  Object.values(charts).forEach(c => c && c.destroy());
  charts = {};

  const tick = cssVar("--tick") || "#6a7691";
  const grd  = cssVar("--grid") || "rgba(120,130,150,.18)";
  Chart.defaults.color       = tick;
  Chart.defaults.borderColor = grd;
  const font = { family: "system-ui,'Segoe UI',sans-serif", size: 11 };
  const grid = { color: grd };

  // ── Daily cost bar ────────────────────────────────────────────────────────────
  charts.cost = new Chart(document.getElementById("c-cost"), {
    type: "bar",
    data: {
      labels: DATA.daily.labels,
      datasets: [{
        label: "USD",
        data: DATA.daily.cost,
        backgroundColor: "rgba(10,165,201,.55)",
        borderColor:     "rgba(10,165,201,.95)",
        borderWidth: 1,
        borderRadius: 3,
        hoverBackgroundColor: "rgba(10,165,201,.8)",
      }],
    },
    options: {
      responsive: true, maintainAspectRatio: false,
      plugins: {
        legend: { display: false },
        tooltip: { callbacks: { label: ctx => ` ${usd(ctx.raw)}  ·  ${brl(ctx.raw)}` } },
      },
      scales: {
        x: { ticks: { font, maxRotation: 0, autoSkip: true, maxTicksLimit: 12 }, grid },
        y: { ticks: { font, callback: v => "$" + v.toFixed(3) }, grid },
      },
    },
  });

  // ── Doughnut (custo por modelo) ────────────────────────────────────────────────
  const palette = ["#0aa5c9","#7c4dff","#00b35c","#f57c00","#e53860","#3b82f6","#d946ef","#14b8a6"];
  charts.doughnut = new Chart(document.getElementById("c-doughnut"), {
    type: "doughnut",
    data: {
      labels: DATA.doughnut.labels,
      datasets: [{
        data: DATA.doughnut.values,
        backgroundColor: palette,
        borderWidth: 0,
        hoverOffset: 8,
      }],
    },
    options: {
      responsive: true, maintainAspectRatio: false,
      cutout: "68%",
      plugins: {
        legend: {
          position: "bottom",
          labels: { font, padding: 10, boxWidth: 10, usePointStyle: true, pointStyle: "circle" },
        },
        tooltip: { callbacks: {
          label: ctx => ` ${ctx.label}: ${usd(ctx.raw)} · ${brl(ctx.raw)}`,
        } },
      },
    },
  });

  // ── Token stacked bar ─────────────────────────────────────────────────────────
  charts.tokens = new Chart(document.getElementById("c-tokens"), {
    type: "bar",
    data: {
      labels: DATA.daily.labels,
      datasets: [
        { label: "Entrada", data: DATA.daily.input,  backgroundColor: "rgba(10,165,201,.7)",  borderRadius: 2 },
        { label: "Saída",   data: DATA.daily.output, backgroundColor: "rgba(124,77,255,.7)",  borderRadius: 2 },
        { label: "Cache",   data: DATA.daily.cached, backgroundColor: "rgba(0,179,92,.6)",    borderRadius: 2 },
      ],
    },
    options: {
      responsive: true, maintainAspectRatio: false,
      plugins: {
        legend: { labels: { font, boxWidth: 10, usePointStyle: true, pointStyle: "circle", padding: 16 } },
        tooltip: { callbacks: { label: ctx => " " + fmt(ctx.raw) + " tokens" } },
      },
      scales: {
        x: { stacked: true, ticks: { font, maxRotation: 0, autoSkip: true, maxTicksLimit: 12 }, grid },
        y: { stacked: true, ticks: { font, callback: v => fmt(v) }, grid },
      },
    },
  });
}

function render() {
  // Stats
  document.getElementById("upd-time").textContent  = DATA.updated_at;
  document.getElementById("fx-rate").textContent   = "R$ " + RATE.toFixed(2);
  document.getElementById("s-cost").textContent    = DATA.month_cost.toFixed(4);
  document.getElementById("s-cost-brl").textContent = brl(DATA.month_cost) + "  ·  fatura OpenAI";
  document.getElementById("s-models").textContent  = DATA.active_models;
  document.getElementById("s-reqs").textContent    = fmt(DATA.total_requests);
  const tot = DATA.total_input + DATA.total_output;
  document.getElementById("s-tokens").textContent  = fmt(tot);
  document.getElementById("s-tok-detail").textContent =
    `↑ ${fmt(DATA.total_input)} · ↓ ${fmt(DATA.total_output)} · ♻ ${fmt(DATA.total_cached)}`;

  buildCharts();

  // ── Model table (custo por modelo) ──────────────────────────────────────────────
  const mtb = document.getElementById("model-tbody");
  if (!DATA.model_rows.length) {
    mtb.innerHTML = '<tr><td colspan="6" style="text-align:center;color:var(--muted);padding:24px">Sem dados de uso no período</td></tr>';
  } else {
    DATA.model_rows.forEach(m => {
      const tr = document.createElement("tr");
      tr.innerHTML = `
        <td><span class="tag">${m.name}</span></td>
        <td class="num">${fmt(m.requests)}</td>
        <td class="num">${fmt(m.input)}</td>
        <td class="num">${fmt(m.output)}</td>
        <td class="num">${fmt(m.cached)}</td>
        <td><span style="color:var(--cyan)">${usd(m.cost_est)}</span>
            <span style="color:var(--muted);font-size:11px"> · ${brl(m.cost_est)}</span></td>
      `;
      mtb.appendChild(tr);
    });
  }

  // ── API Keys table ────────────────────────────────────────────────────────────
  const ktb = document.getElementById("keys-tbody");
  if (!DATA.api_keys.length) {
    ktb.innerHTML = '<tr><td colspan="3" style="text-align:center;color:var(--muted);padding:24px">Nenhuma chave encontrada</td></tr>';
  } else {
    DATA.api_keys.forEach(k => {
      const tr = document.createElement("tr");
      tr.innerHTML = `
        <td><span class="tag">${k.name}</span></td>
        <td class="num">${k.created}</td>
        <td class="num">${k.last_used}</td>
      `;
      ktb.appendChild(tr);
    });
  }

  // ── Countdown (next refresh in 5.5 min) ────────────────────────────────────
  let secs = 330;
  const tick = () => {
    const m = Math.floor(secs / 60), s = String(secs % 60).padStart(2, "0");
    document.getElementById("nxt").textContent = `${m}:${s}`;
    if (--secs < 0) location.reload();
  };
  tick();
  setInterval(tick, 1000);
}

// ── Boot ──────────────────────────────────────────────────────────────────────
initTheme();
if (sessionOk()) showDash();
</script>
</body>
</html>"""

html = HTML.replace("__DATA_JSON__", DATA_JSON).replace("__PWD_HASH__", PWD_HASH)

os.makedirs("docs", exist_ok=True)
with open(OUTPUT_PATH, "w", encoding="utf-8") as f:
    f.write(html)

total_tok = DATA["total_input"] + DATA["total_output"] + DATA["total_cached"]
print(f"✓ Saved {OUTPUT_PATH}")
print(f"  Cost ${DATA['month_cost']:.4f} | Tokens {total_tok:,} | Requests {total_requests:,}")
