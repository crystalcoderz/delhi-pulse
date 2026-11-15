export default async function run(page) {
  await page.waitForTimeout(4000);
  const out = {
    canvas: await page.evaluate(() => document.querySelectorAll('.maplibregl-canvas').length),
    title: await page.evaluate(() => document.title),
    hourLabel: await page.evaluate(() => document.getElementById('hh')?.textContent),
    geoFeatures: await page.evaluate(async () => (await (await fetch('/api/geo?hour=21')).json()).features.length),
    nightSpeed: await page.evaluate(async () => {
      const g = await (await fetch('/api/geo?hour=21')).json();
      return g.features[0].properties.speed_kmh;
    }),
  };
  // slider interaction
  await page.evaluate(() => {
    const h = document.getElementById('hour');
    h.value = 21; h.dispatchEvent(new Event('input'));
  });
  await page.waitForTimeout(1500);
  out.afterSlider = await page.evaluate(() => document.getElementById('hh').textContent);
  await page.screenshot({ path: 'C:/Users/praya/delhi-pulse/docs/qa_edge_live.png' });
  out.screenshot = 'saved docs/qa_edge_live.png';
  return out;
}
