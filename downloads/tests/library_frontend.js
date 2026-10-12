/* Isolated bulk removal: chunking, CSRF, partial failures and selection state. */
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const {Element} = require('./frontend');
const root = new Element(); root.dataset.maxBulk = '2';
const controls = {};
['all', 'clear', 'count', 'remove', 'message'].forEach(name => { controls[name] = new Element(); });
const rows = ['a', 'b', 'c'].map(key => {
    const row = new Element('li'); row.dataset.key = key;
    const checkbox = new Element('input');
    row.querySelector = () => checkbox;
    return row;
});
const count = new Element();
const title = new Element();
title.querySelectorAll = () => rows; title.querySelector = () => count;
root.querySelector = selector => selector.includes('csrfmiddlewaretoken') ? {value: 'csrf-test'} : controls[selector.slice('.d-library-'.length)];
root.querySelectorAll = selector => selector === 'li[data-key]' ? rows : [title];
const calls = [];
const context = {document: {querySelector: () => root}, window: {confirm: () => true}, fetch: async (path, options) => {
    calls.push({path, options});
    const keys = JSON.parse(options.body).keys;
    return {ok: true, json: async () => ({results: keys.map(key => ({key, result: key === 'b' ? 'failed' : 'queued', error: 'test failure'}))})};
}};
vm.runInNewContext(fs.readFileSync('downloads/static/downloads/library.js', 'utf8'), context);
(async () => {
    assert.equal(controls.remove.disabled, true);
    controls.all.onclick();
    assert.equal(controls.count.textContent, '3 selected');
    controls.clear.onclick();
    assert.equal(controls.count.textContent, '0 selected');
    controls.all.onclick();
    await controls.remove.onclick();
    assert.equal(calls.length, 2);
    assert.deepEqual(JSON.parse(calls[0].options.body).keys, ['a', 'b']);
    assert.deepEqual(JSON.parse(calls[1].options.body).keys, ['c']);
    assert.equal(calls[0].options.headers['X-CSRFToken'], 'csrf-test');
    assert.equal(rows[0].hidden, true);
    assert.equal(rows[1].hidden, false);
    assert.equal(rows[2].hidden, true);
    assert.equal(controls.count.textContent, '1 selected');
    assert.equal(controls.remove.disabled, false);
    assert.match(controls.message.textContent, /2 removal\(s\) queued/);
    assert.match(controls.message.textContent, /test failure/);
    assert.equal(count.textContent, '1 stored');
    console.log('Library bulk chunking, CSRF, partial failure and retained selection passed.');
})().catch(error => { console.error(error); process.exitCode = 1; });
