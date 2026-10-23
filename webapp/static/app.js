'use strict';

(() => {
  const $ = id => document.getElementById(id);
  const ui = Object.fromEntries([...document.querySelectorAll('[id]')].map(node=>[node.id,node]));
  const state = {template:null,mode:'hessen',jobs:[],selectedJob:null,linkedJob:null,map:null,pick:null,posting:false,polling:false,timer:null,failures:0,lastCreatedJob:null,selection:null,lastEditedEndpoint:'end',points:{start:null,end:null},route:null,routeVersion:0,previewing:false,searching:false,reusing:false,connected:false};
  const metres = new Intl.NumberFormat('de-DE',{maximumFractionDigits:0});
  const decimal = new Intl.NumberFormat('de-DE',{maximumFractionDigits:1});
  const statuses = {queued:'In Warteschlange',running:'Wird erstellt',succeeded:'Fertig',failed:'Fehlgeschlagen',cancelled:'Abgebrochen'};
  const stages = {queued:'Wartet auf die Generierung …',preparing:'Geodaten werden vorbereitet …',completed:'Strecke ist fertig.'};
  const element=(tag,className,text)=>{const node=document.createElement(tag);if(className)node.className=className;if(text!==undefined)node.textContent=text;return node;};
  const errorMessage=error=>error instanceof Error && error.message ? error.message : 'Die Anfrage konnte nicht abgeschlossen werden.';
  function localUrl(value) {try {const url=new URL(value,location.origin);return value && url.origin===location.origin && ['http:','https:'].includes(url.protocol) ? url.href : null;}catch{return null;}}
  async function api(path,options={},timeout=25000) {
    const controller=new AbortController(),timer=setTimeout(()=>controller.abort(),timeout);
    try {
      const response=await fetch(path,{...options,signal:controller.signal,headers:{Accept:'application/json',...(options.body?{'Content-Type':'application/json'}:{}),...options.headers}});
      const data=await response.json().catch(()=>null);
      if(!response.ok) throw new Error(typeof data?.error==='string' ? data.error : typeof data?.detail==='string' ? data.detail : `Anfrage fehlgeschlagen (${response.status}).`);
      if(data===null) throw new Error('Der Dienst hat eine ungültige Antwort gesendet.');
      return data;
    }catch(error){
      if(error.name==='AbortError') throw new Error('Die Anfrage hat zu lange gedauert. Bitte erneut versuchen.');
      if(error instanceof TypeError) throw new Error('Keine Verbindung zum Server. Bitte Verbindung und Dienst prüfen.');
      throw error;
    }finally{clearTimeout(timer);}
  }
  function connection(ok,message) {
    state.connected=ok;
    ui['connection-state'].textContent=message || (ok?'Verbunden':'Verbindung unterbrochen');
    ui['connection-state'].parentElement.classList.toggle('disconnected',!ok);
    ui['retry-connection'].hidden=ok;
  }
  function showError(id,message) {ui[id].textContent=message;ui[id].hidden=!message;}
  function selectedEndDistance(end=Number(ui['end-distance'].value)) {return state.template && end===state.template.total ? state.template.length_m : end;}
  function formValues() {
    const common={template_id:state.mode,name:ui['track-name'].value.trim(),road_width_m:Number(ui['road-width'].value),scenery_quality:ui['scenery-quality'].value,vegetation_density:Number(ui['vegetation-density'].value),ai_materials:$('ai-materials').checked};
    return state.mode==='hessen' ? {...common,route_id:state.route?.id} : {...common,start_m:Number(ui['start-distance'].value),end_m:selectedEndDistance()};
  }
  function persistForm() {
    if(!state.template)return;
    try{localStorage.setItem(`road-to-assetto:${state.mode}`,JSON.stringify({...formValues(),points:state.points}));}catch{/* Storage is optional. */}
  }
  function loadTemplate(template,catalog) {
    const coords=template?.map?.coordinates;
    if(!Array.isArray(coords)||coords.length<2||!coords.every(point=>point.length>=3 && point.slice(0,3).every(Number.isFinite)))throw new Error('Für die vorbereitete Strecke fehlen geografische Kartenkoordinaten.');
    const length=Number(template.length_m),total=Math.floor(length),minimum=Math.ceil(Number(template.capabilities?.min_segment_m)||120);
    if(!Number.isFinite(length)||total<minimum)throw new Error('Die vorbereitete Strecke ist zu kurz.');
    state.template={...template,total,minimum,length_m:length};
    state.discovery=catalog.discovery || {max_length_m:2000,min_segment_m:120};
    if(!state.map) state.map=new RoadMap('route-map',onMapPoint,bad=>{$('tile-error').hidden=!bad;},catalog.map_config);
    if(catalog.map_config)state.map.configureTiles(catalog.map_config);
    state.map.setPrepared(state.template);
    ui['map-loading'].hidden=true;
    for(const id of ['start-distance','end-distance','start-slider','end-slider'])ui[id].max=total;
    ui['start-distance'].value=0;ui['end-distance'].value=total;
    $('section-hint').textContent=`Mindestens ${metres.format(minimum)} m, insgesamt ${metres.format(length)} m vorbereitet.`;
    ui['route-mode'].disabled=false;ui['search-button'].disabled=false;ui['form-fields'].disabled=false;ui['fit-map'].disabled=false;
    switchMode(ui['route-mode'].value,false);
    try {
      const saved=JSON.parse(localStorage.getItem(`road-to-assetto:${state.mode}`));
      if(saved && typeof saved==='object') {
        applySettings(saved);
        if(state.mode==='hessen' && saved.points)for(const endpoint of ['start','end'])if(validPoint(saved.points[endpoint]))state.points[endpoint]=saved.points[endpoint];
      }
    }catch{/* A previous form is optional. */}
    if(state.mode==='hessen'){renderNewSelection();state.map.fit();}else setDistances('end');
    updateVegetation();updateSubmit();
  }
  function validPoint(point){return point && Number.isFinite(point.lat)&&Number.isFinite(point.lon)&&Math.abs(point.lat)<=90&&Math.abs(point.lon)<=180;}
  function applySettings(settings) {
    if(typeof settings.name==='string'&&settings.name.trim())ui['track-name'].value=settings.name.slice(0,60);
    if(Number.isFinite(settings.road_width_m)&&settings.road_width_m>=4&&settings.road_width_m<=(state.mode==='altenhain'?6:8))ui['road-width'].value=String(settings.road_width_m);
    if(['standard','high'].includes(settings.scenery_quality))ui['scenery-quality'].value=settings.scenery_quality;
    if(Number.isFinite(settings.vegetation_density))ui['vegetation-density'].value=Math.max(0,Math.min(1,settings.vegetation_density));
    $('ai-materials').checked=settings.ai_materials===true;
    if(state.mode==='altenhain') {
      if(Number.isFinite(settings.start_m))ui['start-distance'].value=Math.round(settings.start_m);
      if(Number.isFinite(settings.end_m))ui['end-distance'].value=Math.round(Math.min(state.template.total,settings.end_m));
    }
    updateVegetation();
  }
  function switchMode(mode,remember=true) {
    if(!state.template)return;
    state.routeVersion++;
    let saved=null;
    try{saved=JSON.parse(localStorage.getItem(`road-to-assetto:${mode}`));}catch{/* Storage is optional. */}
    if(remember)persistForm();
    state.mode=mode==='altenhain'?'altenhain':'hessen';ui['route-mode'].value=state.mode;
    state.map.setMode(state.mode);state.pick=null;updatePick();
    const prepared=state.mode==='altenhain';
    $('prepared-section').hidden=!prepared;$('new-route-section').hidden=prepared;$('aerial-option').hidden=!prepared;
    $('aerial-toggle').checked=false;
    for(const id of ['start-distance','end-distance','start-slider','end-slider'])ui[id].disabled=!prepared;
    ui['region-name'].textContent=prepared ? state.template.name : 'Neue Straße in Hessen';
    ui['region-description'].textContent=prepared ? `${state.template.city} · ${metres.format(state.template.length_m)} m vorbereitet` : `Start und Ziel wählen · ${metres.format(state.discovery.min_segment_m||120)} bis ${metres.format(state.discovery.max_length_m||2000)} m`;
    ui['source-description'].textContent=prepared ? 'Vorbereitete amtliche Gebäudemodelle und Geländehöhen, am Luftbild nachgeführte Straßenränder. Fassaden und Grundstücksanschlüsse sind teilweise nachgebildet.' : 'Straßenverlauf aus OpenStreetMap, Gelände, Luftbilder und Gebäudedaten aus Hessen. Geodaten werden für den gewählten Abschnitt geladen; Fassaden und Grundstücksdetails werden daraus nachgebildet.';
    const limits=prepared ? state.template.limitations || [] : ['Verfügbar für kurze Straßenabschnitte in Hessen.','Der Verlauf wird vor dem Export geprüft. Verfügbare Gebäudedaten und Geländehöhen können je nach Ort variieren.','Eine Route ist keine Vermessung. Fassaden und Grundstücksdetails können vom Original abweichen.'];
    ui['limitations'].replaceChildren(...limits.map(text=>element('li','',String(text))));
    for(const option of ui['road-width'].options){const value=Number(option.value);option.disabled=prepared&&value>6;option.hidden=prepared&&value>6;option.textContent=prepared ? (value===6?'Nach Luftbild · 100 %':`Schmaler · ${Math.round(value/6*100)} %`) : `${decimal.format(value)} m`;}
    if(prepared&&Number(ui['road-width'].value)>6)ui['road-width'].value='6';
    $('width-hint').textContent=prepared ? 'Breitenfaktor für das vorbereitete Luftbildprofil.' : 'Konstante Fahrbahnbreite für den neuen Straßenabschnitt.';
    ui['track-name'].value=String(prepared ? state.template.name : state.route?.name || 'Meine Straße').slice(0,60);
    if(remember&&saved&&typeof saved==='object')applySettings(saved);
    showError('form-error','');
    if(prepared){setDistances('end');state.map.fit();}else{renderNewSelection();if(state.route)state.map.setRoute(state.route.coordinates);state.map.fit();}
    updateSubmit();
  }
  function pointAt(distance) {
    const route=state.template.map.coordinates;
    if(distance<=route[0][2])return route[0];
    for(let i=1;i<route.length;i++)if(route[i][2]>=distance){const a=route[i-1],b=route[i],t=(distance-a[2])/Math.max(.000001,b[2]-a[2]);return[a[0]+(b[0]-a[0])*t,a[1]+(b[1]-a[1])*t,distance];}
    return route.at(-1);
  }
  function inputSelection() {
    if(!state.template||ui['start-distance'].value.trim()===''||ui['end-distance'].value.trim()==='')return null;
    const start=Number(ui['start-distance'].value),end=Number(ui['end-distance'].value);
    return Number.isInteger(start)&&Number.isInteger(end)&&start>=0&&end<=state.template.total&&end-start>=state.template.minimum ? {start,end}:null;
  }
  function setDistances(changed) {
    if(!state.template)return;
    state.lastEditedEndpoint=changed;
    const {total,minimum}=state.template;
    let start=Math.round(Number(ui['start-distance'].value)||0),end=Math.round(Number(ui['end-distance'].value)||total);
    start=Math.max(0,Math.min(total-minimum,start));end=Math.max(minimum,Math.min(total,end));
    if(end-start<minimum){if(changed==='start')start=end-minimum;else end=start+minimum;}
    ui['start-distance'].value=start;ui['end-distance'].value=end;renderSelection(start,end);
  }
  function renderSelection(start,end) {
    state.selection={start,end};ui['start-slider'].value=start;ui['end-slider'].value=end;
    ui['start-distance'].max=end-state.template.minimum;ui['start-slider'].max=state.template.total-state.template.minimum;
    ui['end-distance'].min=start+state.template.minimum;ui['end-slider'].min=state.template.minimum;
    if(state.mode==='altenhain') {
      const preciseEnd=selectedEndDistance(end),selected=[pointAt(start),...state.template.map.coordinates.filter(p=>p[2]>start&&p[2]<preciseEnd),pointAt(preciseEnd)];
      state.map.setRoute(selected);state.map.setMarker('start',selected[0]);state.map.setMarker('end',selected.at(-1));
      ui['selection-distance'].textContent=`${metres.format(preciseEnd-start)} m gewählt`;
    }
    persistForm();updateSubmit();
  }
  function setPick(endpoint){state.pick=state.pick===endpoint?null:endpoint;state.map.setPick(state.pick);updatePick();if(state.pick&&window.innerWidth<=800)ui['map-shell'].scrollIntoView({block:'center',behavior:'auto'});}
  function updatePick() {
    for(const endpoint of ['start','end'])for(const prefix of ['pick-','new-pick-'])$(prefix+endpoint).setAttribute('aria-pressed',String(state.pick===endpoint));
    ui['map-help'].textContent=state.pick ? `${state.pick==='start'?'Start A':'Ziel B'}: Position auf der Karte anklicken${state.mode==='altenhain'?' – rastet an der Langstraße ein':''}.` : 'Karte ziehen oder mit zwei Fingern zoomen. A und B zum Versetzen ziehen.';
  }
  function onMapPoint(endpoint,value) {
    if(state.reusing)return;
    if(state.mode==='altenhain'){ui[endpoint+'-distance'].value=value;setDistances(endpoint);state.pick=null;}
    else {state.points[endpoint]=value;state.route=null;state.routeVersion++;state.map.setRoute([]);state.pick=endpoint==='start'&&!state.points.end?'end':null;renderNewSelection();persistForm();}
    state.map.setPick(state.pick);updatePick();showError('form-error','');
  }
  function renderNewSelection() {
    for(const endpoint of ['start','end']) {
      const point=state.points[endpoint];$(endpoint+'-coordinate').textContent=point?`${point.lat.toFixed(5)}, ${point.lon.toFixed(5)}`:'Noch nicht gesetzt';
      if(state.mode==='hessen')state.map.setMarker(endpoint,point);
    }
    $('preview-route').disabled=state.previewing||!state.points.start||!state.points.end;
    $('preview-route').textContent=state.previewing?'Verlauf wird geprüft …':'Straßenverlauf prüfen';
    if(state.mode==='hessen')ui['selection-distance'].textContent=state.route?`${metres.format(state.route.length_m)} m gewählt`:'Verlauf noch nicht geprüft';
    if(!state.route){$('route-feedback').textContent=state.previewing?'Straßenverbindungen werden geladen. Das kann einen Moment dauern.':state.points.start&&state.points.end?'Start und Ziel sind gesetzt. Jetzt den Straßenverlauf prüfen.':'Eine Adresse suchen oder direkt auf der Karte Start und Ziel wählen.';$('route-notes').hidden=true;}
    updateSubmit();
  }
  function validateRoute(route) {
    if(!route || !/^[a-f0-9]{32}$/.test(route.id)||!Array.isArray(route.coordinates)||route.coordinates.length<2||!route.coordinates.every(point=>Array.isArray(point)&&point.length>=2&&point.slice(0,2).every(Number.isFinite))||!Number.isFinite(route.length_m))throw new Error('Der Dienst hat keinen gültigen Straßenverlauf zurückgegeben.');
    return route;
  }
  function adoptRoute(route,rename=false) {
    state.route=validateRoute(route);const coords=route.coordinates;
    state.points={start:validPoint(route.start)?route.start:{lat:coords[0][0],lon:coords[0][1]},end:validPoint(route.end)?route.end:{lat:coords.at(-1)[0],lon:coords.at(-1)[1]}};
    if(rename||ui['track-name'].value==='Meine Straße')ui['track-name'].value=String(route.name||'Meine Straße').slice(0,60);
    renderNewSelection();state.map.setRoute(coords,true);
    $('route-feedback').textContent=`${metres.format(route.length_m)} m Straßenverlauf geprüft. Die Strecke kann jetzt erstellt werden.`;
    const warnings=Array.isArray(route.warnings)?route.warnings:[];
    $('route-warnings').replaceChildren(...[...new Set(warnings.map(String))].map(text=>element('li','',text)));$('route-notes').hidden=!warnings.length;$('route-notes').open=false;
    persistForm();updateSubmit();
  }
  async function previewRoute() {
    if(state.previewing||!state.points.start||!state.points.end)return;
    const version=state.routeVersion,points=structuredClone(state.points);state.previewing=true;state.route=null;state.map.setRoute([]);renderNewSelection();showError('form-error','');
    try{const response=await api('/api/routes/preview',{method:'POST',body:JSON.stringify(points)},90000);if(version===state.routeVersion&&state.mode==='hessen')adoptRoute(response.route);}
    catch(error){if(version===state.routeVersion)showError('form-error',errorMessage(error));}
    finally{state.previewing=false;renderNewSelection();}
  }
  function updateVegetation(){ui['vegetation-value'].textContent=`${Math.round(Number(ui['vegetation-density'].value)*100)} %`;}
  function pendingDuplicate() {
    const values=formValues();
    return state.jobs.some(job=>['queued','running'].includes(job.status)&&job.settings&&Object.entries(values).every(([key,value])=>typeof value==='number'?Math.abs(Number(job.settings[key])-value)<1e-6:job.settings[key]===value));
  }
  function updateSubmit() {
    const pending=state.jobs.filter(job=>['queued','running'].includes(job.status)).length;
    const ready=state.mode==='hessen'?Boolean(state.route):Boolean(inputSelection());
    const duplicate=Boolean(state.template)&&pendingDuplicate();
    ui['generate-button'].disabled=!state.template||!ready||state.posting||state.reusing||state.previewing||pending>=5||duplicate;
    ui['generate-button'].textContent=state.posting?'Wird gestartet …':duplicate?'Diese Strecke wird bereits erstellt':pending>=5?'Warteschlange voll':pending?'Strecke zur Warteschlange hinzufügen':'Strecke generieren';
  }
  const dateLabel=value=>{const date=new Date(value);return Number.isNaN(date.getTime())?'':new Intl.DateTimeFormat('de-DE',{day:'2-digit',month:'2-digit',hour:'2-digit',minute:'2-digit'}).format(date);};
  const hashJob=()=>/^#job=([a-f0-9]{32})$/.exec(location.hash)?.[1] || null;
  function selectJob(id,hash=true){state.selectedJob=id;if(hash)history.replaceState(null,'',`${location.pathname}${location.search}#job=${id}`);$('share-url').hidden=true;$('share-feedback').textContent='';renderJobs();}
  function renderJobs() {
    ui['jobs-count'].textContent=state.jobs.length?`${state.jobs.length} ${state.jobs.length===1?'Strecke':'Strecken'}`:'Noch keine Strecken';
    if(!state.jobs.length){ui['job-list'].replaceChildren(element('p','empty-state','Hier erscheinen die Strecken aller Nutzer dieses Servers.'));ui['job-detail'].hidden=true;updateSubmit();return;}
    if(!state.jobs.some(job=>job.id===state.selectedJob))state.selectedJob=state.jobs.find(job=>['running','queued'].includes(job.status))?.id||state.jobs[0].id;
    const focusId=document.activeElement?.closest('.job-row')?.dataset.jobId;
    const rows=state.jobs.map(job=>{const button=element('button',`job-row${job.id===state.selectedJob?' selected':''}`);button.type='button';button.dataset.jobId=job.id;button.setAttribute('aria-pressed',String(job.id===state.selectedJob));button.append(element('span','job-row-name',job.name||'Unbenannte Strecke'));const meta=element('span','job-row-meta');meta.append(element('span','',dateLabel(job.created_at)),element('span',job.status==='succeeded'?'success':job.status==='failed'?'failed':'',statuses[job.status]||'Unbekannt'));button.append(meta);button.addEventListener('click',()=>selectJob(job.id));return button;});
    ui['job-list'].replaceChildren(...rows);if(focusId)rows.find(row=>row.dataset.jobId===focusId)?.focus({preventScroll:true});
    renderDetail(state.jobs.find(job=>job.id===state.selectedJob));updateSubmit();
  }
  function renderDetail(job) {
    if (!job) {ui['job-detail'].hidden=true;return;}
    ui['job-detail'].hidden=false;
    $('reuse-job').disabled=!job.settings || state.reusing;
    $('share-job').disabled=false;
    ui['job-name'].textContent=job.name || 'Unbenannte Strecke';
    ui['job-state'].textContent=statuses[job.status] || job.status;
    const pending=['queued','running'].includes(job.status);
    ui['job-progress-block'].hidden=!pending;
    const progress=Math.round(Math.max(0,Math.min(100,Number(job.progress)||0)));
    const stage=stages[job.stage] || (typeof job.stage === 'string' && job.stage ? job.stage : statuses[job.status]);
    if (ui['job-stage'].textContent !== stage) ui['job-stage'].textContent=stage;
    ui['job-progress'].value=progress;
    ui['job-progress-text'].textContent=`${progress} %`;
    ui['job-error'].hidden=job.status!=='failed';
    ui['job-error'].textContent=typeof job.error === 'string' ? job.error : 'Die Strecke konnte nicht erzeugt werden. Du kannst es mit denselben Einstellungen erneut versuchen.';
    ui['job-success'].hidden=job.status!=='succeeded';
    if (job.status==='succeeded') {
      const summary=job.summary || {};
      const warnings=[...new Set((Array.isArray(summary.source_route?.warnings)?summary.source_route.warnings:[]).filter(value=>typeof value==='string'&&value.trim()).map(value=>value.trim()))];
      $('job-source-warnings').replaceChildren(...warnings.map(value=>element('li','',value)));
      $('job-source-title').textContent=`Hinweise zu den Geodaten (${warnings.length})`;
      $('job-source-notes').hidden=!warnings.length;
      const parts=[];
      if (Number.isFinite(Number(summary.length_m))) parts.push(`${metres.format(Number(summary.length_m))} m Strecke`);
      if (Number.isFinite(Number(summary.buildings))) parts.push(`${metres.format(Number(summary.buildings))} Gebäude`);
      const elevation = summary.elevation_gain_m ?? (Number.isFinite(summary.elevation_max_m) && Number.isFinite(summary.elevation_min_m) ? summary.elevation_max_m-summary.elevation_min_m : undefined);
      if (Number.isFinite(elevation)) parts.push(`${decimal.format(elevation)} m Höhenunterschied`);
      ui['job-summary'].textContent=parts.length ? `${parts.join(' · ')}. Bereit für deine Testfahrt.` : 'Deine Strecke ist fertig und bereit für die Testfahrt.';
      const hillsideParts=[];
      for (const [key,singular,plural] of [['hillside_retaining_assemblies','Stützmauer','Stützmauern'],['hillside_staircases','Treppe','Treppen'],['hillside_driveways','Zufahrt','Zufahrten']]) {
        if (Number.isInteger(summary[key]) && summary[key]>0) hillsideParts.push(`${metres.format(summary[key])} ${summary[key]===1 ? singular : plural}`);
      }
      if (hillsideParts.length) ui['job-summary'].textContent+=` Hanganschlüsse: ${hillsideParts.join(', ')}. Aus Geländehöhen abgeleitet.`;
      if (summary.ai_materials?.enabled) {
        ui['job-summary'].textContent+=' Mit KI-Materialvarianten.';
        if (summary.ai_generation?.cache_hit) ui['job-summary'].textContent+=' Vorhandenes Materialpaket wiederverwendet.';
        else if (Number.isInteger(summary.ai_generation?.usage?.images_generated)) ui['job-summary'].textContent+=` ${summary.ai_generation.usage.images_generated} Bilder neu erzeugt.`;
      }
      const download=localUrl(job.download_url);
      ui['job-download'].hidden=!download;
      if (download) ui['job-download'].href=download;
      const views=job.preview_urls && Object.keys(job.preview_urls).length ? job.preview_urls : {overview:job.preview_url};
      if (!views[state.previewView]) state.previewView=views.driver ? 'driver' : 'overview';
      const viewNames={overview:'Übersicht',driver:'Straßenansicht',environment:'Umgebung'};
      $('preview-views').replaceChildren(...Object.keys(views).filter(key=>viewNames[key]).map(key=>{
        const button=element('button','text-button',viewNames[key]);button.type='button';
        button.setAttribute('aria-pressed',String(key===state.previewView));
        button.addEventListener('click',()=>{state.previewView=key;renderDetail(job);});return button;
      }));
      const preview=localUrl(views[state.previewView] || job.preview_url);
      ui['job-preview'].alt=summary.preview_kind === 'aerial_map' ? 'Luftbild mit dem ausgewählten Straßenverlauf' : 'Gerenderte Vorschau der erzeugten Strecke';
      if (preview) {
        $('preview-open').href=preview;
        if (ui['job-preview'].src!==preview) {ui['job-preview'].hidden=false;ui['job-preview'].src=preview;}
      } else {ui['job-preview'].hidden=true;ui['job-preview'].removeAttribute('src');}
    }
  }


  async function refresh() {
    if(state.polling)return;state.polling=true;clearTimeout(state.timer);
    try {
      if(!state.template){const catalog=await api('/api/catalog');const template=catalog.templates?.find(item=>item.id==='altenhain');if(!template)throw new Error('Der Streckenkatalog ist nicht verfügbar.');loadTemplate(template,catalog);}
      const data=await api('/api/jobs');if(!Array.isArray(data.jobs))throw new Error('Die Streckenliste konnte nicht geladen werden.');
      state.jobs=data.jobs.filter(job=>job&&/^[a-f0-9]{32}$/.test(job.id)).sort((a,b)=>new Date(b.created_at)-new Date(a.created_at));
      if(state.lastCreatedJob&&!state.jobs.some(job=>job.id===state.lastCreatedJob.id))state.jobs.unshift(state.lastCreatedJob);else state.lastCreatedJob=null;
      const linked=hashJob();
      if(linked&&!state.jobs.some(job=>job.id===linked)){
        try{state.linkedJob=state.linkedJob?.id===linked?state.linkedJob:await api(`/api/jobs/${linked}`);if(state.linkedJob?.id===linked)state.jobs.push(state.linkedJob);}catch{showError('share-feedback','Diese Strecke wurde nicht gefunden.');}
      }
      if(linked&&state.jobs.some(job=>job.id===linked))state.selectedJob=linked;
      renderJobs();state.failures=0;connection(true);showError('global-error','');
    }catch(error){state.failures++;connection(false);showError('global-error',errorMessage(error));if(!state.map)ui['map-loading'].textContent='Die Karte wird geladen, sobald der Server wieder erreichbar ist.';}
    finally{state.polling=false;state.timer=setTimeout(refresh,state.failures?Math.min(30000,3000*state.failures):state.jobs.some(job=>['queued','running'].includes(job.status))?2000:8000);}
  }
  ui['track-form'].noValidate=true;
  ui['track-form'].addEventListener('submit',async event=>{
    event.preventDefault();if(state.posting||!state.template||state.previewing||pendingDuplicate())return;
    if(state.mode==='altenhain')setDistances(state.lastEditedEndpoint);
    if(!ui['track-form'].reportValidity())return;
    if(state.mode==='hessen'&&!state.route){showError('form-error','Bitte zuerst Start und Ziel wählen und den Straßenverlauf prüfen.');return;}
    const values=formValues();if(!values.name){showError('form-error','Bitte einen Streckennamen angeben.');return;}
    state.posting=true;updateSubmit();showError('form-error','');persistForm();
    try{const job=await api('/api/jobs',{method:'POST',body:JSON.stringify(values)});if(!job||!/^[a-f0-9]{32}$/.test(job.id))throw new Error('Keine gültige Auftragsnummer. Vor einem erneuten Start die Streckenliste prüfen.');state.lastCreatedJob=job;state.jobs=[job,...state.jobs.filter(existing=>existing.id!==job.id)];selectJob(job.id);ui['job-detail'].scrollIntoView({block:'nearest',behavior:'auto'});connection(true);}
    catch(error){showError('form-error',errorMessage(error));}
    finally{state.posting=false;updateSubmit();clearTimeout(state.timer);state.timer=setTimeout(refresh,500);}
  });
  $('search-form').addEventListener('submit',async event=>{
    event.preventDefault();if(state.searching||!$('search-form').reportValidity())return;
    const query=$('place-query').value.trim();if(query.length<3)return;
    state.searching=true;$('search-button').disabled=true;$('search-results').hidden=true;showError('search-feedback','Adresse wird gesucht …');
    try {
      const data=await api('/api/search',{method:'POST',body:JSON.stringify({query})});
      const results=Array.isArray(data.results)?data.results.filter(validPoint):[];
      $('search-results').replaceChildren(...results.map(result=>{const button=element('button','search-result',result.label||`${result.lat}, ${result.lon}`);button.type='button';button.addEventListener('click',()=>{state.map.showResult(result);showError('search-feedback',`Karte bei ${result.label||'Suchergebnis'}. Jetzt Start und Ziel setzen.`);$('search-results').hidden=true;if(state.mode==='hessen')setPick(state.points.start?'end':'start');});return button;}));
      $('search-results').hidden=!results.length;showError('search-feedback',results.length?`${results.length} ${results.length===1?'Ergebnis':'Ergebnisse'} – Ort auf der Karte anzeigen:`:'Kein passender Ort gefunden. Straße, Ort und gegebenenfalls Postleitzahl ergänzen.');
    }catch(error){showError('search-feedback',errorMessage(error));}
    finally{state.searching=false;$('search-button').disabled=!state.template;}
  });
  $('route-mode').addEventListener('change',()=>switchMode($('route-mode').value));
  $('preview-route').addEventListener('click',previewRoute);
  $('clear-route').addEventListener('click',()=>{state.points={start:null,end:null};state.route=null;state.routeVersion++;state.map.setRoute([]);state.pick=null;state.map.setPick(null);updatePick();renderNewSelection();persistForm();showError('form-error','');});
  for(const endpoint of ['start','end']) {
    $('new-pick-'+endpoint).addEventListener('click',()=>setPick(endpoint));$('center-'+endpoint).addEventListener('click',()=>state.map.chooseCenter(endpoint));
    ui[endpoint+'-distance'].addEventListener('input',()=>{state.lastEditedEndpoint=endpoint;const value=inputSelection();if(value)renderSelection(value.start,value.end);else updateSubmit();});
    ui[endpoint+'-distance'].addEventListener('change',()=>setDistances(endpoint));
    ui[endpoint+'-slider'].addEventListener('input',()=>{ui[endpoint+'-distance'].value=ui[endpoint+'-slider'].value;setDistances(endpoint);});
    ui['pick-'+endpoint].addEventListener('click',()=>setPick(endpoint));
  }
  ui['reset-section'].addEventListener('click',()=>{ui['start-distance'].value=0;ui['end-distance'].value=state.template.total;setDistances('end');state.map.fit();});
  for(const id of ['track-name','road-width','scenery-quality','ai-materials'])ui[id].addEventListener('change',()=>{persistForm();updateSubmit();});
  ui['track-name'].addEventListener('input',updateSubmit);
  ui['vegetation-density'].addEventListener('input',()=>{updateVegetation();persistForm();updateSubmit();});
  ui['fit-map'].addEventListener('click',()=>state.map.fit());$('aerial-toggle').addEventListener('change',()=>state.map.showAerial($('aerial-toggle').checked));
  ui['retry-connection'].addEventListener('click',refresh);$('refresh-jobs').addEventListener('click',refresh);
  ui['job-preview'].addEventListener('error',()=>{ui['job-preview'].hidden=true;});
  $('share-job').addEventListener('click',async()=>{
    if(!state.selectedJob)return;const url=new URL(location.href);url.hash=`job=${state.selectedJob}`;
    $('share-url').value=url.href;$('share-url').hidden=false;
    try{await navigator.clipboard.writeText(url.href);$('share-feedback').textContent='Link kopiert.';}catch{$('share-url').focus();$('share-url').select();$('share-feedback').textContent='Link markieren und kopieren.';}
  });
  $('reuse-job').addEventListener('click',async()=>{
    const job=state.jobs.find(item=>item.id===state.selectedJob);if(!job?.settings||state.reusing)return;
    state.reusing=true;$('reuse-job').disabled=true;ui['form-fields'].disabled=true;ui['route-mode'].disabled=true;updateSubmit();showError('form-error','');
    try{
      const values=job.settings;switchMode(values.template_id);applySettings(values);
      if(values.template_id==='hessen'){state.route=null;state.points={start:null,end:null};state.map.setRoute([]);renderNewSelection();const data=await api(`/api/routes/${encodeURIComponent(values.route_id)}`);adoptRoute(data.route);applySettings(values);}
      else{setDistances('end');state.map.fit();}
      $('workspace').scrollIntoView({block:'start',behavior:'auto'});$('track-name').focus({preventScroll:true});persistForm();
    }catch(error){showError('form-error',errorMessage(error));}
    finally{state.reusing=false;$('reuse-job').disabled=false;ui['form-fields'].disabled=false;ui['route-mode'].disabled=false;updateSubmit();}
  });
  window.addEventListener('hashchange',()=>{state.selectedJob=hashJob();refresh();});
  window.addEventListener('online',refresh);document.addEventListener('visibilitychange',()=>{if(!document.hidden)refresh();});
  let aiDirty=false;
  function updateAIFields(settings) {
    $('ai-provider').value=settings.provider;
    $('ai-base-url').value=settings.base_url;
    $('ai-base-url').readOnly=settings.provider==='openai';
    $('ai-model').value=settings.image_model || '';
    $('ai-key').value='';$('ai-clear-key').checked=false;
    $('ai-key-state').textContent=settings.api_key_configured ? 'Ein Schlüssel ist gespeichert.' : 'Kein Schlüssel gespeichert. Lokale Anbieter können ohne Schlüssel arbeiten.';
    aiDirty=false;
  }
  async function openAI() {
    $('ai-settings-status').textContent='Einstellungen laden …';
    $('ai-dialog').showModal();
    try {updateAIFields(await api('/api/ai/settings'));$('ai-settings-status').textContent='';}
    catch(error) {$('ai-settings-status').textContent=errorMessage(error);}
  }
  $('open-ai-settings').addEventListener('click',openAI);
  $('configure-ai').addEventListener('click',openAI);
  $('close-ai-settings').addEventListener('click',()=>{$('ai-key').value='';$('ai-dialog').close();});
  $('ai-dialog').addEventListener('close',()=>{$('ai-key').value='';});
  $('ai-settings-form').addEventListener('input',()=>{aiDirty=true;});
  $('ai-provider').addEventListener('change',()=>{
    $('ai-base-url').readOnly=$('ai-provider').value==='openai';
    if ($('ai-provider').value==='openai') $('ai-base-url').value='https://api.openai.com/v1';
    $('ai-key').value='';
  });
  $('ai-settings-form').addEventListener('submit',async event=>{
    event.preventDefault();$('ai-save').disabled=true;$('ai-test').disabled=true;
    try {
      const settings=await api('/api/ai/settings',{method:'POST',body:JSON.stringify({provider:$('ai-provider').value,base_url:$('ai-base-url').value.trim(),image_model:$('ai-model').value.trim(),api_key:$('ai-key').value,clear_api_key:$('ai-clear-key').checked})});
      updateAIFields(settings);$('ai-settings-status').textContent='Gespeichert. KI-Materialien kannst du pro Export einschalten.';
    } catch(error) {$('ai-settings-status').textContent=errorMessage(error);}
    finally {$('ai-key').value='';$('ai-save').disabled=false;$('ai-test').disabled=false;}
  });
  $('ai-test').addEventListener('click',async()=>{
    if (aiDirty) {$('ai-settings-status').textContent='Bitte die geänderten Einstellungen zuerst speichern.';return;}
    $('ai-test').disabled=true;$('ai-save').disabled=true;$('ai-settings-status').textContent='Verbindung prüfen …';
    try {
      const result=await api('/api/ai/test',{method:'POST',body:'{}'});
      $('ai-models').replaceChildren(...(result.models || []).map(name=>{const option=document.createElement('option');option.value=name;return option;}));
      $('ai-settings-status').textContent=result.message+' Es wurde kein Bild erzeugt. Bitte ein bildfähiges Modell wählen.';
    } catch(error) {$('ai-settings-status').textContent=errorMessage(error);}
    finally {$('ai-test').disabled=false;$('ai-save').disabled=false;}
  });
  refresh();
})();
