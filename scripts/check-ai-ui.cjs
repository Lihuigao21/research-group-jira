/* Offline regression for the actual UI: stale receipts, safe text and local-only links. */
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");
const assert = require("node:assert/strict");
const created = [];
class Element {
  constructor(tag) {this.tagName = tag; this.children = []; this.dataset = {}; this.attributes = {}; this.listeners = {}; this.textContent = ""; created.push(this);}
  append(...items) {for (const item of items) {if (item.tagName === "#fragment") this.children.push(...item.children); else this.children.push(item);}}
  prepend(...items) {const group = new Element("#fragment"); group.append(...items); this.children.unshift(...group.children);}
  replaceChildren(...items) {this.children = []; this.append(...items);}
  setAttribute(key, value) {this.attributes[key] = value;}
  addEventListener(event, callback) {this.listeners[event] = callback;}
  get firstChild() {return this.children[0];}
  querySelector() {return null;}
  set innerHTML(_) {throw new Error("Dynamic innerHTML is forbidden");}
}
const context = {window: {research_group: {}}, document: {createElement: tag => new Element(tag), createDocumentFragment: () => new Element("#fragment")},
  setTimeout, clearTimeout, console};
vm.createContext(context);
vm.runInContext(fs.readFileSync(path.join(__dirname, "../src/research_group/research_group/public/js/ai_assistant.js"), "utf8"), context);
const ns = context.window.research_group;
let checks = 0;
function check(value) {assert.ok(value); checks++;}
function fresh() {
  const app = Object.create(ns.AIAssistant.prototype);
  Object.assign(app, {current: "A", epoch: 1, loadSequence: 0, messages: new Element("main"), ops: new Element("section"), status: new Element("p"),
    input: {value: ""}, topic: {value: ""}, sendButton: {}, cancelButton: {}, recheck: {}});
  app.error = value => {app.errorValue = value;};
  return app;
}
const deferred = () => {let resolve, reject; const promise = new Promise((done, fail) => {resolve = done; reject = fail;}); return {promise, resolve, reject};};
(async () => {
  check(ns.aiSafeSource("/desk/rg-topic/RG-TOP-00001"));
  for (const url of ["https://example.invalid/a", "//evil.invalid/x", "javascript:alert(1)", "/desk/rg-topic/../x", "/desk/rg-topic/a?x=1", "/desk/rg-topic/a\\b"]) {
    check(!ns.aiSafeSource(url));
  }
  const app = fresh(), a = deferred(), b = deferred();
  app.api = (_, args) => args.name === "A" ? a.promise : b.promise;
  const rendered = []; app.renderConversation = data => rendered.push(data.name);
  const first = app.loadConversation(); app.current = "B"; app.epoch++; const second = app.loadConversation();
  b.resolve({name: "B"}); await second; a.resolve({name: "A"}); await first;
  check(rendered.length === 1 && rendered[0] === "B");
  const old = deferred(), latest = deferred(); let calls = 0;
  app.api = () => (++calls === 1 ? old.promise : latest.promise);
  const older = app.loadConversation(), newer = app.loadConversation(); latest.resolve({name: "B-new"}); await newer;
  old.resolve({name: "B-old"}); await older;
  check(rendered[rendered.length - 1] === "B-new" && !rendered.includes("B-old"));
  const safe = fresh(), attack = '<img src="https://evil.invalid/secret" onerror="alert(1)"><script>alert(1)</script>';
  safe.renderConversation({messages: [{role: "assistant", content: attack, sources: [
    {url: "https://evil.invalid/secret", doctype: "bad", name: "bad"},
    {url: "/desk/rg-topic/RG-TOP-00001", doctype: "RG Topic", name: "RG-TOP-00001"}]}], operations: []}, false);
  check(created.some(el => el.tagName === "div" && el.textContent === attack));
  check(!created.some(el => ["img", "script", "iframe"].includes(el.tagName)));
  check(created.filter(el => el.tagName === "a").every(el => el.attributes.href.startsWith("/desk/")));
  safe.renderConversation({messages: [{role: "user", content: "新会话", sources: []}], operations: []}, false);
  check(safe.lastAnswer === "");
  safe.lastAnswer = "最近建议";
  safe.renderConversation({messages: [{role: "assistant", content: "较早建议", sources: []}], operations: []}, true);
  check(safe.lastAnswer === "最近建议");
  const sending = fresh(), receipt = deferred(); sending.input.value = "A中已提交的草稿";
  sending.api = () => receipt.promise; sending.loadConversation = async () => {}; sending.loadHistory = async () => {}; sending.checkRun = async () => {};
  const pendingSend = sending.send(); sending.current = "B"; sending.epoch++; sending.input.value = "B中新输入的草稿";
  receipt.resolve({run_id: "run-A", conversation: "A", status: "queued"}); await pendingSend;
  check(sending.input.value === "B中新输入的草稿"); check(sending.running.conversation === "A");
  const receipts = fresh();
  const statuses = [["active", "有效"], ["completed", "已完成"], ["deleted", "已删除"], ["待审批", "待审批"]];
  receipts.renderOperations(statuses.map(([status], index) => ({name: "op-" + index, status: "consumed", result: {status, name: "result-" + index}})));
  statuses.forEach(([, label], index) => check(receipts.ops.children[index].children.some(element => element.textContent === label + " · result-" + index)));
  const secretary = fresh();
  Object.assign(secretary, {notice: new Element("p"), topic: new Element("select"), quick: new Element("nav"), progressButton: {}, appointmentButton: {}});
  secretary.api = async () => ({configuration: {label: "离线模拟"}, privacy_notice: "仅本人", topics: [], roles: ["课题组秘书"], conversations: {items: []}});
  secretary.renderHistory = () => {};
  let shortcutPrompt; secretary.send = async () => {shortcutPrompt = secretary.input.value;};
  await secretary.start();
  check(secretary.quick.children.some(element => element.textContent === "近期排期") && !secretary.quick.children.some(element => element.textContent === "本周排期"));
  secretary.quick.children.find(element => element.textContent === "近期排期").listeners.click();
  check(shortcutPrompt === "近期排期");
  secretary.api = async () => ({configuration: {label: "离线模拟"}, privacy_notice: "仅本人", topics: [], roles: ["课题组教师"], conversations: {items: []}});
  await secretary.start();
  check(secretary.quick.children.length === 1 && secretary.quick.children[0].textContent === "全组最近讨论");

  const reloaded = fresh();
  Object.assign(reloaded, {current: null, epoch: 0, input: {value: "尚未发送的草稿"}, notice: new Element("p"), topic: new Element("select"),
    quick: new Element("nav"), progressButton: {}, appointmentButton: {}, deleteButton: {}, history: {}, historyList: new Element("nav"),
    panel: new Element("aside"), panelSequence: 0});
  reloaded.renderHistory = () => {};
  reloaded.api = async method => {
    if (method === "bootstrap") return {configuration: {label: "离线模拟"}, privacy_notice: "仅本人", topics: [], roles: [], conversations: {items: [{name: "A"}]}};
    if (method === "get_conversation") return {name: "A", messages: [{role: "user", content: "已接受的消息", request_id: "reload-run", sources: []}], operations: []};
    if (method === "get_run") return {run_id: "reload-run", conversation: "A", status: "running"};
    throw new Error("Unexpected recovery endpoint " + method);
  };
  await reloaded.start(); clearTimeout(reloaded.timer);
  check(reloaded.current === "A" && reloaded.running.run_id === "reload-run");
  check(reloaded.sendButton.disabled && !reloaded.cancelButton.hidden && !reloaded.recheck.hidden);
  check(reloaded.input.value === "尚未发送的草稿");

  const lost = fresh(), lostReceipt = deferred(); let lostRequests = 0;
  lost.input.value = "已接受但回执丢失";
  lost.api = async method => {
    if (method === "send_message") {lostRequests++; return lostReceipt.promise;}
    if (method === "get_conversation") return {name: "A", messages: [{role: "user", content: "已接受但回执丢失", request_id: "lost-run", sources: []}], operations: []};
    if (method === "get_run") return {run_id: "lost-run", conversation: "A", status: "queued"};
    throw new Error("Unexpected lost-receipt endpoint " + method);
  };
  const lostPending = lost.send(); lost.current = "B"; lost.epoch++; lost.input.value = "B的草稿";
  lost.messages.replaceChildren(new Element("B-content"));
  lostReceipt.reject(new Error("Lost transport receipt")); await lostPending; clearTimeout(lost.timer);
  check(lostRequests === 1 && lost.running.run_id === "lost-run" && !lost.cancelButton.hidden && !lost.recheck.hidden);
  check(lost.current === "B" && lost.input.value === "B的草稿" && lost.messages.firstChild.tagName === "B-content");
  const retry = fresh(); retry.input.value = "保持草稿"; let reads = 0, retrySubmissions = 0;
  retry.api = async method => {
    if (method === "send_message") {retrySubmissions++; throw new Error("Receipt lost");}
    if (method === "get_conversation") {
      if (++reads === 1) throw new Error("Readback also unavailable");
      return {name: "A", messages: [{role: "user", content: "保持草稿", request_id: "retry-run", sources: []}], operations: []};
    }
    if (method === "get_run") return {run_id: "retry-run", conversation: "A", status: "running"};
    throw new Error("Unexpected retry endpoint " + method);
  };
  await retry.send(); await retry.send();
  check(retrySubmissions === 1 && retry.sendButton.disabled && !retry.recheck.hidden && retry.pendingReceipt === "A");
  await retry.checkRun(); clearTimeout(retry.timer);
  check(retry.running.run_id === "retry-run" && !retry.cancelButton.hidden && !retry.pendingReceipt && retry.input.value === "保持草稿");

  const twice = fresh(); twice.input.value = "只发送一次"; let submissions = 0;
  twice.api = async method => {
    if (method === "send_message") {submissions++; return {run_id: "fast-run", conversation: "A", status: "queued"};}
    if (method === "get_run") return {run_id: "fast-run", conversation: "A", status: "completed"};
    throw new Error("Unexpected duplicate-send endpoint " + method);
  };
  twice.loadConversation = twice.loadHistory = async () => {};
  await Promise.all([twice.send(), twice.send()]);
  check(submissions === 1 && !twice.sending && !twice.running && !twice.sendButton.disabled);
  const createFirst = fresh(), creation = deferred(); createFirst.current = null; createFirst.input.value = "新会话仅发送一次";
  let creations = 0, createdSubmissions = 0;
  createFirst.newConversation = async () => {creations++; await creation.promise; createFirst.current = "A"; return "A";};
  createFirst.api = async method => {
    if (method === "send_message") {createdSubmissions++; return {run_id: "created-run", conversation: "A", status: "queued"};}
    if (method === "get_run") return {run_id: "created-run", conversation: "A", status: "completed"};
    throw new Error("Unexpected create/send endpoint " + method);
  };
  createFirst.loadConversation = createFirst.loadHistory = async () => {};
  const creates = [createFirst.send(), createFirst.send()];
  check(creations === 1 && createdSubmissions === 0);
  creation.resolve(); await Promise.all(creates);
  check(createdSubmissions === 1 && !createFirst.sending);

  let deletionDialog;
  context.frappe = {ui: {Dialog: class {
    constructor(config) {deletionDialog = config;}
    show() {} hide() {} disable_primary_action() {} enable_primary_action() {}
  }}};
  const deleting = fresh(), deleted = deferred(); deleting.deleteButton = {}; let historyRefreshes = 0;
  deleting.api = async method => method === "conversation_memory" ? [] : deleted.promise;
  deleting.loadHistory = async () => {historyRefreshes++;};
  await deleting.removeConversation(); const pendingDeletion = deletionDialog.primary_action({});
  deleting.current = "B"; deleting.epoch++; deleting.input.value = "B保留的草稿"; deleting.lastAnswer = "B的建议";
  const bMessage = new Element("B-message"), bOperation = new Element("B-operation");
  deleting.messages.replaceChildren(bMessage); deleting.ops.replaceChildren(bOperation);
  deleted.resolve({ok: true}); await pendingDeletion;
  check(deleting.current === "B" && deleting.messages.firstChild === bMessage && deleting.ops.firstChild === bOperation && deleting.lastAnswer === "B的建议");
  check(deleting.input.value === "B保留的草稿" && !deleting.deleteButton.disabled && historyRefreshes === 1);
  console.log(JSON.stringify({check: "ai-ui-offline", ok: true, checks, browser_tested: false}));
})().catch(error => {console.error(error); process.exitCode = 1;});
