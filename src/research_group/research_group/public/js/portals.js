/* Native Desk role pages. Selected role never supplies server authority. */
(() => {
  window.research_group = window.research_group || {};
  const rg = window.research_group;
  const esc = value => frappe.utils.escape_html(String(value == null ? "" : value));
  const UI = rg.PortalUI = {
    esc,
    async api(module, method, args = {}) {
      const response = await frappe.call({method: `research_group.${module}.${method}`, args});
      if (response.exc) throw response;
      return response.message;
    },
    money(cents) { return (Number(cents || 0) / 100).toFixed(2); },
    date(value) { return value ? frappe.datetime.str_to_user(String(value)) : "尚无记录"; },
    go(route) { frappe.set_route(...route.split("/")); },
    link(route, label) {
      const parts = route.split("/");
      if (["List", "Form"].includes(parts[0])) {
        const type = parts[1].toLowerCase().replace(/ /g, "-");
        return `<a href="/desk/${encodeURIComponent(type)}${parts[0] === "Form" ? "/" + encodeURIComponent(parts.slice(2).join("/")) : ""}">${esc(label)}</a>`;
      }
      return `<a href="/desk/${parts.map(encodeURIComponent).join("/")}">${esc(label)}</a>`;
    },
    section(parent, title) { return $("<section class='rg-portal-section'></section>").append($("<h3></h3>").text(title)).appendTo(parent); },
    text(parent, value, cls = "rg-portal-text") { return $("<div></div>").addClass(cls).text(value || "").appendTo(parent); },
    field(parent, label, type = "text", value = "") {
      const row = $("<div class='rg-portal-field'></div>").appendTo(parent);
      const id = "rg-field-" + Math.random().toString(36).slice(2);
      $("<label></label>").attr("for", id).text(label).appendTo(row);
      const input = $(type === "textarea" ? "<textarea class='form-control'></textarea>" : "<input class='form-control'>");
      if (type !== "textarea") input.attr("type", type);
      return input.attr("id", id).val(value == null ? "" : value).appendTo(row);
    },
    select(parent, label, values, current = "") {
      const input = UI.field(parent, label);
      const select = $("<select class='form-control'></select>").attr("id", input.attr("id"));
      values.forEach(item => { const row = typeof item === "string" ? {value: item, label: item} : item;
        $("<option></option>").val(row.value).text(row.label).appendTo(select); });
      input.replaceWith(select); return select.val(current);
    },
    button(parent, label, fn, primary = false) {
      const button = $("<button type='button' class='btn'></button>").addClass(primary ? "btn-primary" : "btn-default").text(label).appendTo(parent);
      button.on("click", async () => {
        if (button.prop("disabled")) return;
        button.prop("disabled", true);
        UI.clearError(parent);
        try { await fn(); } catch (error) { UI.error(parent, error); }
        finally { button.prop("disabled", false); }
      });
      return button;
    },
    async busy(fields, operation) {
      const before = fields.map(field => Boolean(field.prop("disabled")));
      fields.forEach(field => field.prop("disabled", true));
      try { return await operation(); }
      finally { fields.forEach((field, index) => field.prop("disabled", before[index])); }
    },
    clearError(parent) {
      const area = parent.closest(".rg-portal").length ? parent.closest(".rg-portal") : parent;
      area.children(".rg-portal-error").remove();
      return area;
    },
    error(parent, error) {
      let message = error && error.message;
      try { const raw = error._server_messages || (error.responseJSON || {})._server_messages;
        if (raw) message = JSON.parse(JSON.parse(raw)[0]).message;
      } catch (_) { /* Server detail may be unavailable after connection loss. */ }
      const area = UI.clearError(parent);
      $("<div role='alert' class='rg-portal-error'></div>").text(message || "未收到操作回执，请刷新核对结果后再试。").prependTo(area);
    },
    status(parent, text) { return $("<div role='status' aria-live='polite' class='rg-portal-status'></div>").text(text).appendTo(parent); },
    table(parent, headings, rows) {
      const box = $("<div class='rg-portal-table'></div>").appendTo(parent);
      const table = $("<table class='table'></table>").appendTo(box);
      const head = $("<tr></tr>").appendTo($("<thead></thead>").appendTo(table));
      headings.forEach(label => $("<th scope='col'></th>").text(label).appendTo(head));
      const body = $("<tbody></tbody>").appendTo(table);
      rows.forEach(row => { const tr = $("<tr></tr>").appendTo(body); row.forEach(value => $("<td></td>").text(value == null ? "" : value).appendTo(tr)); });
      return box;
    },
    async shell(wrapper, title, portal = null, isCurrent = () => true) {
      const data = await UI.api("portals", "bootstrap", portal ? {portal} : {});
      if (!isCurrent()) return null;
      const root = $(wrapper).find(".layout-main-section").empty();
      const body = $("<div class='rg-portal'></div>").appendTo(root);
      const header = $("<header class='rg-portal-header'></header>").appendTo(body);
      const intro = $("<div></div>").appendTo(header);
      $("<span class='rg-portal-eyebrow'></span>").text("课题组协作").appendTo(intro);
      $("<h2></h2>").text(title).appendTo(intro);
      $("<p></p>").text("使用同一账号协作，资料按当前权限显示。").appendTo(intro);
      const select = $("<select class='form-control rg-portal-role' aria-label='切换工作台'></select>").appendTo(header);
      data.portals.forEach(item => $("<option></option>").val(item.route).text(item.title).appendTo(select));
      select.val((data.portals.find(item => item.id === data.portal) || data.portals[0]).route);
      select.on("change", () => UI.go(select.val()));
      return {body, data};
    },
    tabs(parent, labels, render) {
      const tabs = $("<div class='rg-portal-tabs' role='tablist'></div>").appendTo(parent);
      const area = $("<div></div>").appendTo(parent);
      let epoch = 0;
      labels.forEach((label, index) => {
        const button = UI.button(tabs, label, async () => {
          const ticket = ++epoch;
          tabs.find("button").removeClass("btn-primary").addClass("btn-default").attr("aria-selected", "false");
          button.removeClass("btn-default").addClass("btn-primary").attr("aria-selected", "true");
          const slot = $("<div></div>"); area.empty().append(slot);
          await render(index, slot, () => ticket === epoch);
        });
        button.attr("role", "tab");
      });
      tabs.find("button").first().trigger("click");
    },
  };

  class RolePortal {
    constructor(wrapper, role) { this.wrapper = wrapper; this.role = role; this.epoch = 0; this.ready = this.load(); }
    async load() {
      const ticket = ++this.epoch;
      const title = {teacher: "教师工作台", secretary: "秘书工作台", student: "学生工作台"}[this.role];
      try {
        const shell = await UI.shell(this.wrapper, title, this.role, () => ticket === this.epoch);
        if (!shell) return;
        const {body, data} = shell;
        if (data.introduction) UI.text(body, data.introduction, "rg-portal-text rg-portal-intro");
        if (data.announcements.length) {
          const section = UI.section(body, "课题组公告");
          const cards = $("<div class='rg-portal-cards'></div>").appendTo(section);
          data.announcements.forEach(item => { const card = $("<article class='rg-portal-card'></article>").appendTo(cards);
            $("<h4></h4>").text(item.title).appendTo(card);
            UI.text(card, item.body); UI.text(card, UI.date(item.published_at), "rg-portal-meta"); });
        }
        const links = $("<nav class='rg-portal-links' aria-label='课题组功能'></nav>").appendTo(body);
        data.navigation.forEach(item => links.append(UI.link(item.route, item.label)));
        if (this.role === "teacher") UI.tabs(body, ["学生进展", "研究方向", "重要消息"],
          (index, area) => [() => this.students(area), () => this.directions(area), () => rg.renderMessages(area)][index]());
        if (this.role === "secretary") UI.tabs(body, ["信息收集与排期", "重要消息", "网页维护"],
          (index, area) => [() => this.collections(area), () => rg.renderMessages(area), () => this.home(area)][index]());
        if (this.role === "student") this.student(body);
      } catch (error) { if (ticket === this.epoch) UI.error($(this.wrapper).find(".layout-main-section"), error); }
    }
    student(body) {
      const section = UI.section(body, "我的科研与材料");
      UI.text(section, "追加研究进展、预约讨论，按组会任务上传 PPT；论文各版本和教师意见在“论文修改”中保留。");
      const links = $("<div class='rg-portal-links'></div>").appendTo(section);
      [["List/RG Progress", "追加进展"], ["List/RG Appointment", "预约讨论"], ["List/RG Discussion Record", "讨论与 PPT"],
        ["List/RG Meeting Report", "组会 PPT"], ["query-report/RG Student Timeline", "我的时间线"],
        ["rg-manuscripts", "论文修改"], ["rg-accounts", "资料、邮箱与密码"]].forEach(([route, label]) => links.append(UI.link(route, label)));
      const collect = UI.section(body, "填写信息收集");
      const nav = $("<div class='rg-portal-links'></div>").appendTo(collect);
      nav.append(UI.link("List/RG Survey", "投票与问卷"), UI.link("rg-meals", "工作餐"));
    }
    collections(area) {
      const section = UI.section(area, "信息收集");
      UI.text(section, "按活动发布实名投票、问卷和工作餐，查看填写进度及统计。");
      const links = $("<div class='rg-portal-links'></div>").appendTo(section);
      [["List/RG Survey", "投票与问卷"], ["rg-meals", "工作餐"], ["List/RG Group Meeting", "组会安排"],
        ["query-report/RG Secretary Schedule", "公开排期与材料"], ["rg-accounts", "入组审核与组员"]]
        .forEach(([route, label]) => links.append(UI.link(route, label)));
    }
    async students(area) {
      const data = await UI.api("portals", "teacher_students");
      const select = UI.select(area, "查看学生", [{value: "", label: "请选择学生"}, ...data.students.map(item => ({
        value: item.user, label: item.display_name + (!item.active || !item.enabled ? "（已停用，保留历史）" : "")}))]);
      if (data.more) UI.text(area, "当前列出前 500 位学生，可使用教师总览筛选其余成员。", "rg-portal-note");
      const detail = $("<div></div>").appendTo(area);
      let epoch = 0;
      select.on("change", async () => {
        const student = select.val(), ticket = ++epoch;
        detail.empty(); if (!student) return;
        try {
          const result = await UI.api("portals", "student_detail", {student});
          if (ticket !== epoch) return;
          $("<h3></h3>").text(result.student.display_name).appendTo(detail);
          if (!result.topics.length) UI.text(detail, "该学生尚无课题，已保留学生入口。", "rg-portal-empty");
          result.topics.forEach(topic => {
            const section = UI.section(detail, topic.title); section.append(UI.link("Form/RG Topic/" + topic.topic, "打开课题"));
            UI.text(section, `${topic.direction} · ${topic.priority}优先 · ${topic.status}`, "rg-portal-meta");
            UI.text(section, topic.summary || "尚未记录进展");
            UI.text(section, "进展来源时间：" + UI.date(topic.last_progress_at), "rg-portal-meta");
            if (topic.blockers) UI.text(section, "当前阻碍：" + topic.blockers);
            UI.text(section, "下一里程碑：" + (topic.next_milestone || "尚未填写"));
          });
          const section = UI.section(detail, "最近正式讨论");
          if (!result.discussions.length) UI.text(section, "尚无正式讨论记录。", "rg-portal-empty");
          result.discussions.forEach(row => { const item = UI.section(section, UI.date(row.discussed_at));
            item.append(UI.link("Form/RG Discussion Record/" + row.name, "查看原始讨论与材料"));
            UI.text(item, row.conclusion); if (row.teacher_feedback) UI.text(item, "教师意见：" + row.teacher_feedback);
            UI.text(item, "后续行动（记录归档）：" + (row.actions || "未填写")); });
          if (result.more_discussions) UI.text(section, "显示最近 20 条，可在原始列表查看更早记录。", "rg-portal-note");
        } catch (error) { if (ticket === epoch) UI.error(detail, error); }
      });
    }
    async directions(area) {
      const data = await UI.api("portals", "directions");
      const actions = $("<div class='rg-portal-actions'></div>").appendTo(area);
      UI.button(actions, "新增研究方向", () => edit(), true);
      const list = $("<ul class='rg-portal-list'></ul>").appendTo(area);
      data.directions.forEach(item => {
        const li = $("<li></li>").appendTo(list);
        const row = $("<div class='rg-portal-row'></div>").appendTo(li);
        $("<h3></h3>").text(item.title).appendTo(row);
        UI.button(row, "维护目标", () => edit(item)); UI.text(li, item.priority + "优先", "rg-portal-meta");
        UI.text(li, item.goal || "尚未填写组级目标");
        if (!item.topics.length) UI.text(li, "尚未关联课题", "rg-portal-empty");
        item.topics.forEach(topic => { const entry = $("<div></div>").appendTo(li);
          entry.append(UI.link("Form/RG Topic/" + topic.topic, topic.title));
          UI.text(entry, `${topic.student} · ${topic.status}`, "rg-portal-meta");
          UI.text(entry, topic.summary || "尚无进展"); UI.text(entry, "来源时间：" + UI.date(topic.last_progress_at), "rg-portal-meta");
          UI.text(entry, "里程碑：" + (topic.next_milestone || "未填写") + (topic.blockers ? "；阻碍：" + topic.blockers : "")); });
      });
      const edit = (item = {}) => {
        const dialog = new frappe.ui.Dialog({title: "维护研究方向", fields: [
          {fieldname: "title", label: "方向名称", fieldtype: "Data", reqd: 1, default: item.title || ""},
          {fieldname: "goal", label: "组级目标", fieldtype: "Small Text", default: item.goal || ""},
          {fieldname: "priority", label: "优先级", fieldtype: "Select", options: "高\n中\n低", default: item.priority || "中"},
          {fieldname: "topics", label: "关联课题", fieldtype: "MultiSelectList", default: (item.topics || []).map(row => row.topic),
            get_data: txt => data.available_topics.filter(row => (row.title + row.student).includes(txt || "")).map(row => ({value: row.name, description: row.title + " · " + row.student}))},
        ], primary_action_label: "保存目标", primary_action: async values => {
          dialog.disable_primary_action();
          try { await UI.api("portals", "save_direction", {...values, name: item.name || null, expected_revision: item.revision || 0}); dialog.hide(); area.empty(); await this.directions(area); }
          catch (error) { UI.error(dialog.$wrapper, error); } finally { dialog.enable_primary_action(); }
        }}); dialog.show();
      };
    }
    async home(area) {
      const data = await UI.api("portals", "home_settings");
      UI.text(area, "维护首页介绍、公告和批准栏目。导航目的地由站点固定配置。", "rg-portal-note");
      const intro = UI.field(area, "首页介绍", "textarea", data.introduction); intro.attr("maxlength", 12000);
      const navArea = UI.section(area, "栏目名称、顺序与显示");
      const rows = data.navigation.map(item => ({...item}));
      const renderNav = () => {
        navArea.children(".rg-portal-nav-row").remove();
        rows.forEach((item, index) => {
          const row = $("<div class='rg-portal-nav-row'></div>").appendTo(navArea);
          $("<input type='checkbox'>").prop("checked", item.visible).attr("aria-label", "显示" + item.label).appendTo(row).on("change", event => { item.visible = event.target.checked; });
          $("<input class='form-control' maxlength='40'>").val(item.label).attr("aria-label", "栏目名称").appendTo(row).on("input", event => { item.label = event.target.value; });
          const buttons = $("<div></div>").appendTo(row);
          UI.button(buttons, "上移", () => { if (index > 0) { [rows[index - 1], rows[index]] = [rows[index], rows[index - 1]]; renderNav(); } });
          UI.button(buttons, "下移", () => { if (index < rows.length - 1) { [rows[index], rows[index + 1]] = [rows[index + 1], rows[index]]; renderNav(); } });
        });
      }; renderNav();
      const actions = $("<div class='rg-portal-actions'></div>").appendTo(area);
      UI.button(actions, "保存首页", async () => { await UI.api("portals", "save_home", {introduction: intro.val(), navigation: rows, expected_revision: data.revision}); area.empty(); await this.home(area); }, true);
      if (data.can_restore) UI.button(actions, "恢复上一版", async () => { await UI.api("portals", "restore_home", {expected_revision: data.revision}); area.empty(); await this.home(area); });
      const announcements = UI.section(area, "公告");
      UI.button(announcements, "新建公告", () => editAnnouncement());
      data.announcements.forEach(item => {
        const row = $("<div class='rg-portal-row'></div>").appendTo(announcements);
        UI.text(row, item.title + " · " + ({draft: "草稿", published: "已发布", offline: "已下线"}[item.status]), "rg-portal-meta");
        UI.button(row, "维护公告", () => editAnnouncement(item));
      });
      const editAnnouncement = (item = {}) => {
        const dialog = new frappe.ui.Dialog({title: "维护公告", fields: [
          {fieldname: "title", label: "标题", fieldtype: "Data", reqd: 1, default: item.title || ""},
          {fieldname: "body", label: "正文（纯文本）", fieldtype: "Small Text", reqd: 1, default: item.body || ""},
          {fieldname: "audience", label: "可见范围", fieldtype: "Select", options: [
            {value: "all", label: "全组"}, {value: "teacher", label: "教师"}, {value: "student", label: "学生"}, {value: "secretary", label: "秘书"}], default: item.audience || "all"},
          {fieldname: "status", label: "发布状态", fieldtype: "Select", options: [
            {value: "draft", label: "草稿"}, {value: "published", label: "发布"}, {value: "offline", label: "下线"}], default: item.status || "draft"},
        ], primary_action_label: "保存公告", primary_action: async values => {
          dialog.disable_primary_action();
          try { await UI.api("portals", "save_announcement", {...values, name: item.name || null, expected_revision: item.revision || 0}); dialog.hide(); area.empty(); await this.home(area); }
          catch (error) { UI.error(dialog.$wrapper, error); } finally { dialog.enable_primary_action(); }
        }}); dialog.show();
      };
    }
  }
  rg.RolePortal = RolePortal;

  rg.renderMessages = async function (area) {
    const recipients = await UI.api("notifications", "recipient_options");
    UI.text(area, "每人单独投递。未验证联系邮箱的组员须先到“我的账号”完成验证。", "rg-portal-note");
    const subject = UI.field(area, "主题"); subject.attr("maxlength", 180);
    const body = UI.field(area, "正文", "textarea");
    body.attr("maxlength", 4000);
    const kind = UI.select(area, "消息类型", [{value: "important", label: "重要消息"}, {value: "meeting", label: "组会"}, {value: "discussion", label: "讨论"}, {value: "activity", label: "活动"}], "important");
    const group = UI.select(area, "按角色选择", [{value: "", label: "手动选择成员"}, {value: "all", label: "全组成员"}, ...recipients.roles.map(role => ({value: role, label: role}))]);
    const list = $("<div class='rg-portal-list'></div>").appendTo(area);
    const selected = new Set();
    recipients.members.forEach(item => {
      const label = $("<label class='rg-portal-recipient'></label>").appendTo(list);
      const input = $("<input type='checkbox'>").val(item.user).prop("disabled", item.eligible === false).appendTo(label);
      const text = $("<span></span>").appendTo(label);
      UI.text(text, item.display_name); UI.text(text, item.email || item.unavailable_reason || "联系邮箱尚未验证", "rg-portal-meta");
      input.on("change", () => { group.val(""); if (input.prop("checked")) selected.add(item.user); else selected.delete(item.user); });
    });
    group.on("change", () => {
      selected.clear(); const chosen = group.val();
      list.find("input").each((_, input) => {
        const item = recipients.members.find(row => row.user === input.value);
        const checked = chosen === "all" || (chosen && item.roles.includes(chosen));
        input.checked = checked && !input.disabled; if (input.checked) selected.add(input.value);
      });
    });
    const preview = $("<div></div>").appendTo(area);
    let previewEpoch = 0;
    const invalidatePreview = () => { previewEpoch += 1; preview.empty(); };
    [subject, body, kind, group].forEach(input => input.on("input change", invalidatePreview));
    list.on("change", invalidatePreview);
    const actions = $("<div class='rg-portal-actions'></div>").appendTo(area);
    UI.button(actions, "预览名单与正文", async () => {
      const ticket = ++previewEpoch;
      const chosen = group.val();
      const result = await UI.api("notifications", "preview_message", {subject: subject.val(), body: body.val(), kind: kind.val(),
        ...(chosen === "all" ? {all_members: 1} : chosen ? {recipient_role: chosen} : {recipients: [...selected]})});
      if (ticket !== previewEpoch) return;
      preview.empty();
      const box = $("<section class='rg-portal-preview'></section>").appendTo(preview);
      $("<h3></h3>").text("确认本次发送").appendTo(box);
      UI.text(box, result.subject); UI.text(box, result.body);
      UI.text(box, `收件人 ${result.recipient_count} 位；预览有效至 ${UI.date(result.expires_at)}`, "rg-portal-meta");
      UI.table(box, ["姓名", "收件邮箱"], result.recipients.map(item => [item.display_name, item.email]));
      UI.button(box, "本人确认，逐人发送", async () => {
        if (ticket !== previewEpoch) return;
        let receipt;
        try {
          receipt = await UI.api("notifications", "confirm_message", {name: result.name, confirmation_digest: result.confirmation_digest});
        } catch (error) { if (ticket === previewEpoch) throw error; return; }
        if (ticket !== previewEpoch) return;
        preview.empty(); UI.status(preview, "消息已发布，逐人投递状态如下。SMTP 接受不代表外网已收件。");
        renderEvents(preview, receipt.events || []); await history();
      }, true);
    }, true);
    const records = UI.section(area, "我的发送记录");
    const renderEvents = (parent, events) => {
      UI.table(parent, ["收件人", "邮箱", "状态", "次数"], events.map(row => [row.recipient_user, row.recipient_email, row.status_label || row.status, row.attempts]));
      events.filter(row => ["failed", "unconfigured"].includes(row.status)).forEach(row => {
        UI.button(parent, "重试 " + row.recipient_user, async () => {
          const receipt = await UI.api("notifications", "retry_event", {name: row.name}); UI.status(parent, receipt.status_label || receipt.status);
        });
      });
    };
    const history = async () => {
      records.children(":not(h3)").remove(); const result = await UI.api("notifications", "list_messages");
      if (!result.messages.length) UI.text(records, "尚无发送记录。", "rg-portal-empty");
      result.messages.forEach(item => {
        const row = $("<div class='rg-portal-row'></div>").appendTo(records);
        UI.text(row, item.subject + " · " + item.status + " · " + item.recipient_count + " 位", "rg-portal-meta");
        UI.button(row, "查看状态", async () => { const detail = await UI.api("notifications", "get_message", {name: item.name});
          const box = $("<div></div>").appendTo(records); UI.text(box, detail.subject); UI.text(box, detail.body); renderEvents(box, detail.events || []); });
      });
    }; await history();
  };
})();
