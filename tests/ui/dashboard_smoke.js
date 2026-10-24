/*
 * Dashboard smoke test (real browser). Not part of pytest: it needs Node + a Chrome install.
 *
 *   npm install --no-save playwright-core
 *   python web_ui.py            # PORT=5077 python web_ui.py
 *   BASE_URL=http://127.0.0.1:5077/ node tests/ui/dashboard_smoke.js
 *
 * Environment: BASE_URL (default http://127.0.0.1:5000/), CHROME_PATH (default: the usual
 * Windows / Linux Chrome locations). Exits 1 and lists every failed check.
 *
 * What it proves (the static tests in tests/test_dashboard_template.py cannot):
 *   - the wizard loads the option model and has no JavaScript errors;
 *   - real clicks walk steps 1 -> 5 and the review "Edit" buttons return to the owning step;
 *   - each engine hides the options it does not use;
 *   - the request sent to /api/start carries the model's per-engine defaults.
 */
const fs = require('fs');
const { chromium } = require('playwright-core');

const BASE = process.env.BASE_URL || 'http://127.0.0.1:5000/';
const CANDIDATES = [
  process.env.CHROME_PATH,
  'C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe',
  'C:\\Program Files (x86)\\Google\\Chrome\\Application\\chrome.exe',
  '/usr/bin/google-chrome', '/usr/bin/chromium', '/usr/bin/chromium-browser',
].filter(Boolean);
const CHROME = CANDIDATES.find(p => fs.existsSync(p));

const failures = [];
const check = (name, ok, detail) => { if (!ok) failures.push(`${name}${detail ? ' -> ' + detail : ''}`); };
const same = (a, b) => JSON.stringify(a) === JSON.stringify(b);

