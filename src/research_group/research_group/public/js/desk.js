/* Native Desk helpers; no replacement authentication or client-only permissions. */
window.research_group = window.research_group || {};
(() => {
  const boot = frappe.boot || {};
  const home = boot.research_group_home_route;
  const portals = boot.research_group_portals || [];
  const setupComplete = boot.sysdefaults?.setup_complete ?? boot.setup_complete;
  // Also runs before a sidebar is rebuilt from a fresh customization payload.
  const normalizeNavigation = () => {
    if (!home || !setupComplete) return;
    frappe.re_route = frappe.re_route || {};
    const aliases = {"": home, "research-group": home, "课题组协作": home,
      "research-group/课题组协作": home, "Workspaces/课题组协作": home};
    portals.forEach(item => {
      aliases[item.title] = item.route;
      aliases["research-group/" + item.title] = item.route;
      aliases["Workspaces/" + item.title] = item.route;
    });
    const destinations = {"课题组协作": home, ...Object.fromEntries(portals.map(item => [item.title, item.route]))};
    Object.entries(boot.module_sidebars || {}).forEach(([shell, sidebar]) => {
      const slug = frappe.router.shell_slug(shell);
      // /private/<title> identifies a user's own page, even if its title matches.
      if (slug === "private") return;
      (sidebar.items || []).forEach(item => {
        if (item.link_type === "Workspace" && Object.hasOwn(destinations, item.link_to)) {
          aliases[slug + "/" + item.link_to] = destinations[item.link_to];
        }
      });
    });
    Object.entries(aliases).forEach(([from, to]) => {
      frappe.re_route[from] = to;
      if (from) frappe.re_route[from + "/"] = to;
    });
    const sidebar = boot.module_sidebars?.["Research Group"];
    if (!sidebar) return;
    const routes = portals.map(item => item.route);
    const original = sidebar.items || [];
    const first = original.filter(item => item.link_type === "Page" && routes.includes(item.link_to))
      .map(item => ({...item, child: 0})).sort((a, b) => routes.indexOf(a.link_to) - routes.indexOf(b.link_to));
    const rest = original.filter(item => !(item.link_type === "Workspace" && Object.hasOwn(destinations, item.link_to)) &&
      !(item.link_type === "Page" && routes.includes(item.link_to)));
    boot.module_sidebars["Research Group"] = {...sidebar, items: [...first, ...rest],
      workspaces: (sidebar.workspaces || []).filter(name => !Object.hasOwn(destinations, name))};
  };
  // Register before the initial router.parse(), including bookmarked old URLs.
  normalizeNavigation();
  $(document).on("sidebar_setup", normalizeNavigation);
  const pages = new Set(["rg-teacher-portal", "rg-secretary-portal", "rg-student-portal",
    "rg-accounts", "rg-meals", "rg-manuscripts", "rg-ai-assistant"]);
  const reports = new Set(["RG Teacher Overview", "RG Student Timeline", "RG Secretary Schedule"]);
  const syncTheme = () => {
    const route = frappe.get_route() || [];
    const group = pages.has(route[0]) || (!route[0] && !!home) ||
      (["List", "Form", "Tree"].includes(route[0]) && /^RG /.test(route[1] || "")) ||
      (route[0] === "query-report" && reports.has(route[1]));
    document.body.classList.toggle("rg-desk", group);
  };
  frappe.router.on("change", syncTheme);
  $(document).on("app_ready page-change", syncTheme);
  syncTheme();
})();
Object.assign(window.research_group, {
  has_role(role) {
    return (frappe.boot.research_group_roles || []).includes(role);
  },
  escape(value) {
    return frappe.utils.escape_html(String(value == null ? "" : value));
  },
  async action(frm, method, args) {
    const result = await frappe.call({method: "research_group.services." + method, args, freeze: true});
    await frm.reload_doc();
    return result.message;
  },
  upload(frm, {title, method, args, with_topic = false}) {
    const fields = [];
    if (with_topic) fields.push({fieldname: "topic", label: "报告主题", fieldtype: "Data", length: 200, reqd: 1, default: frm.doc.topic || ""});
    fields.push({fieldname: "file", fieldtype: "HTML", options:
      '<label class="control-label">材料文件</label><input class="form-control rg-file-input" type="file" accept=".ppt,.pptx,.pdf">' +
      '<p class="text-muted small mt-2">支持 PPT、PPTX、PDF，默认上限 50MB。提交后保留每个版本及原提交时间。</p>'});
    const dialog = new frappe.ui.Dialog({title, fields, primary_action_label: "提交材料", async primary_action(values) {
      const input = dialog.$wrapper.find(".rg-file-input")[0];
      const file = input.files[0];
      if (!file) { frappe.msgprint("请选择材料文件。"); return; }
      if (file.size > 50 * 1024 * 1024) { frappe.msgprint("材料超过 50MB。"); return; }
      const form = new FormData();
      Object.entries(args).forEach(([key, value]) => form.append(key, value));
      if (with_topic) form.append("topic", values.topic);
      form.append("file", file);
      dialog.disable_primary_action();
      try {
        const response = await fetch("/api/method/research_group.services." + method, {
          method: "POST", headers: {"X-Frappe-CSRF-Token": frappe.csrf_token}, body: form,
        });
        const result = await response.json();
        if (!response.ok || result.exc) {
          let message = "提交失败，未产生已提交状态。";
          try {
            const messages = JSON.parse(result._server_messages || "[]");
            if (messages.length) message = JSON.parse(messages[0]).message;
          } catch (_) { /* show the bounded generic error */ }
          frappe.msgprint(message);
          return;
        }
        dialog.hide();
        frappe.show_alert({message: "材料已提交，服务端已记录时间。", indicator: "green"});
        await frm.reload_doc();
      } catch (_) {
        frappe.msgprint("未收到提交回执，请刷新材料版本后确认结果。");
      } finally { dialog.enable_primary_action(); }
    }});
    dialog.show();
  },
});

$(document).on("app_ready", () => {
  if (frappe.boot.research_group_demo) {
    frappe.show_alert({message: "当前为虚构演示站点，成员与内容均为虚构。", indicator: "orange"}, 10);
  }
});

