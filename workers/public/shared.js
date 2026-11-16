window.DelhiPulse = (() => {
  'use strict';

  const API = location.origin;
  const MAP_STYLE = 'https://basemaps.cartocdn.com/gl/positron-gl-style/style.json';
  const COLORS = {
    ink: '#14211b',
    green: '#0a6b47',
    amber: '#ffb000',
    amberDeep: '#d98e00',
    red: '#c2401c',
    white: '#ffffff',
  };
  const REDUCED_MOTION = matchMedia('(prefers-reduced-motion: reduce)').matches;

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

  function createMap(containerId, view) {
    const container = document.getElementById(containerId);
    try {
      const map = new maplibregl.Map({
        container,
        style: MAP_STYLE,
        center: view.center,
        zoom: view.zoom,
        attributionControl: false,
      });
      map.addControl(new maplibregl.NavigationControl({ showCompass: false }), 'top-right');
      map.addControl(new maplibregl.AttributionControl({ compact: true }), 'bottom-right');
      return map;
    } catch (err) {
      container.replaceChildren(
        h('p', { class: 'map-fallback', text: 'The map could not start in this browser. Everything else still works.' }),
      );
      return null;
    }
  }

  // Id of the first layer in the style's trailing block of labels, so data draws beneath place names.
  function labelLayerId(map) {
    const layers = map.getStyle().layers;
    let i = layers.length;
    while (i > 0 && layers[i - 1].type === 'symbol') i--;
    return layers[i] ? layers[i].id : undefined;
  }

  return { API, COLORS, REDUCED_MOTION, h, getJSON, createMap, labelLayerId };
})();
