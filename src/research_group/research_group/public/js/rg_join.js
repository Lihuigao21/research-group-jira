(() => {
  "use strict";
  const page = document.querySelector(".rg-join");
  if (!page) return;
  const form = document.getElementById("rg-join-form");
  const area = document.getElementById("rg-join-fields");
  const button = document.getElementById("rg-join-submit");
  const status = document.getElementById("rg-join-status");
  const params = new URLSearchParams(location.search);
  for (const [key, value] of new URLSearchParams(location.hash.slice(1))) {
    if (key === "token" || key === "key") params.set(key, value);
  }
  const mode = params.get("mode") || "apply";
  const fields = {};
  let nonce = "", sending = false;
  const text = (id, value) => {document.getElementById(id).textContent = value;};
  function input(name, label, type = "text", max = 140, autocomplete = "off") {
    const id = "rg-join-" + name;
    const caption = document.createElement("label"); caption.htmlFor = id; caption.textContent = label;
    const field = document.createElement("input");
    Object.assign(field, {id, name, type, required: true, maxLength: max, autocomplete, className: "form-control"});
    area.append(caption, field); fields[name] = field;
    return field;
  }
  function configure() {
    if (mode === "apply") {
      text("rg-join-title", "申请加入课题组");
      input("display_name", "本人姓名", "text", 80, "name");
      input("student_id", "学号", "text", 32);
      input("email", "本人复旦官方邮箱", "email", 140, "email");
      button.textContent = "发送邮箱验证请求";
    } else if (mode === "verify") {
      text("rg-join-title", "确认入组邮箱");
      text("rg-join-intro", "点击下方按钮后才会验证邮箱。完成后仍需秘书核实入组，当前没有业务权限。");
      if (!params.get("application") || !params.get("token")) throw new Error("验证链接不完整，请重新打开邮件中的链接。");
      button.textContent = "本人确认验证邮箱";
    } else if (mode === "contact") {
      text("rg-join-title", "确认新的联系邮箱");
      text("rg-join-intro", "验证完成后通知与找回密码使用新邮箱，原登录ID不变；旧邮箱会收到变更告知。");
      if (!params.get("token")) throw new Error("验证链接不完整。");
      if (page.dataset.signedIn !== "1") {
        const link = document.createElement("a");
        link.href = "/login";
        link.textContent = "先登录本人课题组账号"; area.append(link);
        throw new Error("请先登录本人账号，再重新打开此验证链接。");
      }
      button.textContent = "本人确认联系邮箱变更";
    } else if (mode === "password") {
      text("rg-join-title", "设置网站密码");
      text("rg-join-intro", "设置12至128字符的强密码；勿使用校园邮箱密码。提交成功后请重新登录。");
      if (!params.get("key")) throw new Error("设密链接不完整，请重新申请找回网站密码。");
      input("password", "新网站密码", "password", 128, "new-password").minLength = 12;
      input("confirm", "再次输入新网站密码", "password", 128, "new-password").minLength = 12;
      button.textContent = "保存网站密码";
    } else if (mode === "reset") {
      text("rg-join-title", "找回网站密码");
      text("rg-join-intro", "输入初始登录邮箱或当前已验证联系邮箱。符合条件时，设密链接会发到已验证联系邮箱。");
      input("user", "登录邮箱或已验证联系邮箱", "email", 140, "email");
      button.textContent = "申请重设网站密码";
    } else throw new Error("此账号操作无效，请使用下方入口。");
  }
  async function api(method, values = null) {
    const headers = {"Accept": "application/json"};
    if (values) {
      headers["Content-Type"] = "application/json";
      headers["X-RG-CSRF-Token"] = nonce;
      if (window.frappe?.csrf_token) headers["X-Frappe-CSRF-Token"] = window.frappe.csrf_token;
    }
    const response = await fetch("/api/method/research_group.accounts." + method, {
      method: values ? "POST" : "GET", credentials: "same-origin", cache: "no-store", headers,
      ...(values ? {body: JSON.stringify(values)} : {})
    });
    const result = await response.json().catch(() => ({}));
    if (!response.ok || result.exc) {
      let message = "操作未完成，请检查输入或重新打开邮件链接。";
      try {
        const messages = JSON.parse(result._server_messages || "[]");
        const first = JSON.parse(messages[0] || "{}");
        if (first.message) message = String(first.message).replace(/<[^>]*>/g, "");
      } catch (_) { /* Display a fixed fallback without exposing traceback. */ }
      throw new Error(message);
    }
    return result.message;
  }
  form.addEventListener("submit", async event => {
    event.preventDefault();
    if (sending || !nonce || !form.reportValidity()) return;
    sending = true; button.disabled = true; status.dataset.error = "0"; status.textContent = "正在提交…";
    try {
      let method, values;
      if (mode === "apply") {
        method = "request_registration";
        values = Object.fromEntries(["display_name", "student_id", "email"].map(key => [key, fields[key].value]));
      } else if (mode === "verify") {
        method = "verify_registration"; values = {application: params.get("application"), token: params.get("token")};
      } else if (mode === "contact") {
        method = "verify_contact_change"; values = {token: params.get("token")};
      } else if (mode === "password") {
        if (fields.password.value !== fields.confirm.value) throw new Error("两次输入的新密码不一致。");
        method = "update_password"; values = {new_password: fields.password.value, key: params.get("key")};
      } else {method = "request_password_reset"; values = {user: fields.user.value};}
      const result = await api(method, values);
      status.textContent = typeof result === "object" && result?.message ? result.message :
        (mode === "password" ? "网站密码已保存。请返回登录页面重新登录。" : "操作已完成。");
      if (["verify", "contact", "password"].includes(mode)) {
        button.hidden = true; form.querySelectorAll("input").forEach(field => {field.value = ""; field.disabled = true;});
        history.replaceState(null, "", "/rg_join?mode=" + mode);
      }
    } catch (error) {status.dataset.error = "1"; status.textContent = error.message;}
    finally {sending = false; button.disabled = false;}
  });
  (async () => {
    try {configure(); nonce = (await api("csrf_token"))?.token || ""; if (!nonce) throw new Error("安全表单初始化失败，请刷新后重试。"); button.disabled = false;}
    catch (error) {status.dataset.error = "1"; status.textContent = error.message; button.hidden = true;}
  })();
})();
