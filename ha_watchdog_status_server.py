#!/usr/bin/env python3

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from concurrent.futures import ThreadPoolExecutor
import json
import re
import time
from datetime import datetime
from pathlib import Path
from urllib.parse import unquote, urlsplit
import requests
import tinytuya

from runtime_config import (
    BIND_HOST,
    BOOT_GRACE_PERIOD,
    CHECK_INTERVAL,
    COOLDOWN_AFTER_REBOOT,
    DASHBOARD_HOST,
    DRY_RUN,
    ENABLE_REMOTE_CHECK,
    HA_CORE_URL,
    HA_HOST,
    HA_OBSERVER_URL,
    HARD_FAILURE_THRESHOLD,
    NABU_CASA_TIMEOUT,
    NABU_CASA_URL,
    PORT,
    POWER_OFF_SECONDS,
    REBOOT_WINDOW_SECONDS,
    REQUEST_TIMEOUT,
    SOFT_FAILURE_TIMEOUT,
    TUYA_DEVICE_ID,
    TUYA_DEVICE_IP,
    TUYA_LOCAL_KEY,
    TUYA_VERSION,
    require_settings,
)

# =========================
# CONFIG
# =========================

WATCHDOG_CHECK_INTERVAL = CHECK_INTERVAL
WATCHDOG_FAILURES_REQUIRED = HARD_FAILURE_THRESHOLD
WATCHDOG_BOOT_GRACE = BOOT_GRACE_PERIOD
WATCHDOG_COOLDOWN = COOLDOWN_AFTER_REBOOT
WATCHDOG_POWER_OFF_SECONDS = POWER_OFF_SECONDS
WATCHDOG_MAX_REBOOTS_PER_HOUR = 3
WATCHDOG_REBOOT_WINDOW = REBOOT_WINDOW_SECONDS
WATCHDOG_DRY_RUN = DRY_RUN

# Soft-failure thresholds (mirrors ha_watchdog.py)
SOFT_FAILURE_WARN_AT  = 50   # seconds Core offline (Observer alive) → show SOFT FAILURE badge
SOFT_FAILURE_TIMEOUT  = 120  # seconds Core offline (Observer alive) → watchdog power cycles

BASE_DIR = Path(__file__).resolve().parent
ASSETS_DIR = BASE_DIR / "assets"
LOG_FILE = BASE_DIR / "logs" / "watchdog.log"
SERVER_START_TS = time.time()

# Soft-failure timestamp tracked across requests (set when Core goes offline w/ Observer alive)
_core_offline_since: float = 0.0

