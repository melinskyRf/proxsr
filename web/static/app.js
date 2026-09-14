/* === ProxsR Dashboard - JavaScript Moderno com Animações === */

const API_BASE = '';
let refreshInterval = null;
let currentData = { proxies: [], rotator: {}, stats: {}, config: {} };
let startTime = Date.now();
let fetching = false;
let logFilter = 'all';
let testResults = {};

// === Inicialização ===

document.addEventListener('DOMContentLoaded', () => {
    updateClock();
    setInterval(updateClock, 1000);
    fetchAll();
    refreshInterval = setInterval(fetchAll, 1500);
    setupConfigTabs();
    setupModal();
    loadConfig();
});

function updateClock() {
    const now = new Date();
    const uptime = Date.now() - startTime;
    const hours = Math.floor(uptime / 3600000).toString().padStart(2, '0');
    const minutes = Math.floor((uptime % 3600000) / 60000).toString().padStart(2, '0');
    const seconds = Math.floor((uptime % 60000) / 1000).toString().padStart(2, '0');
    
    document.getElementById('current-time').textContent =
        now.toLocaleTimeString('pt-BR') + ' ' + now.toLocaleDateString('pt-BR');
    document.getElementById('stat-uptime').textContent = `${hours}:${minutes}:${seconds}`;
}

// === API Fetch ===

async function fetchJSON(url) {
    try {
        const resp = await fetch(API_BASE + url, { signal: AbortSignal.timeout(10000) });
        if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
        return await resp.json();
    } catch (e) {
        console.error(`Erro ao buscar ${url}:`, e);
        return null;
    }
}

async function postJSON(url, data) {
    try {
        const resp = await fetch(API_BASE + url, {
            method: 'POST',
            signal: AbortSignal.timeout(120000),
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(data),
        });
        return await resp.json();
    } catch (e) {
        console.error(`Erro ao postar ${url}:`, e);
        return null;
    }
}

async function fetchAll() {
    if (fetching) return;
    fetching = true;
    try {
    const [status, proxies, rotator, activity, health] = await Promise.all([
        fetchJSON('/api/status'),
        fetchJSON('/api/proxies'),
        fetchJSON('/api/rotator'),
        fetchJSON('/api/activity'),
        fetchJSON('/api/health'),
    ]);

    if (status) {
        startTime = Date.now() - status.uptime * 1000;
        updateStats(status);
        document.getElementById('connection-notice').hidden = true;
    } else {
        document.getElementById('connection-notice').hidden = false;
        document.getElementById('status-text').textContent = 'Sem conexão';
        document.getElementById('system-status').className = 'system-status offline';
    }
    if (proxies) { currentData.proxies = proxies.proxies || []; renderProxies(); renderProxyConfig(); }
    if (rotator) { currentData.rotator = rotator; renderRotation(); }
    if (activity) renderActivity(activity.entries || []);
    if (health) updateHealthStatus(health);
    } finally { fetching = false; }
}

// === Stats ===

function updateStats(status) {
    const pool = status.pool || {};
    animateValue('stat-total', pool.total_proxies || 0);
    animateValue('stat-alive', (pool.rotating_alive || 0) + (pool.static_alive || 0));
    animateValue('stat-rotating', pool.rotating_alive || 0);
    animateValue('stat-requests', pool.total_requests || 0);

    const rot = status.rotator || {};
    const current = rot.current_rotations || 0;
    const max = rot.max_rotations || 5;
    document.getElementById('cycle-value').textContent = `${current}/${max}`;
    const cyclePercent = max > 0 ? (current / max * 100) : 0;
    document.getElementById('cycle-fill').style.width = `${cyclePercent}%`;
    
    // Status do sistema
    const sysStatus = document.getElementById('system-status');
    const statusText = document.getElementById('status-text');
    if ((pool.rotating_alive || 0) + (pool.static_alive || 0) > 0) {
        sysStatus.classList.add('online');
        sysStatus.classList.remove('offline');
        statusText.textContent = 'Painel conectado';
    } else {
        sysStatus.classList.add('offline');
        sysStatus.classList.remove('online');
        statusText.textContent = 'Sem proxies disponíveis';
    }
}

function animateValue(id, newValue) {
    const el = document.getElementById(id);
    if (!el) return;
    const oldValue = parseInt(el.textContent) || 0;
    if (oldValue !== newValue) {
        el.textContent = newValue;
        el.style.animation = 'none';
        el.offsetHeight; // trigger reflow
        el.style.animation = 'fadeIn 0.3s ease';
    }
}

// === Rotação Visual ===

function renderStable(container, markup) {
    if (container._markup === markup) return;
    const scrollLeft = container.scrollLeft;
    const focused = container.contains(document.activeElement) ? document.activeElement.closest('[data-action]') : null;
    const identity = focused ? {...focused.dataset} : null;
    container.innerHTML = markup;
    container._markup = markup;
    container.scrollLeft = scrollLeft;
    if (identity) {
        [...container.querySelectorAll('[data-action]')].find(el => el.dataset.action === identity.action && el.dataset.id === identity.id)?.focus({preventScroll:true});
    }
}

