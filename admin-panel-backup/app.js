/**
 * AIGC提示词站 · 后台管理面板（纯静态单页，无需构建）
 *
 * 设计说明：
 * - 这是运营方自己用的内部工具，不是产品主站前端（主站由其他人/Agent独立开发）。
 *   所以没有用 React/Vue 任何框架，直接原生 JS + fetch，双击打开 index.html 即可用，
 *   也可以丢到任意静态服务器（python -m http.server / nginx）里跑，零配置零依赖。
 * - 鉴权方式：对应后端 app/utils/admin_auth.py 的 X-Admin-Key 请求头校验，
 *   密钥只存在浏览器 sessionStorage 里（关闭标签页后失效，不做"记住我"），
 *   不是生产级的安全设计，但对"运营方自己在本机/内网用"这个场景够用。
 */

// ---------- 全局状态 ----------
let adminKey = sessionStorage.getItem('admin_key') || '';
let apiBase = sessionStorage.getItem('api_base') || 'http://localhost:8000';
let currentPromptPage = 1;
let currentReportPage = 1;
let editingPromptId = null; // null = 新建，非 null = 编辑中
let handlingReportId = null;

// 对应 app/routers/categories.py 的 CATEGORY_LABELS，手动同步一份在前端，
// 避免每次打开页面都额外发一次请求。如果后端分类有变动，这里也要跟着改。
const CATEGORY_LABELS = {
  head_avatar: '头像/壁纸',
  portrait: '人像写真',
  poster: '文案配图/海报',
  illustration: '插画/壁纸',
  logo: 'Logo/品牌设计',
  food: '美食/探店',
  scene: '场景/建筑',
};

// ---------- 初始化 ----------
window.addEventListener('DOMContentLoaded', () => {
  if (adminKey) {
    showApp();
  } else {
    showLogin();
  }
  const categorySelect = document.getElementById('f-category');
  categorySelect.innerHTML = Object.entries(CATEGORY_LABELS)
    .map(([value, label]) => `<option value="${value}">${label}</option>`)
    .join('');
});

function showLogin() {
  document.getElementById('login-view').style.display = 'block';
  document.getElementById('app-view').style.display = 'none';
}

function showApp() {
  document.getElementById('login-view').style.display = 'none';
  document.getElementById('app-view').style.display = 'block';
  document.getElementById('api-base-label').textContent = apiBase;
  loadPrompts(1);
}

// ---------- 登录 / 登出 ----------
// 这里的"登录"不是真正的身份认证请求，只是把密钥存起来，第一次调用管理接口
// 时如果密钥不对，后端会返回 401，到时候再提示重新输入即可（见 apiRequest 的错误处理）。
async function doLogin() {
  const key = document.getElementById('admin-key-input').value.trim();
  const base = document.getElementById('api-base-input').value.trim() || 'http://localhost:8000';
  if (!key) {
    document.getElementById('login-error').textContent = '请输入管理员密钥';
    return;
  }
  adminKey = key;
  apiBase = base.replace(/\/$/, '');
  sessionStorage.setItem('admin_key', adminKey);
  sessionStorage.setItem('api_base', apiBase);

  // 用一次真实请求验证密钥是否正确，而不是直接假设成功
  try {
    await apiRequest('/api/admin/prompts?page=1&page_size=1', { method: 'GET' });
    document.getElementById('login-error').textContent = '';
    showApp();
  } catch (e) {
    document.getElementById('login-error').textContent = '登录失败：' + e.message;
    sessionStorage.removeItem('admin_key');
    adminKey = '';
  }
}

function doLogout() {
  sessionStorage.removeItem('admin_key');
  adminKey = '';
  showLogin();
}

