'use strict';

const siteTemplates = {newapi:'标准 New API 签到',sub2api:'Sub2API 签到',sign_in:'AnyRouter 兼容签到',balance:'仅同步余额'};
const siteDefaults = {newapi:'/api/user/checkin',sub2api:'/api/v1/user/check-in',sign_in:'/api/user/sign_in',balance:''};
const siteProbeStates = new WeakMap();
const visibleSiteMode = template => template==='sub2api_balance'?'balance':template;

function currencyPicker(){
  const units=Object.keys(state.data.amounts||{USD:{}});
  if(!units.includes(state.currency))state.currency='USD';
  return units.length<2?'':`<div class="currency-picker"><label for="currency-picker">额度统计</label><select id="currency-picker">${units.map(unit=>`<option value="${esc(unit)}" ${state.currency===unit?'selected':''}>${currencyLabel(unit)}</option>`).join('')}</select><span>按单位分别统计，积分按站点独立统计</span></div>`;
}

function sitesView(){
  const builtin=providers().filter(p=>!p.custom),custom=providers().filter(p=>p.custom);
  const card=p=>`<article class="site-card" data-site-id="${esc(p.id)}"><div class="site-card-head"><span class="provider-badge ${p.custom?'custom':p.id==='anyrouter'?'any':'agent'}">${esc(p.name.slice(0,1))}</span><div><h3>${esc(p.name)}</h3><p>${esc(p.domain)}</p></div><span class="site-tag">${p.custom?'自定义':'内置'}</span></div><div class="site-facts"><span>${p.custom?siteTemplates[visibleSiteMode(p.template)]:p.id==='agentrouter'?'登录领取奖励':'接口签到'}</span><span>${p.account_count||0} 个账号 · ${currencyLabel(p.currency)}</span></div>${p.custom?'<p class="field-help">使用兼容模板；实际登录与签到能力以本站响应为准。</p>':'<p class="field-help">内置适配由应用维护，账号可独立配置。</p>'}<div class="site-actions"><button class="button secondary small" data-action="site-account" data-site="${esc(p.id)}">${icon('plus')}添加账号</button>${p.custom?`<button class="panel-link" data-action="edit-site" data-site="${esc(p.id)}">编辑配置</button><button class="panel-link danger" data-action="remove-site" data-site="${esc(p.id)}">移除</button>`:'<span class="site-lock">内置配置只读</span>'}</div></article>`;
  return `<section class="site-section"><div class="panel-head"><div><div class="panel-title">内置站点</div><div class="panel-sub">AnyRouter 与 AgentRouter</div></div><span class="site-section-count">2 个</span></div><div class="site-grid">${builtin.map(card).join('')}</div></section><section class="site-section"><div class="panel-head"><div><div class="panel-title">自定义站点</div><div class="panel-sub">配置一次，即可关联多个独立账号</div></div><button class="button primary small" data-action="add-site">${icon('plus')}添加自定义站点</button></div>${custom.length?`<div class="site-grid">${custom.map(card).join('')}</div>`:`<div class="site-empty"><span class="empty-illustration">${icon('layers')}</span><h3>连接你常用的其他站点</h3><p>填写网站地址，自动匹配签到模式，再添加账号。<br>支持 New API 与 Sub2API 的账号登录和额度响应。</p><button class="button secondary" data-action="add-site">${icon('plus')}添加第一个站点</button></div>`}</section><div class="notice">${icon('info')}转盘、任务奖励及其他登录协议需要专门适配。添加网址不代表这些功能会自动兼容。</div>`;
}

