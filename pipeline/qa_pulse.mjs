export default async function run(page) {
  const out = { steps: [] };
  await page.waitForTimeout(2500);
  out.steps.push('canvas: ' + await page.evaluate(() => document.querySelectorAll('.maplibregl-canvas').length));
  out.steps.push('day options: ' + await page.evaluate(() => document.querySelectorAll('#day option').length));
  out.steps.push('geo fetch works: ' + await page.evaluate(async () => {
    const r = await fetch('/geo?hour=9').then(x => x.json());
    return r.features.length + ' features, first speed ' + r.features[0].properties.speed_kmh;
  }));
  // drag hour to 21 -> /geo should refetch and repaint
  await page.evaluate(() => { const h = document.getElementById('hour'); h.value = 21; h.dispatchEvent(new Event('input')); });
  await page.waitForTimeout(1200);
  out.steps.push('after slider: hour label = ' + await page.evaluate(() => document.getElementById('hh').textContent));
  await page.screenshot({ path: 'C:/Users/praya/delhi-pulse/docs/qa_pulse_v05.png' });
  out.steps.push('screenshot saved');
  return out;
}