function proxyName(proxy) { return proxy.name || proxy.id; }
function proxyState(proxy) {
    return !proxy.enabled ? 'Desativada' : !proxy.is_alive ? 'Indisponível' : !proxy.last_checked ? 'Não verificada' : 'Verificada';
}
function rotationOrder() {
    const proxies = currentData.proxies.filter(p => p.type === 'rotating');
    const ids = new Set(proxies.map(p => p.id));
    return [...new Set([...(currentData.rotator.cycle_order || []).filter(id => ids.has(id)), ...ids])];
}
function renderRotation() {
    const container = document.getElementById('rotation-visual');
    const rot = currentData.rotator;
    const proxies = currentData.proxies.filter(p => p.type === 'rotating');
    if (!proxies.length) {
        container.innerHTML = '<div class="rotation-empty">Seu mapa começa com a primeira proxy.<br>Adicione uma conexão para acompanhar a rotação aqui.</div>';
        updateRotationInfo(rot, null);
        return;
    }
    const order = rotationOrder();
    const markup = order.map((id, index) => {
        const p = proxies.find(item => item.id === id);
        const available = p.enabled && p.is_alive;
        const selected = available && id === rot.last_proxy_id;
        const state = !available ? proxyState(p) : selected ? 'Selecionada' : proxyState(p);
        const progress = rot.strategy === 'cycle' ? Math.min(100, Math.max(0, p.current_rotations / p.rotation_count * 100)) : 0;
        const marker = String(currentData.proxies.findIndex(item => item.id === id) + 1).padStart(2, '0');
        return `<div class="connection-node ${selected ? 'is-selected' : ''} ${!available ? 'is-unavailable' : ''}">
            <span class="node-sequence">CONEXÃO ${String(index + 1).padStart(2, '0')}</span>
            <button class="proxy-orbit" data-action="edit" data-id="${escapeHtml(id)}" aria-label="Editar ${escapeHtml(proxyName(p))}" title="${escapeHtml(proxyName(p))} · ${escapeHtml(p.host)}:${p.port}">
                <svg class="orbit-ring" viewBox="0 0 120 120" aria-hidden="true"><circle class="orbit-track" cx="60" cy="60" r="53"/><circle class="orbit-progress" cx="60" cy="60" r="53" pathLength="100" stroke-dasharray="${progress} 100"/></svg>
                <span class="orbit-core"><span class="orbit-number">P${marker}</span><span class="orbit-protocol">${escapeHtml(p.protocol.toUpperCase())}</span></span>
            </button>
            <strong class="node-name">${escapeHtml(proxyName(p))}</strong><span class="node-id">${escapeHtml(p.id)}</span>
            <span class="node-address">${escapeHtml(p.host)}:${p.port}</span>
            <span class="node-status">${escapeHtml(state)}</span>
            <span class="node-usage">${rot.strategy === 'cycle' ? `${p.current_rotations} / ${p.rotation_count} requisições` : `${p.total_requests} requisições registradas`}</span>
            <span class="node-ip">IP ${escapeHtml(p.current_ip || 'aguardando verificação')}</span>
        </div>`;
    }).join('');
    renderStable(container, markup);
    updateRotationInfo(rot, proxies.find(p => p.id === rot.last_proxy_id && p.enabled && p.is_alive));
}
function updateRotationInfo(rot, activeProxy) {
    const strategies = {cycle:'Ciclo', round_robin:'Sequencial', random:'Aleatória', least_used:'Menos usada'};
    document.getElementById('rot-strategy').textContent = strategies[rot.strategy] || '—';
    document.getElementById('rot-current').textContent = activeProxy ? proxyName(activeProxy) : 'Nenhuma selecionada';
    document.getElementById('rot-index').textContent = activeProxy ? rotationOrder().indexOf(activeProxy.id) + 1 : '—';
    const eligible = rotationOrder().map(id => currentData.proxies.find(p => p.id === id)).filter(p => p.enabled && p.is_alive);
    let next = 'Nenhuma disponível';
    if (eligible.length) {
        if (['random', 'least_used'].includes(rot.strategy)) next = 'Definida na seleção';
        else next = proxyName(eligible[(eligible.findIndex(p => p.id === activeProxy?.id) + 1) % eligible.length]);
    }
    document.getElementById('rot-next').textContent = next;
}

// === Proxies Lists ===

function renderProxies() {
    const rotatingList = document.getElementById('rotating-proxies-list');
    const staticList = document.getElementById('static-proxies-list');

    const query = (document.getElementById('proxy-search')?.value || '').trim().toLocaleLowerCase();
    const matches = p => [p.name, p.id, p.host, p.current_ip, p.protocol].some(value => String(value || '').toLocaleLowerCase().includes(query));
    const rotating = currentData.proxies.filter(p => p.type === 'rotating' && matches(p));
    const statics = currentData.proxies.filter(p => p.type === 'static' && matches(p));

    renderStable(rotatingList, rotating.length === 0 
        ? '<div class="proxy-empty">Nenhuma proxy rotativa encontrada</div>'
        : rotating.map(p => proxyItemHTML(p, true)).join(''));

    renderStable(staticList, statics.length === 0
        ? '<div class="proxy-empty">Nenhuma proxy fixa encontrada</div>'
        : statics.map(p => proxyItemHTML(p, false)).join(''));
}

