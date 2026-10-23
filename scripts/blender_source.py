"""Create an editable .blend and render preview from the prepared scene.

Run with Blender --background --python scripts/blender_source.py.
"""
from pathlib import Path
import json
import math
import argparse
import sys
import bpy
from mathutils import Vector, Matrix

ROOT=Path(__file__).resolve().parents[1]
parser=argparse.ArgumentParser()
parser.add_argument('--root',type=Path,default=ROOT)
parser.add_argument('--view',choices=('overview','driver'),default='overview')
parser.add_argument('--distance',type=float,default=200.)
parser.add_argument('--output',type=Path)
parser.add_argument('--preview-only',action='store_true')
parser.add_argument('--samples',type=int,default=32)
parser.add_argument('--gpu',action='store_true',help='Use an available OptiX/CUDA device for local previews')
options=parser.parse_args(sys.argv[sys.argv.index('--')+1:] if '--' in sys.argv else [])
ROOT=options.root.resolve()
cfg=json.loads((ROOT/'tracks/altenhain.json').read_text(encoding='utf-8'))
scene=json.loads((ROOT/cfg['data_dir']/'scene.json').read_text())
track=ROOT/'build'/cfg['id']
source=ROOT/'dist'/(cfg['id']+'-source')
source.mkdir(parents=True,exist_ok=True)
bpy.ops.object.select_all(action='SELECT')
bpy.ops.object.delete(use_global=False)
materials=[]
for mat in scene['materials']:
    m=bpy.data.materials.new(mat['name'])
    m.use_nodes=True
    bsdf=m.node_tree.nodes.get('Principled BSDF')
    image=bpy.data.images.load(str(ROOT/scene['textures_dir']/mat['texture']),check_existing=True)
    texture=m.node_tree.nodes.new('ShaderNodeTexImage')
    texture.image=image
    texture.extension='REPEAT'
    # AC texture coordinates have image origin at top-left; Blender uses bottom-left.
    uv=m.node_tree.nodes.new('ShaderNodeTexCoord')
    mapping=m.node_tree.nodes.new('ShaderNodeVectorMath'); mapping.operation='MULTIPLY_ADD'
    mapping.inputs[1].default_value=(1,-1,1); mapping.inputs[2].default_value=(0,1,0)
    m.node_tree.links.new(uv.outputs['UV'],mapping.inputs[0])
    m.node_tree.links.new(mapping.outputs[0],texture.inputs['Vector'])
    m.node_tree.links.new(texture.outputs['Color'],bsdf.inputs['Base Color'])
    if mat.get('shader')=='ksMultilayer':
        # Match the stock AC world-XZ multilayer surface in the preview.
        geometry=m.node_tree.nodes.new('ShaderNodeNewGeometry')
        mask=m.node_tree.nodes.new('ShaderNodeTexImage')
        mask.image=bpy.data.images.load(str(ROOT/scene['textures_dir']/mat['samplers']['txMask']),check_existing=True)
        mask.image.colorspace_settings.name='Non-Color'
        mask.image.alpha_mode='CHANNEL_PACKED'
        m.node_tree.links.new(mapping.outputs[0],mask.inputs['Vector'])
        channels=m.node_tree.nodes.new('ShaderNodeSeparateColor');channels.mode='RGB'
        m.node_tree.links.new(mask.outputs['Color'],channels.inputs[0])
        accumulated=None
        for channel,index in [('R',0),('G',1),('B',2)]:
            detail=m.node_tree.nodes.new('ShaderNodeTexImage')
            detail.image=bpy.data.images.load(str(ROOT/scene['textures_dir']/mat['samplers']['txDetail'+channel]),check_existing=True)
            detail.image.colorspace_settings.name=mat.get('detail_color_space','Non-Color')
            scale=m.node_tree.nodes.new('ShaderNodeVectorMath');scale.operation='MULTIPLY'
            multiplier=mat['shader_properties']['mult'+channel]
            scale.inputs[1].default_value=(multiplier,-multiplier,1.)
            m.node_tree.links.new(geometry.outputs['Position'],scale.inputs[0])
            m.node_tree.links.new(scale.outputs[0],detail.inputs['Vector'])
            weighted=m.node_tree.nodes.new('ShaderNodeVectorMath');weighted.operation='SCALE'
            m.node_tree.links.new(detail.outputs['Color'],weighted.inputs[0])
            m.node_tree.links.new(channels.outputs[index],weighted.inputs['Scale'])
            if accumulated is None:
                accumulated=weighted.outputs[0]
            else:
                add=m.node_tree.nodes.new('ShaderNodeVectorMath');add.operation='ADD'
                m.node_tree.links.new(accumulated,add.inputs[0]);m.node_tree.links.new(weighted.outputs[0],add.inputs[1])
                accumulated=add.outputs[0]
        strength=m.node_tree.nodes.new('ShaderNodeVectorMath');strength.operation='SCALE'
        strength.inputs['Scale'].default_value=mat['shader_properties']['magicMult']
        m.node_tree.links.new(accumulated,strength.inputs[0])
        multiply=m.node_tree.nodes.new('ShaderNodeVectorMath');multiply.operation='MULTIPLY'
        m.node_tree.links.new(texture.outputs['Color'],multiply.inputs[0]);m.node_tree.links.new(strength.outputs[0],multiply.inputs[1])
        m.node_tree.links.new(multiply.outputs[0],bsdf.inputs['Base Color'])
    if 'albedo_multiplier' in mat:
        # Match the intentional ambient/diffuse material tint used in KN5;
        # the underlying photographic source stays unchanged.
        socket=bsdf.inputs['Base Color']
        source_color=socket.links[0].from_socket
        tint=m.node_tree.nodes.new('ShaderNodeVectorMath');tint.operation='MULTIPLY'
        tint.inputs[1].default_value=tuple(mat['albedo_multiplier'])
        m.node_tree.links.new(source_color,tint.inputs[0])
        m.node_tree.links.new(tint.outputs[0],socket)
    bsdf.inputs['Roughness'].default_value=mat.get('roughness',.85)
    if mat.get('alpha_test'):
        cutoff=m.node_tree.nodes.new('ShaderNodeMath');cutoff.operation='GREATER_THAN'
        cutoff.inputs[1].default_value=mat.get('alpha_ref',.5)
        m.node_tree.links.new(texture.outputs['Alpha'],cutoff.inputs[0])
        m.node_tree.links.new(cutoff.outputs[0],bsdf.inputs['Alpha'])
    if mat.get('normal'):
        normal_image=bpy.data.images.load(str(ROOT/scene['textures_dir']/mat['normal']),check_existing=True)
        normal_image.colorspace_settings.name='Non-Color'
        normal_texture=m.node_tree.nodes.new('ShaderNodeTexImage')
        normal_texture.image=normal_image
        normal_map=m.node_tree.nodes.new('ShaderNodeNormalMap')
        normal_map.inputs['Strength'].default_value=mat.get('normal_strength',1.)
        m.node_tree.links.new(mapping.outputs[0],normal_texture.inputs['Vector'])
        # AC uses DirectX normals; Blender requires the inverted green channel.
        dx_to_gl=m.node_tree.nodes.new('ShaderNodeVectorMath');dx_to_gl.operation='MULTIPLY_ADD'
        dx_to_gl.inputs[1].default_value=(1,-1,1);dx_to_gl.inputs[2].default_value=(0,1,0)
        m.node_tree.links.new(normal_texture.outputs['Color'],dx_to_gl.inputs[0])
        m.node_tree.links.new(dx_to_gl.outputs[0],normal_map.inputs['Color'])
        m.node_tree.links.new(normal_map.outputs['Normal'],bsdf.inputs['Normal'])
    m['ac_shader']=mat.get('shader') or ('ksPerPixelAT' if mat.get('alpha_test') else 'ksPerPixelNM' if mat.get('normal') else 'ksPerPixel')
    materials.append(m)
