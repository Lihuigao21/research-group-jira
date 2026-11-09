/* Native Desk forms and dialogs; all survey text is rendered as text. */
if (!window.research_group_surveys_installed) {
  window.research_group_surveys_installed = true;
  const rg = window.research_group;
  const secretary = () => rg.has_role("课题组秘书");
  let dialogNumber = 0;
  function sameRecord(frm, doctype, name) {
    return frm.doc && frm.doc.doctype === doctype && frm.doc.name === name;
  }
  function refreshGuard(frm) {
    const doc = frm.doc;
    const name = doc.name;
    const sequence = (frm._rg_survey_refresh_sequence || 0) + 1;
    frm._rg_survey_refresh_sequence = sequence;
    return () => frm._rg_survey_refresh_sequence === sequence && frm.doc === doc && frm.doc.name === name;
  }
  async function call(method, args) {
    const result = await frappe.call({method: "research_group.surveys." + method, args, freeze: true});
    return result.message;
  }
  function state(survey) {
    return survey.status === "已发布" && !survey.accepting_responses ? "已截止" : survey.status;
  }
  function answerText(value) {
    return Array.isArray(value) ? value.join("；") : (value || "未填写");
  }
  function answers(wrapper, questions, values) {
    questions.forEach((question, index) => {
      const section = $("<div class='rg-survey-answer mb-3'></div>").appendTo(wrapper);
      $("<strong></strong>").text(`${index + 1}. ${question.title}`).appendTo(section);
      $("<div class='rg-survey-text mt-1'></div>").text(answerText(values[question.name])).appendTo(section);
    });
  }
  function fill(frm, survey) {
    const doctype = frm.doc.doctype;
    const record = frm.doc.name;
    const mine = survey.my_response;
    const initial = mine ? mine.answers : {};
    const controls = new Map();
    const id = ++dialogNumber;
    const dialog = new frappe.ui.Dialog({
      title: mine ? "修改本人答卷" : "填写" + survey.kind,
      fields: [{fieldname: "notice", fieldtype: "HTML", options:
        '<p class="text-muted">实名收集，秘书可查看实名明细。每人保留一份最新答卷，截止或关闭前可修改。</p>'},
      {fieldname: "questions", fieldtype: "HTML"}],
      primary_action_label: mine ? "保存修改" : "提交答卷",
      async primary_action() {
        if (!sameRecord(frm, doctype, record)) return;
        const values = {};
        survey.questions.forEach(question => {
          const container = controls.get(question.name);
          if (question.question_type === "简答") {
            values[question.name] = container.find("textarea").val() || "";
          } else {
            const selected = container.find("input:checked").map((_, element) => element.value).get();
            values[question.name] = question.question_type === "多选" ? selected : (selected[0] || "");
          }
        });
        dialog.disable_primary_action();
        try {
          await call("submit_response", {survey: survey.name, answers: JSON.stringify(values)});
          dialog.hide();
          frappe.show_alert({message: "答卷已保存，以服务器记录为准。", indicator: "green"});
          if (sameRecord(frm, doctype, record)) await frm.reload_doc();
        } finally { dialog.enable_primary_action(); }
      },
    });
    const wrapper = dialog.fields_dict.questions.$wrapper;
    survey.questions.forEach((question, index) => {
      const section = $("<div class='rg-survey-question form-group mb-4'></div>").appendTo(wrapper);
      controls.set(question.name, section);
      const labelId = `rg-survey-label-${id}-${index}`;
      $("<div class='control-label rg-survey-text mb-2'></div>")
        .attr("id", labelId)
        .text(`${index + 1}. ${question.title}（${question.question_type}${question.required ? "，必填" : "，选填"}）`).appendTo(section);
      if (question.question_type === "简答") {
        const input = $("<textarea class='form-control' rows='3' maxlength='5000'></textarea>")
          .attr("aria-label", question.title).val(initial[question.name] || "").appendTo(section);
        input.attr("aria-labelledby", labelId);
      } else {
        section.attr({role: question.question_type === "多选" ? "group" : "radiogroup", "aria-labelledby": labelId});
        if (question.question_type === "单选") section.attr("aria-required", question.required ? "true" : "false");
        const selection = initial[question.name];
        const selected = Array.isArray(selection) ? selection : [selection];
        question.options.forEach(option => {
          const item = $("<div class='checkbox rg-survey-choice'></div>").appendTo(section);
          const optionLabel = $("<label></label>").appendTo(item);
          $("<input>").attr({type: question.question_type === "多选" ? "checkbox" : "radio",
            name: `rg-survey-${id}-${index}`, "aria-label": option}).val(option)
            .prop("checked", selected.includes(option)).appendTo(optionLabel);
          $("<span></span>").text(option).appendTo(optionLabel);
        });
        if (!question.required && question.question_type === "单选") {
          $("<button type='button' class='btn btn-default btn-xs'>清除选择</button>")
            .on("click", () => section.find("input").prop("checked", false)).appendTo(section);
        }
      }
    });
    dialog.show();
  }
  function participation(frm, survey, current) {
    const wrapper = frm.get_field("participation").$wrapper.empty();
    if (survey.status === "草稿") {
      $("<p class='text-muted'></p>").text("草稿仅秘书可见，发布后启用成员可填写。发布后锁定题目和截止时间。")
        .appendTo(wrapper);
      return;
    }
    $("<p></p>").text(`当前状态：${state(survey)}。` + (survey.deadline
      ? "截止时间：" + frappe.datetime.str_to_user(survey.deadline) + "（服务器时间）。"
      : "未设置自动截止时间，秘书可提前关闭。"))
      .appendTo(wrapper);
    const mine = survey.my_response;
    if (mine) {
      $("<p class='text-muted small'></p>").text("本人已填写，最近保存：" + frappe.datetime.str_to_user(mine.updated_at))
        .appendTo(wrapper);
      answers(wrapper, survey.questions, mine.answers);
    } else {
      $("<p class='text-muted'></p>").text("尚未填写本人答卷。").appendTo(wrapper);
    }
    if (survey.accepting_responses) {
      const label = mine ? "修改本人答卷" : "填写" + survey.kind;
      $("<button type='button' class='btn btn-primary'></button>").text(label)
        .on("click", () => { if (current()) fill(frm, survey); }).appendTo(wrapper);
    } else {
      $("<p class='text-muted'></p>").text("当前已停止填写，已保存的答卷保留可读。").appendTo(wrapper);
    }
  }
  function table(wrapper, headers) {
    const container = $("<div class='rg-responsive-table mb-3'></div>").appendTo(wrapper);
    const element = $("<table class='table table-bordered'></table>").appendTo(container);
    const head = $("<tr></tr>").appendTo($("<thead></thead>").appendTo(element));
    headers.forEach(header => $("<th></th>").text(header).appendTo(head));
    return $("<tbody></tbody>").appendTo(element);
  }
  function row(body, values) {
    const element = $("<tr></tr>").appendTo(body);
    values.forEach(value => $("<td class='rg-survey-text'></td>").text(value == null ? "" : String(value)).appendTo(element));
  }
  async function results(frm, survey, current) {
    if (!secretary()) {
      if (current()) frm.toggle_display("results", false);
      return;
    }
    const data = await call("survey_results", {survey: survey.name});
    if (!current() || data.survey.name !== survey.name) return;
    const wrapper = $("<div></div>");
    $("<p></p>").text(`已收 ${data.submitted_count} 份答卷；当前启用成员 ${data.active_member_count} 人，` +
      `其中已填 ${data.active_submitted_count} 人，未填 ${data.pending_count} 人。`).appendTo(wrapper);
    $("<p class='text-muted small'></p>").text("每人只计最新答卷。已停用成员的历史答卷仍保留在结果中。").appendTo(wrapper);
    if (data.pending_count) {
      const pending = $("<details class='mb-3'></details>").appendTo(wrapper);
      $("<summary></summary>").text("查看当前未填成员").appendTo(pending);
      const list = $("<ul class='mt-2'></ul>").appendTo(pending);
      data.pending_members.forEach(member => $("<li></li>").text(`${member.display_name}（${member.user}）`).appendTo(list));
    }
    data.questions.forEach((question, index) => {
      const section = $("<section class='mb-4'></section>").appendTo(wrapper);
      $("<h5 class='rg-survey-text'></h5>").text(`${index + 1}. ${question.title}（已答 ${question.answered_count} 人）`).appendTo(section);
      if (question.question_type === "简答") {
        const body = table(section, ["姓名", "账号", "简答内容"]);
        question.answers.forEach(answer => row(body, [answer.respondent_name, answer.respondent, answer.answer]));
      } else {
        const body = table(section, ["选项", "人数"]);
        question.options.forEach(option => row(body, [option.label, option.count]));
      }
    });
    $("<h5>实名答卷明细</h5>").appendTo(wrapper);
    const body = table(wrapper, ["姓名", "账号", "最近保存", ...survey.questions.map(question => question.title)]);
    data.responses.forEach(response => row(body, [response.respondent_name, response.respondent,
      frappe.datetime.str_to_user(response.updated_at),
      ...survey.questions.map(question => answerText(response.answers[question.name]))]));
    if (!data.responses.length) $("<p class='text-muted'>尚未收到答卷。</p>").appendTo(wrapper);
    // Render off-screen and commit once; an older refresh never appends here.
    frm.get_field("results").$wrapper.empty().append(wrapper.contents());
    frm.toggle_display("results", true);
  }
  frappe.ui.form.on("RG Survey", {
    async refresh(frm) {
      const current = refreshGuard(frm);
      const name = frm.doc.name;
      frm.get_field("participation").$wrapper.empty();
      frm.get_field("results").$wrapper.empty();
      frm.toggle_display("participation", !frm.is_new());
      frm.toggle_display("results", !frm.is_new() && secretary());
      if (frm.is_new()) {
        ["title", "description", "deadline", "questions"].forEach(field =>
          frm.set_df_property(field, "read_only", secretary() ? 0 : 1));
        if (secretary()) frm.enable_save();
        frm.set_intro("实名收集；单题为投票，多题为问卷。可先保存草稿，发布前校验题目和选项。", "blue");
        return;
      }
      const survey = await call("get_survey", {name});
      if (!current() || survey.name !== name) return;
      const editable = secretary() && survey.status === "草稿";
      ["title", "description", "deadline", "questions"].forEach(field => frm.set_df_property(field, "read_only", editable ? 0 : 1));
      if (editable) frm.enable_save(); else frm.disable_save();
      frm.set_intro(survey.status === "草稿" ? "草稿仅秘书可见；发布后锁题。" :
        `实名收集，${state(survey)}。普通成员仅可查看本人答卷；实名明细和统计仅秘书可见。`, "blue");
      participation(frm, survey, current);
      await results(frm, survey, current);
      if (!current() || !secretary()) return;
      if (survey.status === "草稿") {
        frm.add_custom_button("发布问卷", () => {
          if (!current()) return;
          frappe.confirm("发布后锁定题目与截止时间，启用成员可实名填写。确认发布？", async () => {
            if (!current()) return;
            if (frm.is_dirty()) await frm.save();
            if (!sameRecord(frm, "RG Survey", survey.name) || frm.is_dirty()) return;
            await call("publish_survey", {name: survey.name});
            if (sameRecord(frm, "RG Survey", survey.name)) await frm.reload_doc();
          });
        });
      } else if (survey.status === "已发布") {
        frm.add_custom_button("提前关闭", () => {
          if (!current()) return;
          frappe.confirm("关闭后成员不能再新增或修改答卷。确认关闭？", async () => {
            if (!current()) return;
            await call("close_survey", {name: survey.name});
            if (sameRecord(frm, "RG Survey", survey.name)) await frm.reload_doc();
          });
        });
      }
      frm.add_custom_button("刷新填写进度", () => { if (current()) return frm.reload_doc(); });
      frm.add_custom_button("导出 CSV", () => {
        if (current()) window.open(
          "/api/method/research_group.surveys.export_csv?survey=" + encodeURIComponent(survey.name), "_blank", "noopener");
      });
    },
  });
  frappe.ui.form.on("RG Survey Response", {
    async refresh(frm) {
      const current = refreshGuard(frm);
      const response = frm.doc;
      frm.get_field("answer_preview").$wrapper.empty();
      frm.disable_save();
      if (frm.is_new()) return;
      const survey = await call("get_survey", {name: response.survey});
      if (!current() || survey.name !== response.survey) return;
      const wrapper = frm.get_field("answer_preview").$wrapper.empty();
      answers(wrapper, survey.questions, JSON.parse(response.answers_json));
      frm.add_custom_button("返回问卷", () => { if (current()) frappe.set_route("Form", "RG Survey", survey.name); });
      if (response.respondent === frappe.session.user && survey.accepting_responses) {
        frm.add_custom_button("修改本人答卷", () => { if (current()) fill(frm, survey); });
      }
    },
  });
}
