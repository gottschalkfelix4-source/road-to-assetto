'use strict';

(() => {
  const $ = id => document.getElementById(id);
  const ui = Object.fromEntries(['connection-state','retry-connection','global-error','region-name','region-description','fit-map','map-shell','route-map','aerial-image','route-base-outline','route-base','route-selected-outline','route-selected','start-marker','end-marker','map-loading','zoom-in','zoom-out','scale-label','scale-line','map-help','selection-distance','source-description','limitations','track-form','form-fields','track-name','reset-section','start-distance','end-distance','start-slider','end-slider','pick-start','pick-end','section-hint','road-width','scenery-quality','vegetation-density','vegetation-value','form-error','generate-button','jobs-count','job-list','job-detail','job-name','job-state','job-progress-block','job-stage','job-progress','job-progress-text','job-error','job-success','job-summary','job-preview','job-download'].map(id => [id, $(id)]));
  const state = { template: null, jobs: [], selectedJob: null, pick: null, posting: false, polling: false, timer: null, failures: 0, lastCreatedJob: null, selection: null, lastEditedEndpoint: 'end', mapWidth: 1000, mapHeight: 708, view: null, drag: null, imageLoaded: false };
  const metres = new Intl.NumberFormat('de-DE', {maximumFractionDigits: 0});
  const decimal = new Intl.NumberFormat('de-DE', {maximumFractionDigits: 1});
  const statuses = {queued:'In Warteschlange', running:'Wird erstellt', succeeded:'Fertig', failed:'Fehlgeschlagen', cancelled:'Abgebrochen'};
  const stages = {queued:'Wartet auf die Generierung …', preparing:'Geodaten werden vorbereitet …', prepare:'Geodaten werden vorbereitet …', geometry:'Straße und Umgebung werden aufgebaut …', scenery:'Umgebungsdetails werden ergänzt …', exporting:'Strecke wird exportiert …', export:'Strecke wird exportiert …', packaging:'ZIP-Paket wird erstellt …', validating:'Strecke wird geprüft …', rendering:'Vorschau wird erstellt …', completed:'Strecke ist fertig.', succeeded:'Strecke ist fertig.'};

  function element(tag, className, text) {
    const node = document.createElement(tag);
    if (className) node.className = className;
    if (text !== undefined) node.textContent = text;
    return node;
  }

  function localUrl(value) {
    if (typeof value !== 'string' || !value) return null;
    try { const url = new URL(value, location.origin); return url.origin === location.origin && ['http:','https:'].includes(url.protocol) ? url.href : null; } catch { return null; }
  }

  function errorMessage(error) {
    return error instanceof Error && error.message ? error.message : 'Die Anfrage konnte nicht abgeschlossen werden.';
  }

  async function api(path, options = {}) {
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), 25000);
    try {
      const response = await fetch(path, {...options, signal: controller.signal, headers:{'Accept':'application/json', ...(options.body ? {'Content-Type':'application/json'} : {}), ...options.headers}});
      const data = await response.json().catch(() => null);
      if (!response.ok) {
        const detail = data?.detail || data?.error || data?.message;
        const message = typeof detail === 'string' ? detail : Array.isArray(detail) ? detail.map(item => typeof item?.msg === 'string' ? item.msg : '').filter(Boolean).join(' · ') : '';
        throw new Error(message || `Anfrage fehlgeschlagen (${response.status}).`);
      }
      if (data === null) throw new Error('Der Dienst hat eine ungültige Antwort gesendet.');
      return data;
    } catch (error) {
      if (error.name === 'AbortError') throw new Error('Der lokale Dienst antwortet derzeit nicht. Bitte erneut versuchen.');
      if (error instanceof TypeError) throw new Error('Keine Verbindung zum lokalen Dienst. Prüfe, ob Road to Assetto läuft.');
      throw error;
    } finally { clearTimeout(timer); }
  }

  function connection(connected, message) {
    ui['connection-state'].textContent = message || (connected ? 'Lokal verbunden' : 'Verbindung unterbrochen');
    ui['connection-state'].parentElement.classList.toggle('disconnected', !connected);
    ui['retry-connection'].hidden = connected;
  }

  function persistForm() {
    if (!state.template || !inputSelection()) return;
    try { localStorage.setItem(`road-to-assetto:${state.template.id}`, JSON.stringify(formValues())); } catch { /* Storage is optional. */ }
  }

  function formValues() {
    return {template_id:state.template?.id, name:ui['track-name'].value.trim(), start_m:Number(ui['start-distance'].value), end_m:selectedEndDistance(), road_width_m:Number(ui['road-width'].value), scenery_quality:ui['scenery-quality'].value, vegetation_density:Number(ui['vegetation-density'].value), ai_materials:$('ai-materials').checked};
  }

  function selectedEndDistance(end = Number(ui['end-distance'].value)) {
    // The controls show whole metres; the full-road export retains its precise endpoint.
    return state.template && end === state.template.total ? Number(state.template.length_m) : end;
  }

  function loadTemplate(template) {
    if (!template?.map || !Array.isArray(template.map.bounds) || template.map.bounds.length !== 4 || !Array.isArray(template.map.route) || template.map.route.length < 2) throw new Error('Für die Region fehlen gültige Karten- oder Streckendaten.');
    const bounds = template.map.bounds.map(Number);
    if (!bounds.every(Number.isFinite) || bounds[2] <= bounds[0] || bounds[3] <= bounds[1]) throw new Error('Die Kartengrenzen sind ungültig.');
    const route = template.map.route.map(point => point.map(Number));
    if (!route.every(point => point.length >= 3 && point.slice(0,3).every(Number.isFinite))) throw new Error('Die Route enthält ungültige Punkte.');
    const length = Number(template.length_m) || route[route.length - 1][2];
    const total = Math.floor(length);
    const minimum = Math.ceil(Number(template.capabilities?.min_segment_m) || 120);
    if (total < minimum) throw new Error('Die verfügbare Strecke ist zu kurz.');
    state.template = {...template, length_m:length, map:{...template.map, bounds, route}, total, minimum};
    state.mapHeight = state.mapWidth * (bounds[3] - bounds[1]) / (bounds[2] - bounds[0]);
    ui['region-name'].textContent = template.name || 'Altenhain – Langstraße';
    ui['region-description'].textContent = `${template.city || 'Bad Soden am Taunus'} · ${metres.format(total)} m verfügbar`;
    ui['route-map'].setAttribute('aria-label', `Luftbild mit dem Verlauf von ${template.name || 'der verfügbaren Straße'}. Start und Ziel sind über die Eingabefelder einstellbar.`);
    ui['track-name'].value = template.name || 'Altenhain – Langstraße';
    ui['section-hint'].textContent = `Mindestens ${metres.format(minimum)} m für eine fahrbare Teststrecke.`;
    for (const id of ['start-distance','end-distance','start-slider','end-slider']) ui[id].max = total;
    ui['start-distance'].value = 0;
    ui['end-distance'].value = total;
    const maxWidth = Number(template.capabilities?.max_road_width_m) || 6;
    for (const option of ui['road-width'].options) option.disabled = Number(option.value) > maxWidth;
    if (Number(ui['road-width'].value) > maxWidth) ui['road-width'].value = [...ui['road-width'].options].filter(option => !option.disabled).at(-1)?.value || '4';
    try {
      const saved = JSON.parse(localStorage.getItem(`road-to-assetto:${template.id}`));
      if (saved && typeof saved === 'object') {
        if (typeof saved.name === 'string' && saved.name.trim()) ui['track-name'].value = saved.name.slice(0,60);
        if (Number.isFinite(saved.start_m)) ui['start-distance'].value = saved.start_m;
        if (Number.isFinite(saved.end_m)) ui['end-distance'].value = saved.end_m;
        if ([...ui['road-width'].options].some(option => !option.disabled && Number(option.value) === saved.road_width_m)) ui['road-width'].value = saved.road_width_m;
        if (['standard','high'].includes(saved.scenery_quality)) ui['scenery-quality'].value = saved.scenery_quality;
        $('ai-materials').checked=saved.ai_materials===true;
        if (Number.isFinite(saved.vegetation_density)) ui['vegetation-density'].value = Math.max(0,Math.min(1,saved.vegetation_density));
      }
    } catch { /* A previous form is optional. */ }
    const limitations = Array.isArray(template.limitations) ? template.limitations : [];
    ui['limitations'].replaceChildren(...limitations.map(value => element('li', '', String(value))));
    if (!limitations.length) ui['limitations'].append(element('li', '', 'Der Prototyp unterstützt zurzeit die Langstraße in Altenhain. Fassaden und zusätzliche Details sind nachgebildet, keine Originalfotos.'));
    const buildings = Number(template.summary?.buildings);
    ui['source-description'].textContent = `${Number.isFinite(buildings) ? `${metres.format(buildings)} amtliche Gebäudemodelle, ` : ''}Geländehöhen und Luftbilder aus offenen Daten. Fassaden und zusätzliche Straßendetails sind nachgebildet; der Prototyp ist noch kein fotorealistischer Nachbau.`;
    ui['aerial-image'].setAttribute('width', state.mapWidth);
    ui['aerial-image'].setAttribute('height', state.mapHeight);
    const imageUrl = localUrl(template.map.image_url);
    if (!imageUrl) throw new Error('Das lokale Luftbild ist nicht verfügbar.');
    const image = new Image();
    image.onload = () => { state.imageLoaded = true; ui['aerial-image'].setAttribute('href', imageUrl); ui['map-loading'].hidden = true; };
    image.onerror = () => { ui['map-loading'].textContent = 'Das Luftbild konnte nicht geladen werden. Die Streckenauswahl ist über die Felder weiterhin möglich.'; };
    image.src = imageUrl;
    const fullPath = pathFromPoints(route.map(toMap));
    ui['route-base'].setAttribute('d', fullPath);
    ui['route-base-outline'].setAttribute('d', fullPath);
    fitMap();
    setDistances('end');
    updateVegetation();
    ui['form-fields'].disabled = false;
    ui['fit-map'].disabled = false;
    ui['zoom-in'].disabled = false;
    ui['zoom-out'].disabled = false;
    updateSubmit();
  }

  function toMap(point) {
    const [west,south,east,north] = state.template.map.bounds;
    return [(point[0] - west) / (east - west) * state.mapWidth, (north - point[1]) / (north - south) * state.mapHeight];
  }

  function pointAt(distance) {
    const route = state.template.map.route;
    if (distance <= route[0][2]) return route[0];
    for (let i = 1; i < route.length; i++) {
      if (route[i][2] >= distance) {
        const a = route[i-1], b = route[i], fraction = (distance - a[2]) / Math.max(0.000001,b[2] - a[2]);
        return [a[0] + (b[0] - a[0]) * fraction, a[1] + (b[1] - a[1]) * fraction, distance];
      }
    }
    return route[route.length - 1];
  }

  const pathFromPoints = points => points.map((point,index) => `${index ? 'L' : 'M'}${point[0].toFixed(3)} ${point[1].toFixed(3)}`).join(' ');

  function inputSelection() {
    if (!state.template || ui['start-distance'].value.trim() === '' || ui['end-distance'].value.trim() === '') return null;
    const start = Number(ui['start-distance'].value);
    const end = Number(ui['end-distance'].value);
    const {total,minimum} = state.template;
    if (!Number.isInteger(start) || !Number.isInteger(end) || start < 0 || end > total || end-start < minimum) return null;
    return {start,end};
  }

  function previewDistances(endpoint) {
    state.lastEditedEndpoint = endpoint;
    const selection = inputSelection();
    // Keep incomplete keystrokes intact; only a valid pair updates the map.
    if (selection) renderSelection(selection.start,selection.end);
  }

  function setDistances(changed) {
    if (!state.template) return;
    const {total,minimum} = state.template;
    const startInput = ui['start-distance'].value.trim() === '' ? NaN : Number(ui['start-distance'].value);
    const endInput = ui['end-distance'].value.trim() === '' ? NaN : Number(ui['end-distance'].value);
    let start = Number.isFinite(startInput) ? Math.round(startInput) : state.selection?.start ?? 0;
    let end = Number.isFinite(endInput) ? Math.round(endInput) : state.selection?.end ?? total;
    start = Math.max(0,Math.min(total-minimum,start));
    end = Math.max(minimum,Math.min(total,end));
    if (end - start < minimum) {
      if (changed === 'start') start = end - minimum;
      else end = start + minimum;
    }
    ui['start-distance'].value = start;
    ui['end-distance'].value = end;
    renderSelection(start,end);
  }

  function renderSelection(start,end) {
    const {total,minimum} = state.template;
    state.selection = {start,end};
    ui['start-slider'].value = start;
    ui['end-slider'].value = end;
    ui['start-distance'].max = end-minimum;
    ui['start-slider'].max = total-minimum;
    ui['end-distance'].min = start+minimum;
    ui['end-slider'].min = minimum;
    const preciseEnd = selectedEndDistance(end);
    const selected = [pointAt(start),...state.template.map.route.filter(point => point[2] > start && point[2] < preciseEnd),pointAt(preciseEnd)];
    const path = pathFromPoints(selected.map(toMap));
    ui['route-selected'].setAttribute('d', path);
    ui['route-selected-outline'].setAttribute('d', path);
    updateMarkers();
    ui['selection-distance'].textContent = `${metres.format(preciseEnd-start)} m gewählt`;
    persistForm();
  }

  function updateMarkers() {
    if (!state.template || !state.view) return;
    const screenScale = ui['route-map'].getScreenCTM()?.a;
    const scale = screenScale ? 1/screenScale : state.view.w/state.mapWidth;
    for (const [marker,distance] of [['start-marker',state.selection?.start ?? 0],['end-marker',selectedEndDistance(state.selection?.end ?? state.template.total)]]) {
      const point = toMap(pointAt(distance));
      ui[marker].setAttribute('transform', `translate(${point[0]} ${point[1]}) scale(${scale})`);
    }
  }

  function updateView() {
    const view = state.view;
    ui['route-map'].setAttribute('viewBox', `${view.x} ${view.y} ${view.w} ${view.h}`);
    updateMarkers();
    updateScale();
  }

  function fitMap() {
    if (!state.template) return;
    state.view = {x:0,y:0,w:state.mapWidth,h:state.mapHeight};
    updateView();
  }

  function zoom(factor) {
    if (!state.view) return;
    const view = state.view;
    const width = Math.min(state.mapWidth*1.15,Math.max(state.mapWidth/7,view.w*factor));
    const height = width*state.mapHeight/state.mapWidth;
    state.view = {x:view.x+(view.w-width)/2,y:view.y+(view.h-height)/2,w:width,h:height};
    updateView();
  }

  function updateScale() {
    if (!state.template || !state.view) return;
    const ctm = ui['route-map'].getScreenCTM();
    if (!ctm) return;
    const pixelsPerMetre = ctm.a*state.mapWidth/(state.template.map.bounds[2]-state.template.map.bounds[0]);
    const length = [10,20,50,100,200].filter(value => value*pixelsPerMetre < 135).at(-1) || 10;
    ui['scale-label'].textContent = `${length} m`;
    ui['scale-line'].style.width = `${length*pixelsPerMetre}px`;
  }

  function mapPoint(event) {
    const matrix = ui['route-map'].getScreenCTM();
    if (!matrix) return null;
    return new DOMPoint(event.clientX,event.clientY).matrixTransform(matrix.inverse());
  }

  function nearestDistance(point) {
    let best = {distance:Infinity,along:0};
    const route = state.template.map.route;
    for (let i = 1; i < route.length; i++) {
      const a = toMap(route[i-1]), b = toMap(route[i]);
      const dx = b[0]-a[0], dy = b[1]-a[1];
      const t = Math.max(0,Math.min(1,((point.x-a[0])*dx+(point.y-a[1])*dy)/(dx*dx+dy*dy || 1)));
      const distance = (point.x-a[0]-t*dx)**2+(point.y-a[1]-t*dy)**2;
      if (distance < best.distance) best = {distance,along:route[i-1][2]+t*(route[i][2]-route[i-1][2])};
    }
    return Math.round(best.along);
  }

  function setPick(endpoint) {
    state.pick = state.pick === endpoint ? null : endpoint;
    ui['pick-start'].setAttribute('aria-pressed', String(state.pick === 'start'));
    ui['pick-end'].setAttribute('aria-pressed', String(state.pick === 'end'));
    ui['route-map'].classList.toggle('picking', Boolean(state.pick));
    ui['map-help'].textContent = state.pick ? `${state.pick === 'start' ? 'Start' : 'Ziel'}: auf die Straße klicken. Die Position rastet am Straßenverlauf ein.` : 'Karte ziehen zum Verschieben. Mit + und − vergrößern.';
    if (state.pick && window.innerWidth <= 850) ui['map-shell'].scrollIntoView({block:'center',behavior:'auto'});
  }

  function updateVegetation() {
    ui['vegetation-value'].textContent = `${Math.round(Number(ui['vegetation-density'].value)*100)} %`;
  }

  function updateSubmit() {
    const active = state.jobs.some(job => ['queued','running'].includes(job.status));
    ui['generate-button'].disabled = !state.template || state.posting || active;
    ui['generate-button'].replaceChildren(document.createTextNode(state.posting ? 'Wird gestartet …' : active ? 'Strecke wird erstellt …' : 'Strecke generieren'), element('span','',active || state.posting ? '' : '→'));
    ui['generate-button'].lastElementChild.setAttribute('aria-hidden','true');
  }

  function dateLabel(value) {
    const date = new Date(value);
    return Number.isNaN(date.getTime()) ? '' : new Intl.DateTimeFormat('de-DE',{day:'2-digit',month:'2-digit',hour:'2-digit',minute:'2-digit'}).format(date);
  }

  function renderJobs() {
    ui['jobs-count'].textContent = state.jobs.length ? `${state.jobs.length} ${state.jobs.length === 1 ? 'Export' : 'Exporte'}` : 'Noch keine Exporte';
    if (!state.jobs.length) {
      ui['job-list'].replaceChildren(element('p','empty-state','Deine erzeugten Strecken erscheinen hier. Wähle oben einen Abschnitt und starte den Export.'));
      ui['job-detail'].hidden = true;
      updateSubmit();
      return;
    }
    if (!state.jobs.some(job => job.id === state.selectedJob)) state.selectedJob = state.jobs.find(job => ['running','queued'].includes(job.status))?.id || state.jobs[0].id;
    const rows = state.jobs.map(job => {
      const button = element('button',`job-row${job.id === state.selectedJob ? ' selected' : ''}`);
      button.type = 'button';
      button.setAttribute('aria-pressed',String(job.id === state.selectedJob));
      button.append(element('span','job-row-name',job.name || 'Unbenannte Strecke'));
      const meta = element('span','job-row-meta');
      meta.append(element('span','',dateLabel(job.created_at)),element('span',job.status === 'succeeded' ? 'success' : job.status === 'failed' ? 'failed' : '',statuses[job.status] || 'Unbekannt'));
      button.append(meta);
      button.addEventListener('click',() => {state.selectedJob=job.id;renderJobs();});
      return button;
    });
    const focusId = document.activeElement?.closest('.job-row')?.dataset.jobId;
    rows.forEach((row,index) => {row.dataset.jobId=state.jobs[index].id;});
    ui['job-list'].replaceChildren(...rows);
    if (focusId) rows.find(row => row.dataset.jobId === focusId)?.focus({preventScroll:true});
    renderDetail(state.jobs.find(job => job.id === state.selectedJob));
    updateSubmit();
  }

  function renderDetail(job) {
    if (!job) {ui['job-detail'].hidden=true;return;}
    ui['job-detail'].hidden=false;
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
    if (state.polling) return;
    state.polling=true;
    clearTimeout(state.timer);
    try {
      if (!state.template) {
        const catalog=await api('/api/catalog');
        if (!Array.isArray(catalog.templates) || !catalog.templates.length) throw new Error('Es ist derzeit keine vorbereitete Region verfügbar.');
        loadTemplate(catalog.templates[0]);
      }
      const data=await api('/api/jobs');
      if (!Array.isArray(data.jobs)) throw new Error('Die Exportliste konnte nicht geladen werden.');
      state.jobs=data.jobs.filter(job=>job && typeof job.id==='string').sort((a,b)=>new Date(b.created_at)-new Date(a.created_at));
      // A list request can begin just before a new export is accepted.
      if (state.lastCreatedJob && !state.jobs.some(job=>job.id===state.lastCreatedJob.id)) state.jobs.unshift(state.lastCreatedJob);
      else if (state.lastCreatedJob) state.lastCreatedJob=null;
      renderJobs();
      state.failures=0;
      connection(true);
      ui['global-error'].hidden=true;
    } catch(error) {
      state.failures++;
      connection(false,state.template ? 'Verbindung unterbrochen' : 'Dienst nicht erreichbar');
      ui['global-error'].textContent=errorMessage(error);
      ui['global-error'].hidden=false;
      if (!state.template) ui['map-loading'].textContent='Die Karte wird geladen, sobald der lokale Dienst wieder erreichbar ist.';
    } finally {
      state.polling=false;
      const active=state.jobs.some(job=>['queued','running'].includes(job.status));
      state.timer=setTimeout(refresh,state.failures ? Math.min(30000,3000*state.failures) : active ? 2000 : 8000);
    }
  }

  // Normalize numeric fields before the explicit native validity check below.
  ui['track-form'].noValidate=true;
  ui['track-form'].addEventListener('submit',async event=>{
    event.preventDefault();
    if (state.posting || !state.template || state.jobs.some(job=>['queued','running'].includes(job.status))) return;
    setDistances(state.lastEditedEndpoint);
    if (!ui['track-form'].reportValidity()) return;
    const values=formValues();
    if (!values.name || values.end_m-values.start_m<state.template.minimum) {ui['form-error'].textContent='Bitte einen Namen und einen ausreichend langen Abschnitt angeben.';ui['form-error'].hidden=false;return;}
    state.posting=true;
    updateSubmit();
    ui['form-error'].hidden=true;
    persistForm();
    try {
      const job=await api('/api/jobs',{method:'POST',body:JSON.stringify(values)});
      if (!job || typeof job.id!=='string') throw new Error('Der Dienst hat keine gültige Exportnummer zurückgegeben. Prüfe die Exportliste vor einem erneuten Start.');
      state.lastCreatedJob=job;
      state.jobs=[job,...state.jobs.filter(existing=>existing.id!==job.id)];
      state.selectedJob=job.id;
      renderJobs();
      ui['job-detail'].scrollIntoView({block:'nearest',behavior:'auto'});
      connection(true);
      clearTimeout(state.timer);
      state.timer=setTimeout(refresh,500);
    } catch(error) {
      ui['form-error'].textContent=errorMessage(error);
      ui['form-error'].hidden=false;
      clearTimeout(state.timer);
      state.timer=setTimeout(refresh,500);
    } finally {state.posting=false;updateSubmit();}
  });

  for (const endpoint of ['start','end']) {
    ui[`${endpoint}-distance`].addEventListener('input',()=>previewDistances(endpoint));
    ui[`${endpoint}-distance`].addEventListener('change',()=>setDistances(endpoint));
    ui[`${endpoint}-distance`].addEventListener('blur',()=>setDistances(endpoint));
    ui[`${endpoint}-slider`].addEventListener('input',()=>{ui[`${endpoint}-distance`].value=ui[`${endpoint}-slider`].value;setDistances(endpoint);});
    ui[`pick-${endpoint}`].addEventListener('click',()=>setPick(endpoint));
  }
  ui['reset-section'].addEventListener('click',()=>{ui['start-distance'].value=0;ui['end-distance'].value=state.template.total;setDistances('end');});
  ui['track-name'].addEventListener('change',persistForm);
  ui['road-width'].addEventListener('change',persistForm);
  ui['scenery-quality'].addEventListener('change',persistForm);
  ui['vegetation-density'].addEventListener('input',()=>{updateVegetation();persistForm();});
  $('ai-materials').addEventListener('change',persistForm);
  ui['fit-map'].addEventListener('click',fitMap);
  ui['zoom-in'].addEventListener('click',()=>zoom(0.7));
  ui['zoom-out'].addEventListener('click',()=>zoom(1/0.7));
  ui['retry-connection'].addEventListener('click',refresh);
  ui['job-preview'].addEventListener('error',()=>{ui['job-preview'].hidden=true;});
  ui['route-map'].addEventListener('pointerdown',event=>{
    if (!state.template || event.button!==0) return;
    const point=mapPoint(event);
    if (!point) return;
    state.drag={id:event.pointerId,x:event.clientX,y:event.clientY,point,view:{...state.view},moved:false};
    ui['route-map'].setPointerCapture(event.pointerId);
  });
  ui['route-map'].addEventListener('pointermove',event=>{
    const drag=state.drag;
    if (!drag || event.pointerId!==drag.id) return;
    if (Math.hypot(event.clientX-drag.x,event.clientY-drag.y)>5) drag.moved=true;
    if (!drag.moved) return;
    const point=mapPoint(event);
    if (!point) return;
    state.view.x+=drag.point.x-point.x;
    state.view.y+=drag.point.y-point.y;
    state.view.x=Math.max(-state.view.w*.4,Math.min(state.mapWidth-state.view.w*.6,state.view.x));
    state.view.y=Math.max(-state.view.h*.4,Math.min(state.mapHeight-state.view.h*.6,state.view.y));
    ui['route-map'].classList.add('dragging');
    updateView();
  });
  ui['route-map'].addEventListener('pointerup',event=>{
    const drag=state.drag;
    if (!drag || event.pointerId!==drag.id) return;
    const point=mapPoint(event);
    if (!drag.moved && state.pick && point) {
      const endpoint=state.pick;
      ui[`${endpoint}-distance`].value=nearestDistance(point);
      setDistances(endpoint);
      setPick(endpoint);
    }
    state.drag=null;
    ui['route-map'].classList.remove('dragging');
    if (ui['route-map'].hasPointerCapture(event.pointerId)) ui['route-map'].releasePointerCapture(event.pointerId);
  });
  ui['route-map'].addEventListener('pointercancel',()=>{state.drag=null;ui['route-map'].classList.remove('dragging');});
  window.addEventListener('resize',()=>{updateMarkers();updateScale();});
  window.addEventListener('online',refresh);
  document.addEventListener('visibilitychange',()=>{if (!document.hidden) refresh();});
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
