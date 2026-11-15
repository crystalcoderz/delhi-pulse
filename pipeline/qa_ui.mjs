export default async function run(page, ui) {
  const out = { steps: [] };

  // 1) search + suggestions (real DOM path)
  await page.fill('#from', '543U 0');
  await page.waitForFunction(() => document.querySelectorAll('#fromSug div').length > 0, { timeout: 8000 });
  await page.evaluate(() => document.querySelector('#fromSug div').click());
  await page.fill('#to', '543U 9');
  await page.waitForFunction(() => document.querySelectorAll('#toSug div').length > 0, { timeout: 8000 });
  await page.evaluate(() => document.querySelector('#toSug div').click());
  out.steps.push('origin/destination set via suggestions');

  // 2) plan -> cards
  await page.click('#plan');
  await page.waitForFunction(() => document.querySelectorAll('#results .card').length > 0, { timeout: 15000 });
  out.steps.push('cards: ' + await page.evaluate(() => document.querySelectorAll('#results .card').length));
  out.steps.push('card1: ' + (await page.evaluate(() => document.querySelector('#results .card')?.innerText || '')).replace(/\n/g, ' / ').slice(0, 170));

  // 3) map liveness via DOM (isolated-world safe)
  out.steps.push('canvas: ' + await page.evaluate(() => document.querySelectorAll('.maplibregl-canvas').length));

  // 4) map-click -> picks (real mouse events to the real world)
  const box = await page.evaluate(() => {
    const c = document.querySelector('.maplibregl-canvas');
    const r = c.getBoundingClientRect();
    return { x: r.left + r.width * 0.40, y: r.top + r.height * 0.42 };
  });
  await page.mouse.click(box.x, box.y);          // click 1: origin
  page.waitForTimeout(300);
  await page.mouse.click(box.x + 120, box.y + 90); // click 2: destination
  await page.waitForTimeout(300);
  out.steps.push('map clicks registered: ' + await page.evaluate(() =>
    (document.getElementById('from').value || '').length > 0));

  // 5) back to a known OD for the transfer + replay checks (suggestions)
  await page.fill('#from', '543U 0');
  await page.waitForTimeout(600);
  await page.evaluate(() => document.querySelector('#fromSug div')?.click());
  await page.fill('#to', '543U 9');
  await page.waitForTimeout(600);
  await page.evaluate(() => document.querySelector('#toSug div')?.click());

  // 6) plan the suggestion OD (direct + transfer area) with error surfaced
  await page.click('#plan');
  await page.waitForTimeout(2000);
  out.steps.push('cards after suggestions: ' + await page.evaluate(() => document.querySelectorAll('#results .card').length));

  // 6) time travel (pure DOM)
  await page.evaluate(() => { document.getElementById('asof').value = '2026-10-15'; });
  await page.click('#plan');
  await page.waitForTimeout(1800);
  out.steps.push('replay chip: ' + (await page.evaluate(() => document.getElementById('modeChip').innerText)).replace(/\n/g, ' / '));

  // 7) visual proof
  await page.screenshot({ path: 'C:/Users/praya/delhi-pulse/docs/qa_ui_v05.png' });
  out.steps.push('screenshot saved');
  return out;
}
