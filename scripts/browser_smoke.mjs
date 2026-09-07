// Browser smoke test: real Chromium, real viewports, the pages a reviewer will open.
//
// Playwright's Node package and a Chromium build are already installed on this machine;
// the Python package is not, and the disk has no room for it. Driving the browser from
// Node costs nothing extra and tests exactly the same thing: the rendered page.
// Playwright is resolved from wherever it is installed on this machine rather than from
// a package.json: this project has no Node dependencies of its own and should not grow
// one just to open a browser. PLAYWRIGHT_PACKAGE overrides the search.
const candidates = [
  process.env.PLAYWRIGHT_PACKAGE,
  'playwright',
].filter(Boolean);

let chromium = null;
for (const spec of candidates) {
  try {
    ({ chromium } = await import(spec));
    break;
  } catch { /* try the next one */ }
}
if (!chromium) {
  console.error('playwright is not installed. Run:  npx playwright@1.63.0 install chromium');
  process.exit(2);
}

const BASE = process.argv[2] || 'http://127.0.0.1:8099';
const VIEWPORTS = [
  { name: '1440', width: 1440, height: 900 },
  { name: '1280', width: 1280, height: 800 },
  { name: '768',  width: 768,  height: 1024 },
  { name: '390',  width: 390,  height: 844 },
];

const PAGES = [
  { path: '/',                                  name: 'home' },
  { path: '/games',                             name: 'catalog' },
  { path: '/games?q=lament',                    name: 'search' },
  { path: '/games?q=zzzznothing',               name: 'search-empty' },
  { path: '/games?platform=nintendo-switch-2&score_band=excellent', name: 'filters-empty' },
  { path: '/games/the-lament-of-thorne-hollow', name: 'game-long-title' },
  { path: '/games/ashen-veil',                  name: 'game-full' },
  { path: '/games/quiet-harbor',                name: 'game-no-scores' },
  { path: '/games/midnight-parade',             name: 'game-no-cover' },
  { path: '/about',                             name: 'about' },
  { path: '/admin/monitoring',                  name: 'monitoring' },
  { path: '/games/nope',                        name: 'not-found' },
];

const failures = [];
const rows = [];

// Prefer a browser already on the machine over one Playwright would download: this box
// has 200 MB free, and a Chromium build is 150 of them. CHROME_PATH overrides.
async function launch() {
  const attempts = [
    process.env.CHROME_PATH ? { executablePath: process.env.CHROME_PATH } : null,
    { channel: 'chrome' },
    { channel: 'msedge' },
    {},
  ].filter(Boolean);
  const problems = [];
  for (const options of attempts) {
    try {
      return await chromium.launch(options);
    } catch (error) {
      problems.push(error.message.split('\n')[0]);
    }
  }
  console.error('no usable browser: ' + problems.join(' | '));
  process.exit(2);
}

