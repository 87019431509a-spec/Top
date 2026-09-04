/* Shared UI behavior. This prototype uses only the device-local fixture adapter. */
'use strict';
const $=(q,e=document)=>e.querySelector(q), $$=(q,e=document)=>[...e.querySelectorAll(q)];
const D=window.PazlixDemo, page=document.body.dataset.page;
const esc=v=>String(v??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const icon=n=>`<svg class="icon" aria-hidden="true"><use href="icons.svg#${n}"></use></svg>`;
const tile=(n,c='primary')=>`<span class="icon-wrap icon-${c}">${icon(n)}</span>`;
const btn=(text,action,style='',attrs='')=>`<button type="button" class="btn ${style}" data-action="${action}" ${attrs}>${text}</button>`;
const link=(text,href,style='')=>`<a class="btn ${style}" href="${esc(href)}">${text}</a>`;
const field=(label,name,value='',type='text',attrs='')=>`<label class="field"><span>${label}</span>${type==='textarea'?`<textarea name="${name}" ${attrs}>${esc(value)}</textarea>`:`<input name="${name}" type="${type}" value="${esc(value)}" ${attrs}>`}</label>`;
const select=(label,name,rows,value='',attrs='')=>`<label class="field"><span>${label}</span><select name="${name}" ${attrs}>${rows.map(([k,v])=>`<option value="${esc(k)}" ${String(k)===String(value)?'selected':''}>${esc(v)}</option>`).join('')}</select></label>`;
const money=v=>new Intl.NumberFormat('ru-RU').format(v)+' ₽';
const dateLabel=(key,opts={day:'numeric',month:'long'})=>new Intl.DateTimeFormat('ru-RU',opts).format(new Date(key+'T12:00:00'));
const duration=n=>`${Math.floor(n/60)?Math.floor(n/60)+' ч ':''}${n%60?n%60+' мин':''}`.trim();
const service=b=>D.state.services.find(s=>s.id===b.serviceId)||{name:'Услуга',duration:90,price:0};
const master=b=>D.state.masters.find(m=>m.id===b.masterId)||{name:'Мастер'};
const initials=n=>n.trim().split(/\s+/).slice(0,2).map(s=>s[0]).join('');
const statuses={pending:['Ожидает решения','orange'],confirmed:['Подтверждена','green'],completed:['Завершена','green'],rejected:['Отклонена','red'],cancelled:['Отменена','gray']};
const badge=b=>`<span class="badge ${statuses[b.status]?.[1]||'gray'}">${statuses[b.status]?.[0]||esc(b.status)}</span>`;
const empty=(title,text='',action='')=>`<div class="empty-state">${tile('calendar')}<h3>${title}</h3><p>${text}</p>${action}</div>`;
function toast(text){const t=$('#toast');t.textContent=text;t.classList.add('show');clearTimeout(window.__toast);window.__toast=setTimeout(()=>t.classList.remove('show'),3500);}
document.addEventListener('storage-error',()=>toast('Не удалось сохранить. Освободите место в браузере и повторите.'));
function commit(fn,message){if(!D.mutate(fn))return false;render();if(message)toast(message);return true;}
function eventLog(text){D.state.events.unshift({text,at:new Date().toISOString()});D.state.events=D.state.events.slice(0,30);}
function error(form,message){let node=$('.form-error',form);if(!node){node=document.createElement('p');node.className='form-error';node.setAttribute('role','alert');form.prepend(node);}node.textContent=message;node.scrollIntoView({block:'nearest'});}
function download(name,body,type='application/json'){const url=URL.createObjectURL(new Blob([body],{type})),a=document.createElement('a');a.href=url;a.download=name;a.click();setTimeout(()=>URL.revokeObjectURL(url),1500);}
let selected=new URLSearchParams(location.search).get('date')||'2026-09-24';
if(!/^\d{4}-\d{2}-\d{2}$/.test(selected)||isNaN(new Date(selected+'T12:00:00')))selected=D.today();
let current=new Date(selected+'T12:00:00');current.setDate(1);
let requestTab='pending',historyTab='month',requestFilter={date:'',serviceId:''},rating=5;
let sheetContext={},returnFocus=null;
const sheet=$('#actionSheet'),sheetContent=$('#sheetContent');
function closeSheet(){if(sheet.open)sheet.close();}
sheet.addEventListener('close',()=>{sheet.classList.remove('menu-sheet');if(returnFocus?.isConnected)returnFocus.focus();else{const heading=$('h1');heading?.setAttribute('tabindex','-1');heading?.focus({preventScroll:true});}});
sheet.addEventListener('click',e=>{if(e.target!==sheet)return;const r=sheet.getBoundingClientRect();if(e.clientX<r.left||e.clientX>r.right||e.clientY<r.top||e.clientY>r.bottom)closeSheet();});
function showSheet(title,sub,content,form='',submit='Сохранить',danger=false){
  if(!sheet.open)returnFocus=document.activeElement;
  sheetContent.innerHTML=`<h2 id="sheetTitle" class="sheet-title">${title}</h2>${sub?`<p class="sheet-sub">${sub}</p>`:''}`+(form?`<form data-form="${form}" class="sheet-form">${content}<p class="form-error" role="alert"></p><div class="sheet-actions">${btn('Отмена','close-sheet')}<button class="btn ${danger?'danger-solid':'primary'}" type="submit">${submit}</button></div></form>`:content);
  if(!sheet.open)sheet.showModal();
  renderGallery();
  const focus=$('input:not([type=checkbox]):not([type=radio]),textarea,select',sheetContent)||$('.sheet-top button',sheet);
  focus?.focus({preventScroll:true});
}
function activeForDate(k){return D.state.bookings.filter(b=>D.active(b)&&b.date===k).sort((a,b)=>a.time.localeCompare(b.time));}
function calendarForDate(k){return D.state.bookings.filter(b=>(D.active(b)||b.status==='completed')&&b.date===k).sort((a,b)=>a.time.localeCompare(b.time));}
function canComplete(b){return b.status==='confirmed'&&new Date(b.date+'T'+b.time).getTime()+b.duration*60000<=Date.now();}
function clientCanChange(b){return D.active(b)&&new Date(b.date+'T'+b.time).getTime()-Date.now()>=Number(D.state.rules.cancelHours||0)*3600000;}
function record(b,{client=false,compact=false}={}){
  const s=service(b),rid=`data-id="${esc(b.id)}"`;
  let actions='';
  if(client){
    if(D.active(b))actions=clientCanChange(b)?btn('Перенести','reschedule','',rid+' data-client="true"')+btn('Отменить','cancel','danger',rid):'<p class="field-help">Для изменения этого визита свяжитесь с мастером.</p>';
    if(b.status==='completed')actions=D.state.reviews.some(r=>r.bookingId===b.id)?'<span class="badge green">Отзыв опубликован</span>':link('Оставить отзыв','client-review.html?id='+b.id,'soft');
  }else{
    if(b.status==='pending'&&!compact)actions=btn(icon('check')+'Подтвердить','confirm','green',rid)+btn('Отклонить','reject','danger',rid);
    if(b.status==='completed'&&!D.state.reviews.some(r=>r.bookingId===b.id))actions=btn('Ссылка на отзыв','review-link','soft',rid);
    actions+=link(compact?'Открыть запись':'Детали записи','request-detail.html?id='+b.id,'text-btn detail-link');
  }
  return `<article class="record"><div class="record-top">${badge(b)}<small>${dateLabel(b.date)}</small></div><div class="record-person"><div class="avatar">${esc(initials(b.name))}</div><div><h3>${esc(client?s.name:b.name)}</h3><p>${esc(client?master(b).name:b.phone)}</p></div></div><div class="record-meta">${!client?`<div>${icon('bag')}<span>${esc(s.name)}</span></div>`:''}<div>${icon('clock')}<span>${b.time} · ${duration(b.duration)} · ${money(b.price)}</span></div></div>${b.comment&&!compact?`<p class="record-comment">${esc(b.comment)}</p>`:''}${actions?`<div class="record-actions">${actions}</div>`:''}</article>`;
}
function renderCalendar(){
  const calendar=$('#monthCalendar');if(!calendar)return;
  const y=current.getFullYear(),m=current.getMonth(),monthPrefix=`${y}-${String(m+1).padStart(2,'0')}`;
  $('#monthTitle').textContent=dateLabel(D.dateKey(current),{month:'long',year:'numeric'}).replace(' г.','').replace(/^./,c=>c.toUpperCase());
  const count=D.state.bookings.filter(b=>(D.active(b)||b.status==='completed')&&b.date.startsWith(monthPrefix)).length;
  $('#monthSummary').textContent=`${count} ${plural(count,'запись','записи','записей')}`;
  const offset=(current.getDay()+6)%7,days=new Date(y,m+1,0).getDate(),cells=Math.ceil((days+offset)/7)*7;
  calendar.innerHTML=Array.from({length:cells},(_,i)=>{
    const d=new Date(y,m,i-offset+1),k=D.dateKey(d),booked=calendarForDate(k).length,blocks=D.state.blocks.filter(b=>b.date===k),closed=!D.dayAvailability(k);
    const markers=(booked?'<i class="booked"></i>':'')+(!closed?'<i class="free"></i>':'')+(blocks.length||closed?'<i class="closed"></i>':'');
    const state=[booked?`${booked} ${plural(booked,'запись','записи','записей')}`:'',closed?'закрыто':'есть свободное время',blocks.length&&!closed?'часть времени закрыта':''].filter(Boolean).join(', ');
    return `<button class="day-cell${d.getMonth()!==m?' other':''}${k===selected?' selected':''}${k===D.today()?' today':''}" data-action="select-day" data-date="${k}" aria-pressed="${k===selected}" aria-label="${dateLabel(k,{day:'numeric',month:'long',year:'numeric'})}, ${state}"><span class="num">${d.getDate()}</span><span class="day-markers" aria-hidden="true">${markers}</span></button>`;
  }).join('');
  $('#selectedDateTitle').textContent=dateLabel(selected,{weekday:'long',day:'numeric',month:'long'}).replace(/^./,c=>c.toUpperCase());
  const rows=calendarForDate(selected),blocks=D.state.blocks.filter(b=>b.date===selected);
  $('#selectedDaySummary').textContent=rows.length?`${rows.length} ${plural(rows.length,'запись','записи','записей')} · ${money(rows.reduce((s,b)=>s+b.price,0))}`:blocks.length?'Есть закрытое время':'Записей пока нет';
  $('.day-head-actions').innerHTML=btn(icon('plus')+'Запись','booking','primary')+btn(icon('lock')+'Закрыть время','block');
  const list=[...rows.map(b=>({time:b.time,html:`<article class="slot"><div class="slot-time">${b.time}</div><div><strong>${esc(b.name)}</strong><p>${esc(service(b).name)}</p><small>${duration(b.duration)} · ${money(b.price)}</small><div>${badge(b)}</div></div><div class="slot-actions">${D.active(b)?btn('Перенести','reschedule','','data-id="'+b.id+'"'):''}${link('Открыть','request-detail.html?id='+b.id,'text-btn')}</div></article>`})),...blocks.map(b=>({time:b.start,html:`<article class="slot blocked"><div class="slot-time">${icon('lock')}</div><div><strong>${b.start==='00:00'&&b.end==='23:59'?'Весь день закрыт':b.start+'–'+b.end}</strong><p>${esc(b.reason||'Личное время')}</p></div><div class="slot-actions">${btn('Открыть время','reopen','','data-id="'+b.id+'"')}</div></article>`}))].sort((a,b)=>a.time.localeCompare(b.time));
  $('#dayTimeline').innerHTML=list.map(x=>x.html).join('')||(D.dayAvailability(selected)?empty('День свободен','Добавьте запись или закройте личное время.'):empty('Нерабочий день','Рабочие часы можно изменить в графике мастера.',link('Графики мастеров','masters.html','soft')));
}
function plural(n,a,b,c){const r=n%100;return r>=11&&r<=14?c:n%10===1?a:n%10>=2&&n%10<=4?b:c;}
function renderRequests(){
  if(!$('#requestList'))return;
  const q=($('#requestSearch').value||'').trim().toLocaleLowerCase('ru');
  const list=D.state.bookings.filter(b=>requestTab==='archived'?['rejected','cancelled'].includes(b.status):b.status===requestTab).filter(b=>(b.name+' '+b.phone).toLowerCase().includes(q)&&(!requestFilter.date||b.date===requestFilter.date)&&(!requestFilter.serviceId||b.serviceId===requestFilter.serviceId)).sort((a,b)=>(a.date+a.time).localeCompare(b.date+b.time));
  $$('[data-action=request-tab]').forEach(b=>b.setAttribute('aria-pressed',b.dataset.value===requestTab));
  $('#requestCount').textContent=`${list.length} ${plural(list.length,'запись','записи','записей')}${requestFilter.date||requestFilter.serviceId?' · фильтр включён':''}`;
  $('#requestList').innerHTML=list.map(b=>record(b)).join('')||empty('Заявок нет',q||requestFilter.date||requestFilter.serviceId?'Попробуйте изменить поиск или фильтр.':'Новые записи появятся здесь.');
}
function renderDetail(){
  if(!$('#requestDetail'))return;
  const id=new URLSearchParams(location.search).get('id')||'b2',b=D.state.bookings.find(b=>b.id===id);
  if(!b){$('#requestDetail').innerHTML=empty('Запись не найдена','Вернитесь к списку заявок.',link('К заявкам','requests.html','primary'));return;}
  const details=[['Услуга',esc(service(b).name)],['Мастер',esc(master(b).name)],['Дата',dateLabel(b.date,{day:'numeric',month:'long',year:'numeric'})],['Время',`${b.time} · ${duration(b.duration)}`],['Стоимость',money(b.price)],['Комментарий',esc(b.comment||'Без комментария')]];
  const rid=`data-id="${b.id}"`;
  $('#requestDetail').innerHTML=`<section class="card"><div class="record-top">${badge(b)}<small>№ ${b.id.toUpperCase()}</small></div><div class="record-person"><div class="avatar avatar-lg">${esc(initials(b.name))}</div><div><h2>${esc(b.name)}</h2><a href="tel:${esc(b.phone.replace(/[^+\d]/g,''))}">${esc(b.phone)}</a></div></div><dl class="detail-list">${details.map(([l,v])=>`<div class="detail-line"><dt>${l}</dt><dd>${v}</dd></div>`).join('')}</dl>${b.status==='pending'?'<div class="note"><p>Это время зарезервировано до вашего решения.</p></div>':''}<div class="record-actions">${b.status==='pending'?btn(icon('check')+'Подтвердить','confirm','green',rid):''}${D.active(b)?btn('Перенести','reschedule','',rid)+btn(b.status==='pending'?'Отклонить':'Отменить запись',b.status==='pending'?'reject':'cancel','danger',rid):''}${canComplete(b)?btn('Завершить визит','complete','green',rid):''}${b.status==='completed'?btn('Ссылка на отзыв','review-link','soft',rid):''}</div></section>`;
}
function renderHistory(){
  if(!$('#historyList'))return;
  const now=new Date(),start=new Date();start.setHours(0,0,0,0);if(historyTab==='week')start.setDate(start.getDate()-6);if(historyTab==='month')start.setDate(1);
  const list=D.state.bookings.filter(b=>b.status==='completed'&&new Date(b.date+'T'+b.time)>=start&&new Date(b.date+'T'+b.time)<=now).sort((a,b)=>(b.date+b.time).localeCompare(a.date+a.time));
  $$('[data-action=history-tab]').forEach(b=>b.setAttribute('aria-pressed',b.dataset.value===historyTab));
  $('#historySummary').textContent=`${list.length} ${plural(list.length,'визит','визита','визитов')} · ${money(list.reduce((s,b)=>s+b.price,0))}`;
  $('#historyList').innerHTML=list.map(b=>record(b)).join('')||empty('Завершённых визитов нет','После завершения записи она появится здесь.');
}
function renderServices(){
  if(!$('#servicesList'))return;
  $('#servicesList').innerHTML=D.state.services.map(s=>`<article class="record">${D.state.uploads['service-'+s.id]?`<img class="service-photo" src="${D.state.uploads['service-'+s.id]}" alt="${esc(s.name)}">`:''}<div class="record-top">${tile('bag')}<span class="badge ${s.active?'green':'gray'}">${s.active?'Опубликована':'Скрыта'}</span></div><h2>${esc(s.name)}</h2><p class="subtitle">${esc(s.description)}</p><div class="record-meta"><div>${icon('clock')}${duration(s.duration)} · ${money(s.price)}</div></div><div class="record-actions">${btn(icon('edit')+'Изменить','edit-service','soft',`data-id="${s.id}"`)}${btn(s.active?'Скрыть':'Опубликовать','toggle-service','',`data-id="${s.id}"`)}</div></article>`).join('')||empty('Добавьте первую услугу','Укажите название, стоимость и длительность.',btn('Добавить услугу','add-service','primary'));
}
function renderMasters(){
  if(!$('#mastersList'))return;
  $('#mastersList').innerHTML=D.state.masters.map(m=>`<article class="record"><div class="record-person"><div class="avatar avatar-lg">${esc(initials(m.name))}</div><div><h2>${esc(m.name)}</h2><p>${esc(m.specialty)} · ${esc(m.experience)}</p></div></div><div class="record-meta"><div><span class="badge ${m.active?'green':'gray'}">${m.active?'Принимает записи':'В архиве'}</span></div><p>${m.services.map(id=>esc(D.state.services.find(s=>s.id===id)?.name||'')).join(', ')}</p></div><div class="record-actions">${btn('Профиль','edit-master','soft',`data-id="${m.id}"`)}${btn(icon('calendar')+'График','schedule','',`data-id="${m.id}"`)}${btn(m.active?'Архивировать':'Восстановить','archive-master',m.active?'danger':'',`data-id="${m.id}"`)}</div></article>`).join('');
}
function renderReviews(){
  const valid=D.state.reviews.filter(r=>D.state.bookings.some(b=>b.id===r.bookingId&&b.status==='completed'));
  const items=valid.map(r=>{const b=D.state.bookings.find(b=>b.id===r.bookingId);return `<article class="record"><div class="record-top"><strong>${esc(b.name)}</strong><span class="review-rating" aria-label="Оценка ${r.rating} из 5">${Array.from({length:r.rating},()=>icon('star')).join('')}</span></div><small>${esc(service(b).name)} · ${dateLabel(b.date)}</small><p style="margin-top:14px">${esc(r.text)}</p>${r.photo?`<img class="review-photo" src="${r.photo}" alt="Фото к отзыву">`:''}${r.reply?`<div class="review-reply"><strong>Ответ студии</strong>${esc(r.reply)}</div>`:''}${page==='reviews'?btn(r.reply?'Изменить ответ':'Ответить','reply','text-btn',`data-id="${r.id}"`):''}</article>`;}).join('');
  if($('#reviewsSummary'))$('#reviewsSummary').innerHTML=`<div class="card section-head"><div><h2>${valid.length?(valid.reduce((n,r)=>n+r.rating,0)/valid.length).toFixed(1).replace('.',',')+' из 5':'Пока без оценки'}</h2><small>${valid.length} ${plural(valid.length,'подтверждённый отзыв','подтверждённых отзыва','подтверждённых отзывов')}</small></div>${tile('star','green')}</div>`;
  if($('#reviewsList'))$('#reviewsList').innerHTML=items||empty('Пока нет отзывов','Клиенты смогут оставить их после визита.');
  if($('#publicReviews'))$('#publicReviews').innerHTML=items||'<p class="subtitle">Первые отзывы появятся после завершённых визитов.</p>';
}
function renderGallery(){
  if($('#gallery'))$('#gallery').innerHTML=D.state.portfolio.map((p,i)=>`<figure><img src="${p.src}" alt="${esc(p.name)}"><div class="actions">${btn(icon('up'),'photo-up','icon-btn',`data-id="${p.id}" aria-label="Переместить фото выше" ${i===0?'disabled':''}`)}${btn(icon('down'),'photo-down','icon-btn',`data-id="${p.id}" aria-label="Переместить фото ниже" ${i===D.state.portfolio.length-1?'disabled':''}`)}${btn(icon('trash'),'photo-delete','icon-btn danger',`data-id="${p.id}" aria-label="Удалить фото"`)}</div></figure>`).join('')||empty('Портфолио пока пустое','Добавьте свои работы. Они появятся на клиентской странице.');
  if($('#publicGallery'))$('#publicGallery').innerHTML=D.state.portfolio.map(p=>`<img src="${p.src}" alt="${esc(p.name)}">`).join('')||'<p class="subtitle">Студия пока не добавила фотографии.</p>';
  $$('[data-preview]').forEach(el=>{const src=D.state.uploads[el.dataset.preview];el.innerHTML=src?`<img src="${src}" alt="Загруженное изображение">`:'';});
}
function safeUrl(value){try{const u=new URL(value);return ['https:','http:'].includes(u.protocol)?u.href:'';}catch{return '';}}
function renderPublic(){
  if(document.body.dataset.kind==='public'){
    const a=D.state.appearance;document.body.dataset.theme=a.theme;
    const colors={indigo:'#4e52bd',blue:'#286e9b',green:'#25764f',purple:'#7550a7'};
    document.body.style.setProperty('--primary',colors[a.accent]||colors.indigo);
    document.body.style.fontFamily=a.font==='humanist'?'"Trebuchet MS", "Segoe UI", sans-serif':'';
  }
  if($('#publicServices'))$('#publicServices').innerHTML=D.state.services.filter(s=>s.active).map(s=>`<a class="public-service" href="client-booking.html?service=${s.id}"><div><strong>${esc(s.name)}</strong><small>${duration(s.duration)}</small></div><div class="price">${money(s.price)}</div>${icon('right')}</a>`).join('')||empty('Запись пока недоступна','Студия обновляет список услуг.');
  if($('#publicContacts')){const c=D.state.contacts;$('#publicContacts').innerHTML=`<p>${esc(c.city)}, ${esc(c.address)}</p><p class="subtitle">${esc(c.directions)}</p><div class="actions">${link(icon('phone')+'Позвонить','tel:'+c.phone.replace(/[^+\d]/g,''),'soft')}${[['map2gis','2ГИС'],['mapYandex','Яндекс Карты'],['mapGoogle','Google Maps']].filter(([k])=>safeUrl(c[k])).map(([k,t])=>link(t,safeUrl(c[k]))).join('')}</div>`;}
  if($('.consultant-invite'))$('.consultant-invite').hidden=!D.state.settings.consultant;
  if($('#telegramWelcome')&&D.state.uploads.tgCover&&!$('.telegram-cover'))$('#telegramWelcome').insertAdjacentHTML('beforebegin',`<img class="telegram-cover" src="${D.state.uploads.tgCover}" alt="Обложка студии">`);
  if($('#telegramWelcome'))$('#telegramWelcome').textContent=D.state.telegramContent?.welcome||'Здравствуйте! Выберите услугу или посмотрите свободное время.';
}
function render(){
  $$('[data-owner-name]').forEach(e=>e.textContent=D.state.profile.name.split(' ')[0]);
  $$('[data-project-name]').forEach(e=>e.textContent=D.state.profile.project);
  $$('[data-category]').forEach(e=>e.textContent=D.state.profile.category);
  $$('[data-studio-avatar]').forEach(e=>{e.innerHTML=D.state.uploads.profile?`<img src="${D.state.uploads.profile}" alt="Фото студии">`:esc(initials(D.state.profile.name));});
  $$('[data-setting]').forEach(e=>e.setAttribute('aria-checked',String(D.state.settings[e.dataset.setting]!==false)));
  renderCalendar();renderRequests();renderDetail();renderHistory();renderServices();renderMasters();renderReviews();renderGallery();renderPublic();
  if($('#homeBookings')){const rows=D.state.bookings.filter(b=>D.active(b)&&b.date>=D.today()).sort((a,b)=>(a.date+a.time).localeCompare(b.date+b.time));$('#homeSummary').textContent=`${D.state.bookings.filter(b=>b.status==='pending').length} заявки ждут вашего решения.`;$('#homeBookings').innerHTML=rows.slice(0,3).map(b=>record(b,{compact:true})).join('')||empty('Ближайших записей нет','Добавьте запись в календаре.');}
  if($('#clientBookingsList'))$('#clientBookingsList').innerHTML=D.state.bookings.filter(b=>b.client).sort((a,b)=>D.active(a)!==D.active(b)?Number(D.active(b))-Number(D.active(a)):D.active(a)?(a.date+a.time).localeCompare(b.date+b.time):(b.date+b.time).localeCompare(a.date+a.time)).map(b=>record(b,{client:true})).join('')||empty('Вы ещё не записаны','Выберите услугу и удобное время.',link('Записаться','client-booking.html','primary'));
  if($('#paymentMethod'))$('#paymentMethod').textContent='Демонстрационная карта · •••• '+(D.state.paymentLast4||'4242');
}

function openSheet(type,ctx={}){
  sheetContext={type,...ctx};
  const b=D.state.bookings.find(x=>x.id===ctx.id),s=D.state.services.find(x=>x.id===ctx.id),m=D.state.masters.find(x=>x.id===ctx.id);
  if(type==='booking'||type==='reschedule'){
    if(type==='reschedule'&&(!b||!D.active(b)))return toast('Эту запись уже нельзя перенести.');
    if(ctx.client&&!clientCanChange(b))return toast('Срок изменения прошёл. Свяжитесь с мастером.');
    const services=D.state.services.filter(x=>x.active),masters=D.state.masters.filter(x=>x.active);
    if(!services.length||!masters.length)return showSheet('Добавьте услугу и мастера','Для записи нужны опубликованная услуга и активный мастер.',link('Услуги','services.html','primary')+link('Мастера','masters.html','soft'));
    const content=(type==='booking'?field('Имя клиента','name','','text','required autocomplete="name"')+field('Телефон','phone','','tel','required autocomplete="tel" minlength="10"')+select('Услуга','serviceId',services.map(s=>[s.id,`${s.name} · ${duration(s.duration)}`]),services[0].id)+select('Мастер','masterId',masters.map(m=>[m.id,m.name]),masters[0].id):'')+`<div class="form-grid">${field(type==='reschedule'?'Новая дата':'Дата','date',b?.date||selected,'date','required')}${field('Время','time',b?.time||'12:00','time','required step="1800"')}</div>`;
    showSheet(type==='booking'?'Новая запись':'Перенести запись',b?`${esc(b.name)} · ${dateLabel(b.date)}, ${b.time}`:'Добавьте клиента в расписание.',content,type,type==='booking'?'Добавить запись':'Перенести');
  }else if(type==='block'){
    showSheet('Закрыть время',dateLabel(selected,{weekday:'long',day:'numeric',month:'long'}),'<label class="check-row"><input type="checkbox" name="allDay"><span>Закрыть весь день</span></label><div class="form-grid">'+field('С','start','14:00','time','required')+field('До','end','16:00','time','required')+'</div>'+field('Причина','reason','','text','placeholder="Личное время"'),'block','Закрыть время');
  }else if(type==='reopen'){
    const block=D.state.blocks.find(x=>x.id===ctx.id);if(!block)return;
    showSheet('Открыть время?',`${dateLabel(block.date)} · ${block.start==='00:00'?'весь день':block.start+'–'+block.end}`,'<p>Этот период снова станет доступен для записи.</p>','reopen','Открыть время');
  }else if(['reject','cancel','complete'].includes(type)){
    if(!b)return;if(type==='complete'&&!canComplete(b))return toast('Визит ещё не завершился.');
    const words={reject:['Отклонить заявку?','Отклонить'],cancel:['Отменить запись?','Отменить запись'],complete:['Завершить визит?','Завершить']};
    showSheet(words[type][0],`${esc(b.name)} · ${dateLabel(b.date)}, ${b.time}`,type==='complete'?'<p>Запись появится в истории. Клиент сможет оставить отзыв.</p>':'<p>Время освободится, а изменение останется в истории заявки.</p>'+field('Причина, если нужно','reason','','textarea'),type,words[type][1],type!=='complete');
  }else if(type==='edit-service'||type==='add-service'){
    showSheet(s?'Изменить услугу':'Новая услуга','Название, стоимость и время для онлайн-записи.',field('Название','name',s?.name||'','text','required maxlength="100"')+'<div class="form-grid">'+field('Цена, ₽','price',s?.price??'','number','required min="0" max="1000000" step="1"')+select('Длительность','duration',[[30,'30 мин'],[60,'1 ч'],[90,'1 ч 30 мин'],[120,'2 ч'],[150,'2 ч 30 мин'],[180,'3 ч']],s?.duration||90)+'</div>'+field('Описание','description',s?.description||'','textarea')+(s?`<label class="upload-box">${icon('image')}<span>Фото услуги</span><input type="file" accept="image/jpeg,image/png,image/webp" data-upload="service-${s.id}"><span class="upload-preview" data-preview="service-${s.id}"></span></label>`:''),'service');
  }else if(type==='edit-master'||type==='add-master'){
    showSheet(m?'Профиль мастера':'Новый мастер','Эти сведения появятся на странице студии.',field('Имя мастера','name',m?.name||'','text','required')+field('Специализация','specialty',m?.specialty||'','text','required')+field('Опыт работы','experience',m?.experience||'')+'<fieldset class="code-field"><legend>Услуги мастера</legend>'+D.state.services.map(s=>`<label class="check-row"><input type="checkbox" name="services" value="${s.id}" ${m?.services.includes(s.id)?'checked':''}><span>${esc(s.name)}</span></label>`).join('')+'</fieldset>','master');
  }else if(type==='schedule'){
    if(!m)return;
    const names=['Понедельник','Вторник','Среда','Четверг','Пятница','Суббота','Воскресенье'];
    showSheet('График мастера',esc(m.name),m.schedule.map((d,i)=>`<div class="schedule-row"><label class="check-row"><input type="checkbox" name="day${i}" ${d.enabled?'checked':''}><span>${['Пн','Вт','Ср','Чт','Пт','Сб','Вс'][i]}</span></label>${field('Начало','start'+i,d.start,'time',`aria-label="${names[i]}, начало" required`)}${field('Конец','end'+i,d.end,'time',`aria-label="${names[i]}, конец" required`)}</div>`).join(''),'schedule');
  }else if(type==='archive-master'){
    if(!m)return;
    if(!m.active)return commit(st=>st.masters.find(x=>x.id===m.id).active=true,'Мастер восстановлен');
    showSheet('Архивировать мастера?',esc(m.name),'<p>Новые записи к мастеру будут недоступны. Существующие визиты останутся в календаре.</p>','archive-master','Архивировать',true);
  }else if(type==='edit-profile'){
    showSheet('Профиль студии','Название увидят ваши клиенты.',field('Ваше имя','name',D.state.profile.name,'text','required')+field('Название студии','project',D.state.profile.project,'text','required maxlength="80"')+field('Специализация','category',D.state.profile.category)+`<label class="upload-box">${icon('image')}<strong>Фото студии</strong><input type="file" accept="image/jpeg,image/png,image/webp" data-upload="profile"></label>`,'profile');
  }else if(type==='filters'){
    showSheet('Фильтры заявок','Уточните список по дате и услуге.',field('Дата','date',requestFilter.date,'date')+select('Услуга','serviceId',[['','Все услуги'],...D.state.services.map(s=>[s.id,s.name])],requestFilter.serviceId)+btn('Сбросить фильтры','clear-filters','text-btn'),'filters','Показать заявки');
  }else if(type==='reply'){
    const r=D.state.reviews.find(x=>x.id===ctx.id);if(!r)return;
    showSheet('Ответ клиенту',esc(r.text),field('Ваш ответ','reply',r.reply,'textarea','required maxlength="2000"'),'reply','Опубликовать ответ');
  }else if(type==='review-link'){
    if(!b||b.status!=='completed')return toast('Отзыв доступен только после завершения визита.');
    if(D.state.reviews.some(r=>r.bookingId===b.id))return showSheet('Отзыв уже опубликован','На один визит можно оставить один отзыв.',link('Открыть отзывы','reviews.html','soft'));
    const url=new URL('client-review.html?id='+b.id,location.href).href;
    showSheet('Ссылка на отзыв',`${esc(b.name)} · ${dateLabel(b.date)}`,field('Ссылка для клиента','reviewUrl',url,'url','readonly')+`<div class="actions">${btn(icon('share')+'Скопировать','copy-review','primary',`data-url="${esc(url)}"`)}${link('Проверить форму',url,'soft')}</div>`);
  }else if(type==='payment'){
    showSheet('Способ оплаты','Демонстрация выбора. Настоящие данные карты не нужны.',select('Демонстрационная карта','last4',[['4242','Карта •••• 4242'],['5556','Карта •••• 5556']],D.state.paymentLast4||'4242'),'payment','Выбрать карту');
  }else if(type==='auto-renew'){
    showSheet('Отключить автопродление?','Подписка останется активной до конца оплаченного периода.','<p>В прототипе платежи не выполняются.</p>','auto-renew','Отключить',true);
  }
}

document.addEventListener('click',async e=>{
  const toggle=e.target.closest('[data-setting]');
  if(toggle){const key=toggle.dataset.setting;if(key==='autoRenew'&&D.state.settings[key])return openSheet('auto-renew');commit(s=>s.settings[key]=!s.settings[key],'Настройка сохранена');return;}
  const el=e.target.closest('[data-action]');if(!el)return;
  const a=el.dataset.action,ctx={id:el.dataset.id,client:el.dataset.client==='true'};
  if(a==='close-sheet')return closeSheet();
  if(a==='menu'){
    const nav=[['index','Главная','globe','primary'],['ai','AI-помощник','spark','purple'],['calendar','Календарь','calendar','green'],['requests','Заявки','inbox','orange'],['history','История','history','blue'],['profile','Профиль','user','primary']];
    showSheet('PAZLIX','Рабочий кабинет',`<nav class="drawer-nav" aria-label="Меню кабинета">${nav.map(([r,t,i,c])=>`<a href="${r}.html">${tile(i,c)}<span>${t}</span>${icon('right')}</a>`).join('')}</nav><div class="drawer-footer">${esc(D.state.profile.project)}<br>Демонстрационный проект</div>`);sheet.classList.add('menu-sheet');return;
  }
  if(a==='notifications'){
    const pending=D.state.bookings.filter(b=>b.status==='pending');
    showSheet('Уведомления','События вашей студии',pending.map(b=>`<a class="notification-item" href="request-detail.html?id=${b.id}">${tile('inbox','orange')}<div><strong>Новая заявка</strong><small>${esc(b.name)} · ${dateLabel(b.date)}, ${b.time}</small></div></a>`).join('')+D.state.events.slice(0,4).map(v=>`<div class="notification-item">${tile('check','green')}<div>${esc(v.text)}</div></div>`).join('')+(!pending.length&&!D.state.events.length?'<p>Новых событий пока нет.</p>':'')+link('Настроить уведомления','notification-settings.html','soft full'));return;
  }
  if(a==='select-day'){selected=el.dataset.date;current=new Date(selected+'T12:00:00');current.setDate(1);renderCalendar();return;}
  if(a==='today'){selected=D.today();current=new Date(selected+'T12:00:00');current.setDate(1);renderCalendar();return;}
  if(a==='prev-month'||a==='next-month'){current.setMonth(current.getMonth()+(a==='next-month'?1:-1));selected=D.dateKey(current);renderCalendar();return;}
  if(a==='request-tab'){requestTab=el.dataset.value;renderRequests();return;}
  if(a==='history-tab'){historyTab=el.dataset.value;renderHistory();return;}
  if(a==='clear-filters'){requestFilter={date:'',serviceId:''};closeSheet();renderRequests();return;}
  if(a==='confirm'){const b=D.state.bookings.find(x=>x.id===ctx.id);if(!b||b.status!=='pending')return;commit(s=>{s.bookings.find(x=>x.id===ctx.id).status='confirmed';eventLog('Запись подтверждена: '+b.name);},'Запись подтверждена');return;}
  if(a==='toggle-service'){commit(s=>{const row=s.services.find(x=>x.id===ctx.id);row.active=!row.active;},'Публикация услуги обновлена');return;}
  if(a==='export'){download('pazlix-demo-export.json',JSON.stringify(D.state,null,2));toast('Копия проекта подготовлена');return;}
  if(a==='receipt'){download('pazlix-demo-payment.txt','PAZLIX · ДЕМОНСТРАЦИОННЫЙ ДОКУМЕНТ\nНе является платёжным документом.\nТариф: Business\nПример оплаты: 2 990 ₽\nДата: 4 сентября 2026','text/plain;charset=utf-8');return;}
  if(a==='share'||a==='copy-review'){const url=el.dataset.url||new URL('public-preview.html',location.href).href;try{await navigator.clipboard.writeText(url);toast('Ссылка скопирована');}catch{showSheet('Ссылка', 'Скопируйте ссылку из поля.',field('Ссылка','url',url,'url','readonly'));}return;}
  if(a==='photo-up'||a==='photo-down'||a==='photo-delete'){commit(s=>{const i=s.portfolio.findIndex(x=>x.id===ctx.id);if(i<0)return;if(a==='photo-delete'){s.portfolio.splice(i,1);return;}const j=i+(a==='photo-up'?-1:1);if(j>=0&&j<s.portfolio.length)[s.portfolio[i],s.portfolio[j]]=[s.portfolio[j],s.portfolio[i]];},a==='photo-delete'?'Фото удалено':'Порядок сохранён');return;}
  if(a==='ai-quick'){sendChat(el.dataset.prompt);return;}
  if(a==='rating'){rating=Number(el.dataset.value);$$('[data-action=rating]').forEach(b=>b.setAttribute('aria-pressed',Number(b.dataset.value)<=rating));return;}
  if(a==='resend'){resendCode();return;}
  if(a==='onboard-back'){onboardStep=Math.max(0,onboardStep-1);renderOnboarding();return;}
  if(a==='install'){if(installPrompt){await installPrompt.prompt();const result=await installPrompt.userChoice;installPrompt=null;updateInstall(result.outcome==='accepted');}return;}
  openSheet(a,ctx);
});

document.addEventListener('submit',e=>{
  const f=e.target;if(!f.dataset.form)return;e.preventDefault();if(!f.reportValidity())return;
  const type=f.dataset.form,fd=new FormData(f),v=Object.fromEntries(fd.entries());
  if(['register','login','verify','forgot','reset','onboard','client-booking','client-review','chat'].includes(type))return submitJourney(type,f,v,fd);
  if(type==='settings'){
    const scope=f.dataset.scope;
    if(!commit(s=>s[scope]={...(s[scope]||{}),...v},'Изменения сохранены'))return;
    $('.save-status',f).textContent='Сохранено';return;
  }
  if(type==='telegram'){
    if(!v.username.endsWith('bot'))return error(f,'Имя Telegram-бота должно оканчиваться на bot.');
    if(commit(s=>s.telegram={username:v.username},'Настройки бота сохранены'))$('#telegramState').innerHTML='<span class="badge blue">Демо-подключение готово</span>';return;
  }
  const ctx=sheetContext,b=D.state.bookings.find(x=>x.id===ctx.id);
  if(type==='booking'||type==='reschedule'){
    if(type==='reschedule'&&(!b||!D.active(b)))return error(f,'Запись больше не доступна для переноса.');
    const s=type==='booking'?D.state.services.find(x=>x.id===v.serviceId):service(b),masterId=v.masterId||b.masterId;
    if(!s)return error(f,'Выберите услугу.');
    if(type==='booking'&&!D.state.masters.find(m=>m.id===masterId)?.services.includes(s.id))return error(f,'Этот мастер не выполняет выбранную услугу.');
    const when=new Date(v.date+'T'+v.time);
    if(isNaN(when)||when.getTime()<Date.now())return error(f,'Выберите дату и время в будущем.');
    if(type==='booking'&&v.phone.replace(/\D/g,'').length<10)return error(f,'Введите полный номер телефона.');
    if(ctx.client){if(!clientCanChange(b))return error(f,'Для изменения визита свяжитесь с мастером.');if(!D.slots(v.date,b.serviceId,b.masterId,b.id).includes(v.time))return error(f,'Это время недоступно для онлайн-записи. Выберите рабочее время с шагом 30 минут.');}
    const conflict=D.conflict(v.date,v.time,type==='booking'?s.duration:b.duration,masterId,b?.id);
    if(conflict)return error(f,conflict);
    if(!commit(st=>{if(type==='booking'){st.bookings.push({id:D.id('b'),...v,duration:s.duration,price:s.price,status:'confirmed'});}else{const row=st.bookings.find(x=>x.id===b.id);row.date=v.date;row.time=v.time;}eventLog(type==='booking'?'Добавлена запись: '+v.name:'Перенесена запись: '+b.name);},type==='booking'?'Запись добавлена':'Запись перенесена'))return;
    if(page==='calendar'){selected=v.date;current=new Date(selected+'T12:00:00');current.setDate(1);renderCalendar();}
  }else if(type==='block'){
    const start=v.allDay?'00:00':v.start,end=v.allDay?'23:59':v.end;
    if(D.minutes(end)<=D.minutes(start))return error(f,'Конец периода должен быть позже начала.');
    if(activeForDate(selected).some(b=>D.minutes(b.time)<D.minutes(end)&&D.minutes(b.time)+b.duration>D.minutes(start)))return error(f,'В этом периоде есть записи. Сначала перенесите их.');
    if(D.state.blocks.some(b=>b.date===selected&&D.minutes(start)<D.minutes(b.end)&&D.minutes(end)>D.minutes(b.start)))return error(f,'Этот период уже закрыт.');
    if(!commit(s=>s.blocks.push({id:D.id('closed'),date:selected,start,end,reason:v.reason||'Личное время'}),'Время закрыто'))return;
  }else if(type==='reopen'){
    if(!commit(s=>s.blocks=s.blocks.filter(x=>x.id!==ctx.id),'Время снова открыто'))return;
  }else if(['reject','cancel','complete'].includes(type)){
    if(!b||!D.active(b))return error(f,'Эта запись уже обработана.');
    if(type==='complete'&&!canComplete(b))return error(f,'Визит ещё не завершился.');
    if(type==='cancel'&&page==='client-bookings'&&!clientCanChange(b))return error(f,'Срок отмены прошёл. Свяжитесь с мастером.');
    if(!commit(s=>{const row=s.bookings.find(x=>x.id===ctx.id);row.status=type==='reject'?'rejected':type==='cancel'?'cancelled':'completed';row.reason=v.reason||'';eventLog(statuses[row.status][0]+': '+row.name);},type==='complete'?'Визит завершён':'Решение сохранено'))return;
  }else if(type==='service'){
    if(!v.name.trim())return error(f,'Укажите название услуги.');
    if(!commit(s=>{const existing=s.services.find(x=>x.id===ctx.id),data={...v,name:v.name.trim(),price:Number(v.price),duration:Number(v.duration)};if(existing)Object.assign(existing,data);else{const row={id:D.id('s'),active:true,...data};s.services.push(row);s.masters.filter(m=>m.active).forEach(m=>m.services.push(row.id));}},'Услуга сохранена'))return;
  }else if(type==='master'){
    const services=fd.getAll('services');if(!services.length)return error(f,'Выберите хотя бы одну услугу.');
    if(!commit(s=>{const existing=s.masters.find(x=>x.id===ctx.id),data={name:v.name.trim(),specialty:v.specialty,experience:v.experience,services};if(existing)Object.assign(existing,data);else s.masters.push({id:D.id('m'),active:true,...data,schedule:Array.from({length:7},(_,i)=>({enabled:i<5,start:'10:00',end:'19:00'}))});},'Профиль мастера сохранён'))return;
  }else if(type==='schedule'){
    const schedule=Array.from({length:7},(_,i)=>({enabled:!!v['day'+i],start:v['start'+i],end:v['end'+i]}));
    if(schedule.some(d=>d.enabled&&D.minutes(d.end)<=D.minutes(d.start)))return error(f,'Проверьте время окончания рабочего дня.');
    if(!commit(s=>s.masters.find(m=>m.id===ctx.id).schedule=schedule,'График сохранён'))return;
  }else if(type==='archive-master'){
    if(!commit(s=>s.masters.find(m=>m.id===ctx.id).active=false,'Мастер перемещён в архив'))return;
  }else if(type==='profile'){
    if(!v.name.trim()||!v.project.trim())return error(f,'Укажите имя и название студии.');
    if(!commit(s=>s.profile={...s.profile,...v},'Профиль сохранён'))return;
  }else if(type==='filters'){requestFilter=v;renderRequests();
  }else if(type==='reply'){
    if(!v.reply.trim())return error(f,'Введите текст ответа.');
    if(!commit(s=>s.reviews.find(r=>r.id===ctx.id).reply=v.reply.trim(),'Ответ опубликован'))return;
  }else if(type==='payment'){
    if(!commit(s=>s.paymentLast4=v.last4,'Демонстрационный способ оплаты выбран'))return;
  }else if(type==='auto-renew'){
    if(!commit(s=>s.settings.autoRenew=false,'Автопродление отключено'))return;
  }
  closeSheet();
});

let onboardStep=0,onboardDraft={name:'',category:'Наращивание ресниц',city:'',address:'',service:'',price:'',duration:'90'};
function renderOnboarding(){
  const host=$('#onboardingFlow');if(!host)return;
  const steps=[['Как называется ваша студия?','Начнём с главного. Это название увидят ваши клиенты.',field('Название студии','name',onboardDraft.name,'text','required placeholder="Например, Lash Studio Анны"')+field('Чем вы занимаетесь?','category',onboardDraft.category,'text','required')],['На какую услугу записываются чаще?','Добавим её первой. Остальные можно добавить в кабинете.',field('Название услуги','service',onboardDraft.service,'text','required placeholder="Классическое наращивание"')+field('Стоимость, ₽','price',onboardDraft.price,'number','required min="0" max="1000000"')+select('Сколько времени нужно?','duration',[[30,'30 минут'],[60,'1 час'],[90,'1 час 30 минут'],[120,'2 часа']],onboardDraft.duration)],['Где вы принимаете клиентов?','Адрес поможет клиентам найти вас.',field('Город','city',onboardDraft.city,'text','required placeholder="Новосибирск"')+field('Адрес','address',onboardDraft.address,'text','required placeholder="Улица, дом, офис"')],['Всё готово к первому шагу','Проверьте данные. Их можно изменить в любой момент.',`<div class="onboard-summary"><div><strong>${esc(onboardDraft.name)}</strong><small>${esc(onboardDraft.category)}</small></div><div><strong>${esc(onboardDraft.service)}</strong><small>${money(onboardDraft.price)} · ${duration(Number(onboardDraft.duration))}</small></div><div><strong>${esc(onboardDraft.city)}</strong><small>${esc(onboardDraft.address)}</small></div></div>`]];
  const [heading,sub,content]=steps[onboardStep];
  host.innerHTML=`<div class="progress" aria-label="Шаг ${onboardStep+1} из 4">${steps.map((_,i)=>`<i class="${i<=onboardStep?'active':''}"></i>`).join('')}</div><div class="onboard-assistant">${tile('spark','purple')}<span>Помощник PAZLIX · шаг ${onboardStep+1} из 4</span></div><h1>${heading}</h1><p class="subtitle">${sub}</p><form data-form="onboard">${content}<div class="form-error" role="alert"></div><div class="onboard-actions">${onboardStep?btn('Назад','onboard-back'):''}<button class="btn primary" type="submit">${onboardStep===3?'Открыть кабинет':'Продолжить'}${icon('right')}</button></div></form>`;
}
function renderClientFields(){
  const host=$('#clientBookingFields');if(!host)return;
  const services=D.state.services.filter(s=>s.active),masters=D.state.masters.filter(m=>m.active);
  if(!services.length||!masters.length||!D.state.settings.onlineBooking){host.innerHTML=empty('Онлайн-запись пока закрыта','Свяжитесь со студией, чтобы выбрать время.',link('Контакты студии','public-preview.html','soft'));$('#clientBookingForm button[type=submit]').disabled=true;return;}
  const requested=new URLSearchParams(location.search).get('service'),serviceId=services.some(s=>s.id===requested)?requested:services[0].id;
  const masterId=masters.find(m=>m.services.includes(serviceId))?.id||masters[0].id;
  let chosenDate=D.today();
  for(let i=0;i<Number(D.state.rules.bookingHorizon);i++){const next=new Date();next.setDate(next.getDate()+i);const k=D.dateKey(next);if(D.slots(k,serviceId,masterId).length){chosenDate=k;break;}}
  host.innerHTML=`<section class="card">${select('Услуга','serviceId',services.map(s=>[s.id,s.name+' · '+money(s.price)]),serviceId)}${select('Мастер','masterId',masters.map(m=>[m.id,m.name]),masterId)}${field('Дата','date',chosenDate,'date',`required min="${D.today()}"`)}<div id="availableSlots"></div></section><section class="card" style="margin-top:16px"><h2>Ваши данные</h2>${field('Имя','name','','text','required autocomplete="name"')}${field('Телефон','phone','','tel','required autocomplete="tel" minlength="10" placeholder="+7 900 000-00-00"')}${field('Комментарий','comment','','textarea')}</section>`;
  updateAvailableSlots();
}
function updateAvailableSlots(){
  const f=$('#clientBookingForm'),host=$('#availableSlots');if(!host)return;
  const v=Object.fromEntries(new FormData(f)),mSelect=$('[name=masterId]',f),masters=D.state.masters.filter(m=>m.active&&m.services.includes(v.serviceId));
  mSelect.innerHTML=masters.map(m=>`<option value="${m.id}" ${m.id===v.masterId?'selected':''}>${esc(m.name)}</option>`).join('');
  const times=D.slots(v.date,v.serviceId,mSelect.value),s=D.state.services.find(s=>s.id===v.serviceId);
  host.innerHTML=`<p class="field-help">${duration(s.duration)} · ${money(s.price)}</p><h3 style="margin-top:18px">Свободное время</h3>`+(times.length?`<div class="slot-picker">${times.map(t=>`<label class="slot-choice"><input name="time" type="radio" value="${t}" required><span>${t}</span></label>`).join('')}</div>`:'<p class="subtitle">На эту дату свободного времени нет. Выберите другой день или мастера.</p>');
  $('#clientBookingForm button[type=submit]').disabled=!times.length;
}
function renderReviewForm(){
  const host=$('#reviewFormContent');if(!host)return;
  const id=new URLSearchParams(location.search).get('id'),b=D.state.bookings.find(b=>b.id===id);
  if(!b||b.status!=='completed'){host.innerHTML=empty('Отзыв после визита','Оставить отзыв можно только по ссылке завершённой записи.',link('Мои записи','client-bookings.html','soft'));return;}
  if(D.state.reviews.some(r=>r.bookingId===id)){host.innerHTML=empty('Спасибо за отзыв','Ваш отзыв уже опубликован.',link('На страницу студии','public-preview.html','primary'));return;}
  host.innerHTML=`<section class="card"><span class="badge green">Визит завершён</span><h2 style="margin-top:16px">${esc(service(b).name)}</h2><p>${dateLabel(b.date)} · ${esc(master(b).name)}</p><form data-form="client-review" data-id="${id}"><label class="field"><span>Ваша оценка</span></label><div class="rating-choices" role="group" aria-label="Оценка визита">${[1,2,3,4,5].map(i=>btn(icon('star'),'rating','icon-btn',`data-value="${i}" aria-label="${i} из 5" aria-pressed="true"`)).join('')}</div>${field('Комментарий','text','','textarea','required maxlength="2000" placeholder="Что вам понравилось?"')}<label class="upload-box">${icon('image')}<span>Добавить фото, если хотите</span><input type="file" accept="image/jpeg,image/png,image/webp" data-upload="review-${id}"><span class="upload-preview" data-preview="review-${id}"></span></label><p class="form-error" role="alert"></p><button class="btn primary full" type="submit" style="margin-top:20px">Опубликовать отзыв</button></form></section>`;
}
function submitJourney(type,f,v,fd){
  if(type==='chat'){sendChat($('#aiText').value);$('#aiText').value='';return;}
  if(type==='register'||type==='reset'){
    if(v.password!==v.repeat)return error(f,'Пароли не совпадают. Проверьте повтор пароля.');
    if(type==='reset'){f.hidden=true;$('#resetResult').hidden=false;return;}
    if(!v.name.trim())return error(f,'Укажите ваше имя.');
    sessionStorage.setItem('pazlix-signup',JSON.stringify({name:v.name.trim(),email:v.email,issued:Date.now(),verified:false}));
    location.href='verify-email.html';return;
  }
  if(type==='login'){location.href='index.html';return;}
  if(type==='verify'){
    const code=$$('.code-row input').map(i=>i.value).join('');
    if(code.length!==6)return error(f,'Введите все 6 цифр.');
    let signup=readSignup();if(Date.now()-signup.issued>600000)return error(f,'Срок действия кода истёк. Запросите новый.');
    if(code!=='123456'){error(f,'Неверный код. Для демонстрации используйте 123456.');$$('.code-row input').forEach(x=>x.setAttribute('aria-invalid','true'));return;}
    signup.verified=true;sessionStorage.setItem('pazlix-signup',JSON.stringify(signup));f.hidden=true;$('#verifySuccess').hidden=false;$('#resendCode').hidden=true;return;
  }
  if(type==='forgot'){f.hidden=true;$('#recoveryResult').hidden=false;return;}
  if(type==='onboard'){
    Object.assign(onboardDraft,v);
    if(Object.values(v).some(x=>!String(x).trim()))return error(f,'Заполните ответ, чтобы продолжить.');
    if(onboardStep<3){onboardStep++;renderOnboarding();return;}
    const signup=readSignup();
    if(!commit(s=>{s.profile={...s.profile,project:onboardDraft.name,category:onboardDraft.category,name:signup.name||s.profile.name};s.contacts={...s.contacts,city:onboardDraft.city,address:onboardDraft.address};const row={id:D.id('s'),name:onboardDraft.service,price:Number(onboardDraft.price),duration:Number(onboardDraft.duration),description:'',active:true};s.services.push(row);s.masters[0].services.push(row.id);s.onboarded=true;}))return;
    location.href='index.html';return;
  }
  if(type==='client-booking'){
    const s=D.state.services.find(s=>s.id===v.serviceId);
    if(!v.name.trim()||v.phone.replace(/\D/g,'').length<10)return error(f,'Укажите имя и полный номер телефона.');
    if(!v.time||!D.slots(v.date,v.serviceId,v.masterId).includes(v.time))return error(f,'Время уже недоступно. Выберите другой свободный слот.');
    if(!commit(st=>{const b={id:D.id('b'),...v,name:v.name.trim(),client:true,duration:s.duration,price:s.price,status:st.settings.manualConfirmation?'pending':'confirmed'};st.bookings.push(b);eventLog('Новая заявка: '+b.name);} ))return;
    f.hidden=true;const host=$('#clientBookingSuccess');host.hidden=false;host.innerHTML=`<div class="card success-panel">${tile('check','green')}<h2>${D.state.settings.manualConfirmation?'Заявка отправлена':'Вы записаны'}</h2><p>${esc(s.name)}<br>${dateLabel(v.date)}, ${v.time}</p>${link('Мои записи','client-bookings.html','primary full')}</div>`;return;
  }
  if(type==='client-review'){
    const b=D.state.bookings.find(b=>b.id===f.dataset.id);
    if(!b||b.status!=='completed'||D.state.reviews.some(r=>r.bookingId===b.id))return error(f,'Этот визит недоступен для нового отзыва.');
    if(!v.text.trim())return error(f,'Напишите пару слов о визите.');
    if(commit(s=>s.reviews.push({id:D.id('r'),bookingId:b.id,rating,text:v.text.trim(),reply:'',photo:s.uploads['review-'+b.id]||''}),'Спасибо за отзыв'))renderReviewForm();
  }
}
function readSignup(){try{return JSON.parse(sessionStorage.getItem('pazlix-signup'))||{email:'anna@example.com',issued:Date.now()};}catch{return {email:'anna@example.com',issued:Date.now()};}}
let resendTimer=null;
function resendCode(){
  const signup=readSignup();signup.issued=Date.now();signup.verified=false;sessionStorage.setItem('pazlix-signup',JSON.stringify(signup));
  const b=$('#resendCode');b.disabled=true;let remaining=30;b.textContent='Повторить через 30 с';
  clearInterval(resendTimer);resendTimer=setInterval(()=>{remaining--;b.textContent=remaining?'Повторить через '+remaining+' с':'Отправить код ещё раз';if(!remaining){clearInterval(resendTimer);b.disabled=false;}},1000);toast('Демо-код обновлён: 123456');
}
const codeInputs=$$('.code-row input');
codeInputs.forEach((input,i)=>{
  input.addEventListener('input',()=>{input.value=input.value.replace(/\D/g,'').slice(0,1);input.removeAttribute('aria-invalid');if(input.value)codeInputs[i+1]?.focus();});
  input.addEventListener('keydown',e=>{if(e.key==='Backspace'&&!input.value)codeInputs[i-1]?.focus();if(e.key==='ArrowLeft'){e.preventDefault();codeInputs[i-1]?.focus();}if(e.key==='ArrowRight'){e.preventDefault();codeInputs[i+1]?.focus();}});
  input.addEventListener('paste',e=>{const digits=(e.clipboardData.getData('text')||'').replace(/\D/g,'').slice(0,6);if(!digits)return;e.preventDefault();codeInputs.forEach((x,j)=>{x.value=digits[j]||'';x.removeAttribute('aria-invalid');});codeInputs[Math.min(digits.length,5)].focus();});
});
if($('#verifyEmail')){const signup=readSignup();$('#verifyEmail').textContent=signup.email;sessionStorage.setItem('pazlix-signup',JSON.stringify(signup));}
if(page==='register'){const signup=readSignup();if(signup.name)$('[name=name]').value=signup.name;if(signup.email!=='anna@example.com')$('[name=email]').value=signup.email;}

function sendChat(message){
  const text=message.trim(),log=$('#chatLog');if(!text||!log)return;
  const user=document.createElement('div');user.className='bubble user';user.innerHTML=`<p>${esc(text)}</p>`;log.appendChild(user);
  const lower=text.toLowerCase();let answer,href,label;
  if(page==='client-consultant'){
    if(!D.state.settings.consultant){answer='Консультант сейчас выключен. Вы можете связаться со студией.';href='public-preview.html';label='Контакты студии';}
    else if(/подготов|визит/.test(lower)){answer=D.state.knowledge?.preparation||'Приходите без макияжа глаз. Если есть чувствительность к клею, заранее сообщите мастеру.';}
    else if(/найти|адрес|где/.test(lower)){answer=`${D.state.contacts.city}, ${D.state.contacts.address}. ${D.state.contacts.directions}`;}
    else if(/сто|цен/.test(lower)){answer=D.state.services.filter(s=>s.active).map(s=>`${s.name} — ${money(s.price)}, ${duration(s.duration)}`).join('. ');}
    else if(/классик|2d|выбр/.test(lower)){answer='Классика даёт более естественный эффект, 2D — больше объёма. Окончательный вариант мастер поможет выбрать на консультации.';}
    else{answer='В этой демонстрации могу подсказать услуги, цены, адрес и подготовку. Для персонального вопроса свяжитесь с мастером.';}
    href=href||'client-booking.html';label=label||'Выбрать время';
  }else{
    const routes=[[/услуг|цен/,'services.html','Открыть услуги','Добавим название, стоимость и длительность услуги.'],[/расписан|график|врем/,'booking-rules.html','Правила записи','Откройте рабочие часы и правила. Разовый перерыв можно добавить в календаре.'],[/мастер/,'masters.html','Открыть мастеров','Добавьте профиль мастера и выберите его услуги.'],[/работ|фото|портфолио/,'portfolio.html','Добавить фотографии','Загрузите работы и расположите их в нужном порядке.'],[/telegram|телеграм|бот/,'telegram.html','Настроить Telegram','Настроим приветствие и проверим клиентское меню.'],[/шрифт|стил|цвет/,'appearance.html','Оформление','Выберите стиль и посмотрите его на публичной странице.']];
    const found=routes.find(([rx])=>rx.test(lower));[href,label,answer]=found?found.slice(1):['profile.html','Открыть настройки','Помогу с услугами, расписанием, командой или оформлением. Выберите раздел, чтобы продолжить.'];
  }
  const reply=document.createElement('div');reply.className='bubble';reply.innerHTML=`<p>${esc(answer)}</p><div class="actions">${link(label,href,'soft')}</div>`;log.appendChild(reply);reply.scrollIntoView({behavior:'smooth',block:'nearest'});
}
$('#aiText')?.addEventListener('keydown',e=>{if(e.key==='Enter'&&!e.shiftKey){e.preventDefault();$('#aiText').closest('form').requestSubmit();}});
$('#requestSearch')?.addEventListener('input',renderRequests);
document.addEventListener('change',e=>{
  const el=e.target;
  if(el.closest('#clientBookingForm')&&['serviceId','masterId','date'].includes(el.name))updateAvailableSlots();
  if(el.name==='allDay')$$('[name=start],[name=end]',el.form).forEach(x=>x.disabled=el.checked);
  if(el.closest('[data-scope=appearance]'))previewAppearance();
});
function hydrateSettings(){
  $$('form[data-scope]').forEach(f=>{const data=D.state[f.dataset.scope]||{};$$('[name]',f).forEach(input=>{if(data[input.name]===undefined)return;if(input.type==='radio')input.checked=input.value===data[input.name];else input.value=data[input.name];});});
}
function previewAppearance(){
  const host=$('#appearancePreview');if(!host)return;const f=host.closest('form'),v=Object.fromEntries(new FormData(f)),colors={indigo:'#4e52bd',blue:'#286e9b',green:'#25764f',purple:'#7550a7'};
  host.style.setProperty('--primary',colors[v.accent]);host.style.background=v.theme==='dark'?'#292e39':'#fff';host.style.color=v.theme==='dark'?'#f0f1f5':'#242834';host.style.fontFamily=v.font==='humanist'?'"Trebuchet MS",sans-serif':'';
}
document.addEventListener('change',async e=>{
  const input=e.target;if(!input.matches('[data-upload]'))return;
  const files=[...input.files];if(!files.length)return;
  const key=input.dataset.upload;
  if(files.some(f=>!['image/jpeg','image/png','image/webp'].includes(f.type)||f.size>5*1024*1024)){toast('Выберите JPG, PNG или WebP размером до 5 МБ.');input.value='';return;}
  if(key==='portfolio'&&D.state.portfolio.length+files.length>30){toast('В портфолио можно добавить до 30 фотографий.');return;}
  try{
    const photos=[];
    for(const f of files){const src=await resizeUpload(f);photos.push({id:D.id('photo'),name:f.name,src});}
    commit(s=>{if(key==='portfolio')s.portfolio.push(...photos);else s.uploads[key]=photos[0].src;},'Фотографии сохранены');
  }catch{toast('Не удалось прочитать изображение. Попробуйте другой файл.');}
  input.value='';
});
async function resizeUpload(file){
  const img=await createImageBitmap(file),scale=Math.min(1,1000/Math.max(img.width,img.height)),canvas=document.createElement('canvas');canvas.width=Math.round(img.width*scale);canvas.height=Math.round(img.height*scale);canvas.getContext('2d').drawImage(img,0,0,canvas.width,canvas.height);img.close();return canvas.toDataURL('image/jpeg',.8);
}
let installPrompt=null;
window.addEventListener('beforeinstallprompt',e=>{e.preventDefault();installPrompt=e;updateInstall();});
window.addEventListener('appinstalled',()=>updateInstall(true));
function updateInstall(installed=false){
  const text=$('#installStatus'),button=$('#installBtn');if(!text||!button)return;
  if(installed||window.matchMedia('(display-mode: standalone)').matches){text.textContent='PAZLIX уже установлен на этом устройстве.';button.hidden=true;}
  else if(installPrompt){text.textContent='Браузер поддерживает установку PAZLIX.';button.disabled=false;button.hidden=false;}
  else{text.textContent='Используйте меню браузера. Инструкция для вашего телефона — ниже.';button.disabled=true;button.hidden=true;}
}
if('serviceWorker' in navigator&&(location.protocol==='https:'||location.hostname==='127.0.0.1'||location.hostname==='localhost'))navigator.serviceWorker.register('sw.js').catch(()=>{});
window.addEventListener('storage',e=>{if(e.key==='pazlix-ui-v6')location.reload();});
hydrateSettings();render();renderClientFields();renderReviewForm();renderOnboarding();previewAppearance();updateInstall();
