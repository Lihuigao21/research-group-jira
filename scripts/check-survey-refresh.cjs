/* Deterministic async regression checks, not a browser/layout acceptance test. */
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");
const source = path.resolve(__dirname, "../src/research_group/research_group/public/js/surveys.js");
const sourceCode = fs.readFileSync(source, "utf8");

class Elements extends Array {
  val(value) { return arguments.length ? this[0].val(value) : this[0]?.val(); }
  prop(name, value) { this.forEach(element => element.prop(name, value)); return this; }
  map(callback) { return new Elements(...Array.from(this, (element, index) => callback(index, element))); }
  get() { return Array.from(this); }
}
class Element {
  constructor(html = "") {
    this.tag = (html.match(/^<(\w+)/) || [])[1] || "div";
    this.textValue = html.replace(/<[^>]*>/g, "");
    this.value = "";
    this.children = [];
    this.attributes = {};
    this.properties = {};
    this.events = {};
    this.commits = 0;
    this.appends = 0;
  }
  text(value) {
    if (!arguments.length) return this.textValue + this.children.map(child => child.text()).join("");
    this.textValue = String(value); return this;
  }
  appendTo(parent) { parent.children.push(this); return this; }
  append(items) { this.children.push(...(Array.isArray(items) ? items : [items])); this.appends++; return this; }
  contents() { return this.children.slice(); }
  empty() { this.textValue = ""; this.children = []; this.commits++; return this; }
  on(name, callback) { this.events[name] = callback; return this; }
  attr(name, value) {
    if (typeof name === "object") { Object.assign(this.attributes, name); return this; }
    if (arguments.length === 1) return this.attributes[name];
    this.attributes[name] = value; return this;
  }
  prop(name, value) { if (arguments.length === 1) return this.properties[name]; this.properties[name] = value; return this; }
  val(value) { if (!arguments.length) return this.value; this.value = value; return this; }
  all() { return [this, ...this.children.flatMap(child => child.all())]; }
  find(selector) {
    return new Elements(...this.all().filter(child => selector === "input:checked"
      ? child.tag === "input" && child.properties.checked : child.tag === selector));
  }
}
function environment(isSecretary = true, code = sourceCode) {
  const env = {requests: [], handlers: {}, confirmations: [], opens: [], routes: [], dialogs: []};
  const frappe = {
    call(options) { return new Promise((resolve, reject) => env.requests.push({...options, resolve, reject})); },
    ui: {form: {on(name, handler) { env.handlers[name] = handler; }}, Dialog: class {
      constructor(options) {
        this.options = options;
        this.fields_dict = {questions: {$wrapper: new Element()}};
        env.dialogs.push(this);
      }
      show() { this.shown = true; }
      hide() { this.hidden = true; }
      disable_primary_action() { this.disabled = true; }
      enable_primary_action() { this.disabled = false; }
    }},
    confirm(message, callback) { env.confirmations.push(callback); },
    datetime: {str_to_user(value) { return value; }}, session: {user: "member@example.invalid"},
    set_route(...args) { env.routes.push(args); }, show_alert() {},
  };
  const window = {research_group: {has_role() { return isSecretary; }}, open(...args) { env.opens.push(args); }};
  vm.runInNewContext(code, {window, frappe, $: html => new Element(html)}, {filename: source});
  env.resolve = (index, message) => env.requests[index].resolve({message});
  env.reject = (index, message) => env.requests[index].reject(new Error(message));
  return env;
}
function form(doctype, name, extra = {}) {
  const fields = Object.fromEntries(["results", "participation", "answer_preview"].map(key => [key, {$wrapper: new Element()}]));
  return {
    doc: {doctype, name, ...extra}, fields, buttons: [], reloads: [], dirty: false, saves: 0,
    is_new() { return Boolean(this.doc.__islocal); }, is_dirty() { return this.dirty; },
    toggle_display() {}, set_df_property() {}, enable_save() {}, disable_save() {},
    set_intro(value) { this.intro = value; },
    get_field(name) { return fields[name]; },
    add_custom_button(label, callback) { this.buttons.push({label, callback, record: this.doc.name}); },
    async save() { this.saves++; this.dirty = false; },
    async reload_doc() { this.reloads.push(this.doc.name); },
  };
}
function survey(name, status = "已发布") {
  return {name, status, kind: "问卷", accepting_responses: status === "已发布", deadline: null,
    questions: [{name: "q", title: name + "题目", question_type: "简答", required: 0, options: []}], my_response: null};
}
function statistics(name, count) {
  return {survey: {name}, submitted_count: count, active_member_count: 5, active_submitted_count: count,
    pending_count: 0, pending_members: [],
    questions: [{name: "q", title: name + "题目", question_type: "简答", answered_count: count, answers: []}],
    responses: Array.from({length: count}, (_, index) => ({respondent_name: name + "实名成员" + index,
      respondent: `${name}.${index}@example.invalid`, updated_at: "2030-01-01 00:00:00", answers: {q: name + "秘密答卷"}}))};
}
const drain = () => new Promise(setImmediate);
function button(frm, label) { return frm.buttons.filter(item => item.label === label).at(-1).callback; }
async function render(env, frm, data, count = 0) {
  const index = env.requests.length;
  const pending = env.handlers[frm.doc.doctype].refresh(frm);
  env.resolve(index, data);
  await drain();
  if (env.requests.length > index + 1) env.resolve(index + 1, statistics(data.name, count));
  await pending;
}

