"""Dismiss informational site notices, never authentication or verification dialogs."""
import json
from urllib.parse import urlsplit


async def setup_notice_guard(context, domain, *, additional_origins=()):
    url = urlsplit(domain)
    origin = f'{url.scheme}://{url.netloc}'
    origins = [origin, *additional_origins]
    script = r'''(() => {
      if (!ORIGINS.includes(location.origin) || window.top !== window) return;
      const visible = el => el.getClientRects().length && getComputedStyle(el).visibility !== 'hidden';
      const dismiss = () => {
        for (const modal of document.querySelectorAll('[role="dialog"], dialog[open], .semi-modal-content, .d-modal__container')) {
          if (!visible(modal)) continue;
          const text = modal.innerText || '';
          const heading = modal.querySelector('.semi-modal-title, [role="heading"], h1, h2, h3');
          const title = heading?.textContent || '';
          const protectedText = /滑动|人机|验证码|验证|captcha|turnstile|授权|权限|同意|条款|consent|permission|删除|清空|支付|购买|充值|绑定|退出/i;
          if (protectedText.test(title) || modal.querySelector('form, input, textarea, select, iframe, [role="slider"], [data-sitekey], .nc-container, .cf-turnstile')) continue;
          const notice = /公告|通知|欢迎|更新提示|announcement|notice|welcome/i.test(title);
          // A notice may discuss authentication in its prose. That must not
          // turn its explicit close button into a blocked consent control.
          if (!notice && protectedText.test(text)) continue;
          const informational = /^(提示|温馨提示|操作成功|登录成功|信息|Information|Success)$/i.test(title.trim());
          const buttons = [...modal.querySelectorAll('button')].filter(visible);
          const specific = buttons.find(b => /^(关闭公告|Close Notice)$/i.test(b.innerText.trim()))
            || buttons.find(b => /^(今日关闭|Close Today)$/i.test(b.innerText.trim()));
          if (!notice && !informational && !specific) continue;
          const close = specific || buttons.find(b =>
            /^(关闭|知道了|我知道了|稍后|close|dismiss|got it)$/i.test((b.getAttribute('aria-label') || b.innerText).trim())
            || b.classList.contains('semi-modal-close')
            || (informational && /^(确定|好的|OK)$/i.test(b.innerText.trim())));
          if (close && !close.disabled) close.click();
        }
      };
      let timer;
      const start = () => {
        new MutationObserver(() => { if (!timer) timer = setTimeout(() => {timer=null; dismiss();}, 100); })
          .observe(document.documentElement, {childList: true, characterData: true, subtree: true, attributes: true,
            attributeFilter: ['class', 'style', 'hidden', 'open', 'aria-hidden', 'aria-modal', 'role', 'disabled']});
        dismiss();
      };
      if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', start, {once:true});
      else start();
    })();'''
    await context.add_init_script(script.replace('ORIGINS', json.dumps(origins)))