for mesh in scene['meshes']:
    vertices=[(p[0],-p[2],p[1]) for p in mesh['vertices']]
    data=bpy.data.meshes.new(mesh['name'])
    data.from_pydata(vertices,[],mesh['faces'])
    data.update()
    obj=bpy.data.objects.new(mesh['name'],data)
    bpy.context.collection.objects.link(obj)
    obj.data.materials.append(materials[mesh['material']])
    layer=data.uv_layers.new(name='UVMap')
    for poly in data.polygons:
        for index in poly.loop_indices:
            layer.data[index].uv=mesh['uv'][data.loops[index].vertex_index]
    if any(part in obj.name for part in ('ROAD','TERRAIN','SHOULDER','TREE_TRUNKS','TREE_BRANCHES','TREE_LEAVES','SHRUB_LEAVES')):
        for face in data.polygons:
            face.use_smooth=True
    if 'normals' in mesh:
        data.normals_split_custom_set_from_vertices([(n[0],-n[2],n[1]) for n in mesh['normals']])
    obj['prototype_collision']=mesh['collision']
for marker in scene['markers']:
    obj=bpy.data.objects.new(marker['name'],None)
    bpy.context.collection.objects.link(obj)
    ac=Matrix(marker['matrix']).transposed()
    basis=Matrix(((1,0,0,0),(0,0,-1,0),(0,1,0,0),(0,0,0,1)))
    obj.matrix_world=basis@ac@basis.inverted()
    obj.empty_display_type='ARROWS';obj.empty_display_size=1.2
