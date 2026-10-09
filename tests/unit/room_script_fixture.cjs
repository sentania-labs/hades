// Execute the served room script with deterministic DOM, SSE and HTTP boundaries.
const assert = require('node:assert/strict');
const vm = require('node:vm');
const fs = require('node:fs');
class Element {
  constructor(tag = '') { this.tag = tag; this.dataset = {}; this.children = []; this.handlers = {}; this.value = ''; this.textContent = ''; this.hidden = true; this.disabled = false; }
  append(...children) { this.children.push(...children); }
  addEventListener(name, handler) { this.handlers[name] = handler; }
  querySelectorAll() { return this.children.filter(n => n.dataset.seq); }
  querySelector(selector) {
    if (selector === '.room-empty') return null;
    if (selector === '.room-turn-text') return this.children.find(n => n.className === 'room-turn-text');
    const seq = selector.match(/data-seq="(\d+)"/);
    return seq ? this.children.find(n => Number(n.dataset.seq) === Number(seq[1])) : null;
  }
}
const elements = Object.fromEntries(['.room-shell', '#room-transcript', '#room-composer', '#room-message', '#room-send', '#room-interrupt', '#room-status', '#room-target'].map(s => [s, new Element()]));
elements['.room-shell'].dataset = {roomId: 'room', csrf: 'csrf'};
global.document = {querySelector: s => elements[s], createElement: tag => new Element(tag)};
let stream;
global.EventSource = class {
  constructor() { this.handlers = {}; stream = this; }
  addEventListener(name, handler) { this.handlers[name] = handler; }
  close() {}
  emit(name, data) { this.handlers[name]?.({data: JSON.stringify(data)}); }
};
global.window = {setTimeout() {}, location: {reload() {}}};
let response;
let posts = 0;
global.fetch = async () => { posts++; return {ok: response.ok, status: response.status, json: async () => response.body}; };
vm.runInThisContext(JSON.parse(fs.readFileSync(0, 'utf8')));
const message = elements['#room-message'];
const interrupt = elements['#room-interrupt'];
const send = elements['#room-send'];
const status = elements['#room-status'];
const submit = () => elements['#room-composer'].handlers.submit({preventDefault() {}});
(async () => {
  for (const state of ['warm', 'starting', 'interrupted']) {
    stream.emit('room', {state});
    assert.equal(interrupt.hidden, true, `idle ${state}`);
  }
  const saved = {seq: 1, role: 'user', text: 'Keep the restart sequence explicit.', ended_at: 'now'};
  message.value = saved.text;
  response = {ok: false, status: 503, body: {type: 'urn:crucible:problem:room-runner-unavailable', detail: 'No runner configured'}};
  stream.emit('turn', saved); // Persistence can arrive before the failed POST response.
  await submit();
  assert.equal(message.value, '');
  assert.match(status.textContent, /Message saved and queued/);
  assert.equal(send.disabled, false);
  assert.equal(interrupt.hidden, true);
  await submit();
  assert.equal(posts, 1, 'accepted instruction cannot be retried from the composer');
  assert.equal(elements['#room-transcript'].children.length, 1);
  message.value = 'Later instruction';
  await submit(); // Persistence can also arrive after the failed POST response.
  stream.emit('turn', {...saved, seq: 2, text: 'Later instruction'});
  assert.equal(message.value, '');
  assert.equal(elements['#room-transcript'].children.length, 2);
  for (const code of [403, 409, 422, 503]) {
    message.value = 'Keep this draft';
    response = {ok: false, status: code, body: {detail: 'Rejected'}};
    await submit();
    assert.equal(message.value, 'Keep this draft');
    assert.equal(status.textContent, 'Rejected');
  }
  response = {ok: true, body: {...saved, seq: 3}};
  await submit();
  assert.equal(message.value, '');
  assert.equal(interrupt.hidden, true, 'POST success does not open an assistant turn');
  stream.emit('turn', {seq: 4, role: 'assistant', text: '', ended_at: null});
  assert.equal(interrupt.hidden, false);
  assert.equal(send.disabled, true);
  response = {ok: true, body: {state: 'interrupted'}};
  await elements['#room-interrupt'].handlers.click();
  assert.equal(status.textContent, 'Interrupt requested.');
  assert.equal(interrupt.hidden, false, 'wait for turn_end after the interrupt request');
  stream.emit('delta', {seq: 4, text: 'Working'});
  assert.equal(elements['#room-transcript'].querySelector('[data-seq="4"]').querySelector('.room-turn-text').textContent, 'Working');
  stream.emit('turn_end', {seq: 2, role: 'assistant'});
  assert.equal(interrupt.hidden, false, 'an older completed turn does not clear the active reply');
  stream.emit('turn_end', {seq: 4, role: 'assistant', interrupted: true});
  assert.equal(interrupt.hidden, true);
  assert.equal(send.disabled, false);
  assert.equal(status.textContent, 'Reply interrupted.');
  stream.emit('room', {state: 'warm'});
  assert.equal(interrupt.hidden, true);
})().catch(error => { console.error(error); process.exitCode = 1; });
