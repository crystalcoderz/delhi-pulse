(() => {
  'use strict';

  const { API, COLORS, REDUCED_MOTION, h, getJSON, createMap, labelLayerId } = window.DelhiPulse;
  const $ = id => document.getElementById(id);
  const enc = encodeURIComponent;

  const ENDS = ['from', 'to'];
  const MY_LOCATION = 'My location';
  // Coordinates rather than stop ids, so the same examples work on the edge and the local API.
  const EXAMPLES = [
    { from: ['ISBT Kashmere Gate', 77.229413, 28.666763], to: ['Nehru Place Terminal', 77.256981, 28.5487] },
    { from: ['New Delhi Railway Station', 77.223592, 28.641768], to: ['AIIMS', 77.213693, 28.568914] },
    { from: ['Anand Vihar ISBT', 77.314668, 28.646953], to: ['Shivaji Stadium, Connaught Place', 77.215455, 28.633064] },
  ];

  const picks = { from: null, to: null };
  const markers = { from: null, to: null };
  const combos = {};
  const replayDays = new Set();
  let planSeq = 0;
  let shownJourney = null;

  const map = createMap('map', { center: [77.21, 28.62], zoom: 10.5 });
  window.map = map;
  const mapReady = new Promise(resolve => {
    if (map) map.on('load', resolve);
  });

  // ---------- small helpers ----------
  const bold = text => h('b', { text });
  const minutes = value => {
    const n = Number(value) || 0;
    return n > 0 && n < 1 ? 1 : Math.round(n);
  };
  const isNarrow = () => matchMedia('(max-width: 899px)').matches;

  function clockTime(iso) {
    const date = new Date(iso);
    return Number.isNaN(date.getTime()) ? '' : date.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
  }

  function dayLabel(isoDay) {
    const [y, m, d] = isoDay.split('-').map(Number);
    const date = new Date(Date.UTC(y, m - 1, d));
    return Number.isNaN(date.getTime())
      ? isoDay
      : date.toLocaleDateString('en-IN', { day: 'numeric', month: 'short', year: 'numeric', timeZone: 'UTC' });
  }

  function sentence(text) {
    const s = String(text).trim();
    if (!s) return s;
    const capped = s[0].toUpperCase() + s.slice(1);
    return /[.!?]$/.test(capped) ? capped : capped + '.';
  }

  function nowLocalISO() {
    const now = new Date();
    now.setMinutes(now.getMinutes() - now.getTimezoneOffset());
    return now.toISOString().slice(0, 16);
  }

  function departISO() {
    const date = new Date($('when').value);
    return Number.isNaN(date.getTime()) ? '' : date.toISOString();
  }

  function announce(text) {
    $('planStatus').textContent = text;
  }

  function showFormMsg(text) {
    const msg = $('formMsg');
    msg.textContent = text;
    msg.hidden = !text;
  }

  function debounce(fn, ms) {
    let timer;
    return (...args) => {
      clearTimeout(timer);
      timer = setTimeout(() => fn(...args), ms);
    };
  }

  // ---------- map: journey layers ----------
  mapReady.then(() => {
    const before = labelLayerId(map);
    const empty = { type: 'FeatureCollection', features: [] };
    const round = { 'line-cap': 'round', 'line-join': 'round' };
    for (const id of ['journey-walk', 'journey-bus', 'journey-stops']) {
      map.addSource(id, { type: 'geojson', data: empty });
    }
    map.addLayer({
      id: 'journey-walk', type: 'line', source: 'journey-walk', layout: round,
      paint: { 'line-color': COLORS.ink, 'line-width': 4, 'line-dasharray': [0.1, 2] },
    }, before);
    map.addLayer({
      id: 'journey-bus-casing', type: 'line', source: 'journey-bus', layout: round,
      paint: { 'line-color': COLORS.white, 'line-width': 9 },
    }, before);
    map.addLayer({
      id: 'journey-bus', type: 'line', source: 'journey-bus', layout: round,
      paint: { 'line-color': COLORS.green, 'line-width': 5 },
    }, before);
    map.addLayer({
      id: 'journey-stops-minor', type: 'circle', source: 'journey-stops', minzoom: 12,
      filter: ['==', ['get', 'kind'], 'stop'],
      paint: {
        'circle-radius': 3, 'circle-color': COLORS.white,
        'circle-stroke-color': COLORS.green, 'circle-stroke-width': 1.5,
      },
    }, before);
    map.addLayer({
      id: 'journey-stops-major', type: 'circle', source: 'journey-stops',
      filter: ['!=', ['get', 'kind'], 'stop'],
      paint: {
        'circle-radius': 6,
        'circle-color': ['match', ['get', 'kind'], 'transfer', COLORS.amber, COLORS.white],
        'circle-stroke-color': COLORS.ink, 'circle-stroke-width': 2.5,
      },
    }, before);
  });

  const collection = features => ({ type: 'FeatureCollection', features });
  const lineFeature = coordinates => ({ type: 'Feature', properties: {}, geometry: { type: 'LineString', coordinates } });
  const pointFeature = (coordinates, kind) => ({ type: 'Feature', properties: { kind }, geometry: { type: 'Point', coordinates } });

  function draw(journey) {
    shownJourney = journey;
    if (!map) return;
    mapReady.then(() => {
      if (shownJourney !== journey) return;
      const walk = [], bus = [], stops = [], extent = [];
      const legs = journey && Array.isArray(journey.legs) ? journey.legs : [];
      const busLegs = legs.filter(leg => leg.type === 'bus');

      for (const leg of legs) {
        if (leg.type === 'transfer') continue;
        // The API sends [lat, lng]; GeoJSON wants [lng, lat].
        const pts = (leg.stops || leg.coords || [])
          .filter(c => Array.isArray(c) && c.length >= 2)
          .map(c => [c[1], c[0]]);
        if (pts.length < 2) continue;
        extent.push(...pts);
        if (leg.type !== 'bus') {
          walk.push(lineFeature(pts));
          continue;
        }
        bus.push(lineFeature(pts));
        const n = busLegs.indexOf(leg);
        pts.forEach((pt, i) => {
          let kind = 'stop';
          if (i === 0) kind = n === 0 ? 'board' : 'transfer';
          else if (i === pts.length - 1) kind = n === busLegs.length - 1 ? 'alight' : 'transfer';
          stops.push(pointFeature(pt, kind));
        });
      }

      map.getSource('journey-walk').setData(collection(walk));
      map.getSource('journey-bus').setData(collection(bus));
      map.getSource('journey-stops').setData(collection(stops));

      if (!journey) return;
      for (const end of ENDS) if (picks[end]) extent.push(picks[end].coord);
      if (extent.length < 2) return;
      map.resize();
      const bounds = extent.reduce((b, c) => b.extend(c), new maplibregl.LngLatBounds(extent[0], extent[0]));
      const box = map.getContainer();
      const padding = Math.max(24, Math.min(64, Math.round(Math.min(box.clientWidth, box.clientHeight) / 6)));
      map.fitBounds(bounds, { padding, maxZoom: 15, duration: REDUCED_MOTION ? 0 : 600 });
    });
  }

  // ---------- endpoints: picks, markers, hints ----------
  const coordLabel = (lng, lat) => lat.toFixed(4) + ', ' + lng.toFixed(4);
  const isEmpty = end => !picks[end] && !$(end).value.trim();

  function placeMarker(end) {
    if (!map) return;
    const pick = picks[end];
    if (!pick) {
      if (markers[end]) markers[end].remove();
      return;
    }
    if (!markers[end]) {
      const pin = h('div', { class: 'pin pin-' + end, text: end === 'from' ? 'A' : 'B' });
      const marker = new maplibregl.Marker({ element: pin, draggable: true });
      marker.on('dragend', () => {
        const p = marker.getLngLat();
        setPoint(end, coordLabel(p.lng, p.lat), [p.lng, p.lat], null, { nearby: true });
      });
      markers[end] = marker;
    }
    markers[end].setLngLat(pick.coord).addTo(map);
  }

  function updateHint() {
    const hint = $('mapHint');
    let text = '';
    if (map) {
      if (isEmpty('from')) text = 'Or pick your start on the map.';
      else if (isEmpty('to')) text = 'Or pick your destination on the map.';
      else if (picks.from && picks.to) text = 'Drag pin A or B on the map to adjust.';
    }
    hint.textContent = text;
    hint.hidden = !text;
  }

  function clearPick(end) {
    if (!picks[end]) return;
    picks[end] = null;
    placeMarker(end);
  }

  // Replaces a bare coordinate label with the nearest stop name once it is known.
  function nameFromNearestStop(end, pick) {
    const [lng, lat] = pick.coord;
    // The edge reads `radius`, the local API reads `radius_m`.
    getJSON(`/stops/nearby?lat=${lat}&lng=${lng}&radius=400&radius_m=400`, null).then(near => {
      if (picks[end] !== pick || !Array.isArray(near) || !near[0] || !near[0].name) return;
      const label = (pick.label === MY_LOCATION ? MY_LOCATION + ', near ' : 'Near ') + near[0].name;
      if ($(end).value === pick.label) $(end).value = label;
      pick.label = label;
    });
  }

  function setPoint(end, label, coord, stopId, opts = {}) {
    const pick = { label, coord, stopId: stopId == null ? null : String(stopId) };
    picks[end] = pick;
    $(end).value = label;
    combos[end].seq++;
    closeList(combos[end]);
    placeMarker(end);
    showFormMsg('');
    updateHint();
    if (opts.nearby) nameFromNearestStop(end, pick);
    if (picks.from && picks.to) plan();
    else if (map && opts.pan) {
      map.easeTo({ center: coord, zoom: Math.max(map.getZoom(), 13), duration: REDUCED_MOTION ? 0 : 500 });
    }
  }

  if (map) {
    map.on('click', e => {
      const target = e.originalEvent && e.originalEvent.target;
      if (target && target.closest && target.closest('.pin')) return;
      const end = isEmpty('from') ? 'from' : isEmpty('to') ? 'to' : null;
      if (!end) return;
      setPoint(end, coordLabel(e.lngLat.lng, e.lngLat.lat), [e.lngLat.lng, e.lngLat.lat], null, { nearby: true });
    });
  }

  // ---------- search: DTC stops + Google Places via the edge proxy ----------
  function openList(combo) {
    combo.list.hidden = false;
    combo.input.setAttribute('aria-expanded', 'true');
  }

  function closeList(combo) {
    combo.list.hidden = true;
    combo.active = -1;
    combo.input.setAttribute('aria-expanded', 'false');
    combo.input.removeAttribute('aria-activedescendant');
  }

  function setActive(combo, index) {
    combo.active = index;
    combo.items.forEach((item, i) => item.node.setAttribute('aria-selected', String(i === index)));
    const item = combo.items[index];
    if (item) {
      combo.input.setAttribute('aria-activedescendant', item.node.id);
      item.node.scrollIntoView({ block: 'nearest' });
    } else {
      combo.input.removeAttribute('aria-activedescendant');
    }
  }

  function afterChoose(end) {
    const other = end === 'from' ? 'to' : 'from';
    if (isEmpty(other)) $(other).focus();
    else $(end).blur();
  }

  function renderSuggestions(end, stopHits, placeHits, settled) {
    const combo = combos[end];
    combo.list.replaceChildren();
    combo.items = [];

    const addOption = (node, choose) => {
      node.id = `${end}-opt-${combo.items.length}`;
      node.setAttribute('role', 'option');
      node.setAttribute('aria-selected', 'false');
      node.addEventListener('click', choose);
      combo.items.push({ node, choose });
      combo.list.append(node);
    };

    if (stopHits.length) {
      combo.list.append(h('div', { class: 'sug-heading', role: 'presentation', text: 'Bus stops' }));
      for (const stop of stopHits) {
        addOption(
          h('div', { class: 'sug-item' },
            h('span', { class: 'nm', text: stop.name }),
            h('span', { class: 'dst', text: 'Stop ' + stop.stop_id })),
          () => {
            setPoint(end, stop.name, [stop.lng, stop.lat], stop.stop_id, { pan: true });
            afterChoose(end);
          },
        );
      }
    }

    if (placeHits.length) {
      combo.list.append(h('div', { class: 'sug-heading', role: 'presentation', text: 'Places \u00b7 Google' }));
      for (const place of placeHits.slice(0, 6)) {
        const name = h('span', { class: 'nm', text: place.text });
        const kind = String((place.types || [])[0] || '').replace(/_/g, ' ');
        addOption(
          h('div', { class: 'sug-item place-item' }, name, kind && h('span', { class: 'dst', text: kind })),
          async () => {
            name.textContent = 'Locating\u2026';
            try {
              const found = await fetch(`${API}/places/resolve?place_id=${enc(place.id)}`).then(r => r.json());
              if (found.lat == null || found.lng == null) throw new Error('no coordinates');
              setPoint(end, found.name || place.text, [found.lng, found.lat], null, { pan: true });
              afterChoose(end);
            } catch (err) {
              name.textContent = 'Could not locate that place. Pick a point on the map instead.';
            }
          },
        );
      }
    }

    if (!combo.items.length) {
      if (!settled) return closeList(combo);
      combo.list.append(h('div', {
        class: 'sug-empty', role: 'presentation',
        text: 'No matching stops or places. Try a nearby landmark, or pick a point on the map.',
      }));
    }
    combo.active = -1;
    combo.input.removeAttribute('aria-activedescendant');
    openList(combo);
  }

  function suggest(end) {
    const combo = combos[end];
    const query = combo.input.value.trim();
    const seq = ++combo.seq;
    if (query.length < 2) return closeList(combo);

    const wantPlaces = query.length >= 3;
    let stopHits = [], placeHits = [], pending = wantPlaces ? 2 : 1;
    // Stops render as soon as they arrive instead of waiting on the slower Places call.
    const update = () => {
      if (seq === combo.seq && !picks[end]) renderSuggestions(end, stopHits, placeHits, pending === 0);
    };

    getJSON(`/stops/search?q=${enc(query)}`, []).then(hits => {
      stopHits = Array.isArray(hits) ? hits : [];
      pending--;
      update();
    });
    if (wantPlaces) {
      getJSON(`/places/autocomplete?input=${enc(query)}`, null).then(res => {
        placeHits = res && Array.isArray(res.suggestions) ? res.suggestions : [];
        pending--;
        update();
      });
    }
  }

  function onComboKey(end, e) {
    // Enter while an IME is composing confirms the composition; it must not pick or submit.
    if (e.isComposing || e.keyCode === 229) return;
    const combo = combos[end];
    const open = !combo.list.hidden && combo.items.length > 0;
    if (e.key === 'ArrowDown' || e.key === 'ArrowUp') {
      if (!open) return;
      e.preventDefault();
      const n = combo.items.length;
      const step = e.key === 'ArrowDown' ? 1 : -1;
      setActive(combo, combo.active < 0 ? (step > 0 ? 0 : n - 1) : (combo.active + step + n) % n);
    } else if (e.key === 'Enter') {
      if (open && combo.active >= 0) {
        e.preventDefault();
        combo.items[combo.active].choose();
      }
    } else if (e.key === 'Escape') {
      if (open) {
        e.preventDefault();
        closeList(combo);
      }
    } else if (e.key === 'Tab') {
      closeList(combo);
    }
  }

  for (const end of ENDS) {
    const combo = { input: $(end), list: $(end + 'Sug'), items: [], active: -1, seq: 0 };
    combos[end] = combo;
    const run = debounce(() => suggest(end), 200);
    combo.input.addEventListener('input', () => {
      clearPick(end);
      showFormMsg('');
      updateHint();
      run();
    });
    combo.input.addEventListener('keydown', e => onComboKey(end, e));
    // Keeps focus in the input while a suggestion is tapped, so the layout cannot shift under the tap.
    combo.list.addEventListener('mousedown', e => e.preventDefault());
  }

  document.addEventListener('pointerdown', e => {
    for (const end of ENDS) {
      if (!e.target.closest || !e.target.closest('#' + end + 'Field')) closeList(combos[end]);
    }
  });

  // ---------- plan ----------
  function endpointParam(end) {
    const pick = picks[end];
    if (pick) return pick.stopId ? 'stop:' + pick.stopId : pick.coord[1] + ',' + pick.coord[0];
    return $(end).value.trim();
  }

  function setBusy(busy) {
    const button = $('plan');
    button.disabled = busy;
    button.textContent = busy ? 'Finding\u2026' : 'Find journeys';
  }

  async function plan() {
    const fromVal = endpointParam('from');
    const toVal = endpointParam('to');
    if (!fromVal || !toVal) {
      showFormMsg(!fromVal && !toVal ? 'Enter a start and a destination.'
        : !fromVal ? 'Enter where you are starting from.' : 'Enter where you are going.');
      $(fromVal ? 'to' : 'from').focus();
      return;
    }
    showFormMsg('');

    const seq = ++planSeq;
    setBusy(true);
    draw(null);
    $('results').replaceChildren(h('div', { class: 'ghost' }), h('div', { class: 'ghost' }));
    announce('Finding journeys.');

    const depart = departISO();
    const asOf = $('asof').value ? $('asof').value + 'T08:00:00+05:30' : '';
    const url = `${API}/journeys?from=${enc(fromVal)}&to=${enc(toVal)}`
      + (depart ? `&depart=${enc(depart)}` : '')
      + (asOf ? `&asOf=${enc(asOf)}` : '')
      + '&max=4';

    let result;
    try {
      const response = await fetch(url);
      result = await response.json().catch(() => ({}));
      if (!response.ok && !Array.isArray(result.options)) {
        result = { options: [], note: typeof result.detail === 'string' ? result.detail : null, failed: true };
      }
    } catch (err) {
      result = { options: [], note: 'Could not reach the planner. Check your connection and try again.' };
    }

    // A newer search has started; it owns the button and the results now.
    if (seq !== planSeq) return;
    setBusy(false);
    render(result);
    fetchDriveCompare(result, seq);
  }

  // Splits a journey into the pieces shown in the leg bar and the step list.
  function segments(journey) {
    const legs = Array.isArray(journey.legs) ? journey.legs : [];
    const changeMin = legs.filter(l => l.type === 'transfer').reduce((sum, l) => sum + (Number(l.minutes) || 0), 0);
    // wait_min includes the time spent changing buses; the remainder is the wait at the first stop.
    const firstWait = Math.max(0, (Number(journey.wait_min) || 0) - changeMin);
    const out = [];
    let boarded = false;
    for (const leg of legs) {
      const min = Number(leg.minutes) || 0;
      if (leg.type === 'walk') out.push({ kind: 'walk', min, access: !boarded });
      else if (leg.type === 'transfer') out.push({ kind: 'change', min });
      else if (leg.type === 'bus') {
        if (!boarded && firstWait > 0) out.push({ kind: 'wait', min: firstWait });
        boarded = true;
        out.push({ kind: 'bus', min, route: leg.route, stops: Math.max(0, (leg.stops || []).length - 1) });
      }
    }
    return out;
  }

  function stepItems(journey, segs) {
    const busSegs = segs.filter(s => s.kind === 'bus');
    const items = [];
    for (const seg of segs) {
      const m = minutes(seg.min);
      if (seg.kind === 'walk' && seg.access) {
        const stop = bold(journey.board_name || 'the stop');
        items.push(seg.min > 0 ? ['Walk ' + m + ' min to ', stop] : ['Start at ', stop]);
      } else if (seg.kind === 'walk') {
        if (seg.min > 0) items.push(['Walk ' + m + ' min to your destination']);
      } else if (seg.kind === 'wait') {
        items.push(['Wait about ' + m + ' min for the bus']);
      } else if (seg.kind === 'change') {
        items.push(['Change at ', bold(journey.transfer_name || 'the transfer stop'), ', about ' + m + ' min']);
      } else {
        const isLast = busSegs.indexOf(seg) === busSegs.length - 1;
        const dest = isLast ? journey.alight_name : journey.transfer_name;
        const stopCount = seg.stops ? ', ' + seg.stops + (seg.stops === 1 ? ' stop' : ' stops') : '';
        items.push(['Ride ', bold(seg.route || journey.route_name || 'the bus'), ' for ' + m + ' min' + stopCount,
          dest ? ' to ' : '', dest ? bold(dest) : '']);
      }
    }
    return items;
  }

  function journeyCard(journey) {
    const eta = Number(journey.eta_p50_min != null ? journey.eta_p50_min : journey.total_min) || 0;
    const low = Number(journey.eta_p10_min);
    const high = Number(journey.eta_p90_min);
    // Shown only when the band actually brackets the headline time.
    const hasBand = Number.isFinite(low) && Number.isFinite(high)
      && Math.round(low) < Math.round(high) && low <= eta && eta <= high;
    const routes = String(journey.route_name || '').split(/\s+\+\s+/).filter(Boolean);
    const segs = segments(journey);
    const arrive = clockTime(journey.arrive);

    const plates = h('div', { class: 'plates' });
    routes.forEach((route, i) => {
      if (i) plates.append(h('span', { class: 'plate-sep', text: 'then' }));
      plates.append(h('span', { class: 'plate', text: route }));
    });

    const legbar = h('div', { class: 'legbar', 'aria-hidden': 'true' });
    for (const seg of segs) {
      if (!(seg.min > 0)) continue;
      const part = h('span', { class: 'seg seg-' + seg.kind });
      part.style.flexGrow = String(seg.min);
      legbar.append(part);
    }

    const labels = { walk: 'Walk', wait: 'Wait', bus: 'Ride', change: 'Change' };
    const summary = segs.filter(s => s.min > 0).map(s => labels[s.kind] + ' ' + minutes(s.min)).join(' \u00b7 ');

    let rush = null;
    const effect = Number(journey.rush_effect_min);
    if (Number.isFinite(effect) && effect !== 0) {
      const usual = journey.usual_p50_min != null && journey.usual_hour_ist != null
        ? ` Usually ${Math.round(journey.usual_p50_min)} min around ${journey.usual_hour_ist}:00.` : '';
      rush = h('p', {
        class: 'rush ' + (effect > 0 ? 'rush-slower' : 'rush-faster'),
        text: `${effect > 0 ? '+' : '\u2212'}${Math.abs(effect)} min compared with a quiet hour.${usual}`,
      });
    }

    const steps = stepItems(journey, segs);
    const route = h('p', { class: 'card-route' },
      bold(journey.board_name || 'Start'),
      journey.transfer_name ? [' \u2192 change at ', bold(journey.transfer_name)] : '',
      ' \u2192 ', bold(journey.alight_name || 'destination'));

    const card = h('div', { class: 'card', role: 'button', tabindex: '0', 'aria-pressed': 'false' },
      h('div', { class: 'card-top' },
        h('div', {},
          h('p', {}, h('span', { class: 'eta', text: String(Math.round(eta)) }), h('span', { class: 'eta-unit', text: 'min' })),
          hasBand && h('p', { class: 'eta-band', text: `Likely ${Math.round(low)}\u2013${Math.round(high)} min` })),
        arrive && h('p', { class: 'arrive' }, 'Arrive', bold(arrive))),
      routes.length > 0 && plates,
      legbar.childElementCount > 0 && legbar,
      summary && h('p', { class: 'card-sum', text: summary + ' min' }),
      rush,
      route,
      steps.length > 0 && h('ol', { class: 'steps', hidden: true }, steps.map(parts => h('li', {}, parts))));

    const choose = () => selectCard(card, journey);
    card.addEventListener('click', choose);
    card.addEventListener('keydown', e => {
      if (e.key === 'Enter' || e.key === ' ') {
        e.preventDefault();
        choose();
      }
    });
    return card;
  }

  function selectCard(card, journey) {
    for (const other of $('results').querySelectorAll('.card[role="button"]')) {
      const on = other === card;
      other.classList.toggle('sel', on);
      other.setAttribute('aria-pressed', String(on));
      const steps = other.querySelector('.steps');
      const route = other.querySelector('.card-route');
      if (steps) steps.hidden = !on;
      if (steps && route) route.hidden = on;
    }
    draw(journey);
  }

  function modeNote(result) {
    if (result.replay_mode === 'live') return 'Using live bus positions from the last few minutes.';
    const day = String(result.replay_day || '').replace(/_/g, '-');
    return day ? `Replaying the bus speeds recorded on ${dayLabel(day)}.` : '';
  }

  function render(result) {
    const box = $('results');
    box.replaceChildren();
    const options = Array.isArray(result.options) ? result.options : [];

    if (!options.length) {
      const message = sentence(result.note || (result.failed
        ? 'The planner ran into a problem. Please try again'
        : 'No bus route found. Try a start or destination closer to a bus corridor'));
      box.append(h('div', { class: 'card error-card' }, h('p', { class: 'meta', text: message })));
      announce(message);
      return;
    }

    const note = result.replay_mode ? modeNote(result) : '';
    if (note) box.append(h('p', { class: 'mode-note', text: note }));
    box.append(h('h2', { class: 'label', text: options.length === 1 ? '1 journey' : options.length + ' journeys, fastest first' }));
    const cards = options.map(journeyCard);
    box.append(...cards);
    selectCard(cards[0], options[0]);
    announce(`${options.length === 1 ? '1 journey' : options.length + ' journeys'} found. `
      + `The fastest takes about ${Math.round(options[0].eta_p50_min || options[0].total_min || 0)} minutes.`);
    if (isNarrow()) box.scrollIntoView({ block: 'start', behavior: REDUCED_MOTION ? 'auto' : 'smooth' });
  }

  // Live Google traffic-aware driving time, shown beside the fastest bus option.
  function fetchDriveCompare(result, seq) {
    const best = (result.options || [])[0];
    const from = picks.from && picks.from.coord;
    const to = picks.to && picks.to.coord;
    if (!best || !from || !to) return;
    const depart = departISO();
    getJSON(`/gmaps-drive?from=${from[1]},${from[0]}&to=${to[1]},${to[0]}` + (depart ? `&depart=${enc(depart)}` : ''), null)
      .then(drive => {
        if (seq !== planSeq || !drive || !(drive.drive_min > 0)) return;
        const ours = Number(best.total_min) || 0;
        $('results').prepend(h('p', { class: 'gcompare' },
          'Driving takes about ', bold(Math.round(drive.drive_min) + ' min'), ' in traffic',
          drive.drive_km ? ` (${drive.drive_km} km)` : '',
          '. The fastest bus takes ', bold(Math.round(ours) + ' min'),
          `, ${(ours / drive.drive_min).toFixed(1)}\u00d7 the drive. `,
          h('span', { class: 'glab', text: 'Driving time from Google Maps.' })));
      });
  }

  // ---------- controls ----------
  $('planForm').addEventListener('submit', e => {
    e.preventDefault();
    ENDS.forEach(end => closeList(combos[end]));
    plan();
  });

  const replanIfReady = () => {
    if (endpointParam('from') && endpointParam('to')) plan();
  };

  $('when').addEventListener('change', replanIfReady);
  $('asof').addEventListener('change', replanIfReady);
  $('now').addEventListener('click', () => {
    $('when').value = nowLocalISO();
    replanIfReady();
  });

  $('swap').addEventListener('click', () => {
    [picks.from, picks.to] = [picks.to, picks.from];
    const text = $('from').value;
    $('from').value = $('to').value;
    $('to').value = text;
    ENDS.forEach(end => {
      combos[end].seq++;
      closeList(combos[end]);
      placeMarker(end);
    });
    updateHint();
    replanIfReady();
  });

  $('locate').addEventListener('click', () => {
    const button = $('locate');
    const fail = () => {
      button.disabled = false;
      showFormMsg('Could not get your location. Type a stop or pick a point on the map.');
    };
    if (!navigator.geolocation) return fail();
    button.disabled = true;
    navigator.geolocation.getCurrentPosition(pos => {
      button.disabled = false;
      setPoint('from', MY_LOCATION, [pos.coords.longitude, pos.coords.latitude], null, { pan: true, nearby: true });
    }, fail, { enableHighAccuracy: true, timeout: 10000, maximumAge: 60000 });
  });

  function showExamples() {
    $('results').replaceChildren(h('div', { class: 'examples' },
      h('h2', { class: 'label', text: 'Try a trip' }),
      EXAMPLES.map(trip => {
        const button = h('button', { type: 'button', class: 'example' },
          h('span', {}, trip.from[0], ' \u2192 ', trip.to[0]),
          h('span', { class: 'go', text: 'Plan' }));
        button.addEventListener('click', () => {
          ENDS.forEach(clearPick);
          setPoint('from', trip.from[0], [trip.from[1], trip.from[2]], null);
          setPoint('to', trip.to[0], [trip.to[1], trip.to[2]], null);
        });
        return button;
      })));
  }

  function addReplayDays(days) {
    for (const raw of days) {
      const day = String(raw).replace(/_/g, '-');
      if (!/^\d{4}-\d{2}-\d{2}$/.test(day) || replayDays.has(day)) continue;
      replayDays.add(day);
    }
    if (!replayDays.size) return;
    const select = $('asof');
    const current = select.value;
    select.replaceChildren(h('option', { value: '', text: 'Typical day' }),
      [...replayDays].sort().map(day => h('option', { value: day, text: dayLabel(day) })));
    select.value = current;
    $('asofField').hidden = false;
  }

  // ---------- startup ----------
  $('when').value = nowLocalISO();
  showExamples();
  updateHint();

  getJSON('/status', null).then(status => {
    const live = Boolean(status && status.live && status.live.vehicles > 0);
    const net = $('netstats');
    net.textContent = live ? `Live \u00b7 ${status.live.vehicles} buses reporting` : 'Bus traffic model for Delhi';
    net.classList.toggle('is-live', live);
    if (status && status.routes) {
      const stats = $('stats');
      stats.textContent = `${Number(status.routes).toLocaleString('en-IN')} routes \u00b7 839k quantile cells \u00b7 669k trips observed`;
      stats.hidden = false;
    }
    if (status && Array.isArray(status.replay_days)) addReplayDays(status.replay_days);
  });

  // Replay is offered only where recorded days exist (the local API; the edge has none).
  getJSON('/days', null).then(res => {
    if (res && Array.isArray(res.days)) addReplayDays(res.days);
  });
})();