async function dialogSubmission(editing, code = sourceCode) {
  const env = environment(false, code), frm = form("RG Survey", "A");
  const data = survey("A");
  data.questions = [
    {name: "single", title: "单选题", question_type: "单选", required: 1, options: ["甲", "乙"]},
    {name: "multiple", title: "多选题", question_type: "多选", required: 1, options: ["早", "午", "晚"]},
    {name: "text", title: "简答题", question_type: "简答", required: 1, options: []},
  ];
  if (editing) data.my_response = {updated_at: "2030-01-01 00:00:00",
    answers: {single: "甲", multiple: ["午", "晚"], text: "原简答内容"}};
  await render(env, frm, data);
  frm.fields.participation.$wrapper.find("button")[0].events.click();
  const dialog = env.dialogs[0];
  assert.ok(dialog.shown);
  const sections = dialog.fields_dict.questions.$wrapper.children;
  assert.equal(sections.length, 3);
  for (const [index, role] of [[0, "radiogroup"], [1, "group"]]) {
    const section = sections[index];
    assert.equal(section.attributes.role, role);
    const label = section.all().find(element => element.attributes.id === section.attributes["aria-labelledby"]);
    assert.ok(label && label.text().includes("必填"), "choice group must reference its visible required label");
  }
  const single = sections[0].find("input"), multiple = sections[1].find("input");
  assert.deepEqual(single.filter(input => input.properties.checked).map((_, input) => input.value).get(), editing ? ["甲"] : []);
  assert.deepEqual(multiple.filter(input => input.properties.checked).map((_, input) => input.value).get(), editing ? ["午", "晚"] : []);
  assert.equal(sections[2].find("textarea").val(), editing ? "原简答内容" : "");
  single.prop("checked", false);
  single[1].prop("checked", true);
  multiple.prop("checked", false);
  multiple[0].prop("checked", true);
  multiple[editing ? 1 : 2].prop("checked", true);
  const answer = editing ? "修改后的简答 <b>仍为文本</b>" : "首次简答\n第二行";
  sections[2].find("textarea").val(answer);
  // Exercise the real dialog callback, not a reconstructed request serializer.
  const index = env.requests.length;
  const submitting = dialog.options.primary_action();
  try {
    assert.equal(env.requests[index].method, "research_group.surveys.submit_response");
    assert.equal(env.requests[index].args.survey, "A");
    assert.equal(frm.doc.name, "A");
    const payload = JSON.parse(env.requests[index].args.answers);
    assert.ok(Array.isArray(payload.multiple), "multi-select answer must remain an array");
    assert.equal(typeof payload.single, "string");
    assert.equal(typeof payload.text, "string");
    assert.deepEqual(payload, {single: "乙", multiple: editing ? ["早", "午"] : ["早", "晚"], text: answer});
    assert.equal(dialog.disabled, true);
  } finally {
    env.resolve(index, {name: "response-A", survey: "A"});
    await submitting;
  }
  assert.equal(dialog.hidden, true);
  assert.equal(dialog.disabled, false);
  assert.deepEqual(frm.reloads, ["A"]);
}

