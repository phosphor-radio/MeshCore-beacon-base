/* Beacon map. Everything shown comes from the base's own API (no other host is contacted). Names are untrusted text from the
   air, so the page is built with DOM calls and textContent only, never with markup made from data. */
(function () {
  'use strict';

  const POLL_MS = 5000;
  const TRACK_HOURS = 6;
  const BATT_EMPTY_MV = 3300, BATT_FULL_MV = 4200; // a lithium cell, roughly

  const S = {
    data: null,        // the last /api/state
    fetchedAt: 0,      // performance.now() when it arrived
    offline: false,
    lastOk: 0,
    selected: null,    // prefix of the selected beacon
    detail: null,      // {beacon, transmissions, hearing, track} for the selected one
    fitted: false,
    touched: false,    // the person has moved or zoomed the map, so it is no longer re-fitted by itself
    labels: true,
    token: null,       // kept in sessionStorage when the browser lets us
  };

  // ---------------------------------------------------------------- helpers

  function h(tag, props, ...kids) {
    const el = document.createElement(tag);
    for (const [k, v] of Object.entries(props || {})) {
      if (v === null || v === undefined || v === false) continue;
      if (k === 'class') el.className = v;
      else if (k === 'text') el.textContent = v;
      else if (k.startsWith('on')) el.addEventListener(k.slice(2), v);
      else el.setAttribute(k, v === true ? '' : v);
    }
    for (const kid of kids.flat()) {
      if (kid === null || kid === undefined || kid === false) continue;
      el.append(kid.nodeType ? kid : document.createTextNode(String(kid)));
    }
    return el;
  }

  const $ = (id) => document.getElementById(id);
  const css = (name) => getComputedStyle(document.documentElement).getPropertyValue(name).trim();

  function serverNow() {
    return S.data ? S.data.now + (performance.now() - S.fetchedAt) / 1000 : Date.now() / 1000;
  }

  function ageText(ts) {
    if (ts === null || ts === undefined) return 'never';
    const s = Math.max(0, serverNow() - ts);
    if (s < 90) return Math.round(s) + ' s ago';
    if (s < 90 * 60) return Math.round(s / 60) + ' min ago';
    if (s < 36 * 3600) return Math.round(s / 3600) + ' h ago';
    return Math.round(s / 86400) + ' d ago';
  }

  function age(ts, extra) {
    return h('span', { class: 'age', 'data-ts': ts === null || ts === undefined ? null : ts, text: ageText(ts) }, extra);
  }

  function clock(ts) {
    return new Date(ts * 1000).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', second: '2-digit' });
  }

  const volts = (mv) => (mv ? (mv / 1000).toFixed(2) + ' V' : '-');
  const metres = (m) => (m >= 1000 ? (m / 1000).toFixed(1) + ' km' : Math.round(m) + ' m');
  const shortName = (b) => b.name || b.prefix.slice(0, 12);

  function positionText(p) {
    if (!p) return 'no position';
    return (p.method === 'range' ? 'range of one repeater' : p.n_repeaters + ' repeaters') + ', within about ' + metres(p.radius_m);
  }

  async function getJSON(path) {
    const r = await fetch(path, { cache: 'no-store' });
    if (!r.ok) throw new Error(path + ': ' + r.status);
    return r.json();
  }

  function toast(text) {
    const t = $('toast');
    t.textContent = text;
    t.hidden = false;
    clearTimeout(toast.timer);
    toast.timer = setTimeout(() => { t.hidden = true; }, 5000);
  }

  // Render a part of the page only when what it shows changed, so focus and hover survive the polling.
  const rendered = {};
  function renderOnce(key, model, build, into) {
    const sig = JSON.stringify(model);
    if (rendered[key] === sig) return false;
    rendered[key] = sig;
    into.replaceChildren(...[].concat(build()));
    return true;
  }

  // ---------------------------------------------------------------- map

  const map = L.map('map', { zoomControl: true, minZoom: 0, maxZoom: 22 }).setView([0, 0], 2);
  L.control.scale({ imperial: false }).addTo(map);
  for (const [name, z] of [['accuracy', 350], ['links', 380]]) map.createPane(name).style.zIndex = z;
  const repeaterLayer = L.layerGroup().addTo(map);
  const beaconLayer = L.layerGroup().addTo(map);
  const trackLayer = L.layerGroup().addTo(map);
  const repeaterObjs = new Map();
  const beaconObjs = new Map();

  const GridLayer = L.GridLayer.extend({
    createTile(coords) {
      const size = this.getTileSize();
      const tile = document.createElement('canvas');
      tile.width = size.x; tile.height = size.y;
      tile.className = 'grid-tile';
      const ctx = tile.getContext('2d');
      ctx.strokeStyle = css('--silent');
      ctx.globalAlpha = 0.45;
      ctx.strokeRect(0.5, 0.5, size.x - 1, size.y - 1);
      ctx.globalAlpha = 0.2;
      ctx.beginPath();
      for (let i = 1; i < 4; i++) {
        ctx.moveTo(Math.round((size.x * i) / 4) + 0.5, 0); ctx.lineTo(Math.round((size.x * i) / 4) + 0.5, size.y);
        ctx.moveTo(0, Math.round((size.y * i) / 4) + 0.5); ctx.lineTo(size.x, Math.round((size.y * i) / 4) + 0.5);
      }
      ctx.stroke();
      ctx.globalAlpha = 1;
      const nw = this._map.unproject(coords.scaleBy(size), coords.z);
      const digits = Math.max(1, Math.min(6, Math.ceil(coords.z / 3)));
      ctx.fillStyle = css('--muted');
      ctx.font = '10px system-ui, sans-serif';
      ctx.fillText(nw.lat.toFixed(digits) + ', ' + nw.lng.toFixed(digits), 4, 12);
      return tile;
    },
  });

  function setupBackground(info) {
    let note = '';
    if (info.mode === 'grid') {
      new GridLayer({ minZoom: 0, maxZoom: 22 }).addTo(map);
      note = info.problem ? 'Map tiles are not available (' + info.problem + '): showing a plain grid.' : 'No map tiles are installed: showing a plain grid with coordinates.';
    } else {
      const max = info.max_zoom;
      // Online tile servers such as OpenStreetMap's refuse requests that carry no Referer, so the tile images are told to send one
      L.tileLayer(info.url, {
        minZoom: info.min_zoom, maxZoom: max + 3, maxNativeZoom: max, attribution: info.attribution || '',
        referrerPolicy: info.referrer_policy || 'no-referrer',
      }).addTo(map);
      map.setMinZoom(info.min_zoom);
      map.setMaxZoom(max + 3);
      if (info.mode === 'online') note = 'Development mode: map tiles are loaded from the internet.';
      if (info.bounds && !S.fitted) map.fitBounds([[info.bounds[1], info.bounds[0]], [info.bounds[3], info.bounds[2]]]);
    }
    $('mapnote').textContent = note;
    $('mapnote').hidden = !note;
  }

  function stateColor(state) {
    return { ok: css('--ok'), rejected: css('--bad'), silent: css('--silent'), disabled: css('--silent') }[state] || css('--silent');
  }

  // Leaflet reads a string given to a tooltip as markup, and names come from the air: give it an element holding text instead.
  function tipEl(text) {
    return h('span', { text });
  }

  function setTip(layer, obj, text, cls) {
    const key = text + '|' + cls + '|' + S.labels;
    if (obj.tipKey === key) return;
    obj.tipKey = key;
    layer.unbindTooltip();
    layer.bindTooltip(tipEl(text), { permanent: S.labels, sticky: !S.labels, direction: 'right', offset: [S.labels ? 10 : 0, 0], className: 'bl ' + cls });
  }

  function repeaterIcon(cls) {
    return L.divIcon({ className: '', html: '<div class="' + cls + '"></div>', iconSize: [18, 18] });
  }

  function syncRepeaters(list) {
    const seen = new Set();
    for (const r of list) {
      if (!r.located) continue;
      seen.add(r.prefix);
      const cls = 'repeater-icon' + (r.enabled ? '' : ' off');
      let obj = repeaterObjs.get(r.prefix);
      if (!obj) {
        // made with its own icon: Leaflet's default one would be fetched from images/ that this page does not ship
        obj = { cls, marker: L.marker([r.lat, r.lon], { icon: repeaterIcon(cls), keyboard: false, riseOnHover: true }).addTo(repeaterLayer) };
        repeaterObjs.set(r.prefix, obj);
      }
      obj.marker.setLatLng([r.lat, r.lon]);
      if (obj.cls !== cls) {
        obj.cls = cls;
        obj.marker.setIcon(repeaterIcon(cls));
      }
      setTip(obj.marker, obj, r.name || r.prefix.slice(0, 12), 'repeater');
    }
    for (const [prefix, obj] of repeaterObjs) {
      if (!seen.has(prefix)) { repeaterLayer.removeLayer(obj.marker); repeaterObjs.delete(prefix); }
    }
  }

  function syncBeacons(list) {
    const seen = new Set();
    for (const b of list) {
      const p = b.position;
      if (!p) continue;
      seen.add(b.prefix);
      const color = stateColor(b.state);
      const faded = b.state === 'silent' || b.state === 'disabled';
      let obj = beaconObjs.get(b.prefix);
      if (!obj) {
        const circle = L.circle([p.lat, p.lon], { radius: p.radius_m, pane: 'accuracy', interactive: false }).addTo(beaconLayer);
        const marker = L.circleMarker([p.lat, p.lon], { radius: 8, weight: 2 }).addTo(beaconLayer);
        marker.on('click', () => select(b.prefix));
        obj = { circle, marker };
        beaconObjs.set(b.prefix, obj);
      }
      obj.circle.setLatLng([p.lat, p.lon]);
      obj.circle.setRadius(p.radius_m);
      // A range circle is the whole position of a beacon one repeater heard, so it is always drawn; the circle of a centroid is only
      // an uncertainty and is drawn for the selected beacon, or thirty of them would hide the map.
      const shown = p.method === 'range' || b.prefix === S.selected;
      obj.circle.setStyle({
        color, weight: 1, dashArray: p.method === 'range' ? '5 5' : null, fillColor: color,
        fillOpacity: !shown ? 0 : faded ? 0.03 : 0.1, opacity: !shown ? 0 : faded ? 0.4 : 0.8,
      });
      obj.marker.setLatLng([p.lat, p.lon]);
      obj.marker.setStyle({ color: css('--surface'), fillColor: color, fillOpacity: faded ? 0.55 : 0.95, opacity: 1 });
      setTip(obj.marker, obj, shortName(b), b.state);
    }
    for (const [prefix, obj] of beaconObjs) {
      if (!seen.has(prefix)) { beaconLayer.removeLayer(obj.circle); beaconLayer.removeLayer(obj.marker); beaconObjs.delete(prefix); }
    }
  }

  function allPoints() {
    const pts = [];
    for (const o of repeaterObjs.values()) pts.push(o.marker.getLatLng());
    for (const o of beaconObjs.values()) pts.push(o.marker.getLatLng());
    return pts;
  }

  function fit() {
    const pts = allPoints();
    if (!pts.length) return;
    map.fitBounds(L.latLngBounds(pts).pad(0.25), { maxZoom: 17 });
    S.fitted = true;
  }

  let highlight = null;
  function drawSelection() {
    trackLayer.clearLayers();
    highlight = null;
    const d = S.detail;
    if (!d || d.beacon.prefix !== S.selected) return;
    const accent = css('--accent');
    const pts = d.track.map((p) => [p.lat, p.lon]);
    if (pts.length > 1) L.polyline(pts, { color: accent, weight: 2, opacity: 0.8, interactive: false }).addTo(trackLayer);
    for (const p of d.track) {
      L.circleMarker([p.lat, p.lon], { radius: 3, color: accent, weight: 1, fillColor: accent, fillOpacity: 0.9 })
        .bindTooltip(tipEl('counter ' + p.counter + ', ' + ageText(p.at)), { direction: 'top' }).addTo(trackLayer);
    }
    for (const o of d.hearing) {
      if (o.lat === null || !d.beacon.position) continue;
      L.polyline([[d.beacon.position.lat, d.beacon.position.lon], [o.lat, o.lon]], { pane: 'links', color: css('--repeater'), weight: 1.5, opacity: 0.6, dashArray: '2 5' })
        .bindTooltip(tipEl(o.repeater_label + ': ' + o.rssi + ' dBm'), { sticky: true }).addTo(trackLayer);
    }
    if (d.beacon.position) {
      highlight = L.circleMarker([d.beacon.position.lat, d.beacon.position.lon], { radius: 14, color: accent, weight: 2, fill: false, interactive: false }).addTo(trackLayer);
    }
  }

  // ---------------------------------------------------------------- top bar and banners

  function chip(cls, text, ts) {
    return h('span', { class: 'chip' }, h('span', { class: 'dot ' + cls }), text, ts !== undefined ? [' ', age(ts)] : null);
  }

  function renderStatus() {
    const d = S.data;
    const items = [];
    if (S.offline && !d) items.push(chip('bad', 'Cannot reach the base'));
    if (d) {
      const ing = d.health.ingest;
      const up = ing.state === 'up';
      items.push(chip(up ? 'ok' : 'bad', up ? 'Ingest running' : 'Ingest ' + (ing.state === 'never' ? 'has never run' : ing.state === 'stopped' ? 'stopped' : 'not running')));
      if (up) items.push(ing.connected ? chip('ok', 'Companion connected, last report', ing.last_report_at) : chip('bad', 'Companion not connected'));
      items.push(d.health.clock.trusted ? chip('ok', 'Clock set') : chip('warn', 'Clock not set'));
    }
    renderOnce('status', [S.data && S.data.health, S.offline], () => items, $('status'));
    renderUpdated();
  }

  function renderUpdated() {
    const el = $('updated');
    if (!S.lastOk) { el.textContent = ''; return; }
    const s = Math.round((performance.now() - S.fetchedAt) / 1000);
    el.textContent = 'Updated ' + (s < 2 ? 'just now' : s + ' s ago');
  }

  function bannerFor(b) {
    const r = b.rejects;
    const detail = r
      ? r.count + ' report' + (r.count === 1 ? '' : 's') + ' rejected since ' + clock(r.since) + ' (counters ' + (r.min_counter === r.max_counter ? r.min_counter : r.min_counter + '-' + r.max_counter) +
        ' against a high-water mark of ' + b.hwm + ') via ' + r.via.join(', ')
      : 'reports are being rejected';
    const resetCmd = 'beaconctl beacon reset ' + b.prefix;
    return h('div', { class: 'banner rejected', role: 'alert' },
      h('span', { class: 'what' }, h('strong', {}, shortName(b) + ' is locked out'), h('small', {}, detail + '. The next report after a reset becomes its baseline: reset while it is transmitting.')),
      S.data.operator
        ? h('button', { type: 'button', class: 'danger', onclick: () => resetBeacon(b) }, 'Reset')
        : h('code', { class: 'cmd', title: 'No web token is configured, so reset from the command line' }, resetCmd));
  }

  function renderBanners() {
    const d = S.data;
    const model = d ? [d.health.problems, d.beacons.filter((b) => b.state === 'rejected').map((b) => [b.prefix, b.name, b.rejects, b.hwm]), d.operator] : null;
    renderOnce('banners', [model, S.offline, S.lastOk && Math.floor(performance.now() / 15000)], () => {
      const out = [];
      if (S.offline) {
        out.push(h('div', { class: 'banner error', role: 'alert' }, h('span', { class: 'what' }, h('strong', {}, 'Cannot reach the base web server. '), 'What is shown may be out of date; the last update was ' + ageOffline() + '.')));
      }
      if (d) {
        for (const p of d.health.problems) out.push(h('div', { class: 'banner ' + (p.level === 'error' ? 'error' : 'warning'), role: p.level === 'error' ? 'alert' : 'status' }, h('span', { class: 'what' }, p.text)));
        for (const b of d.beacons) if (b.state === 'rejected') out.push(bannerFor(b));
      }
      return out;
    }, $('banners'));
    const count = d ? d.beacons.filter((b) => b.state === 'rejected').length + d.health.problems.filter((p) => p.level === 'error').length : 0;
    document.title = (count ? '(' + count + ') ' : '') + 'Beacon map';
  }

  function ageOffline() {
    const s = Math.round((performance.now() - S.fetchedAt) / 1000);
    return S.lastOk ? (s < 90 ? s + ' s ago' : Math.round(s / 60) + ' min ago') : 'never';
  }

  // ---------------------------------------------------------------- lists

  function beaconRow(b) {
    const sub = [volts(b.batt_mv), b.position ? positionText(b.position) : b.state === 'silent' && b.last_heard_at === null ? 'not heard yet' : 'no position'];
    if (b.hwm !== null) sub.push('counter ' + b.hwm);
    return h('li', {}, h('button', { type: 'button', class: 'row', 'data-prefix': b.prefix, 'aria-pressed': S.selected === b.prefix ? 'true' : 'false', onclick: () => select(b.prefix, true) },
      h('span', { class: 'dot ' + b.state, title: b.state }),
      h('span', { class: 'name' }, shortName(b), b.state !== 'ok' ? h('span', { class: 'tag ' + b.state }, b.state) : null),
      age(b.last_heard_at),
      h('span', { class: 'sub' }, sub.join(' · '))));
  }

  function repeaterRow(r) {
    const where = r.located ? r.lat.toFixed(5) + ', ' + r.lon.toFixed(5) + (r.location_source ? ' (' + r.location_source + ')' : '') : 'no location: not used for positions';
    return h('li', {}, h('button', { type: 'button', class: 'row', onclick: () => r.located && repeaterObjs.has(r.prefix) && map.flyTo([r.lat, r.lon], Math.max(map.getZoom(), 15)) },
      h('span', { class: 'dot ' + (r.enabled ? (r.located ? 'ok' : 'warn') : 'disabled') }),
      h('span', { class: 'name' }, r.name || r.prefix.slice(0, 12), r.enabled ? null : h('span', { class: 'tag disabled' }, 'off')),
      age(r.last_report_at),
      h('span', { class: 'sub' }, where)));
  }

  function renderLists() {
    const d = S.data;
    if (!d) return;
    renderOnce('beacons', [d.beacons, S.selected], () => (d.beacons.length ? d.beacons.map(beaconRow) : h('li', { class: 'note' }, 'No beacons on the allowlist yet (beaconctl beacon add).')), $('beacons'));
    $('beacon-count').textContent = '(' + d.beacons.length + ')';
    renderOnce('repeaters', d.repeaters, () => (d.repeaters.length ? d.repeaters.map(repeaterRow) : h('li', { class: 'note' }, 'No repeaters yet (beaconctl repeater add).')), $('repeaters'));
    $('repeater-count').textContent = '(' + d.repeaters.length + ')';
    const u = d.unknown;
    const bits = [];
    if (u.beacons) bits.push(u.beacons + ' beacon' + (u.beacons === 1 ? '' : 's'));
    if (u.repeaters) bits.push(u.repeaters + ' repeater' + (u.repeaters === 1 ? '' : 's'));
    $('unknown').textContent = bits.length ? bits.join(' and ') + ' heard in the last day but not trusted yet; see beaconctl status.' : '';
    $('unknown').hidden = !bits.length;
  }

  // ---------------------------------------------------------------- detail

  function renderDetail() {
    const el = $('detail');
    const d = S.detail;
    if (!S.selected || !d || d.beacon.prefix !== S.selected) { el.hidden = true; rendered.detail = null; return; }
    el.hidden = false;
    renderOnce('detail', d, () => {
      const b = d.beacon;
      const pct = b.batt_mv ? Math.max(0, Math.min(1, (b.batt_mv - BATT_EMPTY_MV) / (BATT_FULL_MV - BATT_EMPTY_MV))) : 0;
      const out = [
        h('h3', {}, h('span', { class: 'dot ' + b.state }), shortName(b), h('button', { type: 'button', class: 'close', title: 'Close (Esc)', onclick: () => select(null) }, '×')),
        h('dl', {},
          h('dt', {}, 'Key prefix'), h('dd', {}, h('code', {}, b.prefix)),
          h('dt', {}, 'State'), h('dd', {}, b.state + (b.baseline_pending ? ' (waiting for a baseline)' : '')),
          h('dt', {}, 'Last heard'), h('dd', {}, age(b.last_heard_at)),
          h('dt', {}, 'Counter'), h('dd', {}, b.hwm === null ? 'none yet' : String(b.hwm)),
          h('dt', {}, 'Battery'), h('dd', {}, volts(b.batt_mv), h('div', { class: 'bar', title: 'roughly ' + Math.round(pct * 100) + '% of a lithium cell' }, h('i', { style: 'width:' + Math.round(pct * 100) + '%' }))),
          h('dt', {}, 'Position'), h('dd', {}, positionText(b.position)),
          b.notes ? [h('dt', {}, 'Notes'), h('dd', {}, b.notes)] : null),
      ];
      if (d.hearing.length) {
        out.push(h('h4', {}, 'Heard by (latest transmission)'),
          h('table', {}, h('thead', {}, h('tr', {}, h('th', {}, 'Repeater'), h('th', {}, 'RSSI'), h('th', {}, 'SNR'), h('th', {}, 'Status'))),
            h('tbody', {}, d.hearing.map((o) => h('tr', {}, h('td', {}, o.repeater_label), h('td', {}, o.rssi + ' dBm'), h('td', {}, o.snr.toFixed(1) + ' dB'), h('td', {}, o.status))))));
      }
      if (d.transmissions.length) {
        out.push(h('h4', {}, 'Recent transmissions'),
          h('table', {}, h('thead', {}, h('tr', {}, h('th', {}, 'Counter'), h('th', {}, 'Heard'), h('th', {}, 'By'), h('th', {}, 'Position'))),
            h('tbody', {}, d.transmissions.slice(0, 12).map((t) => h('tr', {}, h('td', {}, t.counter), h('td', {}, age(t.last_seen)), h('td', {}, t.n_repeaters), h('td', {}, t.position ? (t.position.method === 'range' ? 'range' : 'centroid') + ' ±' + metres(t.position.radius_m) : 'none'))))));
      }
      return out;
    }, el);
  }

  async function loadDetail() {
    const prefix = S.selected;
    if (!prefix) return;
    try {
      const [beacon, track] = await Promise.all([getJSON('api/beacons/' + prefix), getJSON('api/beacons/' + prefix + '/track?hours=' + TRACK_HOURS)]);
      let hearing = [];
      if (beacon.transmissions.length) {
        const tx = await getJSON('api/transmissions/' + beacon.transmissions[0].id);
        hearing = tx.observations.filter((o) => o.status === 'accepted');
      }
      if (S.selected !== prefix) return; // another one was picked while this loaded
      S.detail = { beacon: beacon.beacon, transmissions: beacon.transmissions, hearing, track: track.points };
      renderDetail();
      drawSelection();
    } catch (e) {
      console.warn('beacon detail:', e);
    }
  }

  function select(prefix, pan) {
    S.selected = prefix === S.selected && !pan ? null : prefix;
    if (pan && prefix) {
      const obj = beaconObjs.get(prefix);
      if (obj && !map.getBounds().contains(obj.marker.getLatLng())) map.panTo(obj.marker.getLatLng());
    }
    S.detail = null;
    trackLayer.clearLayers();
    rendered.beacons = null;
    renderLists();
    renderDetail();
    if (S.data) syncBeacons(S.data.beacons);
    if (S.selected) loadDetail();
  }

  // ---------------------------------------------------------------- operator action

  function dialogResult(dialog, setup) {
    return new Promise((resolve) => {
      const onClose = () => { dialog.removeEventListener('close', onClose); resolve(dialog.returnValue); };
      dialog.addEventListener('close', onClose);
      setup();
      dialog.returnValue = '';
      dialog.showModal();
    });
  }

  function wireDialog(formId, dialogId, cancelId) {
    const dialog = $(dialogId);
    $(formId).addEventListener('submit', (e) => { e.preventDefault(); dialog.close('ok'); });
    $(cancelId).addEventListener('click', () => dialog.close('cancel'));
    return dialog;
  }

  const tokenDialog = wireDialog('token-form', 'token-dialog', 'token-cancel');
  const confirmDialog = wireDialog('confirm-form', 'confirm-dialog', 'confirm-cancel');

  function storedToken() {
    if (S.token) return S.token;
    try { return sessionStorage.getItem('beacon-token'); } catch (e) { return null; }
  }
  function storeToken(t) {
    S.token = t;
    try { if (t) sessionStorage.setItem('beacon-token', t); else sessionStorage.removeItem('beacon-token'); } catch (e) { /* kept in memory only */ }
  }

  async function askToken(problem) {
    const input = $('token-input');
    const result = await dialogResult(tokenDialog, () => {
      input.value = '';
      $('token-error').textContent = problem || '';
      $('token-error').hidden = !problem;
    });
    const value = input.value.trim();
    input.value = '';
    return result === 'ok' && value ? value : null;
  }

  async function resetBeacon(b) {
    const r = b.rejects;
    const ok = await dialogResult(confirmDialog, () => {
      $('confirm-title').textContent = 'Reset ' + shortName(b) + '?';
      $('confirm-text').textContent = 'Its high-water mark (' + b.hwm + ') is cleared' + (r ? ' and the ' + r.count + ' rejected report' + (r.count === 1 ? '' : 's') + ' stop counting against it' : '') +
        '. The next report from it becomes its new baseline, so do this while the beacon is transmitting and nothing hostile is replaying its reports.';
    });
    if (ok !== 'ok') return;
    let problem = null;
    for (let attempt = 0; attempt < 3; attempt++) {
      let token = storedToken();
      if (!token) {
        token = await askToken(problem);
        if (!token) return;
      }
      let res;
      try {
        res = await fetch('api/beacons/' + b.prefix + '/reset', { method: 'POST', headers: { Authorization: 'Bearer ' + token } });
      } catch (e) {
        toast('Could not reach the base: ' + e.message);
        return;
      }
      if (res.status === 401) { storeToken(null); problem = 'That token was not accepted.'; continue; }
      const body = await res.json().catch(() => ({}));
      if (!res.ok) { toast(body.detail || 'Reset failed (' + res.status + ')'); return; }
      storeToken(token);
      toast('Reset ' + shortName(b) + '. Its next report becomes the new baseline.');
      refresh();
      return;
    }
  }

  // ---------------------------------------------------------------- polling

  function render() {
    renderStatus();
    renderBanners();
    renderLists();
    syncRepeaters(S.data.repeaters);
    syncBeacons(S.data.beacons);
    const any = allPoints().length > 0;
    $('empty').hidden = any;
    if (any && !S.fitted) fit();
    if (S.selected) {
      const known = S.data.beacons.some((b) => b.prefix === S.selected);
      if (!known) select(null); else renderDetail();
    }
  }

  let polling = null;
  async function refresh() {
    clearTimeout(polling);
    try {
      const data = await getJSON('api/state');
      S.data = data;
      S.fetchedAt = performance.now();
      S.lastOk = Date.now();
      S.offline = false;
      render();
      if (S.selected) await loadDetail();
    } catch (e) {
      S.offline = true;
      renderStatus();
      renderBanners();
    }
    polling = setTimeout(refresh, POLL_MS);
  }

  function tick() {
    for (const el of document.querySelectorAll('[data-ts]')) el.textContent = ageText(Number(el.dataset.ts));
    renderUpdated();
  }

  // ---------------------------------------------------------------- start

  $('fit').addEventListener('click', () => { S.touched = false; fit(); });
  for (const type of ['pointerdown', 'wheel', 'keydown']) map.getContainer().addEventListener(type, () => { S.touched = true; }, { passive: true });
  // The pane can be laid out after the map was made (a hidden tab, a rotated phone): follow its size, and until the map is touched keep it fitted.
  new ResizeObserver(() => {
    map.invalidateSize({ pan: false });
    if (!S.touched && S.fitted) fit();
  }).observe($('map'));
  S.labels = window.innerWidth > 760; // labels crowd a phone's map; the person's choice, once made, is remembered
  try { const saved = localStorage.getItem('beacon-labels'); if (saved !== null) S.labels = saved === '1'; } catch (e) { /* default */ }
  $('labels').checked = S.labels;
  $('labels').addEventListener('change', (e) => {
    S.labels = e.target.checked;
    try { localStorage.setItem('beacon-labels', S.labels ? '1' : '0'); } catch (err) { /* not remembered */ }
    if (S.data) { syncRepeaters(S.data.repeaters); syncBeacons(S.data.beacons); }
  });
  map.on('click', () => { if (S.selected) select(null); });
  document.addEventListener('keydown', (e) => { if (e.key === 'Escape' && S.selected && !document.querySelector('dialog[open]')) select(null); });
  document.addEventListener('visibilitychange', () => { if (!document.hidden) refresh(); });
  setInterval(tick, 1000);

  getJSON('api/map').then(setupBackground, () => setupBackground({ mode: 'grid', problem: 'the map settings could not be read' })).finally(refresh);
})();