HTML = r'''<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <meta name="theme-color" content="#080808" />
  <title>Vœrynth Sentinel</title>
  <style>
    @font-face {
      font-family: 'JetBrains Mono';
      font-style: normal;
      font-weight: 400;
      font-display: swap;
      src: url('/assets/fonts/jetbrains-mono-400.ttf') format('truetype');
    }
    @font-face {
      font-family: 'JetBrains Mono';
      font-style: normal;
      font-weight: 500;
      font-display: swap;
      src: url('/assets/fonts/jetbrains-mono-500.ttf') format('truetype');
    }
    @font-face {
      font-family: 'Kumbh Sans';
      font-style: normal;
      font-weight: 300;
      font-display: swap;
      src: url('/assets/fonts/kumbh-sans-300.ttf') format('truetype');
    }
    @font-face {
      font-family: 'Kumbh Sans';
      font-style: normal;
      font-weight: 400;
      font-display: swap;
      src: url('/assets/fonts/kumbh-sans-400.ttf') format('truetype');
    }
    @font-face {
      font-family: 'Kumbh Sans';
      font-style: normal;
      font-weight: 500;
      font-display: swap;
      src: url('/assets/fonts/kumbh-sans-500.ttf') format('truetype');
    }
    @font-face {
      font-family: 'Kumbh Sans';
      font-style: normal;
      font-weight: 600;
      font-display: swap;
      src: url('/assets/fonts/kumbh-sans-600.ttf') format('truetype');
    }
    @font-face {
      font-family: 'Kumbh Sans';
      font-style: normal;
      font-weight: 700;
      font-display: swap;
      src: url('/assets/fonts/kumbh-sans-700.ttf') format('truetype');
    }
    @font-face {
      font-family: 'Playfair Display';
      font-style: normal;
      font-weight: 600;
      font-display: swap;
      src: url('/assets/fonts/playfair-display-600.ttf') format('truetype');
    }
    @font-face {
      font-family: 'Playfair Display';
      font-style: normal;
      font-weight: 700;
      font-display: swap;
      src: url('/assets/fonts/playfair-display-700.ttf') format('truetype');
    }
    :root {
      --bg:          #080808;
      --surface:     rgba(12,12,12,0.92);
      --surface2:    rgba(20,18,14,0.88);
      --border:      rgba(212,175,55,0.13);
      --border-hi:   rgba(212,175,55,0.32);
      --gold:        #D4AF37;
      --gold2:       #C9A961;
      --ok:          #4ade80;
      --warn:        #fbbf24;
      --bad:         #f87171;
      --text:        rgba(255,255,255,0.90);
      --text-muted:  rgba(255,255,255,0.52);
      --text-dim:    rgba(255,255,255,0.24);
      --font:        'Kumbh Sans', ui-sans-serif, system-ui, sans-serif;
      --font-mono:   'JetBrains Mono', 'Courier New', monospace;
    }
    *, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }
    html { scrollbar-width:thin; scrollbar-color:rgba(212,175,55,0.25) transparent; }
    ::-webkit-scrollbar { width:5px; }
    ::-webkit-scrollbar-track { background:transparent; }
    ::-webkit-scrollbar-thumb { background:rgba(212,175,55,0.22); border-radius:3px; }
    body {
      background: var(--bg);
      color: var(--text);
      font-family: var(--font);
      font-size: 14px;
      min-height: 100vh;
      overflow-x: hidden;
      -webkit-font-smoothing: antialiased;
    }
    body::before {
      content: '';
      position: fixed; inset: 0; z-index: 0; pointer-events: none;
      background:
        radial-gradient(ellipse 60% 45% at 10% 0%,   rgba(212,175,55,0.045) 0%, transparent 60%),
        radial-gradient(ellipse 50% 40% at 90% 100%,  rgba(201,169,97,0.035) 0%, transparent 60%);
    }
    .app { position:relative; z-index:1; max-width:1400px; margin:0 auto; padding:1.5rem 2rem; }

    /* ── TOP BAR ───────────────────────────────── */
    .topbar { display:flex; justify-content:space-between; align-items:center; padding-bottom:1.4rem; border-bottom:1px solid var(--border); margin-bottom:1.4rem; }
    .topbar-left { display:flex; align-items:center; gap:1rem; }
    .logo-img { height:38px; width:auto; flex-shrink:0; filter:drop-shadow(0 0 8px rgba(212,175,55,0.35)); }
    .sys-title { font-size:0.88rem; font-weight:600; color:rgba(255,255,255,0.88); letter-spacing:0.06em; text-transform:uppercase; }
    .sys-sub   { font-size:0.65rem; color:var(--gold2); opacity:0.75; margin-top:0.12rem; letter-spacing:0.04em; }
    .topbar-right { display:flex; align-items:center; gap:1rem; }
    .clock-block { text-align:right; }
    #live-clock { font-family:var(--font-mono); font-size:1rem; font-weight:500; color:rgba(255,255,255,0.82); letter-spacing:0.08em; }
    #live-date  { font-size:0.62rem; color:var(--text-dim); margin-top:0.1rem; letter-spacing:0.05em; }
    .topbar-btns { display:flex; gap:0.4rem; }
    .btn { background:rgba(212,175,55,0.04); color:var(--text-muted); border:1px solid var(--border); padding:0.32rem 0.75rem; font-family:var(--font); font-size:0.68rem; font-weight:500; border-radius:6px; cursor:pointer; transition:all 0.18s; letter-spacing:0.03em; }
    .btn:hover { background:rgba(212,175,55,0.1); border-color:rgba(212,175,55,0.35); color:var(--gold); }

    /* ── STATUS BAR ────────────────────────────── */
    .statusbar { display:flex; align-items:center; gap:1.25rem; padding:0.8rem 1.2rem; background:rgba(10,9,6,0.88); border:1px solid var(--border); border-radius:10px; margin-bottom:1.4rem; backdrop-filter:blur(16px); flex-wrap:wrap; }
    .sb-item { display:flex; align-items:center; gap:0.5rem; font-size:0.72rem; color:var(--text-dim); }
    .sb-item strong { color:var(--text-muted); font-weight:500; }
    .sb-sep { width:1px; height:18px; background:rgba(212,175,55,0.15); flex-shrink:0; }
    .os-badge { display:inline-flex; align-items:center; gap:0.3rem; padding:0.2rem 0.7rem; border-radius:4px; font-size:0.65rem; font-weight:600; letter-spacing:0.07em; text-transform:uppercase; }
    .os-badge.ok   { background:rgba(74,222,128,0.1);  color:#4ade80; border:1px solid rgba(74,222,128,0.25); }
    .os-badge.warn { background:rgba(251,191,36,0.1);  color:#fbbf24; border:1px solid rgba(251,191,36,0.25); }
    .os-badge.bad  { background:rgba(248,113,113,0.1); color:#f87171; border:1px solid rgba(248,113,113,0.25); }
    .os-badge.init { background:rgba(212,175,55,0.08); color:var(--gold2); border:1px solid rgba(212,175,55,0.2); }
    .bd { width:6px; height:6px; border-radius:50%; background:currentColor; flex-shrink:0; }
    .bd.pulse { animation:dot-pulse 2.4s ease-in-out infinite; }
    @keyframes dot-pulse { 0%,100%{opacity:1} 50%{opacity:0.25} }

    /* ── GRID ──────────────────────────────────── */
    .grid { display:grid; grid-template-columns:repeat(12,1fr); gap:1.1rem; }

    /* ── CARD ──────────────────────────────────── */
    .card { background:rgba(10,9,6,0.85); border:1px solid var(--border); border-radius:10px; padding:1.25rem; backdrop-filter:blur(16px); grid-column:span 4; position:relative; overflow:hidden; transition:border-color 0.22s, box-shadow 0.22s; }
    .card::before { content:''; position:absolute; top:0; left:0; right:0; height:1px; background:linear-gradient(90deg,transparent,var(--cl,rgba(212,175,55,0.28)),transparent); }
    .card:hover { border-color:rgba(212,175,55,0.28); box-shadow:0 6px 32px rgba(0,0,0,0.5), 0 0 0 1px rgba(212,175,55,0.06); }
    .card.wide { grid-column:span 8; }
    .card.full { grid-column:span 12; }
    .card-header { display:flex; justify-content:space-between; align-items:center; margin-bottom:0.8rem; }
    .card-label { font-size:0.62rem; font-weight:600; text-transform:uppercase; letter-spacing:0.12em; color:var(--gold2); opacity:0.65; }
    .card-title { font-size:0.9rem; font-weight:600; color:rgba(255,255,255,0.88); margin-bottom:0.3rem; letter-spacing:0.01em; }
    .card-body  { font-size:0.72rem; color:var(--text-muted); line-height:1.6; }

    /* ── PILL ──────────────────────────────────── */
    .pill { display:inline-flex; align-items:center; gap:0.25rem; padding:0.12rem 0.42rem; border-radius:4px; font-size:0.58rem; font-weight:600; letter-spacing:0.06em; text-transform:uppercase; line-height:1.4; }
    .pill.ok   { background:rgba(74,222,128,0.1);  color:#4ade80; border:1px solid rgba(74,222,128,0.25); }
    .pill.warn { background:rgba(251,191,36,0.1);  color:#fbbf24; border:1px solid rgba(251,191,36,0.25); }
    .pill.bad  { background:rgba(248,113,113,0.1); color:#f87171; border:1px solid rgba(248,113,113,0.25); }
    .pill.init { background:rgba(212,175,55,0.08); color:var(--gold2); border:1px solid rgba(212,175,55,0.2); }
    .dot { width:5px; height:5px; border-radius:50%; background:currentColor; flex-shrink:0; }
    .pill.ok .dot { animation:dot-pulse 1.6s ease-in-out infinite; box-shadow:0 0 5px currentColor; }
    .relay-dot { display:inline-block; width:7px; height:7px; border-radius:50%; margin-right:5px; vertical-align:middle; flex-shrink:0; }
    .relay-dot.on  { background:#4ade80; box-shadow:0 0 6px #4ade80, 0 0 12px rgba(74,222,128,0.4); animation:relay-pulse 1.8s ease-in-out infinite; }
    .relay-dot.off { background:rgba(255,255,255,0.18); box-shadow:none; }
    @keyframes relay-pulse { 0%,100%{opacity:1;box-shadow:0 0 6px #4ade80,0 0 12px rgba(74,222,128,0.4)} 50%{opacity:0.45;box-shadow:0 0 3px #4ade80} }

    /* ── METRICS ───────────────────────────────── */
    .metrics { display:flex; gap:1.5rem; margin-top:1rem; padding-top:1rem; border-top:1px solid var(--border); flex-wrap:wrap; }
    .metric .k { font-size:0.6rem; font-weight:500; text-transform:uppercase; letter-spacing:0.1em; color:var(--gold2); opacity:0.55; margin-bottom:0.2rem; }
    .metric .v { font-size:1rem; font-weight:700; color:rgba(255,255,255,0.88); }

    /* ── BAR ───────────────────────────────────── */
    .bar-wrap { margin-top:0.75rem; }
    .bar-label { display:flex; justify-content:space-between; font-size:0.67rem; color:var(--text-muted); margin-bottom:0.32rem; }
    .bar-track { height:3px; background:rgba(212,175,55,0.08); border-radius:2px; overflow:hidden; }
    .bar-fill  { height:100%; border-radius:2px; transition:width 0.65s cubic-bezier(0.4,0,0.2,1); background:linear-gradient(90deg,var(--bar-a,#D4AF37),var(--bar-b,#C9A961)); }

    /* ── UPTIME ────────────────────────────────── */
    .uptime-val   { font-family:var(--font-mono); font-size:1.5rem; font-weight:400; color:var(--gold); letter-spacing:0.08em; margin:0.5rem 0 0.18rem; text-shadow:0 0 18px rgba(212,175,55,0.3); }
    .uptime-label { font-size:0.6rem; color:var(--gold2); opacity:0.55; text-transform:uppercase; letter-spacing:0.12em; }

    /* ── STATE BADGES ──────────────────────────── */
    .badges { display:flex; gap:0.4rem; margin-top:0.75rem; flex-wrap:wrap; }
    .sbadge { display:inline-flex; align-items:center; gap:0.22rem; padding:0.14rem 0.48rem; border-radius:4px; font-size:0.58rem; font-weight:600; letter-spacing:0.07em; text-transform:uppercase; }
    .sbadge.cd { background:rgba(251,191,36,0.1);  color:#fbbf24; border:1px solid rgba(251,191,36,0.22); }
    .sbadge.bg { background:rgba(212,175,55,0.08); color:var(--gold2); border:1px solid rgba(212,175,55,0.2); }
    .sbadge.dr { background:rgba(248,113,113,0.1); color:#f87171; border:1px solid rgba(248,113,113,0.22); }
    .sbadge.sf { background:rgba(251,146,60,0.12); color:#fb923c; border:1px solid rgba(251,146,60,0.3); animation:sf-pulse 1.4s ease-in-out infinite; }
    .sbadge.ni { background:rgba(56,189,248,0.1);  color:#38bdf8; border:1px solid rgba(56,189,248,0.28); animation:sf-pulse 2.2s ease-in-out infinite; }
    @keyframes sf-pulse { 0%,100%{ opacity:1; } 50%{ opacity:0.6; } }
    .sf-countdown { display:inline-flex; align-items:center; gap:0.3rem; padding:0.14rem 0.55rem; border-radius:4px; font-size:0.62rem; font-weight:700; letter-spacing:0.04em; background:rgba(239,68,68,0.12); color:#ef4444; border:1px solid rgba(239,68,68,0.3); font-variant-numeric:tabular-nums; }

    /* ── POLICY GRID ───────────────────────────── */
    .policy-grid { display:grid; grid-template-columns:1fr 1fr; gap:0.55rem; margin-top:0.8rem; }
    .pi { background:rgba(18,16,10,0.8); border:1px solid rgba(212,175,55,0.1); border-radius:7px; padding:0.55rem 0.7rem; }
    .pi .pk { font-size:0.58rem; text-transform:uppercase; letter-spacing:0.1em; color:var(--gold2); opacity:0.55; margin-bottom:0.14rem; }
    .pi .pv { font-size:0.88rem; font-weight:600; color:rgba(255,255,255,0.85); }

    /* ── TERMINAL ──────────────────────────────── */
    .terminal { background:rgba(4,3,1,0.95); border:1px solid rgba(212,175,55,0.1); border-radius:8px; padding:0.85rem; font-family:var(--font-mono); font-size:0.69rem; line-height:1.8; max-height:210px; overflow-y:auto; margin-top:0.75rem; scrollbar-width:thin; scrollbar-color:rgba(212,175,55,0.2) transparent; }
    .terminal::-webkit-scrollbar { width:3px; }
    .terminal::-webkit-scrollbar-thumb { background:rgba(212,175,55,0.2); border-radius:2px; }
    .tline { display:flex; gap:0.65rem; padding:0.05rem 0; border-bottom:1px solid rgba(212,175,55,0.05); animation:tline-in 0.25s ease forwards; }
    @keyframes tline-in { from{opacity:0;transform:translateX(-4px)} to{opacity:1;transform:translateX(0)} }
    .tts  { color:rgba(212,175,55,0.3); flex-shrink:0; }
    .tlv  { flex-shrink:0; }
    .tlv.ok   { color:#4ade80; }
    .tlv.warn { color:#fbbf24; }
    .tlv.bad  { color:#f87171; }
    .tlv.init { color:var(--gold2); }
    .tmsg { color:rgba(255,255,255,0.42); }

    /* ── RING (hidden, JS compat) ───────────────── */
    .ring-ok   { stroke:#4ade80; }
    .ring-warn { stroke:#fbbf24; }
    .ring-bad  { stroke:#f87171; }
    .ring-gold { stroke:#D4AF37; }

    /* ── FOOTER ────────────────────────────────── */
    .footer { margin-top:2rem; padding-top:1rem; border-top:1px solid var(--border); display:flex; justify-content:space-between; align-items:center; font-size:0.63rem; color:var(--text-dim); letter-spacing:0.04em; }
    .footer-live { display:flex; align-items:center; gap:0.4rem; }
    .footer-dot  { width:5px; height:5px; border-radius:50%; background:var(--gold); opacity:0.7; animation:dot-pulse 2.4s infinite; }

    /* ── ANIMATIONS ────────────────────────────── */
    .fi { opacity:0; animation:fade-up 0.5s ease forwards; }
    @keyframes fade-up { from{opacity:0;transform:translateY(10px)} to{opacity:1;transform:translateY(0)} }
    .fi:nth-child(1){animation-delay:0.04s} .fi:nth-child(2){animation-delay:0.11s}
    .fi:nth-child(3){animation-delay:0.18s} .fi:nth-child(4){animation-delay:0.25s}
    .fi:nth-child(5){animation-delay:0.32s} .fi:nth-child(6){animation-delay:0.39s}
    .fi:nth-child(7){animation-delay:0.46s} .fi:nth-child(8){animation-delay:0.53s}

    /* ── RESPONSIVE ────────────────────────────── */
    @media(max-width:1100px){ .card{grid-column:span 6} .card.wide{grid-column:span 12} }
    @media(max-width:720px) { .card,.card.wide{grid-column:span 12} .topbar{flex-direction:column;gap:1rem;align-items:flex-start} #watchdog-card{order:-1} }
  </style>
</head>


<body>
  <div class="app">

    <!-- ── TOP BAR ─────────────────────────────────────── -->
    <nav class="topbar fi">
      <div class="topbar-left">
        <img class="logo-img" src="/assets/images/logo-gold-200.png" alt="Vœrynth" onerror="this.style.display='none';document.getElementById('logo-fallback').style.display='flex';" />
        <div id="logo-fallback" style="display:none;width:38px;height:38px;background:linear-gradient(135deg,#D4AF37,#C9A961);border-radius:8px;align-items:center;justify-content:center;font-size:17px;font-weight:700;color:#080808;">V</div>
        <div>
          <div class="sys-title">VŒRYNTH SENTINEL</div>
          <div class="sys-sub">Estate Watchdog · Local Oversight Node</div>
        </div>
      </div>
      <div class="topbar-right">
        <div class="clock-block">
          <div id="live-clock">00:00:00</div>
          <div id="live-date">—</div>
        </div>
        <div class="topbar-btns">
          <button class="btn" onclick="refreshStatus()">⟳ Refresh</button>
          <button class="btn" onclick="window.location.reload()">↺ Reload</button>
        </div>
      </div>
    </nav>

    <!-- ── STATUS BAR ──────────────────────────────────── -->
    <div class="statusbar fi">
      <div class="sb-item"><span>Node</span><strong id="header-host">—</strong></div>
      <div class="sb-sep"></div>
      <div class="sb-item"><span>Relay</span><strong id="header-relay">—</strong></div>
      <div class="sb-sep"></div>
      <div class="sb-item"><span>Checked</span><strong id="header-checked">—</strong></div>
      <div class="sb-sep"></div>
      <div id="overall-badge" class="os-badge init"><span class="bd pulse"></span>&nbsp;<span id="header-overall">INIT</span></div>
    </div>

    <!-- hidden ring elements for JS compatibility -->
    <svg style="display:none" aria-hidden="true">
      <circle id="ring-progress" cx="100" cy="100" r="84" fill="none" stroke-width="10"
              stroke-dasharray="527.8" stroke-dashoffset="527.8" class="ring-gold"/>
    </svg>
    <span id="orb-icon"   style="display:none"></span>
    <span id="orb-status" style="display:none"></span>

    <!-- ── CARDS ────────────────────────────────────────── -->
    <div class="grid">

      <!-- Core -->
      <article class="card fi" style="--cl:rgba(212,175,55,0.28);">
        <div class="card-header">
          <div class="card-label">Vœrynth Core · :8123</div>
          <div id="core-pill" class="pill init"><span class="dot"></span>Init</div>
        </div>
        <div class="card-title" id="core-headline">Checking...</div>
        <div class="card-body"  id="core-text">—</div>
      </article>

      <!-- Observer -->
      <article class="card fi" style="--cl:rgba(201,169,97,0.22);">
        <div class="card-header">
          <div class="card-label">Vœrynth Observer · :4357</div>
          <div id="observer-pill" class="pill init"><span class="dot"></span>Init</div>
        </div>
        <div class="card-title" id="observer-headline">Checking...</div>
        <div class="card-body"  id="observer-text">—</div>
      </article>

      <!-- Nabu Casa Remote -->
      <article class="card fi" style="--cl:rgba(168,139,250,0.22);">
        <div class="card-header">
          <div class="card-label">Nabu Casa · Remote Access</div>
          <div id="remote-pill" class="pill init"><span class="dot"></span>Init</div>
        </div>
        <div class="card-title" id="remote-headline">Checking...</div>
        <div class="card-body"  id="remote-text">—</div>
      </article>

      <!-- Watchdog -->
      <article id="watchdog-card" class="card fi" style="--cl:rgba(212,175,55,0.22);">
        <div class="card-header">
          <div class="card-label">Watchdog Engine</div>
          <div id="watchdog-pill" class="pill init"><span class="dot"></span>Init</div>
        </div>
        <div class="card-title" id="watchdog-headline">Reading state...</div>
        <div class="uptime-val"   id="watchdog-uptime">--:--:--</div>
        <div class="uptime-label">Server Uptime</div>
        <div class="card-body" style="margin-top:0.5rem;" id="watchdog-text"></div>
        <div style="margin-top:0.9rem;">
          <div class="bar-wrap">
            <div class="bar-label"><span>Failure Streak</span><span id="wdog-fail-label">0 / 5</span></div>
            <div class="bar-track"><div class="bar-fill" id="wdog-fail-bar" style="width:0%;--bar-a:#fbbf24;--bar-b:#f87171;"></div></div>
          </div>
          <div class="bar-wrap">
            <div class="bar-label"><span>Reboots / hr</span><span id="wdog-reboot-label">0 / 3</span></div>
            <div class="bar-track"><div class="bar-fill" id="wdog-reboot-bar" style="width:0%;--bar-a:#D4AF37;--bar-b:#f87171;"></div></div>
          </div>
        </div>
        <div id="wdog-badges" class="badges">
          <div id="wdog-state-badges" style="display:contents;"></div>
          <span class="sf-countdown" id="sf-countdown" style="display:none;"></span>
        </div>
      </article>

      <!-- Power Relay (wide) -->
      <article class="card wide fi" style="--cl:rgba(212,175,55,0.32);">
        <div class="card-header">
          <div class="card-label">Power Relay · Tuya Smart Plug</div>
          <div id="plug-pill" class="pill init"><span class="dot"></span>Init</div>
        </div>
        <div class="card-title" id="plug-headline">Querying relay...</div>
        <div class="card-body"  id="plug-text">Relay is being queried directly over the local network.</div>
        <div style="margin-top:1rem;">
          <div class="bar-wrap">
            <div class="bar-label"><span>Power</span><span id="plug-power">—</span></div>
            <div class="bar-track"><div class="bar-fill" id="plug-power-bar" style="width:0%;--bar-a:#D4AF37;--bar-b:#fbbf24;"></div></div>
          </div>
          <div class="bar-wrap">
            <div class="bar-label"><span>Voltage</span><span id="plug-voltage">—</span></div>
            <div class="bar-track"><div class="bar-fill" id="plug-voltage-bar" style="width:0%;--bar-a:#C9A961;--bar-b:#D4AF37;"></div></div>
          </div>
          <div class="bar-wrap">
            <div class="bar-label"><span>Current</span><span id="plug-current">—</span></div>
            <div class="bar-track"><div class="bar-fill" id="plug-current-bar" style="width:0%;--bar-a:#fbbf24;--bar-b:#f87171;"></div></div>
          </div>
        </div>
        <div class="metrics">
          <div class="metric"><div class="k">Device IP</div><div class="v" id="plug-ip">—</div></div>
          <div class="metric"><div class="k">Relay State</div><div class="v" id="plug-state">—</div></div>
          <div class="metric"><div class="k">Vœrynth Host</div><div class="v" id="host-value">—</div></div>
          <div class="metric"><div class="k">Server</div><div class="v" id="server-value">—</div></div>
        </div>
      </article>

      <!-- Policy -->
      <article class="card fi" style="--cl:rgba(212,175,55,0.25);">
        <div class="card-header">
          <div class="card-label">Recovery Policy</div>
          <div id="policy-dryrun-pill" style="display:none;" class="pill bad"><span class="dot"></span>DRY RUN</div>
        </div>
        <div class="card-title">Threshold Parameters</div>
        <div class="policy-grid">
          <div class="pi"><div class="pk">Check Interval</div><div class="pv" id="policy-check">—</div></div>
          <div class="pi"><div class="pk">Fails Required</div><div class="pv" id="policy-fails">—</div></div>
          <div class="pi"><div class="pk">Boot Grace</div><div class="pv" id="policy-boot">—</div></div>
          <div class="pi"><div class="pk">Cooldown</div><div class="pv" id="policy-cooldown">—</div></div>
          <div class="pi"><div class="pk">Max Reboots/hr</div><div class="pv" id="policy-max-reboots">—</div></div>
          <div class="pi"><div class="pk">Reboot Window</div><div class="pv" id="policy-reboot-window">—</div></div>
          <div class="pi"><div class="pk">Plug Off Time</div><div class="pv" id="policy-power-off">—</div></div>
        </div>
      </article>

      <!-- Terminal Log (wide) -->
      <article class="card wide fi" style="--cl:rgba(201,169,97,0.2);">
        <div class="card-header">
          <div class="card-label">Activity Log · Watchdog Events</div>
          <div style="font-size:0.62rem;color:var(--text-dim);" id="log-ts">—</div>
        </div>
        <div class="card-title" id="log-headline">Awaiting log...</div>
        <div class="terminal" id="terminal">
          <div class="tline">
            <span class="tts">--:--:--</span>
            <span class="tlv init">[INIT]</span>
            <span class="tmsg">Sentinel console online. Awaiting first poll...</span>
          </div>
        </div>
      </article>

    </div><!-- /grid -->

    <!-- ── FOOTER ──────────────────────────────────────── -->
    <footer class="footer">
      <div class="footer-live">
        <div class="footer-dot"></div>
        <span id="footer">Polling every 2 seconds</span>
      </div>
      <span>VŒRYNTH SENTINEL · LOCAL NODE</span>
    </footer>

  </div><!-- /app -->

  <script>

    // ── LIVE CLOCK ───────────────────────────────────────
    function tickClock() {
      const n = new Date();
      const pad = v => String(v).padStart(2, '0');
      document.getElementById('live-clock').textContent =
        `${pad(n.getHours())}:${pad(n.getMinutes())}:${pad(n.getSeconds())}`;
      const days   = ['SUN','MON','TUE','WED','THU','FRI','SAT'];
      const months = ['JAN','FEB','MAR','APR','MAY','JUN','JUL','AUG','SEP','OCT','NOV','DEC'];
      document.getElementById('live-date').textContent =
        `${days[n.getDay()]} · ${n.getDate()} ${months[n.getMonth()]} ${n.getFullYear()}`;
    }
    setInterval(tickClock, 1000); tickClock();

    // ── LIVE UPTIME TICKER ───────────────────────────────
    let _uptimeBase = 0, _uptimeRefAt = Date.now();
    setInterval(() => {
      const total = _uptimeBase + Math.floor((Date.now() - _uptimeRefAt) / 1000);
      const pad = v => String(v).padStart(2,'0');
      document.getElementById('watchdog-uptime').textContent =
        `${pad(Math.floor(total/3600))}:${pad(Math.floor((total%3600)/60))}:${pad(total%60)}`;
    }, 1000);

    // ── SOFT FAILURE COUNTDOWN (updates every second locally) ────
    // reboot_at is set on each poll; the 1-second ticker keeps it smooth between polls.
    let _reboot_at = 0;   // epoch-ms when watchdog will power-cycle (0 = not in soft failure)
    setInterval(() => {
      const cntEl = document.getElementById('sf-countdown');
      if (!cntEl) return;
      if (_reboot_at === 0) { cntEl.style.display = 'none'; return; }
      const rem = Math.max(0, Math.ceil((_reboot_at - Date.now()) / 1000));
      cntEl.style.display = 'inline-flex';
      cntEl.textContent = `⚡ REBOOT IN ${rem}s`;
    }, 1000);

    // ── STATUS BADGE ─────────────────────────────────────
    function setRing(kind) {
      // update hidden SVG ring (JS compat)
      const ring = document.getElementById('ring-progress');
      const C = 527.8;
      const MAP = {
        ok:   { offset: 0,        cls: 'ring-ok',   label: 'NODE HEALTHY'  },
        warn: { offset: C * 0.35, cls: 'ring-warn', label: 'CORE DEGRADED' },
        bad:  { offset: C * 0.75, cls: 'ring-bad',  label: 'NODE DOWN'     },
        gold: { offset: C * 0.5,  cls: 'ring-gold', label: 'INIT'          },
      };
      const cfg = MAP[kind] || MAP.gold;
      if (ring) { ring.style.strokeDashoffset = cfg.offset; ring.className = cfg.cls; }
      // update visible status bar badge
      const badge = document.getElementById('overall-badge');
      if (badge) badge.className = `os-badge ${kind === 'gold' ? 'init' : kind}`;
    }

    // ── HELPERS ──────────────────────────────────────────
    function applyPill(id, text, kind) {
      const el = document.getElementById(id);
      if (!el) return;
      // map legacy 'gold' kind to 'init' for new CSS
      const cls = kind === 'gold' ? 'init' : kind;
      el.className = `pill ${cls}`;
      el.innerHTML = `<span class="dot"></span>${text}`;
    }

    function setText(id, val) {
      const el = document.getElementById(id);
      if (el) el.textContent = val;
    }

    function setBar(barId, pct) {
      const el = document.getElementById(barId);
      if (el) el.style.width = Math.min(100, Math.max(0, pct)) + '%';
    }

    // ── TERMINAL LOG ─────────────────────────────────────
    const termLog = [];
    function pushLog(ts, level, msg) {
      termLog.push({ ts, level, msg });
      if (termLog.length > 25) termLog.shift();
      const el = document.getElementById('terminal');
      const lvMap = { INFO:'ok', WARN:'warn', ERROR:'bad', INIT:'init' };
      el.innerHTML = termLog.slice(-10).map(l =>
        `<div class="tline">` +
        `<span class="tts">${l.ts}</span>` +
        `<span class="tlv ${lvMap[l.level] || 'ok'}">[${l.level}]</span>` +
        `<span class="tmsg">${l.msg.length > 110 ? l.msg.slice(-110) : l.msg}</span>` +
        `</div>`
      ).join('');
      el.scrollTop = el.scrollHeight;
    }

    function logLevel(line) {
      if (!line) return 'INFO';
      const l = line.toLowerCase();
      if (l.includes('threshold') || l.includes('unreachable')) return 'ERROR';
      if (l.includes('error') || l.includes('fail') || l.includes('power cycle') || l.includes('down')) return 'WARN';
      return 'INFO';
    }

    // ── MAIN POLL ────────────────────────────────────────
    let lastLogLine = '', pollCount = 0;

    async function refreshStatus() {
      try {
        const ctrl = new AbortController();
        const tid  = setTimeout(() => ctrl.abort(), 12000); // 12 s hard timeout
        const res  = await fetch('/api/status', { cache: 'no-store', signal: ctrl.signal });
        clearTimeout(tid);
        const d = await res.json();
        pollCount++;

        _uptimeBase  = d.watchdog.uptime_seconds;
        _uptimeRefAt = Date.now();

        const remoteEnabled = d.remote.enabled !== false;
        const internetDown = remoteEnabled && !d.remote.ok && d.core.ok && d.observer.ok;
        const vkind = d.core.ok ? (internetDown ? 'warn' : 'ok') : d.observer.ok ? 'warn' : 'bad';
        setRing(vkind);

        setText('header-host',    d.host);
        setText('header-checked', d.checked_at);

        const relayEl = document.getElementById('header-relay');
        if (relayEl) {
          const on = d.plug.ok && d.plug.relay_on;
          const offCol = d.plug.ok ? '#fbbf24' : '#f87171';
          const stateHtml = d.plug.ok
            ? (on ? `<span class="relay-dot on" style="width:6px;height:6px;"></span>ON`
                  : `<span class="relay-dot off" style="width:6px;height:6px;"></span>OFF`)
            : 'Error';
          relayEl.style.color = on ? '#4ade80' : offCol;
          relayEl.innerHTML = `${d.plug.device_ip || '\u2014'} \u00b7 ${stateHtml}`;
        }

        const verdictLabels = { ok: 'Node Healthy', warn: internetDown ? 'No Internet · Node Healthy' : 'Vœrynth Core Degraded', bad: 'Node Down' };
        setText('header-overall', verdictLabels[vkind]);

        // Core
        if (d.core.ok) {
          applyPill('core-pill', `UP \u00b7 ${d.core.status}`, 'ok');
          setText('core-headline', 'Vœrynth Core responding normally');
          setText('core-text', `HTTP ${d.core.status} \u00b7 ${d.core.url}`);
        } else {
          applyPill('core-pill', 'DOWN', 'bad');
          setText('core-headline', 'Vœrynth Core unreachable');
          setText('core-text', d.core.error || 'No response');
        }

        // Observer
        if (d.observer.ok) {
          applyPill('observer-pill', `UP \u00b7 ${d.observer.status}`, 'ok');
          setText('observer-headline', 'Vœrynth Observer responding normally');
          setText('observer-text', `HTTP ${d.observer.status} \u00b7 ${d.observer.url}`);
        } else {
          applyPill('observer-pill', 'DOWN', 'bad');
          setText('observer-headline', 'Vœrynth Observer unreachable');
          setText('observer-text', d.observer.error || 'No response');
        }

        // Nabu Casa remote
        if (!remoteEnabled) {
          applyPill('remote-pill', 'LOCAL', 'init');
          setText('remote-headline', 'Remote internet probe disabled');
          setText('remote-text', 'Dashboard is serving a fully local / offline status view');
        } else if (d.remote.ok) {
          applyPill('remote-pill', `UP \u00b7 ${d.remote.status}`, 'ok');
          setText('remote-headline', 'Remote access reachable from internet');
          setText('remote-text', `HTTP ${d.remote.status} \u00b7 ${d.remote.url}`);
        } else {
          applyPill('remote-pill', 'DOWN', 'bad');
          setText('remote-headline', 'Nabu Casa endpoint unreachable');
          setText('remote-text', d.remote.error || 'No response from cloud relay');
        }

        // Watchdog
        applyPill('watchdog-pill', d.watchdog.active ? 'Active' : 'Unknown', d.watchdog.active ? 'ok' : 'warn');
        setText('watchdog-headline', d.watchdog.active ? 'Guardian process is live' : 'No fresh proof from log');
        setText('watchdog-text', d.watchdog.last_log_ts ? `Last log: ${d.watchdog.last_log_ts}` : '');

        // Failure streak bar
        const wf = d.watchdog, pol = d.policy;
        const failPct = pol.failures_required > 0 ? (wf.consecutive_failures / pol.failures_required) * 100 : 0;
        setBar('wdog-fail-bar', failPct);
        setText('wdog-fail-label', `${wf.consecutive_failures} / ${pol.failures_required}`);

        // Reboots per hour bar
        const rebootPct = pol.max_reboots_per_hour > 0 ? (wf.reboots_last_hour / pol.max_reboots_per_hour) * 100 : 0;
        setBar('wdog-reboot-bar', rebootPct);
        setText('wdog-reboot-label', `${wf.reboots_last_hour} / ${pol.max_reboots_per_hour}`);

        // State badges — write into inner div so the countdown span sibling is never wiped
        const sf = d.soft_failure || {};
        const stateBadges = document.getElementById('wdog-state-badges');
        if (stateBadges) {
          const bl = [];
          if (wf.in_cooldown)   bl.push('<span class="sbadge cd"><span class="dot"></span>COOLDOWN</span>');
          if (wf.in_boot_grace) bl.push('<span class="sbadge bg"><span class="dot"></span>BOOT GRACE</span>');
          if (sf.active)        bl.push('<span class="sbadge sf"><span class="dot"></span>SOFT FAILURE</span>');
          if (internetDown)     bl.push('<span class="sbadge ni"><span class="dot"></span>NO INTERNET</span>');
          if (pol.dry_run)      bl.push('<span class="sbadge dr"><span class="dot"></span>DRY RUN</span>');
          stateBadges.innerHTML = bl.join('');
        }
        // Update the reboot countdown target so the 1s ticker (separate stable element) can animate
        if (sf.active && sf.remaining > 0) {
          _reboot_at = Date.now() + sf.remaining * 1000;
        } else {
          _reboot_at = 0;
        }

        // Dry-run pill in policy header
        const drPill = document.getElementById('policy-dryrun-pill');
        if (drPill) drPill.style.display = pol.dry_run ? 'inline-flex' : 'none';

        // Plug
        if (!d.plug.ok) {
          applyPill('plug-pill', 'Relay Error', 'bad');
          setText('plug-headline', 'Relay could not be queried');
          setText('plug-text', d.plug.error || 'No reply from Tuya device');
          ['plug-power','plug-voltage','plug-current'].forEach(id => setText(id, '\u2014'));
          ['plug-power-bar','plug-voltage-bar','plug-current-bar'].forEach(id => setBar(id, 0));
          setText('plug-state', '\u2014');
        } else {
          const on = !!d.plug.relay_on;
          applyPill('plug-pill', on ? 'Relay ON' : 'Relay OFF', on ? 'ok' : 'warn');
          setText('plug-headline', on ? 'Power path is energised' : 'Power path is cut');
          setText('plug-text', on
            ? 'The NUC is receiving mains power through the watchdog relay.'
            : 'Relay is off \u2014 investigate if unintentional.');
          const stateEl = document.getElementById('plug-state');
          if (stateEl) stateEl.innerHTML = on
            ? `<span class="relay-dot on"></span>ON`
            : `<span class="relay-dot off"></span>OFF`;

          const pw = d.plug.power_w, vv = d.plug.voltage_v, ma = d.plug.current_ma;
          setText('plug-power',   pw !== null ? `${pw.toFixed(1)}W`      : '\u2014');
          setText('plug-voltage', vv !== null ? `${vv.toFixed(1)}V`      : '\u2014');
          setText('plug-current', ma !== null ? `${Math.round(ma)}mA`    : '\u2014');
          setBar('plug-power-bar',   pw !== null ? (pw / 300)  * 100 : 0);
          setBar('plug-voltage-bar', vv !== null ? (vv / 260)  * 100 : 0);
          setBar('plug-current-bar', ma !== null ? (ma / 3000) * 100 : 0);
        }

        setText('plug-ip',     d.plug.device_ip || '\u2014');
        setText('host-value',  d.host);
        setText('server-value', d.server_display);

        // Policy
        setText('policy-check',         `${d.policy.check_interval}s`);
        setText('policy-fails',         `${d.policy.failures_required}`);
        setText('policy-boot',          `${d.policy.boot_grace}s`);
        setText('policy-cooldown',      `${d.policy.cooldown}s`);
        setText('policy-max-reboots',   `${d.policy.max_reboots_per_hour}`);
        setText('policy-reboot-window', `${Math.round(d.policy.reboot_window / 60)}min`);
        setText('policy-power-off',     `${d.policy.power_off_seconds}s`);

        // Terminal log — parse "YYYY-MM-DD HH:MM:SS | LEVEL | message" format
        function parseLine(raw) {
          const parts = raw.split(' | ');
          if (parts.length >= 3) {
            const ts  = parts[0].slice(11, 19);
            const rawLv = parts[1].trim();
            const lv  = rawLv === 'WARNING' ? 'WARN' : (rawLv === 'ERROR' ? 'ERROR' : 'INFO');
            const msg = parts.slice(2).join(' | ');
            return { ts, lv, msg };
          }
          return { ts: '--:--:--', lv: logLevel(raw), msg: raw };
        }

        // On first poll, bulk-load the recent log history
        if (pollCount === 1 && d.watchdog.recent_log_lines && d.watchdog.recent_log_lines.length > 0) {
          d.watchdog.recent_log_lines.forEach(raw => {
            const { ts, lv, msg } = parseLine(raw);
            pushLog(ts, lv, msg);
          });
          if (d.watchdog.latest_log_line) lastLogLine = d.watchdog.latest_log_line;
          setText('log-ts', d.watchdog.last_log_ts || '\u2014');
        }

        // Subsequent polls — add only new entries
        if (pollCount > 1) {
          const line = d.watchdog.latest_log_line;
          if (line && line !== lastLogLine) {
            lastLogLine = line;
            const { ts, lv, msg } = parseLine(line);
            pushLog(ts, lv, msg);
            setText('log-ts', d.watchdog.last_log_ts || '\u2014');
          }
        }
        setText('log-headline', d.watchdog.latest_log_summary || 'No log summary available');

        setText('footer', `Poll #${pollCount} \u00b7 ${d.checked_at} \u00b7 ${d.server_display}`);

      } catch (err) {
        setRing('bad');
        applyPill('core-pill', 'Error', 'bad');
        setText('core-headline', 'Console error');
        setText('core-text', String(err));
        const n = new Date();
        const pad = v => String(v).padStart(2,'0');
        pushLog(`${pad(n.getHours())}:${pad(n.getMinutes())}:${pad(n.getSeconds())}`, 'ERROR', String(err));
      }
    }

    pushLog(new Date().toTimeString().slice(0,8), 'INIT', 'V\u0153rynth Sentinel console online. Awaiting first status poll...');
    refreshStatus();
    setInterval(refreshStatus, 2000);
  </script>
</body>
</html>'''