async function main() {
  // Both result requests have begun before the newest one finishes first.
  {
    const env = environment(), frm = form("RG Survey", "A");
    const older = env.handlers["RG Survey"].refresh(frm);
    env.resolve(0, survey("A")); await drain();
    const newer = env.handlers["RG Survey"].refresh(frm);
    env.resolve(2, survey("A")); await drain();
    env.resolve(3, statistics("A", 2)); await newer;
    env.resolve(1, statistics("A", 1)); await older;
    const wrapper = frm.fields.results.$wrapper;
    assert.ok(wrapper.text().includes("已收 2 份答卷"));
    assert.ok(!wrapper.text().includes("已收 1 份答卷"));
    assert.equal(wrapper.text().split("实名答卷明细").length - 1, 1);
    assert.equal(wrapper.commits, 3); // two immediate clears and the newest replacement
    assert.equal(wrapper.appends, 1);
    assert.equal(frm.buttons.length, 3);
  }
  // An older get_survey response cannot change controls or launch more work.
  {
    const env = environment(), frm = form("RG Survey", "A");
    const older = env.handlers["RG Survey"].refresh(frm);
    const newer = env.handlers["RG Survey"].refresh(frm);
    env.resolve(1, survey("A")); await drain();
    env.resolve(2, statistics("A", 2)); await newer;
    env.resolve(0, survey("A", "草稿")); await older;
    assert.equal(env.requests.length, 3);
    assert.ok(!frm.buttons.some(item => item.label === "发布问卷"));
    assert.ok(frm.intro.includes("已发布"));
  }
  // Record switches invalidate old toolbar, participation and confirm callbacks.
  {
    const env = environment(), frm = form("RG Survey", "A");
    await render(env, frm, survey("A"));
    const oldClose = button(frm, "提前关闭"), oldExport = button(frm, "导出 CSV");
    const oldRefresh = button(frm, "刷新填写进度");
    const oldFill = frm.fields.participation.$wrapper.find("button")[0].events.click;
    frm.doc = {doctype: "RG Survey", name: "B"};
    await render(env, frm, survey("B"));
    const calls = env.requests.length;
    oldClose(); oldExport(); oldFill(); await oldRefresh();
    assert.equal(env.requests.length, calls);
    assert.equal(env.confirmations.length, 0);
    assert.equal(env.opens.length, 0);
    assert.equal(env.dialogs.length, 0);
    assert.equal(frm.reloads.length, 0);
    button(frm, "导出 CSV")();
    assert.ok(env.opens[0][0].endsWith("survey=B"));
    button(frm, "提前关闭")();
    frm.doc = {doctype: "RG Survey", name: "C"};
    await env.confirmations[0]();
    assert.equal(env.requests.length, calls);
  }
  // A mutation completes for its captured record without reloading another one.
  {
    const env = environment(), frm = form("RG Survey", "A");
    await render(env, frm, survey("A"));
    button(frm, "提前关闭")();
    const closing = env.confirmations[0]();
    assert.equal(env.requests[2].args.name, "A");
    frm.doc = {doctype: "RG Survey", name: "B"};
    env.resolve(2, {name: "A", status: "已关闭"}); await closing;
    assert.equal(frm.reloads.length, 0);
  }
  // A delayed answer-preview request cannot paint another response's content.
  {
    const env = environment(false), frm = form("RG Survey Response", "RA", {
      survey: "A", respondent: "member@example.invalid", answers_json: JSON.stringify({q: "答卷甲"}),
    });
    const older = env.handlers["RG Survey Response"].refresh(frm);
    frm.doc = {doctype: "RG Survey Response", name: "RB", survey: "B",
      respondent: "member@example.invalid", answers_json: JSON.stringify({q: "答卷乙"})};
    const newer = env.handlers["RG Survey Response"].refresh(frm);
    env.resolve(1, survey("B")); await newer;
    env.resolve(0, survey("A")); await older;
    const text = frm.fields.answer_preview.$wrapper.text();
    assert.ok(text.includes("答卷乙") && !text.includes("答卷甲"));
    assert.equal(frm.buttons.length, 2);
    const back = button(frm, "返回问卷"), edit = button(frm, "修改本人答卷");
    frm.doc = {doctype: "RG Survey Response", name: "RC", survey: "C"};
    back(); edit();
    assert.equal(env.routes.length, 0);
    assert.equal(env.dialogs.length, 0);
  }
  // A transition to a new unsaved form also invalidates an older read.
  {
    const env = environment(), frm = form("RG Survey", "A");
    const older = env.handlers["RG Survey"].refresh(frm);
    frm.doc = {doctype: "RG Survey", name: "new-survey", __islocal: 1};
    await env.handlers["RG Survey"].refresh(frm);
    env.resolve(0, survey("A")); await older;
    assert.equal(env.requests.length, 1);
    assert.equal(frm.fields.results.$wrapper.commits, 2);
    assert.equal(frm.fields.results.$wrapper.appends, 0);
    assert.equal(frm.buttons.length, 0);
  }
  // Frappe may resolve a rejected save without clearing the dirty state.
  {
    const env = environment(), frm = form("RG Survey", "A");
    await render(env, frm, survey("A", "草稿"));
    frm.dirty = true;
    frm.doc.title = "未保存的题目修改";
    frm.doc.questions = [{title: "仍需修正的输入"}];
    const edited = frm.doc.questions;
    frm.save = async function() { this.saves++; return this; };
    const calls = env.requests.length;
    button(frm, "发布问卷")();
    await env.confirmations[0]();
    assert.equal(frm.saves, 1);
    assert.equal(frm.is_dirty(), true);
    assert.equal(frm.doc.questions, edited);
    assert.equal(frm.doc.title, "未保存的题目修改");
    assert.equal(env.requests.length, calls);
    assert.equal(frm.reloads.length, 0);
  }
  // A successful save may replace the doc while keeping the same record current.
  {
    const env = environment(), frm = form("RG Survey", "A");
    await render(env, frm, survey("A", "草稿"));
    frm.dirty = true;
    frm.save = async function() {
      this.saves++; this.dirty = false; this.doc = {...this.doc}; this._rg_survey_refresh_sequence++;
    };
    button(frm, "发布问卷")();
    const publishing = env.confirmations[0]();
    await drain();
    assert.equal(frm.saves, 1);
    assert.equal(env.requests[2].method, "research_group.surveys.publish_survey");
    assert.equal(env.requests[2].args.name, "A");
    env.resolve(2, {name: "A", status: "已发布"}); await publishing;
    assert.equal(frm.is_dirty(), false);
    assert.deepEqual(frm.reloads, ["A"]);
  }
  // A resolved save for a form that switched records cannot publish either one.
  {
    const env = environment(), frm = form("RG Survey", "A");
    await render(env, frm, survey("A", "草稿"));
    frm.dirty = true;
    frm.save = async function() { this.saves++; this.dirty = false; this.doc = {doctype: "RG Survey", name: "B"}; };
    const calls = env.requests.length;
    button(frm, "发布问卷")(); await env.confirmations[0]();
    assert.equal(env.requests.length, calls);
    assert.equal(frm.reloads.length, 0);
  }
  // A failed read after switching records leaves neither old participation nor old results.
  {
    const env = environment(), frm = form("RG Survey", "A");
    const data = survey("A");
    data.my_response = {updated_at: "2030-01-01", answers: {q: "A本人秘密"}};
    await render(env, frm, data, 1);
    assert.ok(frm.fields.results.$wrapper.text().includes("A实名成员"));
    const older = env.handlers["RG Survey"].refresh(frm);
    env.resolve(2, data); await drain();
    assert.ok(frm.fields.participation.$wrapper.text().includes("A本人秘密"));
    frm.doc = {doctype: "RG Survey", name: "B"};
    const newer = env.handlers["RG Survey"].refresh(frm);
    const rejected = assert.rejects(newer, /B read denied/);
    assert.equal(frm.fields.participation.$wrapper.text(), "");
    assert.equal(frm.fields.results.$wrapper.text(), "");
    env.reject(4, "B read denied"); await rejected;
    env.resolve(3, statistics("A", 1)); await older;
    assert.equal(frm.fields.participation.$wrapper.text(), "");
    assert.equal(frm.fields.results.$wrapper.text(), "");
  }
  // A failed statistics call cannot leave another survey's real-name rows visible.
  {
    const env = environment(), frm = form("RG Survey", "A");
    await render(env, frm, survey("A"), 1);
    assert.ok(frm.fields.results.$wrapper.text().includes("A秘密答卷"));
    frm.doc = {doctype: "RG Survey", name: "B"};
    const pending = env.handlers["RG Survey"].refresh(frm);
    const rejected = assert.rejects(pending, /B results denied/);
    assert.equal(frm.fields.results.$wrapper.text(), "");
    assert.equal(frm.fields.participation.$wrapper.text(), "");
    env.resolve(2, survey("B")); await drain();
    env.reject(3, "B results denied"); await rejected;
    assert.equal(frm.fields.results.$wrapper.text(), "");
    assert.ok(!frm.fields.participation.$wrapper.text().includes("A"));
  }
  // Response previews are cleared before a switched record's failing read.
  {
    const env = environment(false), frm = form("RG Survey Response", "RA", {
      survey: "A", respondent: "member@example.invalid", answers_json: JSON.stringify({q: "A答卷秘密"}),
    });
    await render(env, frm, survey("A"));
    assert.ok(frm.fields.answer_preview.$wrapper.text().includes("A答卷秘密"));
    frm.doc = {doctype: "RG Survey Response", name: "RB", survey: "B"};
    const pending = env.handlers["RG Survey Response"].refresh(frm);
    const rejected = assert.rejects(pending, /B response denied/);
    assert.equal(frm.fields.answer_preview.$wrapper.text(), "");
    env.reject(1, "B response denied"); await rejected;
    assert.equal(frm.fields.answer_preview.$wrapper.text(), "");
  }
  await dialogSubmission(false);
  await dialogSubmission(true);
  // This review mutation must be caught by the real primary_action payload check.
  const scalarMutation = sourceCode.replace(
    'question.question_type === "多选" ? selected : (selected[0] || "")', '(selected[0] || "")');
  assert.notEqual(scalarMutation, sourceCode);
  await assert.rejects(dialogSubmission(false, scalarMutation), {
    name: "AssertionError", message: "multi-select answer must remain an array",
  });
  process.stdout.write(JSON.stringify({check: "survey-refresh-races", ok: true, scenarios: 15,
    dialog_payload_scenarios: 2, scalar_mutation_rejected: true,
    actual_source_executed: true, frappe_runtime_tested: false, browser_layout_tested: false}) + "\n");
}
main().catch(error => { process.stderr.write(error.stack + "\n"); process.exitCode = 1; });
