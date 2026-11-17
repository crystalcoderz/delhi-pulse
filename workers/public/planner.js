(() => {
  'use strict';

  const { API, COLORS, h, getJSON, createMap, sketchMap, REDUCED_MOTION } = window.DelhiPulse;
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
  let lastQuery = '';

  let map = null;
  // Stands in for the street map when Google Maps cannot start (no key, blocked referrer, offline).
  let sketch = null;
  let network = [];
  // Resolves only once a working map exists, so map work queued on it never runs against null.
  const mapReady = new Promise(resolve => {
    createMap('map', { center: [77.21, 28.62], zoom: 10.5 }).then(ready => {
      if (!ready) return startSketch();
      map = ready;
      window.map = map;
      resolve(map);
    });
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
    return Number.isNaN(date.getTime())
      ? '' : date.toLocaleTimeString('en-IN',
          { hour: '2-digit', minute: '2-digit', timeZone: 'Asia/Kolkata' }) + ' IST';
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
    const parts = new Intl.DateTimeFormat('en-CA', {
      timeZone: 'Asia/Kolkata', year: 'numeric', month: '2-digit',
      day: '2-digit', hour: '2-digit', minute: '2-digit', hour12: false,
    }).formatToParts(new Date()).reduce((a, p) => ((a[p.type] = p.value), a), {});
    if (parts.hour === '24') parts.hour = '00';
    return `${parts.year}-${parts.month}-${parts.day}T${parts.hour}:${parts.minute}`;
  }

  function departISO() {
    // The datetime field is Delhi wall-clock; keep it in IST end-to-end so
    // a judge in any timezone queries the hour they typed.
    const v = $('when').value;
    return !v ? '' : v + ':00+05:30';
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
  // Journey shapes are plain overlays: walk dots, a casing under the bus line, and stop circles.
  let overlays = [];
  const MINOR_STOP_ZOOM = 12;

  const toLatLng = ([lng, lat]) => ({ lat, lng });

  function clearOverlays() {
    for (const overlay of overlays) overlay.setMap(null);
    overlays = [];
  }

  function addPolyline(path, options) {
    const line = new google.maps.Polyline({ map, path: path.map(toLatLng), clickable: false, ...options });
    overlays.push(line);
  }

  function addStop(pt, kind) {
    const major = kind !== 'stop';
    const marker = new google.maps.Marker({
      map,
      position: toLatLng(pt),
      clickable: false,
      zIndex: major ? 3 : 2,
      visible: major || map.getZoom() >= MINOR_STOP_ZOOM,
      icon: {
        path: google.maps.SymbolPath.CIRCLE,
        scale: major ? 6 : 3,
        fillColor: kind === 'transfer' ? COLORS.amber : COLORS.white,
        fillOpacity: 1,
        strokeColor: major ? COLORS.ink : COLORS.green,
        strokeWeight: major ? 2.5 : 1.5,
      },
    });
    marker.major = major;
    overlays.push(marker);
  }

  mapReady.then(() => {
    map.addListener('zoom_changed', () => {
      const showMinor = map.getZoom() >= MINOR_STOP_ZOOM;
      for (const overlay of overlays) {
        if (overlay.major === false) overlay.setVisible(showMinor);
      }
    });
  });

  function fitTo(extent) {
    const bounds = new google.maps.LatLngBounds();
    for (const pt of extent) bounds.extend(toLatLng(pt));
    const box = map.getDiv();
    const padding = Math.max(24, Math.min(64, Math.round(Math.min(box.clientWidth, box.clientHeight) / 6)));
    google.maps.event.addListenerOnce(map, 'idle', () => {
      if (map.getZoom() > 15) map.setZoom(15);
    });
    map.fitBounds(bounds, padding);
  }

  // Walk paths, bus paths and classified stops of a journey, as [lng, lat] points.
  function journeyShapes(journey) {
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
        walk.push(pts);
        continue;
      }
      bus.push(pts);
      const n = busLegs.indexOf(leg);
      pts.forEach((pt, i) => {
        let kind = 'stop';
        if (i === 0) kind = n === 0 ? 'board' : 'transfer';
        else if (i === pts.length - 1) kind = n === busLegs.length - 1 ? 'alight' : 'transfer';
        stops.push([pt, kind]);
      });
    }
    return { walk, bus, stops, extent };
  }

  function startSketch() {
    const box = $('map');
    box.parentElement.classList.add('is-sketch');
    sketch = sketchMap(box, {
      label: 'Schematic of the Delhi bus network showing your start, destination and the selected journey',
      caption: 'Street map unavailable here. Journeys are drawn over the bus network.',
    });
    drawSketch();
    getJSON('/geo?hour=9&stride=4&limit=400', null).then(fc => {
      if (!fc || !Array.isArray(fc.features)) return;
      network = fc.features
        .map(feature => feature.geometry && feature.geometry.coordinates)
        .filter(Array.isArray);
      drawSketch();
    });
  }

  function drawSketch() {
    const { walk, bus, stops, extent } = journeyShapes(shownJourney);
    const layers = network.map(path => ({ path, color: COLORS.line, width: 1, context: true }));
    for (const path of walk) layers.push({ path, color: COLORS.ink, width: 2.5, dash: [0.5, 7] });
    for (const path of bus) layers.push({ path, color: COLORS.green, width: 5, casing: COLORS.white });
    for (const [at, kind] of stops) {
      layers.push(kind === 'stop'
        ? { at, r: 2.5, fill: COLORS.white, stroke: COLORS.green, strokeWidth: 1.5 }
        : { at, r: 6, fill: kind === 'transfer' ? COLORS.amber : COLORS.white, stroke: COLORS.ink, strokeWidth: 2.5 });
    }
    const focus = [...extent];
    for (const end of ENDS) {
      if (!picks[end]) continue;
      focus.push(picks[end].coord);
      layers.push({
        at: picks[end].coord, r: 13, stroke: COLORS.white, strokeWidth: 2,
        fill: end === 'from' ? COLORS.green : COLORS.ink,
        text: end === 'from' ? 'A' : 'B',
        textColor: end === 'from' ? COLORS.white : COLORS.amber,
      });
    }
    sketch.set(layers, focus);
  }

  function draw(journey) {
    shownJourney = journey;
    if (sketch) return drawSketch();
    mapReady.then(() => {
      if (shownJourney !== journey) return;
      const { walk, bus, stops, extent } = journeyShapes(journey);

      clearOverlays();
      const dot = {
        path: google.maps.SymbolPath.CIRCLE, scale: 2,
        fillColor: COLORS.ink, fillOpacity: 1, strokeOpacity: 0,
      };
      for (const path of walk) {
        addPolyline(path, { strokeOpacity: 0, zIndex: 1, icons: [{ icon: dot, offset: '0', repeat: '9px' }] });
      }
      for (const path of bus) addPolyline(path, { strokeColor: COLORS.white, strokeWeight: 9, strokeOpacity: 1, zIndex: 1 });
      for (const path of bus) addPolyline(path, { strokeColor: COLORS.green, strokeWeight: 5, strokeOpacity: 1, zIndex: 2 });
      for (const [pt, kind] of stops) addStop(pt, kind);

      if (!journey) return;
      for (const end of ENDS) if (picks[end]) extent.push(picks[end].coord);
      if (extent.length < 2) return;
      fitTo(extent);
    });
  }

  // ---------- endpoints: picks, markers, hints ----------
  const coordLabel = (lng, lat) => lat.toFixed(4) + ', ' + lng.toFixed(4);
  const isEmpty = end => !picks[end] && !$(end).value.trim();

  function placeMarker(end) {
    if (sketch) return drawSketch();
    mapReady.then(() => {
      const pick = picks[end];
      if (!pick) {
        if (markers[end]) markers[end].map = null;
        return;
      }
      if (!markers[end]) {
        const pin = h('div', { class: 'pin pin-' + end, text: end === 'from' ? 'A' : 'B' });
        const marker = new google.maps.marker.AdvancedMarkerElement({
          content: pin,
          gmpDraggable: true,
          zIndex: 10,
          title: end === 'from' ? 'Start (A). Drag to move.' : 'Destination (B). Drag to move.',
        });
        marker.addListener('dragend', () => {
          const p = new google.maps.LatLng(marker.position);
          setPoint(end, coordLabel(p.lng(), p.lat()), [p.lng(), p.lat()], null, { nearby: true });
        });
        markers[end] = marker;
      }
      markers[end].position = toLatLng(pick.coord);
      markers[end].map = map;
    });
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
      if (map.getZoom() < 13) map.setZoom(13);
      map.panTo(toLatLng(coord));
    }
  }

  mapReady.then(() => {
    updateHint();
    map.addListener('click', e => {
      if (!e.latLng) return;
      const end = isEmpty('from') ? 'from' : isEmpty('to') ? 'to' : null;
      if (!end) return;
      const lng = e.latLng.lng(), lat = e.latLng.lat();
      setPoint(end, coordLabel(lng, lat), [lng, lat], null, { nearby: true });
    });
  });

  // ---------- search: DTC stops + Google Places via the edge proxy ----------
  // One Places session token per typing session: autocomplete + resolve share
  // it, and hiding the page starts a fresh session instead of resurrecting an
  // old billed one. Guarded, because randomUUID needs a secure context.
  const newToken = () => (typeof crypto.randomUUID === 'function' ? crypto.randomUUID() : '');
  let sessionToken = newToken();
  const sessionHeaders = () => (sessionToken ? { headers: { 'X-Session-Token': sessionToken } } : {});
  document.addEventListener('visibilitychange', () => {
    if (document.hidden) sessionToken = newToken();
  });

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
              const found = await fetch(`${API}/places/resolve?place_id=${enc(place.id)}`, sessionHeaders()).then(r => r.json());
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
      getJSON(`/places/autocomplete?input=${enc(query)}`, null, sessionHeaders()).then(res => {
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
    const query = `from=${enc(fromVal)}&to=${enc(toVal)}`
      + (depart ? `&depart=${enc(depart)}` : '')
      + (asOf ? `&asOf=${enc(asOf)}` : '')
      + '&max=4';

    let result;
    try {
      const response = await fetch(`${API}/journeys?${query}`);
      result = await response.json().catch(() => ({}));
      if (!response.ok && !Array.isArray(result.options)) {
        result = {
          options: [],
          note: [result.detail, result.error, result.hint]
            .filter(x => typeof x === 'string' && x).join(' '),
          failed: true,
        };
      }
    } catch (err) {
      result = { options: [], note: 'Could not reach the planner. Check your connection and try again.' };
    }

    // A newer search has started; it owns the button and the results now.
    if (seq !== planSeq) return;
    setBusy(false);
    lastQuery = query;
    try {
      render(result);
    } catch (err) {
      console.error('render failed:', err);
    }
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
        out.push({
          kind: 'bus', min, route: leg.route, stops: Math.max(0, (leg.stops || []).length - 1),
          source: leg.source, n: leg.n,
        });
      }
    }
    return out;
  }

  // What the ride-time estimate for a bus leg was based on, from the leg's source and n.
  function evidence(seg) {
    const n = Number(seg.n);
    const count = Number.isFinite(n) && n > 0 ? n.toLocaleString('en-IN') : '';
    if (seg.source === 'route-hour') {
      return count
        ? `Speed from ${count} recordings of this route at this hour.`
        : 'Speed recorded on this route at this hour.';
    }
    if (seg.source === 'route-avg') {
      return 'No recordings at this hour, so this uses the route\u2019s all-day average'
        + (count ? ` (${count} recordings).` : '.');
    }
    if (seg.source === 'city') return 'No recordings for this route, so this uses the citywide average for this hour.';
    return '';
  }

  function stepItems(journey, segs) {
    const busSegs = segs.filter(s => s.kind === 'bus');
    const items = [];
    for (const seg of segs) {
      const m = minutes(seg.min);
      if (seg.kind === 'walk' && seg.access && journey.route_name === 'Walk') {
        items.push(['Walk the whole way to your destination']);
      } else if (seg.kind === 'walk' && seg.access) {
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
        const basis = evidence(seg);
        items.push(['Ride ', bold(seg.route || journey.route_name || 'the bus'), ' for ' + m + ' min' + stopCount,
          dest ? ' to ' : '', dest ? bold(dest) : '',
          basis && h('span', { class: 'evidence', text: basis })]);
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
    const keys = segs.filter(s => s.min > 0).map(s =>
      h('span', { class: 'key key-' + s.kind, text: `${labels[s.kind]} ${minutes(s.min)} min` }));

    let rush = null;
    const effect = Number(journey.rush_effect_min);
    if (Number.isFinite(effect) && Math.abs(effect) >= 1) {
      const uh = Number(journey.usual_hour_ist);
      const uhLabel = Number.isFinite(uh) && uh >= 0 && uh <= 23
        ? `${String(uh).padStart(2, '0')}:00 IST` : 'the usual hour';
      const usual = journey.usual_p50_min != null && journey.usual_hour_ist != null
        ? ` Usually about ${Math.round(journey.usual_p50_min)} min at ${uhLabel}.` : '';
      const word = effect > 0 ? 'slower' : 'faster';
      rush = h('p', {
        class: 'rush ' + (effect > 0 ? 'rush-slower' : 'rush-faster'),
        text: `${Math.round(Math.abs(effect))} min ${word} than the usual trip.${usual}`,
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
      keys.length > 0 && h('p', { class: 'keys' }, keys),
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

  const apiLink = () => lastQuery && h('a', {
    class: 'api-link', href: 'playground.html#journeys?' + lastQuery,
    text: 'See this request in the API playground',
  });

  function render(result) {
    const box = $('results');
    box.replaceChildren();
    const options = Array.isArray(result.options) ? result.options : [];

    if (!options.length) {
      const message = sentence(result.note || (result.failed
        ? 'The planner ran into a problem. Please try again'
        : 'No bus route found. Try a start or destination closer to a bus corridor'));
      box.append(h('div', { class: 'card error-card' }, h('p', { class: 'meta', text: message })), apiLink());
      announce(message);
      return;
    }

    const note = result.replay_mode ? modeNote(result) : '';
    if (note) box.append(h('p', { class: 'mode-note', text: note }));
    box.append(h('h2', { class: 'label', text: options.length === 1 ? '1 journey' : options.length + ' journeys, fastest first' }));
    const cards = options.map(journeyCard);
    box.append(...cards, apiLink());
    selectCard(cards[0], options[0]);
    announce(`${options.length === 1 ? '1 journey' : options.length + ' journeys'} found. `
      + `The fastest takes about ${Math.round(options[0].eta_p50_min || options[0].total_min || 0)} minutes.`);
    if (isNarrow()) box.scrollIntoView({ block: 'start', behavior: REDUCED_MOTION ? 'auto' : 'smooth' });
  }

  // Live Google traffic-aware driving time, shown beside the fastest bus option.
  function fetchDriveCompare(result, seq) {
    const best = (result.options || [])[0];
    if (!best) return;
    let from = picks.from && picks.from.coord;
    let to = picks.to && picks.to.coord;
    if (!from || !to) {
      // typed endpoints (no map coords): derive them from the best journey's
      // own leg geometry so the strip still shows
      const legs = Array.isArray(best.legs) ? best.legs : [];
      const first = legs[0] || {};
      const last = legs[legs.length - 1] || {};
      const c0 = (first.coords || [])[0];
      const cN = ((last.coords || [])[1]) || (last.coords || [])[0];
      if (c0) from = [c0[1], c0[0]];
      if (cN) to = [cN[1], cN[0]];
    }
    if (!from || !to) return;
    const depart = departISO();
    // Optimistic chip while Google is asked: it yields to the real comparison
    // when the answer lands, and disappears if it never does.
    const waiting = h('p', { class: 'gcompare', text: 'Asking Google for the drive time\u2026' });
    $('results').prepend(waiting);
    setTimeout(() => waiting.remove(), 10000);
    getJSON(`/gmaps-drive?from=${from[1]},${from[0]}&to=${to[1]},${to[0]}` + (depart ? `&depart=${enc(depart)}` : ''), null)
      .then(drive => {
        waiting.remove();
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
      const edges = status.edges ? `${Math.round(status.edges / 1000)}k` : null;
      stats.textContent = `${Number(status.routes).toLocaleString('en-IN')} routes`
        + (edges ? ` \u00b7 ${edges} quantile cells` : '')
        + ' \u00b7 October measured speeds';
      stats.hidden = false;
    }
    if (status && Array.isArray(status.replay_days)) addReplayDays(status.replay_days);
  });

  // Replay days come from /status when the platform supports them; the days
  // endpoint exists only on the local API, and probing it here logged a 404
  // on every load.
})();
