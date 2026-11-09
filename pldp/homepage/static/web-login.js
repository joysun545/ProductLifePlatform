/* Browser-bound one-use QR approval. No user IDs or roles are supplied by JS. */
(()=>{
const entry=document.getElementById('webLogin'),box=document.getElementById('loginQR'),help=document.getElementById('loginHelp'),status=document.getElementById('loginStatus'),qr=document.getElementById('loginQRImage');
if(!entry)return;
const base='/pldp/web/portal/api/';let timer=null,generation=0,busy=false;
function token(){return document.cookie.split('; ').find(v=>v.startsWith('csrftoken='))?.slice(10)||'';}
async function call(name,method='POST'){
 const r=await fetch(base+name+'/',{method,credentials:'same-origin',cache:'no-store',headers:method==='POST'?{'X-CSRFToken':token()}:{} });
 let data;try{data=await r.json();}catch{throw Error('登录接口暂不可用，请稍后重试。');}
 if(!r.ok||data.code!==0)throw Error(data.msg||'登录请求失败。');return data.data;
}
function stop(){generation++;clearTimeout(timer);timer=null;}
async function poll(g){
 if(g!==generation)return;
 try{const d=await call('status');if(g!==generation)return;if(d.status==='ready'){stop();location.assign('/pldp/web/portal/');return;}timer=setTimeout(()=>poll(g),1800);}
 catch(e){if(g===generation){stop();qr.hidden=true;status.textContent=e.message+' 点击刷新二维码重新获取。';}}
}
async function open(){
 if(busy)return;busy=true;entry.disabled=true;stop();const g=generation;
 document.getElementById('home').scrollIntoView({behavior:'smooth',block:'start'});box.hidden=help.hidden=false;qr.hidden=true;status.textContent='正在生成登录二维码…';
 try{const session=await call('session','GET');if(g!==generation)return;if(session.logged_in){location.assign('/pldp/web/portal/');return;}
 const d=await call('start');if(g!==generation)return;if(d.status==='ready'){location.assign('/pldp/web/portal/');return;}qr.src=d.qr_image;qr.hidden=false;status.textContent='请用小程序扫一扫，并在手机上确认授权 · 5分钟有效';timer=setTimeout(()=>poll(g),1800);}
 catch(e){if(g===generation)status.textContent=e.message;}
 finally{busy=false;entry.disabled=false;}
}
entry.onclick=open;document.getElementById('refreshLogin').onclick=open;
document.getElementById('cancelLogin').onclick=async()=>{stop();box.hidden=help.hidden=true;qr.removeAttribute('src');try{await call('cancel');}catch{}};
window.addEventListener('pagehide',stop);
window.addEventListener('load',async()=>{
 if(new URLSearchParams(location.search).get('login')==='1'){await open();return;}
 try{const d=await call('session','GET');if(d.logged_in)entry.textContent='进入工作台';}catch{}
});
})();