const browser = await launch();
for (const vp of VIEWPORTS) {
  const context = await browser.newContext({ viewport: { width: vp.width, height: vp.height } });
  const page = await context.newPage();

  const consoleErrors = [];
  page.on('console', m => { if (m.type() === 'error') consoleErrors.push(m.text()); });
  page.on('pageerror', e => consoleErrors.push(`pageerror: ${e.message}`));

  for (const target of PAGES) {
    consoleErrors.length = 0;
    // 'domcontentloaded', not 'networkidle': the monitoring page holds an SSE
    // connection open by design, so the network is never idle and never will be.
    const response = await page.goto(BASE + target.path, { waitUntil: 'domcontentloaded' });
    await page.waitForTimeout(350);  // let the stylesheet apply before measuring layout
    const status = response ? response.status() : 0;
    const expected = target.name === 'not-found' ? 404 : 200;

    // Horizontal overflow: the single most common responsive defect, and invisible to
    // any markup assertion.
    const overflow = await page.evaluate(() => {
      const doc = document.documentElement;
      const offenders = [];
      for (const el of document.querySelectorAll('body *')) {
        const r = el.getBoundingClientRect();
        if (r.width > 0 && (r.right > doc.clientWidth + 1 || r.left < -1)) {
          offenders.push(`${el.tagName.toLowerCase()}.${(el.className || '').toString().split(' ')[0]}`);
        }
      }
      return {
        bodyScrolls: doc.scrollWidth > doc.clientWidth + 1,
        scrollWidth: doc.scrollWidth,
        clientWidth: doc.clientWidth,
        offenders: [...new Set(offenders)].slice(0, 5),
      };
    });

    const title = await page.title();
    const h1 = await page.locator('h1').first().textContent().catch(() => null);

    rows.push({ vp: vp.name, page: target.name, status, overflow: overflow.bodyScrolls, errors: consoleErrors.length });


    if (status !== expected) failures.push(`${vp.name} ${target.path}: HTTP ${status}, expected ${expected}`);
    if (overflow.bodyScrolls) {
      failures.push(`${vp.name} ${target.path}: horizontal scroll ${overflow.scrollWidth}>${overflow.clientWidth} via ${overflow.offenders.join(', ')}`);
    }
    // Chrome logs the main document's own 404 as a console error. That is the page
    // working, not a defect, so it is not counted against a page we expect to 404.
    const realErrors = expected === 404
      ? consoleErrors.filter(e => !/status of 404/.test(e))
      : consoleErrors;
    if (realErrors.length) {
      failures.push(`${vp.name} ${target.path}: console errors: ${realErrors.slice(0, 2).join(' | ')}`);
    }
    if (!title) failures.push(`${vp.name} ${target.path}: empty <title>`);
    if (target.name.startsWith('game-') && !h1) failures.push(`${vp.name} ${target.path}: no <h1>`);
  }
  await context.close();
}

// Interactions, at desktop only.
const context = await browser.newContext({ viewport: { width: 1440, height: 900 } });
const page = await context.newPage();
const jsErrors = [];
page.on('pageerror', e => jsErrors.push(e.message));

await page.goto(BASE + '/games/ashen-veil', { waitUntil: 'domcontentloaded' });
const themeButton = page.locator('[data-theme-toggle]');
if (await themeButton.count()) {
  const before = await page.evaluate(() => document.documentElement.getAttribute('data-theme'));
  await themeButton.first().click();
  const after = await page.evaluate(() => document.documentElement.getAttribute('data-theme'));
  if (before === after) failures.push('theme toggle did not change data-theme');
}

await page.goto(BASE + '/games', { waitUntil: 'domcontentloaded' });
const chip = page.locator('.chip input[name="platform"]').first();
if (await chip.count()) {
  await chip.click();
  await page.waitForTimeout(600);
  if (jsErrors.length) failures.push(`filter click raised: ${jsErrors[0]}`);
}

await page.goto(BASE + '/admin/monitoring', { waitUntil: 'domcontentloaded' });
await page.waitForTimeout(1500);
const streamState = await page.locator('#stream-state').textContent().catch(() => '');
rows.push({ vp: '1440', page: 'monitoring-stream', status: 200, overflow: false, errors: jsErrors.length, note: (streamState || '').trim() });
if (jsErrors.length) failures.push(`monitoring page JS errors: ${jsErrors.slice(0, 2).join(' | ')}`);

await context.close();
await browser.close();

console.log('viewport  page                 http  overflow  jsErrors');
for (const r of rows) {
  console.log(
    `${r.vp.padEnd(9)} ${r.page.padEnd(20)} ${String(r.status).padEnd(5)} ${String(r.overflow).padEnd(9)} ${r.errors}${r.note ? '  ' + r.note : ''}`
  );
}
if (failures.length) {
  console.log('\nFAILURES (' + failures.length + '):');
  for (const f of failures) console.log('  - ' + f);
  process.exit(1);
}
console.log('\nAll browser checks passed.');