function providerModal(site=null){
  cancelSiteProbe($('#provider-form'));
  const p=site||{name:'',domain:'',template:'newapi',login_path:'/login',sign_in_path:'/api/user/checkin',user_info_path:'/api/user/self',api_user_key:'new-api-user',currency:'USD',quota_per_unit:500000,login_api_path:'/api/user/login'};
  modal(`<div class="modal-head"><div><h2>${site?'编辑站点配置':'添加自定义站点'}</h2><p>填写网址后自动识别签到模式，也可手动调整。</p></div><button class="icon-button" data-action="close" aria-label="关闭">${icon('x')}</button></div><form id="provider-form" data-id="${esc(site?.id||'')}" novalidate>
    <div class="form-errors-summary" role="alert" hidden></div>
    <div class="field"><label for="site-name">站点名称</label><input id="site-name" name="name" required maxlength="40" value="${esc(p.name)}" placeholder="例如：我的 API 站点"></div>
    <div class="field"><label for="site-domain">站点网址</label><input id="site-domain" name="domain" type="url" required maxlength="250" value="${esc(p.domain)}" placeholder="https://api.example.com" ${p.account_count?'readonly':''}><p class="field-help">${p.account_count?'已关联账号，网址不可修改。更换网站请新增站点并重新授权。':'填写 HTTPS 根网址，系统会自动识别签到模式。'}</p></div>
    <div class="field"><label for="site-template">签到模式</label><select id="site-template" name="template">${Object.entries(siteTemplates).map(([value,label])=>`<option value="${value}" ${visibleSiteMode(p.template)===value?'selected':''}>${label}</option>`).join('')}</select><p class="field-help" id="site-mode-help"></p></div>
    <details class="site-advanced"><summary>高级配置 <span>接口路径、额度单位与换算</span></summary><div class="site-advanced-body">
      <div class="field"><label for="site-login">登录页面路径</label><input id="site-login" name="login_path" required maxlength="200" value="${esc(p.login_path)}"></div>
      <div class="field"><label for="site-login-api">账号密码登录接口（POST）</label><input id="site-login-api" name="login_api_path" required maxlength="200" value="${esc(p.login_api_path||'/api/user/login')}"></div>
      <div class="field" id="site-sign-field"><label for="site-sign">签到接口路径（POST）</label><input id="site-sign" name="sign_in_path" required maxlength="200" value="${esc(p.sign_in_path||'')}"></div>
      <div class="field"><label for="site-info">余额接口路径（GET）</label><input id="site-info" name="user_info_path" required maxlength="200" value="${esc(p.user_info_path)}"><p class="field-help">New API 使用 session 与 quota 响应；Sub2API 使用 Bearer Token 与 balance 响应。</p></div>
      <div class="field" id="site-user-header-field"><label for="site-header">用户标识请求头</label><input id="site-header" name="api_user_key" required maxlength="64" value="${esc(p.api_user_key)}"></div>
      <div class="form-grid"><div class="field"><label for="site-currency">额度单位</label><select id="site-currency" name="currency" ${p.units_locked?'disabled':''}>${[['USD','美元额度（USD）'],['CNY','人民币额度（CNY）'],['credits','本站积分']].map(([v,label])=>`<option value="${v}" ${p.currency===v?'selected':''}>${label}</option>`).join('')}</select></div><div class="field" id="site-quota-field"><label for="site-quota">每单位对应的 quota</label><input id="site-quota" name="quota_per_unit" type="number" min="1" max="1000000000000" step="any" required value="${p.quota_per_unit}" ${p.units_locked?'readonly':''}></div></div>
      <p class="field-help" id="site-quota-help">${p.units_locked?'已有账号或历史记录，额度单位与换算已锁定。':'显示额度 = quota ÷ 换算值。New API 通常为 500000；积分原值可填 1。不同站点积分不合并。'}</p>
    </div></details>
    <div class="site-probe"><div class="site-probe-heading"><b>自动识别</b><button type="button" class="button secondary small" data-action="probe-site">${icon('refresh')}重新检测</button></div><p class="field-help">读取网站公开信息匹配模式，无需账号密码；识别不成功时可手动选择。</p><p id="site-probe-result" role="status">${site?'现有配置已保留，可重新检测获取建议。':'填写完整网址后开始识别。'}</p><button type="button" id="site-apply-probe" class="panel-link" data-action="apply-site-probe" hidden>应用识别结果</button></div>
    <div class="notice">${icon('shield')}此处只保存站点配置。账号与登录凭据在「添加账号」中单独授权、加密保存。</div>
    <div class="modal-actions"><button type="button" class="button secondary" data-action="close">取消</button><button type="submit" class="button primary">${site?'保存配置':'保存站点'}</button></div>
  </form>`);
  const form=$('#provider-form');
  form.dataset.protocol=String(p.template.startsWith('sub2api'));
  siteProbeStates.set(form,{manual:!!site,sequence:0,domain:null,result:null,pending:null,controller:null,timer:null,disposed:false});
  syncSiteMode(false);
}

