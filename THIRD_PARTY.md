# Sources and third-party references — version 0.11.0

The KN5 serialization layout in `roadtoassetto/kn5.py` was implemented using
the reference `scripts/export_kn5.py` from:

https://github.com/KinG-InFeT/blender-assetto-corsa-track-generator

License: Apache License 2.0. That reference identifies RaduMC/kn5-converter
as its binary-format reference. A copy of the Apache license is included
in `licenses/Apache-2.0.txt`. This project's writer is adapted to a standalone
NumPy scene, validates index limits, calculates normals/tangents, and exports
nonrendered AC helper nodes without visible dummy cubes.

OpenStreetMap roads, paths, building footprints and mapped vegetation:
(c) OpenStreetMap contributors, ODbL 1.0.
https://www.openstreetmap.org/copyright

Hessian DGM1, optional DOM1, DOP20 and INSPIRE LoD2: HVBG Hessen, Datenlizenz Deutschland - Zero 2.0.
https://hvbg.hessen.de/geoinformation/open-data

DOM1 was obtained as the bounded 919 x 651 metre extract from the official
https://inspire-hessen.de/raster/dom1/ows service. DOM1 and DGM1 use the same
1-metre EPSG:25832 grid and DHHN2016_NH height reference; the download and hash
are recorded in `data/altenhain/dom1_source.json`. Smoothed DOM1-DGM1 peaks with
DOP20 RGBI vegetation evidence estimate canopy height within 80 m of the route.
This is not an individual-tree survey; dates, crown radius and species remain
limitations. Missing or unusable optional DOM1 retains the RGB-only fallback.

PaintedPlaster006 (color and normal maps): ambientCG / Lennart Demes, CC0 1.0.
https://ambientcg.com/view?id=PaintedPlaster006
https://docs.ambientcg.com/license/

Version 0.11.0 retains the following openly licensed assets introduced in 0.4.
The individual download URLs, SHA256 hashes and source metadata are cached
alongside their source files under `data/altenhain/materials/`.

