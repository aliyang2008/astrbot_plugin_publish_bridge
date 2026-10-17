const bridge = window.AstrBotPluginView || window.AstrBotPluginPage;
const $ = (id) => document.getElementById(id);

let ctx = null;

function escapeHtml(value) {
  return String(value ?? "").replace(/[&<>"']/g, (ch) => {
    return {
      "&": "&amp;",
      "<": "&lt;",
      ">": "&gt;",
      '"': "&quot;",
      "'": "&#39;",
    }[ch];
  });
}

function applyI18n() {
  const title = bridge?.t("views.publish.title", "发布面板") || "发布面板";
  const description =
    bridge?.t(
      "views.publish.description",
      "查看平台、模板并快速发布内容。"
    ) || "查看平台、模板并快速发布内容。";
  $("page-title").textContent = title;
  $("page-desc").textContent = description;
  document.title = title;
}

function applyTheme() {
  document.documentElement.dataset.theme = ctx?.isDark ? "dark" : "light";
}

function renderStats(data) {
  const defaultPlatform = data.default_platform || "—";
  $("stats").innerHTML = `
    <div class="stat">
      <span>${data.platforms.length}</span>
      <label>平台</label>
    </div>
    <div class="stat">
      <span>${data.templates.length}</span>
      <label>模板</label>
    </div>
    <div class="stat">
      <span title="${escapeHtml(defaultPlatform)}">${escapeHtml(defaultPlatform)}</span>
      <label>默认平台</label>
    </div>
  `;
}

function renderTemplates(data) {
  const rows = data.templates
    .map(
      (template) => `
      <tr>
        <td><strong>${escapeHtml(template.name)}</strong></td>
        <td>${escapeHtml(template.platform)}</td>
        <td><code>${escapeHtml(template.action)}</code></td>
        <td>${escapeHtml(template.catid ?? "-")}</td>
        <td>${escapeHtml((template.required || []).join("、"))}</td>
        <td class="muted">${escapeHtml(template.description || "")}</td>
      </tr>`
    )
    .join("");

  $("templates").innerHTML = `
    <thead>
      <tr>
        <th>模板</th>
        <th>平台</th>
        <th>动作</th>
        <th>分类</th>
        <th>必填</th>
        <th>说明</th>
      </tr>
    </thead>
    <tbody>
      ${rows || `<tr><td colspan="6" class="muted">未配置模板</td></tr>`}
    </tbody>
  `;
}

function renderPlatforms(data) {
  const rows = data.platforms
    .map(
      (platform) => `
      <tr>
        <td><strong>${escapeHtml(platform.name)}</strong></td>
        <td class="mono">${escapeHtml(platform.url)}</td>
        <td>${escapeHtml(platform.method)}</td>
        <td>${escapeHtml(platform.auth_type)}</td>
      </tr>`
    )
    .join("");

  $("platforms").innerHTML = `
    <thead>
      <tr>
        <th>平台</th>
        <th>地址</th>
        <th>方法</th>
        <th>鉴权</th>
      </tr>
    </thead>
    <tbody>
      ${rows || `<tr><td colspan="4" class="muted">未配置平台</td></tr>`}
    </tbody>
  `;
}

function renderTargets(data) {
  const select = $("target");
  select.innerHTML = "";

  if (data.templates.length) {
    const group = document.createElement("optgroup");
    group.label = "模板";
    data.templates.forEach((template) => {
      const option = document.createElement("option");
      option.value = template.name;
      option.textContent = `${template.name} · ${template.action}`;
      group.appendChild(option);
    });
    select.appendChild(group);
  }

  if (data.platforms.length) {
    const group = document.createElement("optgroup");
    group.label = "平台";
    data.platforms.forEach((platform) => {
      const option = document.createElement("option");
      option.value = platform.name;
      option.textContent = platform.name;
      group.appendChild(option);
    });
    select.appendChild(group);
  }
}

function render(data) {
  renderStats(data);
  renderTemplates(data);
  renderPlatforms(data);
  renderTargets(data);
}

async function run(kind) {
  const result = $("result");
  const publishButton = $("publish-btn");
  const previewButton = $("preview-btn");

  const payload = {
    target: $("target").value,
    title: $("title").value.trim(),
    body: $("body").value.trim(),
    params: $("params").value.trim(),
  };

  result.hidden = false;
  result.textContent = "处理中…";
  publishButton.disabled = true;
  previewButton.disabled = true;

  try {
    const data = await bridge.apiPost(kind, payload);
    if (kind === "preview") {
      result.textContent = data.preview || JSON.stringify(data, null, 2);
    } else {
      result.textContent = data.result || JSON.stringify(data, null, 2);
    }
  } catch (error) {
    result.textContent = `操作失败：${error?.message || error}`;
  } finally {
    publishButton.disabled = false;
    previewButton.disabled = false;
  }
}

function bindEvents() {
  $("preview-btn").addEventListener("click", () => run("preview"));
  $("publish-form").addEventListener("submit", (event) => {
    event.preventDefault();
    run("publish");
  });
}

async function init() {
  if (!bridge) {
    document.body.innerHTML =
      '<p style="padding:24px">Bridge SDK 未注入，请重载插件后再试。</p>';
    return;
  }

  ctx = await bridge.ready();
  applyTheme();
  applyI18n();
  bindEvents();

  bridge.onContext((newContext) => {
    ctx = newContext;
    applyTheme();
    applyI18n();
  });

  try {
    render(await bridge.apiGet("overview"));
  } catch (error) {
    const result = $("result");
    result.hidden = false;
    result.textContent = `加载概览失败：${error?.message || error}`;
  }
}

init();
