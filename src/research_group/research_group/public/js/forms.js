if (!window.research_group_forms_installed) {
  window.research_group_forms_installed = true;
  const rg = window.research_group;
  const teacher = () => rg.has_role("课题组教师");
  const scheduler = () => teacher() || rg.has_role("课题组秘书");
  const mine = (doc) => doc.student === frappe.session.user;
  const assigned_teacher = (doc) => teacher() && doc.teacher === frappe.session.user;
  const member_query = (role) => ({query: "research_group.services.member_search", filters: {role}});
  function text_dialog(title, label, callback) {
    frappe.prompt([{fieldname: "note", label, fieldtype: "Small Text"}], (v) => callback(v.note || ""), title, "确认");
  }
  function open_row(wrapper, selector, doctype) {
    wrapper.find(selector).on("click", function(event) {
      event.preventDefault();
      frappe.set_route("Form", doctype, $(this).attr("data-name"));
    });
  }
  async function show_reporters(frm) {
    const result = await frappe.db.get_list("RG Meeting Report", {
      filters: {meeting: frm.doc.name}, fields: ["name", "student", "topic", "latest_version", "late"], limit: 500,
    });
    const wrapper = frm.get_field("reporter_list").$wrapper;
    if (!result.length) { wrapper.html('<p class="text-muted">尚未指定报告学生。保存组会后可指定报告人。</p>'); return; }
    wrapper.html('<div class="rg-responsive-table"><table class="table table-bordered"><thead><tr>' +
      '<th>报告学生</th><th>主题</th><th>版本</th><th>迟交</th><th>材料</th></tr></thead><tbody>' + result.map(r =>
        `<tr><td>${rg.escape(r.student)}</td><td>${rg.escape(r.topic || "待提交")}</td><td>${r.latest_version || 0}</td>` +
        `<td>${r.late ? "是" : "否"}</td><td><a href="#" class="rg-report" data-name="${rg.escape(r.name)}">查看报告与材料</a></td></tr>`
      ).join("") + '</tbody></table></div>');
    open_row(wrapper, ".rg-report", "RG Meeting Report");
  }

  frappe.ui.form.on("RG Topic", {
    setup(frm) { frm.set_query("student", () => member_query("课题组学生")); },
    refresh(frm) {
      if (frm.is_new()) return;
      frm.add_custom_button("查看历史时间线", () => frappe.set_route("query-report", "RG Student Timeline"));
      if (mine(frm.doc) && rg.has_role("课题组学生")) {
        frm.add_custom_button("追加进展", () => frappe.new_doc("RG Progress", {topic: frm.doc.name}));
        frm.add_custom_button("申请讨论", () => frappe.new_doc("RG Appointment", {topic: frm.doc.name}));
      }
    },
  });
  frappe.ui.form.on("RG Progress", {
    refresh(frm) {
      if (frm.is_new()) return;
      frm.disable_save();
      ["topic", "progress_date", "summary", "blockers", "next_milestone"].forEach(field => frm.set_df_property(field, "read_only", 1));
      frm.set_intro("进展按次追加，已记录内容保留历史。", "blue");
    },
  });
  frappe.ui.form.on("RG Appointment", {
    setup(frm) { frm.set_query("teacher", () => member_query("课题组教师")); },
    refresh(frm) {
      if (frm.is_new()) return;
      frm.disable_save();
      ["topic", "teacher", "start_at", "end_at", "agenda"].forEach(field => frm.set_df_property(field, "read_only", 1));
      if (assigned_teacher(frm.doc) && frm.doc.status === "待审批") {
        ["已批准", "已拒绝"].forEach(decision => frm.add_custom_button(decision === "已批准" ? "批准" : "拒绝", () =>
          text_dialog(decision === "已批准" ? "批准讨论" : "拒绝讨论", "处理说明", note => rg.action(frm, "decide_appointment", {name: frm.doc.name, decision, note}))));
      }
      if ((mine(frm.doc) || assigned_teacher(frm.doc)) && ["待审批", "已批准"].includes(frm.doc.status)) {
        frm.add_custom_button("取消讨论", () => text_dialog("取消讨论", "取消说明", note => rg.action(frm, "cancel_appointment", {name: frm.doc.name, note})));
      }
      if ((mine(frm.doc) || assigned_teacher(frm.doc)) && frm.doc.status === "已批准") {
        frm.add_custom_button("归档讨论", () => {
          const fields = [
            {fieldname: "conclusion", label: "讨论结论", fieldtype: "Small Text", reqd: 1},
            {fieldname: "actions", label: "后续行动", fieldtype: "Small Text", reqd: 1},
          ];
          if (assigned_teacher(frm.doc)) fields.push({fieldname: "teacher_feedback", label: "教师意见", fieldtype: "Small Text"});
          frappe.prompt(fields, async values => {
            const record = await rg.action(frm, "archive_discussion", {name: frm.doc.name, ...values});
            frappe.set_route("Form", "RG Discussion Record", record.name);
          }, "归档讨论", "归档");
        });
      }
      if (mine(frm.doc) || assigned_teacher(frm.doc)) {
        frm.add_custom_button("上传私人讨论材料", () => rg.upload(frm, {title: "私人讨论材料（仅本人及教师可读）", method: "upload_discussion_material", args: {doctype: frm.doctype, name: frm.doc.name}}));
      }
      if (frm.doc.status === "已归档") {
        frm.add_custom_button("查看讨论记录", async () => {
          const rows = await frappe.db.get_list("RG Discussion Record", {filters: {appointment: frm.doc.name}, fields: ["name"], limit: 1});
          if (rows.length) frappe.set_route("Form", "RG Discussion Record", rows[0].name);
        });
      }
    },
  });
  frappe.ui.form.on("RG Discussion Record", {
    refresh(frm) {
      if (frm.is_new()) return;
      frm.disable_save();
      frm.set_intro("私人讨论记录和附件仅本人及教师可读。", "blue");
      if (assigned_teacher(frm.doc)) {
        frm.add_custom_button("补充教师意见", () => frappe.prompt([
          {fieldname: "feedback", label: "教师意见", fieldtype: "Small Text", reqd: 1, default: frm.doc.teacher_feedback},
        ], values => rg.action(frm, "update_teacher_feedback", {name: frm.doc.name, feedback: values.feedback}), "教师意见", "保存"));
      }
      if (mine(frm.doc) || assigned_teacher(frm.doc)) {
        frm.add_custom_button("上传私人讨论材料", () => rg.upload(frm, {title: "私人讨论材料（仅本人及教师可读）", method: "upload_discussion_material", args: {doctype: frm.doctype, name: frm.doc.name}}));
      }
    },
  });
  frappe.ui.form.on("RG Group Meeting", {
    async refresh(frm) {
      if (frm.is_new()) return;
      if (frm.doc.publisher !== frappe.session.user) frm.disable_save();
      await show_reporters(frm);
      if (scheduler() && frm.doc.publisher === frappe.session.user && frm.doc.status === "草稿") {
        frm.add_custom_button("指定报告学生", () => frappe.prompt([
          {fieldname: "student", label: "报告学生", fieldtype: "Link", options: "User", reqd: 1, get_query: () => member_query("课题组学生")},
        ], values => rg.action(frm, "assign_reporter", {meeting: frm.doc.name, student: values.student}), "指定报告学生", "添加"));
        frm.add_custom_button("发布组会", () => frappe.confirm("发布后全组可以查看组会与材料。确认发布？", () => rg.action(frm, "publish_meeting", {name: frm.doc.name})));
      }
    },
  });
  frappe.ui.form.on("RG Meeting Report", {
    setup(frm) { frm.set_query("student", () => member_query("课题组学生")); },
    async refresh(frm) {
      if (frm.is_new()) return;
      frm.disable_save();
      frm.set_df_property("meeting", "read_only", 1);
      frm.set_df_property("student", "read_only", 1);
      if (scheduler()) {
        const parent = await frappe.db.get_value("RG Group Meeting", frm.doc.meeting, ["publisher", "status"]);
        if (parent.message && parent.message.publisher === frappe.session.user && parent.message.status === "草稿") {
          frm.set_df_property("student", "read_only", 0);
          frm.enable_save();
        }
      }
      if (mine(frm.doc) && rg.has_role("课题组学生")) {
        frm.add_custom_button("提交或重传材料", () => rg.upload(frm, {title: "提交组会报告材料", method: "upload_report_material", args: {report: frm.doc.name}, with_topic: true}));
      }
      const result = await frappe.call({method: "research_group.services.material_history", args: {report: frm.doc.name}});
      const rows = result.message || [];
      const wrapper = frm.get_field("history").$wrapper;
      if (!rows.length) { wrapper.html('<p class="text-muted">尚未提交材料。</p>'); return; }
      wrapper.html('<div class="rg-responsive-table"><table class="table table-bordered"><thead><tr><th>版本</th><th>主题与材料</th><th>服务端提交时间</th><th>迟交</th></tr></thead><tbody>' + rows.map(r =>
        `<tr><td>${r.version}</td><td>${rg.escape(r.report_topic)}<br><a href="${rg.escape(r.file_url)}" target="_blank" rel="noopener">${rg.escape(r.original_filename)}</a></td>` +
        `<td>${rg.escape(frappe.datetime.str_to_user(r.submitted_at))}</td><td>${r.late ? "迟交" : "按时"}</td></tr>`
      ).join("") + '</tbody></table></div>');
    },
  });
  frappe.ui.form.on("RG Material Version", {
    async refresh(frm) {
      if (frm.is_new() || !frm.doc.material_file) return;
      frm.disable_save();
      frm.add_custom_button("下载此版本", async () => {
        const result = await frappe.db.get_doc("File", frm.doc.material_file);
        window.open(result.file_url + "?fid=" + encodeURIComponent(result.name), "_blank", "noopener");
      });
    },
  });
}

