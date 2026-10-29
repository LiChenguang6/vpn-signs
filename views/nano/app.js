const $ = (id) => document.getElementById(id);
let config = null,
  dirty = false,
  busy = false;
const lines = (s) =>
  s
    .split("\n")
    .map((x) => x.trim())
    .filter(Boolean);
function message(s) {
  $("message").textContent = s;
}
const bridge = window.AstrBotPluginView;
async function api(path, method = "GET", data) {
  const endpoint = path.replace(/^\/api\//, "");
  return method === "GET"
    ? bridge.apiGet(endpoint)
    : bridge.apiPost(endpoint, data || {});
}
function ask(text) {
  return new Promise((resolve) => {
    const dialog = $("askDialog");
    $("askText").textContent = text;
    const finish = (value) => {
      dialog.close();
      resolve(value);
    };
    $("confirmAsk").onclick = () => finish(true);
    $("cancelAsk").onclick = () => finish(false);
    dialog.oncancel = (event) => {
      event.preventDefault();
      finish(false);
    };
    dialog.showModal();
  });
}
function collect() {
  return {
    revision: config.revision,
    enabled: $("auto").checked,
    api_base: $("base").value.trim(),
    daily_time: $("time").value,
    timezone: $("timezone").value.trim(),
    targets: lines($("targets").value),
    accounts: [...document.querySelectorAll(".account")].map((row) => ({
      name: row.querySelector(".name").value,
      email: row.querySelector(".email").value,
      password: row.querySelector(".password").value,
      api_base: row.querySelector(".accountBase").value,
      enabled: row.querySelector(".active").checked,
      bearer_prefix: row.querySelector(".bearer").checked,
      login_format: row.querySelector(".loginFormat").value,
      targets: lines(row.querySelector(".destinations").value),
      key: row.dataset.key,
      password_set: row.dataset.hasPassword === "true",
    })),
  };
}
function renderAccounts(accounts) {
  $("accounts").replaceChildren();
  for (let a of accounts) {
    let row = document.createElement("div");
    row.className = "account";
    row.dataset.key = a.key || "";
    row.dataset.hasPassword = String(!!a.password_set);
    row.innerHTML =
      '<div class="toolbar"><label><input class="active" type="checkbox"> 启用此账号</label><div><button class="loginTest secondary">测试登录</button> <button class="run secondary">立即签到</button> <button class="remove danger">删除</button></div></div><div class="grid"><div><label>账号备注</label><input class="name" placeholder="例如：主账号"></div><div><label>登录邮箱 / 账号</label><input class="email" autocomplete="off"></div><div><label>登录密码</label><input class="password" type="password" autocomplete="new-password"></div><div><label>登录提交格式</label><select class="loginFormat"><option value="json">JSON（推荐，Dio 默认）</option><option value="form">表单（旧版兼容）</option></select></div></div><details><summary>账号专属 API 和推送设置</summary><label>API 根地址（留空使用默认地址）</label><input class="accountBase" type="url"><label><input class="bearer" type="checkbox"> 登录返回的 Authorization 需要添加 Bearer 前缀</label><label>专属推送会话（每行一个；留空使用全局目标）</label><textarea class="destinations"></textarea></details>';
    row.querySelector(".name").value = a.name || "";
    row.querySelector(".email").value = a.email || "";
    row.querySelector(".password").value = a.password || "";
    row.querySelector(".password").placeholder = a.password_set
      ? "已设置密码；留空保留"
      : "请输入 Nano 登录密码";
    row.querySelector(".accountBase").value = a.api_base || "";
    row.querySelector(".active").checked = a.enabled !== false;
    row.querySelector(".bearer").checked = !!a.bearer_prefix;
    row.querySelector(".loginFormat").value = a.login_format || "json";
    row.querySelector(".destinations").value = (a.targets || []).join("\n");
    for (let x of row.querySelectorAll("input,textarea,select"))
      x.oninput = () => {
        dirty = true;
      };
    row.querySelector(".remove").onclick = async () => {
      if (await ask("从配置中删除此账号？保存后生效。")) {
        row.remove();
        dirty = true;
      }
    };
    row.querySelector(".loginTest").disabled = !a.key;
    row.querySelector(".run").disabled = !a.key;
    row.querySelector(".loginTest").onclick = () =>
      action("/api/login", { key: a.key });
    row.querySelector(".run").onclick = async () => {
      if (await ask("现在登录并签到此账号？"))
        action("/api/run", { key: a.key });
    };
    $("accounts").append(row);
  }
}
function populate(c) {
  config = c;
  for (let [id, key] of [
    ["base", "api_base"],
    ["time", "daily_time"],
    ["timezone", "timezone"],
  ])
    $(id).value = c[key];
  $("auto").checked = c.enabled;
  $("targets").value = c.targets.join("\n");
  renderAccounts(c.accounts);
  dirty = false;
  $("configError").textContent = c.config_error
    ? "配置有误，任务已暂停：" +
      c.config_error +
      "。请在下方修正并保存，无需删除或重载插件；修复保存前会在本地备份原配置。"
    : "";
  $("configError").classList.toggle("hidden", !c.config_error);
}
async function refreshStatus() {
  let s = await api("/api/status");
  $("enabledStatus").textContent = s.config_error
    ? "配置错误，已暂停"
    : s.enabled
      ? "已开启"
      : "已关闭";
  $("schedule").textContent = s.daily_time;
  $("zone").textContent = s.timezone;
  $("done").textContent = s.completed + " / " + s.total;
  $("pending").textContent = s.pending_count;
  function table(id, rows) {
    $(id).replaceChildren();
    for (let values of rows) {
      let tr = document.createElement("tr");
      for (let v of values) {
        let td = document.createElement("td");
        td.textContent = String(v ?? "");
        tr.append(td);
      }
      $(id).append(tr);
    }
  }
  table(
    "accountStatus",
    s.accounts.map((a) => [
      a.name,
      !a.enabled ? "已禁用" : a.done ? "任务完成" : "未完成",
      a.attempts,
      a.message,
    ]),
  );
  table(
    "history",
    [...s.history]
      .reverse()
      .map((h) => [
        h.time,
        h.name || "旧版记录",
        h.outcome === "completed" ? "完成" : "失败",
        h.message,
      ]),
  );
}
async function refresh() {
  try {
    if (dirty && !(await ask("刷新将放弃未保存修改，继续？"))) return;
    populate(await api("/api/config"));
    await refreshStatus();
    $("manager").classList.remove("hidden");
  } catch (e) {
    message(e.message);
  }
}
async function action(path, data = {}) {
  if (busy) return;
  if (dirty) {
    message("请先保存配置，再执行操作");
    return;
  }
  busy = true;
  message("正在执行，请稍候…");
  try {
    message((await api(path, "POST", data)).message);
    await refreshStatus();
  } catch (e) {
    message(e.message);
  } finally {
    busy = false;
  }
}
$("refresh").onclick = refresh;
for (let id of ["auto", "base", "time", "timezone", "targets"])
  $(id).oninput = () => {
    dirty = true;
  };
$("add").onclick = () => {
  let accounts = collect().accounts;
  accounts.push({
    name: "",
    email: "",
    enabled: true,
    password: "",
    targets: [],
  });
  renderAccounts(accounts);
  dirty = true;
};
$("save").onclick = async () => {
  if (busy) return;
  busy = true;
  message("正在保存配置…");
  try {
    populate(await api("config/save", "POST", collect()));
    await refreshStatus();
    message("已保存；账号、计划和推送配置立即生效");
  } catch (e) {
    message(e.message);
  } finally {
    busy = false;
  }
};
$("runAll").onclick = async () => {
  if (await ask("现在登录并签到全部启用账号？")) action("/api/run");
};
$("pushTest").onclick = async () => {
  if (await ask("向已保存的推送群聊发送测试消息？")) action("/api/push-test");
};
try {
  if (!bridge)
    throw new Error("请在 AstrBot 插件详情中的‘视图’打开 Nano 管理界面");
  await bridge.ready();
  await refresh();
} catch (error) {
  message(error.message);
}
