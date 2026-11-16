(() => {
  'use strict';

  const { API, COLORS, REDUCED_MOTION, h, getJSON, createMap, ramp, colorRamp } = window.DelhiPulse;
  const $ = id => document.getElementById(id);

  const hourInput = $('hour');
  const daySelect = $('day');
  const MIN_HOUR = Number(hourInput.min);
  const MAX_HOUR = Number(hourInput.max);
  const PLAY_INTERVAL_MS = 1100;
  const MAX_CACHED_HOURS = 8;
  const SPEED_COLORS = [[8, COLORS.red], [20, COLORS.amberDeep], [32, COLORS.green]];

  let cityCurve = null;
  let refreshSeq = 0;
  let playing = false;
  let playTimer = null;
  let popup = null;
  let map = null;
  let mapFailed = false;

  const mapReady = new Promise(resolve => {
    createMap('map', { center: [77.21, 28.62], zoom: 10 }).then(ready => {
      if (!ready) {
        mapFailed = true;
        return;
      }
      map = ready;
      window.map = map;
      resolve(map);
    });
  });

  const speedOf = feature => {
    const n = Number(feature.getProperty('speed_kmh'));
    return Number.isFinite(n) ? n : 20;
  };

  // Width repeats the speed encoding, so the map does not rely on color alone.
  function lineWidth(speed, zoom) {
    const far = ramp(speed, [[8, 2.6], [20, 1.4], [32, 0.8]]);
    const near = ramp(speed, [[8, 7], [20, 4.5], [32, 3]]);
    return ramp(zoom, [[9, far], [13, near]]);
  }

  const hourLabel = hour => String(hour).padStart(2, '0') + ':00';
  const nextHour = hour => (hour >= MAX_HOUR ? MIN_HOUR : hour + 1);

  function setStatus(text) {
    $('geoStatus').textContent = text;
  }

  // ---------- map layer ----------
  // No speed-based draw order: where routes share a road, putting the slowest on top
  // would paint the whole corridor with its single worst reading.
  mapReady.then(() => {
    const styleFeature = feature => {
      const speed = speedOf(feature);
      return {
        strokeColor: colorRamp(speed, SPEED_COLORS),
        strokeWeight: lineWidth(speed, map.getZoom()),
        strokeOpacity: feature.getProperty('source') === 'route-avg' ? 0.3 : 0.8,
      };
    };
    map.data.setStyle(styleFeature);
    map.addListener('zoom_changed', () => map.data.setStyle(styleFeature));

    popup = new google.maps.InfoWindow({ maxWidth: 260 });
    map.data.addListener('click', e => {
      const feature = e.feature;
      const speed = Number(feature.getProperty('speed_kmh'));
      const source = feature.getProperty('source');
      popup.setContent(h('div', { class: 'route-pop' },
        h('span', { class: 'plate', text: feature.getProperty('name') || feature.getProperty('route') || 'Route' }),
        Number.isFinite(speed) && h('p', { text: `${speed.toFixed(1)} km/h at ${hourLabel(Number(hourInput.value))}` }),
        source === 'route-avg' && h('p', { class: 'hint', text: 'All-day average. No buses were recorded on this route at this hour.' })));
      popup.setPosition(e.latLng);
      popup.open({ map });
    });
  });

  // ---------- data ----------
  const geoCache = new Map();

  function loadGeo(hour, day) {
    const key = hour + '|' + day;
    if (geoCache.has(key)) return geoCache.get(key);
    const request = fetch(`${API}/geo?hour=${hour}&stride=3&limit=800` + (day ? `&day=${encodeURIComponent(day)}` : ''))
      .then(response => (response.ok ? response.json() : null))
      .then(fc => (fc && Array.isArray(fc.features) ? fc : null))
      .catch(() => null);
    geoCache.set(key, request);
    request.then(fc => {
      if (!fc) geoCache.delete(key);
    });
    if (geoCache.size > MAX_CACHED_HOURS) geoCache.delete(geoCache.keys().next().value);
    return request;
  }

  // The edge serves the curve at /api/pulse (there /pulse is this page); the local API serves it at /pulse.
  async function loadCityCurve() {
    for (const path of ['/api/pulse', '/pulse']) {
      try {
        const response = await fetch(API + path);
        if (!response.ok) continue;
        const data = await response.json();
        if (data && data.city_hour) return data.city_hour;
      } catch (err) {
        continue;
      }
    }
    return null;
  }

  // ---------- readout ----------
  function paintHour(hour) {
    const label = hourLabel(hour);
    $('hh').textContent = label;
    hourInput.setAttribute('aria-valuetext', label);
    for (const bar of $('hourBars').children) {
      bar.setAttribute('aria-pressed', String(Number(bar.dataset.hour) === hour));
    }
    const speed = cityCurve ? Number(cityCurve[hour]) : NaN;
    $('cityReadout').hidden = !Number.isFinite(speed);
    if (Number.isFinite(speed)) $('cityKmh').textContent = speed.toFixed(1);
  }

  function buildCurve() {
    const hours = [];
    for (let hour = MIN_HOUR; hour <= MAX_HOUR; hour++) hours.push(hour);
    const values = hours.map(hour => Number(cityCurve[hour]));
    const peak = Math.max(...values.filter(Number.isFinite));
    if (!(peak > 0)) return;

    $('hourBars').replaceChildren(...hours.map((hour, i) => {
      const value = values[i];
      const known = Number.isFinite(value);
      const bar = h('button', {
        type: 'button', class: 'hour-bar', 'data-hour': hour, 'aria-pressed': 'false',
        'aria-label': known ? `${hourLabel(hour)}, ${value.toFixed(1)} km/h` : `${hourLabel(hour)}, no data`,
      });
      // Zero baseline, so bar heights compare honestly.
      bar.style.height = (known ? Math.max(8, (value / peak) * 100) : 8) + '%';
      bar.addEventListener('click', () => {
        setPlaying(false);
        hourInput.value = String(hour);
        refresh();
      });
      return bar;
    }));
    $('hourTicks').replaceChildren(...hours.map(hour =>
      h('span', { text: (hour - MIN_HOUR) % 3 === 0 ? String(hour).padStart(2, '0') : '' })));
    $('curve').hidden = false;
  }

  function summary(fc, hour) {
    const total = fc.features.length;
    if (!total) return `No route speeds recorded at ${hourLabel(hour)}.`;
    const averaged = fc.features.filter(f => f.properties && f.properties.source === 'route-avg').length;
    const routes = `${total.toLocaleString('en-IN')} routes at ${hourLabel(hour)}.`;
    return averaged
      ? `${routes} ${averaged.toLocaleString('en-IN')} are faded: no buses were recorded on them this hour, so they show their all-day average.`
      : routes;
  }

  async function refresh() {
    const hour = Number(hourInput.value);
    const day = daySelect.value;
    paintHour(hour);

    const seq = ++refreshSeq;
    const slow = setTimeout(() => {
      if (seq === refreshSeq) setStatus('Loading route speeds\u2026');
    }, 300);
    const fc = await loadGeo(hour, day);
    clearTimeout(slow);
    if (seq !== refreshSeq) return;
    if (!fc) return setStatus('Route speeds could not be loaded for this hour. The previous hour is still shown.');
    if (mapFailed) return setStatus(summary(fc, hour));

    await mapReady;
    if (seq !== refreshSeq) return;
    popup.close();
    const previous = [];
    map.data.forEach(feature => previous.push(feature));
    map.data.addGeoJson(fc);
    for (const feature of previous) map.data.remove(feature);
    setStatus(summary(fc, hour));
  }

  // ---------- playback ----------
  async function tick() {
    if (!playing) return;
    hourInput.value = String(nextHour(Number(hourInput.value)));
    await refresh();
    if (!playing) return;
    loadGeo(nextHour(Number(hourInput.value)), daySelect.value);
    playTimer = setTimeout(tick, PLAY_INTERVAL_MS);
  }

  function setPlaying(on) {
    if (playing === on) return;
    playing = on;
    clearTimeout(playTimer);
    const button = $('play');
    button.textContent = on ? 'Pause' : 'Play the day';
    button.setAttribute('aria-pressed', String(on));
    if (on) playTimer = setTimeout(tick, REDUCED_MOTION ? PLAY_INTERVAL_MS * 2 : PLAY_INTERVAL_MS);
  }

  // ---------- controls ----------
  let inputTimer = null;
  hourInput.addEventListener('input', () => {
    setPlaying(false);
    paintHour(Number(hourInput.value));
    clearTimeout(inputTimer);
    inputTimer = setTimeout(refresh, 120);
  });
  daySelect.addEventListener('change', refresh);
  $('play').addEventListener('click', () => setPlaying(!playing));
  document.addEventListener('visibilitychange', () => {
    if (document.hidden) setPlaying(false);
  });

  // ---------- startup ----------
  refresh();

  loadCityCurve().then(curve => {
    if (!curve) return;
    cityCurve = curve;
    buildCurve();
    paintHour(Number(hourInput.value));
  });

  getJSON('/days', null).then(res => {
    const days = res && Array.isArray(res.days) ? res.days : [];
    if (!days.length) return;
    for (const day of days) {
      daySelect.append(h('option', { value: day, text: 'Replay ' + String(day).replace(/_/g, '-') }));
    }
    $('dayField').hidden = false;
  });
})();
