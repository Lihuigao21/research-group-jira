/* Private files stay immutable. AI receives only an explicitly authorized excerpt. */
(() => {
  const rg = window.research_group, UI = rg.PortalUI;
  const STATES = {draft: "待上传初稿", under_review: "待教师审阅", revision_requested: "修改中",
    student_draft: "学生稿", teacher_annotation: "教师批注版", teacher_comment: "教师意见",
    student_reply: "学生回复", ai_suggestion: "AI 辅助建议", open: "待处理", addressed: "已回复或处理", closed: "已核对关闭"};
  class ManuscriptsPage {
    constructor(wrapper) { this.wrapper = wrapper; this.epoch = 0; this.detailEpoch = 0; this.ready = this.load(); }
    async load(selected = null) {
      const ticket = ++this.epoch;
      ++this.detailEpoch;
      try {
        const shell = await UI.shell(this.wrapper, "论文修改", null, () => ticket === this.epoch);
        if (!shell) return;
        const {body, data} = shell;
        this.user = data.user;
        UI.text(body, "论文仅向参与学生和指定教师开放。PDF / DOCX 每次上传生成新版本，原稿和审阅历史保留。", "rg-portal-note");
        const actions = $("<div class='rg-portal-actions'></div>").appendTo(body);
        if (data.portals.some(item => item.id === "student")) UI.button(actions, "创建本人论文", () => this.create(), true);
        const result = await UI.api("manuscripts", "list_manuscripts");
        if (ticket !== this.epoch) return;
        const target = selected || frappe.get_route()[1] || this.selected || "";
        const options = [{value: "", label: "请选择论文"}, ...result.items.map(row => ({value: row.name, label: row.title}))];
        if (target && !options.some(row => row.value === target)) options.push({value: target, label: "打开关联论文"});
        const select = UI.select(body, "本人参与的论文", options, target || (result.items[0] || {}).name || "");
        let offset = result.items.length;
        const more = UI.button(actions, "更多论文", async () => {
          const page = await UI.api("manuscripts", "list_manuscripts", {offset});
          if (ticket !== this.epoch) return;
          page.items.forEach(row => { if (!select.find("option").toArray().some(option => option.value === row.name)) $("<option></option>").val(row.name).text(row.title).appendTo(select); });
          offset += page.items.length; more.toggle(page.has_more);
        }); more.toggle(result.has_more);
        const selectionArea = UI.section(body, "本人授权选区");
        const detail = $("<div></div>").appendTo(body);
        await this.selections(selectionArea, () => ticket === this.epoch,
          async () => { if (select.val()) await this.open(select.val(), detail); });
        if (ticket !== this.epoch) return;
        select.on("change", () => this.open(select.val(), detail));
        if (select.val()) await this.open(select.val(), detail);
        else UI.text(detail, "目前没有本人参与的论文。学生创建论文后可上传初稿并选择指定教师。", "rg-portal-empty");
      } catch (error) { if (ticket === this.epoch) UI.error($(this.wrapper).find(".layout-main-section"), error); }
    }
    create() {
      const dialog = new frappe.ui.Dialog({title: "创建本人论文", fields: [
        {fieldname: "title", label: "论文标题", fieldtype: "Data", reqd: 1},
        {fieldname: "reviewer", label: "指定审阅教师", fieldtype: "Link", options: "User", reqd: 1,
          get_query: () => ({query: "research_group.services.member_search", filters: {role: "课题组教师"}})},
        {fieldname: "topic", label: "关联本人课题（可留空）", fieldtype: "Link", options: "RG Topic"},
      ], primary_action_label: "创建论文", primary_action: async values => {
        dialog.disable_primary_action();
        try { const result = await UI.api("manuscripts", "create_manuscript", values); dialog.hide(); await this.load(result.name); }
        catch (error) { UI.error(dialog.$wrapper, error); } finally { dialog.enable_primary_action(); }
      }}); dialog.show();
    }
    async open(name, area) {
      const ticket = ++this.detailEpoch;
      this.selected = name; area.empty(); if (!name) return;
      try {
        const result = await UI.api("manuscripts", "get_manuscript", {name});
        if (ticket !== this.detailEpoch) return;
        const manuscript = result.manuscript;
        $("<h3></h3>").text(manuscript.title).appendTo(area);
        UI.text(area, `${STATES[manuscript.status] || manuscript.status} · 学生 ${manuscript.author} · 教师 ${manuscript.reviewer}`, "rg-portal-meta");
        if (manuscript.topic) area.append(UI.link("Form/RG Topic/" + manuscript.topic, "查看关联课题"));
        const versions = result.versions.slice(), reviews = result.reviews.slice();
        const versionArea = UI.section(area, "不可变文件版本");
        const versionsSelect = UI.select(versionArea, "当前查看版本", versions.map(row => ({
          value: row.name, label: `第 ${row.version} 版 · ${STATES[row.kind] || row.kind} · ${UI.date(row.submitted_at)}`,
        })), (versions[0] || {}).name || "");
        const versionDetails = $("<div></div>").appendTo(versionArea);
        const reviewArea = UI.section(area, "教师意见、学生回复与 AI 建议");
        const renderVersion = () => {
          versionDetails.empty();
          const row = versions.find(item => item.name === versionsSelect.val());
          if (!row) { UI.text(versionDetails, "尚未上传文件，请先提交 PDF 或 DOCX 初稿。", "rg-portal-empty"); return; }
          UI.text(versionDetails, `文件 ${row.original_filename} · ${row.size_bytes} 字节`, "rg-portal-meta");
          UI.text(versionDetails, "SHA-256：" + row.sha256, "rg-portal-meta");
          if (typeof row.file_url === "string" && row.file_url.startsWith("/private/files/")) {
            $("<a></a>").attr({href: row.file_url, target: "_blank", rel: "noopener"}).text("下载本版原文件").appendTo(versionDetails);
          }
          const actions = $("<div class='rg-portal-actions'></div>").appendTo(versionDetails);
          if (manuscript.reviewer === this.user) UI.button(actions, "为本版追加教师意见", () => this.comment(row, name, area, ticket));
          UI.button(actions, "选择本人授权段落并获取 AI 建议", () => this.selection(row, versionDetails, manuscript));
        };
        versionsSelect.on("change", renderVersion); renderVersion();
        const moreVersions = UI.button(versionArea, "读取更早文件版本", async () => {
          const page = await UI.api("manuscripts", "list_versions", {manuscript: name, offset: versions.length});
          if (ticket !== this.detailEpoch) return;
          page.items.forEach(row => { versions.push(row); $("<option></option>").val(row.name)
            .text(`第 ${row.version} 版 · ${STATES[row.kind] || row.kind} · ${UI.date(row.submitted_at)}`).appendTo(versionsSelect); });
          moreVersions.toggle(page.has_more);
        }); moreVersions.toggle(result.more_versions);
        const uploadArea = UI.section(area, manuscript.author === this.user ? "提交初稿或修改后的新版" : "上传教师批注文件");
        const upload = UI.field(uploadArea, "PDF / DOCX 文件", "file").attr("accept", ".pdf,.docx");
        UI.text(uploadArea, "上传会生成新版本。若只需写文字意见，可使用上方“追加教师意见”。", "rg-portal-note");
        UI.button(uploadArea, "上传并保留新版本", async () => {
          const file = upload[0].files[0]; if (!file) throw new Error("请先选择 PDF 或 DOCX 文件。");
          const form = new FormData();
          form.append("file", file); form.append("manuscript", name); form.append("expected_revision", manuscript.revision);
          form.append("kind", manuscript.author === this.user ? "student_draft" : "teacher_annotation");
          if (versionsSelect.val()) form.append("parent_version", versionsSelect.val());
          await UI.busy([upload, versionsSelect], async () => {
            const response = await fetch("/api/method/research_group.manuscripts.upload_version", {
              method: "POST", credentials: "same-origin", headers: {"X-Frappe-CSRF-Token": frappe.csrf_token}, body: form,
            });
            const receipt = await response.json();
            if (!response.ok || receipt.exc || !receipt.message || !receipt.message.name) throw receipt;
            if (ticket === this.detailEpoch) await this.open(name, area);
          });
        }, true);
        const reviewList = $("<div></div>").appendTo(reviewArea);
        const renderReviews = () => {
          reviewList.empty();
          if (!reviews.length) UI.text(reviewList, "尚无审阅意见或回复。", "rg-portal-empty");
          reviews.forEach(row => {
            const section = UI.section(reviewList, STATES[row.kind] || row.kind);
            const version = versions.find(item => item.name === row.manuscript_version);
            if (row.blocked) {
              UI.text(section, `${version ? "第 " + version.version + " 版" : "版本 " + row.manuscript_version} · ${UI.date(row.recorded_at)} · ${STATES[row.status] || row.status}`, "rg-portal-meta");
              UI.text(section, row.notice || "来源已撤销或无法验证，相关正文已屏蔽。");
              return;
            }
            UI.text(section, `${version ? "第 " + version.version + " 版" : "版本 " + row.manuscript_version} · ${row.author} · ${UI.date(row.recorded_at)} · ${STATES[row.status] || row.status}`, "rg-portal-meta");
            if (row.anchor) UI.text(section, "位置：" + row.anchor, "rg-portal-meta");
            if (row.parent_review) UI.text(section, "回复意见：" + row.parent_review, "rg-portal-meta");
            if (row.original_text) { $("<h4>选定原文</h4>").appendTo(section); UI.text(section, row.original_text); }
            if (row.kind === "ai_suggestion") $("<h4>AI 建议（已由本人确认保存）</h4>").appendTo(section);
            UI.text(section, row.content);
            if (row.reason) UI.text(section, "修改理由：" + row.reason);
            if (row.kind === "ai_suggestion") UI.text(section, "来源选区：" + row.source_selection + " · SHA-256 " + row.source_sha256, "rg-portal-meta");
            if (row.status_history_notice) UI.text(section, row.status_history_notice, "rg-portal-note");
            if ((row.status_history || []).length) {
              const history = $("<details></details>").appendTo(section);
              $("<summary>查看状态变化记录</summary>").appendTo(history);
              row.status_history.forEach(event => UI.text(history,
                `${UI.date(event.at)} · ${event.actor} · ${STATES[event.before] || event.before} → ${STATES[event.after] || event.after} · ${event.action === "reply" ? "学生回复" : "标记状态"}`,
                "rg-portal-meta"));
            }
            if (row.kind === "student_reply") return;
            const actions = $("<div class='rg-portal-actions'></div>").appendTo(section);
            if (manuscript.author === this.user) {
              UI.button(actions, "追加本人回复", () => {
                const dialog = new frappe.ui.Dialog({title: "回复这条意见", fields: [{fieldname: "content", label: "本人回复", fieldtype: "Small Text", reqd: 1}],
                  primary_action_label: "保存回复", primary_action: async values => {
                    dialog.disable_primary_action();
                    try { await UI.api("manuscripts", "reply_review", {review: row.name, content: values.content}); dialog.hide(); if (ticket === this.detailEpoch) await this.open(name, area); }
                    catch (error) { UI.error(dialog.$wrapper, error); } finally { dialog.enable_primary_action(); }
                  }}); dialog.show();
              });
              if (row.status === "open") UI.button(actions, "标记本人已处理", async () => { await UI.api("manuscripts", "resolve_review", {review: row.name, status: "addressed"}); if (ticket === this.detailEpoch) await this.open(name, area); });
            }
            if (manuscript.reviewer === this.user && row.status !== "closed") UI.button(actions, "教师核对并关闭此意见", async () => { await UI.api("manuscripts", "resolve_review", {review: row.name, status: "closed"}); if (ticket === this.detailEpoch) await this.open(name, area); });
          });
        }; renderReviews();
        const moreReviews = UI.button(reviewArea, "读取更多审阅历史", async () => {
          const page = await UI.api("manuscripts", "list_reviews", {manuscript: name, offset: reviews.length});
          if (ticket !== this.detailEpoch) return;
          reviews.push(...page.items); renderReviews(); moreReviews.toggle(page.has_more);
        }); moreReviews.toggle(result.more_reviews);
      } catch (error) { if (ticket === this.detailEpoch) UI.error(area, error); }
    }
    async selections(area, isCurrent, onRevoke) {
      area.children(":not(h3)").remove();
      UI.text(area, "这里仅列出本人在仍可读论文中的选区，包含已自然到期的授权。有效选区最多 20 个；可撤销旧选区释放容量。撤销不删除原稿或教师意见，相关 AI 建议及其后续回复正文将被屏蔽。", "rg-portal-note");
      const count = UI.text(area, "正在读取本人授权状态。", "rg-portal-meta");
      const actions = $("<div class='rg-portal-actions'></div>").appendTo(area);
      const list = $("<div></div>").appendTo(area);
      let offset = 0, requestEpoch = 0;
      const read = async reset => {
        const ticket = ++requestEpoch;
        const page = await UI.api("manuscripts", "list_selections", {offset: reset ? 0 : offset, limit: 20});
        if (!isCurrent() || ticket !== requestEpoch) return;
        if (reset) { offset = 0; list.empty(); }
        count.text(`本人有效选区 ${page.active_count} / ${page.active_limit}。自然到期后不能再交给 AI；已保存建议仍保留，直到本人撤销授权。`);
        if (!page.items.length && !offset) UI.text(list, "目前没有可管理的本人选区。失去论文权限的选区不会在此显示，其有效容量会在自然到期后释放。", "rg-portal-empty");
        page.items.forEach(row => {
          const section = UI.section(list, `${row.manuscript_title} · 第 ${row.version_number} 版`);
          UI.text(section, `${row.effective_status === "active" ? "有效授权" : "已自然到期"} · 到期时间 ${UI.date(row.expires_at)} · 已保存 AI 建议 ${row.saved_suggestions} 条`, "rg-portal-meta");
          if (row.anchor) UI.text(section, "位置：" + row.anchor, "rg-portal-meta");
          const original = $("<details></details>").appendTo(section);
          $("<summary>查看本人选定原文</summary>").appendTo(original);
          UI.text(original, row.original_text);
          UI.button(section, "撤销此选区授权", async () => {
            await UI.api("manuscripts", "revoke_selection", {name: row.name});
            if (!isCurrent()) return;
            await read(true);
            if (isCurrent()) await onRevoke();
          });
        });
        offset += page.items.length;
        more.toggle(page.has_more);
      };
      UI.button(actions, "刷新本人选区", () => read(true));
      const more = UI.button(actions, "读取更多本人选区", () => read(false));
      more.toggle(false);
      await read(true);
    }
    comment(version, manuscript, area, ticket) {
      const dialog = new frappe.ui.Dialog({title: "追加教师意见", fields: [
        {fieldname: "anchor", label: "位置（页码或段落，可留空）", fieldtype: "Data"},
        {fieldname: "content", label: "教师意见", fieldtype: "Small Text", reqd: 1},
      ], primary_action_label: "保存教师意见", primary_action: async values => {
        dialog.disable_primary_action();
        try { await UI.api("manuscripts", "add_review", {version: version.name, ...values}); dialog.hide(); if (ticket === this.detailEpoch) await this.open(manuscript, area); }
        catch (error) { UI.error(dialog.$wrapper, error); } finally { dialog.enable_primary_action(); }
      }}); dialog.show();
    }
    async selection(version, area, manuscript) {
      const pageEpoch = this.epoch, detailEpoch = this.detailEpoch;
      const route = frappe.get_route().join("/");
      area.find(".rg-manuscript-selection").remove();
      const box = UI.section(area, "本人授权的 AI 段落").addClass("rg-manuscript-selection");
      UI.text(box, "选择或粘贴本版中的原文。模型只收到下方明确授权的段落；建议需在 AI 助手中由本人确认，原文件保持不变。", "rg-portal-note");
      const text = UI.field(box, "实际将交给 AI 的原文（最多 6000 字）", "textarea").attr("maxlength", 6000);
      const anchor = UI.field(box, "选区位置", "text", "pasted").attr("maxlength", 300);
      const requirements = UI.field(box, "修改要求", "textarea", "请润色我选定的论文段落，说明修改理由；保留原意，不增加未经证实的结论。").attr("maxlength", 1000);
      const label = $("<label class='rg-portal-check'></label>").appendTo(box);
      const consent = $("<input type='checkbox'>").appendTo(label);
      $("<span>我已核对以上原文，本次仅授权 AI 处理这一选段。</span>").appendTo(label);
      text.on("input", () => consent.prop("checked", false));
      const paragraphs = $("<div class='rg-manuscript-paragraph'></div>").appendTo(box);
      let offset = 0;
      const read = UI.button(box, "从本版提取文字供本人选择", async () => {
        const result = await UI.api("manuscripts", "get_version_text", {version: version.name, offset});
        if (!box.closest("body").length || !box.is(":visible")
            || frappe.get_route().join("/") !== route
            || this.epoch !== pageEpoch || this.detailEpoch !== detailEpoch) return;
        result.paragraphs.forEach(row => {
          $("<button type='button'></button>").attr("aria-label", "选取第 " + (row.index + 1) + " 段")
            .text(row.text).appendTo(paragraphs).on("click", () => { text.val(row.text); anchor.val("paragraph:" + row.index); consent.prop("checked", false); text.trigger("focus"); });
        });
        offset += result.paragraphs.length;
        read.text("读取更多段落供本人选择").toggle(result.has_more);
        if (result.truncated) UI.text(box, "可提取正文受容量限制；请先核对实际显示的原文。PDF 文字顺序可能与版面不同。", "rg-portal-note");
        if (!result.paragraphs.length && !offset) UI.text(paragraphs, "此文件未提取到可选文字，可下载原文件核对；系统不会自动把整篇文件交给模型。", "rg-portal-empty");
      });
      UI.button(box, "确认选段并在 AI 助手中生成建议", async () => {
        if (!consent.prop("checked")) throw new Error("请先核对实际原文，并勾选本次选段授权。");
        if (!text.val().trim()) throw new Error("请先选择或粘贴本版中的原文。");
        const snapshot = {text: text.val(), anchor: anchor.val(), requirements: requirements.val()};
        const stillAuthorized = () => box.closest("body").length && box.is(":visible")
          && frappe.get_route()[0] === "rg-manuscripts" && frappe.get_route().join("/") === route
          && this.epoch === pageEpoch && this.detailEpoch === detailEpoch
          && text.val() === snapshot.text && anchor.val() === snapshot.anchor
          && requirements.val() === snapshot.requirements && consent.prop("checked");
        if (!stillAuthorized()) throw new Error("选段或页面已变化，请重新核对选段。");
        const agentPortal = manuscript.author === this.user ? "student"
          : manuscript.reviewer === this.user ? "teacher" : null;
        if (!agentPortal) throw new Error("当前论文不属于可用的 AI 工作台。");
        const selected = await UI.api("manuscripts", "create_selection", {version: version.name, original_text: snapshot.text, anchor: snapshot.anchor});
        if (!stillAuthorized()) throw new Error("选段或页面已变化，本次尚未发送给 AI；请重新核对选段。");
        const agentRoute = "rg-" + agentPortal + "-agent";
        const conversation = await UI.api("ai.api", "create_conversation", {portal: agentPortal});
        if (!stillAuthorized()) throw new Error("选段或页面已变化，本次尚未发送给 AI；请重新核对选段。");
        rg.pendingAIConversation = conversation.name;
        rg.pendingAIPortal = agentPortal;
        try {
          await UI.api("ai.api", "send_message", {conversation: conversation.name,
            prompt: snapshot.requirements || "请对本人授权的论文选段给出修改建议并说明理由。",
            selections: [selected.name], reasoning_effort: "medium"});
        } catch (_) {
          if (!stillAuthorized()) return;
          UI.text(box, "未收到本轮入队回执，请在 AI 助手查看本人会话与本轮状态，避免重复发送。", "rg-portal-note");
          UI.button(box, "打开 AI 助手核对本轮状态", () => UI.go(agentRoute));
          return;
        }
        if (stillAuthorized()) UI.go(agentRoute);
      }, true);
    }
  }
  rg.ManuscriptsPage = ManuscriptsPage;
})();
