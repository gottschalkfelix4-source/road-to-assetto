'use strict';

// Geographic display only. Route validation and export stay on the server.
window.RoadMap = class RoadMap {
  constructor(container, onPoint, onTileError, config={}) {
    if (!window.L) throw new Error('Die Kartenbibliothek konnte nicht geladen werden. Bitte die Seite neu laden.');
    this.map = L.map(container, {zoomControl:false, preferCanvas:false}).setView([50.159,8.467], 16);
    L.control.zoom({position:'topright',zoomInTitle:'Karte vergrößern',zoomOutTitle:'Karte verkleinern'}).addTo(this.map);
    L.control.scale({imperial:false,position:'bottomleft'}).addTo(this.map);
    this.tileUrl=typeof config.tile_url==='string' && /^https?:\/\//.test(config.tile_url) ? config.tile_url : 'https://tile.openstreetmap.org/{z}/{x}/{y}.png';
    this.tiles = L.tileLayer(this.tileUrl, {
      maxZoom:19, attribution:config.attribution || '&copy; <a href="https://www.openstreetmap.org/copyright" target="_blank" rel="noopener">OpenStreetMap</a> · <a href="https://www.openstreetmap.org/fixthemap" target="_blank" rel="noopener">Kartenfehler melden</a>',
      keepBuffer:2, updateWhenIdle:true
    }).addTo(this.map);
    this.tiles.on('tileerror',()=>onTileError(true));
    this.tiles.on('tileload',()=>onTileError(false));
    this.base = L.polyline([], {color:'#687461',weight:3,dashArray:'5 6',interactive:false}).addTo(this.map);
    this.casing = L.polyline([], {color:'#fff',weight:9,opacity:.9,interactive:false}).addTo(this.map);
    this.line = L.polyline([], {color:'#315c43',weight:5,interactive:false}).addTo(this.map);
    this.markers = {};
    this.pick = null;
    this.mode = 'hessen';
    this.onPoint = onPoint;
    this.map.on('click', event=>{if (this.pick) this.choose(this.pick,event.latlng);});
    this.map.getContainer().setAttribute('aria-label','Interaktive Straßenkarte. Mit Pfeiltasten verschieben, mit Plus und Minus zoomen. Start und Ziel lassen sich auch über die Kartenmitte setzen.');
    this.resizeObserver = new ResizeObserver(()=>this.map.invalidateSize({pan:false}));
    this.resizeObserver.observe(this.map.getContainer());
  }

  configureTiles(config) {
    if (typeof config.tile_url==='string' && /^https?:\/\//.test(config.tile_url) && config.tile_url!==this.tileUrl) {
      this.tileUrl=config.tile_url;this.tiles.setUrl(this.tileUrl);
    }
    if (typeof config.attribution==='string' && config.attribution!==this.tiles.options.attribution) {
      this.map.attributionControl.removeAttribution(this.tiles.options.attribution);
      this.tiles.options.attribution=config.attribution;
      this.map.attributionControl.addAttribution(config.attribution);
    }
  }

  setPrepared(template) {
    this.prepared = template;
    const bounds=template.map.geographic_bounds;
    if (this.aerial) this.map.removeLayer(this.aerial);
    if (Array.isArray(bounds) && bounds.length===4 && template.map.image_url) {
      const url=new URL(template.map.image_url,location.origin);
      if (url.origin===location.origin) this.aerial=L.imageOverlay(url.href,[[bounds[1],bounds[0]],[bounds[3],bounds[2]]],{opacity:1,attribution:'Luftbild: HVBG Hessen · Open Data'});
    }
  }

  setMode(mode) {
    this.mode=mode;
    this.setPick(null);
    this.base.setLatLngs(mode==='altenhain' ? this.prepared?.map.coordinates || [] : []);
    this.setRoute([]);
    for (const marker of Object.values(this.markers)) this.map.removeLayer(marker);
    this.markers={};
    if (mode!=='altenhain') this.showAerial(false);
  }

  showAerial(show) {
    if (!this.aerial) return;
    if (show && this.mode==='altenhain') this.aerial.addTo(this.map);
    else this.map.removeLayer(this.aerial);
  }

  setPick(endpoint) {
    this.pick=endpoint;
    this.map.getContainer().classList.toggle('picking',Boolean(endpoint));
  }

  choose(endpoint, latlng) {
    const point={lat:latlng.lat,lon:latlng.lng};
    if (this.mode==='altenhain') this.onPoint(endpoint,this.nearestChainage(latlng));
    else this.onPoint(endpoint,point);
  }

  chooseCenter(endpoint) { this.choose(endpoint,this.map.getCenter()); }

  setMarker(endpoint, point) {
    if (!point) {if (this.markers[endpoint]) this.map.removeLayer(this.markers[endpoint]);delete this.markers[endpoint];return;}
    const latlng=Array.isArray(point) ? point.slice(0,2) : [point.lat,point.lon];
    if (!this.markers[endpoint]) {
      const label=endpoint==='start' ? 'A' : 'B';
      const marker=L.marker(latlng,{draggable:true,autoPan:true,keyboard:true,title:label==='A' ? 'Start A – ziehen zum Versetzen' : 'Ziel B – ziehen zum Versetzen',
        icon:L.divIcon({className:`route-endpoint endpoint-${endpoint}`,html:`<span>${label}</span>`,iconSize:[32,32],iconAnchor:[16,16]})}).addTo(this.map);
      marker.on('dragend',()=>this.choose(endpoint,marker.getLatLng()));
      this.markers[endpoint]=marker;
    } else this.markers[endpoint].setLatLng(latlng);
  }

  setRoute(points, fit=false) {
    this.line.setLatLngs(points);
    this.casing.setLatLngs(points);
    if (fit && points.length>1) this.map.fitBounds(this.line.getBounds(),{padding:[35,35],maxZoom:18,animate:false});
  }

  fit() {
    let bounds=this.line.getBounds();
    if (!bounds.isValid()) bounds=this.base.getBounds();
    if (!bounds.isValid()) bounds=L.latLngBounds(Object.values(this.markers).map(marker=>marker.getLatLng()));
    if (bounds.isValid()) this.map.fitBounds(bounds,{padding:[35,35],maxZoom:18,animate:false});
  }

  showResult(result) {
    const bounds=result.bbox;
    if (Array.isArray(bounds) && bounds.length===4 && bounds.every(Number.isFinite)) this.map.fitBounds([[bounds[1],bounds[0]],[bounds[3],bounds[2]]],{padding:[40,40],maxZoom:17,animate:false});
    else this.map.setView([result.lat,result.lon],17,{animate:false});
    if (this.searchMarker) this.map.removeLayer(this.searchMarker);
    const label=document.createElement('span');label.textContent=result.label;
    this.searchMarker=L.circleMarker([result.lat,result.lon],{radius:7,color:'#9b542b',weight:2,fillColor:'#fff',fillOpacity:1}).addTo(this.map).bindTooltip(label);
  }

  nearestChainage(latlng) {
    const points=this.prepared?.map.coordinates || [];
    const point=L.CRS.EPSG3857.project(latlng);
    let best={distance:Infinity,chainage:0};
    for (let i=1;i<points.length;i++) {
      const a=L.CRS.EPSG3857.project(L.latLng(points[i-1][0],points[i-1][1]));
      const b=L.CRS.EPSG3857.project(L.latLng(points[i][0],points[i][1]));
      const dx=b.x-a.x,dy=b.y-a.y;
      const t=Math.max(0,Math.min(1,((point.x-a.x)*dx+(point.y-a.y)*dy)/(dx*dx+dy*dy || 1)));
      const distance=(point.x-a.x-t*dx)**2+(point.y-a.y-t*dy)**2;
      if (distance<best.distance) best={distance,chainage:points[i-1][2]+t*(points[i][2]-points[i-1][2])};
    }
    return Math.round(best.chainage);
  }
};
