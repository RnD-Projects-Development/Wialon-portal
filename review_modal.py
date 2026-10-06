"""
The review workbench, shared by both pages.

Verdicts are set in exactly one piece of UI, embedded by the portal (app.PORTAL_HTML) and by the
dashboard (dashboard_page.DASHBOARD_HTML), so the two cannot drift. Each page drops STYLES into its
<style>, MARKUP into its <body>, and SCRIPT into its <script>, then sets `Review.host` to an adapter
that says where the violations come from and what to do once a verdict is saved.

The host adapter:
    esc(s)                  -> HTML-escaped string
    typeLabel(key)          -> "Overspeed"
    typeBadge(key)          -> the coloured chip for a violation type
    toast(title, message)   -> show an error
    violations(ctx)         -> the rows to list; an array or a promise of one.
                               ctx is {plate, vehicleId, focusKey} when opened from a chart,
                               or {} when opened from a header button.
    period()                -> {label, from, to} for the subtitle
    onVerdict(key, verdict) -> the host updates its own copy of the records
    onOpen() / onClose()    -> optional hooks (the dashboard refetches its numbers on close)
"""

STYLES = """
    /* Review workbench: the list where verdicts are set (review_modal.py) */
    .review-panel { width: min(96rem, 100%); height: min(52rem, 92vh); }
    #review-modal:not(.hidden) { display: flex; }
    .review-head, .rvrow {
      display: grid;
      grid-template-columns: 10.5rem 12.5rem 7.5rem 11rem 8rem minmax(6rem, 1fr) 9.5rem;
      gap: 0.75rem; align-items: center;
    }
    /* Narrower windows drop the widest columns rather than letting the grid overflow */
    @media (max-width: 1400px) {
      .review-head, .rvrow { grid-template-columns: 10.5rem 12.5rem 7.5rem minmax(7rem, 1fr) 8rem 9.5rem; }
      .review-head .loc, .rvrow .loc { display: none; }
    }
    @media (max-width: 1100px) {
      .review-head, .rvrow { grid-template-columns: 10.5rem 11rem minmax(6rem, 1fr) 9.5rem; }
      .review-head .drv, .rvrow .drv, .review-head .reg, .rvrow .reg { display: none; }
    }
    /* The head and each row carry their own side padding (the scroll box has none), so a highlighted
       row runs edge to edge and its marker bar sits in that padding instead of over the first cell */
    .review-head { height: 2.6rem; padding: 0 1.25rem; }
    /* one fixed height, because the virtual list maps scroll position to a row index */
    .rvrow { position: relative; height: var(--rvrow-h); padding: 0 1.25rem; font-size: 0.8125rem;
             border-top: 1px solid var(--edge); transition: background-color 120ms ease; }
    .rvrow:first-child { border-top: none; }
    .rvrow:hover { background: var(--hover); }
    /* the violation arrived at from a chart: a blush band fading out to the right, a rust bar on the
       left edge, and its time picked out in rust so the eye lands on it first */
    .rvrow-focus { background: linear-gradient(90deg, var(--brand-soft) 0%, color-mix(in srgb, var(--brand-soft) 40%, var(--card)) 100%);
                   border-top-color: rgba(143, 59, 59, 0.25); box-shadow: inset 0 -1px 0 rgba(143, 59, 59, 0.25); }
    .rvrow-focus:hover { background: var(--brand-soft); }
    .rvrow-focus::before { content: ''; position: absolute; left: 0; top: 0; bottom: 0; width: 0.25rem; background: var(--brand); }
    .rvrow-focus .cell:first-child { color: var(--brand-deep); font-weight: 700; }
    .rvrow .cell { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
    :root { --rvrow-h: 2.9rem; }
"""