| Asset | Creator / provider | License and use |
| --- | --- | --- |
| [Asphalt023S](https://ambientcg.com/view?id=Asphalt023S) | Lennart Demes / ambientCG | CC0-1.0; photogrammetric asphalt, 1.25 m repeat, colour and DirectX normal |
| [Grass004](https://ambientcg.com/view?id=Grass004) | Lennart Demes / ambientCG | CC0-1.0; procedural grass material, 1.4 m repeat; not a photograph |
| [Gravel022](https://ambientcg.com/view?id=Gravel022) | Lennart Demes / ambientCG | CC0-1.0; procedural gravel material, 1.5 m repeat; not a photograph |
| [Roof Tiles](https://polyhaven.com/a/roof_tiles) | Stephan Seeliger / Poly Haven | CC0-1.0; generic photographic red roof material and DirectX normal |
| [Grey Roof 01](https://polyhaven.com/a/grey_roof_01) | Rob Tuytel / Poly Haven | CC0-1.0; generic photographic grey roof material and DirectX normal |
| [Tree Small 02](https://polyhaven.com/a/tree_small_02) | Rico Cilliers / Poly Haven | CC0-1.0; photographed leaf diffuse and opacity atlas, cropped and assembled into new transparent twig cards |
| [Tree Bark 03](https://polyhaven.com/a/tree_bark_03) | Poly Haven; source asset page | CC0-1.0; photographic bark colour and DirectX normal |

Provider license pages: https://docs.ambientcg.com/license/ and
https://polyhaven.com/license . Powered by Poly Haven. The original downloadable
asset textures are used; provider website renders, logos and avatars are not
included as track assets. Photographic materials are generic surface references,
not photographs of the corresponding roads, roofs or trees in Altenhain.

One local facade photograph is registered to one matching official LoD2 wall:
[Altenhain, Langstraße 39](https://commons.wikimedia.org/wiki/File:Altenhain,_Langstra%C3%9Fe_39.JPG),
Karsten Ratzke, photographed 15 January 2012, CC0-1.0. The image has been
perspective-rectified for the north wall of `Building_DEHE062000009j37`.
Its author, date, source link, license and wall association are recorded in
`data/altenhain/facade_overrides.json` and `research/realism-v04.md`; original
Commons metadata and the used original photograph accompany the source archive.
The photo documents the 2012 appearance and contains photographic lighting.

Other facades use the CC0 plaster material as restrained fine surface detail,
with eight muted colour variants and inferred window/shutter details.
Near walls include geometric frames, sills, shutters and glass representations using the
synthetic window atlas described below; a procedural illustration remains the
fallback when that atlas is absent. Modelled door reveals, frames, thresholds
and hardware add depth around synthetic door leaves. Window positions, floor
layouts, entrances, thresholds, gutters and tile choice remain visual estimates.
Building roof shapes/heights come from official
LoD2 data. Near pitched roofs receive metric tile textures; other roofs retain
the native 20-cm DOP20 aerial extract. Version 0.9 terrain instead uses the
unchanged original Grass004, Gravel022 and Asphalt023S colour images at their
1.4/1.5/1.25-metre scales. RGBI supplies inferred vegetation/mineral/impervious
material weights; a weak broad reflectance field uses land-cover evidence,
not photographic brightness. These generated shader-control rasters avoid
projecting aerial cars, roofs and baked shadows onto ground slopes. They do
not establish surveyed gardens, soil types or courtyard paving. Grass004 and
Gravel022 remain procedural source assets, not local photography. The stock
ksMultilayer colour shader and Blender preview use the same blend; terrain
normal mapping is not enabled. Original DOP imagery and CC0 colour files are
not rewritten by the terrain-material process.

Version 0.9 also separates generic mineral-paint families from window styles,
adds metric plinth bands and downpipes, and refines recessed window frames and
sloped sills. Paint, plinth height and architectural details remain inferred;
official LoD2 wall/roof geometry and the registered local photograph remain
unchanged. More detailed generic materials do not constitute a photorealistic
or surveyed reconstruction of the village.

The 0.5.0 window atlas was generated with the built-in OpenAI ImageGen tool on
21 October 2026. `data/altenhain/materials/AIWindows/window_atlas.png` contains
eight synthetic glass, curtain and interior variants. The exact prompt is in
`prompt.txt`; `provenance.json` records the tool, date, image hash and limitations.
These are generic generated images, not photographs or observations of any
Altenhain building. The atlas is included in the source cache and works without
configuring a separate user API. It is not represented as a CC0 photographic
asset from ambientCG, Poly Haven or the local facade collection.

Version 0.6.0 adds an eight-variant synthetic door-leaf atlas, generated with
the built-in OpenAI ImageGen tool on 21 October 2026. It is stored at
`data/altenhain/materials/AIDoors/door_atlas.png`; the adjacent `prompt.txt`
and `provenance.json` record the exact request, tool, date, image SHA256 and
limitations. These generic German village doors are visual artwork, not
photographs of, or evidence about, the doors of particular Altenhain buildings.
The supplied atlas needs no user API configuration and is not described as
CC0 photography. Registered real facade photography remains separately attributed.

The optional user-configured Images-API integration requests three generic
material albedos: plaster, roof tiles and asphalt. `webapp/ai.py` contains the
fixed prompts. Generated packs carry `ai_materials.json` provenance, including
their synthetic status and provider/model information, and are cached locally.
Normal maps derived from those images are approximate bump maps, not measured
surface scans. The integration sends no route coordinates, local facade photos
or project documents to the provider and preserves the registered real facade
and scene geometry. Generated images do not inherit the CC0 license of unrelated
source assets; use depends on the selected provider's terms. No paid live API
generation was performed for verification; 13 local mock-provider tests and
10 integration tests cover the implemented connection and material contracts.
The web UI supports a configurable OpenAI-compatible image endpoint, model and
API key. A ChatGPT login is not implemented.

Paving, kerb and repair imagery and the branch skeletons are original
procedurally generated assets. Tree/shrub positions combine mapped OSM features, optional DOM1-DGM1
canopy peaks and an interpretation of DOP20 colour and local contrast. Source
counts distinguish DOM trees, OSM trees and visually inferred vegetation. Tree
types and fallback heights, continuous pavement
stretches, short house-foot paved aprons, retaining edges and asphalt repairs
are estimates; repairs do not represent a survey
of real damage. All mapped OSM highways and LoD2/OSM building footprints are
reserved for clearance. Decorative buildings, vegetation, kerbs and pavement
do not add collision.

Version 0.6.0 deliberately changes the driving geometry. The centreline and
variable width reference in `data/altenhain/road_alignment.json` combine OSM
with manual interpretation of 31 cross-sections of the cached HVBG DOP20 image.
The reference records image/OSM hashes, edge picks, uncertainty, obscured
fallback sections and reviewed junction control pixels. The road profile
ranges from 4.4 to 6.6 metres at its natural scale; the UI's nominal 6-m setting
now selects that natural scale. Smaller nominal settings scale the profile.
`road_alignment_review.json` and the companion PNGs document this interpretation.
It is not ground survey data. The 20-cm pixel spacing is not an accuracy claim;
the stated approximately 0.5/0.8/1.5-m tolerances are interpretation assumptions
for visible edges, obscured edges and the open junction, not independently
measured error bounds. Version 0.6 used a flat cross-section.

Version 0.7.0 adds a synthetic symmetric 2.5% road crown, matching shoulders,
terrain tie-in and roadside detail heights. This is a typified construction
assumption, not measured crossfall in Altenhain. The cached OSM `sidewalk`
tags determine eligible sides in each original way's direction, including
`sidewalk=no`. OSM `lane_markings=no` on the K 797 sections supports leaving
out a generic centre line. Current isolated road markings are not fully mapped;
the red school-area marking visible in the 2012 photo is not added as a
purported current marking. Version 0.8 adds individually reviewed white paint
features from the cached HVBG DOP20 image: crossing bars, a zigzag and separate
guidance dashes. `data/altenhain/marking_observations.json` records coordinates,
confidence and source hash; a changed source hash requires review before use.
The Kirchstrasse mouth outline is separately reviewed in
`data/altenhain/junction_observations.json`. Evidence crops and overlays are
under `research/markings-v08/`. The aerial extract was obtained on 21 October
2026 and reviewed on 22 October; its acquisition/flight date is unknown.
Estimated reading uncertainty is about 0.35-0.4 m; paint widths below the
20-cm pixel size are estimates. This is image interpretation, not survey data.
Paint is original generated warm-white surface detail clipped onto actual
exported asphalt triangles; no generic route-wide centre line is invented.

New one-metre concrete kerbs with bevels, 20x10-cm pavers, two-row stone
channels, small roadside grates, resealed joints, irregular asphalt patches
and circular utility covers are original generated geometry/material detail.
The kerb reveal, sidewalk width, drain positions and construction details are
inferred, not surveyed. The approximately 12-cm kerb reveal must not be confused
with the full buried height of a manufactured kerbstone. Road wear conforms
to the actual exported road triangles; these additions have no collision.
Short visual side-road mouths use shared-node OSM topology and original
side-road centreline directions, extending at most eight metres; absent width
tags are estimated. They do not create additional playable routes.

Historical local material guidance uses the already cached CC0 photographs of
[Langstrasse 39](https://commons.wikimedia.org/wiki/File:Altenhain,_Langstra%C3%9Fe_39.JPG)
and [Langstrasse 13](https://commons.wikimedia.org/wiki/File:Altenhain,_Langstra%C3%9Fe_13,_Vorderbau.JPG),
both Karsten Ratzke, 15 January 2012. Red-brown pavers and a dark asphalt sidewalk
are interpreted only near the corresponding building and on its side of the
road. The generated small-format paving is not an exact reconstruction of the
interlocking stone shape in the Nr.39 photograph. These references do not
establish current materials, exact borders, or the positions of road furniture.

Primary manufacturer/municipal references for plausible sizes and construction
are documented in [research/german-streets-v07.md](research/german-streets-v07.md):
HACO kerbstone dimensions, KANN 20x10-cm paving, MeierGuss drainage grates, ACO
utility-cover dimensions and Hamburg ReStra drainage/crossfall examples.
Only factual design references are used. No catalogue product images,
certification marks or municipal emblems are copied into textures.

DGM1 elevations are sampled at 1-m intervals in the 18-m near-road corridor;
more distant ground keeps the configured coarser spacing. Road grading is
limited to its shoulder plus a 2-m blending band. Exported terrain pieces share
global vertex normals. In version 0.8, paved surfaces, terrain tie-in and house
contacts share explicit UTM vertices, heights and triangle diagonals. Sidewalk
widths follow nearby source building edges up to 2.8 m with bounded longitudinal
changes and eased ends; open-land sections retain a conservative inferred width.
Small connections join suitable LoD2 wall bottoms to accepted sidewalk back
edges, with at most two shallow steps for limited height differences. Bases
use the actual exported terrain. The terrain is locally graded beneath these
accepted surfaces and returns to the DGM in a short outside blending strip;
this is modelled construction, not additional measured terrain resolution.
Terrain construction uses one shared noded edge network and constrained
triangulation, with a 2-mm numerical topology grid to avoid disconnected
intersections and float32 slivers. That grid is not a claim of geographic
accuracy or DGM resolution; visible road geometry is not snapped or moved.
Road and side-street reservations and neighbouring buildings are excluded.
These additions are noncolliding scenery; their appearance does not establish
actual cadastral boundaries, accesses or construction heights.

Version 0.9 adds inferred wall-foot gravel strips up to 0.45 m wide at suitable
original LoD2 lower edges, limited to 0.45 m local cut and 0.30 m fill. Public
paving, roads and neighbouring buildings are reserved. A fixed outer source
DGM height and monotone straight transition reduce artificial waves behind
sidewalks without moving the road. These narrow locally graded supports and
their decorative gravel surfaces are construction assumptions. The nine
individually observed DOP20 paint features remain source-gated and projected
onto actual asphalt triangles; no continuous centre line is invented.
In the historical 0.9 build, some larger DGM hillside/LoD2 wall-base differences remained unresolved near
route stations 533-544 m, including local differences greater than one metre.
The bounded contact work is not a reconstruction of unknown retaining walls,
terraces or private entries. The observed limitations and further evidence
needed are recorded in `test-results/ground-limitations-v09.json`.

Version 0.10 adds inferred hillside terraces, retaining-wall bodies and coping,
connected stair flights/landings, side footpaths and suitable visual driveway
connections. Long steep roadside runs near houses on the same side can receive
up to three retaining tiers with gravel benches and ends tied into the DGM.
Only accepted local construction areas are regraded; this does not resolve
every discrepancy or incomplete building underside in the source LoD2 model. Their
plans use the lowest official LoD2 wall edges and existing DGM/road grades;
locations, dimensions and materials are generated construction hypotheses, not
surveyed property infrastructure. Terrain is generated from the same plan as
the visible surfaces. Original road and building coordinates are retained.
The masonry, mortar, concrete-coping and metal shader images are original
procedural project assets generated by `roadtoassetto/hillside_render.py`;
they do not edit or incorporate new third-party photographs. Existing pavement
and asphalt materials keep their provenance documented above.

No Google imagery or proprietary track meshes are used.
Version 0.11 adds separate inferred basement shells, terrain-following plinths
and underside surfaces from the original LoD2 ground faces. The walls retain
the source lower exterior edges and extend inward with a buried foot tied to
actual exported terrain triangles. No original wall or roof is relocated.
The larger basement surfaces reuse the existing CC0 fine-plaster texture with
a matching mineral-paint multiplier; no new third-party imagery is introduced.
These additions are visual construction hypotheses, not observed basement
architecture. Road and neighbouring building reservations constrain generation;
gaps greater than six metres are rejected. Plans and provenance are saved in
`data/altenhain/building_bases.json` and the generated manifest.

Assetto Corsa material interfaces were checked against the locally installed SDK.
No proprietary Kunos shader binaries, disassembly, textures or models are included
in either archive; the track refers to standard shader names supplied by the game.
The web UI uses Leaflet 1.9.4 (BSD-2-Clause), vendored from the official
distribution with its license at `webapp/static/vendor/leaflet/LICENSE`.
https://leafletjs.com/ and https://github.com/Leaflet/Leaflet/tree/v1.9.4
It displays OpenStreetMap tiles with visible attribution and normal browser
caching. The prepared Altenhain aerial image remains an optional layer.
Explicit address searches use Nominatim; bounded road/context requests use
Overpass. Their caches and the Hessian DGM1/DOP20/LoD2/DOM1 downloads remain
server-side. New route exports include the relevant OSM extract, route IDs,
source licenses, acquisition dates and hashes in `provenance/`.
https://operations.osmfoundation.org/policies/tiles/
https://operations.osmfoundation.org/policies/nominatim/
The HTTP server uses Python's standard library;
the frontend is served as local static HTML, CSS and JavaScript. Provider
requests use the existing `requests` dependency. API settings and runtime image
caches remain in `web-data/`, or the persistent `/data` volume under Docker.
API keys are excluded from public settings responses and track/source archives.

Blender is downloaded from its official distribution into `.tools/`; it is an
optional local build tool, not included in the track ZIP.
It is also excluded from the source ZIP. Without Blender, the local web builder
uses an aerial route preview while retaining the KN5 export. Local web job
history, generated per-job files, user API settings/caches and private game-setting
backups are excluded from the source archive. The 0.11.0 source archive includes
the prepared geographic/material cache, window and door atlases with their
prompts/provenance, the reviewed road reference, and Docker/Compose build files.
See [DOCKER.md](DOCKER.md) for the
container runtime and its separately downloaded Blender distribution.
