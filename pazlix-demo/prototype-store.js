/* Device-local UI fixtures, not authentication or a backend. Never store secrets. */
(() => {
  const KEY = 'pazlix-ui-v6';
  const schedule = () => Array.from({length:7}, (_,i) => ({enabled:i<5,start:'10:00',end:'19:00'}));
  const seed = {
    version:6,
    profile:{name:'Анна Иванова',project:'Lash Studio Анны',category:'Наращивание ресниц'},
    settings:{consultant:true,onlineBooking:true,manualConfirmation:true,autoRenew:true,notifyNew:true,notifyChange:true,notifyConfirm:true,notifySummary:true,skipEmpty:true},
    contacts:{phone:'+7 900 123-45-67',telegram:'@lash_anna',city:'Новосибирск',address:'Красный проспект, 25',directions:'Вход со стороны проспекта, второй этаж.',map2gis:'',mapYandex:'',mapGoogle:''},
    rules:{bookingHorizon:'30',leadHours:'2',cancelHours:'6'},
    appearance:{theme:'light',font:'system',accent:'indigo'},
    services:[{id:'s1',name:'Классическое наращивание',price:2800,duration:90,description:'Естественный эффект и аккуратный изгиб.',active:true},{id:'s2',name:'Объём 2D',price:3200,duration:120,description:'Выразительный объём без утяжеления.',active:true},{id:'s3',name:'Коррекция',price:2400,duration:90,description:'Обновление формы и густоты ресниц.',active:true}],
    masters:[{id:'m1',name:'Анна Иванова',specialty:'Lash-мастер',experience:'5 лет',services:['s1','s2','s3'],active:true,schedule:schedule()}],
    bookings:[
      {id:'b1',name:'Екатерина Смирнова',phone:'+7 900 123-45-67',serviceId:'s1',masterId:'m1',date:'2026-09-24',time:'10:00',duration:90,price:2800,status:'confirmed',comment:'Натуральный эффект, длина 8–10 мм',client:true},
      {id:'b2',name:'Ксения Морозова',phone:'+7 911 234-56-78',serviceId:'s2',masterId:'m1',date:'2026-09-24',time:'13:30',duration:120,price:3200,status:'pending',comment:'Предпочитаю натуральный эффект.'},
      {id:'b3',name:'Анастасия Лебедева',phone:'+7 965 345-67-89',serviceId:'s3',masterId:'m1',date:'2026-09-24',time:'17:00',duration:90,price:2400,status:'confirmed'},
      {id:'b4',name:'Дарья Соколова',phone:'+7 900 111-22-33',serviceId:'s1',masterId:'m1',date:'2026-09-05',time:'11:00',duration:90,price:2800,status:'confirmed'},
      {id:'b5',name:'Ксения Морозова',phone:'+7 911 234-56-78',serviceId:'s2',masterId:'m1',date:'2026-09-10',time:'15:00',duration:120,price:3200,status:'pending'},
      {id:'b6',name:'Ирина Жарких',phone:'+7 900 222-33-44',serviceId:'s1',masterId:'m1',date:'2026-09-18',time:'17:00',duration:90,price:2800,status:'confirmed'},
      {id:'b7',name:'Анна Лебедева',phone:'+7 900 333-44-55',serviceId:'s3',masterId:'m1',date:'2026-09-22',time:'12:00',duration:90,price:2400,status:'confirmed'},
      {id:'b8',name:'Ольга Кузнецова',phone:'+7 900 444-55-66',serviceId:'s1',masterId:'m1',date:'2026-09-26',time:'12:30',duration:90,price:2800,status:'confirmed'},
      {id:'b9',name:'Мария Петрова',phone:'+7 900 555-66-77',serviceId:'s3',masterId:'m1',date:'2026-09-29',time:'16:00',duration:90,price:2400,status:'pending'},
      {id:'h1',name:'Екатерина Смирнова',phone:'+7 900 123-45-67',serviceId:'s1',masterId:'m1',date:'2026-09-03',time:'10:00',duration:90,price:2800,status:'completed',client:true},
      {id:'h2',name:'Анастасия Иванова',phone:'+7 911 234-56-78',serviceId:'s2',masterId:'m1',date:'2026-09-02',time:'13:00',duration:120,price:3200,status:'completed'},
      {id:'h3',name:'Дарья Соколова',phone:'+7 900 111-22-33',serviceId:'s1',masterId:'m1',date:'2026-09-03',time:'15:00',duration:90,price:2800,status:'completed',client:true}
    ],
    blocks:['07','14','21','28'].map(d=>({id:'closed'+d,date:'2026-09-'+d,start:'00:00',end:'23:59',reason:'Выходной'})),
    reviews:[{id:'r1',bookingId:'h1',rating:5,text:'Очень аккуратно и красиво. Всё понравилось.',reply:''},{id:'r2',bookingId:'h2',rating:5,text:'Записалась через сайт, всё понятно и удобно.',reply:''}],
    portfolio:[],uploads:{},events:[]
  };
  let state;
  try { state=JSON.parse(localStorage.getItem(KEY)); } catch {}
  if (!state || state.version!==6 || !Array.isArray(state.bookings)) state=structuredClone(seed);
  function save(){
    try { localStorage.setItem(KEY,JSON.stringify(state)); return true; }
    catch { document.dispatchEvent(new CustomEvent('storage-error')); return false; }
  }
  function mutate(fn){const backup=structuredClone(state);fn(state);if(!save()){state=backup;return false;}return true;}
  const dateKey=d=>`${d.getFullYear()}-${String(d.getMonth()+1).padStart(2,'0')}-${String(d.getDate()).padStart(2,'0')}`;
  const minutes=t=>Number(t.slice(0,2))*60+Number(t.slice(3));
  const time=m=>`${String(Math.floor(m/60)).padStart(2,'0')}:${String(m%60).padStart(2,'0')}`;
  const active=b=>['pending','confirmed'].includes(b.status);
  function conflict(date,start,duration,masterId,ignoreId=''){
    const from=minutes(start),to=from+Number(duration);
    if(!date||!start||!Number.isFinite(to)||duration<=0||to>1440)return 'Проверьте дату, время и длительность.';
    if(state.blocks.some(b=>b.date===date&&from<minutes(b.end)&&to>minutes(b.start)))return 'Это время закрыто. Выберите другой период.';
    if(state.bookings.some(b=>b.id!==ignoreId&&active(b)&&b.masterId===masterId&&b.date===date&&from<minutes(b.time)+Number(b.duration)&&to>minutes(b.time)))return 'У мастера уже есть запись на это время.';
    return '';
  }
  function slots(date,serviceId,masterId,ignoreId=''){
    const s=state.services.find(x=>x.id===serviceId),m=state.masters.find(x=>x.id===masterId);
    if(!s?.active||!m?.active||!m.services.includes(serviceId)||!state.settings.onlineBooking)return [];
    const weekday=(new Date(date+'T12:00:00').getDay()+6)%7,day=m.schedule[weekday];
    if(!day?.enabled)return [];
    const now=Date.now(),lead=Number(state.rules.leadHours||2)*3600000,end=new Date();end.setDate(end.getDate()+Number(state.rules.bookingHorizon||30));end.setHours(23,59,59,999);
    const out=[];
    for(let min=minutes(day.start);min+s.duration<=minutes(day.end);min+=30){const t=time(min),at=new Date(date+'T'+t);if(at.getTime()<now+lead||at>end||conflict(date,t,s.duration,masterId,ignoreId))continue;out.push(t);}
    return out;
  }
  function dayAvailability(date){
    const weekday=(new Date(date+'T12:00:00').getDay()+6)%7;
    return state.masters.filter(m=>m.active).some(m=>{
      const day=m.schedule[weekday];if(!day?.enabled)return false;
      const durations=state.services.filter(s=>s.active&&m.services.includes(s.id)).map(s=>s.duration);
      if(!durations.length)return false;const length=Math.min(...durations);
      for(let min=minutes(day.start);min+length<=minutes(day.end);min+=30){if(!conflict(date,time(min),length,m.id))return true;}return false;
    });
  }
  window.PazlixDemo={get state(){return state},save,mutate,dateKey,minutes,time,active,conflict,slots,dayAvailability,id:prefix=>prefix+crypto.randomUUID().slice(0,8),today:()=>dateKey(new Date())};
})();
