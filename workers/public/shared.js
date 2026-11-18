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
    line: '#d6ddd5',
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

  function getJSON(path, fallback, options) {
    return fetch(API + path, options)
      .then(response => response.json())
      .catch(() => fallback);
  }

  // The edge serves the config at /api/maps-config; the local API at /maps-config.
  let configReady = null;
  async function fetchMapsConfig() {
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

  // Cached so every map on a page shares one request; a failure is not cached.
  function mapsConfig() {
    if (!configReady) {
      configReady = fetchMapsConfig();
      configReady.catch(() => { configReady = null; });
    }
    return configReady;
  }

  let googleReady = null;
  const authListeners = new Set();
  function loadGoogleMaps(key) {
    if (googleReady) return googleReady;
    googleReady = new Promise((resolve, reject) => {
      const callback = '__delhiPulseMaps';
      window[callback] = () => {
        delete window[callback];
        resolve(window.google.maps);
      };
      // Google calls this on key or billing errors after the script has loaded, and it can
      // arrive after a map has already rendered, so every live map hears about it.
      window.gm_authFailure = () => {
        reject(new Error('google maps auth failed'));
        for (const listener of authListeners) listener();
      };
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

  // Clears whatever Google left behind; the page then draws a schematic with sketchMap.
  function mapFailed(container) {
    container.replaceChildren();
    return null;
  }

  // A basemap-free drawing of routes and stops on a canvas, fitted to its content.
  // Layers are lines { path, color, width, dash, casing, opacity, context } or
  // dots { at, r, fill, stroke, strokeWidth, text, textColor }; coordinates are [lng, lat].
  function sketchMap(container, options = {}) {
    const MIN_SPAN_DEG = 0.006;
    const CAPTION_INSET_PX = 48;
    const canvas = h('canvas', {
      class: 'sketch', role: 'img', 'aria-label': options.label || 'Schematic map of bus routes',
    });
    container.replaceChildren(canvas);
    if (options.caption) container.append(h('p', { class: 'sketch-note', text: options.caption }));
    const ctx = canvas.getContext('2d');
    let layers = [];
    let focus = null;

    function extent() {
      let points = focus;
      if (!points || !points.length) {
        const own = layers.filter(layer => !layer.context);
        points = (own.length ? own : layers).flatMap(layer => layer.path || [layer.at]);
      }
      if (!points.length) return null;
      let west = Infinity, east = -Infinity, south = Infinity, north = -Infinity;
      for (const [lng, lat] of points) {
        if (lng < west) west = lng;
        if (lng > east) east = lng;
        if (lat < south) south = lat;
        if (lat > north) north = lat;
      }
      return { west, east, south, north };
    }

    function render() {
      const width = container.clientWidth, height = container.clientHeight;
      if (!width || !height) return;
      const dpr = Math.min(window.devicePixelRatio || 1, 2);
      canvas.width = Math.round(width * dpr);
      canvas.height = Math.round(height * dpr);
      ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
      const box = extent();
      if (!box) return;

      // Longitude is squeezed by cos(latitude) so shapes are not stretched east-west.
      const squeeze = Math.cos(((box.south + box.north) / 2) * Math.PI / 180);
      const spanX = Math.max((box.east - box.west) * squeeze, MIN_SPAN_DEG);
      const spanY = Math.max(box.north - box.south, MIN_SPAN_DEG);
      const pad = Math.max(24, Math.min(64, Math.min(width, height) / 7));
      // The caption sits along the bottom edge; the drawing is fitted to the space above it.
      const usable = height - (options.caption ? CAPTION_INSET_PX : 0);
      const scale = Math.min((width - 2 * pad) / spanX, (usable - 2 * pad) / spanY);
      const midLng = (box.west + box.east) / 2, midLat = (box.south + box.north) / 2;
      const project = ([lng, lat]) =>
        [width / 2 + (lng - midLng) * squeeze * scale, usable / 2 - (lat - midLat) * scale];

      ctx.lineJoin = 'round';
      ctx.lineCap = 'round';
      for (const layer of layers) {
        ctx.globalAlpha = layer.opacity == null ? 1 : layer.opacity;
        if (layer.path) {
          const stroke = (color, lineWidth, dash) => {
            ctx.beginPath();
            layer.path.forEach((point, i) => {
              const [x, y] = project(point);
              if (i) ctx.lineTo(x, y);
              else ctx.moveTo(x, y);
            });
            ctx.setLineDash(dash || []);
            ctx.strokeStyle = color;
            ctx.lineWidth = lineWidth;
            ctx.stroke();
          };
          if (layer.casing) stroke(layer.casing, layer.width + 4);
          stroke(layer.color, layer.width, layer.dash);
        } else if (layer.at) {
          const [x, y] = project(layer.at);
          ctx.setLineDash([]);
          ctx.beginPath();
          ctx.arc(x, y, layer.r, 0, Math.PI * 2);
          ctx.fillStyle = layer.fill;
          ctx.fill();
          if (layer.stroke) {
            ctx.strokeStyle = layer.stroke;
            ctx.lineWidth = layer.strokeWidth || 2;
            ctx.stroke();
          }
          if (layer.text) {
            ctx.fillStyle = layer.textColor || COLORS.white;
            ctx.font = '700 16px "Barlow Condensed", sans-serif';
            ctx.textAlign = 'center';
            ctx.textBaseline = 'middle';
            ctx.fillText(layer.text, x, y + 1);
          }
        }
      }
      ctx.globalAlpha = 1;
    }

    new ResizeObserver(render).observe(container);
    if (document.fonts && document.fonts.ready) document.fonts.ready.then(render);

    return {
      set(nextLayers, nextFocus) {
        layers = nextLayers || [];
        focus = nextFocus || null;
        render();
      },
    };
  }

  // Resolves to a google.maps.Map once it has rendered, or null when the map cannot start.
  function createMap(containerId, view) {
    return createMapIn(document.getElementById(containerId), view);
  }

  // onLost runs if Google rejects the key after the map has already been returned.
  async function createMapIn(container, view, onLost) {
    let finished = false;
    let lost;
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
        lost = () => {
          authListeners.delete(lost);
          mapFailed(container);
          if (finished) {
            if (onLost) onLost();
          } else {
            reject(new Error('google maps auth failed'));
          }
        };
        authListeners.add(lost);
        maps.event.addListenerOnce(map, 'tilesloaded', resolve);
      });
      finished = true;
      return map;
    } catch (err) {
      if (lost) authListeners.delete(lost);
      return mapFailed(container);
    }
  }

  // Draws the same layers as sketchMap, but over the Google street map. When Google cannot
  // start (no key, blocked referrer, offline, hidden box that never loads) it becomes a sketchMap.
  function layerMap(container, options = {}) {
    const START_TIMEOUT_MS = 10000;
    const MAX_ZOOM = 16;
    let layers = [], focus = null;
    let map = null, sketch = null, overlays = [];

    function useSketch() {
      if (sketch || map) return;
      container.replaceChildren();
      sketch = sketchMap(container, options);
      sketch.set(layers, focus);
    }

    function draw() {
      const maps = window.google.maps;
      for (const overlay of overlays) overlay.setMap(null);
      overlays = [];
      const toLatLng = ([lng, lat]) => ({ lat, lng });
      const symbol = (r, fill, stroke, strokeWeight) => ({
        path: maps.SymbolPath.CIRCLE, scale: r, fillColor: fill, fillOpacity: 1,
        strokeColor: stroke || fill, strokeOpacity: 1, strokeWeight: strokeWeight || 0,
      });
      const own = layers.filter(layer => !layer.context);

      for (const layer of layers) {
        const opacity = layer.opacity == null ? 1 : layer.opacity;
        if (layer.path) {
          const path = layer.path.map(toLatLng);
          if (layer.casing) {
            overlays.push(new maps.Polyline({
              map, path, clickable: false, zIndex: 1,
              strokeColor: layer.casing, strokeWeight: layer.width + 4, strokeOpacity: opacity,
            }));
          }
          overlays.push(new maps.Polyline(layer.dash
            ? {
              map, path, clickable: false, zIndex: 2, strokeOpacity: 0,
              icons: [{ icon: { ...symbol(Math.max(1.5, layer.width / 2), layer.color), fillOpacity: opacity }, offset: '0', repeat: '9px' }],
            }
            : {
              map, path, clickable: false, zIndex: 2,
              strokeColor: layer.color, strokeWeight: layer.width, strokeOpacity: opacity,
            }));
        } else if (layer.at) {
          overlays.push(new maps.Marker({
            map, position: toLatLng(layer.at), clickable: false, zIndex: 3,
            icon: symbol(layer.r, layer.fill, layer.stroke, layer.stroke ? layer.strokeWidth || 2 : 0),
            label: layer.text
              ? { text: layer.text, color: layer.textColor || COLORS.white, fontWeight: '700', fontSize: '13px' }
              : undefined,
          }));
        }
      }

      const points = focus && focus.length
        ? focus
        : (own.length ? own : layers).flatMap(layer => layer.path || [layer.at]);
      if (!points.length) return;
      const bounds = new maps.LatLngBounds();
      for (const point of points) bounds.extend(toLatLng(point));
      const padding = Math.max(24, Math.min(64, Math.round(Math.min(container.clientWidth, container.clientHeight) / 6)));
      maps.event.addListenerOnce(map, 'idle', () => {
        if (map.getZoom() > MAX_ZOOM) map.setZoom(MAX_ZOOM);
      });
      map.fitBounds(bounds, padding);
    }

    const timer = setTimeout(useSketch, START_TIMEOUT_MS);
    const lostLater = () => {
      map = null;
      useSketch();
    };
    createMapIn(container, { center: [77.21, 28.62], zoom: 10 }, lostLater).then(ready => {
      clearTimeout(timer);
      if (sketch) return;
      if (!ready) return useSketch();
      map = ready;
      drawOrSketch();
    });

    // A drawing error must not leave a blank box, so it degrades to the schematic.
    function drawOrSketch() {
      try {
        draw();
      } catch (err) {
        map = null;
        useSketch();
      }
    }

    return {
      set(nextLayers, nextFocus) {
        layers = nextLayers || [];
        focus = nextFocus || null;
        if (sketch) sketch.set(layers, focus);
        else if (map) drawOrSketch();
      },
    };
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

  return { API, COLORS, REDUCED_MOTION, h, getJSON, createMap, sketchMap, layerMap, ramp, colorRamp };
})();
