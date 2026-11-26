// The reply-side 📄 inserts the fixed HTML brief template without sending it.
const { test } = require('node:test');
const assert = require('node:assert/strict');
const { chromium } = require('playwright');
const fs = require('node:fs');
const path = require('node:path');
const root = path.resolve(__dirname, '../claude_discord/console/static');

const template = (project) => `このスレッドの直近のやり取りを、あとで読み返せるHTMLにまとめてください。\n\n範囲：［直近の話題すべて／特定の話題だけ］\n読み手：［自分の振り返り用／チームに共有する］\n保存先：Ebi Workspace の「${project}」の html/ フォルダに YYYYMMDD_件名.html（プロジェクトが分からなければ候補を挙げて聞く）\n\n作り方：/home/a_suto/services/html-brief/create-html/SKILL.md を読んで従う。ページは scripts/new-page.mjs から作り、scripts/check-html.mjs が OK になるまで直す。\n- 会話に出ていない事実は足さない。決まったこと・決まっていないこと・次にやることを分けて書く\n- JavaScript は使わない。html共有くんを通さないので、表のスマホ向け折りたたみはページのCSSに入れ、color-scheme: light を指定する\n\n仕上げ：\n- 同じフォルダの _index.md の先頭に1行足す（日付・件名・HTMLへのリンク・このスレッドへのリンク /#focus/t<スレッドID>）\n- HTMLを添付ファイルとして送る\n`;

test('HTML brief button inserts template and fits beside night and send buttons', { timeout: 30000 }, async (t) => {
  const browser = await chromium.launch({ headless: true,
    executablePath: process.env.CONSOLE_TEST_CHROMIUM, args: ['--no-sandbox'] });
  t.after(() => browser.close());
  const page = await browser.newPage({ viewport: { width: 1440, height: 900 }, locale: 'ja-JP' });
  let posts = 0;
  const item = { id: 'c1', thread_id: '1', title: 'Thread', status: 'idle', running: false,
    bucket: 'idle', priority: 2, workspace_path: 'Personal/CCDB改良' };
  await page.route('http://console.test/**', async (route) => {
    const pathname = new URL(route.request().url()).pathname;
    if (pathname === '/' || pathname.startsWith('/static/')) {
      const file = pathname === '/' ? 'index.html' : path.basename(pathname);
      return route.fulfill({ body: fs.readFileSync(path.join(root, file)), contentType:
        file.endsWith('.js') ? 'text/javascript' : file.endsWith('.css') ? 'text/css' : 'text/html' });
    }
    if (route.request().method() === 'POST') posts++;
    const data = pathname.endsWith('/board') ? { items: [item], slots: {}, places: [] } :
      pathname.endsWith('/messages') ? { messages: [] } : {};
    return route.fulfill({ contentType: 'application/json', body: JSON.stringify(data) });
  });
  await page.goto('http://console.test/#focus/c1');
  await page.waitForSelector('#reply');

  for (const [width, height, shot] of [[1440, 900, 'html-brief-desktop.png'], [390, 844, 'html-brief-mobile-390.png']]) {
    await page.setViewportSize({ width, height });
    const box = page.locator('#reply');
    await box.fill('既存のメモ');
    const button = page.locator('.reply-side .html-brief');
    assert.equal(await button.count(), 1);
    assert.equal(await button.getAttribute('title'), 'HTMLにまとめるテンプレートを入れる（送信はしません）');
    await button.click();
    const expected = `既存のメモ\n\n${template(item.workspace_path)}`;
    assert.equal(await box.inputValue(), expected);
    assert.equal(await box.evaluate((el) => el.value.slice(el.selectionStart, el.selectionEnd)), '［直近の話題すべて／特定の話題だけ］');
    const night = await page.locator('.reply-side .night').boundingBox();
    const brief = await button.boundingBox();
    const send = await page.locator('.reply button[type="submit"]').boundingBox();
    const separated = (a, b) => a.x + a.width <= b.x || b.x + b.width <= a.x ||
      a.y + a.height <= b.y || b.y + b.height <= a.y;
    assert.ok(separated(night, brief), `📄 overlaps 🌙 at ${width}px`);
    assert.ok(separated(brief, send), `📄 overlaps send at ${width}px`);
    assert.ok(send.x + send.width <= width, `send button is off-screen at ${width}px`);
    if (process.env.CONSOLE_SCREENSHOT_DIR) {
      fs.mkdirSync(process.env.CONSOLE_SCREENSHOT_DIR, { recursive: true });
      await page.screenshot({ path: path.join(process.env.CONSOLE_SCREENSHOT_DIR, shot) });
    }
  }
  const fallback = await page.evaluate(() => {
    const box = document.createElement('textarea');
    window.ConsoleHtmlBrief.insert(box);
    return box.value.includes('「［プロジェクト］」');
  });
  assert.equal(fallback, true, 'missing workspace path uses the project placeholder');
  assert.equal(posts, 0, 'inserting a template does not send');
});
