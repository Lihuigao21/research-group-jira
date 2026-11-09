/* Execute the shipped account page and shared UI with controlled API receipts.
 * This checks change-password behavior, not browser or layout acceptance.
 */
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");
const app = path.resolve(__dirname, "../src/research_group/research_group/public/js");
let checks = 0;
const check = (value, label) => { assert.ok(value, label); checks++; };
const deferred = () => { let resolve, reject; const promise = new Promise((yes, no) => { resolve = yes; reject = no; }); return {promise, resolve, reject}; };
const tick = () => new Promise(resolve => setImmediate(resolve));

class Collection extends Array {
  remove() { this.forEach(element => element.remove()); return this; }
}
class Element {
  constructor(html = "<div>") {
    this.tag = (html.match(/^<(\w+)/) || [])[1] || "div";
    this.classes = new Set((html.match(/class=['"]([^'"]+)/) || ["", ""])[1].split(" ").filter(Boolean));
    this.childrenList = []; this.textValue = ""; this.attributes = {}; this.props = {}; this.events = {}; this.value = "";
    this.length = 1; this[0] = this;
  }
  appendTo(parent) { this.parent = parent; parent.childrenList.push(this); return this; }
  prependTo(parent) { this.parent = parent; parent.childrenList.unshift(this); return this; }
  append(value) { value.appendTo(this); return this; }
  text(value) { if (!arguments.length) return this.textValue + this.childrenList.map(child => child.text()).join(""); this.textValue = String(value); return this; }
  attr(key, value) { if (arguments.length === 1) return this.attributes[key]; this.attributes[key] = value; return this; }
  prop(key, value) { if (arguments.length === 1) return this.props[key]; this.props[key] = value; return this; }
  val(value) { if (!arguments.length) return this.value; this.value = value; return this; }
  empty() { this.childrenList.forEach(child => { child.parent = null; }); this.childrenList = []; this.textValue = ""; return this; }
  addClass(value) { value.split(" ").forEach(name => this.classes.add(name)); return this; }
  on(name, action) { this.events[name] = action; return this; }
  trigger(name) { return this.events[name] && this.events[name]({target: this}); }
  all() { return [this, ...this.childrenList.flatMap(child => child.all())]; }
  contains(element) { return this.all().includes(element); }
  matches(selector) { return selector.startsWith(".") ? this.classes.has(selector.slice(1)) : this.tag === selector; }
  children(selector) { return new Collection(...this.childrenList.filter(child => child.matches(selector))); }
  closest(selector) { let current = this; while (current) { if (current.matches(selector)) return current; current = current.parent; } return {length: 0}; }
  remove() { if (this.parent) this.parent.childrenList = this.parent.childrenList.filter(child => child !== this); this.parent = null; return this; }
}

function environment() {
  const env = {calls: [], replacements: [], redirectValues: []};
  const body = new Element("<body>"), root = new Element("<div class='rg-portal'>").appendTo(body);
  const window = {research_group: {}, addEventListener() {}, location: {
    pathname: "/desk/rg-accounts", hash: "",
    replace(target) { env.redirectValues.push(env.passwords.map(field => field.val())); env.replacements.push(target); },
  }};
  const document = {readyState: "complete", body};
  const frappe = {call: async options => { env.calls.push(options); return env.reply(options); }};
  const context = {window, document, frappe, console, Date, Math,
    $: value => value instanceof Element ? value : new Element(value)};
  vm.createContext(context);
  for (const file of ["portals.js", "accounts.js"]) {
    vm.runInContext(fs.readFileSync(path.join(app, file), "utf8"), context, {filename: file});
  }
  const page = Object.create(window.research_group.AccountsPage.prototype);
  page.epoch = 1;
  page.profile(root, {login_id: "student@example.invalid", display_name: "虚构学生", contact_verified: true,
    contact_email: "contact@example.invalid", student_id: "fictional-id"});
  const security = root.all().find(element => element.tag === "section" && element.childrenList[0].textValue === "修改网站密码");
  const passwords = root.all().filter(element => element.attributes.type === "password");
  const fields = security.all().filter(element => element.attributes.type === "password");
  const change = security.all().find(element => element.tag === "button" && element.textValue === "修改本人密码");
  passwords[0].val("fictional-contact-password");
  fields[0].val("fictional-old-password"); fields[1].val("fictional-new-password"); fields[2].val("fictional-new-password");
  return Object.assign(env, {window, root, fields, passwords, change});
}

(async () => {
  const pending = environment(), receipt = deferred();
  pending.reply = () => receipt.promise;
  const first = pending.change.trigger("click"), duplicate = pending.change.trigger("click");
  await tick();
  check(pending.calls.length === 1 && pending.change.prop("disabled"), "one request while awaiting acknowledgement");
  check(pending.replacements.length === 0 && pending.passwords.every(field => field.val()), "no premature clearing or navigation");
  check(pending.calls[0].method === "research_group.accounts.change_password", "real shared API dispatch");
  check(JSON.stringify(pending.calls[0].args) === JSON.stringify({old_password: "fictional-old-password", new_password: "fictional-new-password"}), "only intended credentials submitted");
  pending.window.location.pathname = "/desk/rg-student-portal";
  // The receipt cannot supply the navigation target, even if a future server
  // response changes. The current session is revoked after successful change.
  receipt.resolve({message: "https://outside.invalid/untrusted-target"});
  await first; await duplicate;
  check(pending.passwords.length === 4 && pending.passwords.every(field => field.val() === ""), "all current page password fields cleared");
  check(pending.replacements.length === 1 && pending.replacements[0] === "/login", "successful late receipt replaces location with fixed login");
  check(pending.redirectValues[0].every(value => value === ""), "clear secrets before navigation");
  check(pending.calls.length === 1, "no authenticated refresh after session revocation");

  const invalid = environment();
  invalid.fields[2].val("different-confirmation");
  invalid.reply = () => { throw Error("mismatch must not reach API"); };
  await invalid.change.trigger("click");
  check(invalid.calls.length === 0 && invalid.replacements.length === 0, "mismatch stays local");
  check(invalid.passwords.every(field => field.val()) && !invalid.change.prop("disabled"), "mismatch preserves editable values");
  check(invalid.root.text().includes("两次输入的新密码不一致。"), "visible mismatch error");

  const failed = environment(), failure = deferred();
  failed.reply = () => failure.promise;
  const attempt = failed.change.trigger("click"); await tick();
  failure.reject(new Error("current password rejected")); await attempt;
  check(failed.replacements.length === 0 && failed.passwords.every(field => field.val()), "failed API does not clear or leave page");
  check(!failed.change.prop("disabled") && failed.root.text().includes("current password rejected"), "failure remains actionable");
  failed.reply = async () => ({message: "/login"});
  await failed.change.trigger("click");
  check(failed.calls.length === 2 && failed.replacements[0] === "/login", "corrected retry reaches login");
  check(failed.passwords.every(field => field.val() === "") && failed.root.children(".rg-portal-error").length === 0, "successful retry clears secrets and old error");
  console.log(JSON.stringify({ok: true, checks, browser_layout_tested: false}));
})().catch(error => { console.error(error); process.exitCode = 1; });
