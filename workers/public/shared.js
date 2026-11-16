window.DelhiPulse = (() => {
  'use strict';

  const API = location.origin;
  const COLORS = {
    ink: '#14211b',
    green: '#0a6b47',
    amber: '#ffb000',
    amberDeep: '#d98e00',
    red: '#c2401c',
    white: '#ffffff',
  };
  const REDUCED_MOTION = matchMedia('(prefers-reduced-motion: reduce)').matches;
  const DELHI_BOUNDS = { south: 28.15, west: 76.6, north: 29.0, east: 77.65 };

  // Builds DOM with textContent only, so API and user strings are never parsed as HTML.
  function h(tag, attrs, ...children) {
    const node = document.createElement(tag);
    for (const [key, value] of Object.entries(attrs || {})) {
      if (value == null || value === false) continue;
      if (key === 'class') node.className = value;
      else if (key === 'text') node.textContent = value;
      else node.setAttribute(key, value === true ? '' : String(value));
    }
    for (const child of children.flat(Infinity)) {
      if (child == null || child === false || child === '') continue;
      node.append(child.nodeType ? child : document.createTextNode(String(child)));
    }
    return node;
  }

  function getJSON(path, fallback) {
    return fetch(API + path)
      .then(response => response.json())
      .catch(() => fallback);
  }

  // The edge serves the config at /api/maps-config; the local API at /maps-config.
  async function mapsConfig() {
    for (const path of ['/api/maps-config', '/maps-config']) {
      try {
        const response = await fetch(API + path);
        if (!response.ok) continue;
        const data = await response.json();
        if (data && data.key) return data;
      } catch (err) {
        continue;
      }
    }
    throw new Error('maps config unavailable');
  }

  let googleReady = null;
  function loadGoogleMaps(key) {
    if (googleReady) return googleReady;
    googleReady = new Promise((resolve, reject) => {
      const callback = '__delhiPulseMaps';
      window[callback] = () => {
        delete window[callback];
        resolve(window.google.maps);
      };
      // Google calls this on key or billing errors after the script has loaded.
      window.gm_authFailure = () => reject(new Error('google maps auth failed'));
      const params = new URLSearchParams({
        key, v: 'weekly', libraries: 'marker', loading: 'async',
        callback, region: 'IN', language: 'en',
      });
      const script = h('script', { src: 'https://maps.googleapis.com/maps/api/js?' + params, async: true });
      script.onerror = () => reject(new Error('google maps script failed'));
      document.head.append(script);
    });
    return googleReady;
  }

  function mapFailed(container) {
    container.replaceChildren(
      h('p', { class: 'map-fallback', text: 'The map could not load. Everything else still works.' }),
    );
    return null;
  }

  // Resolves to a google.maps.Map once it has rendered, or null when the map cannot start.
  async function createMap(containerId, view) {
    const container = document.getElementById(containerId);
    try {
      const config = await mapsConfig();
      const maps = await loadGoogleMaps(config.key);
      const map = new maps.Map(container, {
        center: { lat: view.center[1], lng: view.center[0] },
        zoom: view.zoom,
        mapId: config.mapId,
        colorScheme: 'LIGHT',
        clickableIcons: false,
        gestureHandling: 'greedy',
        disableDefaultUI: true,
        zoomControl: true,
        zoomControlOptions: { position: maps.ControlPosition.RIGHT_TOP },
        restriction: { latLngBounds: DELHI_BOUNDS, strictBounds: false },
      });
      await new Promise((resolve, reject) => {
        window.gm_authFailure = () => {
          mapFailed(container);
          reject(new Error('google maps auth failed'));
        };
        maps.event.addListenerOnce(map, 'tilesloaded', resolve);
      });
      return map;
    } catch (err) {
      return mapFailed(container);
    }
  }

  // Linear interpolation through [stop, value] pairs, clamped at both ends.
  function ramp(value, stops) {
    if (value <= stops[0][0]) return stops[0][1];
    for (let i = 1; i < stops.length; i++) {
      const [x1, y1] = stops[i];
      if (value <= x1) {
        const [x0, y0] = stops[i - 1];
        return y0 + ((value - x0) / (x1 - x0)) * (y1 - y0);
      }
    }
    return stops[stops.length - 1][1];
  }

  function mixColor(a, b, t) {
    const pa = parseInt(a.slice(1), 16), pb = parseInt(b.slice(1), 16);
    const ch = shift => Math.round(((pa >> shift) & 255) + ((((pb >> shift) & 255) - ((pa >> shift) & 255)) * t));
    return '#' + ((ch(16) << 16) | (ch(8) << 8) | ch(0)).toString(16).padStart(6, '0');
  }

  // Interpolates a color through [stop, '#rrggbb'] pairs.
  function colorRamp(value, stops) {
    if (value <= stops[0][0]) return stops[0][1];
    for (let i = 1; i < stops.length; i++) {
      if (value <= stops[i][0]) {
        const t = (value - stops[i - 1][0]) / (stops[i][0] - stops[i - 1][0]);
        return mixColor(stops[i - 1][1], stops[i][1], t);
      }
    }
    return stops[stops.length - 1][1];
  }

  return { API, COLORS, REDUCED_MOTION, h, getJSON, createMap, ramp, colorRamp };
})();
