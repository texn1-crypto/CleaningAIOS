const $=(selector,root=document)=>root.querySelector(selector);
const $$=(selector,root=document)=>[...root.querySelectorAll(selector)];
const esc=value=>String(value??'').replace(/[&<>"']/g,char=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[char]));

$('[data-year]').textContent=new Date().getFullYear();
const header=$('[data-header]');
addEventListener('scroll',()=>header.classList.toggle('scrolled',scrollY>18),{passive:true});
const menu=$('[data-menu-toggle]');
const nav=$('[data-nav]');
menu.addEventListener('click',()=>{const open=nav.classList.toggle('open');menu.setAttribute('aria-expanded',String(open))});
$$('a',nav).forEach(link=>link.addEventListener('click',()=>{nav.classList.remove('open');menu.setAttribute('aria-expanded','false')}));

if(!matchMedia('(prefers-reduced-motion: reduce)').matches){
  const observer=new IntersectionObserver(entries=>entries.forEach(entry=>{if(entry.isIntersecting){entry.target.classList.add('visible');observer.unobserve(entry.target)}}),{threshold:.12});
  $$('.reveal').forEach(item=>observer.observe(item));
}else{$$('.reveal').forEach(item=>item.classList.add('visible'))}

async function loadSite(){
  try{
    const response=await fetch('/api/public/site');
    if(!response.ok)throw new Error('site data unavailable');
    const data=await response.json();
    $$('[data-company-name]').forEach(node=>node.textContent=data.company.name||'CleaningAI');
    $('[data-service-area]').textContent=data.company.service_area||'Москва и Московская область';
    const phone=$('[data-company-phone]');
    if(data.company.phone){phone.hidden=false;phone.textContent=data.company.phone;phone.href='tel:'+data.company.phone.replace(/[^+\d]/g,'')}
    const email=$('[data-company-email]');
    if(data.company.email){email.hidden=false;email.textContent=data.company.email;email.href='mailto:'+data.company.email}
    const news=$('[data-news]');
    if(data.news.length){news.innerHTML=data.news.map(item=>`<article class="news-card"><time datetime="${esc(item.published_at)}">${new Intl.DateTimeFormat('ru-RU',{day:'numeric',month:'long',year:'numeric'}).format(new Date(item.published_at))}</time><h3>${esc(item.title)}</h3><p>${esc(item.body).slice(0,240)}</p></article>`).join('')}
    if(!data.lead_form.enabled){
      const submit=$('.form-submit');submit.disabled=true;submit.textContent='Форма готовится к публикации';
      $('[data-form-status]').textContent='Владелец добавляет юридические данные оператора.';
    }
  }catch(error){console.warn('Public site data could not be loaded')}
}
loadSite();

const form=$('[data-lead-form]');
const statusNode=$('[data-form-status]');
form.addEventListener('submit',async event=>{
  event.preventDefault();statusNode.className='form-status';statusNode.textContent='';
  if(!form.reportValidity())return;
  const values=Object.fromEntries(new FormData(form).entries());
  if(!values.phone&&!values.email){statusNode.classList.add('error');statusNode.textContent='Укажите телефон или email.';return}
  const params=new URLSearchParams(location.search);
  const payload={...values,consent:Boolean(values.consent),object_area:values.object_area?Number(values.object_area):null,budget:null,source:'website',utm_source:params.get('utm_source')||'',utm_medium:params.get('utm_medium')||'',utm_campaign:params.get('utm_campaign')||''};
  const button=$('.form-submit',form);button.disabled=true;button.textContent='Отправляем…';
  try{
    const response=await fetch('/api/public/leads',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(payload)});
    const result=await response.json().catch(()=>({}));
    if(!response.ok)throw new Error(response.status===429?'Слишком много попыток. Попробуйте немного позже.':result.detail||'Не удалось отправить заявку.');
    form.reset();statusNode.classList.add('success');statusNode.textContent=result.status==='qualified'?'Спасибо. Мы отметили заявку как приоритетную и скоро свяжемся.':'Спасибо. Заявка принята, специалист свяжется с вами.';
  }catch(error){statusNode.classList.add('error');statusNode.textContent=error.message}
  finally{button.disabled=false;button.textContent='Отправить заявку'}
});
