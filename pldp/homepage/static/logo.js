/* The stationary masks and wordmark are prepared at build time. The page
   paints the rainbow logo directly; no source-image download or pixel loop. */
(()=>{
 const host=document.querySelector('.brand-art');
 if(!host)return;
 const control=host.querySelector('.logo-motion');
 if(!control)return;
 control.onclick=()=>{const paused=host.classList.toggle('motion-paused');control.textContent=paused?'播放动画':'暂停动画';control.setAttribute('aria-label',paused?'播放彩虹动画':'暂停彩虹动画');control.setAttribute('aria-pressed',String(paused));};
})();
