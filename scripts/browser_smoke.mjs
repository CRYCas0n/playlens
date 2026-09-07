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

// The pages are chosen from whatever the deployment actually holds, not from a list of
// slugs. Hardcoded ones came from the demo seed and are all 404s against production --
// a browser pass that opens twelve missing pages proves only that 404 renders.
//
// Each edge case is looked for in the real catalogue, and when the deployment has no
// example of one it is skipped by name rather than quietly dropped: "no game without a
// cover" is a fact about the data, and a reviewer should see it said out loud.
async function resolvePages() {
  const pages = [
    { path: '/', name: 'home' },
    { path: '/games', name: 'catalog' },
    { path: '/about', name: 'about' },
    { path: '/admin/monitoring', name: 'monitoring' },
    { path: '/games/definitely-not-a-game', name: 'not-found' },
  ];

  let items = [];
  try {
    const res = await fetch(BASE + '/api/v1/games?limit=100');
    items = (await res.json()).items || [];
  } catch (error) {
    console.error('could not read the catalogue: ' + error.message);
  }
  if (!items.length) {
    console.error('the catalogue is empty; only the static pages will be checked');
    return { pages, skipped: ['every game page'] };
  }

  const skipped = [];
  const pick = (name, predicate, label) => {
    const hit = items.find(predicate);
    if (hit) pages.push({ path: '/games/' + hit.slug, name });
    else skipped.push(label);
  };

  const scored = g => g.metascore && g.metascore.value != null;
  pick('game-full', g => scored(g) && g.userscore && g.userscore.value != null,
       'a game with both scores');
  pick('game-no-scores', g => !scored(g), 'a game without a metascore');
  pick('game-no-cover', g => !g.cover_url, 'a game without a cover');

  const longest = [...items].sort((a, b) => b.title.length - a.title.length)[0];
  if (longest) pages.push({ path: '/games/' + longest.slug, name: 'game-long-title' });

  // A search term that is in the data, and one that cannot be.
  const word = (items[0].title.match(/[A-Za-z]{4,}/) || ['game'])[0].toLowerCase();
  pages.push({ path: '/games?q=' + word, name: 'search' });
  pages.push({ path: '/games?q=zzzznothing', name: 'search-empty' });
  pages.push({
    path: '/games?platform=nintendo-switch-2&score_band=excellent',
    name: 'filters-empty',
  });

  return { pages, skipped, sample: items[0].slug };
}

const { pages: PAGES, skipped: SKIPPED, sample: SAMPLE_SLUG } = await resolvePages();

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

await page.goto(BASE + '/games/' + (SAMPLE_SLUG || 'nope'), { waitUntil: 'domcontentloaded' });
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

if (SKIPPED.length) {
  console.log('not present in this deployment: ' + SKIPPED.join(', '));
  console.log('');
}
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
