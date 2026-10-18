"""Build a self-contained QA map (Leaflet) of a processed batch for visual review.

Usage: python tools/qa_map.py data/batches/pilot_001 [out.html]
The basemap is for human viewing only; it is never used as AI input.
"""

import json
import os
import sys

import pyogrio

TEMPLATE = """<!doctype html><html><head><meta charset="utf-8"><title>Batch QA map</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<link rel="stylesheet" href="https://cdn.jsdelivr.net/npm/leaflet@1.9.4/dist/leaflet.css">
<script src="https://cdn.jsdelivr.net/npm/leaflet@1.9.4/dist/leaflet.js"></script>
<style>html,body{margin:0;height:100%;font:13px system-ui,sans-serif}#map{position:absolute;left:340px;right:0;top:0;bottom:0}
#side{position:absolute;left:0;top:0;bottom:0;width:340px;overflow:auto;border-right:1px solid #ccc;background:#fafafa}
.r{padding:6px 8px;border-bottom:1px solid #e5e5e5;cursor:pointer}.r:hover,.r.sel{background:#e8f0fe}
.b{font-weight:600}.m{color:#555;font-size:12px}.CLEAR{color:#137333}.AMBIGUOUS{color:#b06000}
#info{position:absolute;right:8px;top:8px;z-index:999;background:#fff;padding:8px;max-width:360px;border-radius:6px;box-shadow:0 1px 4px #0005;font-size:12px}
</style></head><body><div id="side"></div><div id="map"></div><div id="info">Select a record</div>
<script>
const D = __DATA__;
const map = L.map('map');
L.tileLayer('https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}',
  {maxZoom: 21, maxNativeZoom: 19, attribution: 'Imagery &copy; Esri (viewing only)'}).addTo(map);
let layer = L.layerGroup().addTo(map);
function show(i){
  const r = D.records[i]; layer.clearLayers();
  document.querySelectorAll('.r').forEach((e,k)=>e.classList.toggle('sel', k===i));
  (D.cands[r.survey_id]||[]).forEach(c=>{
    const g = L.geoJSON(c.geometry,{style:{color:c.properties.chosen?'#ffd400':'#00e5ff',weight:c.properties.chosen?3:1.5,fillOpacity:0.08}});
    g.bindTooltip('#'+c.properties.rank+' '+c.properties.score+' '+c.properties.dataset); g.addTo(layer);});
  if (r.proposal) L.geoJSON(r.proposal,{style:{color:'#ff3b30',weight:2,fill:false,dashArray:'4 3'}}).addTo(layer);
  L.circleMarker([r.lat,r.lon],{radius:6,color:'#fff',weight:2,fillColor:'#e91e63',fillOpacity:1}).addTo(layer);
  L.circle([r.lat,r.lon],{radius:r.radius||50,color:'#fff',weight:1,fill:false,dashArray:'2 4'}).addTo(layer);
  if (r.corr) L.circleMarker(r.corr,{radius:4,color:'#000',fillColor:'#76ff03',fillOpacity:1}).addTo(layer);
  map.setView([r.lat,r.lon], 19);
  document.getElementById('info').innerHTML = '<b>'+r.survey_id+' '+r.name+'</b><br>'+r.addr+'<br>'+r.decision+' conf '+r.conf+
   '<br>'+r.reason+'<br>'+(r.flags||'')+'<br>'+(r.evidence||'');
}
const side = document.getElementById('side');
D.records.forEach((r,i)=>{const d=document.createElement('div');d.className='r';
 d.innerHTML='<span class="b">'+r.survey_id+'</span> '+r.name+'<div class="m"><span class="'+(r.decision||'').replace('IDENTITY_','')+'">'+(r.decision||'')+'</span> conf '+r.conf+' · '+r.ncand+' cands</div>';
 d.onclick=()=>show(i); side.appendChild(d);});
show(0);
document.addEventListener('keydown',e=>{const s=[...document.querySelectorAll('.r')].findIndex(x=>x.classList.contains('sel'));
 if(e.key==='ArrowDown'&&s<D.records.length-1)show(s+1); if(e.key==='ArrowUp'&&s>0)show(s-1);});
</script></body></html>"""


def main(batch_dir, out=None):
    work = os.path.join(batch_dir, "work.gpkg")
    recs = pyogrio.read_dataframe(work, layer="survey_record")
    props = pyogrio.read_dataframe(work, layer="proposal")
    cands = pyogrio.read_dataframe(work, layer="candidate")
    pmap = {r["survey_id"]: r for _, r in props.iterrows()}
    cmap = {}
    for _, c in cands.iterrows():
        cmap.setdefault(c["survey_id"], []).append({
            "geometry": c.geometry.__geo_interface__,
            "properties": {"rank": int(c["rank"]), "score": round(float(c["score"]), 3), "dataset": c["dataset"],
                           "chosen": False, "candidate_id": c["candidate_id"]}})
    out_recs = []
    for _, r in recs.iterrows():
        f = json.loads(r["fields_json"])
        p = pmap.get(r["survey_id"])
        chosen = p["chosen_candidate_id"] if p is not None else None
        for c in cmap.get(r["survey_id"], []):
            c["properties"]["chosen"] = c["properties"]["candidate_id"] == chosen
        ev = ""
        cand_rows = cands[(cands["survey_id"] == r["survey_id"]) & (cands["candidate_id"] == chosen)]
        if len(cand_rows):
            cr = cand_rows.iloc[0]
            ev = "s_dist %.2f s_name %.2f s_addr %.2f s_road %.2f s_plaus %.2f s_source %.2f<br>%s" % (
                cr["s_dist"], cr["s_name"], cr["s_addr"], cr["s_road"], cr["s_plaus"], cr["s_source"],
                cr["evidence_json"][:400])
        out_recs.append({
            "survey_id": r["survey_id"], "name": f.get("building_name", ""),
            "addr": ", ".join(x for x in [f.get("street"), f.get("area"), f.get("pincode")] if x),
            "lat": float(r["latitude"]), "lon": float(r["longitude"]),
            "decision": p["decision"] if p is not None else "", "conf": round(float(p["confidence"]), 3) if p is not None else "",
            "reason": "", "flags": (p["flags"] + " " + p["notes"]) if p is not None else "",
            "proposal": p.geometry.__geo_interface__ if p is not None and p.geometry is not None else None,
            "corr": [float(p["corrected_lat"]), float(p["corrected_lon"])] if p is not None and p["corrected_lat"] == p["corrected_lat"] else None,
            "ncand": len(cmap.get(r["survey_id"], [])), "evidence": ev, "radius": 50,
        })
    html = TEMPLATE.replace("__DATA__", json.dumps({"records": out_recs, "cands": cmap}))
    out = out or os.path.join(batch_dir, "output", "qa_map.html")
    with open(out, "w", encoding="utf-8") as fh:
        fh.write(html)
    print(out)


if __name__ == "__main__":
    main(*sys.argv[1:])
