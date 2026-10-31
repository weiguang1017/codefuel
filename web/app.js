'use strict';
const $ = (s, el = document) => el.querySelector(s);
const $$ = (s, el = document) => [...el.querySelectorAll(s)];
const icon = (name, cls = '') => `<svg class="${cls}"><use href="#i-${name}"/></svg>`;
const esc = (v) => String(v ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const labels = {idle:'等待签到',running:'正在签到',queued:'排队中',success:'签到成功',already_checked:'今日已签到',synced:'状态已同步',manual_required:'需要处理',failed:'签到失败',interrupted:'已中断'};
const providers = () => state.data?.providers || [];
const providerName = id => esc(providers().find(p=>p.id===id)?.name || id || '未知平台');
const isOAuth = method => ['github','linuxdo'].includes(method);
const isBrowser = method => isOAuth(method) || method==='manual';
const money = n => n == null ? '—' : `$${Number(n).toFixed(2)}`;
const authLabel = a => a.auth_mode === 'password' ? '邮箱密码'
  : a.login_via === 'github' ? 'GitHub 授权'
  : a.login_via === 'linuxdo' ? 'LinuxDO 授权'
  : a.login_via === 'manual' ? '浏览器登录'
  : 'Session';
const state = {data:null, view:'overview', filter:'all', query:'', editing:null, busy:false, loginMethod:'github', loginSession:null, reauth:false, connecting:null, selectedProviders:[], loginQueue:null};
let toastTimer, loginPoll;
function toast(message){ const el=$('#toast'); el.textContent=message; el.classList.add('show'); clearTimeout(toastTimer); toastTimer=setTimeout(()=>el.classList.remove('show'),3300); }
async function api(path, options={}) {
  const method=options.method || 'GET';
  const response=await fetch(path,{method,headers:method==='GET'?{}:{'Content-Type':'application/json','X-CSRF-Token':state.data.csrf_token},body:options.body?JSON.stringify(options.body):undefined});
  if(!response.ok){let msg='请求未完成';try{const b=await response.json();msg=typeof b.detail==='string'?b.detail:'请检查填写内容';}catch{}const error=new Error(msg);error.definitive=response.status>=400&&response.status<500;throw error;}
  return response.json();
}
async function refresh(silent=false){
  try{const res=await fetch('/api/bootstrap'); if(!res.ok)throw new Error(); state.data=await res.json(); render(); if(!silent){} }
  catch{if(!silent)toast('无法连接本地服务，请确认启动窗口仍在运行。')}
}
function timeLabel(value, date=false){
  if(!value)return '尚未执行';
  const d=new Date(value); if(Number.isNaN(d.getTime()))return '—';
  const diff=d-Date.now();
  if(diff>0&&diff<86400000){const h=Math.floor(diff/3600000),m=Math.max(1,Math.floor(diff%3600000/60000));return h?`${h} 小时 ${m} 分后`:`${m} 分钟后`;}
  return d.toLocaleString('zh-CN',{month:'2-digit',day:'2-digit',hour:'2-digit',minute:'2-digit',hour12:false});
}
function nextRun(){return state.data.accounts.filter(a=>a.enabled&&a.next_run).sort((a,b)=>a.next_run.localeCompare(b.next_run))[0];}
function metric(title,value,note,ico){return `<div class="metric"><div class="metric-top"><span>${title}</span><span class="metric-icon">${icon(ico)}</span></div><div class="metric-value">${value}</div><div class="metric-note">${note}</div></div>`;}
function stats(){const s=state.data.stats;const n=state.data.accounts.length;return `<section class="metrics">
  ${metric('当前总余额',s.balance==null?'<span style="color:#b3bbb5">—</span>':`<small>$</small>${s.balance.toFixed(2)}`,s.balance==null?'添加账号后，从这里掌握额度':'已同步账号的余额合计','wallet')}
  ${metric('今日已领取',`<small>$</small>${s.today_reward.toFixed(2)}`,s.today_reward?'仅统计有额度佐证的到账':'暂无可确认的额度到账','trend')}
  ${metric('今日完成',`${s.today_completed}<small> / ${n}</small>`,n?'成功、已签到或状态同步':'等待添加第一个账号','check')}
  ${metric('管理账号',`${n}<small> 个</small>`,`${state.data.accounts.filter(a=>a.enabled).length} 个已启用自动签到`,'layers')}</section>`;}
function chart(){const trend=state.data.trend;const max=Math.max(1,...trend.map(t=>t.reward));const has=trend.some(t=>t.runs);return `<section class="panel"><div class="panel-head"><div><div class="panel-title">额度变化</div><div class="panel-sub">最近 7 天 · 以站点实际返回为准</div></div><span class="panel-sub">单位 / USD</span></div><div class="chart-wrap"><div class="chart-y"><span>$${max}</span><span>$${(max/2).toFixed(1)}</span><span>$0</span></div><div class="chart-bars">${trend.map((t,i)=>`<div class="bar-day"><div class="bar-track"><div class="bar" style="height:${t.reward?Math.max(8,t.reward/max*100):1}%">${t.reward?`<span class="bar-value">+$${t.reward}</span>`:''}</div></div><label>${i===6?'今天':t.date}</label></div>`).join('')}</div>${has?'':`<div class="chart-empty">${icon('trend')}<span>完成首次签到后，这里会记录额度轨迹</span></div>`}</div></section>`;}
function schedule(){const s=state.data.settings,n=nextRun();return `<section class="panel schedule-panel"><div class="panel-head"><div class="panel-title">自动签到</div><button class="icon-button" data-action="settings" title="定时设置">${icon('settings')}</button></div><div class="schedule-main"><div class="schedule-status"><i></i>${s.scheduler_enabled?'调度器已启用':'调度器已暂停'}</div><div class="schedule-time">${n?timeLabel(n.next_run).replace(' 后',''):'—'}<small>${n?'':'未配置'}</small></div><div class="schedule-caption">${n?`${esc(n.name)} · ${providerName(n.provider)}`:'添加账号后自动安排下一次任务'}</div></div><div class="switch-row"><span>后台定时执行</span><button class="switch ${s.scheduler_enabled?'on':''}" data-action="toggle-scheduler" aria-label="切换自动签到" aria-pressed="${s.scheduler_enabled}"></button></div></section>`;}
function statusBadge(status){return `<span class="status-badge ${esc(status)}">${labels[status]||esc(status)}</span>`;}
function accountRows(accounts){
  if(!accounts.length)return `<tr><td colspan="7" class="empty-row"><div class="empty-state"><div class="empty-illustration">${icon('users')}</div><b>${state.query||state.filter!=='all'?'没有匹配的账号':'从这里，开始自动化每日签到'}</b><p>${state.query||state.filter!=='all'?'试试其他关键词或平台筛选':'账号凭据加密保存，仅用于本机签到'}</p>${state.query||state.filter!=='all'?'':`<button class="button primary small" data-action="add">${icon('plus')}添加第一个账号</button>`}</div></td></tr>`;
  return accounts.map(a=>`<tr><td><div class="account-name"><span class="provider-badge ${a.provider==='anyrouter'?'any':'agent'}">A</span><div>${esc(a.name)}<small>${providerName(a.provider)}</small></div></div></td><td>${statusBadge(a.status)}</td><td class="balance">${money(a.balance)}</td><td>${timeLabel(a.last_run)}</td><td class="hide-medium">${a.enabled?timeLabel(a.next_run):'已停用自动签到'}</td><td class="hide-medium">${authLabel(a)}</td><td><div class="row-actions"><button class="icon-button" data-action="run" data-id="${a.id}" title="立即签到" ${['running','queued'].includes(a.status)?'disabled':''}>${['running','queued'].includes(a.status)?'<i class="inline-spinner"></i>':icon('play')}</button>${a.auth_mode==='session'?`<button class="icon-button" data-action="relogin" data-id="${a.id}" title="重新授权登录">${icon('refresh')}</button>`:''}${isOAuth(a.login_via)?`<button class="icon-button" data-action="connect-platform" data-id="${a.id}" title="连接其他平台" aria-label="连接其他平台">${icon('plus')}</button>`:''}<button class="icon-button" data-action="edit" data-id="${a.id}" title="编辑账号">${icon('settings')}</button></div></td></tr>`).join('');
}
function accountsPanel(limit=false){let accounts=state.data.accounts;if(state.filter!=='all')accounts=accounts.filter(a=>a.provider===state.filter);if(state.query)accounts=accounts.filter(a=>a.name.toLowerCase().includes(state.query.toLowerCase()));if(limit)accounts=accounts.slice(0,5);return `<section class="panel accounts-panel"><div class="panel-head"><div><div class="panel-title">${limit?'我的账号':'账号管理'}</div><div class="panel-sub">${limit?'统一管理已适配平台的独立账号':'凭据加密保存，可随时更新'}</div></div><div class="accounts-head-actions">${limit?`<button class="panel-link" data-view="accounts">查看全部${icon('arrow')}</button>`:''}<button class="button secondary small" data-action="add">${icon('plus')}添加账号</button></div></div><div class="table-wrap"><table class="account-table"><thead><tr><th>账号 / 平台</th><th>状态</th><th>当前余额</th><th>最近签到</th><th class="hide-medium">下次执行</th><th class="hide-medium">认证方式</th><th style="text-align:right">操作</th></tr></thead><tbody>${accountRows(accounts)}</tbody></table></div></section>`;}
function recentRuns(limit=5){const runs=state.data.runs.slice(0,limit);return `<section class="panel"><div class="panel-head"><div class="panel-title">最近动态</div><button class="panel-link" data-view="logs">完整日志${icon('arrow')}</button></div>${runs.length?runs.map(r=>`<button class="recent-item" style="width:100%;background:none;border-left:0;border-right:0;border-bottom:0;text-align:left" data-action="log" data-id="${r.id}"><span class="provider-badge ${r.provider==='anyrouter'?'any':'agent'}" style="width:27px;height:27px;font-size:14px">A</span><div class="grow"><b style="font-weight:500;color:#64736a">${esc(r.account_name)}</b><small>${providerName(r.provider)} · ${r.trigger==='manual'?'手动执行':'定时执行'}</small></div>${statusBadge(r.status)}<time>${timeLabel(r.started_at)}</time>${icon('chevron')}</button>`).join(''):`<div class="empty-row"><div class="empty-state">${icon('list')}<b>还没有运行记录</b><p>每次执行都会留下可追溯的结果</p></div></div>`}</section>`;}
function referralBanner(){return `<section class="referral-banner"><div class="referral-banner-icon">${icon('gift')}</div><div class="referral-banner-text"><b>还没账号？通过专属链接注册，额外多得 <em>$50.00</em> 奖励额度</b><p>注册后把账号加进码粮，每日签到持续累积额度</p></div><div class="referral-banner-links"><a class="referral-link" href="https://agentrouter.org/register?aff=MBCp" target="_blank" rel="noopener">注册 AgentRouter${icon('arrow')}</a><a class="referral-link" href="https://anyrouter.top/register?aff=x73u" target="_blank" rel="noopener">注册 AnyRouter${icon('arrow')}</a></div></section>`;}
function overview(){return `${referralBanner()}${stats()}<div class="overview-grid">${chart()}${schedule()}</div>${accountsPanel(true)}<div class="recent-grid">${recentRuns()}</div>`;}
function accountsView(){return `<div class="toolbar"><div class="search">${icon('search')}<input id="account-search" value="${esc(state.query)}" placeholder="搜索账号名称…" aria-label="搜索账号"></div><div class="filter-group">${[['all','全部平台'],...providers().map(p=>[p.id,p.name]),...Array.from(new Set(state.data.accounts.map(a=>a.provider))).filter(id=>!providers().some(p=>p.id===id)).map(id=>[id,id])].map(([v,t])=>`<button class="filter ${state.filter===v?'active':''}" data-filter="${esc(v)}">${esc(t)}</button>`).join('')}</div></div><div class="view-panel">${accountsPanel()}</div>`;}
function logsView(){const runs=state.data.runs;return `<section class="panel"><div class="panel-head"><div><div class="panel-title">运行日志</div><div class="panel-sub">展示最近 100 条 · 原始凭据不会写入日志</div></div><button class="button secondary small" data-action="export">${icon('download')}导出 CSV</button></div><div class="table-wrap"><table class="account-table"><thead><tr><th>执行时间</th><th>账号 / 平台</th><th>触发方式</th><th>状态</th><th>额度变化</th><th>结果说明</th><th></th></tr></thead><tbody>${runs.length?runs.map(r=>`<tr><td style="white-space:nowrap">${timeLabel(r.started_at,true)}</td><td><div class="account-name">${esc(r.account_name)}<small>${providerName(r.provider)}</small></div></td><td>${r.trigger==='manual'?'手动':'定时'}</td><td>${statusBadge(r.status)}</td><td class="balance ${r.reward>0?'success-text':''}">${r.reward==null?'—':(r.reward>0?'+':'')+money(r.reward)}</td><td class="log-message">${esc(r.message)}</td><td><button class="icon-button" data-action="log" data-id="${r.id}">${icon('chevron')}</button></td></tr>`).join(''):`<tr><td colspan="7" class="empty-row"><div class="empty-state">${icon('list')}<b>日志还是空的</b><p>点击「立即签到全部」或单账号的播放按钮开始</p></div></td></tr>`}</tbody></table></div></section>`;}
function render(){if(!state.data)return;$('#provider-links').innerHTML=providers().map(p=>`<div><span class="mini-provider ${p.id==='anyrouter'?'any':'agent'}">${esc(p.name.slice(0,1))}</span>${esc(p.name)}</div>`).join('');const titles={overview:['概览','为代码添粮，<span>让灵感续航。</span>','码粮集中管理已适配平台的签到、额度与运行记录。'],accounts:['账号管理','账号在手，<span>井然有序。</span>','集中管理各平台的独立账号、认证方式与签到状态。'],logs:['运行日志','每一次执行，<span>都有迹可循。</span>','查看真实站点响应、额度变化与需要人工处理的情况。']};const t=titles[state.view];$('#crumb').textContent=t[0];$('#page-title').innerHTML=t[1];$('#page-description').textContent=t[2];$('#nav-count').textContent=state.data.accounts.length;$$('.nav-item[data-view]').forEach(el=>el.classList.toggle('active',el.dataset.view===state.view));$('#app').innerHTML=state.view==='overview'?overview():state.view==='accounts'?accountsView():logsView();}
function modal(content){$('#modal-content').innerHTML=content;$('#modal').showModal();}
function stopLoginPoll(){clearTimeout(loginPoll);loginPoll=null;}
function closeModal(){
  stopLoginPoll();
  const queue=state.loginQueue;
  if(queue){
    queue.stopped=true;
    if(queue.session)void cancelLoginSession(queue,queue.session);
  }
  state.loginSession=null;
  $('#modal').close();
  state.editing=null;
  state.connecting=null;
}
function methodName(method){return {github:'GitHub',linuxdo:'LinuxDO',manual:'浏览器手动登录',password:'账号密码',session:'Session'}[method]||method;}
function accountMethod(account){return account.auth_mode==='password'?'password':(['github','linuxdo','manual'].includes(account.login_via)?account.login_via:'session');}
function eligibleProviders(){return providers().filter(p=>(p.login_methods||[]).includes(state.loginMethod)&&p.id!==state.connecting?.provider);}
function accountModal(account=null,connecting=null){
  if(state.loginQueue&&!state.loginQueue.stopped&&!state.loginQueue.done)return;
  state.loginQueue=null;
  state.editing=account;
  state.connecting=connecting;
  state.reauth=false;
  state.loginMethod=account?accountMethod(account):connecting?connecting.login_via:'github';
  state.selectedProviders=[];
  modal(`<div class="modal-head"><div><h2>${account?'编辑账号':connecting?'连接其他平台':'添加账号'}</h2><p>${account?'平台不可修改；连接其他平台需要独立授权。':connecting?`沿用「${esc(connecting.name)}」的身份方式，不复制原平台 Cookie。`:'先选择登录方式，再选择要独立连接的平台。'}</p></div><button class="icon-button" data-action="close" aria-label="关闭">${icon('x')}</button></div>
  <form id="account-form">
    <div class="field"><label>账号备注</label><input name="name" required maxlength="40" value="${esc((account||connecting)?.name||'')}" placeholder="例如：我的主账号"></div>
    <div class="field"><label>1 · 登录方式</label>
      <div class="login-methods">
        <button type="button" class="login-method" data-method="github">${icon('github','brand-icon')}<span>使用 GitHub 继续</span></button>
        <button type="button" class="login-method" data-method="linuxdo">${icon('linuxdo','brand-icon')}<span>使用 LinuxDO 继续</span></button>
      </div>
      <div class="login-or"><span>或使用其他方式</span></div>
      <div class="segmented"><button type="button" data-method="password">账号密码</button><button type="button" data-method="session">Session Cookie</button><button type="button" data-method="manual">浏览器手动登录</button></div>
    </div>
    <div id="platform-picker"></div>
    <div class="field"><label>执行间隔</label><select name="interval_hours">${[3,6,12,24].map(h=>`<option value="${h}" ${((account||connecting)?.interval_hours||6)===h?'selected':''}>每 ${h} 小时</option>`).join('')}</select></div>
    <div id="credentials"></div>
    <div class="notice">${icon('shield')}每个平台分别授权、分别保存站点 Cookie，不会自动合并账号，也不会收集 GitHub / LinuxDO 密码。</div>
    <div class="modal-actions">${account?`<button type="button" class="button ghost danger" data-action="delete" data-id="${account.id}" style="margin-right:auto">删除账号</button>`:''}<button type="button" class="button secondary" data-action="close">取消</button><button class="button primary" type="submit" id="account-submit"></button></div>
  </form>`);
  syncMethod();
}
function platformPicker(){
  const target=$('#platform-picker');if(!target)return;
  const eligible=eligibleProviders();
  if(state.editing){
    state.selectedProviders=[state.editing.provider];
    target.innerHTML=`<div class="field"><label>2 · 所属平台（不可修改）</label><select name="provider" disabled><option value="${esc(state.editing.provider)}">${providerName(state.editing.provider)}</option></select><p class="field-help">连接其他平台请使用账号列表中的「连接其他平台」，每个平台独立授权。</p></div>`;
  }else{
    const valid=state.selectedProviders.filter(id=>eligible.some(p=>p.id===id));
    state.selectedProviders=valid.length?valid:[eligible.find(p=>p.id==='anyrouter')?.id||eligible[0]?.id].filter(Boolean);
    if(!isOAuth(state.loginMethod))state.selectedProviders=state.selectedProviders.slice(0,1);
    const multiple=isOAuth(state.loginMethod);
    target.innerHTML=`<div class="field"><div class="platform-heading"><label>2 · ${multiple?'选择平台（可多选）':'选择平台'}</label>${multiple&&eligible.length?'<button type="button" class="panel-link" data-action="select-all-platforms">选择全部适配平台</button>':''}</div>${multiple?`<div class="platform-options">${eligible.map(p=>`<label class="platform-option"><input type="checkbox" name="providers" value="${esc(p.id)}" ${state.selectedProviders.includes(p.id)?'checked':''}><span><b>${esc(p.name)}</b><small>已适配 · 登录时检测入口</small></span></label>`).join('')}</div>`:`<select name="provider">${eligible.map(p=>`<option value="${esc(p.id)}" ${state.selectedProviders.includes(p.id)?'selected':''}>${esc(p.name)}</option>`).join('')}</select>`}${eligible.length?`<p class="field-help">${multiple?'同一个身份可以连接多个平台，但必须逐个平台完成授权。已适配不代表当前站点已提供对应入口。':'此登录方式一次仅连接一个平台。'}</p>`:'<p class="field-help">当前没有适配此登录方式的可选平台。</p>'}</div>`;
  }
  updateAccountSubmit();
}
function updateAccountSubmit(){
  const submit=$('#account-submit');if(!submit)return;
  const browser=isBrowser(state.loginMethod);
  submit.disabled=!state.editing&&!state.selectedProviders.length;
  submit.textContent=browser?(state.editing?(state.reauth?'重新授权并保存':'保存修改'):`${isOAuth(state.loginMethod)?'开始逐平台授权':'打开登录窗口'}${isOAuth(state.loginMethod)?`（${state.selectedProviders.length}）`:''}`):(state.editing?'保存修改':'添加账号');
}
function syncMethod(){
  $$('[data-method]').forEach(b=>{
    b.classList.toggle('active',b.dataset.method===state.loginMethod);
    const candidates=providers().filter(p=>state.editing?p.id===state.editing.provider:p.id!==state.connecting?.provider);
    b.disabled=(!!state.connecting&&b.dataset.method!==state.connecting.login_via)||!candidates.some(p=>(p.login_methods||[]).includes(b.dataset.method));
  });
  platformPicker();
  credentialFields(state.loginMethod,!!state.editing);
}
function credentialFields(method,editing=false){
  const f=$('#credentials');if(!f)return;
  if(isBrowser(method)){
    const label=methodName(method);
    f.innerHTML=`<div class="auth-hint">${icon(isOAuth(method)?method:'users','brand-icon')}<div><b>${isOAuth(method)?`使用 ${label} 授权登录`:'在浏览器中手动登录'}</b><p>${isOAuth(method)?'按所选平台依次打开独立授权窗口；每个平台经后端确认成功后才会保存，并继续下一个。':'在当前平台的浏览器窗口中登录，完成后校验并保存为浏览器登录账号。'}</p></div></div>${isOAuth(method)?`<div class="notice">${icon('info')}如果平台不支持或未提供 ${label} 入口，会明确显示原因；你可以重试或跳过，不会将其他登录方式冒充为 ${label} 授权。</div>`:''}`;
    return;
  }
  const preserve=editing&&accountMethod(state.editing)===method;
  if(method==='password'){
    f.innerHTML=`<div class="field"><label>登录邮箱</label><input name="email" type="email" maxlength="200" placeholder="${preserve?'留空则保留原邮箱':'name@example.com'}" ${preserve?'':'required'} autocomplete="username"></div><div class="field"><label>登录密码</label><input name="password" type="password" maxlength="300" placeholder="${preserve?'留空则保留原密码':'输入此平台的登录密码'}" ${preserve?'':'required'} autocomplete="new-password"></div><div class="notice">${icon('info')}切换为账号密码需要填写新凭据；这里不收集 OAuth 身份提供方密码。</div>`;
    return;
  }
  f.innerHTML=`<div class="field"><label>Session Cookie</label><input name="session" type="password" maxlength="8000" placeholder="${preserve?'留空则保留原 Session':'仅填写当前平台 session 的值'}" ${preserve?'':'required'} autocomplete="off"></div><div class="field"><label>API User</label><input name="api_user" ${preserve?'':'required'} maxlength="100" placeholder="${preserve?'留空则保留原值':'当前平台请求头 new-api-user 的值'}"></div><div class="notice">${icon('info')}切换为 Session 需要填写此平台的新凭据，不会复用其他认证方式或其他平台的 Cookie。</div>`;
}
function settingsModal(){const s=state.data.settings;modal(`<div class="modal-head"><div><h2>定时设置</h2><p>任务由本地后台执行，关闭此页面不会中断。</p></div><button class="icon-button" data-action="close">${icon('x')}</button></div><form id="settings-form"><div class="settings-line"><div><b>启用自动签到</b><p>仅为已启用的账号安排定时任务</p></div><button type="button" class="switch ${s.scheduler_enabled?'on':''}" data-action="modal-switch"></button></div><div class="settings-line"><div><b>统一执行间隔</b><p>保存后将应用到所有账号</p></div><select name="interval_hours">${[3,6,12,24].map(h=>`<option value="${h}" ${s.interval_hours===h?'selected':''}>每 ${h} 小时</option>`).join('')}</select></div><input type="hidden" name="scheduler_enabled" value="${s.scheduler_enabled}"><div class="notice" style="margin-top:20px">${icon('info')}电脑关机、休眠或退出后台后无法执行；恢复运行时会补做一次到期任务。站点可能尚未到领取时间，此时会记录为「已签到」。</div><div class="modal-actions"><button type="button" class="button secondary" data-action="close">取消</button><button class="button primary" type="submit">保存设置</button></div></form>`);}
async function saveAccount(form){
  if(state.busy)return;
  state.busy=true;
  try{
    const data=Object.fromEntries(new FormData(form));
    const method=state.loginMethod,editing=state.editing;
    const name=(data.name||'').trim();
    if(!name)throw new Error('请先填写账号备注');
    const selected=editing?[editing.provider]:state.selectedProviders;
    if(!selected.length)throw new Error('请至少选择一个平台');
    if(isBrowser(method)&&(!editing||state.reauth)){
      return launchLogin({method,interval_hours:Number(data.interval_hours),name,account_id:editing?.id||null},selected);
    }
    const mode=method==='password'?'password':'session';
    const body={name,provider:editing?.provider||selected[0],auth_mode:mode,interval_hours:Number(data.interval_hours),enabled:editing?editing.enabled:true};
    if(!isBrowser(method))body.login_via=method;
    for(const k of ['password','session','api_user','email'])if(data[k])body[k]=data[k];
    await api(editing?`/api/accounts/${editing.id}`:'/api/accounts',{method:editing?'PUT':'POST',body});
    closeModal();toast(editing?'账号已更新':'账号已添加，可点击播放按钮进行首次签到');await refresh(true);
  }finally{state.busy=false;updateAccountSubmit();}
}
function queueActive(queue){return !!queue&&state.loginQueue===queue&&!queue.stopped&&!queue.done;}
function launchLogin(options,selected=[options.provider]){
  if(state.loginQueue&&!state.loginQueue.stopped&&!state.loginQueue.done)return;
  const queue={options,items:selected.map(provider=>({provider,status:'pending',message:'等待授权'})),index:0,session:null,stopped:false,done:false,starting:false,polling:false,cancelling:false};
  state.loginQueue=queue;
  void startQueueItem(queue);
}
function renderLoginQueue(queue){
  if(state.loginQueue!==queue||queue.stopped)return;
  const item=queue.items[queue.index];
  const statuses={pending:'等待',starting:'正在发起',waiting:'等待授权',success:'已保存',error:'未完成',network:'连接中断',unknown:'结果待确认',skipped:'已跳过'};
  const finished=queue.items.filter(i=>i.status==='success').length;
  modal(`<div class="modal-head"><div><h2>${queue.done?'授权结果':`${methodName(queue.options.method)} · 逐平台授权`}</h2><p>${queue.done?`${finished} / ${queue.items.length} 个平台已独立保存`:`当前 ${queue.index+1} / ${queue.items.length} · ${providerName(item.provider)}`}；已完成的账号会保留。</p></div><button class="icon-button" data-action="close" aria-label="关闭">${icon('x')}</button></div>
    <ol class="platform-progress" aria-live="polite">${queue.items.map((entry,index)=>`<li class="${esc(entry.status)} ${index===queue.index&&!queue.done?'current':''}"><div><b>${providerName(entry.provider)}</b><span>${statuses[entry.status]||esc(entry.status)}</span></div><p>${esc(entry.message)}</p></li>`).join('')}</ol>
    ${!queue.done?`<div class="notice">${icon('info')}每个平台分别在浏览器中授权。入口不可用时可跳过；取消或关闭会停止整个队列，不撤销已保存账号。</div>`:''}
    <div class="modal-actions">${queue.done?'<button class="button primary" data-action="close">完成</button>':`<button class="button secondary" data-action="cancel-login">取消全部</button>${item.status==='error'?'<button class="button secondary" data-action="skip-login">跳过此平台</button><button class="button primary" data-action="retry-login">重试此平台</button>':''}${item.status==='network'?'<button class="button primary" data-action="retry-poll">重试查询当前授权</button>':''}${item.status==='unknown'?'<span class="field-help">为避免重复账号，不自动重新发起授权。</span>':''}`}</div>`);
}
async function startQueueItem(queue){
  if(!queueActive(queue)||queue.starting)return;
  const item=queue.items[queue.index];
  queue.starting=true;
  item.status='starting';item.message='正在请求打开此平台的登录窗口…';
  renderLoginQueue(queue);
  try{
    const res=await api('/api/login/start',{method:'POST',body:{...queue.options,provider:item.provider}});
    if(!res.id)throw new Error('服务未返回授权会话编号，结果尚无法确认');
    queue.session=res.id;
    if(!queueActive(queue)){await cancelLoginSession(queue,res.id);return;}
    state.loginSession=res.id;
    item.status='waiting';item.message=res.message||'请在浏览器中完成当前平台授权。';
    renderLoginQueue(queue);
    void pollLogin(queue);
  }catch(error){
    if(!queueActive(queue)){toast('授权请求结果尚未确认，请检查登录窗口与账号列表；队列已停止。');return;}
    item.status=error.definitive?'error':'unknown';
    item.message=error.definitive?error.message:`网络或响应异常：${error.message}。请求可能已到达服务，请先核对账号列表和登录窗口。`;
    renderLoginQueue(queue);
  }finally{queue.starting=false;}
}
async function pollLogin(queue){
  if(!queueActive(queue)||queue.polling||!queue.session)return;
  queue.polling=true;
  const id=queue.session,item=queue.items[queue.index];
  try{
    const result=await api(`/api/login/${encodeURIComponent(id)}`);
    if(!queueActive(queue)||queue.session!==id)return;
    item.message=result.message||'等待当前平台完成授权…';
    if(result.status==='success'){
      item.status='success';queue.session=null;state.loginSession=null;
      renderLoginQueue(queue);
      await refresh(true);
      if(queueActive(queue))advanceQueue(queue);
      return;
    }
    if(['error','cancelled','expired','unavailable','failed'].includes(result.status)){
      item.status='error';queue.session=null;state.loginSession=null;
      if(result.status==='unavailable')item.message=result.message||`平台不支持或未提供 ${methodName(queue.options.method)} 入口。`;
      renderLoginQueue(queue);return;
    }
    item.status='waiting';renderLoginQueue(queue);
    loginPoll=setTimeout(()=>pollLogin(queue),1000);
  }catch(error){
    if(!queueActive(queue))return;
    item.status='network';item.message=`查询失败：${error.message}。可重试查询同一会话，不会再次创建授权。`;
    renderLoginQueue(queue);
  }finally{queue.polling=false;}
}
function advanceQueue(queue){
  if(!queueActive(queue))return;
  if(queue.index+1>=queue.items.length){queue.done=true;renderLoginQueue(queue);return;}
  queue.index++;
  // 等当前轮询退出后才发起下一平台，避免共享状态与轮询重叠。
  loginPoll=setTimeout(()=>startQueueItem(queue),0);
}
async function cancelLoginSession(queue,id){
  if(queue.cancelling)return;
  queue.cancelling=true;
  try{
    const result=await api(`/api/login/${encodeURIComponent(id)}/cancel`,{method:'POST',body:{}});
    if(result.status==='success')toast('队列已停止；当前平台在取消前已保存，账号会保留。');
    else toast('队列已停止，已完成的账号保留。');
    if(queue.session===id)queue.session=null;
    await refresh(true);
  }catch(error){toast(`队列已停止，但取消当前窗口未确认：${error.message}。请手动关闭登录窗口。`);}
  finally{queue.cancelling=false;}
}
async function toggleScheduler(){const s=state.data.settings;await api('/api/settings',{method:'PATCH',body:{scheduler_enabled:!s.scheduler_enabled,interval_hours:s.interval_hours}});await refresh(true);toast(!s.scheduler_enabled?'自动签到已启用':'自动签到已暂停');}
document.addEventListener('click',async e=>{const btn=e.target.closest('button');if(!btn)return;try{
 if(btn.dataset.view){state.view=btn.dataset.view;render();}
 else if(btn.dataset.filter){state.filter=btn.dataset.filter;render();}
 else if(btn.dataset.method){state.loginMethod=btn.dataset.method;state.reauth=isBrowser(btn.dataset.method);syncMethod();}
 else if(btn.dataset.action==='select-all-platforms'){state.selectedProviders=eligibleProviders().map(p=>p.id);platformPicker();}
 else if(btn.dataset.action==='cancel-login')closeModal();
 else if(btn.dataset.action==='retry-login'){const q=state.loginQueue;if(queueActive(q)&&q.items[q.index].status==='error')void startQueueItem(q);}
 else if(btn.dataset.action==='skip-login'){const q=state.loginQueue;if(queueActive(q)&&q.items[q.index].status==='error'){q.items[q.index].status='skipped';advanceQueue(q);}}
 else if(btn.dataset.action==='retry-poll'){const q=state.loginQueue;if(queueActive(q)&&q.items[q.index].status==='network')void pollLogin(q);}
 else if(btn.dataset.action==='connect-platform'){const a=state.data.accounts.find(x=>x.id===Number(btn.dataset.id));if(a&&isOAuth(a.login_via))accountModal(null,a);}
 else if(btn.dataset.action==='relogin'){const a=state.data.accounts.find(x=>x.id===Number(btn.dataset.id));if(!a)return;const method=isBrowser(a.login_via)?a.login_via:'manual';launchLogin({provider:a.provider,method,interval_hours:a.interval_hours,name:a.name,account_id:a.id});}
 else if(btn.dataset.action==='add')accountModal();
 else if(btn.dataset.action==='close')closeModal();
 else if(btn.dataset.action==='settings')settingsModal();
 else if(btn.dataset.action==='edit')accountModal(state.data.accounts.find(a=>a.id===Number(btn.dataset.id)));
 else if(btn.dataset.action==='run'){btn.disabled=true;await api(`/api/accounts/${btn.dataset.id}/run`,{method:'POST',body:{}});toast('已加入签到队列，正在连接站点');await refresh(true);}
 else if(btn.dataset.action==='run-all'){if(!state.data.accounts.some(a=>a.enabled))return toast('请先添加并启用一个账号');btn.disabled=true;const r=await api('/api/run-all',{method:'POST',body:{}});toast(r.queued?`已启动 ${r.queued} 个签到任务`:'任务已在执行中');await refresh(true);}
 else if(btn.dataset.action==='toggle-scheduler')await toggleScheduler();
 else if(btn.dataset.action==='modal-switch'){btn.classList.toggle('on');$('input[name="scheduler_enabled"]').value=btn.classList.contains('on');}
 else if(btn.dataset.action==='delete'){const a=state.data.accounts.find(x=>x.id===Number(btn.dataset.id));modal(`<div class="modal-head"><div><h2>删除账号？</h2><p>将从本机删除「${esc(a.name)}」的加密凭据与调度。</p></div></div><div class="notice">${icon('info')}已有运行日志会保留账号名称快照。此操作无法撤销，但不会影响站点上的账号。</div><div class="modal-actions"><button class="button secondary" data-action="close">取消</button><button class="button primary" style="background:#a4564e;border-color:#a4564e" data-action="confirm-delete" data-id="${a.id}">确认删除</button></div>`);}
 else if(btn.dataset.action==='confirm-delete'){await api(`/api/accounts/${btn.dataset.id}`,{method:'DELETE',body:{}});closeModal();toast('账号及其本机凭据已删除');await refresh(true);}
 else if(btn.dataset.action==='export'){const a=document.createElement('a');a.href='/api/logs/export';a.download='codefuel-logs.csv';a.click();toast('已导出当前日志');}
 else if(btn.dataset.action==='log'){const r=state.data.runs.find(x=>x.id===Number(btn.dataset.id));modal(`<div class="modal-head"><div><h2>${esc(r.account_name)}</h2><p>${providerName(r.provider)} · ${timeLabel(r.started_at,true)}</p></div><button class="icon-button" data-action="close">${icon('x')}</button></div><div class="log-detail">status: ${labels[r.status]||r.status}<br>trigger: ${r.trigger}<br>before: ${money(r.before_balance)}<br>after: ${money(r.after_balance)}<br>observed_change: ${r.reward==null?'unverified':money(r.reward)}<br>duration: ${r.duration_ms||0} ms<br><br>${esc(r.message)}</div><div class="modal-actions"><button class="button primary" data-action="close">知道了</button></div>`);}
 }catch(err){toast(err.message||'操作未完成');await refresh(true);}finally{if(btn.isConnected)btn.disabled=false;}});
document.addEventListener('submit',async e=>{e.preventDefault();const submit=e.submitter;if(submit)submit.disabled=true;try{if(e.target.id==='account-form')await saveAccount(e.target);if(e.target.id==='settings-form'){const body=Object.fromEntries(new FormData(e.target));body.scheduler_enabled=body.scheduler_enabled==='true';body.interval_hours=Number(body.interval_hours);await api('/api/settings',{method:'PATCH',body});closeModal();toast('定时设置已保存');await refresh(true);}}catch(err){toast(err.message);if(submit)submit.disabled=false;}});
document.addEventListener('input',e=>{if(e.target.id==='account-search'){state.query=e.target.value;const pos=e.target.selectionStart;render();const input=$('#account-search');input.focus();input.setSelectionRange(pos,pos);}});
document.addEventListener('change',e=>{
  if(e.target.name==='providers'){state.selectedProviders=$$('input[name="providers"]:checked').map(input=>input.value);updateAccountSubmit();}
  if(e.target.name==='provider'&&!state.editing){state.selectedProviders=[e.target.value];updateAccountSubmit();}
});
$('#modal').addEventListener('click',e=>{if(e.target===$('#modal'))closeModal();});
$('#modal').addEventListener('cancel',e=>{e.preventDefault();closeModal();});
window.__dqEnabledAccounts=()=>state.data?state.data.accounts.filter(a=>a.enabled).length:0;
async function refreshLoop(silent=true){await refresh(silent);setTimeout(refreshLoop,5000);}
refreshLoop(false);
