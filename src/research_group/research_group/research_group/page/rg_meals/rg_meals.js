frappe.pages["rg-meals"].on_page_load = function (wrapper) {
  frappe.ui.make_app_page({parent: wrapper, title: "工作餐", single_column: true});
  wrapper.rg_initializing = true;
  const create = () => {
    wrapper.rg_feature = new window.research_group.MealsPage(wrapper);
    Promise.resolve(wrapper.rg_feature.ready).finally(() => { wrapper.rg_initializing = false; });
  };
  frappe.require("/assets/research_group/js/portals.js", () => { frappe.require("/assets/research_group/js/meals.js", create); });
};
frappe.pages["rg-meals"].on_page_show = function (wrapper) {
  if (wrapper.rg_feature && !wrapper.rg_initializing) wrapper.rg_feature.load();
};
