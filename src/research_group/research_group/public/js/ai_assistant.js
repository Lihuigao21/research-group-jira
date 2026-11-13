/* Dedicated same-origin UI. Model text is always textContent, never HTML/Markdown. */
window.research_group = window.research_group || {};
(function (ns) {
  "use strict";
  const STATES = {queued: "排队中", running: "生成中", completed: "已完成", pending: "待确认", consumed: "已提交",
    cancelled: "已取消", failed: "失败", expired: "已过期", active: "有效", candidate: "候选（未生效）", deleted: "已删除"};
  const LABELS = {change: "变更", name: "对象", kind: "记忆类型", content: "内容", topic: "课题", status: "个人状态",
    teacher: "指定教师", progress_date: "进展日期", summary: "本次进展", blockers: "当前阻碍", next_milestone: "下一步",
    start_at: "开始时间", end_at: "结束时间", agenda: "讨论议题", selection: "授权论文选区",
    original_text: "选定原文", suggestion: "AI辅助建议", reason: "建议理由", manuscript_version: "来源版本"};
  const VALUES = {add: "新增", edit: "修改", delete: "删除", preference: "回答偏好", background: "本人背景",
    follow_up: "待跟进事项", active: "进行中", completed: "个人已完成"};
  const PORTALS = {teacher: {title: "教师 AI 助手", role: "课题组教师"},
    secretary: {title: "秘书 AI 助手", role: "课题组秘书"},
    student: {title: "学生 AI 助手", role: "课题组学生"}};
  const EFFORT = {low: "低", medium: "中", high: "高"};
  const ACTIONS = {memory_change: "记忆变更", plan_change: "计划变更", progress: "研究进展",
    appointment: "讨论预约", manuscript_suggestion: "论文建议"};
  function node(tag, cls, text) {
    const el = document.createElement(tag);
    if (cls) el.className = cls;
    if (text != null) el.textContent = String(text);
    return el;
  }
  function button(label, action, cls = "btn btn-default btn-sm") {
    const el = node("button", cls, label); el.type = "button"; el.addEventListener("click", action); return el;
  }
  function safeSource(url) {
    if (typeof url !== "string" || !/^\/desk\/[a-z0-9-]+\/[^/\s\\?#]+$/i.test(url)) return false;
    try {
      const name = decodeURIComponent(url.split("/").pop());
      return name !== "." && name !== ".." && !/[\/\\\x00-\x20]/.test(name);
    } catch (_) {return false;}
  }
  function sources(parent, refs) {
    if (!refs || !refs.length) return;
    const list = node("details", "rg-ai-sources");
    list.append(node("summary", "", "原记录来源（" + refs.length + "）"));
    for (const ref of refs) {
      if (!safeSource(ref.url)) continue;
      const link = node("a", "rg-ai-source", ref.doctype + " · " + ref.name);
      link.setAttribute("href", ref.url); list.append(link);
    }
    parent.append(list);
  }
  class AIAssistant {
    constructor(wrapper, portal) {
      if (!Object.hasOwn(PORTALS, portal)) throw new Error("未知工作台。");
      this.wrapper = wrapper; this.portal = portal; this.current = null; this.epoch = 0; this.loadSequence = 0; this.panelSequence = 0;
      this.running = null; this.sending = false; this.sendingConversation = null;
      this.pendingReceipt = null; this.timer = null; this.hidden = false; this.historyOffset = 0;
      this.api = async (method, args = {}) => {
        const result = await frappe.call({method: "research_group.ai.api." + method, args, type: "POST"});
        if (result.exc) throw new Error("请求未成功");
        return result.message;
      };
      this.build(); this.start();
    }
    build() {
      this.root = node("section", "rg-ai"); this.root.setAttribute("aria-label", PORTALS[this.portal].title);
      this.wrapper.querySelector(".layout-main-section").append(this.root);
      this.notice = node("div", "rg-ai-notice", "正在读取本人助手配置…"); this.root.append(this.notice);
      const identity = node("div", "rg-ai-identity"); this.root.append(identity);
      identity.append(node("h2", "", PORTALS[this.portal].title));
      const model = node("p", "rg-ai-model-line");
      this.modelPrefix = node("span", "", "默认模型："); model.append(this.modelPrefix);
      this.model = node("strong", "", "gpt-6.1-sol"); model.append(this.model); identity.append(model);
      const effortLabel = node("label", "rg-ai-effort-label", "推理强度");
      this.reasoning = node("select", "form-control rg-ai-effort");
      for (const [value, label] of Object.entries(EFFORT)) {
        const option = node("option", "", label); option.value = value; this.reasoning.append(option);
      }
      this.reasoning.value = "medium"; effortLabel.append(this.reasoning); identity.append(effortLabel);
      const legacy = node("a", "rg-ai-legacy-link", "查看旧版混合会话（只读）");
      legacy.href = "/desk/rg-ai-assistant"; identity.append(legacy);
      const layout = node("div", "rg-ai-layout"); this.root.append(layout);
      this.history = node("details", "rg-ai-history"); this.history.open = window.innerWidth > 800;
      this.history.append(node("summary", "rg-ai-history-title", "历史会话"));
      this.history.append(button("＋ 新会话", () => this.newConversation(), "btn btn-primary btn-sm rg-ai-new"));
      this.historyList = node("nav", "rg-ai-history-list"); this.historyList.setAttribute("aria-label", "我的历史会话");
      this.history.append(this.historyList);
      this.moreHistory = button("更多会话", () => this.loadHistory(true)); this.moreHistory.hidden = true;
      this.history.append(this.moreHistory); layout.append(this.history);
      const main = node("main", "rg-ai-main"); layout.append(main);
      const toolbar = node("div", "rg-ai-toolbar"); main.append(toolbar);
      const topicLabel = node("label", "rg-ai-topic-label", "当前上下文");
      this.topic = node("select", "form-control rg-ai-topic"); this.topic.setAttribute("aria-label", "选择课题上下文");
      topicLabel.append(this.topic); toolbar.append(topicLabel);
      toolbar.append(button("我的记忆", () => this.showPrivate("memory")), button("我的计划", () => this.showPrivate("plan")));
      this.deleteButton = button("删除会话", () => this.removeConversation()); this.deleteButton.disabled = true;
      toolbar.append(this.deleteButton);
      this.quick = node("div", "rg-ai-quick"); main.append(this.quick);
      this.messages = node("div", "rg-ai-messages"); this.messages.setAttribute("aria-label", "会话消息");
      this.messages.setAttribute("aria-live", "polite"); main.append(this.messages);
      this.ops = node("div", "rg-ai-operations"); this.ops.setAttribute("aria-label", "待本人确认的操作"); main.append(this.ops);
      this.status = node("p", "rg-ai-status", "新建或选择会话后开始聊天。"); this.status.setAttribute("role", "status"); main.append(this.status);
      const form = node("form", "rg-ai-composer"); main.append(form);
      const label = node("label", "", "发送消息");
      this.input = node("textarea", "form-control"); this.input.rows = 3; this.input.maxLength = 12000;
      this.input.placeholder = "询问本站操作，或根据正式反馈规划下一步…"; this.input.setAttribute("aria-label", "消息，最多12000字");
      label.append(this.input); form.append(label);
      const actions = node("div", "rg-ai-compose-actions"); form.append(actions);
      this.sendButton = button("发送", () => this.send(), "btn btn-primary"); actions.append(this.sendButton);
      this.cancelButton = button("取消本轮", () => this.cancelRun()); this.cancelButton.hidden = true; actions.append(this.cancelButton);
      this.recheck = button("查询本轮状态", () => this.checkRun()); this.recheck.hidden = true; actions.append(this.recheck);
      actions.append(button("保存为计划", () => this.editItem("plan")), button("新增记忆", () => this.editItem("memory")));
      this.progressButton = button("追加正式进展", () => this.businessProposal("progress"));
      this.appointmentButton = button("申请预约", () => this.businessProposal("appointment"));
      actions.append(this.progressButton, this.appointmentButton);
      form.addEventListener("submit", event => {event.preventDefault(); this.send();});
      this.input.addEventListener("keydown", event => {if ((event.ctrlKey || event.metaKey) && event.key === "Enter") {event.preventDefault(); this.send();}});
      this.panel = node("aside", "rg-ai-panel"); this.panel.hidden = true; layout.append(this.panel);
    }
    setStatus(message, busy = false) {
      this.status.dataset.busy = busy ? "true" : "false";
      const text = node("span", "", message);
      const spinner = node("span", "rg-ai-spinner"); spinner.setAttribute("aria-hidden", "true");
      this.status.replaceChildren(...(busy ? [spinner, text] : [text]));
    }
    runStatus(run) {
      const label = STATES[run.status] || run.status || "处理中";
      return label + (EFFORT[run.reasoning_effort] ? " · 推理强度：" + EFFORT[run.reasoning_effort] : "");
    }
    error(message = "请求失败，请检查当前状态后重试。") { this.setStatus(message); }
    async start() {
      try {
        this.boot = await this.api("bootstrap", {portal: this.portal});
        if (this.boot.portal && this.boot.portal !== this.portal) throw new Error("工作台不匹配。");
        this.notice.textContent = this.boot.configuration.label + " · " + this.boot.privacy_notice;
        this.modelPrefix.textContent = ["mock", "disabled"].includes(this.boot.configuration.mode) ? "默认模型：" : "当前模型：";
        this.model.textContent = this.boot.configuration.model || "gpt-6.1-sol";
        const defaultEffort = this.boot.configuration.default_reasoning_effort || this.boot.configuration.reasoning_default;
        this.reasoning.value = Object.hasOwn(EFFORT, defaultEffort) ? defaultEffort : "medium";
        this.topic.replaceChildren(node("option", "", "站内操作帮助")); this.topic.firstChild.value = "";
        for (const topic of this.boot.topics) {const option = node("option", "", topic.title + " · " + topic.name); option.value = topic.name; this.topic.append(option);}
        const shortcuts = [];
        if (this.portal === "teacher") shortcuts.push("全组最近讨论");
        if (this.portal === "student") shortcuts.push("根据上次反馈安排工作", "查看我的下次组会");
        if (this.portal === "secretary") shortcuts.push("近期排期", "问卷完成概况");
        this.quick.replaceChildren(...shortcuts.map(label => button(label, () => {this.input.value = label; this.send();}, "btn btn-light btn-sm")));
        this.progressButton.hidden = this.appointmentButton.hidden = this.portal !== "student";
        this.renderHistory(this.boot.conversations, false);
        if (await this.openPendingConversation()) return;
        if (!this.current && this.epoch === 0) {
          if (this.boot.conversations.items.length) await this.select(this.boot.conversations.items[0].name);
          else this.renderEmpty();
        }
      } catch (_) { this.error("无法读取助手。请确认账号已启用并具有课题组成员身份。"); }
    }
    renderEmpty() {this.messages.replaceChildren(node("div", "rg-ai-empty", "这是你的私人站内会话。可询问本站操作、查询获准记录、整理计划，或从论文页明确选段后请求修改建议。正式写入须本人确认。"));}
    async openPendingConversation() {
      const name = ns.pendingAIConversation;
      if (typeof name !== "string" || !/^[A-Za-z0-9_-]{1,140}$/.test(name)) return false;
      if (ns.pendingAIPortal && ns.pendingAIPortal !== this.portal) return false;
      delete ns.pendingAIConversation;
      delete ns.pendingAIPortal;
      clearTimeout(this.timer); this.running = null; this.pendingReceipt = null;
      await this.select(name);
      return true;
    }
    renderHistory(data, append) {
      if (!append) this.historyList.replaceChildren();
      for (const item of data.items) {
        const entry = button(item.title || "新会话", () => this.select(item.name), "rg-ai-history-entry");
        entry.dataset.name = item.name; entry.setAttribute("title", item.title || "新会话"); this.historyList.append(entry);
      }
      this.historyOffset = data.offset + data.items.length; this.moreHistory.hidden = !data.has_more;
      this.markCurrent();
    }
    markCurrent() {for (const el of this.historyList.children) el.setAttribute("aria-current", el.dataset.name === this.current ? "page" : "false");}
    async loadHistory(append = false) {
      const sequence = this.historySequence = (this.historySequence || 0) + 1;
      try {
        const result = await this.api("list_conversations", {portal: this.portal, offset: append ? this.historyOffset : 0});
        if (this.historySequence === sequence) this.renderHistory(result, append);
      } catch (_) {if (this.historySequence === sequence) this.error();}
    }
    async newConversation() {
      const epoch = this.epoch, sequence = this.creationSequence = (this.creationSequence || 0) + 1;
      try {
        const item = await this.api("create_conversation", {portal: this.portal});
        if (this.epoch === epoch && this.creationSequence === sequence) await this.select(item.name);
        await this.loadHistory();
        return this.current === item.name ? item.name : null;
      } catch (_) {this.error();}
    }
    async select(name) {
      this.current = name; this.epoch++; this.before = null; this.lastAnswer = ""; this.deleteButton.disabled = false; this.markCurrent();
      this.ops.replaceChildren(); this.panel.hidden = true; this.panelSequence++;
      this.cancelButton.hidden = !this.running || this.running.conversation !== name;
      this.recheck.hidden = !(this.running && this.running.conversation === name) && this.pendingReceipt !== name;
      this.setStatus("正在读取本人会话…");
      if (window.innerWidth <= 800) this.history.open = false;
      await this.loadConversation();
    }
    isCurrent(sequence, name, epoch) {return this.loadSequence === sequence && this.current === name && this.epoch === epoch;}
    async loadConversation(older = false) {
      if (!this.current) return;
      const name = this.current, epoch = this.epoch, sequence = ++this.loadSequence;
      try {
        const data = await this.api("get_conversation", {name, before: older ? this.before : undefined});
        if (!this.isCurrent(sequence, name, epoch)) return;
        this.renderConversation(data, older);
        if (!older) await this.recoverRun(data, name);
      } catch (_) {if (this.isCurrent(sequence, name, epoch)) this.error("会话不可用，或权限已经变化。请刷新历史。");}
    }
    async recoverRun(data, conversation) {
      if (this.running) return;
      const message = [...(data.messages || [])].reverse().find(item => item.request_id);
      if (!message) return;
      const run = {run_id: message.request_id, conversation, deadline: Date.now() + 170000};
      try {
        const result = await this.api("get_run", {run_id: run.run_id});
        if (this.running || !["queued", "running"].includes(result.status) || result.conversation !== conversation) return;
        this.running = {...run, ...result};
        if (this.current === conversation) this.setStatus(this.runStatus(result), true);
        if (!this.hidden) this.timer = setTimeout(() => this.checkRun(), 1800);
      } catch (_) {
        if (this.running) return;
        // The owner-scoped message identifies a round even when its status receipt is lost.
        this.running = run;
        if (this.current === conversation) this.error("本轮状态暂不可用，请查询本轮状态；不会自动重新发送。");
      }
      this.sendButton.disabled = true;
      this.cancelButton.hidden = this.current !== conversation;
      this.recheck.hidden = this.current !== conversation;
    }
    async recoverSubmission(conversation) {
      const epoch = this.epoch;
      const data = await this.api("get_conversation", {name: conversation});
      if (this.current === conversation && this.epoch === epoch) this.renderConversation(data, false);
      await this.recoverRun(data, conversation);
      if (this.pendingReceipt === conversation) this.pendingReceipt = null;
      this.sendButton.disabled = !!this.sending || !!this.running || !!this.pendingReceipt;
      if (!this.running && !this.pendingReceipt) this.recheck.hidden = true;
    }
    renderConversation(data, older) {
      const fragment = document.createDocumentFragment();
      if (!older) this.lastAnswer = "";
      if (data.has_older) fragment.append(button("读取更早消息", () => this.loadConversation(true), "btn btn-default btn-sm rg-ai-older"));
      this.before = data.before;
      for (const message of data.messages) {
        if (message.role === "tool") continue;
        const item = node("article", "rg-ai-message rg-ai-" + message.role);
        item.append(node("div", "rg-ai-message-label", message.role === "user" ? "你" : "助手"));
        item.append(node("div", "rg-ai-message-text", message.content)); sources(item, message.sources); fragment.append(item);
        if (!older && message.role === "assistant" && !message.blocked) this.lastAnswer = message.content;
      }
      if (older) {
        const oldButton = this.messages.querySelector(".rg-ai-older"); if (oldButton) oldButton.remove();
        this.messages.prepend(fragment);
      } else {
        this.messages.replaceChildren(fragment); if (!data.messages.some(row => row.role !== "tool")) this.renderEmpty();
        this.messages.scrollTop = this.messages.scrollHeight;
      }
      this.renderOperations(data.operations);
      if (this.running && this.running.conversation === this.current && this.running.status) {
        this.setStatus(this.runStatus(this.running), true);
      } else if (this.sending && this.sendingConversation === this.current) this.setStatus("正在提交本轮…", true);
      else if (this.running || this.sending || this.pendingReceipt) this.setStatus("另一会话正在处理，请回到原会话查看状态。");
      else this.setStatus("会话仅本人及当前工作台可见。");
    }
    renderOperations(items) {
      const fragment = document.createDocumentFragment();
      for (const item of items) {
        const card = node("section", "rg-ai-operation"); card.dataset.operation = item.name;
        const preview = item.preview || {};
        card.append(node("h3", "", preview.label || "操作回执"));
        card.append(node("p", "rg-ai-operation-state", STATES[item.status] || item.status));
        if (item.status === "pending" && !item.stale && preview.parameters) {
          const fields = node("dl", "rg-ai-preview");
          if (preview.original_text) fields.append(node("dt", "", "选定原文"), node("dd", "", preview.original_text));
          if (preview.manuscript_version) fields.append(node("dt", "", "来源版本"), node("dd", "", preview.manuscript_version));
          for (const [key, value] of Object.entries(preview.parameters)) {
            if (value == null || value === "") continue;
            fields.append(node("dt", "", LABELS[key] || key), node("dd", "", VALUES[value] || value));
          }
          card.append(fields, node("p", "", preview.scope), node("p", "rg-ai-notice-small", preview.notice),
            node("p", "text-muted", "有效期至 " + item.expires_at + "（Asia/Shanghai）")); sources(card, item.sources);
          const confirm = button("确认内容并提交", () => this.decide(item, "confirm", confirm, cancel), "btn btn-primary btn-sm");
          const cancel = button("取消", () => this.decide(item, "cancel", confirm, cancel)); card.append(confirm, cancel);
        } else if (item.stale) card.append(node("p", "", "来源或权限已变化，请重新生成预览。"));
        if (item.result && item.result.name) {
          card.append(node("p", "", (STATES[item.result.status] || item.result.status || "已记录") + " · " + item.result.name));
          if (safeSource(item.result.url)) {const link = node("a", "", "打开正式记录"); link.href = item.result.url; card.append(link);}
        }
        fragment.append(card);
      }
      this.ops.replaceChildren(fragment);
    }
    async decide(item, decision, ...buttons) {
      const name = this.current, epoch = this.epoch; buttons.forEach(el => el.disabled = true);
      try {
        const result = await this.api("decide_operation", {operation_id: item.name, decision});
        if (this.current !== name || this.epoch !== epoch) return;
        this.setStatus((STATES[result.status] || result.status) + (result.result && result.result.status === "待审批" ? "：预约待老师审批。" : ""));
        await this.loadConversation();
        if (!this.panel.hidden && this.panelKind) await this.showPrivate(this.panelKind);
      } catch (_) {if (this.current === name && this.epoch === epoch) this.error("未收到有效确认回执。请查询操作卡状态；旧预览失效时重新生成。");}
      finally {buttons.forEach(el => el.disabled = false);}
    }
    async send() {
      if (this.sending || this.running || this.pendingReceipt || !this.input.value.trim()) return;
      this.sending = true;
      const prompt = this.input.value;
      const reasoningEffort = this.reasoning.value;
      let conversation, epoch;
      this.sendButton.disabled = true; this.setStatus("正在提交本轮…", true);
      try {
        if (!this.current) {
          const created = await this.newConversation();
          if (!created || this.current !== created) return;
        }
        conversation = this.current; epoch = this.epoch;
        this.sendingConversation = conversation;
        this.setStatus("正在提交本轮…", true);
        const result = await this.api("send_message", {conversation, prompt, topic: this.topic.value || null,
          reasoning_effort: reasoningEffort});
        this.running = {...result, conversation, status: result.status || "queued", deadline: Date.now() + 170000};
        if (this.current === conversation && this.epoch === epoch && this.input.value === prompt) this.input.value = "";
        this.cancelButton.hidden = this.current !== conversation; this.recheck.hidden = true;
        if (this.current === conversation && this.epoch === epoch) await this.loadConversation();
        await this.loadHistory(); await this.checkRun();
      } catch (_) {
        if (this.current === conversation && this.epoch === epoch) this.error("未收到入队回执。请先刷新会话与本轮状态，避免重复发送。");
        if (conversation) {
          this.pendingReceipt = conversation; this.recheck.hidden = this.current !== conversation;
          try {await this.recoverSubmission(conversation);} catch (_) { /* Keep the status retry and draft. */ }
        }
      } finally {
        this.sending = false; this.sendingConversation = null;
        this.sendButton.disabled = !!this.running || !!this.pendingReceipt;
        if (!this.running && !this.pendingReceipt && this.status.dataset.busy === "true") {
          this.setStatus("会话仅本人及当前工作台可见。");
        }
      }
    }
    async checkRun() {
      if (this.hidden) return;
      if (!this.running) {
        if (this.pendingReceipt) {
          try {await this.recoverSubmission(this.pendingReceipt);} catch (_) {this.recheck.hidden = false; this.error("状态回执暂不可用，请点击查询本轮状态；不会自动重发。");}
        }
        return;
      }
      clearTimeout(this.timer);
      const run = this.running;
      try {
        const result = await this.api("get_run", {run_id: run.run_id});
        if (this.running !== run) return;
        run.status = result.status; run.reasoning_effort = result.reasoning_effort || run.reasoning_effort;
        if (this.current === run.conversation) this.setStatus(this.runStatus(run), ["queued", "running"].includes(result.status));
        if (["queued", "running"].includes(result.status)) {
          if (Date.now() < run.deadline) this.timer = setTimeout(() => this.checkRun(), 1800);
          else if (this.current === run.conversation) {
            this.recheck.hidden = false; this.error("等待已达上限，请查询本轮状态；不会自动重新发送。");
          }
          return;
        }
        this.running = null; this.sendButton.disabled = !!this.sending || !!this.pendingReceipt; this.cancelButton.hidden = true; this.recheck.hidden = true;
        if (this.current === run.conversation) await this.loadConversation();
        if (this.current === run.conversation) {
          if (result.status === "completed") this.setStatus("本轮已完成。");
          else this.error("本轮" + (STATES[result.status] || result.status) + "。未收到成功回执时不要认为操作已提交；可检查操作卡或重新发起。");
        } else if (this.status.firstChild?.textContent === "另一会话正在处理，请回到原会话查看状态。") {
          this.setStatus("会话仅本人及当前工作台可见。");
        }
      } catch (_) {if (this.running === run && this.current === run.conversation) {
        this.recheck.hidden = false; this.error("状态回执暂不可用，请点击查询本轮状态；不会自动重发。");
      }}
    }
    async cancelRun() {
      if (!this.running) return;
      const run = this.running, conversation = this.current, epoch = this.epoch;
      this.setStatus("正在取消本轮…", true);
      try {await this.api("cancel_run", {run_id: run.run_id}); await this.checkRun();}
      catch (_) {if (this.current === conversation && this.epoch === epoch && this.running === run) this.error();}
    }
    async ensureConversation() {if (!this.current) await this.newConversation(); return this.current;}
    async proposal(action, parameters) {
      if (!await this.ensureConversation()) return;
      const conversation = this.current, epoch = this.epoch;
      try {
        await this.api("propose_change", {conversation, action, parameters: JSON.stringify(parameters)});
        if (this.current === conversation && this.epoch === epoch) {await this.loadConversation(); this.ops.scrollIntoView({block: "nearest", behavior: "smooth"});}
      } catch (_) {this.error("无法生成预览。请核对内容、来源和当前权限。");}
    }
    editItem(kind, item = null) {
      const memory = kind === "memory";
      const conversation = this.current, epoch = this.epoch;
      const fields = [];
      if (memory) fields.push({fieldname: "kind", label: "记忆类型", fieldtype: "Select", reqd: 1,
        options: ["回答偏好", "本人背景", "待跟进事项"], default: ({preference: "回答偏好", background: "本人背景", follow_up: "待跟进事项"})[item && item.kind] || "回答偏好"});
      fields.push({fieldname: "topic", label: "关联课题（可留空）", fieldtype: "Link", options: "RG Topic", default: item ? item.topic : this.topic.value});
      fields.push({fieldname: "content", label: memory ? "记忆内容（最多1000字）" : "本人计划（最多12000字）", fieldtype: "Small Text", reqd: 1,
        default: item ? item.content : (memory ? "" : this.lastAnswer || "")});
      if (!memory) fields.push({fieldname: "status", label: "个人状态（不代表科研成功或正式进展）", fieldtype: "Select",
        options: ["进行中", "个人已完成"], default: item && item.status === "completed" ? "个人已完成" : "进行中"});
      const dialog = new frappe.ui.Dialog({title: (item ? "修改" : "新增") + (memory ? "本人记忆" : "本人计划"), fields,
        primary_action_label: "生成待确认预览", primary_action: values => {
          if (this.current !== conversation || this.epoch !== epoch) {dialog.hide(); this.error("会话已切换，请在当前会话重新打开编辑卡。"); return;}
          const params = {change: item ? "edit" : "add", topic: values.topic || "", content: values.content};
          if (item) params.name = item.name;
          if (memory) params.kind = ({"回答偏好": "preference", "本人背景": "background", "待跟进事项": "follow_up"})[values.kind];
          else params.status = values.status === "个人已完成" ? "completed" : "active";
          this.proposal(memory ? "memory_change" : "plan_change", params); dialog.hide();
        }}); dialog.show();
    }
    async showPrivate(kind, offset = 0) {
      const sequence = ++this.panelSequence; this.panelKind = kind; this.panel.hidden = false;
      this.panel.replaceChildren(node("p", "", "正在读取本人内容…"));
      try {
        const result = await this.api("private_items", {portal: this.portal, kind, offset});
        if (sequence !== this.panelSequence || this.panelKind !== kind) return;
        const fragment = document.createDocumentFragment();
        const heading = node("div", "rg-ai-panel-heading"); heading.append(node("h2", "", kind === "memory" ? "我的记忆" : "我的计划"),
          button("关闭", () => {this.panel.hidden = true; this.panelSequence++;})); fragment.append(heading);
        fragment.append(node("p", "text-muted", "仅本人可见；来源更新、过期或失权后停止注入模型。"));
        if (kind === "memory") {
          const label = node("label", "rg-ai-memory-switch", "使用跨会话记忆 "); const toggle = node("input"); toggle.type = "checkbox"; toggle.checked = this.boot.memory_enabled;
          toggle.addEventListener("change", async () => {
            toggle.disabled = true;
            try {const result = await this.api("set_memory_enabled", {portal: this.portal, enabled: JSON.stringify(toggle.checked)}); this.boot.memory_enabled = result.memory_enabled; await this.loadConversation();}
            catch (_) {toggle.checked = this.boot.memory_enabled; this.error();} finally {toggle.disabled = false;}
          }); label.append(toggle); fragment.append(label);
        }
        fragment.append(button(kind === "memory" ? "新增记忆" : "新增计划", () => this.editItem(kind)));
        if (!result.items.length) fragment.append(node("p", "rg-ai-empty", "这一页还没有私人条目。"));
        for (const item of result.items) {
          const card = node("article", "rg-ai-private-item"); card.append(node("h3", "", (VALUES[item.kind] || "本人计划") + " · " + (STATES[item.status] || item.status)),
            node("div", "rg-ai-message-text", item.content), node("p", "text-muted", "更新 " + item.updated_at + (item.expires_at ? " · 有效期 " + item.expires_at : " · 直到本人修改或删除")));
          if (item.stale) card.append(node("p", "rg-ai-notice-small", "依据已过期或失权；不用于回答。重新核对来源后再确认。"));
          sources(card, item.sources);
          if (item.source_valid) card.append(button(item.expired ? "核对并重新确认" : "修改", () => this.editItem(kind, item)));
          card.append(button("删除（先预览）", () => this.proposal(kind === "memory" ? "memory_change" : "plan_change", {change: "delete", name: item.name})));
          fragment.append(card);
        }
        const pager = node("div", "rg-ai-pager");
        if (offset) pager.append(button("上一页", () => this.showPrivate(kind, Math.max(0, offset - 20))));
        if (result.items.length === 20) pager.append(button("下一页", () => this.showPrivate(kind, offset + 20)));
        fragment.append(pager); this.panel.replaceChildren(fragment);
      } catch (_) {if (sequence === this.panelSequence) this.panel.replaceChildren(node("p", "", "无法读取本人条目，请检查账号和权限。"));}
    }
    businessProposal(action) {
      const progress = action === "progress";
      const conversation = this.current, epoch = this.epoch;
      const fields = [{fieldname: "topic", label: "本人课题", fieldtype: "Link", options: "RG Topic", reqd: 1, default: this.topic.value}];
      if (progress) fields.push({fieldname: "progress_date", label: "进展日期", fieldtype: "Date", reqd: 1, default: frappe.datetime.get_today()},
        {fieldname: "summary", label: "本次已发生的进展", fieldtype: "Small Text", reqd: 1},
        {fieldname: "blockers", label: "当前阻碍", fieldtype: "Small Text"}, {fieldname: "next_milestone", label: "下一步", fieldtype: "Small Text", reqd: 1});
      else fields.push({fieldname: "teacher", label: "指定教师", fieldtype: "Link", options: "User", reqd: 1,
        get_query: () => ({query: "research_group.services.member_search", filters: {role: "课题组教师"}})},
        {fieldname: "start_at", label: "开始时间（Asia/Shanghai）", fieldtype: "Datetime", reqd: 1},
        {fieldname: "end_at", label: "结束时间（Asia/Shanghai）", fieldtype: "Datetime", reqd: 1},
        {fieldname: "agenda", label: "讨论议题", fieldtype: "Small Text", reqd: 1});
      const dialog = new frappe.ui.Dialog({title: progress ? "追加正式进展" : "预约申请（提交后待教师审批）", fields,
        primary_action_label: "生成待确认预览", primary_action: values => {
          if (this.current !== conversation || this.epoch !== epoch) {dialog.hide(); this.error("会话已切换，请重新打开操作卡。"); return;}
          this.proposal(action, values); dialog.hide();
        }}); dialog.show();
    }
    async removeConversation() {
      if (!this.current) return;
      const name = this.current, epoch = this.epoch;
      try {
        const memory = await this.api("conversation_memory", {name});
        if (this.current !== name || this.epoch !== epoch) return;
        const fields = [{fieldname: "notice", fieldtype: "HTML", options: "<p>将删除本会话与消息，默认同时删除派生记忆。已确认提交的正式业务记录继续保留。勾选的记忆独立保留，并继续受全部来源权限约束。</p>"}];
        memory.forEach((item, index) => fields.push({fieldname: "keep_" + index, fieldtype: "Check", label: "独立保留 " + item.name + "（" + (VALUES[item.kind] || "记忆") + "）", default: 0}));
        const dialog = new frappe.ui.Dialog({title: "删除本人会话", fields, primary_action_label: "确认删除", primary_action: async values => {
          if (this.current !== name || this.epoch !== epoch) {dialog.hide(); return;}
          dialog.disable_primary_action();
          try {
            const keep = memory.filter((_, index) => values["keep_" + index]).map(item => item.name);
            await this.api("remove_conversation", {name, keep_memory: JSON.stringify(keep)}); dialog.hide();
            if (this.current === name && this.epoch === epoch) {
              this.current = null; this.epoch++; this.loadSequence++; this.lastAnswer = ""; this.ops.replaceChildren(); this.renderEmpty();
              this.deleteButton.disabled = true; this.setStatus("会话已删除。正式业务记录保留。");
            }
            if (this.running && this.running.conversation === name) {
              this.running = null; clearTimeout(this.timer); this.cancelButton.hidden = true; this.recheck.hidden = true;
            }
            if (this.pendingReceipt === name) {this.pendingReceipt = null; this.recheck.hidden = true;}
            this.sendButton.disabled = !!this.sending || !!this.running || !!this.pendingReceipt;
            await this.loadHistory();
          } catch (_) {if (this.current === name && this.epoch === epoch) this.error("删除未收到成功回执，请刷新历史确认。"); dialog.enable_primary_action();}
        }}); dialog.show();
      } catch (_) {this.error();}
    }
    pause() {this.hidden = true; clearTimeout(this.timer);}
    async resume() {this.hidden = false; if (await this.openPendingConversation()) return; if (this.running) this.checkRun();}
  }
  class LegacyAIAssistant {
    constructor(wrapper) {
      this.wrapper = wrapper; this.current = null; this.historyEpoch = 0; this.conversationEpoch = 0;
      this.panelEpoch = 0; this.offset = 0; this.before = null;
      this.api = async (method, args = {}) => {
        const result = await frappe.call({method: "research_group.ai.api." + method, args, type: "POST"});
        if (result.exc) throw new Error("请求未成功");
        return result.message;
      };
      this.build(); this.loadHistory();
    }
    build() {
      this.root = node("section", "rg-ai rg-ai-legacy");
      this.root.setAttribute("aria-label", "旧版 AI 历史，只读");
      this.wrapper.querySelector(".layout-main-section").append(this.root);
      this.root.append(node("div", "rg-ai-notice", "旧版混合会话仅供回看。这里不能发送消息、确认操作，也不会将旧内容加入新工作台的 AI 上下文。"));
      const layout = node("div", "rg-ai-layout"); this.root.append(layout);
      const history = node("details", "rg-ai-history"); history.open = window.innerWidth > 800;
      history.append(node("summary", "rg-ai-history-title", "旧版会话"));
      this.historyList = node("nav", "rg-ai-history-list"); this.historyList.setAttribute("aria-label", "旧版会话列表");
      history.append(this.historyList);
      this.moreHistory = button("更多旧版会话", () => this.loadHistory(true)); this.moreHistory.hidden = true;
      history.append(this.moreHistory); layout.append(history);
      const main = node("main", "rg-ai-main"); layout.append(main);
      const heading = node("div", "rg-ai-identity");
      heading.append(node("h2", "", "旧版混合历史")); main.append(heading);
      heading.append(button("旧版记忆", () => this.showLegacyItems("memory")),
        button("旧版计划", () => this.showLegacyItems("plan")));
      this.messages = node("div", "rg-ai-messages"); this.messages.setAttribute("aria-label", "旧版会话消息"); main.append(this.messages);
      this.operations = node("div", "rg-ai-operations"); this.operations.setAttribute("aria-label", "旧版操作记录"); main.append(this.operations);
      this.status = node("p", "rg-ai-status", "正在读取旧版历史…"); this.status.setAttribute("role", "status"); main.append(this.status);
      this.panel = node("aside", "rg-ai-panel"); this.panel.hidden = true; layout.append(this.panel);
    }
    async loadHistory(append = false) {
      const epoch = ++this.historyEpoch;
      try {
        const data = await this.api("list_legacy_conversations", {offset: append ? this.offset : 0});
        if (epoch !== this.historyEpoch) return;
        if (!append) this.historyList.replaceChildren();
        for (const item of data.items) {
          const entry = button(item.title || "旧版会话", () => this.select(item.name), "rg-ai-history-entry");
          entry.dataset.name = item.name; this.historyList.append(entry);
        }
        this.offset = data.offset + data.items.length; this.moreHistory.hidden = !data.has_more;
        if (!append && data.items.length) await this.select(data.items[0].name);
        else if (!append && !data.items.length) {
          this.messages.replaceChildren(node("div", "rg-ai-empty", "没有旧版混合会话。"));
          this.status.textContent = "没有旧版混合会话。";
        }
      } catch (_) {if (epoch === this.historyEpoch) this.status.textContent = "无法读取旧版历史，请核对当前成员权限。";}
    }
    async select(name) {
      this.current = name; this.before = null; this.conversationEpoch++;
      for (const el of this.historyList.children) el.setAttribute("aria-current", el.dataset.name === name ? "page" : "false");
      await this.loadConversation();
    }
    async loadConversation(older = false) {
      const name = this.current, before = older ? this.before : undefined, epoch = ++this.conversationEpoch;
      this.status.textContent = "正在读取旧版会话…";
      try {
        const data = await this.api("get_legacy_conversation", {name, before});
        if (epoch !== this.conversationEpoch || name !== this.current) return;
        const fragment = document.createDocumentFragment();
        if (data.has_older) fragment.append(button("读取更早消息", () => this.loadConversation(true), "btn btn-default btn-sm rg-ai-older"));
        this.before = data.before;
        for (const message of data.messages) {
          const label = {user: "你", assistant: "助手", tool: "工具记录"}[message.role] || "旧记录";
          const item = node("article", "rg-ai-message rg-ai-" + message.role);
          item.append(node("div", "rg-ai-message-label", label), node("div", "rg-ai-message-text", message.content));
          sources(item, message.sources); fragment.append(item);
        }
        if (older) {
          const previous = this.messages.querySelector(".rg-ai-older"); if (previous) previous.remove();
          this.messages.prepend(fragment);
        } else {
          this.messages.replaceChildren(fragment);
          if (data.read_only !== true || !Array.isArray(data.operations)) throw new Error("旧版回执无效");
          const archived = document.createDocumentFragment();
          for (const operation of data.operations) {
            if (operation.read_only !== true) throw new Error("旧版操作回执无效");
            const card = node("article", "rg-ai-operation");
            card.append(node("h3", "", ACTIONS[operation.action] || "旧版操作"),
              node("p", "rg-ai-operation-state", (STATES[operation.status] || operation.status) + " · 只读"));
            if (operation.stale) card.append(node("p", "", "来源已变化，旧预览内容已屏蔽。"));
            else if (operation.preview && Object.keys(operation.preview).length) {
              const fields = node("dl", "rg-ai-preview");
              if (operation.preview.original_text) fields.append(node("dt", "", "选定原文"),
                node("dd", "", operation.preview.original_text));
              for (const [key, value] of Object.entries(operation.preview.parameters || {})) {
                if (value != null && value !== "") fields.append(node("dt", "", LABELS[key] || key),
                  node("dd", "", VALUES[value] || value));
              }
              card.append(fields);
              if (operation.preview.notice) card.append(node("p", "rg-ai-notice-small", operation.preview.notice));
            }
            if (operation.result && Object.keys(operation.result).length) {
              card.append(node("p", "rg-ai-message-text", "历史结果：" + JSON.stringify(operation.result)));
            }
            sources(card, operation.sources); archived.append(card);
          }
          this.operations.replaceChildren(archived);
        }
        this.status.textContent = "只读历史；不包含可确认操作。";
      } catch (_) {if (epoch === this.conversationEpoch) this.status.textContent = "旧版会话不可用，请刷新后核对归属。";}
    }
    async showLegacyItems(kind, offset = 0) {
      const epoch = ++this.panelEpoch; this.panel.hidden = false;
      this.panel.replaceChildren(node("p", "", "正在读取旧版内容…"));
      try {
        const data = await this.api("list_legacy_items", {kind, offset});
        if (epoch !== this.panelEpoch) return;
        if (data.read_only !== true) throw new Error("旧版条目回执无效");
        const fragment = document.createDocumentFragment();
        const heading = node("div", "rg-ai-panel-heading");
        heading.append(node("h2", "", kind === "memory" ? "旧版记忆" : "旧版计划"),
          button("关闭", () => {this.panelEpoch++; this.panel.hidden = true;}));
        fragment.append(heading);
        if (!data.items.length) fragment.append(node("p", "rg-ai-empty", "没有旧版条目。"));
        for (const item of data.items) {
          const card = node("article", "rg-ai-private-item");
          card.append(node("h3", "", (VALUES[item.kind] || (kind === "memory" ? "记忆" : "计划")) + " · " + (STATES[item.status] || item.status)),
            node("div", "rg-ai-message-text", item.content));
          if (item.updated_at) card.append(node("p", "text-muted", "更新 " + item.updated_at));
          sources(card, item.sources); fragment.append(card);
        }
        const pager = node("div", "rg-ai-pager");
        if (offset) pager.append(button("上一页", () => this.showLegacyItems(kind, Math.max(0, offset - 20))));
        if (data.has_more) pager.append(button("下一页", () => this.showLegacyItems(kind, offset + 20)));
        fragment.append(pager); this.panel.replaceChildren(fragment);
      } catch (_) {if (epoch === this.panelEpoch) this.panel.replaceChildren(node("p", "", "无法读取旧版条目。"));}
    }
    pause() {}
    resume() {}
  }
  ns.AIAssistant = AIAssistant; ns.LegacyAIAssistant = LegacyAIAssistant; ns.aiSafeSource = safeSource;
})(window.research_group);