function syncSiteMode(reset=true,detectedProtocol=null){
  const form=$('#provider-form');if(!form)return;
  const mode=form.elements.template.value,sign=form.elements.sign_in_path;
  const sub2api=detectedProtocol?detectedProtocol==='sub2api':mode==='balance'?form.dataset.protocol==='true':mode==='sub2api';
  if(reset)sign.value=siteDefaults[mode];
  const balanceOnly=mode==='balance';
  sign.disabled=balanceOnly;sign.required=!balanceOnly;
  if(reset&&form.dataset.protocol!==String(sub2api)){form.elements.user_info_path.value=sub2api?'/api/v1/auth/me':'/api/user/self';form.elements.login_api_path.value=sub2api?'/api/v1/auth/login':'/api/user/login';if(!form.elements.quota_per_unit.readOnly)form.elements.quota_per_unit.value=sub2api?'1':'500000';}
  $('#site-sign-field').hidden=balanceOnly;
  form.dataset.protocol=String(sub2api);
  $('#site-user-header-field').hidden=sub2api;$('#site-quota-field').hidden=sub2api;
  $('#site-quota-help').textContent=sub2api?'余额按站点返回的原值显示，不使用 quota 换算。':form.elements.quota_per_unit.readOnly?'已有账号或历史记录，额度单位与换算已锁定。':'显示额度 = quota ÷ 换算值。New API 通常为 500000；积分原值可填 1。';
  $('#site-mode-help').textContent=balanceOnly?'只查询余额，结果记为「已同步」，不会自动登录领取奖励。':sub2api?'适用于 Sub2API 站点，自动使用 Token 登录，余额按站点返回原值显示。':mode==='newapi'?'默认调用 /api/user/checkin，须由站点明确返回签到成功。':'默认调用 /api/user/sign_in，可在高级配置中调整路径。';
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
  if(data.template==='balance'&&form.dataset.protocol==='true')data.template='sub2api_balance';
  data.sign_in_path=['balance','sub2api_balance'].includes(data.template)?null:data.sign_in_path;
  return data;
}

function siteProbeDomain(form){
  if(!form)return null;
  const raw=form.elements.domain.value.trim();
  try{
    const url=new URL(raw);
    if(url.protocol!=='https:'||url.username||url.password||url.search||url.hash||!['','/'].includes(url.pathname)||/[\s\\]/.test(raw))return null;
    return url.origin;
  }catch{return null;}
}

function cancelSiteProbe(form){
  const probe=siteProbeStates.get(form);if(!probe)return;
  clearTimeout(probe.timer);probe.controller?.abort();probe.disposed=true;probe.sequence++;
}

