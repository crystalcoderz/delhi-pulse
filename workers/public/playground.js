(() => {
  'use strict';

  const { API, COLORS, h, sketchMap, colorRamp } = window.DelhiPulse;
  const $ = id => document.getElementById(id);

  const SPEED_COLORS = [[8, COLORS.red], [20, COLORS.amberDeep], [32, COLORS.green]];
  const TREE_PAGE = 30;
  const STATUS_WORDS = {
    200: 'OK', 400: 'Bad Request', 403: 'Forbidden', 404: 'Not Found',
    500: 'Server Error', 502: 'Bad Gateway', 503: 'Unavailable',
  };

  const KASHMERE_GATE = '28.666763,77.229413';
  const NEHRU_PLACE = '28.5487,77.256981';
  // Today at 09:00 in Delhi, so the default request always lands in a service hour.
  const NINE_AM = new Intl.DateTimeFormat('en-CA', { timeZone: 'Asia/Kolkata' }).format(new Date())
    + 'T09:00:00+05:30';

  const BILLED_NOTE = 'This makes a billed Google call, so it only runs when you press Send. '
    + 'Only pages on this site may call it: the copied curl command returns 403.';

  const ENDPOINTS = [
    {
      id: 'journeys', path: '/api/journeys', preview: 'journeys',
      summary: 'Bus journeys between two points, fastest first. Each option has walk, wait and ride legs and a p10 to p90 arrival band.',
      params: [
        { name: 'from', required: true, hint: 'lat,lng, or stop:<id>, or a stop name.' },
        { name: 'to', required: true, hint: 'Same forms as from.' },
        { name: 'depart', hint: 'ISO 8601. A time without an offset is read as IST. Empty means now.' },
        { name: 'max', numeric: true, hint: '1 to 6 options. Default 3.' },
        { name: 'asOf', hint: 'A recorded day to replay. The edge answers from the pooled month and says so in note.' },
      ],
      defaults: { from: KASHMERE_GATE, to: NEHRU_PLACE, depart: NINE_AM, max: '3' },
      presets: [
        { label: 'Kashmere Gate to Nehru Place', values: { from: KASHMERE_GATE, to: NEHRU_PLACE, depart: NINE_AM, max: '3' } },
        { label: 'By stop name', values: { from: 'ISBT Kashmere Gate', to: 'Nehru Place Terminal', depart: NINE_AM, max: '3' } },
        { label: 'By stop id', values: { from: 'stop:4822', to: 'stop:2560', depart: NINE_AM, max: '2' } },
        { label: 'Walkable trip', values: { from: '28.6328,77.2197', to: '28.6340,77.2205' } },
        { label: 'Unreadable depart', values: { from: KASHMERE_GATE, to: NEHRU_PLACE, depart: 'tomorrow morning' } },
      ],
      notes: [
        'eta_p50_min equals total_min, and eta_p10_min and eta_p90_min bracket it.',
        'Each bus leg carries source and n: what its speed was based on, and how many recordings back it.',
        'A future departure is cacheable for five minutes. A request without depart is never cached.',
      ],
    },
    {
      id: 'stops-search', path: '/api/stops/search', preview: 'stops',
      summary: 'Fuzzy search over the 6,862 real bus stops. Returns the ten best matches by score.',
      params: [{ name: 'q', required: true, hint: 'At least 2 characters.' }],
      defaults: { q: 'nehru place' },
      presets: [
        { label: 'kashmere gate', values: { q: 'kashmere gate' } },
        { label: 'aiims', values: { q: 'aiims' } },
        { label: 'anand vihar', values: { q: 'anand vihar' } },
      ],
    },
    {
      id: 'stops-nearby', path: '/api/stops/nearby', preview: 'stops',
      summary: 'The nearest stops to a point, with the walking time to each. At most six, all inside the radius.',
      params: [
        { name: 'lat', required: true, numeric: true, hint: 'Latitude inside the Delhi region.' },
        { name: 'lng', required: true, numeric: true, hint: 'Longitude inside the Delhi region.' },
        { name: 'radius', numeric: true, hint: '50 to 20000 metres. Default 2000.' },
      ],
      defaults: { lat: '28.6328', lng: '77.2197', radius: '500' },
      presets: [
        { label: 'Connaught Place', values: { lat: '28.6328', lng: '77.2197', radius: '500' } },
        { label: 'AIIMS', values: { lat: '28.568914', lng: '77.213693', radius: '500' } },
        { label: 'Outside Delhi', values: { lat: '19.076', lng: '72.8777' } },
      ],
    },
    {
      id: 'pulse', path: '/api/pulse', preview: 'pulse',
      summary: 'Citywide average bus speed for each hour of the day, in km/h.',
      params: [],
    },
    {
      id: 'geo', path: '/api/geo', preview: 'geo',
      summary: 'Route lines as GeoJSON with the speed recorded on each at one hour. Routes with no recording that hour fall back to their all-day average and are marked route-avg.',
      params: [
        { name: 'hour', numeric: true, hint: '0 to 23. Default 9.' },
        { name: 'limit', numeric: true, hint: '10 to 1500 routes, longest first. Default 600.' },
        { name: 'stride', numeric: true, hint: '1 to 5. Keeps every nth route. Default 1.' },
      ],
      defaults: { hour: '9', limit: '60', stride: '1' },
      presets: [
        { label: 'Morning, 09:00', values: { hour: '9', limit: '60', stride: '1' } },
        { label: 'Evening peak, 18:00', values: { hour: '18', limit: '60', stride: '1' } },
        { label: 'Night, 02:00', values: { hour: '2', limit: '60', stride: '1' } },
      ],
    },
    {
      id: 'lookup', path: '/api/lookup',
      summary: 'Speed and expected wait for one route at one hour, and which fallback level answered: route-hour, route-avg or city.',
      params: [
        { name: 'route', required: true, hint: 'Internal route id, as returned in route_id by /api/journeys.' },
        { name: 'hour', numeric: true, hint: '0 to 23. Default 9.' },
      ],
      defaults: { route: '1928', hour: '9' },
      presets: [
        { label: '429UP at 09:00', values: { route: '1928', hour: '9' } },
        { label: '425UP at 18:00', values: { route: '1920', hour: '18' } },
        { label: 'Unknown route', values: { route: 'nope', hour: '9' } },
      ],
    },
    {
      id: 'places-autocomplete', path: '/api/places/autocomplete', billed: true,
      summary: 'Landmark suggestions from Google Places, restricted to Delhi NCR. The planner search box uses this.',
      params: [{ name: 'input', required: true, hint: 'At least 3 characters.' }],
      defaults: { input: 'connaught place' },
      notes: [BILLED_NOTE],
    },
    {
      id: 'places-resolve', path: '/api/places/resolve', billed: true,
      summary: 'Turns a Google place id into a name and coordinates.',
      params: [{ name: 'place_id', required: true, hint: 'The id field of an autocomplete suggestion.' }],
      defaults: { place_id: 'ChIJL_P_CXMEDTkRw0ZdG-0GVvw' },
      notes: [BILLED_NOTE],
    },
    {
      id: 'gmaps-drive', path: '/api/gmaps-drive', billed: true,
      summary: 'Google\u2019s traffic-aware driving time between two points. The planner shows it beside the fastest bus.',
      params: [
        { name: 'from', required: true, hint: 'lat,lng only.' },
        { name: 'to', required: true, hint: 'lat,lng only, at most 120 km from the start.' },
        { name: 'depart', hint: 'Used when it falls in the next 7 days. Otherwise live traffic.' },
      ],
      defaults: { from: KASHMERE_GATE, to: NEHRU_PLACE },
      notes: [BILLED_NOTE],
    },
    {
      id: 'status', path: '/api/status',
      summary: 'Model size and live-feed counters. The planner header reads this.',
      params: [],
    },
    {
      id: 'health', path: '/api/health',
      summary: 'A lightweight check that the model loaded.',
      params: [],
    },
  ];

  const values = {};
  for (const ep of ENDPOINTS) values[ep.id] = { ...(ep.defaults || {}) };

  let current = null;
  let sendSeq = 0;
  let lastText = '';
  let wantedTab = 'preview';

  // ---------- small helpers ----------
  const valueOf = (ep, name) => String(values[ep.id][name] ?? '').trim();

  // Commas and colons are legal in a query string; leaving them readable keeps the URL legible.
  const encode = text => encodeURIComponent(text).replace(/%2C/g, ',').replace(/%3A/g, ':');

  function queryOf(ep) {
    return ep.params
      .filter(param => valueOf(ep, param.name))
      .map(param => param.name + '=' + encode(valueOf(ep, param.name)))
      .join('&');
  }

  const pathOf = ep => ep.path + (queryOf(ep) ? '?' + queryOf(ep) : '');

  function formatBytes(bytes) {
    return bytes < 1024 ? bytes + ' B' : (bytes / 1024).toFixed(bytes < 10240 ? 1 : 0) + ' kB';
  }

  async function copy(button, text) {
    button.dataset.label = button.dataset.label || button.textContent;
    try {
      await navigator.clipboard.writeText(text);
      button.textContent = 'Copied';
    } catch (err) {
      button.textContent = 'Copy blocked by the browser';
    }
    setTimeout(() => {
      button.textContent = button.dataset.label;
    }, 1600);
  }

  const routePlates = name => String(name || '').split(/\s+\+\s+/).filter(Boolean)
    .map(route => h('span', { class: 'plate plate-sm', text: route }));

  function table(headers, rows) {
    return h('div', { class: 'table-scroll' },
      h('table', { class: 'pv-table' },
        h('thead', {}, h('tr', {}, headers.map(text => h('th', { scope: 'col', text })))),
        h('tbody', {}, rows.map(cells => h('tr', {}, cells.map(cell => h('td', {}, cell == null ? '' : cell)))))));
  }

  // ---------- JSON tree ----------
  const isLeaf = value => value === null || typeof value !== 'object';
  // Short arrays of plain values, such as coordinates, stay on one line.
  const isTuple = value => Array.isArray(value) && value.length > 0 && value.length <= 4 && value.every(isLeaf);

  function leafNode(value) {
    if (typeof value === 'string') return h('span', { class: 'j-str', text: JSON.stringify(value) });
    if (typeof value === 'number') return h('span', { class: 'j-num', text: String(value) });
    return h('span', { class: 'j-null', text: String(value) });
  }

  function summaryOf(value) {
    if (Array.isArray(value)) return value.length === 1 ? '1 item' : value.length + ' items';
    const keys = Object.keys(value);
    const shown = keys.filter(key => isLeaf(value[key])).slice(0, 3);
    const text = shown.map(key => {
      const raw = typeof value[key] === 'string' ? JSON.stringify(value[key]) : String(value[key]);
      return key + ': ' + (raw.length > 28 ? raw.slice(0, 27) + '\u2026' : raw);
    });
    if (keys.length > shown.length) text.push('\u2026');
    return text.join(', ');
  }

  function fillTree(parent, value, depth) {
    const inArray = Array.isArray(value);
    const entries = inArray ? value.map((item, i) => [String(i), item]) : Object.entries(value);
    let shown = 0;
    const more = h('button', { type: 'button', class: 'text-btn j-more' });
    const addPage = () => {
      more.remove();
      for (const [key, item] of entries.slice(shown, shown + TREE_PAGE)) {
        parent.append(treeNode(key, item, depth, inArray, shown === 0));
        shown++;
      }
      const left = entries.length - shown;
      if (left > 0) {
        more.textContent = `Show ${Math.min(TREE_PAGE, left)} more (${left} left)`;
        parent.append(more);
      }
    };
    more.addEventListener('click', addPage);
    addPage();
  }

  function treeNode(key, value, depth, inArray, isFirst) {
    const label = key == null ? [] : [h('span', { class: inArray ? 'j-idx' : 'j-key', text: key }), ': '];
    if (isLeaf(value)) return h('div', { class: 'j-row' }, label, leafNode(value));
    if (isTuple(value)) {
      return h('div', { class: 'j-row' }, label, '[', value.map((item, i) => [i ? ', ' : '', leafNode(item)]), ']');
    }
    const isArray = Array.isArray(value);
    if (!(isArray ? value.length : Object.keys(value).length)) {
      return h('div', { class: 'j-row' }, label, isArray ? '[]' : '{}');
    }

    // Open the top two levels, plus the first item of a list one level further, so the
    // shape of a record is visible without unfolding the whole response.
    const open = depth < 2 || (depth === 2 && inArray && isFirst);
    const kids = h('div', { class: 'j-kids' });
    const node = h('details', { class: 'j-node', open },
      h('summary', {}, label, isArray ? '[' : '{',
        h('span', { class: 'j-prev', text: ' ' + summaryOf(value) + ' ' }),
        h('span', { class: 'j-close', text: isArray ? ']' : '}' })),
      kids);
    // Collapsed branches are built on first open, which keeps large responses cheap.
    const build = () => fillTree(kids, value, depth + 1);
    if (open) build();
    else node.addEventListener('toggle', build, { once: true });
    return node;
  }

  // ---------- previews ----------
  const note = text => h('p', { class: 'pv-note', text });

  function previewJourneys(data) {
    const options = Array.isArray(data.options) ? data.options : [];
    if (!options.length) return note(data.note ? `No options. note: ${data.note}` : 'No options were returned.');

    const box = h('div', { class: 'sketch-box' });
    const sketch = sketchMap(box, { label: 'Schematic of the selected journey option' });
    const legs = h('div', {});

    const draw = option => {
      const layers = [];
      const busLegs = (option.legs || []).filter(leg => leg.type === 'bus');
      for (const leg of option.legs || []) {
        if (leg.type === 'transfer') continue;
        const path = (leg.stops || leg.coords || [])
          .filter(c => Array.isArray(c) && c.length >= 2).map(c => [c[1], c[0]]);
        if (path.length < 2) continue;
        if (leg.type !== 'bus') {
          layers.push({ path, color: COLORS.ink, width: 2.5, dash: [0.5, 7] });
          continue;
        }
        layers.push({ path, color: COLORS.green, width: 5, casing: COLORS.white });
        const n = busLegs.indexOf(leg);
        path.forEach((at, i) => {
          const first = i === 0, last = i === path.length - 1;
          if (!first && !last) return layers.push({ at, r: 2.5, fill: COLORS.white, stroke: COLORS.green, strokeWidth: 1.5 });
          const isTransfer = (first && n > 0) || (last && n < busLegs.length - 1);
          layers.push({ at, r: 6, fill: isTransfer ? COLORS.amber : COLORS.white, stroke: COLORS.ink, strokeWidth: 2.5 });
        });
      }
      sketch.set(layers);
    };

    const legRows = option => (option.legs || []).map(leg => {
      const stops = Array.isArray(leg.stops) ? leg.stops.length - 1 : null;
      const detail = leg.type === 'bus'
        ? [routePlates(leg.route), stops != null ? ` ${stops} ${stops === 1 ? 'stop' : 'stops'}` : '']
        : leg.type === 'transfer' ? `at stop ${leg.at}` + (leg.walk_min ? `, ${leg.walk_min} min on foot` : '')
          : '';
      return [leg.type, leg.minutes, detail, leg.source, leg.n == null ? '' : Number(leg.n).toLocaleString('en-IN')];
    });

    const buttons = options.map((option, i) => {
      const band = option.eta_p10_min != null && option.eta_p90_min != null
        ? `p10 ${option.eta_p10_min} \u00b7 p90 ${option.eta_p90_min}` : '';
      const button = h('button', { type: 'button', class: 'opt', 'aria-pressed': 'false' },
        h('span', { class: 'opt-min', text: `${option.total_min} min` }),
        h('span', { class: 'plates' }, routePlates(option.route_name)),
        band && h('span', { class: 'hint', text: band }));
      button.addEventListener('click', () => choose(i));
      return button;
    });

    function choose(index) {
      buttons.forEach((button, i) => button.setAttribute('aria-pressed', String(i === index)));
      draw(options[index]);
      legs.replaceChildren(table(['Leg', 'Minutes', 'Detail', 'Source', 'n'], legRows(options[index])));
    }
    choose(0);

    return h('div', { class: 'pv' },
      data.note && note('note: ' + data.note),
      h('div', { class: 'opts', role: 'group', 'aria-label': 'Journey options' }, buttons),
      box, legs);
  }

  function previewGeo(data) {
    const features = (Array.isArray(data.features) ? data.features : [])
      .filter(f => f && f.geometry && Array.isArray(f.geometry.coordinates));
    if (!features.length) return note('No features were returned.');
    const box = h('div', { class: 'sketch-box' });
    sketchMap(box, { label: 'Schematic of the returned routes colored by speed' }).set(features.map(feature => {
      const props = feature.properties || {};
      const speed = Number(props.speed_kmh);
      return {
        path: feature.geometry.coordinates,
        color: colorRamp(Number.isFinite(speed) ? speed : 20, SPEED_COLORS),
        width: 2.5,
        opacity: props.source === 'route-avg' ? 0.3 : 0.85,
      };
    }));
    const averaged = features.filter(f => f.properties && f.properties.source === 'route-avg').length;
    const hour = String(data.hour ?? '').padStart(2, '0');
    return h('div', { class: 'pv' },
      h('p', { text: `${features.length} routes at ${hour}:00, colored by speed_kmh.`
        + (averaged ? ` ${averaged} are faded: their source is route-avg.` : '') }),
      box,
      h('div', { class: 'legend' },
        h('div', { class: 'ramp', 'aria-hidden': 'true' }),
        h('div', { class: 'ramp-ticks' },
          h('span', { text: '8 km/h or slower' }), h('span', { text: '20' }), h('span', { text: '32 or faster' }))));
  }

  function previewPulse(data) {
    const curve = data.city_hour;
    if (!curve || typeof curve !== 'object') return note('No city_hour curve was returned.');
    const hours = Array.from({ length: 24 }, (_, hour) => hour);
    const speeds = hours.map(hour => Number(curve[hour]));
    const known = hours.filter(hour => Number.isFinite(speeds[hour]));
    if (!known.length) return note('No city_hour curve was returned.');
    const peak = Math.max(...known.map(hour => speeds[hour]));
    const slowest = known.reduce((a, b) => (speeds[b] < speeds[a] ? b : a));
    const fastest = known.reduce((a, b) => (speeds[b] > speeds[a] ? b : a));
    const at = hour => `${String(hour).padStart(2, '0')}:00 at ${speeds[hour].toFixed(1)} km/h`;
    const summary = `Slowest hour is ${at(slowest)}. Fastest is ${at(fastest)}.`;

    return h('div', { class: 'pv' },
      h('p', { text: summary }),
      h('div', { class: 'curve' },
        h('div', { class: 'hours', role: 'img', 'aria-label': 'Citywide average bus speed by hour. ' + summary },
          hours.map(hour => {
            const bar = h('div', { class: 'hour-bar is-static' });
            const speed = speeds[hour];
            bar.style.height = (Number.isFinite(speed) ? Math.max(8, (speed / peak) * 100) : 8) + '%';
            if (Number.isFinite(speed)) bar.style.background = colorRamp(speed, SPEED_COLORS);
            return bar;
          })),
        h('div', { class: 'hour-ticks', 'aria-hidden': 'true' },
          hours.map(hour => h('span', { text: hour % 3 === 0 ? String(hour).padStart(2, '0') : '' })))));
  }

  function previewStops(data, ep) {
    if (!Array.isArray(data)) return null;
    if (!data.length) return note('No stops matched.');
    const nearby = ep.id === 'stops-nearby';
    const layers = data.map(stop => ({
      at: [stop.lng, stop.lat], r: 5, fill: COLORS.white, stroke: COLORS.green, strokeWidth: 2.5,
    }));
    const lat = Number(valueOf(ep, 'lat')), lng = Number(valueOf(ep, 'lng'));
    if (nearby && Number.isFinite(lat) && Number.isFinite(lng)) {
      layers.push({ at: [lng, lat], r: 7, fill: COLORS.ink, stroke: COLORS.white, strokeWidth: 2 });
    }
    const box = h('div', { class: 'sketch-box is-short' });
    sketchMap(box, { label: 'Positions of the returned stops' }).set(layers);

    return h('div', { class: 'pv' },
      nearby && note('The dark dot is the point you asked about.'),
      box,
      table(nearby ? ['Stop', 'Name', 'Metres', 'Walk min'] : ['Stop', 'Name', 'Score'],
        data.map(stop => (nearby
          ? [stop.stop_id, stop.name, stop.distance_m, stop.walk_min]
          : [stop.stop_id, stop.name, stop.score]))));
  }

  const PREVIEWS = { journeys: previewJourneys, geo: previewGeo, pulse: previewPulse, stops: previewStops };

  // ---------- tabs ----------
  const TABS = { preview: ['tabPreview', 'panePreview'], json: ['tabJson', 'paneJson'] };

  function showTab(name) {
    for (const [key, [tabId, paneId]] of Object.entries(TABS)) {
      const on = key === name;
      $(tabId).setAttribute('aria-selected', String(on));
      $(tabId).tabIndex = on ? 0 : -1;
      $(paneId).hidden = !on;
    }
  }

  for (const [name, [tabId]] of Object.entries(TABS)) {
    $(tabId).addEventListener('click', () => {
      wantedTab = name;
      showTab(name);
    });
    $(tabId).addEventListener('keydown', e => {
      if (e.key !== 'ArrowLeft' && e.key !== 'ArrowRight') return;
      if ($('tabPreview').hidden) return;
      e.preventDefault();
      wantedTab = name === 'preview' ? 'json' : 'preview';
      showTab(wantedTab);
      $(TABS[wantedTab][0]).focus();
    });
  }

  // ---------- request and response ----------
  function showRequest() {
    const url = API + pathOf(current);
    $('reqUrl').textContent = url;
    $('openRaw').href = url;
  }

  function showPanes(preview, json) {
    $('panePreview').replaceChildren(preview || '');
    $('paneJson').replaceChildren(json || '');
    $('tabPreview').hidden = !preview;
    showTab(preview && wantedTab === 'preview' ? 'preview' : 'json');
  }

  function showIdle(ep) {
    lastText = '';
    $('resMeta').replaceChildren(h('span', {
      text: ep.billed ? 'Not sent yet. This endpoint is billed, so it waits for you to press Send.' : 'Not sent yet.',
    }));
    showPanes(null, note('The response will appear here.'));
  }

  function showResponse(ep, outcome) {
    const meta = $('resMeta');
    if (outcome.failed) {
      lastText = '';
      meta.replaceChildren(
        h('span', { class: 'plate plate-sm status-err', text: 'No response' }),
        h('span', { text: 'The request did not reach the server. Check your connection and send it again.' }));
      return showPanes(null, note('Nothing was received.'));
    }

    const { response, text, ms } = outcome;
    lastText = text;
    let data, parsed = true;
    try {
      data = JSON.parse(text);
    } catch (err) {
      parsed = false;
    }
    const cache = response.headers.get('cache-control');
    meta.replaceChildren(
      h('span', {
        class: 'plate plate-sm ' + (response.ok ? 'status-ok' : 'status-err'),
        text: `${response.status} ${response.statusText || STATUS_WORDS[response.status] || ''}`.trim(),
      }),
      h('span', { text: `${Math.round(ms)} ms` }),
      h('span', { text: formatBytes(new Blob([text]).size) }),
      h('span', { text: cache ? 'cache-control: ' + cache : 'no cache-control header' }));

    const json = parsed
      ? h('div', { class: 'j-tree' }, treeNode(null, data, 0, false, true))
      : note(text ? text.slice(0, 2000) : 'The response body was empty.');
    const preview = parsed && response.ok && ep.preview ? PREVIEWS[ep.preview](data, ep) : null;
    showPanes(preview, json);
  }

  function setBusy(ep, busy) {
    const button = ep.form.querySelector('.plan-btn');
    button.disabled = busy;
    button.textContent = busy ? 'Sending\u2026' : 'Send request';
  }

  // reveal: on phones, bring the response into view. Off for the request sent on page load,
  // so a first visit starts at the endpoint list.
  async function send(reveal = true) {
    const ep = current;
    const msg = ep.form.querySelector('.form-msg');
    const missing = ep.params.find(param => param.required && !valueOf(ep, param.name));
    if (missing) {
      msg.textContent = `${missing.name} is required.`;
      msg.hidden = false;
      ep.form.elements[missing.name].focus();
      return;
    }
    msg.hidden = true;

    const path = pathOf(ep);
    history.replaceState(null, '', '#' + ep.id + path.slice(ep.path.length));
    const seq = ++sendSeq;
    setBusy(ep, true);
    $('resMeta').replaceChildren(h('span', { text: 'Sending\u2026' }));

    const started = performance.now();
    let outcome;
    try {
      const response = await fetch(API + path, { headers: { accept: 'application/json' } });
      const text = await response.text();
      outcome = { response, text, ms: performance.now() - started };
    } catch (err) {
      outcome = { failed: true };
    }
    setBusy(ep, false);
    // A newer request owns the response area now.
    if (seq !== sendSeq) return;
    showResponse(ep, outcome);
    if (reveal && matchMedia('(max-width: 899px)').matches) $('response').scrollIntoView({ block: 'start' });
  }

  // ---------- endpoint list ----------
  function syncInputs(ep) {
    for (const param of ep.params) ep.form.elements[param.name].value = values[ep.id][param.name] ?? '';
  }

  function buildForm(ep) {
    const form = h('form', { class: 'ep-form', novalidate: true, autocomplete: 'off' },
      h('p', { text: ep.summary }));

    for (const param of ep.params) {
      const id = `p-${ep.id}-${param.name}`;
      const input = h('input', {
        id, name: param.name, type: 'text', inputmode: param.numeric ? 'decimal' : null,
        'aria-describedby': id + '-hint', 'aria-required': param.required ? 'true' : null,
        spellcheck: 'false', autocapitalize: 'off',
      });
      input.addEventListener('input', () => {
        values[ep.id][param.name] = input.value;
        showRequest();
      });
      form.append(h('div', { class: 'field' },
        h('div', { class: 'label-row' },
          h('label', { for: id, text: param.name }),
          h('span', { class: 'req', text: param.required ? 'Required' : 'Optional' })),
        input,
        h('p', { class: 'hint', id: id + '-hint', text: param.hint })));
    }
    if (!ep.params.length) form.append(h('p', { class: 'hint', text: 'This endpoint takes no parameters.' }));

    if (ep.presets) {
      form.append(h('div', { class: 'chips', role: 'group', 'aria-label': 'Example requests' },
        ep.presets.map(preset => {
          const chip = h('button', { type: 'button', class: 'chip', text: preset.label });
          chip.addEventListener('click', () => {
            values[ep.id] = { ...preset.values };
            syncInputs(ep);
            showRequest();
            send();
          });
          return chip;
        })));
    }

    form.append(
      h('p', { class: 'form-msg', role: 'alert', hidden: true }),
      h('button', { type: 'submit', class: 'plan-btn', text: 'Send request' }));
    if (ep.notes) form.append(h('ul', { class: 'ep-notes' }, ep.notes.map(text => h('li', { text }))));

    form.addEventListener('submit', e => {
      e.preventDefault();
      send();
    });
    ep.form = form;
  }

  function select(ep, opts = {}) {
    current = ep;
    for (const other of ENDPOINTS) other.chip.setAttribute('aria-pressed', String(other === ep));
    if (!ep.form) buildForm(ep);
    $('epPath').textContent = ep.path;
    $('epTag').hidden = !ep.billed;
    $('epBody').replaceChildren(ep.form);
    syncInputs(ep);
    showRequest();
    if (opts.send) send(!opts.quiet);
    else {
      sendSeq++;
      showIdle(ep);
    }
  }

  // Every endpoint stays one tap away, however long the open form is.
  function buildSwitcher() {
    for (const ep of ENDPOINTS) {
      ep.chip = h('button', {
        type: 'button', class: 'chip', 'aria-pressed': 'false', text: ep.path.replace(/^\/api/, ''),
      });
      ep.chip.addEventListener('click', () => {
        if (current !== ep) select(ep, { send: !ep.billed });
      });
      $('epChips').append(ep.chip);
    }
  }

  // The hash carries a whole request (#journeys?from=...&to=...), so a request can be linked to.
  function applyHash() {
    const hash = location.hash.slice(1);
    const cut = hash.indexOf('?');
    const ep = ENDPOINTS.find(item => item.id === (cut < 0 ? hash : hash.slice(0, cut)));
    if (!ep) return false;
    if (cut >= 0) {
      const given = new URLSearchParams(hash.slice(cut + 1));
      values[ep.id] = {};
      for (const param of ep.params) {
        if (given.has(param.name)) values[ep.id][param.name] = given.get(param.name);
      }
    }
    select(ep, { send: !ep.billed, quiet: true });
    return true;
  }

  // ---------- startup ----------
  $('baseUrl').textContent = API;
  $('copyUrl').addEventListener('click', e => copy(e.currentTarget, API + pathOf(current)));
  $('copyCurl').addEventListener('click', e => copy(e.currentTarget, `curl "${API + pathOf(current)}"`));
  $('copyJson').addEventListener('click', e => {
    let pretty = lastText;
    try {
      pretty = JSON.stringify(JSON.parse(lastText), null, 2);
    } catch (err) {
      // Not JSON: copy the body as received.
    }
    copy(e.currentTarget, pretty);
  });

  buildSwitcher();
  if (!applyHash()) select(ENDPOINTS[0], { send: true, quiet: true });
  window.addEventListener('hashchange', applyHash);
})();
