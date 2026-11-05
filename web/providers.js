'use strict';

const siteTemplates = {newapi:'标准 New API 签到',sign_in:'AnyRouter 兼容签到',balance:'仅同步余额'};
const siteDefaults = {newapi:'/api/user/checkin',sign_in:'/api/user/sign_in',balance:''};

function currencyPicker(){
  const units=Object.keys(state.data.amounts||{USD:{}});
  if(!units.includes(state.currency))state.currency='USD';
  return units.length<2?'':`<div class="currency-picker"><label for="currency-picker">额度统计</label><select id="currency-picker">${units.map(unit=>`<option value="${esc(unit)}" ${state.currency===unit?'selected':''}>${currencyLabel(unit)}</option>`).join('')}</select><span>按单位分别统计，积分按站点独立统计</span></div>`;
}

function sitesView(){
  const builtin=providers().filter(p=>!p.custom),custom=providers().filter(p=>p.custom);
  const card=p=>`<article class="site-card" data-site-id="${esc(p.id)}"><div class="site-card-head"><span class="provider-badge ${p.custom?'custom':p.id==='anyrouter'?'any':'agent'}">${esc(p.name.slice(0,1))}</span><div><h3>${esc(p.name)}</h3><p>${esc(p.domain)}</p></div><span class="site-tag">${p.custom?'自定义':'内置'}</span></div><div class="site-facts"><span>${p.custom?siteTemplates[p.template]:p.id==='agentrouter'?'登录领取奖励':'接口签到'}</span><span>${p.account_count||0} 个账号 · ${currencyLabel(p.currency)}</span></div>${p.custom?'<p class="field-help">使用兼容模板；实际登录与签到能力以本站响应为准。</p>':'<p class="field-help">内置适配由应用维护，账号可独立配置。</p>'}<div class="site-actions"><button class="button secondary small" data-action="site-account" data-site="${esc(p.id)}">${icon('plus')}添加账号</button>${p.custom?`<button class="panel-link" data-action="edit-site" data-site="${esc(p.id)}">编辑配置</button><button class="panel-link danger" data-action="remove-site" data-site="${esc(p.id)}">移除</button>`:'<span class="site-lock">内置配置只读</span>'}</div></article>`;
  return `<section class="site-section"><div class="panel-head"><div><div class="panel-title">内置站点</div><div class="panel-sub">AnyRouter 与 AgentRouter</div></div><span class="site-section-count">2 个</span></div><div class="site-grid">${builtin.map(card).join('')}</div></section><section class="site-section"><div class="panel-head"><div><div class="panel-title">自定义站点</div><div class="panel-sub">配置一次，即可关联多个独立账号</div></div><button class="button primary small" data-action="add-site">${icon('plus')}添加自定义站点</button></div>${custom.length?`<div class="site-grid">${custom.map(card).join('')}</div>`:`<div class="site-empty"><span class="empty-illustration">${icon('layers')}</span><h3>连接你常用的其他站点</h3><p>填写站点网址，选择签到模板，再添加账号。<br>支持兼容 New API 的 Session 登录与额度响应。</p><button class="button secondary" data-action="add-site">${icon('plus')}添加第一个站点</button></div>`}</section><div class="notice">${icon('info')}转盘、任务奖励和非 New API 登录流程需要专门适配。添加网址不代表这些功能会自动兼容。</div>`;
}

