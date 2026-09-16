#!/usr/bin/env python3
"""Build the acquisition suite and offline recording viewer."""
import os
import subprocess
import sys
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from cbrain_studio.suite import VERSION
APPS=[('CBRAIN Device Setup','device_setup.py'),('CBRAIN Studio','studio_gui.py'),('CBRAIN Firmware Builder','fwbuilder_app.py'),('CBRAIN Recording Viewer','recording_viewer.py')]
for name,entry in APPS:
    if len(sys.argv)>1 and sys.argv[1].lower() not in name.lower():continue
    args=[sys.executable,'-m','PyInstaller','--noconfirm','--clean','--windowed','--name',name,'--osx-bundle-identifier','org.cbrain.'+entry.removesuffix('.py').replace('_','-'),'--paths',str(ROOT),'--distpath',str(ROOT/'dist_apps'),'--workpath',str(ROOT/'build/suite'),'--specpath',str(ROOT/'build/suite'),'--add-data',str(ROOT/'app/firmware/headstage_common.hex')+os.pathsep+'firmware','--add-data',str(ROOT/'app/firmware/dongle_bridge.hex')+os.pathsep+'firmware','--hidden-import','serial.tools.list_ports','--collect-all','h5py']
    for mod in ['bleak','CoreBluetooth','objc','PyQt5','PyQt6','matplotlib','IPython','pytest','tkinter']:
        args+=['--exclude-module',mod]
    args.append(str(ROOT/'tools'/entry))
    subprocess.run(args,cwd=ROOT,check=True)
    if sys.platform=='darwin':
        import plistlib
        plist=ROOT/'dist_apps'/(name+'.app')/'Contents/Info.plist'
        info=plistlib.loads(plist.read_bytes());info['CFBundleShortVersionString']=VERSION;info['CFBundleVersion']='20260916.8'
        if entry=='recording_viewer.py':
            info['CFBundleDocumentTypes']=[{'CFBundleTypeName':'CBRAIN HDF5 Recording','CFBundleTypeExtensions':['h5','hdf5'],'CFBundleTypeRole':'Viewer','LSHandlerRank':'Alternate'}]
        info['NSHighResolutionCapable']=True;plist.write_bytes(plistlib.dumps(info))
        subprocess.run(['codesign','--force','--deep','--sign','-',str(plist.parents[1])],check=True)
    print('Built:',name,flush=True)