function proxyItemHTML(p, showProgress) {
    const dotClass = !p.enabled ? ' disabled' : (p.is_alive ? '' : ' dead');
    const progress = p.rotation_count > 0
        ? Math.round((p.current_rotations / p.rotation_count) * 100)
        : 0;

    let progressClass = 'proxy-progress-fill';
    if (progress >= 80) progressClass += ' danger';
    else if (progress >= 50) progressClass += ' warn';
    if (progress >= 100) progressClass += ' complete';

    const protocolClass = p.protocol;
    const protocolLabel = p.protocol.toUpperCase();

    let progressHTML = '';
    if (showProgress) {
        progressHTML = `
            <div class="proxy-progress-container">
                <div class="proxy-progress-bar">
                    <div class="${progressClass}" style="width: ${progress}%"></div>
                </div>
                <span class="proxy-progress-text">${p.current_rotations}/${p.rotation_count}</span>
            </div>
        `;
    }

    // Verifica se tem resultado de teste
    const testResult = testResults[p.id];
    let testHTML = '';
    if (testResult) {
        const testClass = testResult.ok ? 'test-ok' : 'test-fail';
        const testIcon = testResult.ok ? '&#10003;' : '&#10007;';
        const testIP = testResult.ip ? `IP: ${escapeHtml(testResult.ip)}` : '';
        const testTime = testResult.response_time ? `${testResult.response_time}s` : '';
        testHTML = `<span class="proxy-test-badge ${testClass}" title="${testResult.error || 'OK'}">${testIcon} ${testTime}${testIP ? ' · ' + testIP : ''}</span>`;
    }

    return `
        <div class="proxy-item" data-id="${escapeHtml(p.id)}">
            <div class="proxy-item-left">
                <div class="proxy-status-dot${dotClass}"></div>
                <div>
                    <div class="proxy-id">${escapeHtml(proxyName(p))} <span class="proxy-state">${!p.enabled ? 'Desativada' : !p.is_alive ? 'Indisponível' : !p.last_checked ? 'Não verificada' : 'Verificada'}</span>${testHTML}</div>
                    <div class="proxy-info">
                        <span class="proxy-id-tag">${escapeHtml(p.id)}</span>
                        <span class="proxy-protocol ${escapeHtml(protocolClass)}">${escapeHtml(protocolLabel)}</span>
                        <span>${escapeHtml(p.host)}:${p.port}</span>
                        ${p.current_ip ? `<span class="log-ip">${escapeHtml(p.current_ip)}</span>` : ''}
                    </div>
                </div>
            </div>
            <div class="proxy-item-right">
                <div class="proxy-stats">
                    <div class="proxy-stat">
                        <span class="proxy-stat-value">${p.total_requests}</span>
                        <span class="proxy-stat-label">Reqs</span>
                    </div>
                    <div class="proxy-stat">
                        <span class="proxy-stat-value" style="color: ${p.failures > 0 ? 'var(--accent-red)' : 'inherit'}">${p.failures}</span>
                        <span class="proxy-stat-label">Erros</span>
                    </div>
                </div>
                ${progressHTML}
                <div class="proxy-actions">
                    <button class="btn btn-sm btn-blue" data-action="test" data-id="${escapeHtml(p.id)}" title="Testar esta proxy">
                        <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
                            <polygon points="5 3 19 12 5 21 5 3"></polygon>
                        </svg>
                        Testar
                    </button>
                    <button class="btn btn-sm ${p.enabled ? 'btn-red' : 'btn-green'}" 
                            data-action="toggle" data-id="${escapeHtml(p.id)}" data-enable="${!p.enabled}"
                            title="${p.enabled ? 'Desativar' : 'Ativar'}">
                        ${p.enabled ? 'Desativar' : 'Ativar'}
                    </button>
                    <button class="btn btn-sm btn-icon" data-action="edit" data-id="${escapeHtml(p.id)}" title="Editar">
                        <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
                            <path d="M11 4H4a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h14a2 2 0 0 0 2-2v-7"></path>
                            <path d="M18.5 2.5a2.121 2.121 0 0 1 3 3L12 15l-4 1 1-4 9.5-9.5z"></path>
                        </svg>
                    </button>
                    <button class="btn btn-sm btn-icon" data-action="delete" data-id="${escapeHtml(p.id)}" title="Excluir">
                        <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
                            <polyline points="3 6 5 6 21 6"></polyline>
                            <path d="M19 6v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6m3 0V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2"></path>
                        </svg>
                    </button>
                </div>
            </div>
        </div>
    `;
}

