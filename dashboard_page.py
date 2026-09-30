"""
The dashboard page (/dashboard): headline cards and four charts for one shortcut period.

Served as a plain response rather than a template. All numbers come from /api/dashboard, which adds
them up on the server (dashboard_data.py); the page only draws them.
"""

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
  <style>
    /* Palette: Space Cadet (#25344F), Slate Gray (#617891), Luminous Tan (#EBD2B2), Ice Blue (#9EC5E8), Caput Mortuum Lift (#E25763), Warm Amber (#E49767) */
    :root {
      --space-cadet: #25344F;
      --slate-gray: #7C95B1;
      --tan: #D5B893;
      --tan-bright: #EBD2B2;
      --coffee: #6F4D38;
      --caput-mortuum: #632024;
      
      --ice-blue: #9EC5E8;
      --ice-blue-light: #C5DDF5;
      --alert-vibrant: #E25763;
      --alert-soft: #FFCCD0;
      --amber-vibrant: #E49767;
      
      --bg-deep: #0B1019;
      --bg-gradient-top: #152030;
      --panel: rgba(21, 32, 48, 0.85);
      --panel-edge: rgba(158, 197, 232, 0.24);
      --tan-edge: rgba(235, 210, 178, 0.45);
      
      --text: #FFFFFF;
      --text-2: #C5DDF5;
      --text-3: #7C95B1;
    }
    html { font-size: clamp(12px, min(1vw, 1.8vh), 18px); }
    body {
      font-family: 'Inter', sans-serif; margin: 0; color: var(--text); background-color: var(--bg-deep);
      background:
        radial-gradient(ellipse 65% 50% at 50% -10%, rgba(235, 210, 178, 0.18), transparent 70%),
        radial-gradient(ellipse 70% 60% at 85% 10%, rgba(226, 87, 99, 0.12), transparent 70%),
        linear-gradient(180deg, var(--bg-gradient-top) 0%, #111B29 45%, var(--bg-deep) 100%);
      background-attachment: fixed;
    }

    /* The violations page's grid: header row + main; main = one row of headline cards, and the charts
       taking the rest of the window, with the same gaps, padding and 120rem container */
    .app-shell { height: 100vh; height: 100dvh; display: grid; grid-template-rows: auto minmax(0, 1fr); overflow: hidden; }
    .app-main { display: grid; grid-template-rows: auto minmax(0, 1fr); gap: 1rem; padding: 1.125rem 1.75rem 1.25rem;
                width: 100%; max-width: 120rem; margin: 0 auto; min-height: 0; box-sizing: border-box; }
    .charts { display: grid; grid-template-columns: 1fr 1fr; grid-template-rows: minmax(0, 1fr) minmax(0, 1fr); gap: 1rem; min-height: 0; }

    .topbar { background: rgba(21, 32, 48, 0.92); border-bottom: 1px solid var(--panel-edge); backdrop-filter: blur(12px); }
    .logo { background: linear-gradient(135deg, var(--space-cadet), var(--coffee) 50%, var(--tan-bright)); box-shadow: 0 6px 20px rgba(0, 0, 0, 0.5); border: 1px solid var(--tan-edge); }
    .muted { color: var(--text-2); }
    .presets { background: rgba(11, 16, 25, 0.85); border: 1px solid var(--panel-edge); }
    .preset { color: var(--text-2); }
    .preset:hover { color: #fff; }
    .preset.on { color: #fff; background: linear-gradient(135deg, var(--coffee), var(--caput-mortuum)); box-shadow: 0 2px 10px rgba(226, 87, 99, 0.35); border: 1px solid rgba(235, 210, 178, 0.3); }
    .btn-ghost { color: var(--text); background: rgba(37, 52, 79, 0.85); border: 1px solid var(--panel-edge); }
    .btn-ghost:hover { background: rgba(124, 149, 177, 0.35); border-color: var(--ice-blue); }
    .btn-ghost i { color: var(--tan-bright); }
    .btn-main { color: #fff; background: linear-gradient(135deg, var(--coffee), var(--caput-mortuum)); border: 1px solid rgba(235, 210, 178, 0.3); box-shadow: 0 4px 16px rgba(0, 0, 0, 0.4); }
    .btn-main:hover { filter: brightness(1.15); box-shadow: 0 4px 20px rgba(226, 87, 99, 0.35); }

    .surface { background: var(--panel); border: 1px solid var(--panel-edge); backdrop-filter: blur(10px); box-shadow: inset 0 1px 0 rgba(235, 210, 178, 0.12), 0 10px 28px rgba(0, 0, 0, 0.5); }
    /* Headline cards: one row of nine numbers and a wider period card; sizes are rem-based, so the
       numbers shrink with the cards on smaller windows */
    .kpis { display: grid; grid-template-columns: repeat(9, minmax(0, 1fr)) minmax(0, 1.8fr); gap: 0.75rem; }
    .kpi { padding: 1rem; min-width: 0; display: flex; flex-direction: column; justify-content: space-between;
           border: none; }
    .kpi-head { display: flex; align-items: flex-start; justify-content: space-between; gap: 0.4rem; }
    .kpi-head .kpi-label { font-size: 0.6875rem; font-weight: 700; letter-spacing: 0.05em; text-transform: uppercase;
                           line-height: 1.25; min-height: 2.5em; }
    .kpi-head i { font-size: 0.85rem; flex-shrink: 0; margin-top: 0.1rem; }
    .kpi-label { color: var(--text-2); }
    .kpi-sub { color: var(--text-3); }
    .panel { display: flex; flex-direction: column; min-height: 0; position: relative; padding: 0.9rem 1.25rem 0.75rem; }
    .panel h2 { font-size: 1rem; font-weight: 700; color: var(--text); margin: 0 0 0.15rem; }
    .panel .hint { font-size: 0.75rem; color: var(--text-2); margin-bottom: 0.5rem; }
    .chart-box { position: relative; flex: 1; min-height: 0; }
    .empty { position: absolute; inset: 0; display: none; align-items: center; justify-content: center; color: var(--text-3); font-size: 0.875rem; }
    .loading .chart-box canvas { opacity: 0.25; transition: opacity 0.2s; }
    .stale { color: var(--tan-bright) !important; }
    #toast { background: rgba(21, 32, 48, 0.98); border: 1px solid var(--tan-edge); color: var(--text); box-shadow: 0 12px 35px rgba(0, 0, 0, 0.7); }

    /* Custom Drawer Scrollbar & Animations */
    .custom-scrollbar::-webkit-scrollbar { width: 6px; height: 6px; }
    .custom-scrollbar::-webkit-scrollbar-track { background: rgba(11, 16, 25, 0.6); }
    .custom-scrollbar::-webkit-scrollbar-thumb { background: rgba(124, 149, 177, 0.4); border-radius: 3px; }
    .custom-scrollbar::-webkit-scrollbar-thumb:hover { background: var(--tan-bright); }
    

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
<div class="app-shell">
  <header class="topbar z-40">
    <div class="max-w-[120rem] mx-auto px-7 h-16 flex items-center justify-between gap-4">
      <div class="flex items-center gap-3.5 min-w-0">
        <div class="logo w-11 h-11 rounded-xl flex items-center justify-center shrink-0">
          <i class="fa-solid fa-chart-column text-white text-lg"></i>
        </div>
        <div class="min-w-0">
          <h1 class="font-bold text-xl leading-tight tracking-tight truncate text-white">Violations Dashboard</h1>
          <p class="text-xs muted font-mono truncate text-[#9EC5E8]">Unilever fleet · all times PKT (UTC+5)</p>
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
        <div class="kpi-head"><span class="kpi-label">Total Events</span><i class="fa-solid fa-list-check" style="color: var(--tan-bright)"></i></div>
        <strong id="kpi-total" class="text-3xl font-extrabold font-mono leading-none mt-2 block truncate" style="color: var(--tan-bright)">-</strong>
      </div>
      <div class="kpi surface rounded-2xl">
        <div class="kpi-head"><span class="kpi-label">Vehicles Involved</span><i class="fa-solid fa-truck" style="color: var(--ice-blue)"></i></div>
        <strong id="kpi-vehicles" class="text-3xl font-extrabold font-mono leading-none mt-2 block truncate text-white">-</strong>
      </div>
      <div class="kpi surface rounded-2xl">
        <div class="kpi-head"><span class="kpi-label">Regions</span><i class="fa-solid fa-map-location-dot" style="color: var(--ice-blue)"></i></div>
        <strong id="kpi-regions" class="text-3xl font-extrabold font-mono leading-none mt-2 block truncate text-white">-</strong>
      </div>
      <div class="kpi surface rounded-2xl">
        <div class="kpi-head"><span class="kpi-label">Genuine</span><i class="fa-solid fa-circle-check" style="color: var(--tan-bright)"></i></div>
        <strong id="kpi-genuine" class="text-3xl font-extrabold font-mono leading-none mt-2 block truncate" style="color: var(--tan-bright)">-</strong>
      </div>
      <div class="kpi surface rounded-2xl">
        <div class="kpi-head"><span class="kpi-label">False</span><i class="fa-solid fa-circle-xmark" style="color: var(--alert-vibrant)"></i></div>
        <strong id="kpi-false" class="text-3xl font-extrabold font-mono leading-none mt-2 block truncate" style="color: var(--alert-vibrant)">-</strong>
      </div>
      <div class="kpi surface rounded-2xl">
        <div class="kpi-head"><span class="kpi-label">Pending Review</span><i class="fa-solid fa-hourglass-half" style="color: var(--ice-blue)"></i></div>
        <strong id="kpi-pending" class="text-3xl font-extrabold font-mono leading-none mt-2 block truncate" style="color: var(--ice-blue)">-</strong>
      </div>
      <div class="kpi surface rounded-2xl">
        <div class="kpi-head"><span class="kpi-label">Genuine %</span><i class="fa-solid fa-percent" style="color: var(--tan-bright)"></i></div>
        <strong id="kpi-pct" class="text-3xl font-extrabold font-mono leading-none mt-2 block truncate" style="color: var(--tan-bright)">-</strong>
      </div>
      <div class="kpi surface rounded-2xl">
        <div class="kpi-head"><span class="kpi-label">Max Speed (km/h)</span><i class="fa-solid fa-gauge-high" style="color: var(--alert-vibrant)"></i></div>
        <strong id="kpi-max" class="text-3xl font-extrabold font-mono leading-none mt-2 block truncate" style="color: var(--alert-vibrant)">-</strong>
      </div>
      <div class="kpi surface rounded-2xl">
        <div class="kpi-head"><span class="kpi-label">Avg O/S Speed</span><i class="fa-solid fa-gauge" style="color: var(--amber-vibrant)"></i></div>
        <strong id="kpi-avg" class="text-3xl font-extrabold font-mono leading-none mt-2 block truncate text-white">-</strong>
      </div>
      <div class="kpi kpi-wide surface rounded-2xl">
        <div class="kpi-head"><span id="period-title" class="kpi-label">Report Period</span><i class="fa-solid fa-calendar-days" style="color: var(--tan-bright)"></i></div>
        <strong id="period" class="text-base font-extrabold font-mono leading-tight mt-2 block truncate" style="color: var(--tan-bright)">Loading...</strong>
        <span id="source" class="kpi-sub text-xs font-mono block truncate mt-1 text-[#9EC5E8]">&nbsp;</span>
      </div>
    </div>

    <!-- CHARTS (fill the rest of the window, as the violations page's table panel) -->
    <section class="charts" id="charts">
      <div class="panel surface rounded-2xl">
        <div class="flex items-center justify-between mb-0.5 gap-3">
          <div class="flex items-center gap-2.5 min-w-0">
            <button id="region-back" onclick="showAllRegions()" title="Back to all regions (Esc)"
                    class="hidden shrink-0 inline-flex items-center gap-1.5 px-2.5 py-1 rounded-lg bg-[#1E2D42] hover:bg-[#2B3E59] text-[#C5DDF5] hover:text-white text-[11px] font-bold transition">
              <i class="fa-solid fa-chevron-left text-[10px]"></i> All regions
            </button>
            <h2 id="region-title" class="m-0 truncate">Total Violations by Region</h2>
          </div>
          <span class="inline-flex items-center gap-1.5 text-[11px] font-semibold px-2.5 py-0.5 rounded-full bg-[rgba(235,210,178,0.12)] text-[#EBD2B2] border border-[rgba(235,210,178,0.3)] shadow-sm shrink-0">
            <i class="fa-solid fa-hand-pointer text-[10px]"></i> <span id="region-pill">Click bar to view vehicles</span>
          </span>
        </div>
        <div class="hint" id="region-hint">Grouped by the vehicle's depot region in Wialon: where it works, not where the violation happened</div>
        <div class="chart-box"><canvas id="c-region"></canvas><div class="empty">No violations in this period</div></div>
      </div>
      <div class="panel surface rounded-2xl">
        <h2>Event Type — Genuine vs False</h2>
        <div class="hint">Marked in the Inspect view of each vehicle; the rest are pending review</div>
        <div class="chart-box"><canvas id="c-type"></canvas><div class="empty">No violations in this period</div></div>
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
  // Palette: Space Cadet (#25344F), Slate Gray (#7C95B1), Luminous Tan (#EBD2B2), Ice Blue (#9EC5E8), Alert (#E25763), Amber (#E49767)
  const PAL = {
    spaceCadet: '#25344F',
    spaceCadetLight: '#3D547B',
    slateGray: '#7C95B1',
    slateGrayLight: '#9EC5E8',
    tan: '#D5B893',
    tanBright: '#EBD2B2',
    coffee: '#6F4D38',
    coffeeLight: '#94674B',
    caputMortuum: '#632024',
    alertVibrant: '#E25763',
    amberVibrant: '#E49767',
    iceBlue: '#9EC5E8',
    text: '#FFFFFF',
    text2: '#C5DDF5',
    text3: '#7C95B1'
  };
  const TYPE_COLORS = {
    OVERSPEED: PAL.alertVibrant,
    DELAY_DRIVER_SEAT_BELT: PAL.iceBlue,
    SEAT_BELT_DISCONNECTED: PAL.tan,
    SEAT_BELT_IGNITION_OFF: PAL.slateGray,
    OVERSPEED_HIGHWAY: PAL.amberVibrant,
    OVERSPEED_MOTORWAY: '#F47C88',
    NIGHT_DRIVING: PAL.spaceCadetLight,
    FATIGUE_DRIVING: PAL.tanBright
  };
  const VERDICT_COLORS = { genuine: PAL.tanBright, false: PAL.alertVibrant, pending: 'rgba(158, 197, 232, 0.35)' };
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
            ctx.fillStyle = ds.labelColor || '#141d2c';
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
          ctx.fillStyle = '#0B1019';
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
    Chart.defaults.borderColor = 'rgba(158, 197, 232, 0.14)';
    Chart.defaults.font.family = 'Inter, sans-serif';
    Chart.defaults.font.size = Math.round(rem * 0.78);
    Chart.defaults.maintainAspectRatio = false;
    Chart.defaults.plugins.legend.labels.boxWidth = 10;
    Chart.defaults.plugins.legend.labels.boxHeight = 10;
    Object.assign(Chart.defaults.plugins.tooltip, {
      backgroundColor: 'rgba(21, 32, 48, 0.98)', borderColor: 'rgba(235, 210, 178, 0.5)', borderWidth: 1,
      titleColor: PAL.tanBright, bodyColor: PAL.text, padding: 10
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
    if (!area) return PAL.tanBright;
    const g = ctx.chart.ctx.createLinearGradient(area.left, 0, area.right, 0);
    g.addColorStop(0, PAL.spaceCadet);
    g.addColorStop(0.5, PAL.coffee);
    g.addColorStop(1, PAL.tanBright);
    return g;
  }

  // Warm Tan glow under the hourly line
  function glow(ctx) {
    const area = ctx.chart.chartArea;
    if (!area) return 'rgba(235, 210, 178, 0.15)';
    const g = ctx.chart.ctx.createLinearGradient(0, area.top, 0, area.bottom);
    g.addColorStop(0, 'rgba(235, 210, 178, 0.35)');
    g.addColorStop(0.55, 'rgba(111, 77, 56, 0.18)');
    g.addColorStop(1, 'rgba(11, 16, 25, 0)');
    return g;
  }

  function renderCharts(d) {
    const empty = d.cards.total === 0;

    drawRegions(d.by_region, empty);

    const types = d.by_type.filter(t => t.total > 0);
    draw('c-type', {
      type: 'bar',
      data: {
        labels: types.map(t => t.label),
        datasets: [
          { label: 'Genuine', data: types.map(t => t.genuine), backgroundColor: VERDICT_COLORS.genuine, borderRadius: 3 },
          { label: 'False', data: types.map(t => t.false), backgroundColor: VERDICT_COLORS.false, borderRadius: 3 },
          { label: 'Pending', data: types.map(t => t.pending), backgroundColor: VERDICT_COLORS.pending, borderRadius: 3, labelColor: PAL.text }
        ]
      },
      options: {
        indexAxis: 'y',
        plugins: { legend: { position: 'bottom' }, valueLabels: { mode: 'inside' } },
        scales: {
          x: { stacked: true, ticks: { precision: 0 } },
          y: { stacked: true, grid: { display: false }, ticks: { autoSkip: false, font: { size: 11 } } }
        }
      },
      plugins: [valueLabels]
    }, empty);

    const series = d.region_mix.series.filter(s => s.values.some(v => v > 0));
    draw('c-mix', {
      type: 'bar',
      data: {
        labels: d.region_mix.regions,
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
        datasets: [{ label: 'Violations', data: d.by_hour, borderColor: PAL.tanBright, backgroundColor: glow,
                     fill: true, tension: 0.35, pointRadius: 3, pointBackgroundColor: PAL.tanBright, pointBorderColor: PAL.tanBright,
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
  let regionRows = [];           // kept so the back button can redraw without refetching
  let regionEmpty = false;
  let activeRegion = null;

  function regionHeader(title, hint, pill, showBack) {
    document.getElementById('region-title').textContent = title;
    document.getElementById('region-hint').textContent = hint;
    document.getElementById('region-pill').textContent = pill;
    document.getElementById('region-back').classList.toggle('hidden', !showBack);
  }

  function drawRegions(rows, empty) {
    regionRows = rows || [];
    regionEmpty = !!empty;
    activeRegion = null;
    regionHeader('Total Violations by Region', REGION_HINT, 'Click bar to view vehicles', false);
    draw('c-region', {
      type: 'bar',
      data: {
        labels: regionRows.map(r => r.region),
        datasets: [{ label: 'Violations', data: regionRows.map(r => r.total), backgroundColor: sweep,
                     hoverBackgroundColor: PAL.tanBright, borderRadius: 4, barPercentage: 0.75 }]
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
      return TYPE_COLORS[best] || PAL.tanBright;
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
          hoverBackgroundColor: PAL.tanBright,
          hoverBorderColor: PAL.text
        }]
      },
      options: {
        layout: { padding: { top: 10, bottom: 16, left: 14, right: 14 } },
        onClick: (evt, els) => {
          if (!els || !els.length) return;
          const v = points[els[0].index].veh;
          if (v) window.location.href = `/?preset=${preset}&search=${encodeURIComponent(v.vehicle)}`;
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
              lines.push('(Click to open this vehicle in the portal)');
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
    drawRegions(regionRows, regionEmpty);
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
    const asked = (new URLSearchParams(window.location.search).get('preset') || '').toUpperCase();
    load(PRESETS.includes(asked) ? asked : '7D');
  });

  window.addEventListener('keydown', e => {
    if (e.key === 'Escape') {
      showAllRegions();
    }
  });
</script>
</body>
</html>
"""
