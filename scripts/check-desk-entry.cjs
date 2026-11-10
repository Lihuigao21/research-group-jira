/* Exercise shipped entry routing before app_ready, then SPA theme transitions. */
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");
const source = fs.readFileSync(path.join(__dirname, "../src/research_group/research_group/public/js/desk.js"), "utf8");
let checks = 0;
const equal = (actual, expected) => { assert.deepEqual(actual, expected); checks++; };
function load(home, portals = [], complete = true) {
  const events = {}, classes = new Set();
  let route = [];
  const document = {body: {classList: {toggle(name, on) {on ? classes.add(name) : classes.delete(name);}}}};
  const frappe = {boot: {research_group_home_route: home, research_group_portals: portals, sysdefaults: {setup_complete: complete}},
    re_route: {"old-unrelated": "unrelated"}, get_route: () => route,
    router: {on(name, fn) { events["router:" + name] = fn; }, shell_slug: name => name.toLowerCase().replace(/ /g, "-")}};
  const $ = () => ({on(names, fn) { names.split(" ").forEach(name => {
    const before = events[name]; events[name] = () => {if (before) before(); fn();};
  });}});
  vm.runInNewContext(source, {window: {}, document, frappe, $, console});
  return {frappe, classes, events, navigate(next, event = "router:change") {route = next; events[event]();}};
}
const teacher = load("rg-teacher-portal", [{title: "教师工作台", route: "rg-teacher-portal"}]);
// These exist at evaluation time; the startup event has deliberately not fired.
for (const legacy of ["", "research-group", "课题组协作", "research-group/课题组协作", "Workspaces/课题组协作",
  "课题组协作/", "教师工作台", "research-group/教师工作台/", "Workspaces/教师工作台"]) equal(teacher.frappe.re_route[legacy], "rg-teacher-portal");
equal(teacher.frappe.re_route["old-unrelated"], "unrelated");
for (const deep of ["rg-topic/TOPIC-001", "rg-topic?status=active", "query-report/RG Teacher Overview", "rg-accounts", "rg-manuscripts"]) equal(teacher.frappe.re_route[deep], undefined);
const dual = load("rg-secretary-portal", [{title: "秘书工作台", route: "rg-secretary-portal"}, {title: "学生工作台", route: "rg-student-portal"}]);
equal(dual.frappe.re_route[""], "rg-secretary-portal");
equal(dual.frappe.re_route["学生工作台"], "rg-student-portal");
equal(dual.frappe.re_route["教师工作台"], undefined);
const incoming = {"Research Group": {items: [
  {link_type: "Workspace", link_to: "课题组协作"},
  {link_type: "Workspace", link_to: "教师工作台"},
  {link_type: "Page", link_to: "rg-teacher-portal", child: 1},
  {link_type: "DocType", link_to: "RG Topic"},
], workspaces: ["课题组协作", "教师工作台"]},
"My Research": {items: [{link_type: "Workspace", link_to: "教师工作台"}]},
"Private": {items: [{link_type: "Workspace", link_to: "教师工作台"}]},
"Unrelated": {items: [{link_type: "Workspace", link_to: "Custom workspace"}]}};
teacher.frappe.boot.module_sidebars = incoming;
teacher.events.sidebar_setup();
equal(teacher.frappe.boot.module_sidebars["Research Group"].items[0].link_to, "rg-teacher-portal");
equal(teacher.frappe.boot.module_sidebars["Research Group"].items[0].child, 0);
equal(teacher.frappe.boot.module_sidebars["Research Group"].items.length, 2);
equal(teacher.frappe.boot.module_sidebars["My Research"].items[0].link_to, "教师工作台");
equal(teacher.frappe.re_route["my-research/教师工作台"], "rg-teacher-portal");
equal(teacher.frappe.re_route["my-research/rg-topic/TOPIC-001"], undefined);
equal(teacher.frappe.re_route["my-research/private/教师工作台"], undefined);
equal(teacher.frappe.re_route["private/教师工作台"], undefined);
equal(teacher.frappe.re_route["private/课题组协作"], undefined);
equal(teacher.frappe.re_route["unrelated/教师工作台"], undefined);
equal(teacher.frappe.boot.module_sidebars["Private"].items[0].link_to, "教师工作台");
teacher.events.sidebar_setup();
equal(teacher.frappe.boot.module_sidebars["Research Group"].items.length, 2);
for (const route of [["rg-teacher-portal"], ["List", "RG Topic", "List"], ["Form", "RG Survey", "Q-001"],
  ["query-report", "RG Teacher Overview"], ["rg-ai-assistant"], ["rg-accounts"], ["rg-meals"], ["rg-manuscripts"]]) {
  teacher.navigate(route); equal(teacher.classes.has("rg-desk"), true);
}
for (const route of [["Form", "User", "Administrator"], ["query-report", "Unrelated report"], ["Workspaces", "Settings"], ["setup-wizard"]]) {
  teacher.navigate(["rg-teacher-portal"]); teacher.navigate(route, "page-change"); equal(teacher.classes.has("rg-desk"), false);
}
const unrelated = load(undefined);
equal(unrelated.frappe.re_route[""], undefined);
equal(unrelated.classes.has("rg-desk"), false);
const setup = load("rg-teacher-portal", [{title: "教师工作台", route: "rg-teacher-portal"}], false);
equal(setup.frappe.re_route[""], undefined);
equal(setup.frappe.re_route["课题组协作"], undefined);
console.log(JSON.stringify({ok: true, checks, browser_tested: false}));