function providerModal(site=null){
  const p=site||{name:'',domain:'',template:'newapi',login_path:'/login',sign_in_path:'/api/user/checkin',user_info_path:'/api/user/self',api_user_key:'new-api-user',currency:'USD',quota_per_unit:500000};
  modal(`<div class="modal-head"><div><h2>${site?'编辑站点配置':'添加自定义站点'}</h2><p>选择兼容模板，保存后即可添加账号。</p></div><button class="icon-button" data-action="close" aria-label="关闭">${icon('x')}</button></div><form id="provider-form" data-id="${esc(site?.id||'')}" novalidate>
    <div class="form-errors-summary" role="alert" hidden></div>
    <div class="field"><label for="site-name">站点名称</label><input id="site-name" name="name" required maxlength="40" value="${esc(p.name)}" placeholder="例如：我的 API 站点"></div>
    <div class="field"><label for="site-domain">站点网址</label><input id="site-domain" name="domain" type="url" required maxlength="250" value="${esc(p.domain)}" placeholder="https://api.example.com" ${p.account_count?'readonly':''}><p class="field-help">${p.account_count?'已关联账号，网址不可修改。更换网站请新增站点并重新授权。':'填写 HTTPS 根网址，不含 /v1、登录路径或查询参数。'}</p></div>
    <div class="field"><label for="site-template">签到模式</label><select id="site-template" name="template">${Object.entries(siteTemplates).map(([value,label])=>`<option value="${value}" ${p.template===value?'selected':''}>${label}</option>`).join('')}</select><p class="field-help" id="site-mode-help"></p></div>
    <details class="site-advanced"><summary>高级配置 <span>接口路径、额度单位与换算</span></summary><div class="site-advanced-body">
      <div class="field"><label for="site-login">登录页面路径</label><input id="site-login" name="login_path" required maxlength="200" value="${esc(p.login_path)}"></div>
      <div class="field" id="site-sign-field"><label for="site-sign">签到接口路径（POST）</label><input id="site-sign" name="sign_in_path" required maxlength="200" value="${esc(p.sign_in_path||'')}"></div>
      <div class="field"><label for="site-info">余额接口路径（GET）</label><input id="site-info" name="user_info_path" required maxlength="200" value="${esc(p.user_info_path)}"><p class="field-help">响应须包含 success: true 与 data.quota、data.used_quota；登录 Cookie 名为 session。</p></div>
      <div class="field"><label for="site-header">用户标识请求头</label><input id="site-header" name="api_user_key" required maxlength="64" value="${esc(p.api_user_key)}"></div>
      <div class="form-grid"><div class="field"><label for="site-currency">额度单位</label><select id="site-currency" name="currency" ${p.units_locked?'disabled':''}>${[['USD','美元额度（USD）'],['CNY','人民币额度（CNY）'],['credits','本站积分']].map(([v,label])=>`<option value="${v}" ${p.currency===v?'selected':''}>${label}</option>`).join('')}</select></div><div class="field"><label for="site-quota">每单位对应的 quota</label><input id="site-quota" name="quota_per_unit" type="number" min="1" max="1000000000000" step="any" required value="${p.quota_per_unit}" ${p.units_locked?'readonly':''}></div></div>
      <p class="field-help">${p.units_locked?'已有账号或历史记录，额度单位与换算已锁定。':'显示额度 = quota ÷ 换算值。New API 通常为 500000；积分原值可填 1。不同站点积分不合并。'}</p>
    </div></details>
    <div class="site-probe"><button type="button" class="button secondary small" data-action="probe-site">${icon('refresh')}检测公开配置</button><p class="field-help">使用直连网络，只读取公开配置，不登录、不签到。</p><p id="site-probe-result" role="status" hidden></p></div>
    <div class="notice">${icon('shield')}此处只保存站点配置。账号与登录凭据在「添加账号」中单独授权、加密保存。</div>
    <div class="modal-actions"><button type="button" class="button secondary" data-action="close">取消</button><button type="submit" class="button primary">${site?'保存配置':'保存站点'}</button></div>
  </form>`);
  syncSiteMode(false);
}

function syncSiteMode(reset=true){
  const form=$('#provider-form');if(!form)return;
  const mode=form.elements.template.value,sign=form.elements.sign_in_path;
  if(reset)sign.value=siteDefaults[mode];
  sign.disabled=mode==='balance';sign.required=mode!=='balance';
  $('#site-sign-field').hidden=mode==='balance';
  $('#site-mode-help').textContent=mode==='balance'?'只查询余额，结果记为「已同步」，不会自动登录领取奖励。':mode==='newapi'?'默认调用 /api/user/checkin，须由站点明确返回签到成功。':'默认调用 /api/user/sign_in，可在高级配置中调整路径。';
}