// === Proxy Config Panel ===

function renderProxyConfig() {
    const rotating = currentData.proxies.filter(p => p.type === 'rotating');
    const statics = currentData.proxies.filter(p => p.type === 'static');

    document.getElementById('rotating-proxies-config').innerHTML = rotating.length === 0
        ? '<div class="proxy-empty">Nenhuma proxy rotativa</div>'
        : rotating.map(p => proxyConfigItemHTML(p)).join('');

    document.getElementById('static-proxies-config').innerHTML = statics.length === 0
        ? '<div class="proxy-empty">Nenhuma proxy normal</div>'
        : statics.map(p => proxyConfigItemHTML(p)).join('');
}

function proxyConfigItemHTML(p) {
    const statusClass = !p.enabled ? ' disabled' : (p.is_alive ? '' : ' dead');
    return `
        <div class="proxy-item" data-id="${escapeHtml(p.id)}">
            <div class="proxy-item-left">
                <div class="proxy-status-dot${statusClass}"></div>
                <div>
                    <div class="proxy-id">${escapeHtml(proxyName(p))} <span class="proxy-state">${!p.enabled ? 'Desativada' : !p.is_alive ? 'Indisponível' : !p.last_checked ? 'Não verificada' : 'Verificada'}</span></div>
                    <div class="proxy-info">
                        <span class="proxy-id-tag">${escapeHtml(p.id)}</span>
                        <span class="proxy-protocol ${escapeHtml(p.protocol)}">${escapeHtml(p.protocol.toUpperCase())}</span>
                        <span>${escapeHtml(p.host)}:${p.port}</span>
                    </div>
                </div>
            </div>
            <div class="proxy-item-right">
                <div class="proxy-stats">
                    <div class="proxy-stat">
                        <span class="proxy-stat-value">${p.current_rotations}/${p.rotation_count}</span>
                        <span class="proxy-stat-label">Rotações</span>
                    </div>
                    <div class="proxy-stat">
                        <span class="proxy-stat-value">${p.total_requests}</span>
                        <span class="proxy-stat-label">Reqs</span>
                    </div>
                </div>
                <div class="proxy-actions">
                    <button class="btn btn-sm ${p.enabled ? 'btn-red' : 'btn-green'}" 
                            data-action="toggle" data-id="${escapeHtml(p.id)}" data-enable="${!p.enabled}">
                        ${p.enabled ? 'Desativar' : 'Ativar'}
                    </button>
                    <button class="btn btn-sm btn-icon" data-action="edit" data-id="${escapeHtml(p.id)}" title="Editar">
                        <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
                            <path d="M11 4H4a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h14a2 2 0 0 0 2-2v-7"></path>
                            <path d="M18.5 2.5a2.121 2.121 0 0 1 3 3L12 15l-4 1 1-4 9.5-9.5z"></path>
                        </svg>
                    </button>
                    <button class="btn btn-sm btn-icon" data-action="delete" data-id="${escapeHtml(p.id)}" title="Excluir">
                        <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
                            <polyline points="3 6 5 6 21 6"></polyline>
                            <path d="M19 6v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6m3 0V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2"></path>
                        </svg>
                    </button>
                </div>
            </div>
        </div>
    `;
}

// === Activity Log ===

function renderActivity(entries) {
    const container = document.getElementById('activity-log');
    const filtered = logFilter === 'all' 
        ? entries 
        : entries.filter(e => e.type === logFilter);

    if (filtered.length === 0) {
        container.innerHTML = '<div class="log-empty">Nenhuma atividade' + (logFilter !== 'all' ? ` (filtro: ${logFilter})` : '') + '</div>';
        return;
    }

    container.innerHTML = filtered.map(e => {
        const proxyHtml = e.proxy_id
            ? `<span class="log-proxy">[${escapeHtml(e.proxy_id)}]</span> `
            : '';
        return `
            <div class="log-entry" data-type="${escapeHtml(e.type)}">
                <span class="log-time">${escapeHtml(e.time_str)}</span>
                <span class="log-type ${escapeHtml(e.type)}">${escapeHtml(e.type)}</span>
                <span class="log-message">${proxyHtml}${escapeHtml(e.message)}</span>
            </div>
        `;
    }).join('');
}

function filterLog() {
    logFilter = document.getElementById('log-filter').value;
    renderActivity(activity_log_cache || []);
}

let activity_log_cache = [];

async function clearLog() {
    const result = await postJSON('/api/activity/clear', {});
    if (result?.ok) renderActivity([]);
    else showToast('Não foi possível limpar as atividades', 'error');
}

// Cache para filtro
const originalRenderActivity = renderActivity;
renderActivity = function(entries) {
    activity_log_cache = entries;
    originalRenderActivity(entries);
};

// === Config Panel ===

function toggleConfigPanel() {
    const panel = document.getElementById('config-panel');
    const overlay = document.getElementById('config-overlay');
    panel.classList.toggle('open');
    overlay.classList.toggle('open');
    panel.inert = !panel.classList.contains('open');
    if (!panel.inert) { panel._previousFocus = document.activeElement; panel.querySelector('button').focus(); }
    else panel._previousFocus?.focus();
}

