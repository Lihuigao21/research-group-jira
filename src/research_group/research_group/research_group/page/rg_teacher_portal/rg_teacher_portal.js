frappe.pages["rg-teacher-portal"].on_page_load = function (wrapper) {
  frappe.ui.make_app_page({parent: wrapper, title: "教师工作台", single_column: true});
  wrapper.rg_initializing = true;
  const create = () => {
    wrapper.rg_feature = new window.research_group.RolePortal(wrapper, "teacher");
    Promise.resolve(wrapper.rg_feature.ready).finally(() => { wrapper.rg_initializing = false; });
  };
  frappe.require("/assets/research_group/js/portals.js", () => { create(); });
};
frappe.pages["rg-teacher-portal"].on_page_show = function (wrapper) {
  if (wrapper.rg_feature && !wrapper.rg_initializing) wrapper.rg_feature.load();
};
