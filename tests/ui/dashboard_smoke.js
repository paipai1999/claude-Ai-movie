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
    subtitle: ['custom_thumb_title', 'language', 'subtitle_mode', 'tts_engine', 'watermark_enabled'],
    hardsub: ['custom_thumb_title', 'language', 'subtitle_mode', 'tts_engine', 'watermark_enabled'],
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

  check('no JavaScript errors', errors.length === 0, errors.join(' | '));
  await browser.close();

  if (failures.length) {
    console.error(`FAILED (${failures.length}):\n - ` + failures.join('\n - '));
    process.exit(1);
  }
  console.log('dashboard smoke test passed');
})().catch(e => { console.error(e); process.exit(2); });
