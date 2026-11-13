/* Execute the shipped source against controlled, reordered API receipts.
 * These checks exercise behavior; they do not claim browser/layout acceptance.
 */
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");
const app = path.resolve(__dirname, "../src/research_group/research_group/public/js");
let checks = 0;
const check = value => { assert.ok(value); checks++; };
const deferred = () => { let resolve, reject; const promise = new Promise((done, fail) => { resolve = done; reject = fail; }); return {promise, resolve, reject}; };

class Collection extends Array {
  each(fn) { this.forEach((el, i) => fn(i, el)); return this; }
  toArray() { return [...this]; }
  remove() { this.forEach(el => el.remove()); return this; }
  first() { return this[0]; }
  removeClass(name) { this.forEach(el => el.removeClass(name)); return this; }
  addClass(name) { this.forEach(el => el.addClass(name)); return this; }
  attr(key, value) { this.forEach(el => el.attr(key, value)); return this; }
}
class Element {
  constructor(html = "") {
    this.tag = (html.match(/^<(\w+)/) || [])[1] || "div";
    this.textValue = html.replace(/<[^>]*>/g, "");
    this.classes = new Set((html.match(/class=['"]([^'"]+)/) || ["", ""])[1].split(" ").filter(Boolean));
    this.childrenList = []; this.events = {}; this.props = {}; this.attributes = {}; this.value = ""; this.length = 1;
    if (this.tag === "input") this.attributes.type = (html.match(/type=['"]([^'"]+)/) || ["", "text"])[1];
    this[0] = this;
  }
  appendTo(parent) { this.parent = parent; parent.childrenList.push(this); return this; }
  prependTo(parent) { this.parent = parent; parent.childrenList.unshift(this); return this; }
  append(...values) { values.forEach(value => { if (typeof value === "string") value = new Element(value); value.appendTo(this); }); return this; }
  prepend(value) { value.parent = this; this.childrenList.unshift(value); return this; }
  text(value) { if (!arguments.length) return this.textValue + this.childrenList.map(el => el.text()).join(""); this.textValue = String(value); return this; }
  attr(key, value) { if (typeof key === "object") { Object.assign(this.attributes, key); return this; } if (arguments.length === 1) return this.attributes[key]; this.attributes[key] = value; return this; }
  prop(key, value) { if (arguments.length === 1) return this.props[key]; this.props[key] = value; return this; }
  val(value) { if (!arguments.length) return this.value; this.value = value; return this; }
  empty() { this.childrenList = []; this.textValue = ""; return this; }
  on(name, fn) { name.split(" ").forEach(key => { this.events[key] = fn; }); return this; }
  trigger(name) { return this.events[name] && this.events[name]({target: this}); }
  addClass(name) { name.split(" ").forEach(value => this.classes.add(value)); return this; }
  removeClass(name) { this.classes.delete(name); return this; }
  toggleClass(name, on) { if (on) this.classes.add(name); else this.classes.delete(name); return this; }
  toggle(value) { this.visible = value; return this; }
  is(selector) { if (selector !== ":visible") throw Error("unsupported selector " + selector); return this.visible !== false && (!this.parent || this.parent.is(selector)); }
  remove() { if (this.parent) this.parent.childrenList = this.parent.childrenList.filter(value => value !== this); return this; }
  replaceWith(el) { el.parent = this.parent; if (this.parent) this.parent.childrenList[this.parent.childrenList.indexOf(this)] = el; return el; }
  all() { return [this, ...this.childrenList.flatMap(child => child.all())]; }
  matches(selector) { return selector.startsWith(".") ? this.classes.has(selector.slice(1)) : this.tag === selector; }
  find(selector) { return new Collection(...this.all().slice(1).filter(el => el.matches(selector))); }
  children(selector) {
    return new Collection(...this.childrenList.filter(el => !selector ? true : selector === ":not(h3)" ? el.tag !== "h3" : el.matches(selector)));
  }
  closest(selector) { let el = this; while (el) { if (el.matches(selector)) return el; el = el.parent; } return {length: 0}; }
}
function environment(files = ["portals", "accounts", "meals", "manuscripts"]) {
  const env = {routes: [], replacements: [], calls: [], dialogs: [], route: ["rg-manuscripts"]};
  const document = {readyState: "complete", body: new Element("<body>"), querySelectorAll: () => [], querySelector: () => null, getElementById: () => null, addEventListener() {}};
  const window = {research_group: {}, location: {pathname: "/desk/rg-meals", hash: "", replace(value) {env.replacements.push(value);}}, addEventListener() {}, crypto: {randomUUID: () => "request-fixed-uuid"}};
  const frappe = {utils: {escape_html: value => value.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;").replace(/"/g, "&quot;")},
    datetime: {str_to_user: value => value}, set_route: (...route) => env.routes.push(route), get_route: () => env.route,
    call: async options => { env.calls.push(options); return {message: {}}; }, ui: {Dialog: class {constructor(options) {this.options = options; env.dialogs.push(this);} show() {} }}};
  const context = {window, document, frappe, console, Date, Math, FormData: class {append() {}}, fetch() {throw Error("unexpected fetch");}, $: value => value instanceof Element ? value : new Element(value)};
  vm.createContext(context);
  files.forEach(file => vm.runInContext(fs.readFileSync(path.join(app, file + ".js"), "utf8"), context));
  Object.assign(env, {context, window, document, frappe, rg: window.research_group, UI: window.research_group.PortalUI});
  return env;
}
const tick = () => new Promise(resolve => setImmediate(resolve));

(async () => {
  const links = environment(["portals"]);
  check(links.UI.link("Form/RG Topic/论文 \"<script>", "<img src=x>").includes("/desk/rg-topic/"));
  check(!links.UI.link("Form/RG Topic/论文 \"<script>", "<img src=x>").includes("<script>"));
  check(links.UI.link("List/RG Survey", "问卷").includes("/desk/rg-survey"));
  const old = deferred(); links.UI.api = () => old.promise;
  let touched = false;
  const wrapper = {find() {touched = true; throw Error("stale shell touched DOM");}};
  const pendingShell = links.UI.shell(wrapper, "旧页", null, () => false);
  old.resolve({}); check(await pendingShell === null); check(!touched);
  const active = new Element(), locked = new Element().prop("disabled", true), busyReply = deferred();
  const busy = links.UI.busy([active, locked], () => busyReply.promise);
  check(active.prop("disabled") && locked.prop("disabled"));
  busyReply.reject(new Error("lost acknowledgement")); await assert.rejects(busy);
  check(!active.prop("disabled") && locked.prop("disabled"));

  // Website inclusion never assumes Desk boot exists; the native forgot link
  // is replaced before it can send a different Guest reset request.
  const loginClasses = new Set();
  const loginContext = {window: {location: {pathname: "/login", hash: "#forgot", replace(value) {this.target = value;}}, addEventListener() {}},
    document: {readyState: "complete", body: {classList: {add(name) {loginClasses.add(name);}}}}};
  vm.createContext(loginContext); vm.runInContext(fs.readFileSync(path.join(app, "accounts.js"), "utf8"), loginContext);
  check(loginContext.window.location.target === "/rg_join?mode=reset");
  check(loginClasses.has("rg-web"));

  const accounts = environment(), area = new Element();
  const instance = Object.create(accounts.rg.AccountsPage.prototype);
  let approval;
  instance.load = async () => {};
  accounts.UI.api = async (module, method, args) => { approval = {module, method, args}; return {}; };
  instance.applications(area, {registrations: [{name: "registration-1", display_name: "虚构学生", email: "student@m.fudan.edu.cn",
    student_id: "12345", status: "awaiting_approval", verified_at: "2026-11-08"}]});
  const approve = area.all().find(el => el.textValue === "核实并批准入组");
  check(!!approve); await approve.trigger("click");
  check(approval.method === "review_registration" && approval.args.decision === "approve");
  const mail = new Element();
  instance.mailStatus(mail, [{template: "password_reset", status_label: "SMTP已接受，未验证实际收件", attempts: 1, created_at: "2026-11-08"}]);
  check(mail.text().includes("SMTP已接受，未验证实际收件") && !mail.text().includes("发送成功"));

  const messages = environment(), messageRoot = new Element("<div class='rg-portal'></div>"), previewReply = deferred();
  messages.UI.api = async (module, method) => {
    if (method === "recipient_options") return {roles: [], members: [{user: "one", display_name: "本人", email: "one@m.fudan.edu.cn", eligible: true, roles: []}]};
    if (method === "list_messages") return {messages: []};
    if (method === "preview_message") return previewReply.promise;
    throw Error("unexpected " + method);
  };
  await messages.rg.renderMessages(messageRoot);
  const previewButton = messageRoot.all().find(el => el.textValue === "预览名单与正文");
  const draft = messageRoot.all().find(el => el.tag === "textarea");
  const request = previewButton.trigger("click"); await tick();
  draft.val("用户在旧预览等待期间修改了正文"); draft.trigger("input");
  previewReply.resolve({name: "old-preview", subject: "旧主题", body: "旧正文", recipient_count: 1, recipients: [], confirmation_digest: "digest"});
  await request;
  check(!messageRoot.all().some(el => el.textValue === "本人确认，逐人发送"));

  const confirmation = environment(), confirmationRoot = new Element("<div class='rg-portal'></div>");
  let oldConfirmation = deferred(), heldPreview = "preview-1", previews = 0, historyReads = 0;
  confirmation.UI.api = async (module, method, args) => {
    if (method === "recipient_options") return {roles: [], members: []};
    if (method === "list_messages") { historyReads++; return {messages: []}; }
    if (method === "preview_message") {
      previews++;
      return {name: "preview-" + previews, subject: "预览主题-" + previews, body: "预览正文-" + previews,
        recipient_count: 1, recipients: [], confirmation_digest: "digest-" + previews};
    }
    if (method === "confirm_message") return args.name === heldPreview ? oldConfirmation.promise : {events: []};
    throw Error("unexpected " + method);
  };
  await confirmation.rg.renderMessages(confirmationRoot);
  const makePreview = confirmationRoot.all().find(el => el.textValue === "预览名单与正文");
  await makePreview.trigger("click");
  const confirmOld = confirmationRoot.all().find(el => el.textValue === "本人确认，逐人发送");
  const confirming = confirmOld.trigger("click"); await tick();
  confirmationRoot.all().find(el => el.tag === "textarea").val("新的正文").trigger("input");
  await makePreview.trigger("click");
  oldConfirmation.resolve({events: [{recipient_user: "old-recipient", status: "queued"}]});
  await confirming;
  check(confirmationRoot.text().includes("预览主题-2") && !confirmationRoot.text().includes("消息已发布"));
  check(!confirmationRoot.text().includes("old-recipient") && historyReads === 1);
  await confirmationRoot.all().find(el => el.textValue === "本人确认，逐人发送").trigger("click");
  check(confirmationRoot.text().includes("消息已发布") && historyReads === 2);
  oldConfirmation = deferred(); heldPreview = "preview-3";
  await makePreview.trigger("click");
  const oldFailure = confirmationRoot.all().find(el => el.textValue === "本人确认，逐人发送").trigger("click");
  await tick();
  confirmationRoot.all().find(el => el.tag === "textarea").val("再次修改正文").trigger("input");
  await makePreview.trigger("click");
  oldConfirmation.reject(new Error("old confirmation receipt unavailable")); await oldFailure;
  check(confirmationRoot.text().includes("预览主题-4") && !confirmationRoot.text().includes("old confirmation receipt"));
  check(confirmationRoot.children(".rg-portal-error").length === 0 && historyReads === 2);

  const meals = environment(), mealArea = new Element("<div class='rg-portal'></div>");
  const meal = Object.create(meals.rg.MealsPage.prototype);
  meal.wrapper = {}; meal.request = "previous-id"; meal.roundEpoch = 0;
  const round = {name: "round-1", title: "虚构餐轮", status: "已发布", accepting_orders: true, can_manage: false, my_order: null,
    menu: {items: [{id: "meal-1", category: "meal", name: "主餐", priceCents: 5100, description: "完整配菜", staples: ["米饭", "面"],
      imageUrl: "/assets/research_group/menu/2026-11-07/meal-1.jpg"}]}};
  const submitReply = deferred(); let submits = 0;
  meals.UI.api = async (module, method) => { if (method === "get_round") return round; if (method === "submit_order") {submits++; return submitReply.promise;} throw Error(method); };
  await meal.round(round.name, mealArea);
  const image = mealArea.all().find(el => el.tag === "img"), originalCard = image.parent;
  check(image.attr("src") === round.menu.items[0].imageUrl && !originalCard.classes.has("rg-meal-noimg"));
  await image.trigger("error");
  check(!mealArea.all().includes(image) && mealArea.all().includes(originalCard) && originalCard.classes.has("rg-meal-noimg"));
  const controls = mealArea.all().filter(el => el.tag === "select");
  check(controls.length === 2 && controls.every(control => originalCard.all().includes(control) && !control.prop("disabled")));
  const ack = mealArea.all().find(el => el.tag === "input" && el.attributes.type === "checkbox");
  controls[0].val("1"); controls[0].trigger("change"); ack.prop("checked", true);
  controls[1].val("面"); controls[1].trigger("change");
  check(!ack.prop("checked") && meal.request === null);
  ack.prop("checked", true);
  const submit = mealArea.all().find(el => el.textValue === "提交本人订单");
  const firstSubmit = submit.trigger("click"); const secondSubmit = submit.trigger("click"); await tick();
  check(submits === 1);
  const remarks = mealArea.all().find(el => el.tag === "textarea");
  check(controls.every(control => control.prop("disabled")) && remarks.prop("disabled") && ack.prop("disabled"));
  const frozenQuantity = controls[0].val();
  if (!controls[0].prop("disabled")) controls[0].val("2").trigger("change");
  check(controls[0].val() === frozenQuantity && meal.request === "meal-request-fixed-uuid");
  submitReply.resolve({name: "saved-one-order"}); await firstSubmit; await secondSubmit;

  // Match the real page: errors live at the portal root, while round() redraws
  // only a nested area. Validation and a lost receipt keep the draft/consent;
  // a successful retry must remove the root error and recover the same request.
  const retry = environment(), retryRoot = new Element("<div class='rg-portal'></div>");
  const retryArea = new Element().appendTo(retryRoot);
  const otherRoot = new Element("<div class='rg-portal'></div>");
  retry.UI.error(otherRoot, new Error("another page's error"));
  const retryMeal = Object.create(retry.rg.MealsPage.prototype), retryReply = deferred(), sent = [];
  let savedOrder = null;
  retry.UI.api = async (module, method, args) => {
    if (method === "get_round") return {...round, my_order: savedOrder};
    sent.push(JSON.parse(JSON.stringify(args)));
    if (sent.length === 1) return retryReply.promise;
    savedOrder = {name: "same-saved-order", status: "有效", total_cents: 5100, revision: 1, remarks: args.remarks,
      items: [{id: "meal-1", name: "主餐", quantity: 1, staple: "米饭", subtotalCents: 5100}]};
    return savedOrder;
  };
  await retryMeal.round(round.name, retryArea);
  const retryQuantity = retryArea.all().find(el => el.tag === "select"); retryQuantity.val("1").trigger("change");
  const retryRemarks = retryArea.all().find(el => el.tag === "textarea"); retryRemarks.val("保留本次备注").trigger("input");
  const retryAck = retryArea.all().find(el => el.tag === "input" && el.attributes.type === "checkbox");
  const retrySubmit = retryArea.all().find(el => el.textValue === "提交本人订单");
  await retrySubmit.trigger("click");
  check(retryRoot.children(".rg-portal-error").length === 1 && retryRoot.text().includes("本次合计超过 50 元"));
  check(sent.length === 0 && retryMeal.request === null && !retryAck.prop("checked")
    && retryQuantity.val() === "1" && retryRemarks.val() === "保留本次备注");
  retryAck.prop("checked", true);
  const failedSubmit = retrySubmit.trigger("click"); await tick();
  check(retryRoot.children(".rg-portal-error").length === 0);
  retryReply.reject(new Error("receipt lost")); await failedSubmit;
  check(retryRoot.children(".rg-portal-error").length === 1 && retryRoot.text().includes("receipt lost"));
  check(retryAck.prop("checked") && retryRemarks.val() === "保留本次备注");
  check(!retryQuantity.prop("disabled") && retryQuantity.val() === "1" && retryMeal.request === sent[0].request_id);
  await retrySubmit.trigger("click"); check(sent.length === 2 && JSON.stringify(sent[0]) === JSON.stringify(sent[1]));
  check(sent[1].budget_acknowledged === true && retryRoot.children(".rg-portal-error").length === 0);
  check(retryRoot.text().includes("本人已保存订单") && retryRoot.text().includes("¥51.00"));
  check(otherRoot.children(".rg-portal-error").length === 1 && otherRoot.text().includes("another page's error"));

  const uploadEnv = environment(), uploadRoot = new Element("<div class='rg-portal'></div>");
  const uploadPaper = Object.create(uploadEnv.rg.ManuscriptsPage.prototype); uploadPaper.detailEpoch = 0; uploadPaper.user = "student";
  uploadEnv.UI.api = async () => ({manuscript: {title: "本人论文", author: "student", reviewer: "teacher", revision: 2},
    versions: [{name: "v1", version: 1, original_filename: "draft.pdf", sha256: "original-sha", size_bytes: 20}],
    reviews: [{name: "blocked-ai-review", kind: "ai_suggestion", manuscript_version: "v1", status: "open", blocked: true,
      notice: "来源撤销，正文已屏蔽", content: "不得显示的派生正文"}]});
  await uploadPaper.open("own-manuscript", uploadRoot);
  check(uploadRoot.text().includes("来源撤销，正文已屏蔽") && !uploadRoot.text().includes("不得显示的派生正文"));
  check(!uploadRoot.all().some(el => el.textValue === "追加本人回复" || el.textValue === "标记本人已处理"));
  const fileInput = uploadRoot.all().find(el => el.tag === "input" && el.attributes.type === "file");
  const parentVersion = uploadRoot.all().find(el => el.tag === "select");
  const selectedFile = {name: "draft-A.pdf"}; fileInput.files = [selectedFile];
  const uploadReply = deferred(); uploadEnv.context.fetch = () => uploadReply.promise;
  let reloads = 0; uploadPaper.open = async () => { reloads++; };
  const uploadSend = uploadRoot.all().find(el => el.textValue === "上传并保留新版本");
  const pendingUpload = uploadSend.trigger("click"); await tick();
  check(fileInput.prop("disabled") && parentVersion.prop("disabled"));
  if (!fileInput.prop("disabled")) fileInput.files = [{name: "draft-B.pdf"}];
  uploadReply.reject(new Error("upload receipt unavailable")); await pendingUpload;
  check(fileInput.files[0] === selectedFile && !fileInput.prop("disabled") && !parentVersion.prop("disabled") && reloads === 0);

  const manuscripts = environment(), excerptRoot = new Element("<div class='rg-portal'></div>").appendTo(manuscripts.document.body);
  const paper = Object.create(manuscripts.rg.ManuscriptsPage.prototype);
  paper.user = "student";
  const selectionReply = deferred(); const paperCalls = [];
  manuscripts.UI.api = async (module, method, args) => {paperCalls.push(method); if (method === "create_selection") return selectionReply.promise; throw Error("must not send " + method);};
  await paper.selection({name: "immutable-version"}, excerptRoot, {author: "student", reviewer: "teacher"});
  const text = excerptRoot.all().find(el => el.tag === "textarea"); text.val("本人最初核对的原文");
  const consent = excerptRoot.all().find(el => el.tag === "input" && el.attributes.type === "checkbox"); consent.prop("checked", true);
  const send = excerptRoot.all().find(el => el.textValue === "确认选段并在 AI 助手中生成建议");
  const authorization = send.trigger("click"); await tick(); text.val("等待中改变的原文"); text.trigger("input");
  selectionReply.resolve({name: "old-selection"}); await authorization;
  check(paperCalls.join(",") === "create_selection");
  check(!manuscripts.routes.length);
  const approved = environment(), approvedRoot = new Element("<div class='rg-portal'></div>").appendTo(approved.document.body);
  const approvedPaper = Object.create(approved.rg.ManuscriptsPage.prototype); const approvedCalls = [];
  approvedPaper.user = "student";
  approved.UI.api = async (module, method, args) => {
    approvedCalls.push({method, args});
    return method === "create_selection" ? {name: "explicit-selection"} : method === "create_conversation" ? {name: "owner-conversation"} : {run_id: "one-run"};
  };
  await approvedPaper.selection({name: "specific-version"}, approvedRoot, {author: "student", reviewer: "teacher"});
  approvedRoot.all().find(el => el.tag === "textarea").val("授权原文不会被拼入通用prompt");
  approvedRoot.all().find(el => el.tag === "input" && el.attributes.type === "checkbox").prop("checked", true);
  await approvedRoot.all().find(el => el.textValue === "确认选段并在 AI 助手中生成建议").trigger("click");
  check(approvedCalls.map(row => row.method).join(",") === "create_selection,create_conversation,send_message");
  const modelArgs = approvedCalls[2].args;
  check(modelArgs.selections.length === 1 && modelArgs.selections[0] === "explicit-selection");
  check(!modelArgs.prompt.includes("授权原文不会被拼入通用prompt"));
  check(approvedCalls[1].args.portal === "student" && modelArgs.reasoning_effort === "medium");
  check(approved.rg.pendingAIConversation === "owner-conversation" && approved.routes[0][0] === "rg-student-agent");
  console.log(JSON.stringify({ok: true, checks, browser_layout_tested: false}));
})().catch(error => { console.error(error); process.exitCode = 1; });