def check_url(url: str, timeout: int = REQUEST_TIMEOUT):
    try:
        response = requests.get(url, timeout=timeout)
        return {
            "ok": True,
            "status": response.status_code,
            "error": None,
            "url": url,
        }
    except requests.RequestException as exc:
        return {
            "ok": False,
            "status": None,
            "error": str(exc),
            "url": url,
        }


def offline_remote_status():
    return {
        "enabled": False,
        "ok": False,
        "status": None,
        "error": "Remote check disabled or not configured",
        "url": NABU_CASA_URL,
    }


def guess_asset_content_type(path: Path):
    return {
        ".png": "image/png",
        ".ttf": "font/ttf",
    }.get(path.suffix.lower(), "application/octet-stream")

def make_plug():
    require_settings(
        TUYA_DEVICE_ID=TUYA_DEVICE_ID,
        TUYA_DEVICE_IP=TUYA_DEVICE_IP,
        TUYA_LOCAL_KEY=TUYA_LOCAL_KEY,
    )
    plug = tinytuya.OutletDevice(
        dev_id=TUYA_DEVICE_ID,
        address=TUYA_DEVICE_IP,
        local_key=TUYA_LOCAL_KEY,
        version=TUYA_VERSION,
    )
    plug.set_socketPersistent(True)
    plug.set_socketNODELAY(True)
    plug.set_retry(True)
    return plug