function setupConfigTabs() {
    document.querySelectorAll('.config-tab').forEach(tab => {
        tab.addEventListener('click', () => {
            document.querySelectorAll('.config-tab').forEach(t => t.classList.remove('active'));
            document.querySelectorAll('.config-pane').forEach(p => p.classList.remove('active'));
            tab.classList.add('active');
            document.getElementById('tab-' + tab.dataset.tab).classList.add('active');
        });
    });
}

async function loadConfig() {
    const config = await fetchJSON('/api/config');
    if (!config) return;
    document.getElementById('config-strategy').value = config.rotation.strategy;
    document.getElementById('config-max-rotations').value = config.rotation.max_rotations;
    document.getElementById('config-shuffle').checked = config.rotation.shuffle_on_restart;
    document.getElementById('config-health-interval').value = config.health_check.interval_seconds;
    for (const key of ['http_host', 'http_port', 'socks5_host', 'socks5_port', 'web_port']) {
        document.getElementById('config-' + key.replace('_', '-')).value = config.server[key] || 5000;
    }
    document.getElementById('endpoint-http').textContent = config.server.http_host + ':' + config.server.http_port;
    document.getElementById('endpoint-socks').textContent = config.server.socks5_host + ':' + config.server.socks5_port;
}

async function saveRotationConfig() {
    const result = await postJSON('/api/config', {
        section: 'rotation', strategy: document.getElementById('config-strategy').value,
        max_rotations: Number(document.getElementById('config-max-rotations').value),
        shuffle_on_restart: document.getElementById('config-shuffle').checked,
        interval_seconds: Number(document.getElementById('config-health-interval').value),
    });
    showToast(result?.ok ? 'Rotação salva e aplicada' : result?.error || 'Erro ao salvar', result?.ok ? 'success' : 'error');
    fetchAll();
}

async function saveServerConfig() {
    const data = {section: 'server'};
    for (const key of ['http_host', 'http_port', 'socks5_host', 'socks5_port', 'web_port']) {
        data[key] = document.getElementById('config-' + key.replace('_', '-')).value;
    }
    const result = await postJSON('/api/config', data);
    showToast(result?.ok ? 'Servidor salvo. Reinicie o ProxsR para aplicar.' : result?.error || 'Erro ao salvar', result?.ok ? 'success' : 'error');
}

// === Modal Proxy ===

function setupModal() {
    document.getElementById('proxy-form').addEventListener('submit', saveProxy);
    document.getElementById('proxy-proxy_type').addEventListener('change', (e) => {
        document.getElementById('rotation-count-group').style.display = 
            e.target.value === 'rotating' ? 'block' : 'none';
    });
}

function showProxyModal(type, proxyData = null) {
    const modal = document.getElementById('proxy-modal');
    const form = document.getElementById('proxy-form');
    const title = document.getElementById('modal-title');
    
    form.reset();
    form.password.placeholder = 'senha';
    document.getElementById('proxy-id').value = '';
    document.getElementById('proxy-type').value = type;
    document.getElementById('proxy-proxy_type').value = type;
    document.getElementById('rotation-count-group').style.display = type === 'rotating' ? 'block' : 'none';
    
    if (proxyData) {
        title.textContent = 'Editar Proxy';
        document.getElementById('proxy-id').value = proxyData.id;
        document.getElementById('proxy-protocol').value = proxyData.protocol;
        document.getElementById('proxy-proxy_type').value = proxyData.type;
        form.elements.name.value = proxyData.name || '';
        form.host.value = proxyData.host;
        form.port.value = proxyData.port;
        form.username.value = proxyData.username || '';
        form.password.value = '';
        form.password.placeholder = 'Em branco mantém a senha atual';
        form.rotation_count.value = proxyData.rotation_count || 10;
        form.enabled.checked = proxyData.enabled;
    } else {
        title.textContent = type === 'rotating' ? 'Adicionar Proxy Rotativa' : 'Adicionar Proxy Normal';
    }
    
    modal.classList.add('open');
    modal._previousFocus = document.activeElement;
    form.host.focus();
}

function closeProxyModal() {
    const modal = document.getElementById('proxy-modal');
    modal.classList.remove('open');
    modal._previousFocus?.focus();
}

async function editProxy(proxyId) {
    const proxy = currentData.proxies.find(p => p.id === proxyId);
    if (proxy) {
        showProxyModal(proxy.type, proxy);
    }
}

