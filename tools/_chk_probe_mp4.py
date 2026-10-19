# -*- coding: utf-8 -*-
import sys, os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from unc_atomic import md5_of
a = md5_of(r"D:\D抖音下载\复古造型老爹鞋_打造休闲运动风_U_S_POLO_ASSN_官方品牌_春夏上新.mp4")
b = md5_of(r"\\Sphgj\sphgj\data\_pubtest\probe.mp4")
print("src", a[:12], "dst", b[:12], "MATCH" if a == b else "DIFF")
