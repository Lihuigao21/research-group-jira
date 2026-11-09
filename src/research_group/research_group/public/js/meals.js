(() => {
  const rg = window.research_group, UI = rg.PortalUI;
  class MealsPage {
    constructor(wrapper) { this.wrapper = wrapper; this.epoch = 0; this.request = null; this.ready = this.load(); }
    async load(selected = null) {
      const ticket = ++this.epoch;
      try {
        const shell = await UI.shell(this.wrapper, "工作餐", null, () => ticket === this.epoch);
        if (!shell) return;
        const {body, data} = shell;
        const rounds = await UI.api("meals", "list_rounds");
        if (ticket !== this.epoch) return;
        const canManage = data.portals.some(item => item.id === "secretary");
        UI.text(body, "每人每轮一单。截止前可修改本人同一订单，金额由服务器计算。", "rg-portal-note");
        if (canManage) UI.button(body, "新建工作餐轮次", () => this.editRound());
        const options = [{value: "", label: "请选择工作餐轮次"}, ...rounds.map(row => ({value: row.name, label: row.title + " · " + row.status}))];
        const select = UI.select(body, "工作餐轮次", options, selected || (rounds.find(row => row.status === "已发布") || rounds[0] || {}).name || "");
        const detail = $("<div></div>").appendTo(body);
        select.on("change", () => this.round(select.val(), detail));
        if (select.val()) await this.round(select.val(), detail);
        else UI.text(detail, "目前没有工作餐轮次。", "rg-portal-empty");
        if (canManage && rounds.length) {
          const exports = UI.section(body, "多轮汇总导出");
          const choices = new Set();
          rounds.forEach(row => { const label = $("<label class='rg-portal-check'></label>").appendTo(exports);
            $("<input type='checkbox'>").appendTo(label).on("change", event => { if (event.target.checked) choices.add(row.name); else choices.delete(row.name); });
            $("<span></span>").text(row.title + " · " + row.status).appendTo(label); });
          UI.button(exports, "导出所选轮次 ZIP（最多 50 轮）", () => {
            if (!choices.size || choices.size > 50) throw new Error("请选择 1 至 50 轮。");
            this.download("export_zip", {rounds: [...choices]});
          });
        }
      } catch (error) { if (ticket === this.epoch) UI.error($(this.wrapper).find(".layout-main-section"), error); }
    }
    download(method, args) {
      const form = $("<form method='POST' target='_blank' hidden></form>").attr("action", "/api/method/research_group.meals." + method).appendTo(document.body);
      const fields = {...args, csrf_token: frappe.csrf_token};
      Object.entries(fields).forEach(([key, value]) => $("<input type='hidden'>").attr("name", key).val(typeof value === "object" ? JSON.stringify(value) : value).appendTo(form));
      form[0].submit(); form.remove();
    }
    async editRound(round = null, copy = false) {
      const editing = round && !copy;
      const defaults = JSON.parse(JSON.stringify(round ? round.menu : await UI.api("meals", "default_menu")));
      const dialog = new frappe.ui.Dialog({title: editing ? "编辑工作餐草稿" : "新建工作餐轮次", size: "large", fields: [
        {fieldname: "title", label: "轮次名称", fieldtype: "Data", reqd: 1, default: round ? round.title + (copy ? "（新轮次）" : "") : ""},
        {fieldname: "deadline", label: "截止时间（上海时间）", fieldtype: "Datetime", default: editing ? round.deadline : ""},
        {fieldname: "meeting", label: "关联组会", fieldtype: "Link", options: "RG Group Meeting", default: round ? round.meeting : ""},
        {fieldname: "menu", fieldtype: "HTML"},
      ], primary_action_label: "保存草稿", primary_action: async values => {
        dialog.disable_primary_action();
        try {
          editors.forEach(({item, name, price, description, staples, fixedStaple}) => {
            item.name = name.val(); const raw = price.val();
            if (!/^\d+(?:\.\d{1,2})?$/.test(raw)) throw new Error("价格须为非负金额，最多两位小数。");
            const [whole, fraction = ""] = raw.split("."); item.priceCents = Number(whole) * 100 + Number(fraction.padEnd(2, "0"));
            item.description = description.val();
            if (staples) item.staples = staples.val().split("\n").map(value => value.trim()).filter(Boolean);
            if (fixedStaple) item.fixedStaple = fixedStaple.val();
          });
          const result = await UI.api("meals", editing ? "update_draft" : "create_round", {title: values.title,
            deadline: values.deadline || "", meeting: values.meeting || "", menu: defaults,
            ...(editing ? {name: round.name, expected_revision: round.revision} : {})});
          dialog.hide(); await this.load(result.name);
        } catch (error) { UI.error(dialog.$wrapper, error); } finally { dialog.enable_primary_action(); }
      }});
      const editor = dialog.fields_dict.menu.$wrapper;
      UI.text(editor, "保留 7 主餐、7 加料和参考图；可修改草稿菜名、价格、主食与配菜说明。发布后菜单冻结。", "rg-portal-note");
      const editors = defaults.items.map(item => {
        const section = UI.section(editor, (item.category === "meal" ? "主餐" : "加料") + " · " + item.name);
        const grid = $("<div class='rg-portal-grid'></div>").appendTo(section);
        const name = UI.field(grid, "菜名", "text", item.name), price = UI.field(grid, "价格（元）", "text", UI.money(item.priceCents));
        const description = UI.field(section, "配菜与说明", "textarea", item.description);
        const staples = item.staples ? UI.field(section, "可选主食（每行一项）", "textarea", item.staples.join("\n")) : null;
        const fixedStaple = item.fixedStaple ? UI.field(section, "固定主食", "text", item.fixedStaple) : null;
        return {item, name, price, description, staples, fixedStaple};
      });
      dialog.show();
    }
    async round(name, area) {
      const ticket = this.roundEpoch = (this.roundEpoch || 0) + 1;
      area.empty(); if (!name) return;
      try {
        const round = await UI.api("meals", "get_round", {name});
        if (ticket !== this.roundEpoch) return;
        const order = round.my_order;
        $("<h3></h3>").text(round.title).appendTo(area);
        UI.text(area, `${round.status} · 截止 ${UI.date(round.deadline)} · 服务器时间 ${UI.date(round.server_time)}`, "rg-portal-meta");
        if (round.can_manage) {
          const actions = $("<div class='rg-portal-actions'></div>").appendTo(area);
          UI.button(actions, "复制菜单为新轮次", () => this.editRound(round, true));
          if (round.status === "草稿") {
            UI.button(actions, "编辑菜单草稿", () => this.editRound(round));
            UI.button(actions, "发布并冻结菜单", async () => { await UI.api("meals", "publish_round", {name, expected_revision: round.revision}); if (ticket === this.roundEpoch) await this.round(name, area); }, true);
          } else {
            UI.button(actions, round.status === "已关闭" ? "重新开放" : "关闭本轮", async () => {
              await UI.api("meals", "set_round_status", {name, closed: round.status !== "已关闭", expected_revision: round.revision}); if (ticket === this.roundEpoch) await this.round(name, area);
            });
            UI.button(actions, "调整截止时间", () => {
              const dialog = new frappe.ui.Dialog({title: "调整截止时间", fields: [{fieldname: "deadline", fieldtype: "Datetime", label: "截止时间（上海时间）", reqd: 1, default: round.deadline}], primary_action_label: "保存截止时间", primary_action: async values => {
                try { await UI.api("meals", "set_deadline", {name, deadline: values.deadline, expected_revision: round.revision}); dialog.hide(); if (ticket === this.roundEpoch) await this.round(name, area); } catch (error) { UI.error(dialog.$wrapper, error); }
              }}); dialog.show();
            });
          }
          UI.button(actions, "查看实名统计与导出", () => this.results(name, area, ticket));
        }
        if (round.status === "草稿") { UI.text(area, "菜单尚未发布，暂不能订餐。", "rg-portal-empty"); return; }
        if (order) {
          const saved = UI.section(area, "本人已保存订单");
          UI.text(saved, `${order.status} · ¥${UI.money(order.total_cents)} · 修改于 ${UI.date(order.updated_at || order.submitted_at)}`);
          UI.table(saved, ["餐品", "数量", "主食", "金额"], order.items.map(item => [item.name, item.quantity, item.staple || "", "¥" + UI.money(item.subtotalCents)]));
          if (order.status === "已作废") UI.text(saved, "本单已由秘书作废并留痕，不能自行重开。", "rg-portal-note");
        }
        const canEdit = round.accepting_orders && (!order || order.status !== "已作废");
        const menu = UI.section(area, order ? "修改本人订单" : "填写本人订单");
        const current = new Map((order ? order.items : []).map(item => [item.id, item]));
        const cards = $("<div class='rg-meal-menu'></div>").appendTo(menu);
        const selections = [];
        const menuItems = round.menu.items;
        menuItems.forEach(item => {
          const li = $("<article class='rg-meal-item'></article>").toggleClass("rg-meal-addon", item.category === "addon").appendTo(cards);
          const hasImage = Boolean(item.imageUrl && /^\/assets\/research_group\/menu\/[a-zA-Z0-9/._-]+$/.test(item.imageUrl));
          // Menu photos are deployment-supplied; a missing file falls back to a placeholder instead of a broken layout.
          li.toggleClass("rg-meal-noimg", item.category !== "addon" && !hasImage);
          if (hasImage) $("<img loading='lazy'>").attr({src: item.imageUrl, alt: item.name + "参考图"})
            .on("error", event => { $(event.target).remove(); li.addClass("rg-meal-noimg"); }).appendTo(li);
          const detail = $("<div></div>").appendTo(li); $("<h4></h4>").text(item.name).appendTo(detail);
          UI.text(detail, "¥" + UI.money(item.priceCents), "rg-portal-meta rg-meal-price"); UI.text(detail, item.description);
          if (item.spice) UI.text(detail, "辣度 " + item.spice + "/5", "rg-portal-meta rg-meal-spice");
          const controls = $("<div class='rg-meal-quantity'></div>").appendTo(detail);
          const quantity = UI.select(controls, "份数", Array.from({length: 21}, (_, number) => String(number)), String((current.get(item.id) || {}).quantity || 0)).prop("disabled", !canEdit);
          const stapleOptions = item.staples || (item.fixedStaple ? [item.fixedStaple] : []);
          const staple = stapleOptions.length ? UI.select(controls, "主食", stapleOptions, (current.get(item.id) || {}).staple || stapleOptions[0]).prop("disabled", !canEdit) : null;
          selections.push({item, quantity, staple, card: li});
        });
        const remarks = UI.field(menu, "备注（最多 300 字）", "textarea", order ? order.remarks : "").attr("maxlength", 300).prop("disabled", !canEdit);
        const bar = $("<div class='rg-meal-bar'></div>").appendTo(menu);
        const total = $("<p class='rg-meal-total'></p>").appendTo(bar);
        const label = $("<label class='rg-portal-check'></label>").appendTo(bar);
        const acknowledge = $("<input type='checkbox'>").appendTo(label); $("<span>合计严格超过 50 元，我已核对并确认本次超额。</span>").appendTo(label);
        let submitting = false;
        const recalculate = () => {
          if (submitting) return;
          const cents = selections.reduce((sum, row) => sum + row.item.priceCents * Number(row.quantity.val()), 0);
          selections.forEach(row => row.card.toggleClass("rg-meal-selected", Number(row.quantity.val()) > 0));
          total.text("合计 ¥" + UI.money(cents)); label.toggle(cents > 5000); acknowledge.prop("checked", false);
          this.request = null; return cents;
        };
        selections.forEach(row => { row.quantity.on("change", recalculate); if (row.staple) row.staple.on("change", recalculate); }); remarks.on("input", recalculate); recalculate();
        if (!canEdit) { UI.text(menu, "本轮目前不能提交或修改，请保留已保存订单。", "rg-portal-note"); return; }
        UI.button(bar, order ? "保存本人订单修改" : "提交本人订单", async () => {
          const items = selections.filter(row => Number(row.quantity.val()) > 0).map(row => ({id: row.item.id, quantity: Number(row.quantity.val()), staple: row.staple ? row.staple.val() : ""}));
          const cents = selections.reduce((sum, row) => sum + row.item.priceCents * Number(row.quantity.val()), 0);
          if (!items.some(item => menuItems.find(row => row.id === item.id).category === "meal")) throw new Error("请至少选择一份主餐。");
          if (cents > 5000 && !acknowledge.prop("checked")) throw new Error("本次合计超过 50 元，请本人确认超额。");
          this.request = this.request || ("meal-" + (window.crypto.randomUUID ? window.crypto.randomUUID() : Date.now() + "-" + Math.random().toString(36).slice(2)));
          const requestId = this.request;
          const args = {round: name, items, remarks: remarks.val(), expected_revision: order ? order.revision : 0,
            budget_acknowledged: cents > 5000 && acknowledge.prop("checked"), request_id: requestId};
          const fields = [...selections.flatMap(row => [row.quantity, row.staple].filter(Boolean)), remarks, acknowledge];
          submitting = true;
          try {
            await UI.busy(fields, async () => {
              const receipt = await UI.api("meals", "submit_order", args);
              if (this.request === requestId) this.request = null;
              if (ticket === this.roundEpoch) { UI.status(menu, "订单已保存：" + receipt.name); await this.round(name, area); }
            });
          } finally { submitting = false; }
        }, true);
      } catch (error) { if (ticket === this.roundEpoch) UI.error(area, error); }
    }
    async results(name, area, ticket = this.roundEpoch) {
      if (ticket !== this.roundEpoch) return;
      const data = await UI.api("meals", "round_results", {round: name});
      if (ticket !== this.roundEpoch) return;
      area.find(".rg-meal-results").remove();
      const results = UI.section(area, "实名统计与导出").addClass("rg-meal-results");
      UI.text(results, `${data.order_count} 单 · ¥${UI.money(data.total_cents)} · 超额 ${data.over_budget_count} 单`);
      UI.table(results, ["菜品", "份数", "主食汇总", "金额"], data.items.map(item => [item.name, item.quantity, Object.entries(item.staples || {}).map(([staple, count]) => staple + " " + count + "份").join("；"), "¥" + UI.money(item.totalCents)]));
      data.orders.forEach(order => {
        const section = UI.section(results, order.member_name || order.member);
        UI.text(section, `¥${UI.money(order.total_cents)}${order.over_budget ? " · 超 50 元" : ""} · ${UI.date(order.updated_at || order.submitted_at)}`, "rg-portal-meta");
        UI.text(section, order.items.map(item => item.name + "×" + item.quantity + (item.staple ? "（" + item.staple + "）" : "")).join("；"));
        if (order.remarks) UI.text(section, "备注：" + order.remarks);
        UI.button(section, "作废误填订单并留痕", () => {
          const dialog = new frappe.ui.Dialog({title: "作废误填订单", fields: [{fieldname: "reason", label: "作废原因", fieldtype: "Small Text", reqd: 1}], primary_action_label: "确认作废", primary_action: async values => {
            try { await UI.api("meals", "void_order", {order: order.name, reason: values.reason, expected_revision: order.revision}); dialog.hide(); if (ticket === this.roundEpoch) await this.results(name, area, ticket); } catch (error) { UI.error(dialog.$wrapper, error); }
          }}); dialog.show();
        });
      });
      if (data.voided_orders.length) UI.table(results, ["作废订单", "成员", "原因", "时间"], data.voided_orders.map(order => [order.name, order.member_name || order.member, order.void_reason, UI.date(order.voided_at)]));
      const actions = $("<div class='rg-portal-actions'></div>").appendTo(results);
      UI.button(actions, "导出逐人 CSV", () => this.download("export_csv", {round: name, kind: "orders"}));
      UI.button(actions, "导出餐品汇总 CSV", () => this.download("export_csv", {round: name, kind: "items"}));
      UI.button(actions, "生成商家文本", async () => { const result = await UI.api("meals", "merchant_text", {round: name});
        UI.field(results, "商家汇总文本", "textarea", result.text).prop("readOnly", true); });
    }
  }
  rg.MealsPage = MealsPage;
})();