async function saveProxy(event) {
    event.preventDefault();
    const form = event.target;
    const submit = form.querySelector('[type="submit"]');
    if (submit.disabled) return;
    submit.disabled = true;
    const formData = new FormData(form);
    const data = Object.fromEntries(formData);
    
    // Converter valores
    data.port = parseInt(data.port);
    data.rotation_count = parseInt(data.rotation_count) || 10;
    data.enabled = data.enabled === 'on';
    
    const editId = data.id || null;
    const isEdit = !!editId;
    delete data.id; // ID é gerado pelo servidor se novo
    
    const result = await postJSON('/api/proxy/save', { ...data, edit_id: editId });
    
    submit.disabled = false;
    if (result && result.ok) {
        showToast(isEdit ? 'Proxy atualizada com sucesso!' : 'Proxy adicionada com sucesso!', 'success');
        closeProxyModal();
        fetchAll();
    } else {
        showToast(result?.error || 'Erro ao salvar proxy', 'error');
    }
}

async function deleteProxy(proxyId) {
    if (!confirm('Tem certeza que deseja excluir esta proxy?')) return;
    
    const result = await postJSON('/api/proxy/delete', { proxy_id: proxyId });
    if (result && result.ok) {
        showToast('Proxy excluída com sucesso!', 'success');
        fetchAll();
    } else {
        showToast(result?.error || 'Erro ao excluir proxy', 'error');
    }
}

async function toggleProxy(proxyId, enable) {
    const result = await postJSON(enable ? '/api/proxy/enable' : '/api/proxy/disable', {proxy_id: proxyId});
    if (!result?.ok) showToast(result?.error || 'Erro ao alterar proxy', 'error');
    fetchAll();
}

async function resetRotator() {
    const result = await postJSON('/api/rotator/reset', {});
    showToast(result?.ok ? 'Ciclo reiniciado' : result?.error || 'Erro ao reiniciar', result?.ok ? 'success' : 'error');
    fetchAll();
}

async function forceNextProxy() {
    const result = await postJSON('/api/rotator/force-next', {current: currentData.rotator.last_proxy_id});
    showToast(result?.ok ? 'Proxy selecionada: ' + result.new_proxy : result?.error || 'Não foi possível trocar', result?.ok ? 'success' : 'error');
    fetchAll();
}

async function runHealthCheck() {
    const btn = document.getElementById('btn-health-check');
    btn.disabled = true;
    btn.innerHTML = '<svg class="spin" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="12" cy="12" r="10" stroke-opacity="0.25"></circle><path d="M12 2a10 10 0 0 1 10 10" stroke-opacity="1"></path></svg> Verificando...';
    
    const result = await postJSON('/api/health/run', {});
    
    btn.disabled = false;
    btn.innerHTML = `<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
        <path d="M22 11.08V12a10 10 0 1 1-5.93-9.14"></path>
        <polyline points="22 4 12 14.01 9 11.01"></polyline>
    </svg> Verificar proxies`;
    
    if (result && result.ok) {
        showToast('Verificação concluída!', 'success');
    } else {
        showToast('Erro no health check', 'error');
    }
    fetchAll();
}

// === Proxy Test Functions ===

async function testSingleProxy(proxyId) {
    const btn = document.querySelector(`[data-action="test"][data-id="${proxyId}"]`);
    if (btn) {
        btn.disabled = true;
        btn.innerHTML = '<svg class="spin" width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="12" cy="12" r="10" stroke-opacity="0.25"></circle><path d="M12 2a10 10 0 0 1 10 10" stroke-opacity="1"></path></svg>';
    }

    const result = await postJSON('/api/proxy/test', { proxy_id: proxyId });

    if (btn) {
        btn.disabled = false;
        btn.innerHTML = '<svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><polygon points="5 3 19 12 5 21 5 3"></polygon></svg> Testar';
    }

    if (result && result.ok && result.result) {
        testResults[proxyId] = result.result;
        const r = result.result;
        if (r.ok) {
            showToast(`${proxyId}: OK - IP: ${r.ip || '?'} (${r.response_time}s)`, 'success');
        } else {
            showToast(`${proxyId}: FALHA - ${r.error || 'desconhecido'}`, 'error');
        }
        renderTestResults();
        renderProxies();
    } else {
        showToast(result?.error || 'Erro ao testar proxy', 'error');
    }
}

async function testAllProxies() {
    const btn = document.getElementById('btn-test-all');
    btn.disabled = true;
    btn.innerHTML = '<svg class="spin" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="12" cy="12" r="10" stroke-opacity="0.25"></circle><path d="M12 2a10 10 0 0 1 10 10" stroke-opacity="1"></path></svg> Testando...';

    const result = await postJSON('/api/proxy/test-all', {});

    btn.disabled = false;
    btn.innerHTML = '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><polygon points="5 3 19 12 5 21 5 3"></polygon></svg> Testar Todas';

    if (result && result.ok && result.results) {
        for (const r of result.results) {
            testResults[r.id] = r;
        }
        const okCount = result.results.filter(r => r.ok).length;
        const total = result.results.length;
        showToast(`Teste concluído: ${okCount}/${total} proxies funcionando`, okCount === total ? 'success' : 'warning');
        renderTestResults();
        renderProxies();
    } else {
        showToast(result?.error || 'Erro ao testar proxies', 'error');
    }
}

