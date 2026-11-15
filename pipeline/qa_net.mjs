export default async function run(page) {
  await page.waitForTimeout(2500);
  return await page.evaluate(() => ({
    scripts: [...document.scripts].map(s => s.src || 'inline'),
    maplibregl: typeof window.maplibregl,
    mapObj: window.map ? window.map.constructor.name : null,
    bad: performance.getEntriesByType('resource')
      .filter(e => e.responseStatus >= 400 || e.name.includes('vendor'))
      .map(e => ({ url: e.name.slice(0, 90), st: e.responseStatus })),
    firstInlineHasPicks: document.body.innerHTML.includes('const API'),
    navControls: document.querySelectorAll('.maplibregl-ctrl').length,
  }));
}
