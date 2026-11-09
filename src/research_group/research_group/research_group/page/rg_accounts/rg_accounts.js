frappe.pages["rg-accounts"].on_page_load = function (wrapper) {
  frappe.ui.make_app_page({parent: wrapper, title: "我的账号与组员管理", single_column: true});
  wrapper.rg_initializing = true;
  const create = () => {
    wrapper.rg_feature = new window.research_group.AccountsPage(wrapper);
    Promise.resolve(wrapper.rg_feature.ready).finally(() => { wrapper.rg_initializing = false; });
  };
  frappe.require("/assets/research_group/js/portals.js", () => { frappe.require("/assets/research_group/js/accounts.js", create); });
};
frappe.pages["rg-accounts"].on_page_show = function (wrapper) {
  if (wrapper.rg_feature && !wrapper.rg_initializing) wrapper.rg_feature.load();
};
