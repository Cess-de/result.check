(function(){
if(window.CessBox)return;
const st=document.createElement("style");
st.textContent=`#lb{position:fixed;inset:0;z-index:99999;display:none;align-items:center;justify-content:center;background:rgba(5,9,20,.84);-webkit-backdrop-filter:blur(14px);backdrop-filter:blur(14px);direction:ltr}#lb.on{display:flex}
#lb img{max-width:96vw;max-height:86vh;object-fit:contain;border-radius:14px;box-shadow:0 20px 60px rgba(0,0,0,.5);user-select:none;-webkit-user-drag:none}
#lb button{position:absolute;border:1px solid rgba(255,255,255,.3);background:rgba(255,255,255,.14);color:#fff;width:44px;height:44px;border-radius:50%;font-size:20px;cursor:pointer;-webkit-backdrop-filter:blur(10px);backdrop-filter:blur(10px)}
#lb .x{top:calc(14px + env(safe-area-inset-top));right:14px}#lb .p{left:12px;top:50%;margin-top:-22px}#lb .n{right:12px;top:50%;margin-top:-22px}
#lb .c{position:absolute;bottom:calc(16px + env(safe-area-inset-bottom));left:50%;transform:translateX(-50%);color:#fff;font:600 13px Inter,sans-serif;background:rgba(0,0,0,.4);padding:4px 14px;border-radius:999px}`;
document.head.append(st);
const lb=document.createElement("div");lb.id="lb";
lb.innerHTML='<button class="x" aria-label="إغلاق">✕</button><button class="p" aria-label="السابق">‹</button><img alt=""><button class="n" aria-label="التالي">›</button><div class="c"></div>';
document.body.appendChild(lb);
let L=[],i=0;const im=lb.querySelector("img"),cn=lb.querySelector(".c"),P=lb.querySelector(".p"),N=lb.querySelector(".n");
function show(){im.src=L[i];const m=L.length>1;cn.textContent=m?(i+1)+" / "+L.length:"";cn.style.display=P.style.display=N.style.display=m?"":"none"}
function go(d){i=(i+d+L.length)%L.length;show()}
function close(){lb.classList.remove("on");im.src="";document.body.style.overflow=""}
window.CessBox={open(u,k){L=u;i=k||0;show();lb.classList.add("on");document.body.style.overflow="hidden"}};
lb.querySelector(".x").onclick=close;P.onclick=e=>{e.stopPropagation();go(-1)};N.onclick=e=>{e.stopPropagation();go(1)};
lb.onclick=e=>{if(e.target===lb)close()};
addEventListener("keydown",e=>{if(!lb.classList.contains("on"))return;if(e.key==="Escape")close();if(e.key==="ArrowLeft")go(-1);if(e.key==="ArrowRight")go(1)});
let sx=0;lb.addEventListener("touchstart",e=>{sx=e.touches[0].clientX},{passive:true});
lb.addEventListener("touchend",e=>{const d=e.changedTouches[0].clientX-sx;if(Math.abs(d)>50&&L.length>1)go(d<0?1:-1)});
document.addEventListener("click",e=>{const m=e.target.closest("[data-gal] img");if(!m)return;const A=[...m.closest("[data-gal]").querySelectorAll("img")];e.preventDefault();e.stopPropagation();CessBox.open(A.map(x=>x.dataset.full||x.src),A.indexOf(m))},true);
})();
