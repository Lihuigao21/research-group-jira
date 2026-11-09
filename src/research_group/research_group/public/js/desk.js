/* Native Desk helpers; no replacement authentication or client-only permissions. */
window.research_group = window.research_group || {};
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