world=bpy.data.worlds.new('Daylight')
bpy.context.scene.world=world
world.use_nodes=True
world.node_tree.nodes['Background'].inputs[0].default_value=(.65,.78,1.,1)
world.node_tree.nodes['Background'].inputs[1].default_value=.7
bpy.ops.object.light_add(type='SUN',location=(100,-100,200))
sun=bpy.context.object;sun.name='Preview sunlight';sun.rotation_euler=(math.radians(30),math.radians(-20),math.radians(-30));sun.data.energy=2
sun.data.angle=math.radians(.7)
# A physical sky supplies directional daylight and a horizon gradient. This
# lights the actual exported materials; screenshots receive no image editing.
sky=world.node_tree.nodes.new('ShaderNodeTexSky');sky.sky_type='NISHITA';sky.sun_disc=False
direction=sun.rotation_euler.to_quaternion()@Vector((0,0,1))
sky.sun_elevation=math.asin(direction.z);sky.sun_rotation=math.atan2(direction.y,direction.x)
sky.air_density=1.;sky.dust_density=.6;sky.ozone_density=1.
world.node_tree.links.new(sky.outputs['Color'],world.node_tree.nodes['Background'].inputs['Color'])
world.node_tree.nodes['Background'].inputs['Strength'].default_value=.35
center=scene['metadata']['centerline_ac']
k=len(center)//2
p=Vector((center[k][0],-center[k][2],center[k][1]))
bpy.ops.object.camera_add(location=p+Vector((45,-65,65)))
camera=bpy.context.object;camera.name='Preview camera'
camera.rotation_euler=(p-camera.location).to_track_quat('-Z','Y').to_euler()
camera.data.lens=32
if options.view=='driver':
    distances=[0.]
    for a,b in zip(center,center[1:]):
        distances.append(distances[-1]+math.hypot(b[0]-a[0],b[2]-a[2]))
    k=min(range(len(center)),key=lambda i:abs(distances[i]-options.distance))
    look=min(len(center)-1,k+22)
    camera.location=Vector((center[k][0],-center[k][2],center[k][1]+1.6))
    target=Vector((center[look][0],-center[look][2],center[look][1]+1.6))
    camera.rotation_euler=(target-camera.location).to_track_quat('-Z','Y').to_euler()
    camera.data.lens=28
sc=bpy.context.scene;sc.camera=camera
sc.render.engine='CYCLES'
if options.gpu:
    preferences=bpy.context.preferences.addons['cycles'].preferences
    for device_type in ('OPTIX','CUDA'):
        try:
            preferences.compute_device_type=device_type;preferences.get_devices()
            available=[d for d in preferences.devices if d.type==device_type]
            if available:
                for device in preferences.devices:device.use=device in available
                sc.cycles.device='GPU'
                print('Preview renderer:',device_type,','.join(d.name for d in available))
                break
        except (TypeError,RuntimeError):
            continue
sc.cycles.samples=max(8,min(options.samples,256))
sc.cycles.use_denoising=True
sc.render.resolution_x=1280;sc.render.resolution_y=720;sc.render.resolution_percentage=100
sc.view_settings.view_transform='AgX'
sc.render.image_settings.file_format='PNG'
sc.render.filepath=str(options.output.resolve() if options.output else track/'ui/preview.png')
if not options.preview_only:
    bpy.ops.wm.save_as_mainfile(filepath=str(source/'altenhain_langstrasse.blend'))
    bpy.ops.file.pack_all()
    bpy.ops.wm.save_as_mainfile(filepath=str(source/'altenhain_langstrasse.blend'))
bpy.ops.render.render(write_still=True)
print('EDITABLE SOURCE:',source/'altenhain_langstrasse.blend')
