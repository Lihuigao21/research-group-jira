(() => {
  // Also loaded by Website Settings on the native login page. Keep this small
  // enhancement independent of Desk boot, jQuery and the portal bundle.
  const enhanceLogin = () => {
    if (window.location.pathname.replace(/\/$/, "") !== "/login") return;
    if (window.location.hash === "#forgot") { window.location.replace("/rg_join?mode=reset"); return; }
    document.querySelectorAll('a[href="#forgot"]').forEach(link => { link.href = "/rg_join?mode=reset"; });
    const card = document.querySelector(".for-login .page-card") || document.querySelector(".page-card");
    if (card && !document.getElementById("rg-join-link")) {
      const paragraph = document.createElement("p"); paragraph.id = "rg-join-link"; paragraph.className = "text-center text-muted";
      const link = document.createElement("a"); link.href = "/rg_join"; link.textContent = "复旦邮箱申请入组";
      paragraph.appendChild(link); card.insertAdjacentElement("afterend", paragraph);
    }
  };
  window.addEventListener("hashchange", enhanceLogin);
  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", enhanceLogin, {once: true});
  else enhanceLogin();
  const rg = window.research_group;
  if (!rg || !rg.PortalUI) return;
  const UI = rg.PortalUI;
  class AccountsPage {
    constructor(wrapper) { this.wrapper = wrapper; this.epoch = 0; this.ready = this.load(); }
    async load() {
      const ticket = ++this.epoch;
      try {
        const shell = await UI.shell(this.wrapper, "我的账号与组员管理", null, () => ticket === this.epoch);
        if (!shell) return;
        const {body} = shell;
        const data = await UI.api("accounts", "bootstrap");
        if (ticket !== this.epoch) return;
        if (data.can_manage) UI.tabs(body, ["我的资料与安全", "入组审核", "组员信息"],
          (index, area) => [() => this.profile(area, data.profile, data.mail_status, data.mail_recovery), () => this.applications(area, data), () => this.members(area, data)][index]());
        else this.profile(body, data.profile, data.mail_status, data.mail_recovery);
      } catch (error) { if (ticket === this.epoch) UI.error($(this.wrapper).find(".layout-main-section"), error); }
    }
    mailStatus(area, events = [], target = {}, recovery = {}) {
      const section = UI.section(area, "自动邮件投递记录");
      UI.text(section, "这里显示服务器回执。SMTP 接受不代表收件人实际收到。只有已知失败或未配置且授权仍有效时可恢复；验证和设密会重新签发链接，旧链接失效。", "rg-portal-note");
      const list = $("<div></div>").appendTo(section);
      const pageEpoch = this.epoch;
      let refreshEpoch = 0;
      const names = {registration_verification: "入组邮箱验证", registration_approved: "入组批准与设密",
        registration_rejected: "入组审核结果", contact_verification: "联系邮箱验证", contact_changed: "联系邮箱变更通知",
        password_reset: "重设网站密码", account_status: "账号状态通知", appointment_status: "讨论预约通知",
        meeting_published: "组会通知", meal_published: "工作餐通知", deadline_reminder: "填写截止提醒"};
      const render = (rows, recoveries = {}) => {
        list.empty();
        if (!rows.length) UI.text(list, "暂无可显示记录，可刷新核对。", "rg-portal-empty");
        rows.forEach(row => {
          const item = $("<div></div>").appendTo(list);
          UI.text(item, (names[row.template] || "网站通知") + " · " + row.status_label);
          UI.text(item, "创建于 " + UI.date(row.created_at) + " · 尝试 " + row.attempts + " 次" +
            (row.last_attempt_at ? " · 最近尝试 " + UI.date(row.last_attempt_at) : ""), "rg-portal-meta");
          if (recoveries[row.name]) UI.button(item, recoveries[row.name] === "reissue" ? "重新签发并排队" : "恢复排队", async () => {
            await UI.api("notifications", "retry_event", {name: row.name});
            await refresh();
          });
        });
      };
      render(events || [], recovery || {});
      const refresh = async () => {
        const ticket = ++refreshEpoch;
        const result = await UI.api("notifications", target.application ? "registration_mail_status" : "automatic_mail_status", {...target, include_recovery: 1});
        if (pageEpoch === this.epoch && ticket === refreshEpoch && document.body.contains(section[0])) render(result.events, result.recovery);
      };
      UI.button(section, "刷新邮件状态", refresh);
      return refresh;
    }
    profile(area, profile, mailStatus = [], mailRecovery = {}) {
      const info = UI.section(area, "我的资料");
      UI.text(info, "登录邮箱（固定）：" + profile.login_id);
      UI.text(info, "已验证联系邮箱：" + (profile.contact_verified ? profile.contact_email : "尚未验证"));
      UI.text(info, "登录邮箱保持不变，通知与找回密码发往已验证联系邮箱。", "rg-portal-note");
      const grid = $("<div class='rg-portal-grid'></div>").appendTo(info);
      const name = UI.field(grid, "姓名", "text", profile.display_name); name.attr("maxlength", 140);
      const phone = UI.field(grid, "联系电话", "tel", profile.phone || ""); phone.attr("maxlength", 40);
      const bio = UI.field(info, "个人简介", "textarea", profile.bio || ""); bio.attr("maxlength", 2000);
      UI.text(info, "学号：" + (profile.student_id || "未登记"), "rg-portal-meta");
      UI.button(info, "保存本人资料", async () => {
        await UI.api("accounts", "update_profile", {display_name: name.val(), phone: phone.val(), bio: bio.val()});
        UI.status(info, "本人资料已保存。");
      }, true);
      const contact = UI.section(area, "更换联系邮箱");
      UI.text(contact, "输入本人网站密码，再验证新的复旦邮箱。验证完成后会告知旧邮箱，并退出其他会话。", "rg-portal-note");
      const email = UI.field(contact, "新的复旦联系邮箱", "email"); email.attr("autocomplete", "email");
      const password = UI.field(contact, "当前网站密码", "password"); password.attr("autocomplete", "current-password");
      UI.button(contact, "发送验证邮件", async () => {
        const result = await UI.api("accounts", "request_contact_change", {email: email.val(), password: password.val()});
        password.val(""); UI.status(contact, result.message || "验证请求已记录。请按邮件完成验证；投递状态以服务端回执为准。");
        await refreshMail();
      }, true);
      const security = UI.section(area, "修改网站密码");
      const old = UI.field(security, "当前网站密码", "password").attr("autocomplete", "current-password");
      const next = UI.field(security, "新网站密码", "password").attr("autocomplete", "new-password");
      const confirm = UI.field(security, "再次输入新密码", "password").attr("autocomplete", "new-password");
      UI.text(security, "使用与校园邮箱不同的强密码。", "rg-portal-note");
      UI.button(security, "修改本人密码", async () => {
        if (next.val() !== confirm.val()) throw new Error("两次输入的新密码不一致。");
        await UI.api("accounts", "change_password", {old_password: old.val(), new_password: next.val()});
        password.val(""); old.val(""); next.val(""); confirm.val("");
        window.location.replace("/login");
      }, true);
      const refreshMail = this.mailStatus(area, mailStatus, {}, mailRecovery);
    }
    applications(area, data) {
      UI.text(area, "邮箱验证后仍需核实课题组身份。审批仅建立普通学生，不会接管旧账号。", "rg-portal-note");
      UI.text(area, "申请人尚未取得网站身份时，验证邮件状态仅由已登录秘书核对和恢复；公开申请页使用统一响应保护账号隐私。", "rg-portal-note");
      const list = $("<ul class='rg-portal-list'></ul>").appendTo(area);
      if (!data.registrations.length) UI.text(list, "没有入组申请。", "rg-portal-empty");
      data.registrations.forEach(item => {
        const li = $("<li></li>").appendTo(list);
        $("<h4></h4>").text(item.display_name).appendTo(li);
        UI.text(li, `${item.student_id} · ${item.email}`, "rg-portal-meta");
        UI.text(li, `${item.status} · 邮箱验证 ${UI.date(item.verified_at)}`, "rg-portal-meta");
        this.mailStatus(li, item.mail_status, {application: item.name}, item.mail_recovery);
        if (item.status !== "awaiting_approval") return;
        const note = UI.field(li, "审核说明（不含密码）", "textarea");
        const actions = $("<div class='rg-portal-actions'></div>").appendTo(li);
        ["approve", "reject"].forEach(decision => UI.button(actions, decision === "approve" ? "核实并批准入组" : "拒绝申请", async () => {
          await UI.api("accounts", "review_registration", {application: item.name, decision, note: note.val()});
          await this.load();
        }, decision === "approve"));
      });
      if (data.more_registrations) UI.text(area, "当前仅显示最近一页申请。", "rg-portal-note");
    }
    members(area, data) {
      UI.text(area, "秘书维护普通学生。教师、秘书及系统账号由管理员维护。停用保留历史署名和材料。", "rg-portal-note");
      const list = $("<ul class='rg-portal-list'></ul>").appendTo(area);
      data.members.forEach(item => {
        const li = $("<li></li>").appendTo(list);
        const name = UI.field(li, "姓名", "text", item.display_name).prop("disabled", !item.manageable);
        const student = UI.field(li, "学号", "text", item.student_id || "").prop("disabled", !item.manageable);
        UI.text(li, item.user, "rg-portal-meta");
        UI.text(li, (item.contact_email || "联系邮箱尚未验证") + " · " + (item.enabled && item.active ? "启用" : "停用"), "rg-portal-meta");
        if (!item.manageable) { UI.text(li, "此账号由管理员维护。", "rg-portal-note"); return; }
        const refreshMail = this.mailStatus(li, item.mail_status, {user: item.user}, item.mail_recovery);
        const actions = $("<div class='rg-portal-actions'></div>").appendTo(li);
        UI.button(actions, "保存学生资料", async () => { await UI.api("accounts", "update_student", {user: item.user, display_name: name.val(), student_id: student.val()}); UI.status(li, "学生资料已保存。"); });
        UI.button(actions, item.enabled && item.active ? "暂停该学生" : "恢复该学生", async () => {
          await UI.api("accounts", "set_student_status", {user: item.user, enabled: item.enabled && item.active ? 0 : 1}); await this.load();
        });
        UI.button(actions, "重发本人设密邮件", async () => { await UI.api("accounts", "resend_set_password", {user: item.user}); UI.status(li, "设密邮件请求已记录；学生本人通过邮件设置密码。"); await refreshMail(); });
      });
      if (data.more_members) UI.text(area, "当前仅显示一页组员。", "rg-portal-note");
    }
  }
  rg.AccountsPage = AccountsPage;
})();