MARKUP = """
  <!-- REVIEW WORKBENCH: the one place a verdict can be set (review_modal.py) -->
  <div id="review-backdrop" onclick="Review.close()" class="fixed inset-0 bg-slate/40 backdrop-blur-sm z-50 transition-opacity duration-200 opacity-0 pointer-events-none"></div>
  <div id="review-modal" class="fixed inset-0 z-50 hidden items-center justify-center p-5 pointer-events-none">
    <div class="review-panel bg-card rounded-2xl shadow-2xl shadow-slate/25 pointer-events-auto flex flex-col overflow-hidden">

      <div class="px-5 py-3.5 bg-rail flex flex-wrap items-center justify-between gap-3 flex-shrink-0">
        <div class="flex items-center gap-3 min-w-0">
          <span class="w-9 h-9 rounded-xl bg-brand text-ice-lt shadow-sm shadow-brand/30 flex items-center justify-center flex-shrink-0">
            <i class="fa-solid fa-gavel"></i>
          </span>
          <div class="min-w-0">
            <h2 id="review-heading" class="text-base font-bold text-ink leading-tight">Review Violations</h2>
            <p id="review-subtitle" class="text-xs text-ink2 font-mono truncate">-</p>
          </div>
        </div>
        <button onclick="Review.close()" title="Close (Esc)" class="w-9 h-9 rounded-xl bg-card hover:bg-head text-ink2 hover:text-ink border border-edge flex items-center justify-center transition flex-shrink-0">
          <i class="fa-solid fa-xmark"></i>
        </button>
      </div>

      <div class="px-5 py-3 flex flex-wrap items-center gap-2.5 border-b border-edge flex-shrink-0">
        <div class="relative flex-1 min-w-[16rem]">
          <i class="fa-solid fa-magnifying-glass absolute left-3.5 top-1/2 -translate-y-1/2 text-ink3 text-sm pointer-events-none"></i>
          <input type="text" id="review-search" autocomplete="off" oninput="Review.render()" placeholder="Search vehicle, driver, region, type or location..."
                 class="w-full bg-card border border-edge rounded-xl pl-10 pr-8 py-2 text-sm text-ink placeholder-ink3 focus:outline-none focus:border-accent focus:ring-1 focus:ring-accent/40">
          <button onclick="document.getElementById('review-search').value=''; Review.render();" title="Clear" class="absolute right-2.5 top-1/2 -translate-y-1/2 text-ink3 hover:text-ink text-xs">
            <i class="fa-solid fa-xmark"></i>
          </button>
        </div>
        <select id="review-verdict" onchange="Review.render()" class="bg-card border border-edge rounded-xl px-3 py-2 text-sm text-ink focus:outline-none focus:border-accent">
          <option value="PENDING" selected>Pending only</option>
          <option value="ALL">All verdicts</option>
          <option value="GENUINE">Genuine</option>
          <option value="FALSE">False</option>
        </select>
        <select id="review-type" onchange="Review.render()" class="bg-card border border-edge rounded-xl px-3 py-2 text-sm text-ink focus:outline-none focus:border-accent">
          <option value="ALL" selected>All violation types</option>
        </select>
        <select id="review-region" onchange="Review.render()" class="bg-card border border-edge rounded-xl px-3 py-2 text-sm text-ink focus:outline-none focus:border-accent">
          <option value="ALL" selected>All regions</option>
        </select>
      </div>

      <div class="review-head text-[0.6875rem] uppercase tracking-wider font-bold text-ink bg-head flex-shrink-0">
        <span>Time (PKT)</span><span>Violation</span><span>Vehicle</span><span class="drv">Driver</span><span class="reg">Region</span><span class="loc">Location</span><span class="text-center">Review</span>
      </div>

      <!-- only the rows in view exist in the DOM, so a 30-day period stays smooth -->
      <div id="review-scroll" class="flex-1 min-h-0 overflow-auto">
        <div id="review-spacer" style="position: relative;"><div id="review-rows" style="position: absolute; left: 0; right: 0;"></div></div>
        <div id="review-empty" class="hidden py-14 text-center text-ink2">
          <i class="fa-solid fa-circle-check text-ok text-2xl mb-2 block"></i>
          <strong class="text-ink text-sm">Nothing matches these filters</strong>
          <p class="text-xs text-ink3 mt-1">Clear the search, or switch the verdict filter to All.</p>
        </div>
      </div>

      <div class="px-5 py-2.5 bg-rail flex flex-wrap items-center justify-between gap-3 text-xs font-semibold text-ink2 flex-shrink-0">
        <span id="review-count">-</span>
        <span class="font-mono text-ink3">Verdicts save immediately · click the same verdict again to clear it</span>
      </div>
    </div>
  </div>
"""

