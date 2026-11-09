// Each navigation target fills the area below the persistent header.
if ('scrollRestoration' in history) history.scrollRestoration='manual';
function focusScreen(id, smooth=true){const target=document.getElementById(id);if(!target)return;target.scrollIntoView({behavior:smooth?'smooth':'instant',block:'start'});document.querySelectorAll('header nav a').forEach(a=>a.setAttribute('aria-current',a.hash==='#'+id?'location':'false'));}
document.querySelectorAll('a[href^="#"]').forEach(a=>a.addEventListener('click',e=>{const id=a.hash.slice(1)||'home';if(document.getElementById(id)){e.preventDefault();history.replaceState(null,'','#'+id);focusScreen(id);}}));
window.addEventListener('load',()=>{history.replaceState(null,'',location.pathname+location.search);focusScreen('home',false);});