def get_plug_status():
    try:
        plug = make_plug()
        status = plug.status()
        dps = status.get("dps", {}) if isinstance(status, dict) else {}
        if "Error" in status:
            return {
                "ok": False,
                "error": f"{status.get('Error')} ({status.get('Err')})",
                "device_ip": TUYA_DEVICE_IP,
                "relay_on": None,
                "power_w": None,
                "voltage_v": None,
                "current_ma": None,
            }

        relay_on = bool(dps.get("1")) if "1" in dps else None
        power_w = (dps.get("19") / 10.0) if dps.get("19") is not None else None
        voltage_v = (dps.get("20") / 10.0) if dps.get("20") is not None else None
        current_ma = dps.get("18")

        return {
            "ok": True,
            "error": None,
            "device_ip": TUYA_DEVICE_IP,
            "relay_on": relay_on,
            "power_w": power_w,
            "voltage_v": voltage_v,
            "current_ma": current_ma,
        }
    except Exception as exc:
        return {
            "ok": False,
            "error": str(exc),
            "device_ip": TUYA_DEVICE_IP,
            "relay_on": None,
            "power_w": None,
            "voltage_v": None,
            "current_ma": None,
        }

def read_recent_logs(n: int = 200):
    """Return the last n non-empty lines from the watchdog log file."""
    if not LOG_FILE.exists():
        return []
    try:
        with LOG_FILE.open("r", encoding="utf-8", errors="ignore") as f:
            lines = [ln.strip() for ln in f if ln.strip()]
        return lines[-n:] if lines else []
    except Exception:
        return []