SCRIPT = """
  // ---------------------------------------------------------------- review workbench (review_modal.py)
  // "Unassigned" is not a place: it is the fallback for a vehicle whose region is blank in Wialon,
  // so it is never offered as a filter. Those violations still appear under "All regions".
  const UNASSIGNED_REGION = 'Unassigned';

  const Review = {
    host: null,
    open: false,
    all: [],          // the violations this opening is working on
    rows: [],         // the filtered subset currently listed
    focusKey: null,   // a violation arrived at from a chart, highlighted in the list

    key(v) { return `${v.vehicle_id}|${v.type}|${v.time_unix}`; },

    el(id) { return document.getElementById(id); },

    rowHeight() {
      const root = getComputedStyle(document.documentElement);
      return parseFloat(root.getPropertyValue('--rvrow-h')) * parseFloat(root.fontSize);
    },

    pending() { return this.all.filter(v => !v.verdict).length; },

    // ctx: {plate, vehicleId, focusKey} from a chart, or {} from a header button
    async show(ctx) {
      const h = this.host;
      if (!h) return;
      ctx = ctx || {};
      let rows;
      try {
        rows = await Promise.resolve(h.violations(ctx));
      } catch (e) {
        h.toast('Could not open the review list', e.message);
        return;
      }
      if (!rows) return;

      this.all = rows.slice().sort((a, b) => b.time_unix - a.time_unix);
      this.focusKey = ctx.focusKey || null;
      this.open = true;
      this.el('review-modal').classList.remove('hidden');
      this.el('review-backdrop').classList.remove('opacity-0', 'pointer-events-none');
      this.el('review-heading').textContent = ctx.plate ? `Review · ${ctx.plate}` : 'Review Violations';
      this.fillFilters();
      this.el('review-search').value = '';
      // arriving from a chart shows every verdict, or an already-judged violation would be missing
      this.el('review-verdict').value = ctx.focusKey || ctx.plate ? 'ALL' : 'PENDING';
      this.el('review-scroll').scrollTop = 0;
      this.render();
      this.scrollToFocus();
      if (!ctx.plate) this.el('review-search').focus();
      if (h.onOpen) h.onOpen();
    },

    close() {
      if (!this.open) return;
      this.open = false;
      this.focusKey = null;
      this.el('review-modal').classList.add('hidden');
      this.el('review-backdrop').classList.add('opacity-0', 'pointer-events-none');
      if (this.host && this.host.onClose) this.host.onClose();
    },

    scrollToFocus() {
      if (!this.focusKey) return;
      const i = this.rows.findIndex(v => this.key(v) === this.focusKey);
      if (i < 0) return;
      const scroll = this.el('review-scroll'), h = this.rowHeight();
      // centre it rather than pin it to the top edge, so its neighbours are visible too
      scroll.scrollTop = Math.max(0, (i * h) - (scroll.clientHeight / 2) + h);
      this.render({ keepRows: true });
    },

    // the type and region choices come from what this opening actually contains
    fillFilters() {
      const h = this.host;
      const typeSel = this.el('review-type'), regionSel = this.el('review-region');
      const types = [...new Set(this.all.map(v => v.type))].sort((a, b) => h.typeLabel(a).localeCompare(h.typeLabel(b)));
      const regions = [...new Set(this.all.map(v => v.vehicle_region))]
        .filter(r => r && r !== UNASSIGNED_REGION).sort();
      typeSel.innerHTML = '<option value="ALL">All violation types</option>'
        + types.map(t => `<option value="${h.esc(t)}">${h.esc(h.typeLabel(t))}</option>`).join('');
      regionSel.innerHTML = '<option value="ALL">All regions</option>'
        + regions.map(r => `<option value="${h.esc(r)}">${h.esc(r)}</option>`).join('');
      typeSel.value = 'ALL';
      regionSel.value = 'ALL';
    },

    filtered() {
      const h = this.host;
      const q = (this.el('review-search').value || '').trim().toLowerCase();
      const verdict = this.el('review-verdict').value;
      const type = this.el('review-type').value;
      const region = this.el('review-region').value;
      return this.all.filter(v => {
        if (type !== 'ALL' && v.type !== type) return false;
        if (region !== 'ALL' && (v.vehicle_region || '') !== region) return false;
        if (verdict === 'PENDING' && v.verdict) return false;
        if ((verdict === 'GENUINE' || verdict === 'FALSE') && v.verdict !== verdict) return false;
        if (!q) return true;
        return [v.vehicle, v.driver, v.vehicle_region, h.typeLabel(v.type), v.location, v.time]
          .some(f => String(f || '').toLowerCase().includes(q));
      });
    },

    buttons(v) {
      const h = this.host, key = this.key(v);
      const button = (verdict, label, onCls) => {
        const on = v.verdict === verdict;
        return `<button onclick="Review.setVerdict('${key}', '${verdict}')" data-tip="${on ? 'Click again to clear' : 'Mark as ' + label}"
          class="px-2 py-1 rounded-md text-[0.6875rem] font-bold border transition ${on ? onCls : 'bg-rail text-ink3 border-edge hover:text-ink hover:border-accent'}">${label}</button>`;
      };
      return `<div class="flex items-center justify-center gap-1">
        ${button('GENUINE', 'Genuine', 'bg-ok text-ice-lt border-ok')}
        ${button('FALSE', 'False', 'bg-danger text-ice-lt border-danger')}
      </div>`;
    },

    // Virtual list: the spacer is the full height, and only the visible slice is built
    render(opts) {
      if (!this.open) return;
      const h = this.host;
      if (!(opts && opts.keepRows)) this.rows = this.filtered();
      const scroll = this.el('review-scroll'), spacer = this.el('review-spacer');
      const rowsEl = this.el('review-rows'), emptyEl = this.el('review-empty');
      const rowH = this.rowHeight();
      const period = h.period() || {};

      this.el('review-subtitle').textContent =
        period.label ? `${period.label}: ${period.from} to ${period.to} PKT` : '';
      this.el('review-count').textContent =
        `Showing ${this.rows.length.toLocaleString()} of ${this.all.length.toLocaleString()} violations · ${this.pending().toLocaleString()} still pending`;

      emptyEl.classList.toggle('hidden', this.rows.length > 0);
      spacer.style.height = (this.rows.length * rowH) + 'px';
      if (!this.rows.length) { rowsEl.innerHTML = ''; return; }

      const first = Math.max(0, Math.floor(scroll.scrollTop / rowH) - 6);
      const visible = Math.ceil(scroll.clientHeight / rowH) + 12;
      const slice = this.rows.slice(first, first + visible);
      rowsEl.style.transform = `translateY(${first * rowH}px)`;
      rowsEl.innerHTML = slice.map(v => {
        const named = v.vehicle_region && v.vehicle_region !== UNASSIGNED_REGION;
        return `
        <div class="rvrow${this.focusKey && this.key(v) === this.focusKey ? ' rvrow-focus' : ''}">
          <span class="cell font-mono text-ink">${h.esc(v.time)}</span>
          <span class="cell">${h.typeBadge(v.type)}</span>
          <span class="cell font-mono font-bold text-ink">${h.esc(v.vehicle)}</span>
          <span class="cell drv text-ink2">${h.esc(v.driver || 'Unassigned')}</span>
          <span class="cell reg ${named ? 'text-ink2' : 'text-ink3'}"
            ${named ? '' : 'data-tip="No region set in Wialon for this vehicle"'}
            >${h.esc(named ? v.vehicle_region : '—')}</span>
          <span class="cell loc text-ink2" data-tip="${h.esc(v.details || '')}">${h.esc(v.location || '-')}</span>
          <span>${this.buttons(v)}</span>
        </div>`;
      }).join('');
    },

    // Clicking the verdict a violation already has clears it back to pending
    async setVerdict(key, verdict) {
      const h = this.host;
      const mine = this.all.filter(v => this.key(v) === key);
      if (!mine.length) return;
      const next = mine[0].verdict === verdict ? null : verdict;
      const [vehicleId, type, timeUnix] = key.split('|');
      try {
        const res = await fetch('/api/review', {
          method: 'POST', headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ vehicle_id: Number(vehicleId), type, time_unix: Number(timeUnix), verdict: next })
        });
        const data = await res.json();
        if (!res.ok || data.status !== 'success') throw new Error(data.message || 'Could not save the review');
        mine.forEach(v => { v.verdict = next; });
        if (h.onVerdict) h.onVerdict(key, next);
        // keep the row in place while its verdict changes, even under the "Pending only" filter
        this.render({ keepRows: true });
      } catch (e) {
        h.toast('Review not saved', e.message);
      }
    },

    bind() {
      const scroll = this.el('review-scroll');
      if (scroll) scroll.addEventListener('scroll', () => this.render({ keepRows: true }), { passive: true });
      window.addEventListener('keydown', e => { if (e.key === 'Escape' && this.open) this.close(); });
    }
  };
"""