(async () => {
  if (!CHROME) { console.error('No Chrome found; set CHROME_PATH'); process.exit(2); }
  const browser = await chromium.launch({ executablePath: CHROME, headless: true });
  const page = await (await browser.newContext({ viewport: { width: 1280, height: 1000 } })).newPage();
  const errors = [];
  page.on('pageerror', e => errors.push(e.message));

  let sent = null;
  await page.route('**/api/start', async route => {
    sent = JSON.parse(route.request().postData() || '{}');
    await route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ job_id: 'smoke', status: 'running' }) });
  });
  await page.route('**/api/stream/**', route => route.abort());

  await page.goto(BASE, { waitUntil: 'load' });
  await page.evaluate(() => switchView('studio'));
  await page.waitForFunction(() => !!OPTION_MODEL, null, { timeout: 5000 }).catch(() => {});

  check('option model loaded', await page.evaluate(() => !!OPTION_MODEL));

  // ── walk the wizard with real clicks ──
  await page.evaluate(() => { document.getElementById('input-source').value = 'movies/clip.mp4'; });
  const steps = [await page.evaluate(() => currentStep)];
  for (const id of ['btn-step1-next', 'btn-step2-next', 'btn-step3-next', 'btn-step4-next']) {
    await page.click('#' + id);
    steps.push(await page.evaluate(() => currentStep));
  }
  check('Next buttons walk 1..5', same(steps, [1, 2, 3, 4, 5]), JSON.stringify(steps));

  const editTargets = {};
  for (const [titleId, label] of [['rev-title-media', 'media'], ['rev-title-engine', 'engine'], ['rev-title-audio', 'audio'],
                                  ['rev-title-subtitles', 'subtitles'], ['rev-title-shields', 'shields'], ['rev-title-stages', 'stages']]) {
    await page.evaluate(() => goToStep(5));
    await page.evaluate(id => document.getElementById(id).parentElement.querySelector('button').click(), titleId);
    editTargets[label] = await page.evaluate(() => currentStep);
  }
  check('review Edit buttons return to the owning step',
        same(editTargets, { media: 1, engine: 1, audio: 4, subtitles: 2, shields: 3, stages: 4 }), JSON.stringify(editTargets));

  // ── per engine: relevance + request defaults ──
  const expectedHidden = {
    recap: ['color_grading'],
    subtitle: ['custom_thumb_title', 'language', 'narrative_tone', 'story_length', 'subtitle_mode', 'tts_engine', 'tts_rate', 'watermark_enabled'],
    hardsub: ['custom_thumb_title', 'language', 'narrative_tone', 'story_length', 'subtitle_mode', 'tts_engine', 'tts_rate', 'watermark_enabled'],
  };
  const expectedDefaults = {
    recap: { translation_style: 'recap', audio_mode: 'ai_voiceover', render_video: true, video_format: 'both' },
    subtitle: { translation_style: 'dialogue', audio_mode: 'original', render_video: false, video_format: '16:9' },
    hardsub: { translation_style: 'persona', audio_mode: 'original', render_video: true, video_format: 'both' },
  };
  for (const engine of ['recap', 'subtitle', 'hardsub']) {
    await page.evaluate(() => goToStep(1));
    await page.evaluate(e => {
      const el = document.getElementById('select-engine-mode');
      el.value = e; el.dispatchEvent(new Event('change', { bubbles: true }));
    }, engine);
    const hidden = await page.evaluate(() => [...document.querySelectorAll('[data-option].option-na')].map(e => e.dataset.option).sort());
    check(`${engine}: hidden options`, same(hidden, expectedHidden[engine]), JSON.stringify(hidden));

    sent = null;
    await page.evaluate(() => launchConfirmedJob());
    await page.waitForTimeout(400);
    check(`${engine}: request sent`, !!sent);
    if (sent) {
      const got = {};
      for (const k of Object.keys(expectedDefaults[engine])) got[k] = sent[k];
      check(`${engine}: request defaults`, same(got, expectedDefaults[engine]), JSON.stringify(got));
      check(`${engine}: engine_mode`, sent.engine_mode === engine, sent.engine_mode);
    }
  }

  // ── studio views: they render, switch and survive a theme change without errors ──
  await page.goto(BASE, { waitUntil: 'load' });
  check('PaiStudio is loaded', await page.evaluate(() => typeof PaiStudio === 'object'));
  const viewState = {};
  for (const view of ['projects', 'brand', 'outputs', 'inputs', 'studio', 'welcome']) {
    await page.evaluate(v => switchView(v), view);
    await page.waitForTimeout(300);
    viewState[view] = await page.evaluate(() => document.querySelector('.view-section.active').id);
  }
  check('every nav view activates', same(viewState, Object.fromEntries(Object.keys(viewState).map(v => [v, 'view-' + v]))), JSON.stringify(viewState));
  const themes = await page.evaluate(() => {
    const read = () => document.documentElement.getAttribute('data-theme');
    const first = read();
    document.getElementById('theme-toggle').click();
    return [first, read()];
  });
  check('theme toggle flips the theme', themes[0] !== themes[1] && ['light', 'dark'].includes(themes[1]), JSON.stringify(themes));
  const navIcons = await page.evaluate(() => document.querySelectorAll('.nav-tab-btn svg').length);
  check('nav icons survive language changes', navIcons === 6, String(navIcons));
  await page.evaluate(() => setLanguage('en'));
  check('nav icons survive setLanguage', (await page.evaluate(() => document.querySelectorAll('.nav-tab-btn svg').length)) === 6);

  // ── delivery mode: the wizard picker reaches the request ──
  await page.goto(BASE, { waitUntil: 'load' });
  await page.evaluate(() => switchView('studio'));
  await page.waitForFunction(() => !!OPTION_MODEL, null, { timeout: 5000 }).catch(() => {});
  await page.evaluate(() => { document.getElementById('input-source').value = 'movies/clip.mp4'; goToStep(2); });
  check('delivery picker has three cards', (await page.evaluate(() => document.querySelectorAll('.delivery-card').length)) === 3);
  for (const mode of ['oneclick', 'capcut', 'both']) {
    await page.evaluate(() => goToStep(2));              // launching moves the wizard to the monitor (step 6)
    await page.click(`.delivery-card:has(input[value="${mode}"])`);
    const picked = await page.evaluate(() => ({
      selected: [...document.querySelectorAll('.delivery-card.is-selected input')].map(i => i.value),
      renderDimmed: document.getElementById('wrap-render-video').classList.contains('is-overridden'),
    }));
    check(`delivery ${mode}: one card selected`, same(picked.selected, [mode]), JSON.stringify(picked));
    check(`delivery ${mode}: the render menu is overridden only in the CapCut pipeline`, picked.renderDimmed === (mode === 'capcut'));
    sent = null;
    await page.evaluate(() => launchConfirmedJob());
    await page.waitForTimeout(300);
    check(`delivery ${mode}: request carries it`, !!sent && sent.delivery_mode === mode, sent && sent.delivery_mode);
  }
  await page.evaluate(() => { goToStep(5); });
  check('review shows the delivery mode', (await page.evaluate(() => document.getElementById('rev-delivery').textContent)).length > 3);

  // ── tools: hub, the two panels, the request they send, the monitor and the result viewer (network mocked) ──
  const DEMO_RESULT = {
    project: 'Demo', engine_type: 'shorts', source: 'Podcast', duration: 3725, delivery_mode: 'both', settings: {},
    package_url: '/api/projects/Demo/capcut', captions_url: null, zip_url: '/api/outputs/zip?movie=Demo',
    items: [
      { index: 1, start: 60, end: 90, duration: 30, score: 91, title: 'Hook one', caption: 'A caption', hashtags: ['#a', '#b'], reason: 'why',
        videos: [{ format: '9:16', name: 'short_01.mp4', url: '/x/short_01.mp4' }], clean_url: '/x/clip_01.mp4', srt_url: '/x/short_01.srt', cover_url: null },
      { index: 2, start: 200, end: 232, duration: 32, score: 50, title: 'Hook two', caption: '', hashtags: [], reason: '', videos: [], clean_url: '/x/clip_02.mp4', srt_url: null, cover_url: null },
    ],
  };
  await page.route('**/api/status/smoke', route => route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({
    job_id: 'smoke', status: 'done', phase: 'Made 2 Short(s)', progress: 100, log: ['ok'], elapsed_sec: 5, project_dir: 'Demo', package_url: '/api/projects/Demo/capcut', zip_url: null }) }));
  await page.route('**/api/projects/Demo/tool-result', route => route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(DEMO_RESULT) }));

  await page.goto(BASE, { waitUntil: 'load' });
  await page.waitForFunction(() => document.querySelectorAll('#tool-grid .tool-tile').length > 0, null, { timeout: 5000 }).catch(() => {});
  const tiles = await page.evaluate(() => [...document.querySelectorAll('#tool-grid .tool-tile')].map(t => t.dataset.tool));
  check('the hub has six tiles', same(tiles, ['recap', 'capcut', 'subtitle', 'hardsub', 'shorts', 'splitter']), JSON.stringify(tiles));

  await page.click('.tool-tile[data-tool="capcut"]');
  const capcutTile = await page.evaluate(() => ({ view: document.querySelector('.view-section.active').id, engine: document.getElementById('select-engine-mode').value,
    delivery: document.querySelector('input[name="delivery_mode"]:checked').value, step: currentStep }));
  check('the CapCut tile opens the wizard with the pipeline chosen', same(capcutTile, { view: 'view-studio', engine: 'recap', delivery: 'capcut', step: 1 }), JSON.stringify(capcutTile));

  await page.evaluate(() => switchView('welcome'));
  await page.click('.tool-tile[data-tool="splitter"]');
  await page.waitForSelector('#view-tool .tool-field');       // the panel opens after the option model is fetched
  const splitter = await page.evaluate(() => ({ view: document.querySelector('.view-section.active').id,
    fields: [...document.querySelectorAll('#view-tool .tool-field')].filter(f => f.style.display !== 'none').length,
    minutesVisible: [...document.querySelectorAll('#view-tool input[type=number]')].map(i => i.closest('.tool-field').style.display) }));
  check('the splitter panel opens', splitter.view === 'view-tool' && splitter.fields >= 4, JSON.stringify(splitter));
  await page.selectOption('#view-tool select >> nth=0', 'parts');
  const afterMode = await page.evaluate(() => [...document.querySelectorAll('#view-tool input[type=number]')].map(i => i.closest('.tool-field').style.display));
  check('switching the split mode swaps minutes for parts', same(afterMode, ['none', '']), JSON.stringify(afterMode));
  sent = null;
  await page.fill('#tool-input', 'movies/film.mp4');
  await page.click('#tool-start');
  await page.waitForTimeout(400);
  check('splitter request', !!sent && sent.engine_mode === 'splitter' && sent.split_mode === 'parts' && sent.split_parts === 2 && !('split_minutes' in sent) && !('clip_count' in sent),
        JSON.stringify(sent));

  await page.evaluate(() => switchView('welcome'));
  await page.click('.tool-tile[data-tool="shorts"]');
  await page.click('#view-tool .delivery-card:has(input[value="capcut"])');
  sent = null;
  await page.click('#tool-start');                       // empty source: refused, nothing sent
  await page.waitForTimeout(250);
  check('an empty source is refused', sent === null);
  await page.fill('#tool-input', 'https://example.com/long.mp4');
  await page.click('#tool-start');
  await page.waitForTimeout(2200);
  check('shorts request', !!sent && sent.engine_mode === 'shorts' && sent.delivery_mode === 'capcut' && sent.clip_count === 5 && sent.video_format === '9:16'
        && sent.short_subtitles === 'burmese' && !('split_mode' in sent), JSON.stringify(sent));
  const shorts = await page.evaluate(() => ({
    cards: [...document.querySelectorAll('.short-card')].map(c => c.querySelector('.short-title').textContent),
    package: (document.getElementById('tool-package') || {}).getAttribute && document.getElementById('tool-package').getAttribute('href'),
    clean: [...document.querySelectorAll('.short-card a')].filter(a => (a.getAttribute('href') || '').includes('clip_01')).length,
    hashtags: document.querySelectorAll('.short-card .short-tags .chip').length,
    done: document.getElementById('tool-bar-fill').classList.contains('is-done'),
  }));
  check('the result viewer shows the Shorts', same(shorts.cards, ['Hook one', 'Hook two']) && shorts.package === '/api/projects/Demo/capcut' && shorts.clean === 1
        && shorts.hashtags === 2 && shorts.done, JSON.stringify(shorts));

  await page.evaluate(() => PaiTools.openResult('Demo'));
  await page.waitForTimeout(500);
  check('a project result opens without the form', (await page.evaluate(() => !document.getElementById('tool-form') && document.querySelectorAll('.short-card').length === 2)));

  check('no JavaScript errors', errors.length === 0, errors.join(' | '));
  await browser.close();

  if (failures.length) {
    console.error(`FAILED (${failures.length}):\n - ` + failures.join('\n - '));
    process.exit(1);
  }
  console.log('dashboard smoke test passed');
})().catch(e => { console.error(e); process.exit(2); });