// ---------- 统一请求封装 ----------
async function apiRequest(path, options = {}) {
  const res = await fetch(apiBase + path, {
    ...options,
    headers: {
      'Content-Type': 'application/json',
      'X-Admin-Key': adminKey,
      ...(options.headers || {}),
    },
  });
  if (res.status === 401) {
    throw new Error('管理员密钥不正确或已失效，请重新登录');
  }
  if (!res.ok) {
    let detail = res.statusText;
    try {
      const body = await res.json();
      detail = body.detail || JSON.stringify(body);
    } catch (_) {
      /* 响应体不是 JSON，忽略 */
    }
    throw new Error(`${res.status} ${detail}`);
  }
  if (res.status === 204) return null;
  return res.json();
}

// ---------- Tab 切换 ----------
function switchTab(tab) {
  document.getElementById('nav-prompts').classList.toggle('active', tab === 'prompts');
  document.getElementById('nav-reports').classList.toggle('active', tab === 'reports');
  document.getElementById('tab-prompts').style.display = tab === 'prompts' ? 'block' : 'none';
  document.getElementById('tab-reports').style.display = tab === 'reports' ? 'block' : 'none';
  if (tab === 'reports') loadReports(1);
}

// ---------- 内容管理 ----------
async function loadPrompts(page) {
  currentPromptPage = page;
  const isPublished = document.getElementById('filter-published').value;
  const query = new URLSearchParams({ page, page_size: 20 });
  if (isPublished !== '') query.set('is_published', isPublished);

  const wrap = document.getElementById('prompts-table-wrap');
  wrap.innerHTML = '<div class="empty">加载中...</div>';
  try {
    const data = await apiRequest(`/api/admin/prompts?${query.toString()}`);
    renderPromptsTable(data);
  } catch (e) {
    wrap.innerHTML = `<div class="empty">加载失败：${escapeHtml(e.message)}</div>`;
  }
}

function renderPromptsTable(data) {
  const wrap = document.getElementById('prompts-table-wrap');
  if (data.items.length === 0) {
    wrap.innerHTML = '<div class="empty">暂无内容</div>';
    document.getElementById('prompts-pagination').innerHTML = '';
    return;
  }
  const rows = data.items
    .map((p) => {
      const statusTag = p.is_published
        ? '<span class="tag tag-published">已发布</span>'
        : '<span class="tag tag-draft">草稿</span>';
      return `<tr>
        <td title="${escapeHtml(p.title)}">${escapeHtml(p.title)}</td>
        <td>${CATEGORY_LABELS[p.category] || p.category}</td>
        <td>${(p.models || []).join(', ')}</td>
        <td>${escapeHtml(p.author_name || '-')}</td>
        <td>${statusTag}</td>
        <td>👍${p.like_count} ⭐${p.collect_count} 👁${p.view_count}</td>
        <td>${formatDate(p.created_at)}</td>
        <td class="row-actions">
          <button class="btn" onclick="openPromptModal('${p.id}')">编辑</button>
          ${
            p.is_published
              ? `<button class="btn" onclick="togglePublish('${p.id}', false)">下架</button>`
              : `<button class="btn btn-success" onclick="togglePublish('${p.id}', true)">发布</button>`
          }
          <button class="btn btn-danger" onclick="deletePrompt('${p.id}', '${escapeHtml(p.title)}')">删除</button>
        </td>
      </tr>`;
    })
    .join('');

  wrap.innerHTML = `<table>
    <thead><tr>
      <th>标题</th><th>分类</th><th>模型</th><th>作者</th><th>状态</th><th>数据</th><th>创建时间</th><th>操作</th>
    </tr></thead>
    <tbody>${rows}</tbody>
  </table>`;

  renderPagination('prompts-pagination', data.page, data.page_size, data.total, loadPrompts);
}

async function togglePublish(id, publish) {
  try {
    await apiRequest(`/api/admin/prompts/${id}/${publish ? 'publish' : 'unpublish'}`, { method: 'POST' });
    loadPrompts(currentPromptPage);
  } catch (e) {
    alert('操作失败：' + e.message);
  }
}

