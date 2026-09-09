import assert from 'node:assert/strict';
import fs from 'node:fs';
import path from 'node:path';
import { chromium } from '../ui/node_modules/playwright/index.mjs';

const [htmlPath, controllerPath, phase = 'after'] = process.argv.slice(2);
assert.ok(htmlPath && controllerPath, 'Usage: node tests/bitrix_receivable_documents_browser.mjs HTML EDITOR_CONTROLLER_JS [before|after]');
const directory = path.dirname(path.resolve(htmlPath));
const fragment = fs.readFileSync(htmlPath, 'utf8');
const source = fs.readFileSync(controllerPath, 'utf8');
const start = source.indexOf('if(typeof BX.UI.EditorFieldViewController === "undefined")');
const end = source.indexOf("if (typeof BX.UI.EntityEditorController === 'undefined')", start);
assert.ok(start >= 0 && end > start, 'Known native controller boundaries required');
const controllerSource = source.slice(start, end);
const rawPath = path.join(directory, 'before-field.txt');
const raw = fs.existsSync(rawPath) ? fs.readFileSync(rawPath, 'utf8') : 'Original editable field';
const browser = await chromium.launch({headless: true});
const errors = [];
const checks = [];
try {
  const page = await browser.newPage({viewport: {width: 1050, height: 900}});
  page.on('pageerror', error => errors.push(error.message));
  page.on('console', entry => { if (entry.type() === 'error') errors.push(entry.text()); });
  async function installNativeController(page) {
    await page.setContent('<!doctype html><html lang="ru"><meta charset="utf-8"><title>Проверка раскрытия документов с контроллером Битрикса</title><style>body{margin:0;background:#edf2f5;padding:20px;font-family:Arial,sans-serif}main{max-width:740px;margin:auto;padding:22px;background:#f7fafb;border-radius:14px}h1{font-size:16px;color:#82919b;font-weight:400}textarea{box-sizing:border-box;width:100%;min-height:700px;font:18px Arial}#neighbor{margin-top:20px;padding:8px;border:1px dashed #bbb;color:#64717a}@media(max-width:450px){body{padding:10px}main{padding:10px}}</style><main><h1>Документы задолженности</h1><div id="field" class="ui-entity-editor-content-block">' + fragment + '</div><div id="neighbor">Соседнее обычное поле</div></main></html>');
    await page.evaluate(({raw}) => {
      class Field {
        constructor(wrapper) {
          this.wrapper = wrapper;
          this.switches = 0;
        }
        switchToSingleEditMode() {
          this.switches++;
          this.wrapper.replaceChildren(document.createElement('textarea'));
          this.wrapper.firstChild.value = raw;
        }
      }
      window.BX = {
        UI: {EntityEditorField: Field},
        delegate: (callback, owner) => callback.bind(owner),
        type: {
          isNotEmptyString: value => typeof value === 'string' && value.length > 0,
          isElementNode: value => value instanceof Element,
        },
        prop: {get: (object, key) => object[key], getElementNode: (object, key) => object[key]},
        util: {getRandomString: () => 'test'},
        getEventTarget: event => event.target,
        bind: (node, type, handler) => {
          node.addEventListener(type, handler);
          node.dataset[type + 'Bound'] = 'yes';
        },
        unbind: (node, type, handler) => node.removeEventListener(type, handler),
        findParent: (node, predicate, boundary) => {
          for (let parent = node.parentElement; parent && parent !== boundary; parent = parent.parentElement) {
            if (typeof predicate === 'function' ? predicate(parent) : parent.tagName.toLowerCase() === predicate.tagName.toLowerCase()) return parent;
          }
          return null;
        },
      };
      window.testField = new Field(document.querySelector('#field'));
      window.neighborField = new Field(document.querySelector('#neighbor'));
    }, {raw});
    await page.addScriptTag({content: controllerSource});
    await page.evaluate(() => {
      window.fieldController = BX.UI.EditorFieldViewController.create('documents', {field: testField, wrapper: testField.wrapper});
      window.neighborController = BX.UI.EditorFieldViewController.create('neighbor', {field: neighborField, wrapper: neighborField.wrapper});
    });
    await page.waitForFunction(() => document.querySelector('#field').dataset.mouseupBound === 'yes');
  }
  await installNativeController(page);
  assert.match(await page.title(), /контроллером Битрикса/);
  const summary = page.locator('#field summary');
  await summary.click();
  if (phase === 'before') {
    await page.locator('#field textarea').waitFor();
    assert.equal(await page.evaluate(() => testField.switches), 1);
    await page.screenshot({path: path.join(directory, 'before-click-bug.png'), fullPage: true});
    checks.push('Original markup reproduced native mouseup -> textarea bug');
  } else {
    const rows = page.locator('#field > .mm-receivable-documents > .mm-rd-scroll tbody tr');
    const rowCount = await rows.count();
    assert.ok(rowCount > 0);
    await page.waitForFunction(() => document.querySelector('#field details')?.open === true);
    assert.equal(await page.locator('#field textarea').count(), 0);
    assert.equal(await page.evaluate(() => testField.switches), 0);
    checks.push('Mouse disclosure remains a table under native mousedown/mouseup controller');
    await page.screenshot({path: path.join(directory, phase + '-expanded-desktop.png'), fullPage: true});
    for (let repeat = 0; repeat < 3; repeat++) {
      await summary.click();
      assert.equal(await page.locator('#field details').getAttribute('open'), null);
      await summary.click();
      assert.notEqual(await page.locator('#field details').getAttribute('open'), null);
    }
    await rows.first().locator('td').first().click();
    await rows.first().locator('td').last().dblclick();
    await page.locator('#field details tbody td').first().click();
    assert.equal(await page.evaluate(() => testField.switches), 0);
    checks.push('Repeated disclosure, table clicks and double-clicks do not edit the source');
    await summary.focus();
    await page.keyboard.press('Enter');
    assert.equal(await page.locator('#field details').getAttribute('open'), null);
    await page.keyboard.press('Space');
    assert.notEqual(await page.locator('#field details').getAttribute('open'), null);
    checks.push('Enter/Space retain native disclosure behavior');
    await page.setViewportSize({width: 390, height: 844});
    assert.equal(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), true);
    const scrollRegion = page.locator('#field details .mm-rd-scroll');
    await scrollRegion.evaluate(element => { element.scrollLeft = element.scrollWidth; });
    assert.ok(await scrollRegion.evaluate(element => element.scrollLeft > 0));
    await page.screenshot({path: path.join(directory, phase + '-expanded-mobile.png'), fullPage: true});
    await summary.click();
    await summary.click();
    assert.equal(await page.evaluate(() => testField.switches), 0);
    assert.equal(await rows.count(), rowCount);
    checks.push('390px layout, internal scroll and narrow-screen mouse toggle remain usable');
    await page.locator('#neighbor').click();
    await page.locator('#neighbor textarea').waitFor();
    assert.equal(await page.evaluate(() => neighborField.switches), 1);
    checks.push('Neighbor field still enters ordinary edit mode');
    const touchContext = await browser.newContext({viewport: {width: 390, height: 844}, hasTouch: true});
    const touchPage = await touchContext.newPage();
    touchPage.on('pageerror', error => errors.push(error.message));
    touchPage.on('console', entry => { if (entry.type() === 'error') errors.push(entry.text()); });
    await installNativeController(touchPage);
    await touchPage.locator('#field summary').tap();
    assert.notEqual(await touchPage.locator('#field details').getAttribute('open'), null);
    await touchPage.locator('#field summary').tap();
    assert.equal(await touchPage.locator('#field details').getAttribute('open'), null);
    assert.equal(await touchPage.evaluate(() => testField.switches), 0);
    assert.equal(await touchPage.locator('#field textarea').count(), 0);
    await touchContext.close();
    checks.push('Native disclosure responds to touch taps');
  }
  assert.deepEqual(errors, []);
  const result = {
    phase,
    status: phase === 'before' ? 'reproduced' : 'passed',
    scope: 'Real installed Bitrix controller source and rendered field; minimal BX/field harness, not an authenticated portal session',
    browserPlugin: 'not available; regular Playwright used',
    checks,
    consoleErrors: errors,
  };
  fs.writeFileSync(path.join(directory, phase + '-click-qa.json'), JSON.stringify(result, null, 2));
  console.log(JSON.stringify(result, null, 2));
} finally {
  await browser.close();
}
