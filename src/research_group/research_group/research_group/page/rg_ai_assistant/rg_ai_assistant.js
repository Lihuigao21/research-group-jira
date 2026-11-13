frappe.pages["rg-ai-assistant"].on_page_load = function (wrapper) {
  frappe.ui.make_app_page({parent: wrapper, title: "旧版 AI 历史（只读）", single_column: true});
  frappe.require("/assets/research_group/js/ai_assistant.js", () => {
    wrapper.rg_ai = new window.research_group.LegacyAIAssistant(wrapper);
  });
};
frappe.pages["rg-ai-assistant"].on_page_hide = function (wrapper) {
  if (wrapper.rg_ai) wrapper.rg_ai.pause();
};
frappe.pages["rg-ai-assistant"].on_page_show = function (wrapper) {
  if (wrapper.rg_ai) wrapper.rg_ai.resume();
};
