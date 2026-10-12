/* Run with node downloads/tests/frontend.js; isolated DOM/API doubles, no network. */
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
class Element {
    constructor(tag = 'div') { this.tag = tag; this.children = []; this.style = {}; this.dataset = {}; this.attributes = {}; this.listeners = {}; this.hidden = false; this.value = ''; }
    append(...items) { this.children.push(...items); if (this.tag === 'select' && !this.value && items[0]) this.value = items[0].value; }
    appendChild(item) { this.append(item); }
    prepend(item) { this.children.unshift(item); }
    replaceChildren() { this.children = []; }
    setAttribute(name, value) { this.attributes[name] = value; }
    removeAttribute(name) { delete this.attributes[name]; if (name === 'value') this.value = undefined; }
    addEventListener(name, fn) { this.listeners[name] = fn; }
    querySelector(selector) { return this.children.find(e => selector === 'progress' ? e.tag === 'progress' : (e.className || '').split(' ').includes(selector.slice(1))) || null; }
    showModal() { this.open = true; }
    close() { this.open = false; }
}
const script = fs.readFileSync('downloads/static/downloads/downloads.js', 'utf8');
const flush = () => new Promise(resolve => setImmediate(resolve));
async function scenario(mode) {
    const elements = {};
    const names = ['message'];
    if (mode === 'reader') names.push('close', 'current-download', 'current-remove', 'current-status', 'current-progress');
    else names.push('queue-summary');
    if (mode === 'queue') names.push('queue', 'cache-usage', 'clear-cache');
    if (mode === 'series') names.push('selection-controls', 'group', 'count', 'visible', 'all', 'clear-selection', 'download', 'remove');
    names.forEach(name => { elements[name] = new Element(name === 'group' ? 'select' : 'div'); });
    const root = new Element(); root.dataset = mode === 'queue' ? {} : {source: 'test', series: 'series'};
    root.querySelector = selector => selector.includes('csrfmiddlewaretoken') ? {value: 'test-csrf'} : elements[selector.slice(3)] || null;
    const trigger = new Element();
    const rows = mode === 'series' ? [new Element('tr'), new Element('tr')] : [];
    rows.forEach((row, i) => { row.dataset.chapter = String(i + 1); row.querySelector = selector => row.children[0].querySelector(selector); });
    if (rows[1]) rows[1].style.display = 'none';
    const job = {id: 2, key: 'key1', chapter: '1', group: 'group', series: 'series', kind: 'download', state: 'downloading', pages: 2, total_pages: 5};
    const snapshot = {jobs: [job, {...job, id: 1, state: 'failed'}], chapters: [], active: true, counts: {accepted: 0, failed: 1}, max_bulk_items: 100, cache: {files: 0, max_files: 20, bytes: 0}};
    const catalog = {groups: {group: 'Group'}, downloads: {'1': {group: {key: 'key1'}}, '2': {group: {key: 'key2'}}}};
    const requests = [];
    const document = {hidden: false, querySelector: s => s === '.chapter-downloads' ? root : s === '.d-open' ? trigger : null, querySelectorAll: () => rows, getElementById: () => mode === 'reader' ? {} : null, createElement: tag => new Element(tag), addEventListener() {}};
    const context = {document, window: {location: {href: 'https://reader.invalid/'}, addEventListener() {}, confirm: () => true}, HTMLImageElement: class {}, URL, Reader: {current: catalog, SCP: {chapter: '1'}, getGroup: () => 'group'}, setTimeout: () => 1, clearTimeout() {}, fetch: async (path, options) => {
        requests.push({path, options});
        return {ok: true, json: async () => path.includes('/catalog/') ? catalog : options.method === 'POST' ? {results: JSON.parse(options.body).keys.map(key => ({key, result: 'queued'}))} : snapshot};
    }};
    vm.runInNewContext(script, context);
    await flush();
    assert.equal(elements.message.textContent || '', '');
    if (mode === 'reader') {
        assert.equal(root.open, undefined);
        trigger.onclick(); await flush();
        assert.equal(root.open, true);
        assert.equal(elements['current-progress'].value, 2);
        assert.equal(elements['current-progress'].max, 5);
        assert.equal(elements['current-download'].disabled, true);
        assert.equal(elements['current-status'].textContent, 'downloading');
        job.state = 'cooldown'; trigger.onclick(); await flush();
        assert.equal(elements['current-progress'].value, undefined);
        elements.close.onclick(); assert.equal(root.open, false);
    }
    if (mode === 'series') {
        elements.visible.onclick(); assert.equal(rows[0].querySelector('.d-select').checked, true);
        assert.ok(!rows[1].querySelector('.d-select').checked);
        elements.all.onclick(); assert.equal(rows[1].querySelector('.d-select').checked, true);
        elements.download.onclick(); await flush();
        const posted = requests.find(r => r.options.method === 'POST');
        assert.deepEqual(JSON.parse(posted.options.body).keys, ['key1', 'key2']);
        assert.equal(posted.options.headers['X-CSRFToken'], 'test-csrf');
        assert.equal(rows[0].querySelector('progress').value, 2);
    }
    if (mode === 'queue') {
        assert.equal(elements.queue.children.length, 2);
        assert.equal(elements.queue.children[0].children[0].querySelector('progress').max, 5);
        assert.equal(elements.queue.children[1].children[1].title, 'Retry');
    }
}
module.exports = {Element};
if (require.main === module) (async () => { for (const mode of ['reader', 'series', 'queue']) await scenario(mode); console.log('Reader popup, latest-job progress, bulk selection/CSRF and queue controls passed.'); })().catch(error => { console.error(error); process.exitCode = 1; });