function scheduleSiteProbe(form,delay){
  const probe=siteProbeStates.get(form);if(!probe||probe.disposed)return;
  clearTimeout(probe.timer);
  const domain=siteProbeDomain(form);
  if(domain===probe.domain&&(probe.pending||probe.result))return;
  probe.controller?.abort();probe.sequence++;probe.pending=null;probe.result=null;probe.domain=null;
  if(!form.dataset.id&&!probe.manual){
    form.elements.template.value='newapi';
    syncSiteMode(true,'newapi');
    for(const [name,value] of Object.entries({login_path:'/login',login_api_path:'/api/user/login',user_info_path:'/api/user/self',api_user_key:'new-api-user',quota_per_unit:500000})){
      form.elements[name].value=value;
    }
  }
  $('#site-apply-probe',form).hidden=true;
  const result=$('#site-probe-result',form);result.dataset.state='';
  result.textContent=domain?(form.dataset.id?'网址已更改，可点击重新检测获取建议。':'准备识别网站…'):'填写完整的 HTTPS 网址后开始识别。';
  $('[data-action="probe-site"]',form).disabled=false;
  if(domain&&!form.dataset.id)probe.timer=setTimeout(()=>void probeSite(form),delay);
}

function applySiteProbe(form,reply){
  if(!reply.recognized||!Object.prototype.hasOwnProperty.call(siteTemplates,reply.template)||!['newapi','sub2api'].includes(reply.protocol))return false;
  const probe=siteProbeStates.get(form);
  form.elements.template.value=reply.template;
  syncSiteMode(true,reply.protocol);
  for(const name of ['login_path','login_api_path','user_info_path','sign_in_path','api_user_key','quota_per_unit']){
    const input=form.elements[name],value=reply.defaults?.[name];
    if(input&&!input.readOnly&&value!=null)input.value=value;
  }
  probe.manual=false;
  const result=$('#site-probe-result',form);result.dataset.state='matched';
  result.textContent=`已自动匹配：${siteTemplates[reply.template]}。${reply.checkin_enabled===false?'站点未开放签到，仅同步余额。':reply.checkin_enabled==null?'签到能力将在登录后验证。':'保存后可继续添加账号。'}`;
  $('#site-apply-probe',form).hidden=true;
  if(form.dataset.validationAttempted)validateProviderForm(form,false);
  return true;
}

async function probeSite(form,force=false){
  const probe=siteProbeStates.get(form),domain=siteProbeDomain(form);
  if(!probe||probe.disposed||!domain){
    if(form&&force){$('#site-probe-result',form).textContent='请先填写有效的 HTTPS 根网址。';form.elements.domain.focus();}
    return null;
  }
  clearTimeout(probe.timer);
  if(probe.domain===domain&&probe.pending)return probe.pending;
  if(!force&&probe.domain===domain&&probe.result)return probe.result;
  probe.controller?.abort();
  const sequence=++probe.sequence,controller=new AbortController();
  probe.controller=controller;probe.domain=domain;probe.result=null;
  const output=$('#site-probe-result',form),button=$('[data-action="probe-site"]',form);
  output.dataset.state='loading';output.textContent='正在自动识别网站，稍后将匹配签到模式…';
  $('#site-apply-probe',form).hidden=true;button.disabled=true;
  const current=()=>form.isConnected&&!probe.disposed&&probe.sequence===sequence&&siteProbeDomain(form)===domain;
  probe.pending=(async()=>{
    try{
      const reply=await api('/api/providers/probe',{method:'POST',body:{domain},signal:controller.signal});
      if(!current())return null;
      probe.result=reply;
      if(!form.dataset.id&&!probe.manual&&applySiteProbe(form,reply))return reply;
      output.dataset.state=reply.recognized?'suggestion':'unknown';
      output.textContent=reply.message+(reply.recognized&&(probe.manual||form.dataset.id)?' 已保留当前配置。':'');
      $('#site-apply-probe',form).hidden=!reply.recognized||!!form.dataset.id||!Object.prototype.hasOwnProperty.call(siteTemplates,reply.template);
      return reply;
    }catch(error){
      if(current()&&error.name!=='AbortError'){
        probe.result={recognized:false,message:'自动识别暂未完成，可重新检测或手动选择模式后保存。'};
        output.dataset.state='unknown';output.textContent=probe.result.message;
      }
      return null;
    }finally{
      if(probe.sequence===sequence){probe.pending=null;probe.controller=null;if(button.isConnected)button.disabled=false;}
    }
  })();
  return probe.pending;
}

