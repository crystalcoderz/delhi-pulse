export default async function run(page) {
  const out = { steps: [] };
  await page.waitForTimeout(4000);

  out.steps.push('canvas: ' + await page.evaluate(() => document.querySelectorAll('.maplibregl-canvas').length));
  out.steps.push('title: ' + await page.evaluate(() => document.title));
  out.steps.push('stats loaded: ' + await page.evaluate(() =>
    document.getElementById('netstats')?.textContent || 'MISSING'));

  // search for a stop
  await page.fill('#from', 'Kashmere Gate');
  await page.waitForTimeout(500);
  const hasSug = await page.evaluate(() =>
    document.querySelectorAll('#fromSug .sug-item').length);
  out.steps.push('from suggestions: ' + hasSug);

  if (hasSug > 0) {
    await page.evaluate(() => document.querySelector('#fromSug .sug-item')?.click());
    await page.fill('#to', 'Nehru Place');
    await page.waitForTimeout(500);
    const hasToSug = await page.evaluate(() =>
      document.querySelectorAll('#toSug .sug-item').length);
    out.steps.push('to suggestions: ' + hasToSug);
    if (hasToSug > 0) {
      await page.evaluate(() => document.querySelector('#toSug .sug-item')?.click());
      await page.waitForTimeout(2000);
      const cards = await page.evaluate(() =>
        document.querySelectorAll('#results .card:not(.error-card)').length);
      out.steps.push('journey cards: ' + cards);
      if (cards > 0) {
        const firstCard = await page.evaluate(() =>
          document.querySelector('#results .card:not(.error-card)')?.innerText || '');
        out.steps.push('first card: ' + firstCard.replace(/\n/g, ' / ').slice(0, 200));
      }
    }
  }

  out.steps.push('nearby API: ' + await page.evaluate(async () => {
    const r = await fetch('/stops/nearby?lat=28.6428&lng=77.2211&radius_m=300').then(r=>r.json());
    return r.length + ' stops found near NDLS';
  }));

  await page.screenshot({ path: 'docs/qa_planner_v2.png' });
  out.steps.push('screenshot: docs/qa_planner_v2.png');
  return out;
}