async function deletePrompt(id, title) {
  if (!confirm(`确定删除「${title}」？此操作不可恢复。`)) return;
  try {
    await apiRequest(`/api/admin/prompts/${id}`, { method: 'DELETE' });
    loadPrompts(currentPromptPage);
  } catch (e) {
    alert('删除失败：' + e.message);
  }
}

// ---------- 新建/编辑内容弹窗 ----------
async function openPromptModal(id) {
  editingPromptId = id || null;
  document.getElementById('prompt-modal-error').textContent = '';
  document.getElementById('prompt-modal-title').textContent = id ? '编辑内容' : '新建内容';

  if (id) {
    try {
      const p = await apiRequest(`/api/admin/prompts/${id}`);
      document.getElementById('f-title').value = p.title;
      document.getElementById('f-prompt-text').value = p.prompt_text;
      document.getElementById('f-description').value = p.description || '';
      document.getElementById('f-category').value = p.category;
      document.getElementById('f-models').value = (p.models || []).join(',');
      document.getElementById('f-author').value = p.author_name || '';
      document.getElementById('f-source-url').value = p.source_url || '';
      document.getElementById('f-images').value = ''; // 编辑态不预填图片，避免误以为"不改就是清空"
      document.getElementById('f-published').checked = p.is_published;
    } catch (e) {
      alert('加载内容失败：' + e.message);
      return;
    }
  } else {
    document.getElementById('f-title').value = '';
    document.getElementById('f-prompt-text').value = '';
    document.getElementById('f-description').value = '';
    document.getElementById('f-category').value = Object.keys(CATEGORY_LABELS)[0];
    document.getElementById('f-models').value = '';
    document.getElementById('f-author').value = '';
    document.getElementById('f-source-url').value = '';
    document.getElementById('f-images').value = '';
    document.getElementById('f-published').checked = false;
  }
  document.getElementById('prompt-modal').style.display = 'flex';
}

function closePromptModal() {
  document.getElementById('prompt-modal').style.display = 'none';
}

async function submitPrompt() {
  const title = document.getElementById('f-title').value.trim();
  const promptText = document.getElementById('f-prompt-text').value.trim();
  const category = document.getElementById('f-category').value;
  const errorBox = document.getElementById('prompt-modal-error');

  if (!title || !promptText || !category) {
    errorBox.textContent = '标题、提示词原文、分类为必填项';
    return;
  }

  const models = document.getElementById('f-models').value
    .split(',')
    .map((s) => s.trim())
    .filter(Boolean);

  const imageUrls = document.getElementById('f-images').value
    .split(',')
    .map((s) => s.trim())
    .filter(Boolean);

  const body = {
    title,
    prompt_text: promptText,
    description: document.getElementById('f-description').value.trim() || null,
    category,
    models,
    author_name: document.getElementById('f-author').value.trim() || null,
    source_url: document.getElementById('f-source-url').value.trim() || null,
    is_published: document.getElementById('f-published').checked,
  };

  // 编辑态且用户没有填新图片 URL 时，不传 images 字段，避免把已有图片清空；
  // 新建态或者用户确实填了新图片时才带上 images 字段。
  if (imageUrls.length > 0) {
    body.images = imageUrls.map((url, index) => ({ image_url: url, sort_order: index }));
  }

  try {
    if (editingPromptId) {
      await apiRequest(`/api/admin/prompts/${editingPromptId}`, {
        method: 'PATCH',
        body: JSON.stringify(body),
      });
    } else {
      await apiRequest('/api/admin/prompts', { method: 'POST', body: JSON.stringify(body) });
    }
    closePromptModal();
    loadPrompts(currentPromptPage);
  } catch (e) {
    errorBox.textContent = '保存失败：' + e.message;
  }
}

