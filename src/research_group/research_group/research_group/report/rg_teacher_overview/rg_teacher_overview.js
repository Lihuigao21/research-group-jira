frappe.query_reports["RG Teacher Overview"] = {
  filters: [{fieldname: "student", label: "学生", fieldtype: "Link", options: "User",
    get_query() { return {query: "research_group.services.member_search", filters: {role: "课题组学生"}}; }}],
};