function validateProviderForm(form,focus=true){
  form.dataset.validationAttempted='true';
  $$('.field-error',form).forEach(el=>el.remove());
  $$('.field-invalid',form).forEach(el=>el.classList.remove('field-invalid'));
  let first=null;
  for(const input of $$('input,select',form)){
    input.setCustomValidity('');input.removeAttribute('aria-invalid');input.removeAttribute('aria-describedby');
    if(!input.willValidate)continue;
    const value=input.value.trim();
    if(input.required&&!value)input.setCustomValidity('请填写此项');
    if(value&&input.name==='domain'){
      try{const u=new URL(value);if(u.protocol!=='https:'||u.username||u.password||u.search||u.hash||!['','/'].includes(u.pathname)||/[\s\\]/.test(value))throw new Error();}
      catch{input.setCustomValidity('请填写 HTTPS 根网址，不含账号、路径或参数');}
    }
    if(value&&input.name.endsWith('_path')&&!/^\/(?:[a-zA-Z0-9_-]+\/)*[a-zA-Z0-9_-]*$/.test(value))input.setCustomValidity('请输入站内路径，如 /api/user/checkin，不含参数');
    if(value&&input.name==='api_user_key'&&!/^[a-z][a-z0-9-]*$/i.test(value))input.setCustomValidity('请输入有效的用户标识请求头名称');
    if(!input.checkValidity()){
      first=first||input;input.setAttribute('aria-invalid','true');
      const field=input.closest('.field');field.classList.add('field-invalid');
      const help=document.createElement('p');help.className='field-error';help.id=`error-${input.id}`;help.textContent=input.validationMessage;field.appendChild(help);input.setAttribute('aria-describedby',help.id);
    }
  }
  const summary=$('.form-errors-summary',form);summary.hidden=!first;summary.textContent=first?'请检查标红的站点配置。':'';
  if(first&&focus){const details=first.closest('details');if(details)details.open=true;first.scrollIntoView({block:'center'});first.focus({preventScroll:true});}
  return !first;
}

function sitePayload(form){
  const data=Object.fromEntries(new FormData(form));
  data.currency=form.elements.currency.value;
  data.quota_per_unit=Number(data.quota_per_unit);
  data.sign_in_path=data.template==='balance'?null:data.sign_in_path;
  return data;
}

async function saveProvider(form){
  const submit=$('[type="submit"]',form);
  try{
    if(!validateProviderForm(form))return;
    const id=form.dataset.id;
    await api(id?`/api/providers/${encodeURIComponent(id)}`:'/api/providers',{method:id?'PUT':'POST',body:sitePayload(form)});
    closeModal();state.view='sites';await refresh(true);toast(id?'站点配置已保存':'站点已添加，可以连接账号了');
  }catch(error){const summary=$('.form-errors-summary',form);summary.hidden=false;summary.textContent=error.message;}
  finally{submit.disabled=false;}
}

document.addEventListener('click',async event=>{
  const button=event.target.closest('button[data-action]');if(!button)return;
  const action=button.dataset.action;
  if(!['add-site','edit-site','site-account','remove-site','confirm-remove-site','probe-site'].includes(action))return;
  const site=providers().find(p=>p.id===button.dataset.site);
  try{
    if(action==='add-site')providerModal();
    if(action==='edit-site'&&site)providerModal(site);
    if(action==='site-account'&&site){accountModal();state.loginMethod=site.custom?'manual':'github';state.selectedProviders=[site.id];syncMethod();}
    if(action==='remove-site'&&site){
      if(site.account_count)return toast('请先移除该站点下的账号，再移除站点。');
      modal(`<div class="modal-head"><div><h2>移除自定义站点？</h2><p>「${esc(site.name)}」将从可选站点中移除，历史运行日志保留。</p></div></div><div class="modal-actions"><button class="button secondary" data-action="close">取消</button><button class="button primary" data-action="confirm-remove-site" data-site="${esc(site.id)}">确认移除</button></div>`);
    }
    if(action==='confirm-remove-site'&&site){button.disabled=true;await api(`/api/providers/${encodeURIComponent(site.id)}`,{method:'DELETE',body:{}});closeModal();await refresh(true);toast('站点已移除，历史记录保留');}
    if(action==='probe-site'){
      const form=$('#provider-form');if(!validateProviderForm(form))return;
      button.disabled=true;const result=$('#site-probe-result');result.hidden=false;result.textContent='正在读取公开配置…';
      const payload=sitePayload(form),fingerprint=JSON.stringify(payload);
      const reply=await api('/api/providers/probe',{method:'POST',body:payload});
      if(form.isConnected)result.textContent=JSON.stringify(sitePayload(form))===fingerprint?reply.message:'配置已改变，请重新检测。';
    }
  }catch(error){toast(error.message);}
  finally{if(button.isConnected)button.disabled=false;}
});

document.addEventListener('change',event=>{if(event.target.id==='site-template')syncSiteMode();});
for(const type of ['input','change'])document.addEventListener(type,event=>{
  const form=event.target.closest('#provider-form');if(!form)return;
  if(form.dataset.validationAttempted)validateProviderForm(form,false);
  $('#site-probe-result').hidden=true;
});