// ---------- 举报管理 ----------
async function loadReports(page) {
  currentReportPage = page;
  const status = document.getElementById('filter-report-status').value;
  const query = new URLSearchParams({ page, page_size: 20 });
  if (status) query.set('status', status);

  const wrap = document.getElementById('reports-table-wrap');
  wrap.innerHTML = '<div class="empty">加载中...</div>';
  try {
    const data = await apiRequest(`/api/admin/reports?${query.toString()}`);
    renderReportsTable(data);
  } catch (e) {
    wrap.innerHTML = `<div class="empty">加载失败：${escapeHtml(e.message)}</div>`;
  }
}

function renderReportsTable(data) {
  const wrap = document.getElementById('reports-table-wrap');
  if (data.items.length === 0) {
    wrap.innerHTML = '<div class="empty">暂无举报记录</div>';
    document.getElementById('reports-pagination').innerHTML = '';
    return;
  }
  const rows = data.items
    .map((r) => {
      const statusTag =
        r.status === 'pending'
          ? '<span class="tag tag-pending">待处理</span>'
          : '<span class="tag tag-resolved">已处理</span>';
      return `<tr>
        <td title="${escapeHtml(r.prompt_title || '')}">${escapeHtml(r.prompt_title || '(内容已删除)')}</td>
        <td title="${escapeHtml(r.reason)}">${escapeHtml(r.reason)}</td>
        <td>${escapeHtml(r.reporter_email || '匿名')}</td>
        <td>${statusTag}</td>
        <td title="${escapeHtml(r.handled_note || '')}">${escapeHtml(r.handled_note || '-')}</td>
        <td>${formatDate(r.created_at)}</td>
        <td class="row-actions">
          ${r.status === 'pending' ? `<button class="btn btn-primary" onclick="openReportModal('${r.id}')">处理</button>` : '-'}
        </td>
      </tr>`;
    })
    .join('');

  wrap.innerHTML = `<table>
    <thead><tr>
      <th>内容标题</th><th>举报原因</th><th>举报人</th><th>状态</th><th>处理备注</th><th>举报时间</th><th>操作</th>
    </tr></thead>
    <tbody>${rows}</tbody>
  </table>`;

  renderPagination('reports-pagination', data.page, data.page_size, data.total, loadReports);
}

function openReportModal(id) {
  handlingReportId = id;
  document.getElementById('r-note').value = '';
  document.getElementById('r-unpublish').checked = false;
  document.getElementById('report-modal-error').textContent = '';
  document.getElementById('report-modal').style.display = 'flex';
}

function closeReportModal() {
  document.getElementById('report-modal').style.display = 'none';
}

async function submitReportHandle() {
  const body = {
    handled_note: document.getElementById('r-note').value.trim() || null,
    unpublish_prompt: document.getElementById('r-unpublish').checked,
  };
  try {
    await apiRequest(`/api/admin/reports/${handlingReportId}/handle`, {
      method: 'POST',
      body: JSON.stringify(body),
    });
    closeReportModal();
    loadReports(currentReportPage);
  } catch (e) {
    document.getElementById('report-modal-error').textContent = '处理失败：' + e.message;
  }
}

// ---------- 小工具 ----------
function renderPagination(containerId, page, pageSize, total, loadFn) {
  const totalPages = Math.max(1, Math.ceil(total / pageSize));
  const container = document.getElementById(containerId);
  container.innerHTML = `
    <button class="btn" ${page <= 1 ? 'disabled' : ''} onclick="(${loadFn.name})(${page - 1})">上一页</button>
    <span>第 ${page} / ${totalPages} 页，共 ${total} 条</span>
    <button class="btn" ${page >= totalPages ? 'disabled' : ''} onclick="(${loadFn.name})(${page + 1})">下一页</button>
  `;
}

function formatDate(iso) {
  if (!iso) return '-';
  const d = new Date(iso);
  return d.toLocaleString('zh-CN', { hour12: false });
}

function escapeHtml(str) {
  const div = document.createElement('div');
  div.textContent = str ?? '';
  return div.innerHTML;
}