function renderTestResults() {
    const panel = document.getElementById('test-results-panel');
    const list = document.getElementById('test-results-list');
    const summary = document.getElementById('test-summary');

    const entries = Object.values(testResults);
    if (entries.length === 0) {
        panel.style.display = 'none';
        return;
    }

    panel.style.display = '';
    const okCount = entries.filter(r => r.ok).length;
    const failCount = entries.length - okCount;
    summary.textContent = `${okCount} OK · ${failCount} Falha · ${entries.length} Total`;
    summary.style.color = failCount > 0 ? 'var(--accent-yellow)' : 'var(--accent-green)';

    list.innerHTML = entries.map(r => {
        const statusClass = r.ok ? 'test-ok' : 'test-fail';
        const statusIcon = r.ok ? '&#10003;' : '&#10007;';
        const tests = r.tests || {};

        let testsHTML = '';
        for (const [name, t] of Object.entries(tests)) {
            const icon = t.ok ? '&#10003;' : '&#10007;';
            const cls = t.ok ? 'test-ok' : 'test-fail';
            const detail = t.ip ? `IP: ${escapeHtml(t.ip)}` : (t.error ? escapeHtml(t.error) : `${t.time}s`);
            testsHTML += `<span class="test-sub ${cls}">${icon} ${name}: ${detail}</span>`;
        }

        return `
            <div class="test-result-item ${statusClass}">
                <div class="test-result-header">
                    <span class="test-status-icon ${statusClass}">${statusIcon}</span>
                    <span class="test-result-id">${escapeHtml(r.id)}</span>
                    <span class="test-result-protocol">${escapeHtml((r.protocol || '').toUpperCase())}</span>
                    <span class="test-result-addr">${escapeHtml(r.host)}:${r.port}</span>
                    ${r.ip ? `<span class="test-result-ip">IP: ${escapeHtml(r.ip)}</span>` : ''}
                    ${r.response_time ? `<span class="test-result-time">${r.response_time}s</span>` : ''}
                </div>
                <div class="test-result-tests">${testsHTML}</div>
            </div>
        `;
    }).join('');
}

function clearTestResults() {
    testResults = {};
    document.getElementById('test-results-panel').style.display = 'none';
    document.getElementById('test-results-list').innerHTML = '';
    renderProxies();
}

// === Toast Notifications ===

function showToast(message, type = 'info') {
    const container = document.getElementById('toast-container');
    const toast = document.createElement('div');
    toast.className = `toast ${type}`;
    
    const icons = {
        success: '<svg class="toast-icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M22 11.08V12a10 10 0 1 1-5.93-9.14"></path><polyline points="22 4 12 14.01 9 11.01"></polyline></svg>',
        error: '<svg class="toast-icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="12" cy="12" r="10"></circle><line x1="15" y1="9" x2="9" y2="15"></line><line x1="9" y1="9" x2="15" y2="15"></line></svg>',
        warning: '<svg class="toast-icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M10.29 3.86L1.82 18a2 2 0 0 0 1.71 3h16.94a2 2 0 0 0 1.71-3L13.71 3.86a2 2 0 0 0-3.42 0z"></path><line x1="12" y1="9" x2="12" y2="13"></line><line x1="12" y1="17" x2="12.01" y2="17"></line></svg>',
        info: '<svg class="toast-icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="12" cy="12" r="10"></circle><line x1="12" y1="16" x2="12" y2="12"></line><line x1="12" y1="8" x2="12.01" y2="8"></line></svg>',
    };
    
    toast.innerHTML = `
        ${icons[type] || icons.info}
        <span class="toast-message">${escapeHtml(message)}</span>
        <button class="toast-close" onclick="this.parentElement.remove()">&times;</button>
    `;
    
    container.appendChild(toast);
    
    setTimeout(() => {
        toast.style.animation = 'slideInRight 0.3s ease reverse';
        setTimeout(() => toast.remove(), 300);
    }, 5000);
}

// === Util ===

