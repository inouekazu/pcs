#!/usr/bin/python3
import sys
import os

sys.path.insert(
    0,
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "pcs")
)
import settings


pcs_version = settings.pcs_version
print(os.system("cp dist/pcs-"+pcs_version+".tar dist/pcs-withgems-"+pcs_version+".tar"))
print(os.system("tar --delete -f dist/pcs-"+pcs_version+".tar '*/pcsd/vendor'"))
print(os.system("gzip dist/pcs-"+pcs_version+".tar"))
print(os.system("gzip dist/pcs-withgems-"+pcs_version+".tar"))
