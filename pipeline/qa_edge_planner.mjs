export default async function run(page) {
  const out = { steps: [] };
  await page.waitForTimeout(6000);

  // search + click suggestions
  await page.fill('#from', 'Kashmere Gate');
  await page.waitForTimeout(600);
  const s1 = await page.evaluate(() => document.querySelectorAll('#fromSug .sug-item').length);
  out.steps.push('from suggestions: ' + s1);
  if (s1 > 0) await page.evaluate(() => document.querySelector('#fromSug .sug-item')?.click());

  await page.fill('#to', 'Nehru Place');
  await page.waitForTimeout(600);
  const s2 = await page.evaluate(() => document.querySelectorAll('#toSug .sug-item').length);
  out.steps.push('to suggestions: ' + s2);
  if (s2 > 0) await page.evaluate(() => document.querySelector('#toSug .sug-item')?.click());

  // click the plan button explicitly
  await page.click('#plan');
  await page.waitForTimeout(5000); // generous timeout for edge

  const cards = await page.evaluate(() =>
    document.querySelectorAll('#results .card:not(.error-card)').length);
  out.steps.push('journey cards: ' + cards);

  const allResults = await page.evaluate(() =>
    document.getElementById('results')?.innerText || 'EMPTY');
  out.steps.push('results text: ' + allResults.replace(/\n/g, ' | ').slice(0, 300));

  await page.screenshot({ path: 'docs/qa_edge_planner2.png' });
  out.steps.push('screenshot saved');
  return out;
}