_TS_PAT   = re.compile(r'^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})')
# Match "| failure count=N" (pipe-delimited) to avoid matching "soft_failures=N"
_FAIL_PAT = re.compile(r'\| failure count=(\d+)')

def parse_log_stats(lines: list):
    """Scan recent log lines (chronological order) and extract watchdog metrics."""
    consecutive_failures = 0
    reboots_last_hour = 0
    last_reboot_ts_str = None
    now = time.time()

    for line in lines:
        if "Vœrynth Core alive" in line:
            consecutive_failures = 0

        m = _FAIL_PAT.search(line)
        if m:
            consecutive_failures = int(m.group(1))

        if "Power cycle complete" in line:
            ts_m = _TS_PAT.match(line)
            if ts_m:
                try:
                    ts = datetime.strptime(ts_m.group(1), "%Y-%m-%d %H:%M:%S").timestamp()
                    if now - ts <= WATCHDOG_REBOOT_WINDOW:
                        reboots_last_hour += 1
                    last_reboot_ts_str = ts_m.group(1)
                except ValueError:
                    pass

    in_cooldown = in_boot_grace = False
    if last_reboot_ts_str:
        try:
            ts = datetime.strptime(last_reboot_ts_str, "%Y-%m-%d %H:%M:%S").timestamp()
            in_cooldown   = (now - ts) < WATCHDOG_COOLDOWN
            in_boot_grace = (now - ts) < WATCHDOG_BOOT_GRACE
        except ValueError:
            pass

    return {
        "consecutive_failures": consecutive_failures,
        "reboots_last_hour":    reboots_last_hour,
        "last_reboot_ts":       last_reboot_ts_str,
        "in_cooldown":          in_cooldown,
        "in_boot_grace":        in_boot_grace,
        # boot_grace_secs_remaining lets build_payload clear the tag early when both ports recover
        "boot_grace_deadline":  (datetime.strptime(last_reboot_ts_str, "%Y-%m-%d %H:%M:%S").timestamp()
                                 + WATCHDOG_BOOT_GRACE) if in_boot_grace and last_reboot_ts_str else 0,
    }