function escapeHtml(text) {
    return String(text ?? '').replace(/[&<>"']/g, char => ({'&':'&amp;', '<':'&lt;', '>':'&gt;', '"':'&quot;', "'":'&#39;'}[char]));
}

function updateHealthStatus(health) {
    document.getElementById('health-label').textContent = health.running
        ? `Verificação automática a cada ${health.interval}s` : 'Verificação automática pausada';
}

document.addEventListener('click', event => {
    const button = event.target.closest('[data-action]');
    if (!button) return;
    if (button.dataset.action === 'toggle') toggleProxy(button.dataset.id, button.dataset.enable === 'true');
    if (button.dataset.action === 'edit') editProxy(button.dataset.id);
    if (button.dataset.action === 'delete') deleteProxy(button.dataset.id);
    if (button.dataset.action === 'test') testSingleProxy(button.dataset.id);
});
document.addEventListener('keydown', event => {
    const modal = document.getElementById('proxy-modal');
    const panel = document.getElementById('config-panel');
    if (event.key === 'Escape') {
        if (modal.classList.contains('open')) closeProxyModal();
        else if (panel.classList.contains('open')) toggleConfigPanel();
    }
    const active = modal.classList.contains('open') ? modal : panel.classList.contains('open') ? panel : null;
    if (event.key === 'Tab' && active) {
        const elements = [...active.querySelectorAll('button, input, select')].filter(el => el.offsetParent && !el.disabled);
        const first = elements[0], last = elements[elements.length - 1];
        if (event.shiftKey && document.activeElement === first) { event.preventDefault(); last.focus(); }
        else if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first.focus(); }
    }
});

// Adicionar estilo para spinner do botão
const style = document.createElement('style');
style.textContent = `
    .spin { animation: rotate 1s linear infinite; transform-origin: center; }
    @keyframes rotate { from { transform: rotate(0deg); } to { transform: rotate(360deg); } }
`;
document.head.appendChild(style);

// === System Proxy Functions ===

let systemProxyEnabled = false;

async function loadSystemProxyStatus() {
    try {
        const resp = await fetchJSON('/api/system-proxy/status');
        if (resp && resp.ok) {
            updateSystemProxyUI(resp.enabled, resp.method);
            updateSystemProxyDetails(resp);
        }
    } catch (e) {
        console.error('Erro ao carregar status do system proxy:', e);
    }
}

function updateSystemProxyDetails(data) {
    const details = document.getElementById('sys-proxy-details');
    const methodEl = document.getElementById('sys-proxy-method');
    const pacEl = document.getElementById('sys-proxy-pac');
    const shellEl = document.getElementById('sys-proxy-shell');

    if (!data.enabled) {
        details.style.display = 'none';
        return;
    }

    details.style.display = 'flex';

    // Método
    const methods = { gnome: 'GNOME', kde: 'KDE Plasma', environment: 'Variáveis de Ambiente', none: 'Nenhum' };
    methodEl.textContent = `Método: ${methods[data.method] || data.method}`;

    // PAC
    if (data.pac_url) {
        pacEl.textContent = `PAC: ${data.pac_url}`;
        pacEl.title = data.pac_url;
    } else {
        pacEl.textContent = 'PAC: não gerado';
    }

    // Shell integration
    if (data.shell_integration) {
        shellEl.textContent = 'Shell: integrado ✓';
        shellEl.style.color = 'var(--accent-green)';
    } else if (data.method === 'environment') {
        shellEl.textContent = 'Shell: source ~/.proxsr_env necessário';
        shellEl.style.color = 'var(--accent-yellow)';
    } else {
        shellEl.textContent = '';
    }
}

function updateSystemProxyUI(enabled, method = 'none') {
    systemProxyEnabled = enabled;
    const btn = document.getElementById('btn-system-proxy');
    const label = document.getElementById('sys-proxy-label');
    const status = document.getElementById('sys-proxy-status');
    const container = document.getElementById('system-proxy-toggle');
    
    if (enabled) {
        btn.classList.add('active');
        container.classList.add('active');
        status.textContent = 'ON';
        status.style.color = 'var(--accent-green)';
        label.textContent = 'Sistema';
        btn.title = `Proxy do sistema ATIVO (${method}) - Clique para desativar`;
    } else {
        btn.classList.remove('active');
        container.classList.remove('active');
        status.textContent = 'OFF';
        status.style.color = 'var(--text-muted)';
        label.textContent = 'Sistema';
        btn.title = 'Proxy do sistema INATIVO - Clique para ativar';
    }
}

async function toggleSystemProxy() {
    const btn = document.getElementById('btn-system-proxy');
    btn.disabled = true;
    btn.style.opacity = '0.6';
    
    try {
        const result = await postJSON('/api/system-proxy/toggle', { enabled: !systemProxyEnabled });
        
        if (result && result.ok) {
            updateSystemProxyUI(result.method === 'environment' ? false : !systemProxyEnabled, result.method);
            // Recarrega status completo
            loadSystemProxyStatus();
            const methods = { gnome: 'GNOME', kde: 'KDE', environment: 'variáveis de ambiente', none: '' };
            const methodName = methods[result.method] || '';
            if (result.method === 'environment') {
                showToast(`Arquivo de ambiente criado. Para aplicar no terminal: source ~/.proxsr_env`, 'info');
            } else if (systemProxyEnabled) {
                showToast(`Proxy do sistema ativado via ${methodName}!`, 'success');
            } else {
                showToast(`Proxy do sistema desativado!`, 'success');
            }
        } else {
            showToast(result?.error || 'Erro ao alterar proxy do sistema', 'error');
        }
    } catch (e) {
        showToast('Erro de conexão', 'error');
    } finally {
        btn.disabled = false;
        btn.style.opacity = '';
    }
}

// Inicializa status do system proxy ao carregar
document.addEventListener('DOMContentLoaded', () => {
    // ... existing code ...
    setTimeout(loadSystemProxyStatus, 1000);
    setInterval(loadSystemProxyStatus, 30000); // Verifica a cada 30s
});