async function saveProvider(form){
  const submit=$('[type="submit"]',form);
  try{
    if(!validateProviderForm(form))return;
    const id=form.dataset.id,domain=siteProbeDomain(form),probe=siteProbeStates.get(form);
    if(!id&&!probe?.manual){submit.textContent='正在识别…';await probeSite(form);}
    if(!form.isConnected||probe?.disposed||$('#provider-form')!==form||!$('#modal').open||domain!==siteProbeDomain(form))return;
    if(!validateProviderForm(form))return;
    await api(id?`/api/providers/${encodeURIComponent(id)}`:'/api/providers',{method:id?'PUT':'POST',body:sitePayload(form)});
    if(!probe?.disposed&&$('#provider-form')===form){closeModal();state.view='sites';}
    await refresh(true);toast(id?'站点配置已保存':'站点已添加，可以连接账号了');
  }catch(error){const summary=$('.form-errors-summary',form);summary.hidden=false;summary.textContent=error.message;}
  finally{submit.disabled=false;submit.textContent=form.dataset.id?'保存配置':'保存站点';}
}

document.addEventListener('click',async event=>{
  const button=event.target.closest('button[data-action]');if(!button)return;
  const action=button.dataset.action;
  if(!['add-site','edit-site','site-account','remove-site','confirm-remove-site','probe-site','apply-site-probe'].includes(action))return;
  const site=providers().find(p=>p.id===button.dataset.site);
  try{
    if(action==='add-site')providerModal();
    if(action==='edit-site'&&site)providerModal(site);
    if(action==='site-account'&&site)accountModal(null,null,site.id);
    if(action==='remove-site'&&site){
      if(site.account_count)return toast('请先移除该站点下的账号，再移除站点。');
      modal(`<div class="modal-head"><div><h2>移除自定义站点？</h2><p>「${esc(site.name)}」将从可选站点中移除，历史运行日志保留。</p></div></div><div class="modal-actions"><button class="button secondary" data-action="close">取消</button><button class="button primary" data-action="confirm-remove-site" data-site="${esc(site.id)}">确认移除</button></div>`);
    }
    if(action==='confirm-remove-site'&&site){button.disabled=true;await api(`/api/providers/${encodeURIComponent(site.id)}`,{method:'DELETE',body:{}});closeModal();await refresh(true);toast('站点已移除，历史记录保留');}
    if(action==='probe-site')await probeSite($('#provider-form'),true);
    if(action==='apply-site-probe'){const form=$('#provider-form'),probe=siteProbeStates.get(form);if(probe?.result&&probe.domain===siteProbeDomain(form))applySiteProbe(form,probe.result);}
  }catch(error){toast(error.message);}
  finally{if(button.isConnected)button.disabled=false;}
});

document.addEventListener('change',event=>{if(event.target.id==='site-template')syncSiteMode();});
for(const type of ['input','change'])document.addEventListener(type,event=>{
  const form=event.target.closest('#provider-form');if(!form)return;
  if(form.dataset.validationAttempted)validateProviderForm(form,false);
  const probe=siteProbeStates.get(form);if(!probe)return;
  if(event.target.name==='domain'){scheduleSiteProbe(form,type==='change'?0:650);}
  else if(event.target.name&&event.target.name!=='name'){
    probe.manual=true;
    const output=$('#site-probe-result',form);
    if(probe.result){
      output.dataset.state='suggestion';
      output.textContent=probe.result.recognized?`${probe.result.message} 已保留你的手动配置。`:'已使用手动配置，可直接保存或重新检测。';
      $('#site-apply-probe',form).hidden=!probe.result.recognized||!!form.dataset.id;
    }else if(probe.pending){output.textContent='正在识别网站，完成后仅提供建议；你的手动配置会保留。';}
  }
});