def summarize_log(line: str | None):
    if not line:
        return "No watchdog log line available yet."
    if "Vœrynth Core alive" in line:
        return "Watchdog reports the node as healthy."
    if "Failure threshold reached" in line:
        return "Watchdog crossed the intervention threshold."
    if "Starting power cycle via Tuya plug" in line:
        return "Watchdog has initiated a relay power cycle."
    if "Power cycle complete" in line:
        return "Relay power cycle completed."
    if "cooldown" in line.lower():
        return "Watchdog is intentionally holding back during cooldown."
    if "restart grace" in line:
        return "Vœrynth Core offline but Observer alive — restart in progress, no action taken."
    if "extended restart" in line:
        return "Vœrynth Core still offline beyond normal restart window — monitoring."
    if "machine may be frozen" in line:
        return "Both ports unreachable — machine may be frozen, counting toward threshold."
    if "failed" in line.lower():
        return "Watchdog has detected service degradation."
    return "Watchdog log updated."

class Handler(BaseHTTPRequestHandler):
    def _send_bytes(self, data, content_type, status=200, cache_control="no-store"):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", cache_control)
        self.end_headers()
        self.wfile.write(data)

    def _send_json(self, payload, status=200):
        data = json.dumps(payload).encode("utf-8")
        self._send_bytes(data, "application/json; charset=utf-8", status=status)

    def _send_html(self, html, status=200):
        data = html.encode("utf-8")
        self._send_bytes(data, "text/html; charset=utf-8", status=status)

    def _send_asset(self, asset_path):
        if not asset_path.is_file():
            self._send_json({"error": "Not found"}, status=404)
            return
        self._send_bytes(
            asset_path.read_bytes(),
            guess_asset_content_type(asset_path),
            cache_control="public, max-age=31536000, immutable",
        )

    def build_payload(self):
        global _core_offline_since
        remote_check_enabled = ENABLE_REMOTE_CHECK and bool(NABU_CASA_URL)
        # Run all network checks in parallel so a slow/dead internet connection
        # doesn't stall every check sequentially (was causing 16+ second hangs).
        with ThreadPoolExecutor(max_workers=4 if remote_check_enabled else 3) as ex:
            f_core     = ex.submit(check_url, HA_CORE_URL)
            f_observer = ex.submit(check_url, HA_OBSERVER_URL)
            f_remote   = ex.submit(check_url, NABU_CASA_URL, NABU_CASA_TIMEOUT) if remote_check_enabled else None
            f_plug     = ex.submit(get_plug_status)
            core     = f_core.result()
            observer = f_observer.result()
            remote   = f_remote.result() if f_remote else offline_remote_status()
            plug     = f_plug.result()
        recent_lines = read_recent_logs(200)
        stats = parse_log_stats(recent_lines)
        last_log_line = recent_lines[-1] if recent_lines else None
        display_lines = recent_lines[-15:]  # last 15 lines for the terminal
        now_ts = time.time()
        now_str = time.strftime("%Y-%m-%d %H:%M:%S")
        log_mtime = None
        if LOG_FILE.exists():
            try:
                log_mtime = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(LOG_FILE.stat().st_mtime))
            except Exception:
                log_mtime = None

        # Clear boot grace tag the moment both services are back online after a power cycle
        if core["ok"] and observer["ok"]:
            stats["in_boot_grace"] = False

        # Track how long Core has been offline while Observer is alive (soft failure)
        if not core["ok"] and observer["ok"]:
            if _core_offline_since == 0.0:
                _core_offline_since = now_ts
        else:
            _core_offline_since = 0.0
        soft_elapsed = (now_ts - _core_offline_since) if _core_offline_since > 0.0 else 0.0
        soft_failure_info = {
            "active":    soft_elapsed >= SOFT_FAILURE_WARN_AT,
            "elapsed":   round(soft_elapsed, 1),
            "warn_at":   SOFT_FAILURE_WARN_AT,
            "timeout":   SOFT_FAILURE_TIMEOUT,
            "remaining": max(0.0, round(SOFT_FAILURE_TIMEOUT - soft_elapsed, 1)),
        }

        return {
            "host": HA_HOST,
            "server": f"{DASHBOARD_HOST}:{PORT}",
            "server_display": f"{DASHBOARD_HOST}:{PORT}",
            "checked_at": now_str,
            "core": core,
            "observer": observer,
            "remote": remote,
            "plug": plug,
            "watchdog": {
                "active": LOG_FILE.exists(),
                "uptime_seconds": int(time.time() - SERVER_START_TS),
                "last_log_ts": log_mtime,
                "latest_log_line": last_log_line,
                "latest_log_summary": summarize_log(last_log_line),
                "recent_log_lines": display_lines,
                "consecutive_failures": stats["consecutive_failures"],
                "reboots_last_hour": stats["reboots_last_hour"],
                "last_reboot_ts": stats["last_reboot_ts"],
                "in_cooldown": stats["in_cooldown"],
                "in_boot_grace": stats["in_boot_grace"],
            },
            "soft_failure": soft_failure_info,
            "policy": {
                "check_interval": WATCHDOG_CHECK_INTERVAL,
                "failures_required": WATCHDOG_FAILURES_REQUIRED,
                "boot_grace": WATCHDOG_BOOT_GRACE,
                "cooldown": WATCHDOG_COOLDOWN,
                "max_reboots_per_hour": WATCHDOG_MAX_REBOOTS_PER_HOUR,
                "reboot_window": WATCHDOG_REBOOT_WINDOW,
                "power_off_seconds": WATCHDOG_POWER_OFF_SECONDS,
                "dry_run": WATCHDOG_DRY_RUN,
            },
        }

    def do_GET(self):
        request_path = urlsplit(self.path).path

        if request_path in ("/", "/index.html"):
            self._send_html(HTML)
            return

        if request_path == "/api/status":
            self._send_json(self.build_payload())
            return

        if request_path.startswith("/assets/"):
            assets_root = ASSETS_DIR.resolve()
            asset_path = (assets_root / unquote(request_path.removeprefix("/assets/"))).resolve()
            try:
                asset_path.relative_to(assets_root)
            except ValueError:
                self._send_json({"error": "Not found"}, status=404)
                return
            self._send_asset(asset_path)
            return

        self._send_json({"error": "Not found"}, status=404)

    def log_message(self, format, *args):
        return

if __name__ == "__main__":
    server = ThreadingHTTPServer((BIND_HOST, PORT), Handler)
    print(f"Vœrynth Sentinel running on http://{DASHBOARD_HOST}:{PORT}")
    server.serve_forever()