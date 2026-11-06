"""User-authorized acceptance of ordinary site terms/privacy checkboxes, scoped to the target site."""
import json

from playwright.async_api import Error as PlaywrightError

AGREEMENT_ACTION = r'''origin => {
  if (window.top !== window || location.origin !== new URL(origin).origin) return 0;
  const accepted = window.__codefuelAcceptedAgreements ||= new Set();
  const visible = el => {
    if (!el) return false;
    const r=el.getBoundingClientRect(),s=getComputedStyle(el);
    return r.width>3 && r.height>3 && s.display!=='none' && s.visibility!=='hidden' && Number(s.opacity)>0;
  };
  const terms = /用户协议|隐私政策|隐私协议|服务条款|服务协议|使用条款|签到协议|terms(?:\s+of\s+(?:service|use))?|privacy(?:\s+policy)?/i;
  const agreement = /(?:我|本人)?(?:已)?阅读.{0,15}同意|(?:我|本人)\s*同意|I\s+(?:have\s+)?(?:read.{0,40}(?:agree|accept)|agree|accept)/i;
  const excluded = /验证码|人机|机器人|真人|captcha|turnstile|verify|robot|支付|付款|扣款|续费|订阅|购买|充值|营销|推送|转账|应用授权|授权访问|权限|payment|billing|subscribe|marketing|permission|authori[sz]/i;
  let count=0;
  const elements=[...document.querySelectorAll('[role="checkbox"]'),...document.querySelectorAll('input[type="checkbox"]')];
  for (const el of elements) {
    if (el.checked || el.getAttribute('aria-checked')==='true' || el.getAttribute('data-state')==='checked'
        || el.disabled || el.matches(':disabled') || el.getAttribute('aria-disabled')==='true'
        || el.closest('[data-sitekey],.cf-turnstile,.nc-container,[role="slider"]')) continue;
    const labels=[...(el.labels||[])];
    if(el.id)for(const label of document.querySelectorAll('label'))if(label.htmlFor===el.id&&!labels.includes(label))labels.push(label);
    const labelled=(el.getAttribute('aria-labelledby')||'').split(/\s+/).map(id=>document.getElementById(id)).filter(Boolean);
    let text=[el.getAttribute('aria-label')||'',...labelled.map(n=>n.innerText||n.textContent||''),...labels.map(n=>n.innerText||n.textContent||'')].join(' ').trim();
    if(!text){
      const label=el.closest('label');
      if(label)text=label.innerText;
      else if(el.parentElement?.querySelectorAll('[role="checkbox"],input[type="checkbox"]').length===1)text=el.parentElement.innerText;
    }
    text=(text||'').replace(/\s+/g,' ').trim();
    const target=visible(el)?el:labels.find(visible);
    if(!target || !text || text.length>400 || !terms.test(text) || !agreement.test(text) || excluded.test(text))continue;
    const key=location.pathname+'|'+text;
    if(accepted.has(key))continue;
    accepted.add(key);target.click();count++;
  }
  return count;
}'''


async def accept_site_agreements(page, domain):
    """One-shot operation used before OAuth entry; never submit a form or touch a CAPTCHA."""
    try:
        return await page.evaluate(AGREEMENT_ACTION, domain)
    except (PlaywrightError, AttributeError):
        return 0


async def setup_agreement_guard(context, domain):
    if not hasattr(context, 'add_init_script'):
        return
    script = r'''(() => {
      const origin=ORIGIN;
      if(window.top!==window||location.origin!==new URL(origin).origin)return;
      const accept=ACTION;
      let timer;
      const run=()=>{timer=null;accept(origin)};
      const start=()=>{
        new MutationObserver(()=>{if(!timer)timer=setTimeout(run,100)}).observe(document.documentElement,
          {childList:true,subtree:true,characterData:true,attributes:true,attributeFilter:['checked','aria-checked','disabled','aria-disabled','class','style']});
        run();
      };
      if(document.readyState==='loading')document.addEventListener('DOMContentLoaded',start,{once:true});else start();
    })();'''
    await context.add_init_script(script.replace('ORIGIN', json.dumps(domain)).replace('ACTION', AGREEMENT_ACTION))
