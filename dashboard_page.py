"""
The dashboard page (/dashboard): headline cards and four charts for one shortcut period.

Served as a plain response rather than a template. All numbers come from /api/dashboard, which adds
them up on the server (dashboard_data.py); the page only draws them.

The look comes from theme.py, shared with the portal, so the two pages cannot drift apart.
"""

import review_modal
import theme

DASHBOARD_HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1.0" />
  <title>Violations Dashboard</title>
  <script src="https://cdn.tailwindcss.com"></script>
  <link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/font-awesome/6.4.0/css/all.min.css">
  <link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700;800&display=swap" rel="stylesheet">
  <script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.1/dist/chart.umd.min.js"></script>
"""  + theme.FAVICON_CHART + theme.THEME_HEAD + r"""  <style>
    /* The violations page's grid: header row + main; main = one row of headline cards, and the charts
       taking the rest of the window, with the same gaps, padding and 120rem container */
    .app-shell { height: 100vh; height: 100dvh; display: grid; grid-template-rows: auto minmax(0, 1fr); overflow: hidden; }
    .app-main { display: grid; grid-template-rows: auto minmax(0, 1fr); gap: 1rem; padding: 1.125rem 1.75rem 1.25rem;
                width: 100%; max-width: 120rem; margin: 0 auto; min-height: 0; box-sizing: border-box; }
    .charts { display: grid; grid-template-columns: 1fr 1fr; grid-template-rows: minmax(0, 1fr) minmax(0, 1fr); gap: 1rem; min-height: 0; }

    .topbar { background: var(--slate); box-shadow: 0 2px 10px rgba(43, 36, 36, 0.25); }
    .logo { background: linear-gradient(135deg, var(--brand-deep), var(--brand) 55%, var(--accent)); border: 1px solid rgba(255, 255, 255, 0.15); }
    .muted { color: var(--ice); }
    .presets { background: var(--slate-deep); border: 1px solid var(--slate-deep); }
    .preset { color: var(--ice); }
    .preset:hover { color: #FFFFFF; background: rgba(255, 255, 255, 0.12); }
    .preset.on { color: #FFFFFF; background: var(--brand); box-shadow: 0 2px 8px rgba(0, 0, 0, 0.3); border: 1px solid var(--brand); }
    .btn-ghost { color: var(--ink); background: var(--card); border: 1px solid var(--edge2); }
    .btn-ghost:hover { background: var(--rail); border-color: var(--accent); }
    .btn-ghost i { color: var(--accent); }
    .btn-main { color: var(--ice-lt); background: var(--brand); border: 1px solid var(--brand); box-shadow: 0 4px 12px rgba(0, 0, 0, 0.25); }
    .btn-main:hover { background: var(--brand-deep); }

    .surface { background: var(--card); border: 1px solid var(--edge); box-shadow: 0 6px 18px rgba(64, 55, 55, 0.07); }
    /* Headline cards: one row of nine numbers and a wider period card; sizes are rem-based, so the
       numbers shrink with the cards on smaller windows */
    .kpis { display: grid; grid-template-columns: repeat(9, minmax(0, 1fr)) minmax(0, 1.8fr); gap: 0.75rem; }
    .kpi { padding: 1rem; min-width: 0; display: flex; flex-direction: column; justify-content: space-between;
           border: none; }
    .kpi-head { display: flex; align-items: flex-start; justify-content: space-between; gap: 0.4rem; }
    .kpi-head .kpi-label { font-size: 0.6875rem; font-weight: 700; letter-spacing: 0.05em; text-transform: uppercase;
                           line-height: 1.25; min-height: 2.5em; }
    .kpi-head i { font-size: 0.85rem; flex-shrink: 0; margin-top: 0.1rem; }
    /* Solid tiles, like the portal's: near-white label over the fill, white number. Each tile's colour
       says what it counts rather than following the page palette: rust for the headline total, steel
       blue for the fleet, teal for places, green for Genuine, red for False,
       grey for work still pending (as in the charts), red and amber for top and average speed, charcoal
       for the period. */
    .kpi.surface { --kpi: var(--brand); background: var(--kpi); border-color: var(--kpi);
                   box-shadow: 0 4px 14px color-mix(in srgb, var(--kpi) 28%, transparent); }
    .kpi .kpi-label, .kpi-head i { color: var(--ice); }
    .kpi strong, .kpi .kpi-value { color: #FFFFFF; }
    .kpi-sub { color: var(--ice); opacity: 0.85; }
    .kpi-info.surface    { --kpi: var(--info); }
    .kpi-place.surface   { --kpi: var(--teal); }
    .kpi-genuine.surface { --kpi: var(--ok); }
    .kpi-rate.surface    { --kpi: var(--ok-deep); }
    .kpi-false.surface   { --kpi: var(--danger); }
    .kpi-pending.surface { --kpi: var(--slate); }
    .kpi-warn.surface    { --kpi: var(--warn); }
    .kpi-alert.surface   { --kpi: var(--danger); }
    .kpi-wide.surface    { --kpi: var(--slate-deep); }

    .panel { display: flex; flex-direction: column; min-height: 0; position: relative; padding: 0.9rem 1.25rem 0.75rem; }
    .panel h2 { font-size: 1rem; font-weight: 700; color: var(--ink); margin: 0 0 0.15rem; }
    .panel .hint { font-size: 0.75rem; color: var(--ink2); margin-bottom: 0.5rem; }
    .chart-box { position: relative; flex: 1; min-height: 0; }
    /* The drill-down that lists real violations rather than counts; it takes the canvas's slot */
    .list-box { flex: 1; min-height: 0; overflow: auto; margin: 0 -0.35rem; }
    .list-box.hidden, .chart-box.hidden { display: none; }
    .vrow { display: grid; grid-template-columns: 11.5rem 7.5rem minmax(0, 1fr) 5.5rem; gap: 0.6rem;
            align-items: center; padding: 0.42rem 0.6rem; border-radius: 0.5rem; cursor: pointer;
            font-size: 0.78rem; }
    .vrow + .vrow { border-top: 1px solid var(--edge); }
    .vrow:hover { background: var(--hover); }
    .vrow .t { font-family: ui-monospace, monospace; color: var(--ink); white-space: nowrap; }
    .vrow .v { font-family: ui-monospace, monospace; font-weight: 700; color: var(--brand-deep); }
    .vrow .d { color: var(--ink2); overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
    .vchip { justify-self: end; padding: 0.08rem 0.5rem; border-radius: 999px; font-size: 0.68rem;
             font-weight: 700; border: 1px solid; white-space: nowrap; }
    .vchip.genuine { background: var(--ok-soft);     color: var(--ok-deep);     border-color: var(--ok); }
    .vchip.false   { background: var(--danger-soft); color: var(--danger);      border-color: var(--danger); }
    .vchip.pending { background: var(--rail);        color: var(--ink2);        border-color: var(--edge2); }
    .list-note { padding: 0.5rem 0.6rem; font-size: 0.72rem; color: var(--ink3); }
""" + review_modal.STYLES + r"""
    .empty { position: absolute; inset: 0; display: none; align-items: center; justify-content: center; color: var(--ink3); font-size: 0.875rem; }
    .loading .chart-box canvas { opacity: 0.25; transition: opacity 0.2s; }
    .stale { color: var(--accent-deep) !important; }
    #toast { background: var(--card); border: 1px solid var(--accent); color: var(--ink); box-shadow: 0 12px 30px rgba(64, 55, 55, 0.22); }

    .custom-scrollbar::-webkit-scrollbar { width: 6px; height: 6px; }
    .custom-scrollbar::-webkit-scrollbar-track { background: var(--rail); }
    .custom-scrollbar::-webkit-scrollbar-thumb { background: var(--edge2); border-radius: 3px; }
    .custom-scrollbar::-webkit-scrollbar-thumb:hover { background: var(--ink3); }

    /* Narrow or very short windows: fall back to normal page scrolling, as the violations page does */
    @media (max-width: 1023px), (max-height: 540px) {
      .app-shell { height: auto; overflow: visible; }
      .app-main { grid-template-rows: none; }
      .charts { grid-template-columns: 1fr; grid-template-rows: none; }
      .chart-box { height: 22rem; flex: none; }
    }
    @media (max-width: 1023px) {
      .kpis { grid-template-columns: repeat(3, minmax(0, 1fr)); }
      .kpi-wide { grid-column: span 3; }
      .topbar > div { height: auto; flex-wrap: wrap; row-gap: 0.6rem; padding-top: 0.7rem; padding-bottom: 0.7rem; }
    }
    @media (max-width: 640px) {
      .app-main { padding: 0.75rem 1rem 1rem; }
      .kpis { grid-template-columns: repeat(2, minmax(0, 1fr)); }
      .kpi-wide { grid-column: span 2; }
      .topbar > div { padding-left: 1rem; padding-right: 1rem; }
      .topbar .actions { flex-wrap: wrap; flex-shrink: 1; min-width: 0; row-gap: 0.5rem; }
    }
  </style>
</head>
<body>
""" + review_modal.MARKUP + r"""
<div class="app-shell">
  <header class="topbar z-40">
    <div class="max-w-[120rem] mx-auto px-7 h-16 flex items-center justify-between gap-4">
      <div class="flex items-center gap-3.5 min-w-0">
        <div class="logo w-11 h-11 rounded-xl flex items-center justify-center shrink-0">
          <i class="fa-solid fa-chart-column text-white text-lg"></i>
        </div>
        <div class="min-w-0">
          <h1 class="font-bold text-xl leading-tight tracking-tight truncate text-white">Violations Dashboard</h1>
          <p class="text-xs muted font-mono truncate">Unilever fleet · all times PKT (UTC+5)</p>
        </div>
      </div>
      <div class="actions flex items-center gap-3 shrink-0">
        <div class="presets inline-flex p-0.5 rounded-xl" title="Whole days ending yesterday; today is never included">
          <button data-preset="1D" onclick="load('1D')" class="px-3.5 py-1.5 rounded-lg text-sm font-bold transition">1D</button>
          <button data-preset="7D" onclick="load('7D')" class="px-3.5 py-1.5 rounded-lg text-sm font-bold transition">7D</button>
          <button data-preset="15D" onclick="load('15D')" class="px-3.5 py-1.5 rounded-lg text-sm font-bold transition">15D</button>
          <button data-preset="30D" onclick="load('30D')" class="px-3.5 py-1.5 rounded-lg text-sm font-bold transition">30D</button>
        </div>
        <button onclick="downloadExcel()" title="The same dashboard as an Excel workbook, with the data behind it" class="btn-ghost px-3.5 py-2 rounded-xl text-sm font-semibold transition flex items-center gap-2 shadow-sm">
          <i class="fa-solid fa-file-excel"></i><span>Excel</span>
        </button>
        <button onclick="backToViolations()" class="btn-main px-4 py-2 rounded-xl text-sm font-bold transition flex items-center gap-2 shadow-sm">
          <i class="fa-solid fa-table-list"></i><span>Violations</span>
        </button>
      </div>
    </div>
  </header>

  <main class="app-main">

    <!-- HEADLINE CARDS: one row -->
    <div class="kpis">
      <div class="kpi surface rounded-2xl">
        <div class="kpi-head"><span class="kpi-label">Total Events</span><i class="fa-solid fa-list-check"></i></div>
        <strong id="kpi-total" class="kpi-value text-3xl font-extrabold font-mono leading-none mt-2 block truncate">-</strong>
      </div>
      <div class="kpi kpi-info surface rounded-2xl">
        <div class="kpi-head"><span class="kpi-label">Vehicles Involved</span><i class="fa-solid fa-truck"></i></div>
        <strong id="kpi-vehicles" class="kpi-value text-3xl font-extrabold font-mono leading-none mt-2 block truncate">-</strong>
      </div>
      <div class="kpi kpi-place surface rounded-2xl">
        <div class="kpi-head"><span class="kpi-label">Regions</span><i class="fa-solid fa-map-location-dot"></i></div>
        <strong id="kpi-regions" class="kpi-value text-3xl font-extrabold font-mono leading-none mt-2 block truncate">-</strong>
      </div>
      <div class="kpi kpi-genuine surface rounded-2xl">
        <div class="kpi-head"><span class="kpi-label">Genuine</span><i class="fa-solid fa-circle-check"></i></div>
        <strong id="kpi-genuine" class="kpi-value text-3xl font-extrabold font-mono leading-none mt-2 block truncate">-</strong>
      </div>
      <div class="kpi kpi-false surface rounded-2xl">
        <div class="kpi-head"><span class="kpi-label">False</span><i class="fa-solid fa-circle-xmark"></i></div>
        <strong id="kpi-false" class="kpi-value text-3xl font-extrabold font-mono leading-none mt-2 block truncate">-</strong>
      </div>
      <div class="kpi kpi-pending surface rounded-2xl">
        <div class="kpi-head"><span class="kpi-label">Pending Review</span><i class="fa-solid fa-hourglass-half"></i></div>
        <strong id="kpi-pending" class="kpi-value text-3xl font-extrabold font-mono leading-none mt-2 block truncate">-</strong>
      </div>
      <div class="kpi kpi-rate surface rounded-2xl">
        <div class="kpi-head"><span class="kpi-label">Genuine %</span><i class="fa-solid fa-percent"></i></div>
        <strong id="kpi-pct" class="kpi-value text-3xl font-extrabold font-mono leading-none mt-2 block truncate">-</strong>
      </div>
      <div class="kpi kpi-alert surface rounded-2xl">
        <div class="kpi-head"><span class="kpi-label">Max Speed (km/h)</span><i class="fa-solid fa-gauge-high"></i></div>
        <strong id="kpi-max" class="kpi-value text-3xl font-extrabold font-mono leading-none mt-2 block truncate">-</strong>
      </div>
      <div class="kpi kpi-warn surface rounded-2xl">
        <div class="kpi-head"><span class="kpi-label">Avg O/S Speed</span><i class="fa-solid fa-gauge"></i></div>
        <strong id="kpi-avg" class="kpi-value text-3xl font-extrabold font-mono leading-none mt-2 block truncate">-</strong>
      </div>
      <div class="kpi kpi-wide surface rounded-2xl">
        <div class="kpi-head"><span id="period-title" class="kpi-label">Report Period</span><i class="fa-solid fa-calendar-days"></i></div>
        <strong id="period" class="kpi-value text-base font-extrabold font-mono leading-tight mt-2 block truncate">Loading...</strong>
        <span id="source" class="kpi-sub text-xs font-mono block truncate mt-1">&nbsp;</span>
      </div>
    </div>

    <!-- CHARTS (fill the rest of the window, as the violations page's table panel) -->
    <section class="charts" id="charts">
      <div class="panel surface rounded-2xl">
        <div class="flex items-center justify-between mb-0.5 gap-3">
          <div class="flex items-center gap-2.5 min-w-0">
            <button id="region-back" onclick="showAllRegions()" title="Back to all regions (Esc)"
                    class="hidden shrink-0 inline-flex items-center gap-1.5 px-2.5 py-1 rounded-lg bg-brand-soft hover:bg-head text-ink2 hover:text-ink text-[11px] font-bold transition">
              <i class="fa-solid fa-chevron-left text-[10px]"></i> All regions
            </button>
            <h2 id="region-title" class="m-0 truncate">Total Violations by Region</h2>
          </div>
          <span class="inline-flex items-center gap-1.5 text-[11px] font-semibold px-2.5 py-0.5 rounded-full bg-accent-soft text-accent-deep border border-accent/30 shadow-sm shrink-0">
            <i class="fa-solid fa-hand-pointer text-[10px]"></i> <span id="region-pill">Click bar to view vehicles</span>
          </span>
        </div>
        <div class="hint" id="region-hint">Grouped by the vehicle's depot region in Wialon: where it works, not where the violation happened</div>
        <div class="chart-box"><canvas id="c-region"></canvas><div class="empty">No violations in this period</div></div>
      </div>
      <div class="panel surface rounded-2xl">
        <div class="flex items-center justify-between mb-0.5 gap-3">
          <div class="flex items-center gap-2.5 min-w-0">
            <button id="type-back" onclick="showAllTypes()" title="Back to all types (Esc)"
                    class="hidden shrink-0 inline-flex items-center gap-1.5 px-2.5 py-1 rounded-lg bg-brand-soft hover:bg-head text-ink2 hover:text-ink text-[11px] font-bold transition">
              <i class="fa-solid fa-chevron-left text-[10px]"></i> All types
            </button>
            <h2 id="type-title" class="m-0 truncate">Event Type — Genuine vs False</h2>
          </div>
          <span class="inline-flex items-center gap-1.5 text-[11px] font-semibold px-2.5 py-0.5 rounded-full bg-accent-soft text-accent-deep border border-accent/30 shadow-sm shrink-0">
            <i class="fa-solid fa-hand-pointer text-[10px]"></i> <span id="type-pill">Click bar to list its violations</span>
          </span>
        </div>
        <div class="hint" id="type-hint">Marked in the Inspect view of each vehicle; the rest are pending review</div>
        <div class="chart-box" id="type-chart-box"><canvas id="c-type"></canvas><div class="empty">No violations in this period</div></div>
        <div class="list-box hidden" id="type-list-box"></div>
      </div>
      <div class="panel surface rounded-2xl">
        <h2>Region-wise Violation Mix by Event Type</h2>
        <div class="hint">What each region's vehicles were flagged for</div>
        <div class="chart-box"><canvas id="c-mix"></canvas><div class="empty">No violations in this period</div></div>
      </div>
      <div class="panel surface rounded-2xl">
        <h2>Violations by Hour of Day</h2>
        <div class="hint">Hour of the violation, PKT</div>
        <div class="chart-box"><canvas id="c-hour"></canvas><div class="empty">No violations in this period</div></div>
      </div>
    </section>
  </main>
</div>


<div id="toast" class="fixed bottom-6 right-6 hidden max-w-md p-3.5 rounded-xl shadow-2xl text-sm z-50"></div>

<script>
  const PRESETS = ['1D', '7D', '15D', '30D'];
  const MONTHS = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec'];
  // Chart palette, built from the same tokens as the stylesheet (theme.py) so the charts cannot
  // drift from the page around them. Types and verdicts use the same hues as the portal's badges.
  const CSS = getComputedStyle(document.documentElement);
  const tok = name => CSS.getPropertyValue('--' + name).trim();
  const PAL = {
    brand: tok('brand'),
    brandDeep: tok('brand-deep'),
    accent: tok('accent'),
    slate: tok('slate'),
    text: tok('ink'),
    text2: tok('ink2'),
    text3: tok('ink3'),
    grid: tok('edge'),
    card: tok('card')
  };
  const TYPE_COLORS = {
    OVERSPEED: tok('danger'),
    OVERSPEED_HIGHWAY: tok('warn'),
    OVERSPEED_MOTORWAY: tok('danger-deep'),
    FATIGUE_DRIVING: tok('plum'),
    NIGHT_DRIVING: tok('info'),
    SEAT_BELT_DISCONNECTED: tok('teal'),
    DELAY_DRIVER_SEAT_BELT: tok('coffee'),
    SEAT_BELT_IGNITION_OFF: tok('slate')
  };
  const VERDICT_COLORS = { genuine: tok('ok'), false: tok('danger'), pending: tok('edge2') };
  let preset = '7D';
  let charts = {};
  // Each period is fetched once and kept here; the browser also keeps it across pages, and the server
  // answers "unchanged" to its version check until the archive or a review changes it.
  const dashCache = {};
  let dashRequest = 0;

  function esc(s) {
    return String(s ?? '').replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
  }
  function num(n) { return n === null || n === undefined ? '—' : Number(n).toLocaleString(); }
  // "2026-09-14 00:00:00" -> "14 Sep"
  function day(text, withYear) {
    const t = String(text || '');
    const d = Number(t.slice(8, 10)), m = Number(t.slice(5, 7));
    return m ? `${d} ${MONTHS[m - 1]}${withYear ? ' ' + t.slice(0, 4) : ''}` : t;
  }
  function stamp(text) { const t = String(text || ''); return `${day(t)} ${t.slice(11, 16)}`; }
  function toast(msg) {
    const el = document.getElementById('toast');
    el.textContent = msg;
    el.classList.remove('hidden');
    clearTimeout(toast.timer);
    toast.timer = setTimeout(() => el.classList.add('hidden'), 6000);
  }

  function setPresetButtons() {
    document.querySelectorAll('[data-preset]').forEach(b => {
      const on = b.dataset.preset === preset;
      b.className = `preset px-3.5 py-1.5 rounded-lg text-sm font-bold transition ${on ? 'on' : ''}`;
    });
  }

  // ------------------------------------------------------------------ cards
  // ------------------------------------------------------------------ charts
  const valueLabels = {
    id: 'valueLabels',
    afterDatasetsDraw(chart, args, opts) {
      if (!opts || !opts.mode) return;
      const { ctx } = chart;
      ctx.save();
      ctx.font = `600 ${Math.round(Chart.defaults.font.size)}px Inter, sans-serif`;
      ctx.textBaseline = 'middle';
      chart.data.datasets.forEach((ds, di) => {
        const meta = chart.getDatasetMeta(di);
        if (meta.hidden) return;
        meta.data.forEach((el, i) => {
          const v = ds.data[i];
          if (!v) return;
          if (opts.mode === 'end') {
            ctx.fillStyle = PAL.text;
            ctx.textAlign = 'left';
            ctx.fillText(num(v), el.x + 6, el.y);
          } else if (Math.abs(el.x - el.base) >= 24) {   // inside a stacked segment, when it fits
            ctx.fillStyle = ds.labelColor || PAL.text;
            ctx.textAlign = 'center';
            ctx.fillText(num(v), (el.x + el.base) / 2, el.y);
          }
        });
      });
      ctx.restore();
    }
  };

  // Plate under each bubble, count inside it. Bubbles carry their own label, so no axis has to.
  const bubbleLabels = {
    id: 'bubbleLabels',
    afterDatasetsDraw(chart, args, opts) {
      if (!opts || !opts.show) return;
      const ctx = chart.ctx;
      const size = Math.round(Chart.defaults.font.size);
      const points = chart.data.datasets[0].data;
      const taken = [];                                   // label boxes already on the canvas
      ctx.save();
      ctx.textAlign = 'center';
      chart.getDatasetMeta(0).data.forEach((el, i) => {
        const point = points[i];
        const radius = el.options.radius;
        if (radius >= size * 0.85) {                      // the count only when it fits inside
          ctx.font = '700 ' + Math.round(size * 0.85) + 'px Inter, sans-serif';
          ctx.textBaseline = 'middle';
          ctx.fillStyle = PAL.card;
          ctx.fillText(num(point.veh.total), el.x, el.y);
        }
        // Bubbles come biggest first, so the busiest vehicles win the space; a plate that would
        // touch one already drawn is left to the tooltip rather than smudged over its neighbour.
        ctx.font = '600 ' + Math.round(size * 0.82) + 'px Inter, sans-serif';
        const text = point.veh.vehicle;
        const half = ctx.measureText(text).width / 2 + 3;
        const top = el.y + radius + 3;
        const box = { x1: el.x - half, x2: el.x + half, y1: top, y2: top + size };
        const clash = taken.some(t => box.x1 < t.x2 && box.x2 > t.x1 && box.y1 < t.y2 && box.y2 > t.y1);
        if (clash) return;
        taken.push(box);
        ctx.textBaseline = 'top';
        ctx.fillStyle = PAL.text2;
        ctx.fillText(text, el.x, top);
      });
      ctx.restore();
    }
  };

  function chartDefaults() {
    const rem = parseFloat(getComputedStyle(document.documentElement).fontSize) || 14;
    Chart.defaults.color = PAL.text2;
    Chart.defaults.borderColor = PAL.grid;
    Chart.defaults.font.family = 'Inter, sans-serif';
    Chart.defaults.font.size = Math.round(rem * 0.78);
    Chart.defaults.maintainAspectRatio = false;
    Chart.defaults.plugins.legend.labels.boxWidth = 10;
    Chart.defaults.plugins.legend.labels.boxHeight = 10;
    Object.assign(Chart.defaults.plugins.tooltip, {
      backgroundColor: tok('slate'), borderColor: tok('slate-deep'), borderWidth: 1,
      titleColor: tok('ice-lt'), bodyColor: tok('ice'), padding: 10
    });
  }

  function draw(id, config, isEmpty) {
    if (charts[id]) charts[id].destroy();
    const canvas = document.getElementById(id);
    canvas.parentElement.querySelector('.empty').style.display = isEmpty ? 'flex' : 'none';
    charts[id] = new Chart(canvas, config);
  }

  // Horizontal gradient across the chart area (recomputed as the chart resizes)
  function sweep(ctx) {
    const area = ctx.chart.chartArea;
    if (!area) return PAL.brand;
    const g = ctx.chart.ctx.createLinearGradient(area.left, 0, area.right, 0);
    g.addColorStop(0, PAL.slate);
    g.addColorStop(0.5, PAL.brandDeep);
    g.addColorStop(1, PAL.accent);
    return g;
  }

  // Rust glow under the hourly line
  function glow(ctx) {
    const area = ctx.chart.chartArea;
    if (!area) return 'rgba(143, 59, 59, 0.12)';
    const g = ctx.chart.ctx.createLinearGradient(0, area.top, 0, area.bottom);
    g.addColorStop(0, 'rgba(143, 59, 59, 0.30)');
    g.addColorStop(0.55, 'rgba(253, 220, 220, 0.35)');
    g.addColorStop(1, 'rgba(250, 252, 252, 0)');
    return g;
  }

  function renderCharts(d) {
    const empty = d.cards.total === 0;

    drawRegions(d.by_region, empty);

    drawTypes(d.by_type.filter(t => t.total > 0), empty);

    const mixKeep = d.region_mix.regions.map((r, i) => r === UNASSIGNED_REGION ? -1 : i).filter(i => i >= 0);
    const series = d.region_mix.series
      .map(s => ({ key: s.key, label: s.label, values: mixKeep.map(i => s.values[i]) }))
      .filter(s => s.values.some(v => v > 0));
    draw('c-mix', {
      type: 'bar',
      data: {
        labels: mixKeep.map(i => d.region_mix.regions[i]),
        datasets: series.map(s => ({ label: s.label, data: s.values, backgroundColor: TYPE_COLORS[s.key] || PAL.text3, borderRadius: 2 }))
      },
      options: {
        plugins: { legend: { position: 'bottom' } },
        scales: { x: { stacked: true, grid: { display: false } }, y: { stacked: true, ticks: { precision: 0 } } }
      }
    }, empty);

    draw('c-hour', {
      type: 'line',
      data: {
        labels: d.by_hour.map((_, h) => `${String(h).padStart(2, '0')}:00`),
        datasets: [{ label: 'Violations', data: d.by_hour, borderColor: PAL.brand, backgroundColor: glow,
                     fill: true, tension: 0.35, pointRadius: 3, pointBackgroundColor: PAL.brand, pointBorderColor: PAL.brand,
                     pointHoverRadius: 5, borderWidth: 2 }]
      },
      options: {
        plugins: { legend: { display: false } },
        scales: { x: { grid: { display: false }, ticks: { maxRotation: 90, minRotation: 90, autoSkip: false } },
                   y: { beginAtZero: true, ticks: { precision: 0 } } }
      }
    }, empty);
  }

  // ------------------------------------------------------------------ region drill-down
  // Clicking a region swaps this panel's own chart for that region's vehicles; the back button and Esc
  // bring the regions back. Nothing overlays the dashboard.
  const TYPE_SHORT = {
    FATIGUE_DRIVING: 'Fatigue',
    OVERSPEED: 'Speeding',
    OVERSPEED_HIGHWAY: 'Highway O/S',
    OVERSPEED_MOTORWAY: 'Motorway O/S',
    NIGHT_DRIVING: 'Night Driving',
    DELAY_DRIVER_SEAT_BELT: 'Delay Driver SB',
    SEAT_BELT_DISCONNECTED: 'SB Disconnected',
    SEAT_BELT_IGNITION_OFF: 'SB On - Ign Off'
  };
  const REGION_HINT = "Grouped by the vehicle's depot region in Wialon: where it works, not where the violation happened";
  // UNASSIGNED_REGION comes from review_modal.py: it is not a region, just the fallback for a vehicle
  // whose region is blank in Wialon. The charts show real regions only, and the hint says what that
  // leaves out, so nothing goes missing quietly; the payload still carries it, so totals stay complete.
  const named = rows => (rows || []).filter(r => r.region !== UNASSIGNED_REGION);

  let regionAll = [];            // every row the server sent, Unassigned included
  let regionRows = [];           // the named ones the bars are drawn from
  let regionEmpty = false;
  let activeRegion = null;

  function regionHeader(title, hint, pill, showBack) {
    document.getElementById('region-title').textContent = title;
    document.getElementById('region-hint').textContent = hint;
    document.getElementById('region-pill').textContent = pill;
    document.getElementById('region-back').classList.toggle('hidden', !showBack);
  }

  function drawRegions(rows, empty) {
    regionAll = rows || [];
    regionRows = named(regionAll);
    regionEmpty = !!empty;
    activeRegion = null;
    const hidden = regionAll.filter(r => r.region === UNASSIGNED_REGION);
    const hiddenTotal = hidden.reduce((n, r) => n + r.total, 0);
    const hiddenVehicles = hidden.reduce((n, r) => n + (r.vehicle_count || 0), 0);
    regionHeader('Total Violations by Region',
      REGION_HINT + (hiddenTotal
        ? ` · ${num(hiddenTotal)} from ${num(hiddenVehicles)} vehicle(s) with no region set in Wialon are not shown`
        : ''),
      'Click bar to view vehicles', false);
    draw('c-region', {
      type: 'bar',
      data: {
        labels: regionRows.map(r => r.region),
        datasets: [{ label: 'Violations', data: regionRows.map(r => r.total), backgroundColor: sweep,
                     hoverBackgroundColor: PAL.brand, borderRadius: 4, barPercentage: 0.75 }]
      },
      options: {
        indexAxis: 'y',
        onClick: (evt, els) => { if (els && els.length) drillRegion(regionRows[els[0].index]); },
        onHover: (evt, els) => { evt.native.target.style.cursor = (els && els.length) ? 'pointer' : 'default'; },
        plugins: {
          legend: { display: false }, valueLabels: { mode: 'end' },
          tooltip: { callbacks: { afterLabel: ctx => {
            const r = regionRows[ctx.dataIndex];
            return `Genuine ${r.genuine} · False ${r.false} · Pending ${r.pending} (Click for this region's vehicles)`;
          } } }
        },
        scales: {
          x: { grace: '10%', ticks: { precision: 0 } },
          y: { grid: { display: false }, ticks: { autoSkip: false, font: { size: 11 } } }
        }
      },
      plugins: [valueLabels]
    }, regionEmpty);
  }

  function drillRegion(region) {
    if (!region) return;
    const all = region.vehicles || [];          // already sorted by violation count, worst first
    if (!all.length) { toast(`No vehicle detail stored for ${region.region}`); return; }
    activeRegion = region;

    // Laid out biggest first across a grid, so nothing overlaps and every vehicle in the region fits:
    // a bar per vehicle cannot label 40 plates in this panel's height.
    const cols = Math.max(1, Math.ceil(Math.sqrt(all.length * 2.2)));
    const rows = Math.max(1, Math.ceil(all.length / cols));
    const rMax = all.length <= 20 ? 20 : (all.length <= 40 ? 15 : 11);
    const rMin = 4;
    const worst = all[0].total || 1;
    const colorOf = v => {
      let best = null, most = -1;
      Object.keys(v.by_type || {}).forEach(k => { if (v.by_type[k] > most) { most = v.by_type[k]; best = k; } });
      return TYPE_COLORS[best] || PAL.brand;
    };
    const points = all.map((v, k) => ({
      x: (k % cols) + 0.5,
      y: rows - Math.floor(k / cols) - 0.5,
      r: rMin + (rMax - rMin) * Math.sqrt(v.total / worst),
      veh: v
    }));
    const colors = all.map(colorOf);

    regionHeader(
      `${region.region} — vehicles`,
      `${num(region.total)} violations across ${num(all.length)} vehicle(s) · bubble size = violations, colour = its most common type · Genuine ${num(region.genuine)} · False ${num(region.false)} · Pending ${num(region.pending)}`,
      'Click bubble to inspect in the portal', true);

    draw('c-region', {
      type: 'bubble',
      data: {
        datasets: [{
          data: points,
          backgroundColor: colors.map(c => c + 'AA'),
          borderColor: colors,
          borderWidth: 1.5,
          hoverBackgroundColor: PAL.brand,
          hoverBorderColor: PAL.text
        }]
      },
      options: {
        layout: { padding: { top: 10, bottom: 16, left: 14, right: 14 } },
        onClick: (evt, els) => {
          if (!els || !els.length) return;
          const v = points[els[0].index].veh;
          if (v) openReview(v.vehicle, v.vehicle_id);
        },
        onHover: (evt, els) => { evt.native.target.style.cursor = (els && els.length) ? 'pointer' : 'default'; },
        plugins: {
          legend: { display: false },
          bubbleLabels: { show: true },
          tooltip: { callbacks: {
            title: items => points[items[0].dataIndex].veh.vehicle,
            label: ctx => `Violations: ${num(points[ctx.dataIndex].veh.total)}`,
            afterLabel: ctx => {
              const v = points[ctx.dataIndex].veh;
              const lines = [`Driver: ${v.driver || 'Unassigned'}`,
                             `Genuine ${v.genuine} · False ${v.false} · Pending ${v.pending}`];
              Object.keys(v.by_type || {}).forEach(k => {
                if (v.by_type[k]) lines.push(`   ${TYPE_SHORT[k] || k}: ${v.by_type[k]}`);
              });
              lines.push('(Click to review this vehicle in the portal)');
              return lines;
            }
          } }
        },
        scales: {
          x: { display: false, min: 0, max: cols },
          y: { display: false, min: 0, max: rows }
        }
      },
      plugins: [bubbleLabels]
    }, false);
  }

  function showAllRegions() {
    if (!activeRegion) return;
    drawRegions(regionAll, regionEmpty);
  }

  // ------------------------------------------------------------------ event type, and its violations
  const TYPE_HINT = 'Marked in the Inspect view of each vehicle; the rest are pending review';
  let typeRows = [];             // kept so the back button can redraw without refetching
  let typeEmpty = false;
  let activeType = null;
  const typeViolations = {};     // `${preset}|${TYPE}` -> the rows the endpoint returned

  function typeHeader(title, hint, pill, showBack) {
    document.getElementById('type-title').textContent = title;
    document.getElementById('type-hint').textContent = hint;
    document.getElementById('type-pill').textContent = pill;
    document.getElementById('type-back').classList.toggle('hidden', !showBack);
  }

  function typePanel(showList) {
    document.getElementById('type-chart-box').classList.toggle('hidden', showList);
    document.getElementById('type-list-box').classList.toggle('hidden', !showList);
  }

  function drawTypes(rows, empty) {
    typeRows = rows || [];
    typeEmpty = !!empty;
    activeType = null;
    typeHeader('Event Type — Genuine vs False', TYPE_HINT, 'Click bar to list its violations', false);
    typePanel(false);
    draw('c-type', {
      type: 'bar',
      data: {
        labels: typeRows.map(t => t.label),
        datasets: [
          { label: 'Genuine', data: typeRows.map(t => t.genuine), backgroundColor: VERDICT_COLORS.genuine, borderRadius: 3 },
          { label: 'False', data: typeRows.map(t => t.false), backgroundColor: VERDICT_COLORS.false, borderRadius: 3 },
          { label: 'Pending', data: typeRows.map(t => t.pending), backgroundColor: VERDICT_COLORS.pending, borderRadius: 3, labelColor: PAL.text }
        ]
      },
      options: {
        indexAxis: 'y',
        // the whole row drills in, whichever segment was clicked: the thin ones are hard to hit
        onClick: (evt, els) => { if (els && els.length) drillType(typeRows[els[0].index]); },
        onHover: (evt, els) => { evt.native.target.style.cursor = (els && els.length) ? 'pointer' : 'default'; },
        plugins: {
          legend: { position: 'bottom' }, valueLabels: { mode: 'inside' },
          tooltip: { callbacks: { afterLabel: ctx => {
            const t = typeRows[ctx.dataIndex];
            return `${num(t.total)} in total (Click for this type's violations)`;
          } } }
        },
        scales: {
          x: { stacked: true, ticks: { precision: 0 } },
          y: { stacked: true, grid: { display: false }, ticks: { autoSkip: false, font: { size: 11 } } }
        }
      },
      plugins: [valueLabels]
    }, typeEmpty);
  }

  async function drillType(row) {
    if (!row) return;
    activeType = row;
    typePanel(true);
    typeHeader(`${row.label} — violations`, `Loading ${num(row.total)} violation(s)...`, 'Click a row to inspect in the portal', true);
    const box = document.getElementById('type-list-box');
    box.innerHTML = '<div class="list-note">Loading...</div>';

    const key = `${preset}|${row.key}`;
    try {
      if (!typeViolations[key]) {
        const res = await fetch(`/api/dashboard/type-violations?preset=${preset}&type=${encodeURIComponent(row.key)}`);
        const data = await res.json();
        if (!res.ok) throw new Error(data.error || 'Could not load these violations');
        typeViolations[key] = data;
      }
    } catch (e) {
      box.innerHTML = `<div class="list-note" style="color: var(--danger)">${esc(e.message)}</div>`;
      typeHeader(`${row.label} — violations`, TYPE_HINT, 'Click a row to inspect in the portal', true);
      return;
    }
    if (activeType !== row) return;      // a different bar was opened while this one was loading
    renderTypeList(typeViolations[key], row);
  }

  function renderTypeList(data, row) {
    const rows = data.violations || [];
    const box = document.getElementById('type-list-box');
    typeHeader(
      `${row.label} — violations`,
      `${num(data.total)} in ${data.period.label} · Genuine ${num(row.genuine)} · False ${num(row.false)} · Pending ${num(row.pending)}`,
      'Click a row to inspect in the portal', true);

    if (!rows.length) {
      box.innerHTML = '<div class="list-note">No violations of this type in this period</div>';
      return;
    }
    const chip = v => v === 'GENUINE' ? '<span class="vchip genuine">Genuine</span>'
                   : v === 'FALSE'   ? '<span class="vchip false">False</span>'
                   : '<span class="vchip pending">Pending</span>';
    box.innerHTML =
      (data.truncated ? `<div class="list-note">Showing the newest ${num(data.shown)} of ${num(data.total)} — open the portal for the full list</div>` : '')
      + rows.map(v => `
        <div class="vrow" data-plate="${esc(v.vehicle || '')}" data-key="${esc(v.vehicle_id + '|' + row.key + '|' + v.time_unix)}"
             data-vid="${v.vehicle_id}" onclick="openReview(this.dataset.plate, this.dataset.vid, this.dataset.key)" title="${esc(v.location || '')}">
          <span class="t">${esc(v.time || '')}</span>
          <span class="v">${esc(v.vehicle || '')}</span>
          <span class="d">${esc(v.driver || 'Unassigned')}${v.location ? ' · ' + esc(v.location) : ''}</span>
          ${chip(v.verdict)}
        </div>`).join('');
    box.scrollTop = 0;
  }

""" + review_modal.SCRIPT + r"""
  // Clicking anything on this page opens the review workbench over the dashboard. The violations
  // come from the server, since this page only holds counts; the numbers behind it are refetched
  // once the workbench closes, so they agree with whatever was just decided.
  let reviewDirty = false;

  function typeLabelOf(key) {
    const d = dashCache[preset];
    const row = d && d.by_type && d.by_type.find(t => t.key === key);
    return (row && row.label) || TYPE_SHORT[key] || key;
  }

  Review.host = {
    esc,
    typeLabel: key => typeLabelOf(key),
    typeBadge: key => {
      const colour = TYPE_COLORS[key] || PAL.text3;
      return `<span class="inline-flex items-center gap-1.5 px-2 py-0.5 rounded-full text-[0.6875rem] font-bold"
        style="background:${colour}22; color:${colour}; border:1px solid ${colour}66">${esc(typeLabelOf(key))}</span>`;
    },
    toast: (title, message) => toast(`${title}: ${message}`),
    violations: async ctx => {
      const res = await fetch(`/api/dashboard/vehicle-violations?preset=${preset}&vehicleId=${encodeURIComponent(ctx.vehicleId)}`);
      const data = await res.json();
      if (!res.ok) throw new Error(data.error || 'Could not load this vehicle');
      return data.violations || [];
    },
    period: () => (dashCache[preset] && dashCache[preset].period) || {},
    onVerdict: () => { reviewDirty = true; },
    onClose: () => { if (reviewDirty) { reviewDirty = false; load(preset); } },
  };

  function openReview(plate, vehicleId, focusKey) {
    if (!vehicleId) return;
    Review.show({ plate, vehicleId, focusKey });
  }

  function showAllTypes() {
    if (!activeType) return;
    drawTypes(typeRows, typeEmpty);
  }

  // ------------------------------------------------------------------ loading
  async function load(next) {
    preset = PRESETS.includes(next) ? next : preset;
    setPresetButtons();
    history.replaceState(null, '', `/dashboard?preset=${preset}`);
    const wanted = preset, request = ++dashRequest;
    const kept = dashCache[wanted];
    if (kept) render(kept);                       // opened before: shown at once
    else {
      document.getElementById('charts').classList.add('loading');
      document.getElementById('period').textContent = 'Loading...';
    }
    try {
      const res = await fetch(`/api/dashboard?preset=${wanted}`);
      const d = await res.json();
      if (request !== dashRequest) return;        // a later click took over
      if (!res.ok) throw new Error(d.error || 'Could not load the dashboard');
      if (kept && kept.version === d.version) return;
      dashCache[wanted] = d;
      if (wanted === preset) render(d);
    } catch (e) {
      if (request !== dashRequest) return;
      if (!kept) {
        toast(e.message);
        document.getElementById('period').textContent = 'Failed to load';
      }
    } finally {
      if (request === dashRequest) document.getElementById('charts').classList.remove('loading');
    }
  }

  function render(d) {
      const p = d.period, c = d.cards;
      const set = (id, text) => { document.getElementById(id).textContent = text; };
      const sameDay = p.from.slice(0, 10) === p.to.slice(0, 10);
      set('period-title', `Report Period (${p.label})`);
      set('period', sameDay ? day(p.from, true) : `${day(p.from)} – ${day(p.to, true)}`);
      document.getElementById('period').title = `${p.label}: ${p.from} to ${p.to} PKT`;
      const tip = (id, text) => { document.getElementById(id).closest('.kpi').title = text; };
      const top = c.max_speed_at;
      set('kpi-total', num(c.total));
      tip('kpi-total', `${num(c.total)} violations in the period`);
      set('kpi-vehicles', num(c.vehicles));
      tip('kpi-vehicles', d.fleet_size ? `${num(c.vehicles)} of the ${num(d.fleet_size)} vehicles in the fleet` : '');
      set('kpi-regions', num(c.regions));
      tip('kpi-regions', "Vehicle depot regions, from each vehicle's Wialon record");
      set('kpi-genuine', num(c.genuine));
      tip('kpi-genuine', 'Marked genuine in the Inspect view');
      set('kpi-false', num(c.false));
      tip('kpi-false', 'Marked false in the Inspect view');
      set('kpi-pending', num(c.pending));
      tip('kpi-pending', c.total ? `${Math.round(100 * c.pending / c.total)}% not reviewed yet` : '');
      set('kpi-pct', c.genuine_pct === null ? '—' : `${c.genuine_pct}%`);
      tip('kpi-pct', c.reviewed ? `Of ${num(c.reviewed)} reviewed` : 'Nothing reviewed yet');
      set('kpi-max', num(c.max_speed));
      tip('kpi-max', top ? `${top.vehicle}, ${top.type}, ${top.time} PKT` : '');
      set('kpi-avg', c.avg_overspeed === null ? '—' : c.avg_overspeed);
      tip('kpi-avg', c.overspeed_events ? `Average over ${num(c.overspeed_events)} overspeed alerts` : 'No overspeed alerts');
      renderCharts(d);
      const src = d.archive && d.archive.source === 'archive' ? 'nightly archive' : 'calculated just now';
      const stale = (d.archive && d.archive.stale_days || []).length;
      const sourceEl = document.getElementById('source');
      sourceEl.textContent = stale
        ? `${stale} day(s) being recalculated`
        : `from the ${src}`;
      sourceEl.className = `kpi-sub text-xs font-mono block truncate mt-1 ${stale ? 'stale' : ''}`;
      if (d.reviews_problem) toast(`Genuine / False cannot be saved yet: ${d.reviews_problem}`);
  }

  function backToViolations() { window.location.href = `/?preset=${preset}`; }
  function downloadExcel() {
    toast('Building the Excel dashboard for this period...');
    window.location.href = `/api/export-dashboard-xlsx?preset=${preset}`;
  }

  window.addEventListener('DOMContentLoaded', () => {
    chartDefaults();
    Review.bind();
    const asked = (new URLSearchParams(window.location.search).get('preset') || '').toUpperCase();
    load(PRESETS.includes(asked) ? asked : '7D');
  });

  window.addEventListener('keydown', e => {
    if (e.key === 'Escape') {
      showAllRegions();
      showAllTypes();
    }
  });
</script>
</body>
</html>
"""
