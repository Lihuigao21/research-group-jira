/* Exercise shipped manuscript UI with reordered API receipts; no browser claim. */
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");
const source = fs.readFileSync(path.resolve(__dirname, "../src/research_group/research_group/public/js/manuscripts.js"), "utf8");
let checks = 0;
const check = value => { assert.ok(value); checks++; };
const tick = () => new Promise(resolve => setImmediate(resolve));
const deferred = () => { let resolve, reject; const promise = new Promise((yes, no) => { resolve = yes; reject = no; }); return {promise, resolve, reject}; };

class Collection extends Array {
  remove() { this.forEach(element => element.remove()); return this; }
  toArray() { return Array.from(this); }
}
class Element {
  constructor(html = "<div>") {
    this.tag = (html.match(/^<(\w+)/) || [])[1] || "div";
    this.textValue = html.replace(/<[^>]*>/g, "");
    this.classes = new Set((html.match(/class=['"]([^'"]+)/) || ["", ""])[1].split(" ").filter(Boolean));
    this.childrenList = []; this.events = {}; this.props = {}; this.attributes = {}; this.value = "";
    this.visible = true; this.length = 1; this[0] = this;
    if (this.tag === "input") this.attributes.type = (html.match(/type=['"]([^'"]+)/) || ["", "text"])[1];
  }
  appendTo(parent) { this.parent = parent; parent.childrenList.push(this); return this; }
  append(value) { (typeof value === "string" ? new Element(value) : value).appendTo(this); return this; }
  text(value) { if (!arguments.length) return this.textValue + this.childrenList.map(child => child.text()).join(""); this.textValue = String(value); return this; }
  attr(key, value) { if (typeof key === "object") Object.assign(this.attributes, key); else if (arguments.length === 1) return this.attributes[key]; else this.attributes[key] = value; return this; }
  prop(key, value) { if (arguments.length === 1) return this.props[key]; this.props[key] = value; return this; }
  val(value) { if (!arguments.length) return this.value; this.value = value; return this; }
  empty() { this.childrenList.forEach(child => { child.parent = null; }); this.childrenList = []; this.textValue = ""; return this; }
  on(names, fn) { names.split(" ").forEach(name => { this.events[name] = fn; }); return this; }
  trigger(name) { return this.events[name] && this.events[name]({target: this}); }
  addClass(name) { name.split(" ").forEach(value => this.classes.add(value)); return this; }
  toggle(value) { this.visible = value; return this; }
  remove() { if (this.parent) this.parent.childrenList = this.parent.childrenList.filter(child => child !== this); this.parent = null; return this; }
  all() { return [this, ...this.childrenList.flatMap(child => child.all())]; }
  matches(selector) { return selector.startsWith(".") ? this.classes.has(selector.slice(1)) : this.tag === selector; }
  find(selector) { return new Collection(...this.all().slice(1).filter(child => child.matches(selector))); }
  children(selector) { return new Collection(...this.childrenList.filter(child => selector === ":not(h3)" ? child.tag !== "h3" : child.matches(selector))); }
  closest(selector) { let current = this; while (current) { if (current.matches(selector)) return current; current = current.parent; } return {length: 0}; }
  is(selector) { assert.equal(selector, ":visible"); let current = this; while (current) { if (!current.visible) return false; current = current.parent; } return true; }
}

function environment() {
  const state = {route: ["rg-manuscripts", "paper-a"], routes: [], errors: [], calls: []};
  const body = new Element("<body>"), root = new Element("<div class='rg-portal'>").appendTo(body);
  const UI = {
    section(parent, title) { return new Element("<section>").append(new Element("<h3>").text(title)).appendTo(parent); },
    text(parent, text, cls = "") { return new Element().text(text || "").addClass(cls).appendTo(parent); },
    field(parent, label, type = "text", value = "") { return new Element(type === "textarea" ? "<textarea>" : "<input>").attr("type", type).attr("aria-label", label).val(value).appendTo(parent); },
    select(parent, label, values, current = "") { const select = new Element("<select>").attr("aria-label", label).val(current).appendTo(parent); values.forEach(row => new Element("<option>").text(row.label).val(row.value).appendTo(select)); return select; },
    button(parent, label, fn) { const button = new Element("<button>").text(label).appendTo(parent); button.on("click", async () => { if (button.prop("disabled")) return; button.prop("disabled", true); try { await fn(); } catch (error) { state.errors.push(error.message); } finally { button.prop("disabled", false); } }); return button; },
    date(value) { return value || "none"; },
    link(route, label) { return new Element("<a>").text(label).attr("href", route); },
    go(route) { state.routes.push(route); },
    error(area, error) { state.errors.push(error.message); },
    async busy(fields, fn) { return fn(); },
    async shell() { return {body: root, data: {user: "student", portals: [{id: "student"}]}}; },
    async api(module, method, args = {}) { state.calls.push({module, method, args}); return state.reply(method, args); },
  };
  const window = {research_group: {PortalUI: UI}};
  const frappe = {get_route: () => state.route, ui: {Dialog: class {}}};
  const context = {window, frappe, console, FormData: class {append() {}}, fetch() { throw Error("unexpected fetch"); }, $: value => value instanceof Element ? value : new Element(value)};
  vm.createContext(context); vm.runInContext(source, context);
  const page = Object.create(window.research_group.ManuscriptsPage.prototype);
  page.epoch = 1; page.detailEpoch = 1; page.user = "student"; page.wrapper = root;
  return Object.assign(state, {body, root, UI, page, rg: window.research_group});
}

function button(root, label) { const found = root.all().find(element => element.tag === "button" && element.textValue === label); assert.ok(found, label); return found; }
async function authorize(env, manuscript = {author: env.page.user, reviewer: "teacher"}) {
  await env.page.selection({name: "immutable-version"}, env.root, manuscript);
  const original = env.root.all().find(element => element.attributes["aria-label"] === "实际将交给 AI 的原文（最多 6000 字）");
  original.val("明确授权的原文 <tag> & a < b");
  const consent = env.root.all().find(element => element.tag === "input" && element.attributes.type === "checkbox");
  consent.prop("checked", true);
  return {original, consent, send: button(env.root, "确认选段并在 AI 助手中生成建议")};
}

(async () => {
  // A fresh route takes precedence over the page object's previously selected paper.
  const route = environment(), opened = [];
  route.page.selected = "paper-a"; route.route = ["rg-manuscripts", "paper-b"];
  route.reply = async method => method === "list_manuscripts" ? {items: [{name: "paper-a", title: "A"}, {name: "paper-b", title: "B"}]} : {items: [], active_count: 0, active_limit: 20};
  route.page.open = async name => opened.push(name);
  await route.page.load();
  check(opened.join(",") === "paper-b");
  await route.page.load("newly-created-paper");
  check(opened[1] === "newly-created-paper");
  check(route.root.text().includes("本人授权选区") && route.root.text().includes("最多 20 个"));

  // A fully authorized live-page request passes only selection IDs to the AI route.
  const good = environment();
  good.reply = async method => method === "create_selection" ? {name: "explicit-source"} : method === "create_conversation" ? {name: "own-conversation"} : {run_id: "one-run"};
  const controls = await authorize(good);
  await controls.send.trigger("click");
  check(good.calls.map(call => call.method).join(",") === "create_selection,create_conversation,send_message");
  const args = good.calls[2].args;
  check(args.selections.length === 1 && args.selections[0] === "explicit-source");
  check(!args.prompt.includes(controls.original.val()));
  check(good.calls[1].args.portal === "student" && args.reasoning_effort === "medium");
  check(good.rg.pendingAIConversation === "own-conversation" && good.rg.pendingAIPortal === "student"
    && good.routes.join(",") === "rg-student-agent");

  const dualAuthor = environment(); dualAuthor.page.user = "dual-role";
  dualAuthor.reply = good.reply;
  const dualControls = await authorize(dualAuthor, {author: "dual-role", reviewer: "other-teacher"});
  await dualControls.send.trigger("click");
  check(dualAuthor.calls[1].args.portal === "student" && dualAuthor.routes.join(",") === "rg-student-agent");
  const reviewer = environment(); reviewer.page.user = "teacher"; reviewer.reply = good.reply;
  const reviewerControls = await authorize(reviewer, {author: "student", reviewer: "teacher"});
  await reviewerControls.send.trigger("click");
  check(reviewer.calls[1].args.portal === "teacher" && reviewer.routes.join(",") === "rg-teacher-agent");

  // Frappe leaves old pages attached. Both a new route and hidden ancestors must
  // suppress a late enqueue receipt, including its failure-recovery controls.
  for (const change of ["route", "paper", "hidden", "epoch", "detail", "original", "requirements", "consent", "reject"]) {
    const env = environment(), waiting = deferred();
    env.reply = async method => method === "create_selection" ? {name: "source"} : method === "create_conversation" ? {name: "conversation"} : waiting.promise;
    const fields = await authorize(env);
    const pending = fields.send.trigger("click"); await tick();
    check(env.calls.filter(call => call.method === "send_message").length === 1);
    if (change === "route" || change === "reject") env.route = ["rg-student-portal"];
    if (change === "paper") env.route = ["rg-manuscripts", "paper-b"];
    if (change === "hidden") env.root.visible = false;
    if (change === "epoch") env.page.epoch++;
    if (change === "detail") env.page.detailEpoch++;
    if (change === "original") fields.original.val("修改后的原文");
    if (change === "requirements") env.root.all().find(element => element.attributes["aria-label"] === "修改要求").val("修改后的要求");
    if (change === "consent") fields.consent.prop("checked", false);
    if (change === "reject") waiting.reject(Error("late transport failure")); else waiting.resolve({run_id: "one-run"});
    await pending;
    check(env.routes.length === 0);
    check(!env.root.text().includes("未收到本轮入队回执"));
  }

  // Leave before conversation creation finishes: no model enqueue is allowed.
  const beforeSend = environment(), conversation = deferred();
  beforeSend.reply = async method => method === "create_selection" ? {name: "source"} : conversation.promise;
  const beforeFields = await authorize(beforeSend);
  const pendingConversation = beforeFields.send.trigger("click"); await tick();
  beforeSend.route = ["rg-teacher-portal"]; conversation.resolve({name: "conversation"}); await pendingConversation;
  check(!beforeSend.calls.some(call => call.method === "send_message") && !beforeSend.routes.length);

  const beforeConversation = environment(), selected = deferred();
  beforeConversation.reply = async () => selected.promise;
  const initial = await authorize(beforeConversation);
  const pendingSelection = initial.send.trigger("click"); await tick();
  beforeConversation.root.visible = false; selected.resolve({name: "source"}); await pendingSelection;
  check(beforeConversation.calls.map(call => call.method).join(",") === "create_selection");

  // The actionable manager includes expired sources still backing saved advice,
  // paginates, and refreshes collaboration history immediately after revocation.
  const manager = environment(), managedArea = new Element("<section>").appendTo(manager.root);
  new Element("<h3>").text("本人授权选区").appendTo(managedArea);
  const rows = [{name: "expired-saved-source", manuscript_title: "本人论文", version_number: 1, original_text: "本人的到期原文 <tag>", effective_status: "expired", expires_at: "2026-11-08", saved_suggestions: 1},
    {name: "active-source", manuscript_title: "另一本人论文", version_number: 2, original_text: "本人有效原文", effective_status: "active", expires_at: "2026-11-09", saved_suggestions: 0}];
  let revoked = false, refreshed = 0;
  manager.reply = async (method, args) => {
    if (method === "revoke_selection") { assert.equal(args.name, "expired-saved-source"); revoked = true; return {status: "revoked"}; }
    assert.equal(method, "list_selections");
    const available = revoked ? rows.slice(1) : rows;
    return {items: available.slice(args.offset, args.offset + 1), has_more: available.length > args.offset + 1, active_count: 20, active_limit: 20};
  };
  await manager.page.selections(managedArea, () => true, async () => { refreshed++; });
  check(managedArea.text().includes("20 / 20") && managedArea.text().includes("已自然到期"));
  check(managedArea.text().includes("已保存 AI 建议 1 条") && managedArea.text().includes("本人的到期原文 <tag>"));
  await button(managedArea, "读取更多本人选区").trigger("click");
  check(managedArea.text().includes("另一本人论文") && manager.calls[1].args.offset === 1);
  await button(managedArea, "撤销此选区授权").trigger("click");
  check(refreshed === 1 && revoked);
  check(!managedArea.text().includes("本人的到期原文") && managedArea.text().includes("本人有效原文"));

  // Unavailable legacy history is identified explicitly; metadata is text-only.
  const history = environment();
  history.reply = async () => ({manuscript: {title: "论文", author: "student", reviewer: "teacher"}, versions: [], reviews: [
    {name: "review", kind: "teacher_comment", status: "closed", author: "teacher", content: "正文", status_history: [{actor: "student", at: "2026-11-08", before: "open", after: "addressed", action: "reply"}]},
    {name: "legacy", kind: "teacher_comment", status: "open", author: "teacher", content: "旧正文", status_history: [], status_history_notice: "旧记录未保留早期状态变化，仅展示升级后实际记录的事件。"},
  ]});
  await history.page.open("paper-a", history.root);
  check(history.root.text().includes("查看状态变化记录") && history.root.text().includes("student"));
  check(history.root.text().includes("待处理 → 已回复或处理") && history.root.text().includes("旧记录未保留早期状态变化"));
  console.log(JSON.stringify({ok: true, checks, browser_layout_tested: false}));
})().catch(error => { console.error(error); process.exitCode = 1; });
