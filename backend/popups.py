"""Dismiss informational site notices, never authentication or verification dialogs."""
import json
from urllib.parse import urlsplit


async def setup_notice_guard(context, domain):
    url = urlsplit(domain)
    origin = f'{url.scheme}://{url.netloc}'
    script = r'''(() => {
      if (location.origin !== ORIGIN || window.top !== window) return;
      const visible = el => el.getClientRects().length && getComputedStyle(el).visibility !== 'hidden';
      const dismiss = () => {
        for (const modal of document.querySelectorAll('[role="dialog"], .semi-modal-content')) {
          if (!visible(modal)) continue;
          const text = modal.innerText || '';
          if (/滑动|人机|验证码|验证|captcha|turnstile|授权|权限|同意|条款|consent|permission/i.test(text)
              || modal.querySelector('form, input, iframe, [role="slider"]')) continue;
          const heading = modal.querySelector('.semi-modal-title, [role="heading"], h1, h2, h3');
          const notice = /公告|通知|欢迎|更新提示|announcement|notice|welcome/i.test(heading?.textContent || '');
          const buttons = [...modal.querySelectorAll('button')].filter(visible);
          const specific = buttons.find(b => /^(关闭公告|今日关闭|Close Notice|Close Today)$/i.test(b.innerText.trim()));
          if (!notice && !specific) continue;
          const close = specific || buttons.find(b =>
            /^(关闭|知道了|我知道了|稍后|close|dismiss|got it)$/i.test((b.getAttribute('aria-label') || b.innerText).trim())
            || b.classList.contains('semi-modal-close'));
          if (close && !close.disabled) close.click();
        }
      };
      let timer;
      const start = () => {
        new MutationObserver(() => { clearTimeout(timer); timer = setTimeout(dismiss, 100); })
          .observe(document.documentElement, {childList: true, subtree: true});
        dismiss();
      };
      if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', start, {once:true});
      else start();
    })();'''
    await context.add_init_script(script.replace('ORIGIN', json.dumps(origin)))
